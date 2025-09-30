from __future__ import annotations
import logging
import os
import io
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.templating import Jinja2Templates

import openslide

from .config import AppCfg
from .cache import make_cache, Cache
from .fs_index import build_tree, stable_id_from_path
from .thumbs import make_preview_bytes
from .dz import DZ
from .models import SlideMeta

# --------------------------------------------------------------------------- #
# Logging
log = logging.getLogger("wsi-browser")
logging.basicConfig(level=logging.INFO)

# --------------------------------------------------------------------------- #
# Config

default_path = Path(__file__).resolve().parent.parent / "config.yml"
cfg_path_str = os.getenv("WSI_CONFIG", str(default_path))
CFG_PATH = Path(cfg_path_str).resolve()
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

try:
    cfg = AppCfg.load(CFG_PATH)
except Exception as e:
    log.exception("Failed to load config.yml at %s", CFG_PATH)
    raise

# Cache – optional Redis
try:
    cache: Cache = make_cache(cfg)
except Exception as e:
    log.warning("Cache backend init failed; continuing without Redis: %s", e)
    cache = Cache.noop(
        cfg.cache.ttl_seconds.get("tree", 60),
        cfg.cache.ttl_seconds.get("thumb", 86400),
        cfg.cache.ttl_seconds.get("tile", 3600),
    )

# --------------------------------------------------------------------------- #
app = FastAPI(title="WSI Browser", version="0.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(TEMPLATES_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# --------------------------------------------------------------------------- #
# Helpers
ROOTS = {str(Path(r.path).resolve()): r.label for r in cfg.roots}
EXTS = set([e.lower() for e in cfg.extensions])

def resolve_by_id(slide_id: str) -> Path:
    """Search all roots for a slide whose deterministic ID matches."""
    for base in ROOTS.keys():
        for root, _, files in os.walk(base):
            for f in files:
                p = Path(root) / f
                if p.suffix.lower() in EXTS and stable_id_from_path(p) == slide_id:
                    return p
    raise FileNotFoundError(f"Slide id not found: {slide_id}")

# --------------------------------------------------------------------------- #
# Routes
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "roots": ROOTS})

@app.get("/api/tree")
async def api_tree():
    trees = []
    for base, label in ROOTS.items():
        base_path = Path(base)
        
        # Check if the path exists and is accessible
        if not base_path.exists():
            log.warning(f"Root path does not exist: {base}")
            trees.append({
                "id": stable_id_from_path(base_path),
                "name": label or base_path.name,
                "path": base,
                "is_dir": True,
                "children": [],
                "slide_count": 0,
            })
            continue
            
        if not base_path.is_dir():
            log.warning(f"Root path is not a directory: {base}")
            continue
            
        k = Cache.key("tree", base)
        try:
            raw = cache.get(k)
            if raw:
                log.debug(f"Using cached tree for {base}")
                trees.append(json.loads(raw))
                continue
                
            log.info(f"Building tree for {base}")
            node = build_tree(base_path, list(EXTS), cfg.exclude)
            
            # Use the label from config if available
            if label:
                node.name = label
                
            data = node.model_dump()
            
            try:
                cache.setex(k, cache.ttl_tree, json.dumps(data).encode())
            except Exception as ce:
                log.debug("Tree cache set failed: %s", ce)
                
            trees.append(data)
            
        except Exception as e:
            log.exception("Tree build failed for %s: %s", base, e)
            # Fallback placeholder for the failed root
            trees.append({
                "id": stable_id_from_path(base_path),
                "name": label or base_path.name,
                "path": base,
                "is_dir": True,
                "children": [],
                "slide_count": 0,
            })
            
    return trees


