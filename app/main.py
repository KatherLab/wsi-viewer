from __future__ import annotations
import logging
import os
import json
import asyncio
import time
import pickle
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional
import stat as statmod
import io

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

import openslide
from PIL import Image

from .config import AppCfg
from .cache import make_cache, Cache
from .fs_index import scan_directory_shallow_optimized, stable_id_from_path, build_tree_shallow, nfs_probe
from .thumbs import make_preview_bytes
from .dz import DZ, MxTiffDZ, QptiffDZ, QptiffPool, is_multiplex_tiff, make_dz
from .models import SlideMeta, Node
from .path_cache import PathCache

# --------------------------------------------------------------------------- #
# Logging
log = logging.getLogger("wsi-browser")
logging.basicConfig(level=logging.INFO)

# --------------------------------------------------------------------------- #
# Thread pool for blocking I/O operations
executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="wsi-io")

# --------------------------------------------------------------------------- #
# ✅ Fix #3: Slide handle pool — avoids re-opening WSI files on every request
import threading
from collections import OrderedDict

class SlidePool:
    """Thread-safe LRU pool of OpenSlide handles. Avoids 100ms+ re-open per tile."""
    def __init__(self, max_handles: int = 24):
        self._lock = threading.Lock()
        self._handles: OrderedDict[str, openslide.OpenSlide] = OrderedDict()
        self._max = max_handles

    def get(self, path: Path) -> openslide.OpenSlide:
        key = str(path)
        with self._lock:
            if key in self._handles:
                # Move to end (most recently used)
                self._handles.move_to_end(key)
                return self._handles[key]

        # Open outside lock to avoid blocking other threads
        handle = openslide.open_slide(key)

        with self._lock:
            # Double-check: another thread may have opened it
            if key in self._handles:
                handle.close()
                self._handles.move_to_end(key)
                return self._handles[key]

            self._handles[key] = handle
            self._handles.move_to_end(key)

            # Evict oldest if over capacity
            while len(self._handles) > self._max:
                _, old_handle = self._handles.popitem(last=False)
                try:
                    old_handle.close()
                except Exception:
                    pass

        return handle

    def evict(self, path: Path):
        key = str(path)
        with self._lock:
            handle = self._handles.pop(key, None)
            if handle:
                try:
                    handle.close()
                except Exception:
                    pass

    def close_all(self):
        with self._lock:
            for handle in self._handles.values():
                try:
                    handle.close()
                except Exception:
                    pass
            self._handles.clear()

slide_pool = SlidePool(max_handles=24)
qptiff_pool = QptiffPool(max_handles=8)


# Connection limits for concurrent requests
MAX_CONCURRENT_THUMBNAILS = 8
MAX_CONCURRENT_TILES = 12
thumb_semaphore = asyncio.Semaphore(MAX_CONCURRENT_THUMBNAILS)
tile_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TILES)

# Request tracking for cancellation
active_requests = {}

# Set PIL Max Pixels to 500M
Image.MAX_IMAGE_PIXELS = 500_000_000

# --------------------------------------------------------------------------- #
# Config
default_path = Path(__file__).resolve().parent.parent / "config.yml"
cfg_path_str = os.getenv("WSI_CONFIG", str(default_path))
CFG_PATH = Path(cfg_path_str).resolve()
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

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
# Redis-backed path cache with local LRU read-through (falls back to pickle if Redis disabled)
path_cache_file = Path("/tmp/wsi_path_cache.json")
ns_hash = hashlib.sha1(str(CFG_PATH).encode()).hexdigest()[:12]
PATHCACHE_NS = f"wsi:path:{ns_hash}"
path_cache = PathCache(getattr(cache, "client", None), PATHCACHE_NS, path_cache_file, lru_cap=100_000)

def load_path_cache():
    """Load path cache from disk if Redis is disabled."""
    path_cache.load_pickle()

def save_path_cache():
    """Save path cache to disk if Redis is disabled."""
    path_cache.save_pickle()

# --------------------------------------------------------------------------- #
# Utilities

async def _watch_disconnect(request: Request, request_id: int):
    """Mark the request as cancelled when the client disconnects."""
    try:
        while True:
            if await request.is_disconnected():
                rec = active_requests.get(request_id)
                if rec is not None:
                    rec["cancelled"] = True
                break
            await asyncio.sleep(0.1)
    except Exception:
        pass

