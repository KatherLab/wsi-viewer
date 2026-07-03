"""Authorization client: per-user read/list checks via aclcheckd, with caching.

The authz decision (can this user read this path?) is cached in Redis keyed by
uid + path, with a short TTL. The cache is uid-scoped so there is no cross-user
leakage. Denied results are cached too (shorter TTL) to avoid hammering denied
paths, but expire quickly so newly-granted access propagates.

Fail-closed: if auth is enabled and aclcheckd is unreachable, times out, or
errors, the check returns False. Better to deny than to leak a gated slide.
"""
from __future__ import annotations

import hashlib
import json
import logging
import socket
from typing import Optional

from .auth import Principal

log = logging.getLogger("wsi-authz")


def _path_hash(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()[:16]


class Authz:
    def __init__(self, cfg, cache):
        self.cfg = cfg
        self.cache = cache  # app.cache.Cache (Redis or noop)
        self.socket_path = cfg.acl_socket
        self.timeout = cfg.check_timeout

    # ---- low-level socket call ------------------------------------------
    def _call_helper(self, principal: Principal, op: str, path: str) -> Optional[dict]:
        # No supplementary groups: the NFS server resolves group membership
        # server-side from the UID. We impersonate with uid + primary gid only.
        req = {
            "op": op,
            "uid": principal.uid,
            "gid": principal.gid,
            "path": path,
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(self.socket_path)
                s.sendall((json.dumps(req) + "\n").encode())
                # Read one line.
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            return json.loads(buf.decode())
        except Exception as e:
            log.warning("aclcheckd call failed (op=%s path=%s): %s", op, path, e)
            return None

    # ---- public API -----------------------------------------------------
    def can_read(self, principal: Optional[Principal], path: str) -> bool:
        """True iff `principal` may open `path` for reading.

        None principal (auth disabled) -> allow (legacy/dev mode).
        """
        if principal is None:
            return True

        ck = f"wsi:authz|r|{principal.uid}|{_path_hash(path)}"
        cached = self._cache_get(ck)
        if cached is not None:
            return cached == b"1"

        resp = self._call_helper(principal, "read", path)
        ok = bool(resp and resp.get("ok"))
        # Cache allows and genuine denials. A transient helper failure
        # (socket/timeout) is NOT cached, so the next request retries rather
        # than sticking on a false denial.
        err = resp.get("err") if resp else "nohelper"
        if ok:
            self._cache_set(ck, b"1", self.cfg.authz_ttl)
        elif resp and err in ("eacces", "oserr:13", "noent"):
            self._cache_set(ck, b"0", self.cfg.authz_ttl_deny)
        if not ok:
            log.info("authz DENY read uid=%s path=%s err=%s cached=%s",
                     principal.uid, path, err,
                     ok or err in ("eacces", "oserr:13", "noent"))
        return ok

    def list_dir(self, principal: Optional[Principal], path: str) -> Optional[list[str]]:
        """Return the entry names `principal` can see in `path`.

        Returns:
          - a list (possibly empty) when the user may list the directory
          - None when auth is disabled (caller falls back to service-user scan)
        Raises PermissionError when the user is explicitly denied (EACCES), so
        endpoints can return 403 rather than rendering an empty grid.
        """
        if principal is None:
            return None

        ck = f"wsi:authz|l|{principal.uid}|{_path_hash(path)}"
        cached = self._cache_get(ck)
        if cached is not None:
            try:
                val = json.loads(cached)
            except Exception:
                val = None
            if val is not None:
                # Sentinel "__denied__" means a cached real denial.
                if val == ["__denied__"]:
                    raise PermissionError("access denied")
                return val

        resp = self._call_helper(principal, "listdir", path)
        if not resp or not resp.get("ok"):
            # Distinguish a genuine access denial from a transient helper error
            # (socket problem, timeout). A real EACCES from the kernel is cached
            # (short TTL); a helper/transport failure is NOT cached, so the next
            # request retries instead of sticking on an empty listing.
            err = resp.get("err") if resp else "nohelper"
            is_real_deny = bool(resp and err in ("eacces", "oserr:13"))
            if is_real_deny:
                # Cache the denial as a sentinel; raise so callers can 403.
                self._cache_set(ck, json.dumps(["__denied__"]).encode(),
                                self.cfg.authz_ttl_deny)
                log.info("authz DENY listdir uid=%s path=%s err=%s",
                         principal.uid, path, err)
                raise PermissionError("access denied")
            log.info("authz listdir transient fail uid=%s path=%s err=%s (not cached)",
                     principal.uid, path, err)
            # Transient helper failure -> treat as empty (don't cache) so it
            # retries next time. Not a denial, so no PermissionError.
            return []
        entries = resp.get("entries") or []
        self._cache_set(ck, json.dumps(entries).encode(), self.cfg.authz_ttl)
        return entries

    # ---- cache helpers (tolerant of noop cache) -------------------------
    def _cache_get(self, key: str):
        try:
            return self.cache.get(key)
        except Exception:
            return None

    def _cache_set(self, key: str, value: bytes, ttl: int):
        try:
            self.cache.setex(key, ttl, value)
        except Exception:
            pass
