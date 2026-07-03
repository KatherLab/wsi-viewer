from __future__ import annotations
from pathlib import Path
from pydantic import BaseModel
from pydantic_settings import BaseSettings
import yaml


DEFAULT_EXTS = [".svs", ".tif", ".tiff", ".ndpi", ".scn", ".mrxs", ".bif", ".czi", ".dcm", ".vms", ".vmu", ".svslide", ".qptiff"]


class CacheCfg(BaseModel):
    enabled: bool = False
    redis_url: str | None = None
    ttl_seconds: dict = {"tree": 60, "thumb": 86400, "tile": 3600}


class RootCfg(BaseModel):
    path: Path
    label: str


class ThumbCfg(BaseModel):
    max_px: int = 512
    prefer_associated: bool = True


class AuthCfg(BaseModel):
    # Master switch. When False, ALL requests are treated as anonymous-full-access
    # (legacy/dev mode). When True, every data endpoint requires a valid session
    # and passes a per-user authz check.
    enabled: bool = False
    ldap_url: str = "ldaps://ipa.example.com"
    # Bind as this DN template, {username} substituted. FreeIPA default layout.
    user_dn_template: str = "uid={username},cn=users,cn=accounts,dc=example,dc=com"
    # Session cookie signing secret. MUST be set when enabled=true.
    session_secret: str | None = None
    session_ttl: int = 86400
    # Redis TTLs for authz decisions (seconds).
    authz_ttl: int = 300
    authz_ttl_deny: int = 60
    # Unix socket for the aclcheckd daemon.
    acl_socket: str = "/run/wsi/aclcheck.sock"
    # Per-check timeout talking to aclcheckd.
    check_timeout: float = 5.0


class AppCfg(BaseSettings):
    roots: list[RootCfg]
    exclude: list[str] = []
    extensions: list[str] = DEFAULT_EXTS
    cache: CacheCfg = CacheCfg()
    thumbnails: ThumbCfg = ThumbCfg()
    cors_allow_origins: list[str] = ["*"]
    auth: AuthCfg = AuthCfg()


    @staticmethod
    def load(path: Path) -> "AppCfg":
        data = yaml.safe_load(path.read_text())
        # normalize extensions
        exts = [e.lower() if e.startswith(".") else f".{e.lower()}" for e in data.get("extensions", DEFAULT_EXTS)]
        data["extensions"] = exts
        # coerce paths
        for r in data.get("roots", []):
            r["path"] = str(Path(r["path"]).expanduser().resolve())
        return AppCfg(**data)