def _etag_bytes(*parts: bytes) -> str:
    """Build a weak ETag from given byte parts."""
    h = hashlib.sha1()
    for p in parts:
        h.update(p)
    return '"' + h.hexdigest() + '"'


def _etag_stable(*parts: str) -> str:
    """Build a weak ETag from stable string inputs (no response body needed)."""
    h = hashlib.sha1()
    for p in parts:
        h.update(p.encode())
    return 'W/"' + h.hexdigest() + '"'


def _get_mtime_str(p: Path) -> str:
    """Get file mtime as string for ETag computation."""
    try:
        return str(p.stat().st_mtime)
    except Exception:
        return "0"

def _dir_size_quick(root: Path, max_entries: int = 5000) -> int:
    """Iterative scandir walk to sum sizes; caps entries to avoid runaway on NFS."""
    total = 0
    todo = [root]
    seen = 0
    while todo and seen < max_entries:
        d = todo.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    if seen > max_entries:
                        return total
                    try:
                        if e.is_file(follow_symlinks=False):
                            try:
                                st = e.stat(follow_symlinks=False)
                                if statmod.S_ISREG(st.st_mode):
                                    total += st.st_size
                            except Exception:
                                pass
                        elif e.is_dir(follow_symlinks=False):
                            todo.append(Path(e.path))
                    except Exception:
                        continue
        except Exception:
            continue
    return total

def _mrxs_total_size(p: Path) -> Optional[int]:
    """Return .mrxs file size + same-stem directory size, if present."""
    try:
        if p.suffix.lower() != ".mrxs":
            return p.stat().st_size
        base = p.with_suffix("")  # slide.mrxs -> slide
        size = 0
        try:
            size += p.stat().st_size
        except Exception:
            pass
        if base.exists() and base.is_dir():
            size += _dir_size_quick(base)
        return size
    except Exception:
        return None

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
    slide_pool.close_all()
    qptiff_pool.close_all()
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
# Middleware for request tracking & disconnect watching
@app.middleware("http")
async def track_requests(request: Request, call_next):
    request_id = id(request)
    active_requests[request_id] = {"cancelled": False, "start_time": time.time()}
    watcher = asyncio.create_task(_watch_disconnect(request, request_id))
    try:
        response = await call_next(request)
        return response
    finally:
        watcher.cancel()
        active_requests.pop(request_id, None)

