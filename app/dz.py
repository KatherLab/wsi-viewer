from __future__ import annotations

import io
from pathlib import Path

import openslide
from openslide.deepzoom import DeepZoomGenerator
from PIL import Image

# ------------------------------------------------------------------ #
# Backward-compat re-exports from the deepzoom_backends package
# ------------------------------------------------------------------ #
from .deepzoom_backends.qptiff_dz import QptiffDZ  # noqa: E402, F401
from .deepzoom_backends.qptiff_pool import QptiffPool  # noqa: E402, F401
from .deepzoom_backends.factory import make_dz_backend as make_dz, is_qptiff  # noqa: E402, F401

# ------------------------------------------------------------------ #
# Aliases for existing importers
# ------------------------------------------------------------------ #
MxTiffDZ = QptiffDZ  # noqa: E402 — backward compat alias
is_multiplex_tiff = is_qptiff  # noqa: E402 — backward compat alias

# ------------------------------------------------------------------ #
# OpenSlide-backed Deep Zoom adapter
# ------------------------------------------------------------------ #


class DZ:
    """
    Existing OpenSlide-backed Deep Zoom adapter.
    Kept compatible with the current code.
    """

    def __init__(self, slide: openslide.OpenSlide, tile_size: int = 256, overlap: int = 0):
        self.slide = slide
        self.dz = DeepZoomGenerator(
            slide,
            tile_size=tile_size,
            overlap=overlap,
            limit_bounds=True,
        )
        self.tile_size = tile_size
        self.overlap = overlap

    @property
    def level_count(self) -> int:
        return self.dz.level_count

    def dzi_xml(self) -> str:
        return self.dz.get_dzi("jpeg")

    def tile_jpeg(self, level: int, x: int, y: int) -> bytes:
        if level < 0 or level >= self.level_count:
            raise ValueError(f"Invalid DZI level {level}")

        tile = self.dz.get_tile(level, (x, y))
        if tile.mode != "RGB":
            tile = tile.convert("RGB")

        buf = io.BytesIO()
        tile.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
