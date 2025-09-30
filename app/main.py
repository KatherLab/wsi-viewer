from __future__ import annotations
import logging
import os
import json
import asyncio
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import lru_cache
import pickle

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

import openslide
import io

from .config import AppCfg
from .cache import make_cache, Cache
from .fs_index import scan_directory_shallow_optimized, stable_id_from_path
from .thumbs import make_preview_bytes
from .dz import DZ
from .models import SlideMeta, Node

# --------------------------------------------------------------------------- #
# Logging
log = logging.getLogger("wsi-browser")
logging.basicConfig(level=logging.INFO)

# --------------------------------------------------------------------------- #
# Thread pool for blocking I/O operations
executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="wsi-io")

# Connection limits for concurrent requests
MAX_CONCURRENT_THUMBNAILS = 3
MAX_CONCURRENT_TILES = 6
thumb_semaphore = asyncio.Semaphore(MAX_CONCURRENT_THUMBNAILS)
tile_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TILES)

# Request tracking for cancellation
active_requests = {}

# Path resolution cache - CRITICAL for NFS performance
path_cache = {}  # slide_id -> path mapping
path_cache_file = Path("/tmp/wsi_path_cache.pkl")

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
def load_path_cache():
    """Load path cache from disk if it exists."""
    global path_cache
    if path_cache_file.exists():
        try:
            with open(path_cache_file, 'rb') as f:
                path_cache = pickle.load(f)
                log.info(f"Loaded {len(path_cache)} cached paths")
        except Exception as e:
            log.warning(f"Could not load path cache: {e}")
            path_cache = {}

def save_path_cache():
    """Save path cache to disk."""
    try:
        with open(path_cache_file, 'wb') as f:
            pickle.dump(path_cache, f)
    except Exception as e:
        log.warning(f"Could not save path cache: {e}")

# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    log.info("Starting WSI Browser...")
    load_path_cache()
    yield
    # Shutdown
    log.info("Shutting down...")
    save_path_cache()
    executor.shutdown(wait=False, cancel_futures=True)