@app.get("/api/dir")
async def api_dir(path: str):
    try:
        p = Path(path)
        if not p.exists() or not p.is_dir():
            raise HTTPException(404, "Directory not found")
        entries = []
        for f in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            if f.is_file() and f.suffix.lower() in EXTS:
                try:
                    st = f.stat()
                except Exception:
                    continue
                entries.append(
                    {
                        "id": stable_id_from_path(f),
                        "name": f.name,
                        "path": str(f),
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                    }
                )
        return entries
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Listing failed for %s: %s", path, e)
        raise HTTPException(500, "Failed to list directory")

@app.get("/api/thumb/{slide_id}")
async def api_thumb(slide_id: str):
    ck = Cache.key("thumb", slide_id)
    try:
        raw = cache.get(ck)
    except Exception as e:
        log.debug("Thumb cache get failed: %s", e)
        raw = None
    if raw:
        return Response(content=raw, media_type="image/jpeg")
    try:
        p = resolve_by_id(slide_id)
    except FileNotFoundError:
        raise HTTPException(404, "Slide not found")
    try:
        img = make_preview_bytes(p, cfg.thumbnails.max_px, cfg.thumbnails.prefer_associated)
    except Exception as e:
        log.exception("Preview generation failed for %s: %s", p, e)
        raise HTTPException(500, "Failed to generate thumbnail")
    try:
        cache.setex(ck, cache.ttl_thumb, img)
    except Exception as e:
        log.debug("Thumb cache set failed: %s", e)
    return Response(content=img, media_type="image/jpeg")

@app.get("/api/meta/{slide_id}")
async def api_meta(slide_id: str):
    try:
        p = resolve_by_id(slide_id)
    except FileNotFoundError:
        raise HTTPException(404, "Slide not found")
    try:
        slide = openslide.open_slide(str(p))
    except Exception as e:
        log.exception("OpenSlide failed for %s: %s", p, e)
        raise HTTPException(500, "Failed to open slide")
    try:
        try:
            mpp_x_raw = slide.properties.get(openslide.PROPERTY_NAME_MPP_X, 0)
            mpp_y_raw = slide.properties.get(openslide.PROPERTY_NAME_MPP_Y, 0)
            mpp_x = float(mpp_x_raw or 0) or None
            mpp_y = float(mpp_y_raw or 0) or None
        except Exception:
            mpp_x = mpp_y = None
        
        # Get file size
        try:
            file_size = p.stat().st_size
        except Exception:
            file_size = None
            
        md = SlideMeta(
            id=slide_id,
            name=p.name,
            path=str(p),
            width=slide.dimensions[0],
            height=slide.dimensions[1],
            vendor=slide.properties.get(openslide.PROPERTY_NAME_VENDOR),
            objective_power=slide.properties.get(openslide.PROPERTY_NAME_OBJECTIVE_POWER),
            level_count=slide.level_count,
            mpp_x=mpp_x,
            mpp_y=mpp_y,
            created_ts=p.stat().st_mtime,
            file_size=file_size,  # Add this
        )
        return md
    except Exception as e:
        log.exception("Metadata read failed for %s: %s", p, e)
        raise HTTPException(500, "Failed to read metadata")
    finally:
        try:
            slide.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Deep‑Zoom endpoints
@app.get("/dzi/{slide_id}.dzi")
async def dzi_xml(slide_id: str):
    try:
        p = resolve_by_id(slide_id)
    except FileNotFoundError:
        raise HTTPException(404, "Slide not found")
    try:
        s = openslide.open_slide(str(p))
    except Exception as e:
        log.exception("OpenSlide failed for %s: %s", p, e)
        raise HTTPException(500, "Failed to open slide")
    try:
        dz = DZ(s)
        xml = dz.dzi_xml()
        return Response(content=xml, media_type="application/xml")
    except Exception as e:
        log.exception("DZI XML generation failed for %s: %s", p, e)
        raise HTTPException(500, "Failed to build DZI descriptor")
    finally:
        try:
            s.close()
        except Exception:
            pass

