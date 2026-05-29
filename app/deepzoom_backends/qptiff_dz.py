from __future__ import annotations

import io
import math
from pathlib import Path

import numpy as np
from PIL import Image


class QptiffDZ:
    """
    DZI-compatible adapter for multiplex TIFF / QPTIFF files using mxtifffile.

    Uses mxtifffile.read_region() with native pos=(x,y), shape=(w,h), level=N
    for direct pyramid-level reads — no fallback loops or full-channel reads.

    Pyramid level aware: translates DZI level to the closest native pyramid
    level for fast, already-downsampled reads.

    Per-tile normalization applied to pyramid-level ROI data (much smaller
    reads), avoiding brightness shifts while panning.

    Channels and colors can be overridden per-request via query params.
    """

    PALETTE = ["red", "green", "blue", "cyan", "magenta", "yellow", "orange", "lime", "purple", "teal"]

    COLOR_CHANNELS: dict[str, list[int]] = {
        "red": [0], "green": [1], "blue": [2],
        "cyan": [1, 2], "magenta": [0, 2], "yellow": [0, 1],
        "orange": [0], "lime": [1], "purple": [2], "teal": [1, 2],
        "white": [0, 1, 2], "gray": [0, 1, 2],
    }

    def __init__(
        self,
        path: Path | str,
        tile_size: int = 256,
        overlap: int = 0,
        markers: list[str] | None = None,
        colors: list[str] | None = None,
    ):
        from mxtifffile import MxTiffFile

        self.path = Path(path)
        self.tile_size = tile_size
        self.overlap = overlap
        self.mx = MxTiffFile(str(self.path))

        self.markers = markers or self._default_markers()
        self.colors = colors or self._assign_colors(self.markers)

        # Detect native pyramid levels from mx.series[0].levels
        self._native_levels = self._detect_native_levels()

        self.width, self.height = self._detect_full_resolution_size()

        # DZI level count: compute so that the coarsest DZI level fits in one tile
        self.max_dzi_level = int(math.ceil(math.log2(max(self.width, self.height))))
        self.level_count = self.max_dzi_level + 1

    def close(self) -> None:
        """Close the underlying mxtifffile handle if available."""
        close = getattr(self.mx, "close", None)
        if callable(close):
            close()

    def get_markers(self) -> list[str]:
        try:
            markers = self.mx.get_markers()
            return list(markers)
        except Exception:
            return []

    def get_format_id(self) -> str | None:
        try:
            return str(getattr(self.mx, "format_id", "")) or None
        except Exception:
            return None

    def dzi_xml(self) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Image TileSize="{self.tile_size}" Overlap="{self.overlap}" Format="jpeg" '
            'xmlns="http://schemas.microsoft.com/deepzoom/2008">'
            f'<Size Width="{self.width}" Height="{self.height}"/>'
            "</Image>"
        )

    def tile_jpeg(
        self,
        level: int,
        x: int,
        y: int,
        channels: list[str] | None = None,
        colors: list[str] | None = None,
    ) -> bytes:
        if level < 0 or level >= self.level_count:
            raise ValueError(f"Invalid DZI level {level}")

        # Translate DZI level to native pyramid level
        native_level, native_downsample = self._dzi_to_native_level(level)

        # DZI downsample relative to full-res
        dzi_downsample = 2 ** (self.max_dzi_level - level)

        # Compute ROI in full-res coordinates
        full_x = int(x * self.tile_size * dzi_downsample)
        full_y = int(y * self.tile_size * dzi_downsample)
        full_w = int(self.tile_size * dzi_downsample)
        full_h = int(self.tile_size * dzi_downsample)

        if full_x >= self.width or full_y >= self.height:
            raise ValueError("Tile outside image bounds")

        full_w = min(full_w, self.width - full_x)
        full_h = min(full_h, self.height - full_y)

        # Which channels to use
        active_markers = channels or self.markers
        active_colors = colors or self.colors

        # Read each marker at the native pyramid level.
        # Coordinates in the native level space = full-res coords / native_downsample
        native_x = full_x // native_downsample
        native_y = full_y // native_downsample
        native_w = int(math.ceil(full_w / native_downsample))
        native_h = int(math.ceil(full_h / native_downsample))

        channels_data = []
        for marker in active_markers:
            ch = self._read_marker_region(
                marker,
                x=native_x,
                y=native_y,
                width=native_w,
                height=native_h,
                level=native_level,
            )
            ch = self._normalize_to_uint8(ch)
            channels_data.append(ch)

        # Composite into RGB — each marker contributes to its assigned color channel(s)
        first_shape = channels_data[0].shape if channels_data else (1, 1)
        rgb = np.zeros((*first_shape, 3), dtype=np.uint8)
        for ch_data, color in zip(channels_data, active_colors):
            target_channels = self.COLOR_CHANNELS.get(color, [0, 1, 2])
            for c in target_channels:
                rgb[..., c] = np.maximum(rgb[..., c], ch_data)

        img = Image.fromarray(rgb, mode="RGB")

        # Resize to final DZI tile dimensions if needed
        target_w = int(math.ceil(full_w / dzi_downsample))
        target_h = int(math.ceil(full_h / dzi_downsample))
        if img.size != (target_w, target_h):
            img = img.resize((target_w, target_h), Image.Resampling.BILINEAR)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    def thumbnail_jpeg(self, max_px: int = 512) -> bytes:
        """
        Generate a thumbnail by reading from the most appropriate native
        pyramid level and applying per-block normalization with gamma
        correction for visibility of sparse fluorescence signal.
        """
        # Compute the target downsample from full resolution
        target_downsample = max(self.width, self.height) / max_px

        # Find the native level whose downsample best matches our target
        best_level = min(
            range(self._native_levels),
            key=lambda n: abs(target_downsample - 2 ** n),
        )
        best_downsample = 2 ** best_level

        # Dimensions at this native level
        level_w = max(1, int(math.ceil(self.width / best_downsample)))
        level_h = max(1, int(math.ceil(self.height / best_downsample)))

        # Use 256x256 blocks for per-block normalization
        block_size = 256

        norm_channels = []
        for marker in self.markers:
            ch = self._read_marker_region(
                marker,
                x=0,
                y=0,
                width=level_w,
                height=level_h,
                level=best_level,
            )
            ch_float = ch.astype(np.float32)
            ch_norm = np.zeros_like(ch_float)

            # Per-block percentile normalization for local contrast
            # Uses 0-99% range to avoid outlier-driven compression
            for by in range(0, level_h, block_size):
                bh = min(block_size, level_h - by)
                for bx in range(0, level_w, block_size):
                    bw = min(block_size, level_w - bx)
                    block = ch_float[by:by + bh, bx:bx + bw]
                    lo, hi = np.percentile(block, [0, 99])
                    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
                        lo = float(np.nanmin(block))
                        hi = float(np.nanmax(block))
                    if hi > lo:
                        ch_norm[by:by + bh, bx:bx + bw] = (
                            np.clip((block - lo) / (hi - lo), 0, 1)
                        )
            norm_channels.append((ch_norm * 255).astype(np.uint8))

        first_shape = norm_channels[0].shape if norm_channels else (1, 1)
        rgb = np.zeros((*first_shape, 3), dtype=np.uint8)
        for ch_data, color in zip(norm_channels, self.colors):
            target_channels = self.COLOR_CHANNELS.get(color, [0, 1, 2])
            for c in target_channels:
                rgb[..., c] = np.maximum(rgb[..., c], ch_data)

        img = Image.fromarray(rgb, mode="RGB")

        # Final resize to exactly max_px on the longest side
        img.thumbnail((max_px, max_px), Image.Resampling.BILINEAR)

        # Apply gamma correction to make sparse fluorescence signal visible
        arr = np.array(img, dtype=np.float32) / 255.0
        arr = np.power(arr, 0.3)  # gamma = 0.3 brightens dark regions
        img = Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8), mode="RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _detect_native_levels(self) -> int:
        """Detect the number of actual pyramid levels in the QPTIFF file."""
        try:
            s0 = self.mx.series[0]
            if hasattr(s0, "levels") and s0.levels:
                return len(s0.levels)
            if hasattr(s0, "is_pyramidal") and s0.is_pyramidal:
                return len(s0.levels)
        except Exception:
            pass
        return 1

    def _dzi_to_native_level(self, dzi_level: int) -> tuple[int, int]:
        """
        Translate DZI level to the closest native pyramid level.

        Returns (native_level, native_downsample_from_full_res).
        """
        dzi_downsample = 2 ** (self.max_dzi_level - dzi_level)

        # Find the native level whose downsample is closest to what we need
        best = min(
            range(self._native_levels),
            key=lambda n: abs(dzi_downsample - 2 ** n),
        )

        actual_downsample = 2 ** best
        return best, actual_downsample

    def _read_marker_region(
        self,
        marker: str,
        x: int,
        y: int,
        width: int,
        height: int,
        level: int = 0,
    ) -> np.ndarray:
        """
        Read a region of a single marker channel at a given pyramid level.

        Coordinates are in the level's own coordinate space.
        Single call — no fallback loops or full-channel reads.
        """
        arr = np.asarray(self.mx.read_region(
            marker,
            pos=(x, y),
            shape=(width, height),
            level=level,
        ))
        return self._squeeze_to_2d(arr)

    @staticmethod
    def _squeeze_to_2d(arr: np.ndarray) -> np.ndarray:
        arr = np.asarray(arr)
        arr = np.squeeze(arr)

        if arr.ndim == 2:
            return arr

        if arr.ndim == 3:
            if arr.shape[0] <= 4:
                return arr[0, :, :]
            if arr.shape[-1] <= 4:
                return arr[:, :, 0]

        raise ValueError(f"Expected 2D marker image, got shape {arr.shape}")

    @staticmethod
    def _normalize_to_uint8(arr: np.ndarray) -> np.ndarray:
        """Robustly map fluorescence intensities to uint8 using percentile normalization."""
        arr = np.asarray(arr)

        if arr.size == 0:
            return np.zeros((1, 1), dtype=np.uint8)

        arr = arr.astype(np.float32, copy=False)

        lo, hi = np.percentile(arr, [1.0, 99.8])
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            lo = float(np.nanmin(arr))
            hi = float(np.nanmax(arr))

        if hi <= lo:
            return np.zeros(arr.shape, dtype=np.uint8)

        arr = (arr - lo) / (hi - lo)
        arr = np.clip(arr, 0, 1)
        return (arr * 255).astype(np.uint8)

    def _assign_colors(self, markers: list[str]) -> list[str]:
        """Assign a color from the palette to each marker."""
        colors = []
        for i, m in enumerate(markers):
            if m.upper() in ("DAPI", "HOECHST", "HOECHST 33342"):
                colors.append("blue")
            else:
                colors.append(self.PALETTE[i % len(self.PALETTE)])
        return colors

    def _default_markers(self) -> list[str]:
        markers = self.get_markers()

        if not markers:
            raise ValueError(f"No markers found in multiplex TIFF: {self.path}")

        preferred = []
        for candidate in ("DAPI", "HOECHST", "Hoechst", "Nuclei"):
            if candidate in markers:
                preferred.append(candidate)
                break

        for marker in markers:
            if marker not in preferred:
                preferred.append(marker)
            if len(preferred) >= 3:
                break

        return preferred[:3]

    def _detect_full_resolution_size(self) -> tuple[int, int]:
        # Try mx.series[0] shape first (most reliable for QPTIFFs)
        try:
            s0 = self.mx.series[0]
            shape = s0.shape
            if len(shape) >= 2:
                return int(shape[-1]), int(shape[-2])  # (W, H) from (..., H, W)
        except Exception:
            pass

        # Fallback: read the first marker at level 0
        arr = self.mx.read_region(self.markers[0])
        arr = np.asarray(arr)

        if arr.ndim < 2:
            raise ValueError(f"Could not infer dimensions from marker {self.markers[0]}")

        h, w = int(arr.shape[-2]), int(arr.shape[-1])
        return w, h
