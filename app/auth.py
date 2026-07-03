"""Authentication: FreeIPA LDAP login + signed session cookies.

Prototype scope:
  - Login form (username/password) verified by LDAP bind against FreeIPA.
  - On success we capture the user's numeric UID/GID and supplementary group
    GIDs, because those numbers are what the aclcheckd helper needs to
    impersonate the user for filesystem-level access checks (mirroring what
    the kernel/NFS server would do for an ssh session).
  - Session is a signed cookie (itsdangerous). The future Keycloak/OIDC swap
    replaces only this module's login path; the Principal shape and the
    current_principal dependency stay the same.

AuthN happens here; AuthZ (per-path read/list checks) lives in app/authz.py.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass

from fastapi import HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

log = logging.getLogger("wsi-auth")

COOKIE_NAME = "wsi_session"


@dataclass
class Principal:
    """The authenticated user, as the authz layer needs it.

    uid/gid are numeric POSIX IDs pulled from FreeIPA. The aclcheckd helper
    switches to exactly these before testing read/list access, so the kernel
    evaluates the NFSv4 ACL as this user — identical to an ssh login.

    We do NOT carry supplementary groups: the NFS server resolves group
    membership server-side from the UID (bypassing the 16-group limit), so
    impersonating with just uid+primary gid is sufficient and correct.
    """
    username: str
    uid: int
    gid: int

    def to_token(self) -> dict:
        d = asdict(self)
        d["exp"] = int(time.time())  # informational; serializer enforces real TTL
        return d


class Authenticator:
    def __init__(self, cfg):
        self.cfg = cfg
        secret = cfg.session_secret
        if not secret:
            # Fail loudly at startup if auth is enabled without a secret.
            if cfg.enabled:
                raise RuntimeError("auth.enabled=true but no session_secret "
                                   "(set WSI_SESSION_SECRET or auth.session_secret)")
            secret = "insecure-dev-secret"
        self._ser = URLSafeTimedSerializer(secret, salt="wsi-session")

    # ---- session cookie -------------------------------------------------
    def issue_cookie(self, response: Response, principal: Principal) -> None:
        token = self._ser.dumps(principal.to_token())
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=self.cfg.session_ttl,
            httponly=True,
            samesite="lax",
            secure=False,  # behind a TLS-terminating reverse proxy in prod; prototype
            path="/",
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(COOKIE_NAME, path="/")

    def _read_principal(self, request: Request) -> Principal | None:
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return None
        try:
            data = self._ser.loads(token, max_age=self.cfg.session_ttl)
        except SignatureExpired:
            return None
        except BadSignature:
            return None
        try:
            return Principal(
                username=data["username"],
                uid=int(data["uid"]),
                gid=int(data["gid"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


# --------------------------------------------------------------------------- #
# LDAP login
def _ldap_login(cfg, username: str, password: str) -> Principal | None:
    """Bind to FreeIPA as the user; on success harvest uid/gid.

    Returns None on auth failure or any LDAP error (fail closed).
    """
    import time as _t
    t0 = _t.time()
    try:
        from ldap3 import ALL, Connection, Server, Tls
        import ssl
    except Exception as e:  # ldap3 optional at import time
        log.error("ldap3 not available: %s", e)
        return None

    if not username or not password:
        return None

    user_dn = cfg.user_dn_template.format(username=username)
    log.info("LDAP[%.2fs] user_dn=%s", _t.time()-t0, user_dn)
    # ldaps:// -> implicit TLS (use_ssl=True). ldap:// -> StartTLS: we open the
    # connection plain, then upgrade with start_tls() before binding. Either way
    # credentials only go over an encrypted channel.
    use_ldaps = cfg.ldap_url.startswith("ldaps")
    server = Server(cfg.ldap_url, use_ssl=use_ldaps,
                    tls=Tls(validate=ssl.CERT_REQUIRED), get_info=ALL)

    try:
        # 1) Bind as the user — this IS the password check.
        #    For StartTLS we must NOT auto_bind (that would bind in the clear);
        #    open plain, upgrade, then bind.
        conn = Connection(server, user=user_dn, password=password,
                          auto_bind=use_ldaps, receive_timeout=10)
        log.info("LDAP[%.2fs] connection object created (use_ldaps=%s)", _t.time()-t0, use_ldaps)
        if not use_ldaps:
            log.info("LDAP[%.2fs] start_tls...", _t.time()-t0)
            if not conn.start_tls():
                log.warning("LDAP[%.2fs] StartTLS failed for %r", _t.time()-t0, username)
                conn.unbind()
                return None
            log.info("LDAP[%.2fs] start_tls ok, binding...", _t.time()-t0)
            conn.bind()
            log.info("LDAP[%.2fs] bind done, conn.bound=%s", _t.time()-t0, conn.bound)
        # bind() returns falsy (without raising) on invalid credentials. We MUST
        # check conn.bound explicitly — otherwise an unbound/anonymous session
        # can still read the public user entry and we'd issue a cookie for a
        # failed login (auth bypass).
        if not conn.bound:
            log.warning("LDAP[%.2fs] bind rejected for %r (invalid credentials)",
                        _t.time()-t0, username)
            conn.unbind()
            return None
    except Exception as e:
        log.warning("LDAP[%.2fs] bind failed for %r: %s", _t.time()-t0, username, e)
        return None

    try:
        # 2) Read uidNumber/gidNumber from the user entry. We do NOT read
        #    memberOf/groups: the NFS server resolves group membership
        #    server-side from the UID, so aclcheckd only needs uid+primary gid.
        log.info("LDAP[%.2fs] searching user entry...", _t.time()-t0)
        if not conn.search(user_dn, "(objectClass=*)",
                           attributes=["uidNumber", "gidNumber"]):
            log.warning("LDAP[%.2fs] could not read user entry %s", _t.time()-t0, user_dn)
            return None
        entry = conn.entries[0]
        uid = int(entry.uidNumber.value)
        gid = int(entry.gidNumber.value)
        log.info("LDAP[%.2fs] got uid=%d gid=%d", _t.time()-t0, uid, gid)
        return Principal(username=username, uid=uid, gid=gid)
    except Exception as e:
        log.exception("LDAP[%.2fs] attribute read failed for %r: %s", _t.time()-t0, username, e)
        return None
    finally:
        try:
            conn.unbind()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# FastAPI dependency
def make_current_principal(authenticator: Authenticator):
    """Build the `current_principal` dependency bound to a configured Authenticator."""
    def _dep(request: Request) -> Principal:
        if not authenticator.cfg.enabled:
            # Legacy/dev mode: anonymous full access. No principal attached.
            request.state.principal = None
            return None  # type: ignore[return-value]
        principal = authenticator._read_principal(request)
        if principal is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Not authenticated")
        request.state.principal = principal
        return principal
    return _dep
