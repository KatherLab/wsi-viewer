from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

from .qptiff_dz import QptiffDZ


class QptiffPool:
    """
    Thread-safe LRU pool of QptiffDZ handles.

    Avoids re-opening MxTiffFile on every request.
    Matches the SlidePool pattern used for OpenSlide handles.
    """

    def __init__(self, max_handles: int = 8, tile_size: int = 256, overlap: int = 0):
        self._lock = threading.Lock()
        self._handles: OrderedDict[str, QptiffDZ] = OrderedDict()
        self._max = max_handles
        self._tile_size = tile_size
        self._overlap = overlap

    def get(
        self,
        path: Path,
        markers: list[str] | None = None,
        colors: list[str] | None = None,
    ) -> QptiffDZ:
        key = str(path)
        with self._lock:
            if key in self._handles:
                self._handles.move_to_end(key)
                return self._handles[key]

        # Create outside lock to avoid blocking other threads
        handle = QptiffDZ(
            path,
            tile_size=self._tile_size,
            overlap=self._overlap,
            markers=markers,
            colors=colors,
        )

        with self._lock:
            # Double-check: another thread may have created it
            if key in self._handles:
                handle.close()
                self._handles.move_to_end(key)
                return self._handles[key]

            self._handles[key] = handle
            self._handles.move_to_end(key)

            # Evict oldest if over capacity
            while len(self._handles) > self._max:
                _, old_handle = self._handles.popitem(last=False)
                try:
                    old_handle.close()
                except Exception:
                    pass

        return handle

    def evict(self, path: Path):
        key = str(path)
        with self._lock:
            handle = self._handles.pop(key, None)
            if handle:
                try:
                    handle.close()
                except Exception:
                    pass

    def close_all(self):
        with self._lock:
            for handle in self._handles.values():
                try:
                    handle.close()
                except Exception:
                    pass
            self._handles.clear()
