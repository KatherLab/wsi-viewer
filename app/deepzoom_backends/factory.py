from __future__ import annotations

from pathlib import Path

from .qptiff_dz import QptiffDZ
from .qptiff_pool import QptiffPool

QPTIFF_EXTS = {".qptiff"}


def is_qptiff(path: Path) -> bool:
    return path.suffix.lower() in QPTIFF_EXTS


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

    For QPTIFF:
        returns QptiffDZ(path) from qptiff_pool or fresh

    For OpenSlide-readable files:
        returns DZ(OpenSlide handle)
    """
    if is_qptiff(path):
        if qptiff_pool is not None:
            return qptiff_pool.get(path, markers=markers, colors=colors)
        return QptiffDZ(path, tile_size=tile_size, overlap=overlap, markers=markers, colors=colors)

    if slide_pool is None:
        raise ValueError("slide_pool is required for OpenSlide-backed slides")

    # Lazy import to avoid circular dependency with app.dz
    from app.dz import DZ

    slide = slide_pool.get(path)
    return DZ(slide, tile_size=tile_size, overlap=overlap)
