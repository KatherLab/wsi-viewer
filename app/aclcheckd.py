"""aclcheckd — privileged access-check daemon.

Runs as root so it can switch its credentials to an arbitrary user (by numeric
UID/GID) and ask the kernel/NFS server whether that user could read or list a
path — exactly the decision an ssh session would get. The FastAPI app is
non-root and talks to this daemon over a Unix socket (app/authz.py).

Why a separate root daemon + fork-per-check:
  - setuid/setgid are process-wide credentials; a multithreaded uvicorn cannot
    safely impersonate different users per request. Forking isolates each check.
  - Confining the privileged surface to one small, auditable process is safer
    than running the whole app as root.

Protocol (line-delimited JSON, one request -> one response per connection):
  Request:  {"op": "read"|"listdir", "uid": int, "gid": int, "path": "/abs/path"}
  Response: {"ok": bool, "err": str|null, "entries": [str,...]|null}

Fail-closed: any timeout, exception, or malformed request -> ok=false.
"""
from __future__ import annotations

import json
import logging
import os
import socketserver
import sys
from pathlib import Path

log = logging.getLogger("aclcheckd")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s aclcheckd %(levelname)s %(message)s")

CHECK_TIMEOUT = 5.0  # seconds the child may take


def _drop_privs(uid: int, gid: int) -> None:
    """Switch this process's credentials to the given user.

    Order matters: set groups/gid first while we still have setgid privilege,
    then drop uid last. After setuid we cannot regain root.

    We set supplementary groups to just the primary gid — NOT the user's real
    supplementary groups. The NFS server resolves group membership server-side
    from the UID (bypassing the 16-group limit), so we don't need them.
    Clearing (rather than leaving root's groups) keeps the impersonation clean.
    """
    os.setgroups([gid])
    os.setgid(gid)
    os.setuid(uid)


def _check_read(path: str) -> dict:
    """As the impersonated user: can we open this file for reading?

    os.access(R_OK) can over-report on NFS, so we also do a real open() — the
    same syscall OpenSlide will make. An EACCES here is authoritative.
    """
    p = Path(path)
    if not p.exists():
        # Distinguish "doesn't exist" from "no access" — caller treats both as
        # not-accessible, but err helps logging.
        return {"ok": False, "err": "noent", "entries": None}
    if not os.access(path, os.R_OK):
        return {"ok": False, "err": "eacces", "entries": None}
    try:
        fd = os.open(path, os.O_RDONLY)
        os.close(fd)
    except PermissionError:
        return {"ok": False, "err": "eacces", "entries": None}
    except OSError as e:
        return {"ok": False, "err": f"oserr:{e.errno}", "entries": None}
    return {"ok": True, "err": None, "entries": None}


def _check_listdir(path: str) -> dict:
    """As the impersonated user: list directory entries we can see.

    The kernel/NFS filters traversal on the caller's credentials, so a per-user
    scandir is the correct per-user listing (denied subtrees simply don't appear
    or raise EACCES, which we treat as no-access for that entry).

    Returns entries as [{name, is_dir}] so the app can build its Node tree
    without a second round-trip per entry.
    """
    try:
        out = []
        with os.scandir(path) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    # Entry we can't stat: skip it (treat as invisible to user).
                    continue
                out.append({"name": entry.name, "is_dir": is_dir})
    except PermissionError:
        return {"ok": False, "err": "eacces", "entries": None}
    except OSError as e:
        return {"ok": False, "err": f"oserr:{e.errno}", "entries": None}
    return {"ok": True, "err": None, "entries": out}


def _run_check(uid: int, gid: int, op: str, path: str) -> dict:
    """Drop privileges to the given user, then run the requested check.

    Must be called in a forked child (we use ForkingMixIn, so each connection
    handler IS a child). setuid is irreversible, which is fine — the child exits
    after one request. Forking from a multithreaded parent would risk deadlocks,
    so the server is fork-per-connection, not threaded.
    """
    try:
        _drop_privs(uid, gid)
        if op == "read":
            return _check_read(path)
        if op == "listdir":
            return _check_listdir(path)
        return {"ok": False, "err": f"badop:{op}", "entries": None}
    except Exception as e:  # fail closed
        return {"ok": False, "err": f"exc:{type(e).__name__}", "entries": None}


class _Handler(socketserver.StreamRequestHandler):
    # Per-connection timeout. The client (authz.py) also has its own timeout,
    # but this bounds how long a single forked handler lingers.
    timeout = CHECK_TIMEOUT

    def handle(self):
        line = self.rfile.readline()
        if not line:
            return
        try:
            req = json.loads(line)
            uid = int(req["uid"])
            gid = int(req["gid"])
            op = req["op"]
            path = req["path"]
        except Exception as e:
            self._send({"ok": False, "err": f"badreq:{e}", "entries": None})
            return

        # Sanity: path must be absolute (the app already enforces under-root).
        if not os.path.isabs(path):
            self._send({"ok": False, "err": "relpath", "entries": None})
            return

        result = _run_check(uid, gid, op, path)
        self._send(result)

    def _send(self, obj: dict):
        self.wfile.write((json.dumps(obj) + "\n").encode())
        self.wfile.flush()


class _Server(socketserver.ForkingMixIn, socketserver.UnixStreamServer):
    # Fork per connection so each handler can safely setuid without affecting
    # siblings or the listener. Reap children promptly.
    max_children = 32
    allow_reuse_address = True


def serve(socket_path: str):
    sp = Path(socket_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    if sp.exists():
        sp.unlink()

    server = _Server(str(sp), _Handler)
    # The app runs as UID 1000 (wsi); aclcheckd runs as root. Make the socket
    # group-owned by wsi and group-rw so the app can connect, but not others.
    try:
        os.chown(str(sp), 0, 1000)
        os.chmod(str(sp), 0o660)
    except PermissionError:
        # Not root (e.g. local test run) — best-effort chmod only.
        os.chmod(str(sp), 0o660)
    log.info("aclcheckd listening on %s (uid=%d, mode=660, group=1000)", sp, os.getuid())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sp.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    sock = sys.argv[1] if len(sys.argv) > 1 else "/run/wsi/aclcheck.sock"
    serve(sock)