@app.get("/dzi/{slide_id}_files/{level}/{x}_{y}.jpeg")
async def dzi_tile(slide_id: str, level: int, x: int, y: int):
    ck = Cache.key("tile", slide_id, str(level), str(x), str(y))
    try:
        raw = cache.get(ck)
    except Exception as e:
        log.debug("Tile cache get failed: %s", e)
        raw = None
    if raw:
        return Response(content=raw, media_type="image/jpeg", headers={"Access-Control-Allow-Origin": "*"})
    try:
        p = resolve_by_id(slide_id)
    except FileNotFoundError:
        raise HTTPException(404, "Slide not found")
    try:
        s = openslide.open_slide(str(p))
    except Exception as e:
        log.exception("OpenSlide failed for %s: %s", p, e)
        raise HTTPException(500, "Failed to open slide")
    try:
        dz = DZ(s)
        if level < 0 or level >= dz.dz.level_count:
            raise HTTPException(404, "Invalid level")
        try:
            img = dz.tile_jpeg(level, x, y)
        except Exception:
            raise HTTPException(404, "Tile not found")
        try:
            cache.setex(ck, cache.ttl_tile, img)
        except Exception as e:
            log.debug("Tile cache set failed: %s", e)
        return Response(content=img, media_type="image/jpeg", headers={"Access-Control-Allow-Origin": "*"})
    except HTTPException:
        raise
    except Exception as e:
        log.exception(
            "Tile generation failed for %s level %s (%s,%s): %s",
            slide_id,
            level,
            x,
            y,
            e,
        )
        raise HTTPException(500, "Failed to generate tile")
    finally:
        try:
            s.close()
        except Exception:
            pass

@app.get("/api/associated/{slide_id}")
async def api_associated_list(slide_id: str):
    """List available associated images for a slide."""
    try:
        p = resolve_by_id(slide_id)
    except FileNotFoundError:
        raise HTTPException(404, "Slide not found")
    try:
        slide = openslide.open_slide(str(p))
        associated = list(slide.associated_images.keys())
        slide.close()
        return associated
    except Exception as e:
        log.exception("Failed to list associated images for %s: %s", p, e)
        raise HTTPException(500, "Failed to list associated images")

@app.get("/api/associated/{slide_id}/{image_name}")
async def api_associated_image(slide_id: str, image_name: str):
    """Get a specific associated image (label, macro, thumbnail, etc.)."""
    try:
        p = resolve_by_id(slide_id)
    except FileNotFoundError:
        raise HTTPException(404, "Slide not found")
    
    try:
        slide = openslide.open_slide(str(p))
        if image_name not in slide.associated_images:
            slide.close()
            raise HTTPException(404, f"Associated image '{image_name}' not found")
        
        img = slide.associated_images[image_name]
        slide.close()
        
        # Convert to RGB if needed
        if img.mode == "RGBA":
            img = img.convert("RGB")
        
        # Save to bytes
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return Response(content=buf.getvalue(), media_type="image/jpeg")
        
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Failed to get associated image %s for %s: %s", image_name, p, e)
        raise HTTPException(500, "Failed to get associated image")


# --------------------------------------------------------------------------- #
# Misc
@app.get("/health")
async def health():
    """Health check endpoint for Docker."""
    return {"status": "healthy", "service": "wsi-browser"}


@app.get("/static/{filename}")
async def serve_static(filename: str):
    """Serve static files including logo.svg/logo.png if they exist."""
    static_path = TEMPLATES_DIR / filename
    if filename in ["logo.svg", "logo.png"] and static_path.exists():
        content_type = "image/svg+xml" if filename.endswith(".svg") else "image/png"
        return Response(content=static_path.read_bytes(), media_type=content_type)
    raise HTTPException(404, "File not found")


if __name__ == "__main__":
    import argparse, uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default=None, help="Path to config.yml")
    args = parser.parse_args()

    cfg_path = Path(args.config) if args.config else CFG_PATH
    cfg = AppCfg.load(cfg_path)

    uvicorn.run("app.main:app", host="0.0.0.0", port=8010, reload=True)
