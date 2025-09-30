from __future__ import annotations
from pathlib import Path
from pydantic import BaseModel
from pydantic_settings import BaseSettings
import yaml


DEFAULT_EXTS = [".svs", ".tif", ".tiff", ".ndpi", ".scn", ".mrxs", ".bif", ".czi", ".dcm", ".vms", ".vmu", ".svslide"]


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


class AppCfg(BaseSettings):
    roots: list[RootCfg]
    exclude: list[str] = []
    extensions: list[str] = DEFAULT_EXTS
    cache: CacheCfg = CacheCfg()
    thumbnails: ThumbCfg = ThumbCfg()
    cors_allow_origins: list[str] = ["*"]


    @staticmethod
    def load(path: Path) -> "AppCfg":
        data = yaml.safe_load(path.read_text())
        # normalize extensions
        exts = [e.lower() if e.startswith(".") else f".{e.lower()}" for e in data.get("extensions", DEFAULT_EXTS)]
        data["extensions"] = exts
        # coerce paths
        for r in data.get("roots", []):
            r["path"] = str(Path(r["path"]).resolve())
        return AppCfg(**data)