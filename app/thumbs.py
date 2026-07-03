from __future__ import annotations
from pathlib import Path
import io
import logging
from PIL import Image
import openslide

log = logging.getLogger(__name__)

ASSOC_PREF = ("thumbnail", "macro", "label")

# Safety cap for fallback thumbnail generation. OpenSlide's get_thumbnail()
# downsamples from the largest available pyramid level — for a single-level
# (non-pyramidal) slide that means decoding the ENTIRE full-resolution image
# into RAM (we measured ~26 GB for one 6.4-GP slide). If the smallest pyramid
# level still exceeds this cap, we read a bounded region from it and downsample
# that instead of the whole slide.
THUMB_MAX_SRC_PIXELS = 4_000_000  # ~4 Mpx → decodes to a few MB worst case


def make_preview_bytes(p: Path, max_px: int = 512, prefer_associated: bool = True, slide_pool=None) -> bytes:
    """Generate preview, optionally using a shared slide pool."""
    if slide_pool:
        slide = slide_pool.get(p)
        return _generate_thumb(slide, max_px, prefer_associated)
    else:
        slide = openslide.open_slide(str(p))
        try:
            return _generate_thumb(slide, max_px, prefer_associated)
        finally:
            slide.close()


def _generate_thumb(slide, max_px: int, prefer_associated: bool) -> bytes:
    """Core thumbnail generation logic."""
    if prefer_associated:
        for k in ASSOC_PREF:
            if k in slide.associated_images:
                img = slide.associated_images[k]
                img.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
                if img.mode == "RGBA":
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85, optimize=True)
                return buf.getvalue()

    # Fallback: build a thumbnail from the slide pyramid.
    #
    # OpenSlide.get_thumbnail() downsamples from the LARGEST level, which for a
    # single-level (non-pyramidal) slide decodes the whole image into RAM. Guard
    # against that: if the smallest level is still huge, read a bounded region
    # from it and downsample that instead.
    thumb = _safe_get_thumbnail(slide, max_px)
    if thumb.mode == "RGBA":
        thumb = thumb.convert("RGB")
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue()


def _safe_get_thumbnail(slide, max_px: int) -> Image.Image:
    """get_thumbnail() that never decodes more than THUMB_MAX_SRC_PIXELS.

    For normal multi-level slides the coarsest level is already small, so we
    delegate to OpenSlide directly (preserves existing behaviour). For
    single-level or shallow-pyramid slides whose smallest level still exceeds
    the cap, we read a bounded center region from that level and downsample it.
    """
    try:
        levels = slide.level_count
        smallest_w, smallest_h = slide.level_dimensions[-1] if levels else (0, 0)
    except Exception:
        # Can't introspect — fall back to OpenSlide and hope it's a normal
        # pyramidal slide (the common case).
        return slide.get_thumbnail((max_px, max_px))

    src_pixels = int(smallest_w) * int(smallest_h)
    if src_pixels <= THUMB_MAX_SRC_PIXELS:
        # Smallest level is small enough — OpenSlide will read it cheaply.
        return slide.get_thumbnail((max_px, max_px))

    # Smallest level is too big (e.g. a single-level 85k×75k slide). Read a
    # bounded center region from it instead of decoding the whole thing.
    log.info(
        "Falling back to bounded read_region for thumbnail: smallest level "
        "%dx%d (%.1f Mpx) exceeds cap %d Mpx",
        smallest_w, smallest_h, src_pixels / 1e6, THUMB_MAX_SRC_PIXELS / 1e6,
    )

    # Choose a source region whose longer side keeps the decoded pixel count
    # under the cap. We read from the smallest level (cheapest decode).
    aspect = smallest_w / smallest_h if smallest_h else 1.0
    if aspect >= 1:
        src_w = int((THUMB_MAX_SRC_PIXELS * aspect) ** 0.5)
        src_h = int(THUMB_MAX_SRC_PIXELS / src_w)
    else:
        src_h = int((THUMB_MAX_SRC_PIXELS / aspect) ** 0.5)
        src_w = int(THUMB_MAX_SRC_PIXELS / src_h)
    src_w = max(1, min(src_w, smallest_w))
    src_h = max(1, min(src_h, smallest_h))

    # Center the region within the level.
    x = max(0, (smallest_w - src_w) // 2)
    y = max(0, (smallest_h - src_h) // 2)

    region = slide.read_region((x, y), slide.level_count - 1, (src_w, src_h))
    if region.mode == "RGBA":
        region = region.convert("RGB")
    region.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    return region