# --------------------------------------------------------------------------- #
app.mount("/static", StaticFiles(directory=str(TEMPLATES_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# --------------------------------------------------------------------------- #
# Helpers
ROOTS = {str(Path(r.path).resolve()): r.label for r in cfg.roots}
EXTS = set([e.lower() for e in cfg.extensions])

# ✅ P0: Path traversal guard — prevents access outside configured roots
def _is_under_root(path: Path) -> bool:
    """Check that a resolved path is under one of the configured roots."""
    resolved = str(path.resolve())
    return any(resolved == root or resolved.startswith(root + os.sep) for root in ROOTS.keys())

# ✅ P2: NFS probe wrapper that runs in executor with a timeout
async def _async_nfs_probe(root: Path, timeout: float = 5.0) -> bool:
    """Run nfs_probe in the thread pool with a timeout."""
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(executor, nfs_probe, root),
            timeout=timeout
        )
        return result
    except asyncio.TimeoutError:
        log.error(f"NFS probe TIMED OUT for {root} after {timeout}s")
        return False
    except Exception as e:
        log.error(f"NFS probe exception for {root}: {e}")
        return False

def resolve_by_id_with_fallback(slide_id: str) -> Path:
    """Fast path resolution using shared cache with fallback to search."""
    # Validate slide_id format (must be 16 hex chars from stable_id_from_path)
    if not slide_id or len(slide_id) != 16 or not all(c in '0123456789abcdef' for c in slide_id):
        raise FileNotFoundError(f"Invalid slide id: {slide_id}")

    # 1) Try cache (local LRU -> Redis)
    p = path_cache.get(slide_id)
    if p and p.exists():
        # ✅ Fix #1: Validate cached path is still under a configured root
        if not _is_under_root(p):
            path_cache.delete(slide_id)
            raise FileNotFoundError(f"Slide path escapes root: {slide_id}")
        return p

    # 2) Not in cache — bounded search across roots
    #    ✅ Fix #2: Limit walk depth to avoid thread pool starvation on NFS
    log.info(f"Cache miss for {slide_id}, searching across roots (bounded)...")
    MAX_WALK_DEPTH = 5
    MAX_FILES_CHECKED = 50_000

    files_checked = 0
    for base in ROOTS.keys():
        for root, dirs, files in os.walk(base):
            # Enforce max depth
            depth = root.replace(base, "").count(os.sep)
            if depth >= MAX_WALK_DEPTH:
                dirs.clear()  # Don't descend further
                continue

            for f in files:
                files_checked += 1
                if files_checked > MAX_FILES_CHECKED:
                    log.warning(f"Fallback search hit {MAX_FILES_CHECKED} file limit without finding {slide_id}")
                    raise FileNotFoundError(f"Slide id not found after bounded search: {slide_id}")

                path = Path(root) / f
                if path.suffix.lower() in EXTS:
                    file_id = stable_id_from_path(path)
                    path_cache.set(file_id, path)
                    if file_id == slide_id:
                        return path

    raise FileNotFoundError(f"Slide id not found: {slide_id}")


def update_path_cache_from_dir(dir_path: Path, extensions: list[str]):
    """Update path cache when listing a directory (bulk)."""
    pairs = []
    try:
        with os.scandir(dir_path) as scanner:
            for entry in scanner:
                if entry.is_file(follow_symlinks=False):
                    name_lower = entry.name.lower()
                    if any(name_lower.endswith(ext) for ext in extensions):
                        slide_id = stable_id_from_path(Path(entry.path))
                        pairs.append((slide_id, entry.path))
        if pairs:
            path_cache.mset(pairs)
    except Exception as e:
        log.debug(f"Could not update path cache for {dir_path}: {e}")

async def run_with_timeout(func, *args, timeout=30, **kwargs):
    """Run a blocking function in executor with timeout."""
    import functools
    loop = asyncio.get_event_loop()
    try:
        if kwargs:
            func = functools.partial(func, **kwargs)
        future = loop.run_in_executor(executor, func, *args)
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
    return templates.TemplateResponse(request, "index.html", {"roots": ROOTS})

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

        # ✅ P2: NFS health probe — if mount is unresponsive, return optimistic placeholder
        if not await _async_nfs_probe(base_path, timeout=5.0):
            log.error(f"NFS mount unresponsive: {base} — returning optimistic placeholder")
            trees.append({
                "id": stable_id_from_path(base_path),
                "name": label or base_path.name,
                "path": base,
                "is_dir": True,
                "children": None,
                "slide_count": 0,
                "has_children": True,  # ✅ Optimistic — let user try expanding later
            })
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
                timeout=60
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

            # ✅ P0: Only cache if we actually found children or slides.
            #        If the scan returned empty (possible NFS glitch), do NOT cache it.
            if children or slide_count > 0:
                try:
                    cache.setex(k, cache.ttl_tree, json.dumps(data).encode())
                except Exception as ce:
                    log.debug("Tree cache set failed: %s", ce)
            else:
                log.warning(f"Empty tree result for {base} — NOT caching (possible NFS issue)")

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
                "has_children": True,
            })

    return trees

