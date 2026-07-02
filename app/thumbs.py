from __future__ import annotations
from pathlib import Path
import io
from PIL import Image
import openslide
import logging

log = logging.getLogger(__name__)

ASSOC_PREF = ("thumbnail", "macro", "label")


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

    thumb = slide.get_thumbnail((max_px, max_px))
    if thumb.mode == "RGBA":
        thumb = thumb.convert("RGB")
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue()
