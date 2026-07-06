from __future__ import annotations

import io
from pathlib import Path

import openslide
from openslide.deepzoom import DeepZoomGenerator
from PIL import Image

# Per-tile source-pixel safety cap. OpenSlide's DeepZoomGenerator downsamples a
# tile from the LARGEST available pyramid level; for a single-level
# (non-pyramidal) slide a coarse (zoomed-out) tile therefore decodes a
# 256×downsample full-res region — gigabytes at low zoom. We forbid DZI levels
# whose per-tile source region exceeds this cap, enforced BOTH in the frontend
# (OpenSeadragon minLevel) and the backend (the /dzi tile route returns a blank
# tile below the floor) so a coarse tile can never be decoded regardless of
# client behaviour.
#
# The cap must sit ABOVE the smallest native (thumbnail) level of normal WSI
# pyramids — coarse DZI levels legitimately decode that whole small level
# (typically 2–25 Mpx across SVS/NDPI/SCN/…). It must sit BELOW the genuine
# single-level / shallow-pyramid danger zone, where a coarse tile reads a huge
# region of the full-res native level (≥100 Mpx, often Gpx). 64 Mpx lands in
# that gap with margin. Memory is safe: coarse levels have only a handful of
# tiles (≤16) so they never hit the 12-wide concurrent decode limit — that's
# reached only at fine levels, where each tile decodes a tiny 256×256 region.
TILE_MAX_SRC_PIXELS = 64_000_000

# ------------------------------------------------------------------ #
# Backward-compat re-exports from the deepzoom_backends package
# ------------------------------------------------------------------ #
from .deepzoom_backends.qptiff_dz import QptiffDZ  # noqa: E402, F401
from .deepzoom_backends.qptiff_pool import QptiffPool  # noqa: E402, F401
from .deepzoom_backends.factory import make_dz_backend as make_dz, is_qptiff, probe_is_multiplex_tiff  # noqa: E402, F401

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

    def min_safe_level(self) -> int:
        """Coarsest DZI level whose tiles decode <= TILE_MAX_SRC_PIXELS.

        Returns 0 for normal pyramidal slides (coarsest levels read from a
        small native thumbnail level, so they're cheap — the cap is sized to
        sit above typical thumbnail-level sizes, see TILE_MAX_SRC_PIXELS). For
        single-level / shallow-pyramid slides, coarse DZI levels read a huge
        region from the only (full-res) native level and would decode
        gigabytes — returns the finest level whose source region fits the cap.
        The frontend forbids zooming out below this level.

        Decode size for a DZI level is (tile_size × _l_z_downsamples[L]),
        clamped to the native level's dimensions — matching what
        DeepZoomGenerator actually passes to OpenSlide.read_region().
        """
        dz = self.dz
        levels = dz.level_count
        if levels == 0:
            return 0
        # Private DeepZoomGenerator internals: per-DZ-level native slide level
        # and native-px-per-tile-px downsample. Stable across openslide-python
        # 1.x; guard with getattr in case of future rename.
        slide_from_dz = getattr(dz, "_slide_from_dz_level", None)
        l_z_downsamples = getattr(dz, "_l_z_downsamples", None)
        if slide_from_dz is None or l_z_downsamples is None:
            return 0  # can't introspect -> don't restrict
        tile = self.tile_size
        for lvl in range(levels):
            native = slide_from_dz[lvl]
            nw, nh = self.slide.level_dimensions[native]
            ds = tile * l_z_downsamples[lvl]
            # read_region clamps each axis to the native level dimensions, so
            # the actual decode is the per-axis-clamped rectangle.
            src_w = min(ds, nw)
            src_h = min(ds, nh)
            if src_w * src_h <= TILE_MAX_SRC_PIXELS:
                return lvl
        return levels - 1

    def dzi_xml(self) -> str:
        return self.dz.get_dzi("jpeg")

    def tile_jpeg(self, level: int, x: int, y: int) -> bytes:
        if level < 0 or level >= self.level_count:
            raise ValueError(f"Invalid DZI level {level}")

        # Backend-enforced safety floor: never decode a tile below the safe
        # level, regardless of what the client requests. Coarse tiles on a
        # single-level slide decode gigabytes. Instead of erroring (which
        # spams the logs and makes OpenSeadragon treat zoom-out as a failure),
        # return a blank white tile — OSD renders it harmlessly and the route
        # can cache it like any other tile. No decode, no OOM, no noise.
        floor = self.min_safe_level()
        if level < floor:
            return self._blank_tile()

        tile = self.dz.get_tile(level, (x, y))
        if tile.mode != "RGB":
            tile = tile.convert("RGB")

        buf = io.BytesIO()
        tile.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    def _blank_tile(self) -> bytes:
        """A cached blank white tile, returned for sub-floor (unsafe) levels."""
        cached = getattr(self, "_blank_tile_bytes", None)
        if cached is None:
            img = Image.new("RGB", (self.tile_size, self.tile_size), (255, 255, 255))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            cached = buf.getvalue()
            self._blank_tile_bytes = cached
        return cached