@app.get("/api/expand")
async def api_expand(path: str, request: Request):
    """Expand a directory to get its immediate children."""
    request_id = id(request)

    try:
        dirp = Path(path)

        # ✅ P0: Path traversal protection
        if not _is_under_root(dirp):
            raise HTTPException(403, "Access denied")

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
            timeout=30
        )

        children.sort(key=lambda n: (n.slide_count == 0, n.name.lower()))

        result = [child.model_dump() for child in children]

        # ✅ P0: Only cache non-empty expand results
        if result:
            try:
                cache.setex(k, cache.ttl_tree, json.dumps(result).encode())
            except Exception as e:
                log.debug(f"Expand cache set failed: {e}")
        else:
            log.warning(f"Empty expand result for {path} — NOT caching (possible NFS issue)")

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

        # Path traversal protection
        if not _is_under_root(p):
            raise HTTPException(403, "Access denied")

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

                        # Update shared path cache
                        path_cache.set(slide_id, Path(entry.path))

                        entries.append({
                            "id": slide_id,
                            "name": entry.name,
                            "path": entry.path,
                            "size": stat.st_size,
                            "mtime": int(stat.st_mtime),
                        })

            return entries

        entries = await run_with_timeout(list_slides_optimized, timeout=20)
        return entries

    except HTTPException:
        raise
    except Exception as e:
        log.exception("Listing failed for %s: %s", path, e)
        raise HTTPException(500, "Failed to list directory")

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
            # ✅ Fix #5: Stable ETag — doesn't need the body
            etag = _etag_stable("thumb", slide_id, str(len(raw)))
            if request.headers.get("If-None-Match") == etag:
                return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=86400"})
            return Response(
                content=raw,
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=86400", "ETag": etag}
            )

        if active_requests.get(request_id, {}).get("cancelled"):
            raise HTTPException(499, "Client closed request")

        try:
            p = resolve_by_id_with_fallback(slide_id)
        except FileNotFoundError:
            raise HTTPException(404, "Slide not found")

        # ✅ Fix #5: Compute ETag from stable inputs BEFORE generating the thumbnail
        mtime_str = _get_mtime_str(p)
        etag = _etag_stable("thumb", slide_id, mtime_str)

        # Check If-None-Match before doing expensive work
        if request.headers.get("If-None-Match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=86400"})

        try:
            timeout = 10 if priority > 500 else 15
            if is_multiplex_tiff(p):
                def make_mxtiff_thumb():
                    dz = qptiff_pool.get(p)
                    return dz.thumbnail_jpeg(cfg.thumbnails.max_px)

                img = await run_with_timeout(make_mxtiff_thumb, timeout=timeout)
            else:
                img = await run_with_timeout(
                    make_preview_bytes,
                    p,
                    cfg.thumbnails.max_px,
                    cfg.thumbnails.prefer_associated,
                    slide_pool,
                    timeout=timeout,
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
            headers={"Cache-Control": "public, max-age=86400", "ETag": etag}
        )

@app.get("/api/meta/{slide_id}")
async def api_meta(slide_id: str):
    try:
        p = resolve_by_id_with_fallback(slide_id)
    except FileNotFoundError:
        raise HTTPException(404, "Slide not found")

    def get_metadata():
        if is_multiplex_tiff(p):
            dz = qptiff_pool.get(p)

            try:
                file_size = p.stat().st_size
            except Exception:
                file_size = None

            return SlideMeta(
                id=slide_id,
                name=p.name,
                path=str(p),
                width=dz.width,
                height=dz.height,
                vendor="QPTIFF / mxtifffile",
                objective_power=None,
                level_count=dz.level_count,
                mpp_x=None,
                mpp_y=None,
                created_ts=p.stat().st_mtime,
                file_size=file_size,
            )

        slide = slide_pool.get(p)
        try:
            mpp_x_raw = slide.properties.get(openslide.PROPERTY_NAME_MPP_X, 0)
            mpp_y_raw = slide.properties.get(openslide.PROPERTY_NAME_MPP_Y, 0)
            mpp_x = float(mpp_x_raw or 0) or None
            mpp_y = float(mpp_y_raw or 0) or None
        except Exception:
            mpp_x = mpp_y = None

        try:
            if p.suffix.lower() == ".mrxs":
                file_size = _mrxs_total_size(p)
            else:
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

    try:
        md = await run_with_timeout(get_metadata, timeout=10)
        return md
    except Exception as e:
        log.exception("Metadata read failed for %s: %s", p, e)
        raise HTTPException(500, "Failed to read metadata")

@app.get("/api/associated/{slide_id}")
async def api_associated_list(slide_id: str):
    try:
        p = resolve_by_id_with_fallback(slide_id)
    except FileNotFoundError:
        raise HTTPException(404, "Slide not found")

    def get_associated():
        if is_multiplex_tiff(p):
            return []
        slide = slide_pool.get(p)
        return list(slide.associated_images.keys())

    try:
        associated = await run_with_timeout(get_associated, timeout=10)
        return associated
    except Exception as e:
        log.exception("Failed to list associated images for %s: %s", p, e)
        raise HTTPException(500, "Failed to list associated images")

@app.get("/api/associated/{slide_id}/{image_name}")
async def api_associated_image(slide_id: str, image_name: str):
    try:
        p = resolve_by_id_with_fallback(slide_id)
    except FileNotFoundError:
        raise HTTPException(404, "Slide not found")

    def get_image():
        if is_multiplex_tiff(p):
            return None
        slide = slide_pool.get(p)
        if image_name not in slide.associated_images:
            return None

        img = slide.associated_images[image_name]

        if img.mode == "RGBA":
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()


    try:
        img_bytes = await run_with_timeout(get_image, timeout=10)
        if img_bytes is None:
            raise HTTPException(404, f"Associated image '{image_name}' not found")
        return Response(content=img_bytes, media_type="image/jpeg")
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Failed to get associated image %s for %s: %s", image_name, p, e)
        raise HTTPException(500, "Failed to get associated image")

