from __future__ import annotations

import io
import math
import threading
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


QPTIFF_RENDER_VERSION = "qptiff-render-v2-global-range"


@dataclass(frozen=True)
class ChannelDisplay:
    marker: str
    color: str  # hex RGB, e.g. "#00ffff"
    min: float
    max: float
    gamma: float = 1.0


@dataclass(frozen=True)
class ChannelInfo:
    index: int
    name: str
    marker: str | None = None
    fluorophore: str | None = None
    biomarker: str | None = None
    display_name: str | None = None
    color: str | None = None
    is_unmixed_component: bool = False


class QptiffDZ:
    """
    DZI-compatible adapter for multiplex TIFF / QPTIFF files using mxtifffile.

    Uses mxtifffile.read_region() with native pos=(x,y), shape=(w,h), level=N
    for direct pyramid-level reads — no fallback loops or full-channel reads.

    Supports both:
    - Full .qptiff files with named markers (read_region("DAPI", ...))
    - Unmixed component .tif files with channel indices (read_region(0, ...))

    Pyramid level aware: translates DZI level to the closest native pyramid
    level for fast, already-downsampled reads.

    Display normalization is global per channel — stable min/max/gamma are
    computed once per marker and reused across all tiles and zoom levels.
    """

    DEFAULT_MARKER_COLORS = {
        # Nuclear / DNA
        "dapi": "#3366ff",
        "hoechst": "#3366ff",
        "hoechst33342": "#3366ff",
        "nuclei": "#3366ff",

        # Background / context
        "af": "#808080",
        "autofluorescence": "#808080",
        "background": "#808080",

        # Common immune / tissue markers
        "cd3": "#00ffff",
        "cd4": "#00ff66",
        "cd8": "#ff6600",
        "cd20": "#ff00ff",
        "cd68": "#ffaa00",
        "foxp3": "#cc66ff",
        "ki67": "#ff3333",
        "pdl1": "#ffcc00",
        "panck": "#ffff00",
        "cytokeratin": "#ffff00",
        "ecadherin": "#ffffff",
        "vimentin": "#00ff99",
    }

    # Colorblind-friendlier qualitative fallback palette
    PALETTE_HEX = [
        "#00ffff",  # cyan
        "#ff00ff",  # magenta
        "#ffff00",  # yellow
        "#ff9900",  # orange
        "#66ff66",  # light green
        "#3399ff",  # light blue
        "#ff6666",  # salmon
        "#cc66ff",  # purple
        "#ffffff",  # white
        "#999999",  # gray
        "#00ff99",  # mint
        "#ffcc00",  # amber
    ]

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

        # Channel metadata and lookup maps. This is critical for
        # component_data.tif, where get_markers() may return [None, None, ...]
        # and read_region("DAPI") may fail, while read_region(0) works.
        self._channel_infos = self._load_channel_infos()
        self._channel_lookup = self._build_channel_lookup(self._channel_infos)

        self.markers = markers or self._default_markers()
        self.colors = colors or self._assign_colors(self.markers)

        # Detect native pyramid level shapes
        self._native_level_shapes = self._detect_native_level_shapes()
        self._native_levels = len(self._native_level_shapes)

        self.width, self.height = self._native_level_shapes[0]

        # DZI level count: compute so that the coarsest DZI level fits in one tile
        self.max_dzi_level = int(math.ceil(math.log2(max(self.width, self.height))))
        self.level_count = self.max_dzi_level + 1

        # Per-marker display range cache (computed once globally)
        self._display_cache: dict[str, ChannelDisplay] = {}

        # Read lock around mxtifffile calls
        self._read_lock = threading.RLock()

    def close(self) -> None:
        """Close the underlying mxtifffile handle if available."""
        close = getattr(self.mx, "close", None)
        if callable(close):
            close()

    def min_safe_level(self) -> int:
        """Coarsest DZI level safe to render (0 = no floor).

        QPTIFF/multiplex tiles read by native pyramid level (not OpenSlide
        downsample), so coarse levels are not inherently expensive the way
        single-level OpenSlide slides are. Returns 0 for now; the unbounded
        native-level read in tile_jpeg/thumbnail_jpeg is a separate concern.
        """
        return 0

    def get_markers(self) -> list[str]:
        """
        Return usable display channel names.

        Full QPTIFF files often expose biomarker names via get_markers().
        Component-data TIFFs may return [None, None, ...], so we fall back to
        fluorophores / XML <Name> / synthetic Channel N labels.
        """
        names: list[str] = []

        # First try mxtifffile markers.
        try:
            raw_markers = self.mx.get_markers()
            if raw_markers:
                for m in raw_markers:
                    if m is None:
                        continue
                    s = str(m).strip()
                    if s and s.lower() not in {"none", "null"}:
                        names.append(s)
        except Exception:
            pass

        if names:
            return self._dedupe_keep_order(names)

        # Fall back to parsed channel infos.
        for ci in getattr(self, "_channel_infos", []):
            name = (
                ci.display_name
                or ci.biomarker
                or ci.marker
                or ci.fluorophore
                or ci.name
                or f"Channel {ci.index + 1}"
            )
            name = str(name).strip()
            if name:
                names.append(name)

        return self._dedupe_keep_order(names)

    def get_channel_infos(self) -> list[dict[str, Any]]:
        """Return parsed channel metadata for API/debug use."""
        return [
            {
                "index": ci.index,
                "name": ci.name,
                "marker": ci.marker,
                "fluorophore": ci.fluorophore,
                "biomarker": ci.biomarker,
                "display_name": ci.display_name,
                "color": ci.color,
                "is_unmixed_component": ci.is_unmixed_component,
            }
            for ci in self._channel_infos
        ]

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
        mins: list[float] | None = None,
        maxs: list[float] | None = None,
        gammas: list[float] | None = None,
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
        # channels=None means "use defaults"; channels=[] means "show nothing (black)"
        active_markers = self.markers if channels is None else channels
        active_colors = colors or self._assign_colors(active_markers)

        # Fill missing colors deterministically
        while len(active_colors) < len(active_markers):
            active_colors.append(self._default_color_for_marker(active_markers[len(active_colors)], len(active_colors)))

        # Coordinates in the native level space = full-res coords / native_downsample
        native_x = int(math.floor(full_x / native_downsample))
        native_y = int(math.floor(full_y / native_downsample))
        native_w = int(math.ceil(full_w / native_downsample))
        native_h = int(math.ceil(full_h / native_downsample))

        channels_data: list[tuple[np.ndarray, str]] = []

        for i, marker in enumerate(active_markers):
            color = active_colors[i] if i < len(active_colors) else self._default_color_for_marker(marker, i)

            display = self.get_channel_display(marker=marker, color=color)

            vmin = mins[i] if mins and i < len(mins) else display.min
            vmax = maxs[i] if maxs and i < len(maxs) else display.max
            gamma = gammas[i] if gammas and i < len(gammas) else display.gamma

            raw = self._read_marker_region(
                marker,
                x=native_x,
                y=native_y,
                width=native_w,
                height=native_h,
                level=native_level,
            )

            ch_u8 = self._apply_display_range(raw, vmin, vmax, gamma)
            channels_data.append((ch_u8, color))

        # Additive compositing with clipping — looks more natural for multiplex IF
        if not channels_data:
            # No active channels → return a black tile
            target_h = int(math.ceil(full_h / dzi_downsample))
            target_w = int(math.ceil(full_w / dzi_downsample))
            img = Image.new("RGB", (target_w, target_h), (0, 0, 0))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()

        first_shape = channels_data[0][0].shape
        rgb_float = np.zeros((*first_shape, 3), dtype=np.float32)

        for ch_data, color_hex in channels_data:
            color_rgb = self._hex_to_rgb01(color_hex)
            ch_float = ch_data.astype(np.float32) / 255.0
            rgb_float += ch_float[..., None] * color_rgb[None, None, :]

        rgb = (np.clip(rgb_float, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)

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
        Generate a thumbnail using stable global display ranges,
        consistent with tile rendering.
        """
        # Compute the target downsample from full resolution
        target_downsample = max(self.width, self.height) / max_px

        # Find the native level whose downsample best matches our target
        best_level = min(
            range(self._native_levels),
            key=lambda n: abs(target_downsample - self._native_downsample_for_level(n)),
        )

        level_w, level_h = self._native_level_shapes[best_level]

        rendered: list[tuple[np.ndarray, str]] = []

        for i, marker in enumerate(self.markers):
            color = self.colors[i] if i < len(self.colors) else self._default_color_for_marker(marker, i)

            display = self.get_channel_display(marker=marker, color=color)

            raw = self._read_marker_region(
                marker,
                x=0,
                y=0,
                width=level_w,
                height=level_h,
                level=best_level,
            )

            ch_u8 = self._apply_display_range(raw, display.min, display.max, display.gamma)
            rendered.append((ch_u8, display.color))

        # Additive compositing
        first_shape = rendered[0][0].shape if rendered else (1, 1)
        rgb_float = np.zeros((*first_shape, 3), dtype=np.float32)

        for ch_data, color_hex in rendered:
            color_rgb = self._hex_to_rgb01(color_hex)
            ch_float = ch_data.astype(np.float32) / 255.0
            rgb_float += ch_float[..., None] * color_rgb[None, None, :]

        rgb = (np.clip(rgb_float, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)

        img = Image.fromarray(rgb, mode="RGB")

        img.thumbnail((max_px, max_px), Image.Resampling.BILINEAR)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    # ------------------------------------------------------------------ #
    # Public display range API
    # ------------------------------------------------------------------ #

    def get_channel_display(
        self,
        marker: str,
        color: str | None = None,
        gamma: float = 1.0,
    ) -> ChannelDisplay:
        """
        Return stable display settings for one marker.
        The min/max are computed once and reused.
        """
        cache_key = marker

        if cache_key in self._display_cache:
            cached = self._display_cache[cache_key]
            if color is None or color == cached.color:
                return cached

        vmin, vmax = self._estimate_global_range(marker)

        display = ChannelDisplay(
            marker=marker,
            color=color or self._default_color_for_marker(marker, 0),
            min=vmin,
            max=vmax,
            gamma=gamma,
        )

        self._display_cache[cache_key] = display
        return display

    def get_all_channel_displays(self) -> dict[str, dict]:
        """Return display settings for all markers/channels for API responses."""
        result: dict[str, dict] = {}

        markers = self.get_markers()

        for i, marker in enumerate(markers):
            idx = self._channel_lookup.get(marker)
            if idx is None:
                idx = self._channel_lookup.get(self._marker_key(marker))

            xml_color = None
            if idx is not None and 0 <= idx < len(self._channel_infos):
                xml_color = self._channel_infos[idx].color

            default_color = xml_color or self._default_color_for_marker(marker, i)
            display = self.get_channel_display(marker=marker, color=default_color)

            result[marker] = {
                "index": idx,
                "color": display.color,
                "min": display.min,
                "max": display.max,
                "gamma": display.gamma,
            }

        return result

    # ------------------------------------------------------------------ #
    # Channel metadata helpers
    # ------------------------------------------------------------------ #

    def _load_channel_infos(self) -> list[ChannelInfo]:
        """
        Load channel metadata from mxtifffile and TIFF ImageDescription XML.

        The result must be robust for:
        - full QPTIFF with biomarker names;
        - component_data.tif with fluorophore/XML names;
        - missing/partial metadata.
        """
        count = self._infer_channel_count()
        infos: list[ChannelInfo] = []

        # mxtifffile channel info, if available
        mx_channel_info: list[dict[str, Any]] = []
        try:
            raw_ci = self.mx.get_channel_info()
            if raw_ci:
                mx_channel_info = list(raw_ci)
        except Exception:
            mx_channel_info = []

        # mxtifffile fluorophores, if available
        fluorophores: list[str | None] = []
        try:
            raw_f = self.mx.get_fluorophores()
            if raw_f:
                fluorophores = [None if f is None else str(f) for f in raw_f]
        except Exception:
            fluorophores = []

        # mxtifffile markers, if available
        biomarkers: list[str | None] = []
        try:
            raw_m = self.mx.get_markers()
            if raw_m:
                biomarkers = [None if m is None else str(m) for m in raw_m]
        except Exception:
            biomarkers = []

        xml_by_index = self._read_channel_xml_metadata(max_pages=max(count, 16))

        for i in range(count):
            mx_info = mx_channel_info[i] if i < len(mx_channel_info) and isinstance(mx_channel_info[i], dict) else {}
            xml_info = xml_by_index.get(i, {})

            fluorophore = self._clean_optional_str(
                mx_info.get("fluorophore")
                or (fluorophores[i] if i < len(fluorophores) else None)
                or xml_info.get("Name")
                or xml_info.get("Fluorophore")
            )

            biomarker = self._clean_optional_str(
                mx_info.get("biomarker")
                or (biomarkers[i] if i < len(biomarkers) else None)
                or xml_info.get("Biomarker")
                or xml_info.get("Marker")
            )

            display_name = self._clean_optional_str(
                mx_info.get("display_name")
                or xml_info.get("DisplayName")
                or xml_info.get("DisplayNameShort")
            )

            xml_name = self._clean_optional_str(xml_info.get("Name"))

            name = (
                display_name
                or biomarker
                or fluorophore
                or xml_name
                or f"Channel {i + 1}"
            )

            color = (
                self._parse_xml_color_to_hex(xml_info.get("Color"))
                or self._parse_xml_color_to_hex(mx_info.get("color"))
            )

            is_unmixed = str(
                xml_info.get("IsUnmixedComponent")
                or mx_info.get("is_unmixed_component")
                or ""
            ).strip().lower() in {"true", "1", "yes"}

            infos.append(
                ChannelInfo(
                    index=i,
                    name=name,
                    marker=biomarker,
                    fluorophore=fluorophore,
                    biomarker=biomarker,
                    display_name=display_name,
                    color=color,
                    is_unmixed_component=is_unmixed,
                )
            )

        return infos

    def _channel_ref_for_marker(self, marker: str) -> str | int:
        """
        Return the best mxtifffile read_region channel reference for a display
        marker.

        Component TIFFs need integer channel indices.
        Full QPTIFFs usually accept marker strings.
        """
        if marker is None:
            raise ValueError("Marker/channel name is None")

        s = str(marker).strip()

        if s in self._channel_lookup:
            return self._channel_lookup[s]

        key = self._marker_key(s)
        if key in self._channel_lookup:
            return self._channel_lookup[key]

        # Preserve existing full-QPTIFF behavior.
        return s

    def _infer_channel_count(self) -> int:
        """Infer number of real image channels."""
        # Prefer series axes.
        try:
            s0 = self.mx.series[0]
            shape = tuple(int(v) for v in s0.shape)
            axes = getattr(s0, "axes", "") or ""

            if "C" in axes:
                return int(shape[axes.index("C")])

            # Component TIFF commonly appears as CYX: (N, H, W)
            if len(shape) == 3 and shape[0] <= 64:
                return int(shape[0])
        except Exception:
            pass

        # Try parsed channel info.
        try:
            raw_ci = self.mx.get_channel_info()
            if raw_ci:
                return len(raw_ci)
        except Exception:
            pass

        try:
            raw_f = self.mx.get_fluorophores()
            if raw_f:
                return len(raw_f)
        except Exception:
            pass

        try:
            raw_m = self.mx.get_markers()
            if raw_m:
                return len(raw_m)
        except Exception:
            pass

        # Last resort: count pages with IsUnmixedComponent.
        try:
            import tifffile

            count = 0
            with tifffile.TiffFile(str(self.path)) as tif:
                for page in tif.pages:
                    tag = page.tags.get("ImageDescription") or page.tags.get(270)
                    value = str(tag.value) if tag is not None else ""
                    if "IsUnmixedComponent" in value:
                        count += 1
            if count > 0:
                return count
        except Exception:
            pass

        return 1

    def _read_channel_xml_metadata(
        self, max_pages: int = 64
    ) -> dict[int, dict[str, str]]:
        """
        Read simple XML metadata from per-page ImageDescription tags.

        Returns:
            {channel_index: {"Name": "...", "Color": "...", ...}}
        """
        result: dict[int, dict[str, str]] = {}

        try:
            import tifffile

            with tifffile.TiffFile(str(self.path)) as tif:
                channel_idx = 0

                for page in tif.pages[:max_pages]:
                    tag = page.tags.get("ImageDescription") or page.tags.get(270)
                    if tag is None:
                        continue

                    desc = str(tag.value)
                    parsed = self._parse_xml_description(desc)

                    # Skip obvious thumbnails/overview images.
                    image_type = str(parsed.get("ImageType", "")).lower()
                    if image_type in {"thumbnail", "overview", "macro", "label"}:
                        continue

                    if parsed:
                        result[channel_idx] = parsed
                        channel_idx += 1

        except Exception:
            pass

        return result

    @staticmethod
    def _parse_xml_description(desc: str) -> dict[str, str]:
        if not desc:
            return {}

        # Some TIFF descriptions may contain leading junk before XML.
        start = desc.find("<")
        if start > 0:
            desc = desc[start:]

        try:
            root = ET.fromstring(desc)
        except Exception:
            # Regex fallback for malformed simple XML.
            out: dict[str, str] = {}
            for tag in (
                "Name",
                "Color",
                "IsUnmixedComponent",
                "ImageType",
                "Biomarker",
                "Marker",
                "Fluorophore",
                "DisplayName",
            ):
                m = re.search(rf"<{tag}>\s*([^<]+?)\s*</{tag}>", desc)
                if m:
                    out[tag] = m.group(1).strip()
            return out

        out: dict[str, str] = {}
        for elem in root.iter():
            tag = str(elem.tag).split("}")[-1]
            text = elem.text.strip() if elem.text else ""
            if text and tag not in out:
                out[tag] = text

        return out

    @staticmethod
    def _clean_optional_str(value: Any) -> str | None:
        if value is None:
            return None
        s = str(value).strip()
        if not s or s.lower() in {"none", "null", "nan"}:
            return None
        return s

    @staticmethod
    def _parse_xml_color_to_hex(value: Any) -> str | None:
        if value is None:
            return None

        if isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            if s.startswith("#") and len(s) == 7:
                return s.lower()

            parts = [p.strip() for p in s.split(",")]
            if len(parts) != 3:
                return None

            try:
                rgb = [max(0, min(255, int(float(p)))) for p in parts]
            except ValueError:
                return None

            return "#{:02x}{:02x}{:02x}".format(*rgb)

        return None

    @staticmethod
    def _dedupe_keep_order(values: list[str]) -> list[str]:
        seen = set()
        out = []
        for v in values:
            if v not in seen:
                out.append(v)
                seen.add(v)
        return out

    def _build_channel_lookup(
        self, infos: list[ChannelInfo]
    ) -> dict[str, int]:
        lookup: dict[str, int] = {}

        for ci in infos:
            candidates = [
                ci.name,
                ci.marker,
                ci.fluorophore,
                ci.biomarker,
                ci.display_name,
                f"Channel {ci.index + 1}",
                str(ci.index),
            ]

            for candidate in candidates:
                if not candidate:
                    continue
                lookup[str(candidate)] = ci.index
                lookup[self._marker_key(str(candidate))] = ci.index

        return lookup

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _detect_native_level_shapes(self) -> list[tuple[int, int]]:
        """
        Return native pyramid level shapes as [(width, height), ...].
        """
        try:
            s0 = self.mx.series[0]
            if hasattr(s0, "levels") and s0.levels:
                shapes = []
                for level in s0.levels:
                    shape = level.shape
                    if len(shape) >= 2:
                        shapes.append((int(shape[-1]), int(shape[-2])))
                if shapes:
                    return shapes
        except Exception:
            pass

        # Fallback from series[0].shape
        try:
            shape = self.mx.series[0].shape
            if len(shape) >= 2:
                return [(int(shape[-1]), int(shape[-2]))]
        except Exception:
            pass

        # Last resort: read first marker
        markers = self.get_markers()
        if not markers:
            raise ValueError(f"No markers/channels found in multiplex TIFF: {self.path}")

        arr = np.asarray(self.mx.read_region(markers[0]))
        if arr.ndim < 2:
            raise ValueError(f"Could not infer dimensions from marker {markers[0]}")

        return [(int(arr.shape[-1]), int(arr.shape[-2]))]

    def _native_downsample_for_level(self, native_level: int) -> float:
        full_w, full_h = self._native_level_shapes[0]
        level_w, level_h = self._native_level_shapes[native_level]

        ds_x = full_w / max(1, level_w)
        ds_y = full_h / max(1, level_h)

        return float((ds_x + ds_y) / 2.0)

    def _dzi_to_native_level(self, dzi_level: int) -> tuple[int, float]:
        """
        Translate DZI level to the closest native pyramid level.

        Returns:
            native_level,
            native_downsample_from_full_resolution
        """
        dzi_downsample = 2 ** (self.max_dzi_level - dzi_level)

        best = min(
            range(self._native_levels),
            key=lambda n: abs(dzi_downsample - self._native_downsample_for_level(n)),
        )

        return best, self._native_downsample_for_level(best)

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
        Read a region of a single marker/channel at a given pyramid level.

        For full QPTIFF files, marker name reads often work.

        For component_data.tif files, marker names may not exist. In that case
        we read by integer channel index.

        Thread-safe via RLock.
        """
        channel_ref = self._channel_ref_for_marker(marker)

        with self._read_lock:
            # First try the resolved channel reference. For component TIFF this
            # should be an int. For normal QPTIFF this may be a marker string.
            try:
                arr = np.asarray(
                    self.mx.read_region(
                        channel_ref,
                        pos=(x, y),
                        shape=(width, height),
                        level=level,
                    )
                )
            except TypeError:
                # Some mxtifffile versions may not accept level for single-level
                # component files.
                arr = np.asarray(
                    self.mx.read_region(
                        channel_ref,
                        pos=(x, y),
                        shape=(width, height),
                    )
                )
            except Exception:
                # Fallback: try original marker string. This preserves behavior for
                # full QPTIFF files where marker lookup works but our metadata map
                # failed.
                try:
                    arr = np.asarray(
                        self.mx.read_region(
                            marker,
                            pos=(x, y),
                            shape=(width, height),
                            level=level,
                        )
                    )
                except TypeError:
                    arr = np.asarray(
                        self.mx.read_region(
                            marker,
                            pos=(x, y),
                            shape=(width, height),
                        )
                    )

        return self._squeeze_to_2d(arr, expected_width=width, expected_height=height)

    def _estimate_global_range(self, marker: str) -> tuple[float, float]:
        """
        Estimate a stable display range for a marker using a low-resolution
        native pyramid level or sampled data.

        This must not depend on the requested tile.
        """
        # Prefer a reasonably small native level for fast global statistics.
        # Aim for <= about 2 million pixels.
        target_pixels = 2_000_000

        candidate_levels = list(range(self._native_levels))
        best_level = candidate_levels[-1]

        for level in candidate_levels:
            w, h = self._native_level_shapes[level]
            if w * h <= target_pixels:
                best_level = level
                break

        level_w, level_h = self._native_level_shapes[best_level]

        arr = self._read_marker_region(
            marker,
            x=0,
            y=0,
            width=level_w,
            height=level_h,
            level=best_level,
        )

        arr = np.asarray(arr, dtype=np.float32)
        arr = arr[np.isfinite(arr)]

        if arr.size == 0:
            return 0.0, 1.0

        # Ignore zeros for sparse fluorescence if enough non-zero pixels exist.
        nonzero = arr[arr > 0]
        sample = nonzero if nonzero.size > 1000 else arr

        lo, hi = np.percentile(sample, [0.1, 99.8])

        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            lo = float(np.nanmin(sample))
            hi = float(np.nanmax(sample))

        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            return 0.0, 1.0

        return float(lo), float(hi)

    @staticmethod
    def _apply_display_range(
        arr: np.ndarray,
        vmin: float,
        vmax: float,
        gamma: float = 1.0,
    ) -> np.ndarray:
        """
        Convert raw marker intensities to uint8 using fixed display settings.
        """
        arr = np.asarray(arr, dtype=np.float32)

        if arr.size == 0 or vmax <= vmin:
            return np.zeros(arr.shape, dtype=np.uint8)

        arr = (arr - vmin) / (vmax - vmin)
        arr = np.clip(arr, 0.0, 1.0)

        if gamma != 1.0 and gamma > 0:
            arr = np.power(arr, gamma)

        return (arr * 255.0 + 0.5).astype(np.uint8)

    @staticmethod
    def _squeeze_to_2d(
        arr: np.ndarray,
        expected_width: int | None = None,
        expected_height: int | None = None,
    ) -> np.ndarray:
        arr = np.asarray(arr)
        arr = np.squeeze(arr)

        if arr.ndim == 2:
            out = arr
        elif arr.ndim == 3:
            # CYX or small-leading-channel layout
            if arr.shape[0] <= 4:
                out = arr[0, :, :]
            # YXC / YXS layout
            elif arr.shape[-1] <= 4:
                out = arr[:, :, 0]
            else:
                raise ValueError(f"Expected 2D marker image, got shape {arr.shape}")
        else:
            raise ValueError(f"Expected 2D marker image, got shape {arr.shape}")

        # mxtifffile docs/examples commonly use pos=(x, y), shape=(w, h),
        # but NumPy output should be (h, w). Be defensive for non-square reads.
        if expected_width is not None and expected_height is not None:
            if out.shape == (expected_height, expected_width):
                return out
            if out.shape == (expected_width, expected_height):
                return out.T

        return out

    @staticmethod
    def _marker_key(marker: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", marker.lower())

    @classmethod
    def _default_color_for_marker(cls, marker: str, index: int) -> str:
        marker = str(marker or f"Channel {index + 1}")
        key = cls._marker_key(marker)

        if key in cls.DEFAULT_MARKER_COLORS:
            return cls.DEFAULT_MARKER_COLORS[key]

        # Loose contains match for marker names like "Opal 570 CD3"
        for known, color in cls.DEFAULT_MARKER_COLORS.items():
            if known and known in key:
                return color

        return cls.PALETTE_HEX[index % len(cls.PALETTE_HEX)]

    @staticmethod
    def _hex_to_rgb01(color: str) -> np.ndarray:
        color = color.strip()
        if not color.startswith("#"):
            # Backward compatibility for old query params.
            named = {
                "red": "#ff0000",
                "green": "#00ff00",
                "blue": "#3366ff",
                "cyan": "#00ffff",
                "magenta": "#ff00ff",
                "yellow": "#ffff00",
                "orange": "#ff9900",
                "lime": "#66ff66",
                "purple": "#cc66ff",
                "teal": "#00cccc",
                "white": "#ffffff",
                "gray": "#808080",
                "grey": "#808080",
            }
            color = named.get(color.lower(), "#ffffff")

        color = color.lstrip("#")
        if len(color) != 6:
            color = "ffffff"

        return np.array(
            [
                int(color[0:2], 16) / 255.0,
                int(color[2:4], 16) / 255.0,
                int(color[4:6], 16) / 255.0,
            ],
            dtype=np.float32,
        )

    def _assign_colors(self, markers: list[str]) -> list[str]:
        """
        Assign display colors.

        Prefer embedded XML colors for component TIFFs, then fall back to
        semantic marker colors and finally palette colors.
        """
        colors: list[str] = []

        for i, marker in enumerate(markers):
            idx = self._channel_lookup.get(marker)
            if idx is None:
                idx = self._channel_lookup.get(self._marker_key(marker))

            xml_color = None
            if idx is not None and 0 <= idx < len(self._channel_infos):
                xml_color = self._channel_infos[idx].color

            colors.append(xml_color or self._default_color_for_marker(marker, i))

        return colors

    def _default_markers(self) -> list[str]:
        markers = self.get_markers()

        if not markers:
            raise ValueError(f"No markers/channels found in multiplex TIFF: {self.path}")

        preferred: list[str] = []

        # Case-insensitive nuclear preference.
        nuclear_keys = {"dapi", "hoechst", "hoechst33342", "nuclei"}
        for marker in markers:
            if self._marker_key(marker) in nuclear_keys:
                preferred.append(marker)
                break

        for marker in markers:
            if marker not in preferred:
                preferred.append(marker)
            if len(preferred) >= 3:
                break

        return preferred[:3]
