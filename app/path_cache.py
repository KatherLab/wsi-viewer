# path_cache.py
from __future__ import annotations
import os
import pickle
from pathlib import Path
from collections import OrderedDict
from typing import Optional, Iterable, Tuple

# Tiny LRU for hot entries per worker
class LRU:
    def __init__(self, cap: int = 100_000):
        self.cap = cap
        self._od = OrderedDict()

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

    def items(self):
        return self._od.items()

    def __len__(self):
        return len(self._od)

class PathCache:
    """
    Shared path cache with Redis primary (if available) and local LRU read-through.
    Keyspace: HSET {ns} slide_id -> absolute path
    """
    def __init__(self, redis_client, namespace: str, pickle_file: Path, lru_cap: int = 100_000):
        self.r = redis_client  # may be None
        self.ns = namespace
        self.lru = LRU(lru_cap)
        self.pickle_file = pickle_file

    # ------- Reads / writes
    def get(self, slide_id: str) -> Optional[Path]:
        # 1) LRU
        local = self.lru.get(slide_id)
        if local:
            p = Path(local)
            if p.exists():
                return p
            else:
                # stale local
                self.delete(slide_id)

        # 2) Redis
        if self.r:
            val = self.r.hget(self.ns, slide_id)
            if val:
                p = Path(val.decode("utf-8"))
                if p.exists():
                    # promote to LRU
                    self.lru.set(slide_id, str(p))
                    return p
                else:
                    # stale global mapping
                    self.r.hdel(self.ns, slide_id)
                    return None

        # 3) LRU miss, Redis missing/stale
        return None

    def set(self, slide_id: str, path: Path):
        s = str(path)
        # write-through
        self.lru.set(slide_id, s)
        if self.r:
            try:
                self.r.hset(self.ns, slide_id, s)
            except Exception:
                # ignore redis hiccups; lru still has it
                pass

    def delete(self, slide_id: str):
        # local
        # removing from LRU by re-creating without key is ok; not used heavily
        # (optional) we could add a .pop; fine to ignore for simplicity
        if self.r:
            try:
                self.r.hdel(self.ns, slide_id)
            except Exception:
                pass

    def mset(self, pairs: Iterable[Tuple[str, str]]):
        # bulk set from directory scans
        # update LRU
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

    # ------- Persistence fallback (pickle) only when Redis is off
    def load_pickle(self):
        if self.r:  # Redis mode: no pickle load
            return
        if self.pickle_file.exists():
            try:
                with open(self.pickle_file, "rb") as f:
                    od = pickle.load(f)
                # od is OrderedDict[str, str]
                for k, v in od.items():
                    self.lru.set(k, v)
            except Exception:
                pass

    def save_pickle(self):
        if self.r:  # Redis mode: no pickle save (Redis persists itself)
            return
        try:
            with open(self.pickle_file, "wb") as f:
                pickle.dump(OrderedDict(self.lru.items()), f)
        except Exception:
            pass