app = FastAPI(title="WSI Browser", version="0.1", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------------- #
# Middleware for request tracking
@app.middleware("http")
async def track_requests(request: Request, call_next):
    request_id = id(request)
    active_requests[request_id] = {"cancelled": False, "start_time": time.time()}
    
    try:
        response = await call_next(request)
        return response
    finally:
        active_requests.pop(request_id, None)

# --------------------------------------------------------------------------- #
app.mount("/static", StaticFiles(directory=str(TEMPLATES_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# --------------------------------------------------------------------------- #
# Helpers
ROOTS = {str(Path(r.path).resolve()): r.label for r in cfg.roots}
EXTS = set([e.lower() for e in cfg.extensions])

def resolve_by_id_fast(slide_id: str) -> Path:
    """Fast path resolution using cache."""
    # Check memory cache first
    if slide_id in path_cache:
        p = Path(path_cache[slide_id])
        if p.exists():
            return p
        else:
            # Cached path no longer exists
            del path_cache[slide_id]
    
    # Not in cache - we need to find it (this will be slow the first time)
    # But we'll cache it for future use
    raise FileNotFoundError(f"Slide id not found in cache: {slide_id}")

def update_path_cache_from_dir(dir_path: Path, extensions: list[str]):
    """Update path cache when listing a directory."""
    try:
        with os.scandir(dir_path) as scanner:
            for entry in scanner:
                if entry.is_file(follow_symlinks=False):
                    name_lower = entry.name.lower()
                    if any(name_lower.endswith(ext) for ext in extensions):
                        slide_id = stable_id_from_path(Path(entry.path))
                        path_cache[slide_id] = entry.path
    except Exception as e:
        log.debug(f"Could not update path cache for {dir_path}: {e}")

async def run_with_timeout(func, *args, timeout=30, **kwargs):
    """Run a blocking function in executor with timeout."""
    loop = asyncio.get_event_loop()
    try:
        future = loop.run_in_executor(executor, func, *args, **kwargs)
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        log.warning(f"Operation timed out after {timeout}s: {func.__name__}")
        raise HTTPException(504, "Operation timed out")
    except Exception as e:
        log.exception(f"Operation failed: {func.__name__}")
        raise

# --------------------------------------------------------------------------- #
# Routes
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "roots": ROOTS})

@app.get("/api/tree")
async def api_tree():
    """Get root directories with shallow loading."""
    trees = []
    for base, label in ROOTS.items():
        base_path = Path(base)
        
        if not base_path.exists():
            log.warning(f"Root path does not exist: {base}")
            trees.append({
                "id": stable_id_from_path(base_path),
                "name": label or base_path.name,
                "path": base,
                "is_dir": True,
                "children": None,
                "slide_count": 0,
                "has_children": False,
            })
            continue
            
        if not base_path.is_dir():
            log.warning(f"Root path is not a directory: {base}")
            continue
            
        k = Cache.key("tree_shallow", base)
        try:
            raw = cache.get(k)
            if raw:
                log.debug(f"Using cached tree for {base}")
                trees.append(json.loads(raw))
                continue
                
            log.info(f"Building shallow tree for {base}")
            
            children, slide_count = await run_with_timeout(
                scan_directory_shallow_optimized,
                base_path,
                list(EXTS),
                cfg.exclude,
                timeout=10
            )
            
            node = Node(
                id=stable_id_from_path(base_path),
                name=label or base_path.name,
                path=base,
                is_dir=True,
                children=children if children else None,
                slide_count=slide_count,
                has_children=len(children) > 0
            )
                
            data = node.model_dump()
            
            try:
                cache.setex(k, cache.ttl_tree, json.dumps(data).encode())
            except Exception as ce:
                log.debug("Tree cache set failed: %s", ce)
                
            trees.append(data)
            
        except HTTPException:
            raise
        except Exception as e:
            log.exception("Tree build failed for %s: %s", base, e)
            trees.append({
                "id": stable_id_from_path(base_path),
                "name": label or base_path.name,
                "path": base,
                "is_dir": True,
                "children": None,
                "slide_count": 0,
                "has_children": False,
            })
            
    return trees

@app.get("/api/expand")
async def api_expand(path: str, request: Request):
    """Expand a directory to get its immediate children."""
    request_id = id(request)
    
    try:
        dirp = Path(path)
        
        if not dirp.exists() or not dirp.is_dir():
            raise HTTPException(404, "Directory not found")
        
        # Update path cache while we're scanning
        update_path_cache_from_dir(dirp, list(EXTS))
        
        # Check cache first
        k = Cache.key("expand", path)
        try:
            raw = cache.get(k)
            if raw:
                log.debug(f"Using cached expansion for {path}")
                return json.loads(raw)
        except Exception as e:
            log.debug(f"Expand cache get failed: {e}")
        
        if active_requests.get(request_id, {}).get("cancelled"):
            raise HTTPException(499, "Client closed request")
        
        log.info(f"Expanding directory: {path}")
        
        children, slide_count = await run_with_timeout(
            scan_directory_shallow_optimized,
            dirp,
            list(EXTS),
            cfg.exclude,
            timeout=15
        )
        
        children.sort(key=lambda n: (n.slide_count == 0, n.name.lower()))
        
        result = [child.model_dump() for child in children]
        
        try:
            cache.setex(k, cache.ttl_tree, json.dumps(result).encode())
        except Exception as e:
            log.debug(f"Expand cache set failed: {e}")
        
        # Save path cache periodically
        if len(path_cache) % 100 == 0:
            save_path_cache()
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        log.exception(f"Failed to expand directory {path}: {e}")
        raise HTTPException(500, "Failed to expand directory")

@app.get("/api/dir")
async def api_dir(path: str, request: Request):
    request_id = id(request)
    
    try:
        p = Path(path)
        if not p.exists() or not p.is_dir():
            raise HTTPException(404, "Directory not found")
        
        def list_slides_optimized():
            entries = []
            
            with os.scandir(p) as scanner:
                all_entries = list(scanner)
            
            for entry in all_entries:
                if active_requests.get(request_id, {}).get("cancelled"):
                    break
                
                if entry.is_file(follow_symlinks=False):
                    name_lower = entry.name.lower()
                    is_slide = any(name_lower.endswith(ext) for ext in EXTS)
                    
                    if is_slide:
                        stat = entry.stat(follow_symlinks=False)
                        slide_id = stable_id_from_path(Path(entry.path))
                        
                        # Update path cache
                        path_cache[slide_id] = entry.path
                        
                        entries.append({
                            "id": slide_id,
                            "name": entry.name,
                            "path": entry.path,
                            "size": stat.st_size,
                            "mtime": int(stat.st_mtime),
                        })
            
            return entries
            
        entries = await run_with_timeout(list_slides_optimized, timeout=20)
        
        # Save cache after updating
        if len(path_cache) % 50 == 0:
            save_path_cache()
        
        return entries
        
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Listing failed for %s: %s", path, e)
        raise HTTPException(500, "Failed to list directory")

# For all endpoints that need to resolve slides, use the fast cached version
@app.get("/api/thumb/{slide_id}")
async def api_thumb(slide_id: str, request: Request):
    priority = int(request.headers.get("X-Priority", "0"))
    
    async with thumb_semaphore:
        request_id = id(request)
        
        ck = Cache.key("thumb", slide_id)
        try:
            raw = cache.get(ck)
        except Exception:
            raw = None
            
        if raw:
            return Response(
                content=raw, 
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=86400"}
            )
            
        if active_requests.get(request_id, {}).get("cancelled"):
            raise HTTPException(499, "Client closed request")
            
        try:
            # Use fast cached resolution
            p = resolve_by_id_fast(slide_id)
        except FileNotFoundError:
            # If not in cache, return 404 immediately
            # The slide will be cached when its directory is listed
            raise HTTPException(404, "Slide not found - please navigate to its directory first")
            
        try:
            timeout = 10 if priority > 500 else 15
            img = await run_with_timeout(
                make_preview_bytes,
                p,
                cfg.thumbnails.max_px,
                cfg.thumbnails.prefer_associated,
                timeout=timeout
            )
        except Exception as e:
            log.exception("Preview generation failed for %s: %s", p, e)
            raise HTTPException(500, "Failed to generate thumbnail")
            
        try:
            cache.setex(ck, cache.ttl_thumb, img)
        except Exception:
            pass
            
        return Response(
            content=img, 
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"}
        )

@app.get("/api/meta/{slide_id}")
async def api_meta(slide_id: str):
    try:
        p = resolve_by_id_fast(slide_id)
    except FileNotFoundError:
        raise HTTPException(404, "Slide not found - please navigate to its directory first")
        
    def get_metadata():
        slide = openslide.open_slide(str(p))
        try:
            try:
                mpp_x_raw = slide.properties.get(openslide.PROPERTY_NAME_MPP_X, 0)
                mpp_y_raw = slide.properties.get(openslide.PROPERTY_NAME_MPP_Y, 0)
                mpp_x = float(mpp_x_raw or 0) or None
                mpp_y = float(mpp_y_raw or 0) or None
            except Exception:
                mpp_x = mpp_y = None
                
            try:
                file_size = p.stat().st_size
            except Exception:
                file_size = None
                
            return SlideMeta(
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
                file_size=file_size,
            )
        finally:
            slide.close()
            
    try:
        md = await run_with_timeout(get_metadata, timeout=10)
        return md
    except Exception as e:
        log.exception("Metadata read failed for %s: %s", p, e)
        raise HTTPException(500, "Failed to read metadata")

@app.get("/api/associated/{slide_id}")
async def api_associated_list(slide_id: str):
    try:
        p = resolve_by_id_fast(slide_id)
    except FileNotFoundError:
        raise HTTPException(404, "Slide not found - please navigate to its directory first")
        
    def get_associated():
        slide = openslide.open_slide(str(p))
        try:
            return list(slide.associated_images.keys())
        finally:
            slide.close()
            
    try:
        associated = await run_with_timeout(get_associated, timeout=10)
        return associated
    except Exception as e:
        log.exception("Failed to list associated images for %s: %s", p, e)
        raise HTTPException(500, "Failed to list associated images")

@app.get("/api/associated/{slide_id}/{image_name}")
async def api_associated_image(slide_id: str, image_name: str):
    try:
        p = resolve_by_id_fast(slide_id)
    except FileNotFoundError:
        raise HTTPException(404, "Slide not found - please navigate to its directory first")
        
    def get_image():
        slide = openslide.open_slide(str(p))
        try:
            if image_name not in slide.associated_images:
                raise HTTPException(404, f"Associated image '{image_name}' not found")
                
            img = slide.associated_images[image_name]
            
            if img.mode == "RGBA":
                img = img.convert("RGB")
                
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            return buf.getvalue()
        finally:
            slide.close()
            
    try:
        img_bytes = await run_with_timeout(get_image, timeout=10)
        return Response(content=img_bytes, media_type="image/jpeg")
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Failed to get associated image %s for %s: %s", image_name, p, e)
        raise HTTPException(500, "Failed to get associated image")

@app.get("/static/{filename}")
async def serve_static(filename: str):
    static_path = TEMPLATES_DIR / filename
    if filename in ["logo.svg", "logo.png"] and static_path.exists():
        content_type = "image/svg+xml" if filename.endswith(".svg") else "image/png"
        return Response(content=static_path.read_bytes(), media_type=content_type)
    raise HTTPException(404, "File not found")

@app.get("/dzi/{slide_id}.dzi")
async def dzi_xml(slide_id: str):
    try:
        p = resolve_by_id_fast(slide_id)
    except FileNotFoundError:
        raise HTTPException(404, "Slide not found - please navigate to its directory first")
        
    def get_dzi():
        s = openslide.open_slide(str(p))
        try:
            dz = DZ(s)
            return dz.dzi_xml()
        finally:
            s.close()
            
    try:
        xml = await run_with_timeout(get_dzi, timeout=10)
        return Response(content=xml, media_type="application/xml")
    except Exception as e:
        log.exception("DZI XML generation failed for %s: %s", p, e)
        raise HTTPException(500, "Failed to build DZI descriptor")

@app.get("/dzi/{slide_id}_files/{level}/{x}_{y}.jpeg")
async def dzi_tile(slide_id: str, level: int, x: int, y: int, request: Request):
    async with tile_semaphore:
        request_id = id(request)
        
        ck = Cache.key("tile", slide_id, str(level), str(x), str(y))
        try:
            raw = cache.get(ck)
        except Exception:
            raw = None
            
        if raw:
            return Response(
                content=raw, 
                media_type="image/jpeg", 
                headers={
                    "Access-Control-Allow-Origin": "*", 
                    "Cache-Control": "public, max-age=3600"
                }
            )
            
        if active_requests.get(request_id, {}).get("cancelled"):
            raise HTTPException(499, "Client closed request")
            
        try:
            p = resolve_by_id_fast(slide_id)
        except FileNotFoundError:
            raise HTTPException(404, "Slide not found - please navigate to its directory first")
            
        def get_tile():
            s = openslide.open_slide(str(p))
            try:
                dz = DZ(s)
                if level < 0 or level >= dz.dz.level_count:
                    raise HTTPException(404, "Invalid level")
                return dz.tile_jpeg(level, x, y)
            finally:
                s.close()
                
        try:
            img = await run_with_timeout(get_tile, timeout=10)
            
            try:
                cache.setex(ck, cache.ttl_tile, img)
            except Exception:
                pass
                
            return Response(
                content=img, 
                media_type="image/jpeg", 
                headers={
                    "Access-Control-Allow-Origin": "*", 
                    "Cache-Control": "public, max-age=3600"
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            log.exception("Tile generation failed for %s level %s (%s,%s): %s", slide_id, level, x, y, e)
            raise HTTPException(500, "Failed to generate tile")

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "wsi-browser"}

@app.on_event("shutdown")
async def shutdown_event():
    save_path_cache()
    executor.shutdown(wait=False, cancel_futures=True)
