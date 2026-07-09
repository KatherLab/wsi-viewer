from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from .qptiff_dz import QptiffDZ
from .qptiff_pool import QptiffPool

log = logging.getLogger("wsi-browser.deepzoom.factory")

QPTIFF_EXTS = {".qptiff"}
TIFF_EXTS = {".tif", ".tiff"}
MULTIPLEX_TIFF_EXTS = QPTIFF_EXTS | TIFF_EXTS


def _looks_like_vectra_component_metadata(text: str) -> bool:
    """
    Metadata-only check for Vectra/PerkinElmer/Akoya unmixed component TIFFs.

    Keep this intentionally conservative. dtype=float32 alone is not enough,
    because many unrelated scientific TIFFs are float images.
    """
    if not text:
        return False

    needles = (
        "IsUnmixedComponent",
        "<IsUnmixedComponent>",
        "UnmixedComponent",
        "PerkinElmer",
        "Akoya",
        "Vectra",
        "Nuance",
        "QPTIFF",
        "QPI",
    )
    return any(n in text for n in needles)


def _looks_like_multichannel_ome(tif) -> bool:
    """
    Return True for OME-TIFFs that store multiple channels separately.

    mxtifffile can read OME-TIFF channels individually (like QPTIFF), giving
    the channel-toggle / per-channel display features. RGB brightfield
    OME-TIFFs (interleaved samples, e.g. axes "YXS") are left to OpenSlide,
    which renders their colour correctly.

    Routing rule:
    - >= 2 channels (C axis)          -> multiplex backend
    - RGB interleaved (>= 3 samples)  -> OpenSlide
    - single-channel intensity image  -> multiplex backend (OpenSlide often
      only surfaces one plane of these and can't normalise them)
    """
    if not getattr(tif, "is_ome", False):
        return False

    try:
        series = tif.series[0]
        axes = getattr(series, "axes", "") or ""
        shape = tuple(int(v) for v in series.shape)
    except Exception:
        # is_ome is set but the series is unreadable; let mxtifffile try.
        return True

    n_channels = shape[axes.index("C")] if "C" in axes and axes.index("C") < len(shape) else 1
    n_samples = shape[axes.index("S")] if "S" in axes and axes.index("S") < len(shape) else 1

    if n_channels >= 2:
        return True
    if n_samples >= 3:
        return False
    return True


def probe_is_multiplex_tiff(path: Path) -> bool:
    """
    Return True if this path should be handled by QptiffDZ/mxtifffile.

    For .qptiff we accept the extension.

    For .tif/.tiff (including .ome.tif/.ome.tiff) we inspect metadata. This
    catches OME-TIFF and Vectra/Akoya component TIFFs while preventing normal
    RGB WSI TIFFs from being incorrectly routed away from OpenSlide.
    """
    ext = path.suffix.lower()

    if ext in QPTIFF_EXTS:
        return True

    if ext not in TIFF_EXTS:
        return False

    try:
        import tifffile
    except Exception as e:
        log.warning("tifffile is unavailable; cannot probe %s as multiplex TIFF: %s", path, e)
        return False

    try:
        with tifffile.TiffFile(str(path)) as tif:
            # OME-TIFF: detected via tifffile's is_ome flag + OME-XML.
            # mxtifffile parses channels from the OME metadata directly.
            if _looks_like_multichannel_ome(tif):
                return True

            descs: list[str] = []

            # Inspect a small number of pages only; no pixel read.
            for page in tif.pages[: min(len(tif.pages), 16)]:
                tag = page.tags.get("ImageDescription") or page.tags.get(270)
                if tag is not None:
                    try:
                        descs.append(str(tag.value))
                    except Exception:
                        pass

            joined = "\n".join(descs)

            if not _looks_like_vectra_component_metadata(joined):
                return False

            # Metadata says this is relevant. Validate that shape/dtype look
            # compatible with fluorescence channels.
            try:
                series = tif.series[0]
                dtype = series.dtype
                shape = series.shape
                axes = getattr(series, "axes", "") or ""

                has_image_axes = "Y" in axes and "X" in axes
                has_channel_axis = "C" in axes or len(shape) >= 3
                intensity_dtype = dtype in (
                    np.float32,
                    np.float16,
                    np.uint16,
                    np.uint32,
                    np.uint8,
                )

                return bool(has_image_axes and has_channel_axis and intensity_dtype)
            except Exception:
                # If the metadata is a strong Vectra/QPTIFF marker, still let
                # mxtifffile attempt to open it.
                return True

    except Exception as e:
        log.debug("Multiplex TIFF probe failed for %s: %s", path, e)
        return False


def is_qptiff(path: Path) -> bool:
    """
    Backward-compatible name used by existing imports.

    Despite the name, this now means:
    'should this file use the multiplex mxtifffile backend?'
    """
    return probe_is_multiplex_tiff(path)


def make_dz_backend(
    path: Path,
    slide_pool=None,
    qptiff_pool: QptiffPool | None = None,
    tile_size: int = 256,
    overlap: int = 0,
    markers: list[str] | None = None,
    colors: list[str] | None = None,
):
    """
    Factory used by FastAPI routes.

    For QPTIFF / Vectra component TIFF:
        returns QptiffDZ(path) from qptiff_pool or fresh.

    For OpenSlide-readable files:
        returns DZ(OpenSlide handle)
    """
    if is_qptiff(path):
        if qptiff_pool is not None:
            return qptiff_pool.get(path)
        return QptiffDZ(
            path,
            tile_size=tile_size,
            overlap=overlap,
            markers=markers,
            colors=colors,
        )

    if slide_pool is None:
        raise ValueError("slide_pool is required for OpenSlide-backed slides")

    # Lazy import to avoid circular dependency with app.dz
    from app.dz import DZ

    slide = slide_pool.get(path)
    return DZ(slide, tile_size=tile_size, overlap=overlap)