@app.api_route("/logo", methods=["GET", "HEAD"])
async def logo(request: Request):
    for name, ctype in (("logo.svg", "image/svg+xml"), ("logo.png", "image/png")):
        p = STATIC_DIR / name
        if p.exists():
            etag = f'W/"{hashlib.sha1(p.read_bytes()).hexdigest()}"'
            headers = {"Cache-Control": "public, max-age=86400", "ETag": etag}
            if request.method == "HEAD":
                return Response(status_code=200, media_type=ctype, headers=headers)
            return Response(content=p.read_bytes(), media_type=ctype, headers=headers)
    raise HTTPException(404, "File not found")


# --------------------------------------------------------------------------- #
# Deep-Zoom endpoints
@app.get("/dzi/{slide_id}.dzi")
async def dzi_xml(slide_id: str, request: Request):
    try:
        p = resolve_by_id_with_fallback(slide_id)
    except FileNotFoundError:
        raise HTTPException(404, "Slide not found")

    def get_dzi():
        dz = make_dz(p, slide_pool)
        return dz.dzi_xml()

    try:
        xml = await run_with_timeout(get_dzi, timeout=10)
        # Small, compute ETag on XML too
        etag = _etag_bytes(slide_id.encode(), xml.encode("utf-8"))
        if request.headers.get("If-None-Match") == etag:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=3600"})
        return Response(content=xml, media_type="application/xml", headers={"ETag": etag, "Cache-Control": "public, max-age=3600"})
    except Exception as e:
        log.exception("DZI XML generation failed for %s: %s", p, e)
        raise HTTPException(500, "Failed to build DZI descriptor")

@app.get("/dzi/{slide_id}_files/{level}/{x}_{y}.jpeg")
async def dzi_tile(
    slide_id: str,
    level: int,
    x: int,
    y: int,
    request: Request,
    channels: Optional[str] = None,
    colors: Optional[str] = None,
):
    async with tile_semaphore:
        request_id = id(request)

        try:
            p_for_etag = resolve_by_id_with_fallback(slide_id)
            mtime_str = _get_mtime_str(p_for_etag)
        except FileNotFoundError:
            raise HTTPException(404, "Slide not found")

        backend_key = "mxtiff" if is_multiplex_tiff(p_for_etag) else "openslide"

        # Parse optional channel/color overrides
        channels_list: list[str] | None = None
        colors_list: list[str] | None = None
        if channels:
            channels_list = [c.strip() for c in channels.split(",") if c.strip()]
        if colors:
            colors_list = [c.strip() for c in colors.split(",") if c.strip()]

        etag = _etag_stable(
            "tile",
            backend_key,
            slide_id,
            str(level),
            str(x),
            str(y),
            mtime_str,
            channels or "",
            colors or "",
        )

        if request.headers.get("If-None-Match") == etag:
            return Response(
                status_code=304,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "public, max-age=3600",
                    "ETag": etag
                }
            )

        ck = Cache.key("tile", backend_key, slide_id, str(level), str(x), str(y), channels or "", colors or "")
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
                    "Cache-Control": "public, max-age=3600",
                    "ETag": etag
                }
            )

        if active_requests.get(request_id, {}).get("cancelled"):
            raise HTTPException(499, "Client closed request")

        p = p_for_etag  # Already resolved above

        def get_tile():
            if is_multiplex_tiff(p):
                dz = qptiff_pool.get(p)
            else:
                dz = make_dz(p, slide_pool)
            if level < 0 or level >= dz.level_count:
                raise HTTPException(404, "Invalid level")
            try:
                if is_multiplex_tiff(p):
                    return dz.tile_jpeg(level, x, y, channels=channels_list, colors=colors_list)
                return dz.tile_jpeg(level, x, y)
            except Exception:
                raise HTTPException(404, f"Tile not found at level {level}, ({x},{y})")

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
                    "Cache-Control": "public, max-age=3600",
                    "ETag": etag
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            if "Tile not found" not in str(e):
                log.exception("Tile generation failed for %s level %s (%s,%s): %s", slide_id, level, x, y, e)
            raise HTTPException(500, "Failed to generate tile")

