from __future__ import annotations
import hashlib
from dataclasses import dataclass
from typing import Any

try:
    import redis
except Exception:  # redis optional
    redis = None

@dataclass
class Cache:
    client: Any | None
    ttl_tree: int
    ttl_thumb: int
    ttl_tile: int

    @staticmethod
    def noop(ttl_tree=60, ttl_thumb=86400, ttl_tile=3600) -> "Cache":
        return Cache(None, ttl_tree, ttl_thumb, ttl_tile)

    def get(self, k: str) -> bytes | None:
        if not self.client:
            return None
        return self.client.get(k)

    def setex(self, k: str, ttl: int, v: bytes):
        if not self.client:
            return
        self.client.setex(k, ttl, v)

    @staticmethod
    def key(*parts: str) -> str:
        s = "|".join(parts)
        if len(s) > 100:  # keep keys short
            s = hashlib.sha1(s.encode()).hexdigest()
        return s


def make_cache(cfg) -> Cache:
    if not (cfg.cache.enabled and cfg.cache.redis_url and redis):
        return Cache.noop(
            cfg.cache.ttl_seconds.get("tree", 60),
            cfg.cache.ttl_seconds.get("thumb", 86400),
            cfg.cache.ttl_seconds.get("tile", 3600),
        )
    client = redis.from_url(cfg.cache.redis_url)
    return Cache(
        client,
        cfg.cache.ttl_seconds.get("tree", 60),
        cfg.cache.ttl_seconds.get("thumb", 86400),
        cfg.cache.ttl_seconds.get("tile", 3600),
    )