# path_cache.py
from __future__ import annotations
import json
import os
from pathlib import Path
from collections import OrderedDict
from typing import Optional, Iterable, Tuple
import logging

log = logging.getLogger(__name__)


class LRU:
    def __init__(self, cap: int = 100_000):
        self.cap = cap
        self._od: OrderedDict[str, str] = OrderedDict()

    def get(self, k: str) -> Optional[str]:
        if k in self._od:
            v = self._od.pop(k)
            self._od[k] = v
            return v
        return None

    def set(self, k: str, v: str):
        if k in self._od:
            self._od.pop(k)
        self._od[k] = v
        if len(self._od) > self.cap:
            self._od.popitem(last=False)

    # ✅ Fix #15: Proper delete method
    def pop(self, k: str):
        self._od.pop(k, None)

    def items(self):
        return self._od.items()

    def __len__(self):
        return len(self._od)


class PathCache:
    """
    Shared path cache with Redis primary (if available) and local LRU read-through.
    Keyspace: HSET {ns} slide_id -> absolute path
    """
    def __init__(self, redis_client, namespace: str, cache_file: Path, lru_cap: int = 100_000):
        self.r = redis_client  # may be None
        self.ns = namespace
        self.lru = LRU(lru_cap)
        # ✅ Fix #12: Use JSON instead of pickle for safe deserialization
        self.cache_file = cache_file.with_suffix(".json")

    # ------- Reads / writes
    def get(self, slide_id: str) -> Optional[Path]:
        # 1) LRU
        local = self.lru.get(slide_id)
        if local:
            p = Path(local)
            if p.exists():
                return p
            else:
                self.delete(slide_id)

        # 2) Redis
        if self.r:
            val = self.r.hget(self.ns, slide_id)
            if val:
                p = Path(val.decode("utf-8"))
                if p.exists():
                    self.lru.set(slide_id, str(p))
                    return p
                else:
                    self.r.hdel(self.ns, slide_id)
                    return None

        return None

    def set(self, slide_id: str, path: Path):
        s = str(path)
        self.lru.set(slide_id, s)
        if self.r:
            try:
                self.r.hset(self.ns, slide_id, s)
            except Exception:
                pass

    def delete(self, slide_id: str):
        # ✅ Fix #15: Actually remove from LRU
        self.lru.pop(slide_id)
        if self.r:
            try:
                self.r.hdel(self.ns, slide_id)
            except Exception:
                pass

    def mset(self, pairs: Iterable[Tuple[str, str]]):
        for k, v in pairs:
            self.lru.set(k, v)
        if self.r:
            try:
                pipe = self.r.pipeline()
                for k, v in pairs:
                    pipe.hset(self.ns, k, v)
                pipe.execute()
            except Exception:
                pass

    # ------- Persistence fallback (JSON) only when Redis is off
    def load_pickle(self):
        """Load from JSON file (name kept for backward compat)."""
        if self.r:
            return
        if self.cache_file.exists():
            try:
                with open(self.cache_file, "r") as f:
                    data = json.load(f)
                for k, v in data.items():
                    self.lru.set(k, v)
                log.info(f"Loaded {len(data)} entries from path cache: {self.cache_file}")
            except Exception as e:
                log.warning(f"Failed to load path cache from {self.cache_file}: {e}")

    def save_pickle(self):
        """Save to JSON file (name kept for backward compat)."""
        if self.r:
            return
        try:
            data = dict(self.lru.items())
            # Write to a temp file first, then rename for atomicity
            tmp = self.cache_file.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump(data, f)
            tmp.rename(self.cache_file)
            log.info(f"Saved {len(data)} entries to path cache: {self.cache_file}")
        except Exception as e:
            log.warning(f"Failed to save path cache to {self.cache_file}: {e}")