# --------------------------------------------------------------------------- #
@app.get("/api/markers/{slide_id}")
async def api_markers(slide_id: str):
    try:
        p = resolve_by_id_with_fallback(slide_id)
    except FileNotFoundError:
        raise HTTPException(404, "Slide not found")

    if not is_multiplex_tiff(p):
        return {
            "backend": "openslide",
            "markers": [],
        }

    def get_markers():
        dz = qptiff_pool.get(p)
        markers = dz.get_markers()
        active_markers = dz.markers  # currently displayed markers (up to 3)
        active_colors = dz.colors  # colors mapped to active markers
        # Build a mapping of ALL markers → colors; use palette assignment for extras
        marker_colors = {}
        for i, marker in enumerate(active_markers):
            marker_colors[marker] = active_colors[i] if i < len(active_colors) else "gray"
        # Assign colors to any markers not in the active set
        assigned = set(active_markers)
        palette = QptiffDZ.PALETTE
        palette_idx = len(active_markers)
        for marker in markers:
            if marker not in assigned:
                marker_colors[marker] = palette[palette_idx % len(palette)]
                palette_idx += 1
        return {
            "backend": "mxtifffile",
            "format": dz.get_format_id(),
            "markers": markers,
            "default_markers": active_markers,
            "marker_colors": marker_colors,
            "dimensions": {"width": dz.width, "height": dz.height},
            "level_count": dz.level_count,
        }

    try:
        return await run_with_timeout(get_markers, timeout=10)
    except Exception as e:
        log.exception("Marker read failed for %s: %s", p, e)
        raise HTTPException(500, "Failed to read markers")

# --------------------------------------------------------------------------- #
@app.get("/api/qptiff/{slide_id}/channels")
async def api_qptiff_channels(slide_id: str):
    """Returns marker/channel info for a QPTIFF slide."""
    try:
        p = resolve_by_id_with_fallback(slide_id)
    except FileNotFoundError:
        raise HTTPException(404, "Slide not found")

    if not is_multiplex_tiff(p):
        raise HTTPException(400, "Not a QPTIFF file")

    def get_channels():
        dz = qptiff_pool.get(p)
        markers = dz.get_markers()
        active_markers = dz.markers
        active_colors = dz.colors
        marker_colors = {}
        for i, marker in enumerate(active_markers):
            marker_colors[marker] = active_colors[i] if i < len(active_colors) else "gray"
        # Assign colors to any markers not in the active set
        assigned = set(active_markers)
        palette = QptiffDZ.PALETTE
        palette_idx = len(active_markers)
        for marker in markers:
            if marker not in assigned:
                marker_colors[marker] = palette[palette_idx % len(palette)]
                palette_idx += 1
        return {
            "markers": markers,
            "default_markers": active_markers,
            "marker_colors": marker_colors,
            "dimensions": {"width": dz.width, "height": dz.height},
            "level_count": dz.level_count,
            "format_id": dz.get_format_id(),
        }

    try:
        return await run_with_timeout(get_channels, timeout=10)
    except Exception as e:
        log.exception("Channels read failed for %s: %s", p, e)
        raise HTTPException(500, "Failed to read channels")


# --------------------------------------------------------------------------- #
@app.get("/health")
async def health():
    """
    Health check endpoint for Docker.
    ✅ P2: Now probes NFS mounts. If ANY root is unresponsive, returns 503
    so Docker marks the container unhealthy and restarts it.
    """
    has_redis = bool(cache.client)

    nfs_status = {}
    all_healthy = True

    probe_tasks = {
        base: _async_nfs_probe(Path(base), timeout=5.0)
        for base in ROOTS.keys()
    }
    results = await asyncio.gather(*probe_tasks.values(), return_exceptions=True)

    for base, result in zip(probe_tasks.keys(), results):
        alive = result is True  # exceptions and False both mean unhealthy
        nfs_status[base] = "ok" if alive else "UNREACHABLE"
        if not alive:
            all_healthy = False

    status_code = 200 if all_healthy else 503
    body = {
        "status": "healthy" if all_healthy else "degraded",
        "service": "wsi-browser",
        "cache": "redis" if has_redis else "noop",
        "nfs_mounts": nfs_status,
        "ttl": {"tree": cache.ttl_tree, "thumb": cache.ttl_thumb, "tile": cache.ttl_tile},
    }

    if not all_healthy:
        log.error(f"Health check FAILED — NFS mounts degraded: {nfs_status}")

    return Response(
        content=json.dumps(body),
        media_type="application/json",
        status_code=status_code,
    )
