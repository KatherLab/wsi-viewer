# AGENTS.md — WSI Browser Project Guide

## Overview

WSI Browser is a web-based whole-slide image viewer for digital pathology. It uses OpenSlide and a custom QPTIFF backend for tile generation, FastAPI for the backend, and Vue.js 3 + OpenSeadragon for the frontend. The entire UI is a single Jinja2 template with inline Vue 3.

## Project Structure

```
app/
├── main.py                  # FastAPI app, routes, slide pool, startup/shutdown
├── config.py                # Pydantic-based config (AppCfg, CacheCfg, RootCfg, ThumbCfg)
├── cache.py                 # Redis cache wrapper + noop fallback
├── dz.py                    # DZ class (OpenSlide Deep Zoom adapter), re-exports QptiffDZ
├── fs_index.py              # Directory scanning, NFS probing, path-to-ID hashing
├── models.py                # Pydantic models (Node, SlideMeta)
├── thumbs.py                # Thumbnail generation utilities
├── path_cache.py            # Redis + local LRU path cache with pickle fallback
├── deepzoom_backends/       # QPTIFF-specific DZ backend
│   ├── __init__.py
│   ├── factory.py           # make_dz_backend() factory, is_qptiff() check
│   ├── qptiff_dz.py         # QptiffDZ class for multiplex TIFF files
│   └── qptiff_pool.py       # QptiffPool LRU pool for QPTIFF handles
├── templates/
│   └── index.html           # Single-file Vue 3 frontend (all JS/CSS inline)
├── static/                  # Static assets (logo.svg, logo.png)
```

## Key Concepts

### Slide Resolution
- Slides are identified by a deterministic 16-char hex hash of their absolute path (`stable_id_from_path`).
- Path resolution uses a multi-tier cache: local LRU → Redis → bounded filesystem walk.

### Deep Zoom Tiles
- Two backends exist: `DZ` (OpenSlide-based) and `QptiffDZ` (for `.qptiff` multiplex files).
- The factory in `deepzoom_backends/factory.py` dispatches based on file extension.
- QPTIFF tiles support channel/color overrides via query parameters.

### Caching Layers
- **Redis**: Shared cache for tiles (`ttl_tile`), thumbnails (`ttl_thumb`), directory trees (`ttl_tree`), and path lookups.
- **Local pickle fallback**: Path cache persists to `/tmp/wsi_path_cache.json` when Redis is disabled.
- **Slide handle pools**: `SlidePool` (OpenSlide, 24 handles) and `QptiffPool` (QPTIFF, 8 handles) avoid re-opening files per request.
- **ETags**: Strong/weak ETags on tiles, thumbnails, and DZI XML for HTTP caching.

### Frontend
- Single-page Vue 3 app in `index.html` (no build step, loaded from CDN).
- Grid + list view modes with virtual scrolling via IntersectionObserver.
- Thumbnail queue with priority-based loading (viewport-aware).
- Request cancellation on directory change, filter, or unmount.
- OpenSeadragon viewer with scale bar overlay (Canvas-based ruler).
- QPTIFF marker legend in the sidebar.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Web UI |
| GET | `/api/tree` | Root directory tree (shallow) |
| GET | `/api/expand?path=` | Expand a directory |
| GET | `/api/dir?path=` | List slides in directory |
| GET | `/api/thumb/{slide_id}` | Slide thumbnail (JPEG) |
| GET | `/api/meta/{slide_id}` | Slide metadata |
| GET | `/api/markers/{slide_id}` | QPTIFF marker info |
| GET | `/api/qptiff/{slide_id}/channels` | QPTIFF channel info |
| GET | `/api/associated/{slide_id}` | List associated images |
| GET | `/api/associated/{slide_id}/{name}` | Get associated image |
| GET | `/dzi/{slide_id}.dzi` | Deep Zoom descriptor |
| GET | `/dzi/{slide_id}_files/{level}/{x}_{y}.jpeg` | Deep Zoom tile |
| GET | `/logo` | Branding logo |
| GET | `/health` | Health check (NFS probes + Redis status) |

## Concurrency Model
- `ThreadPoolExecutor(max_workers=8)` for blocking I/O (OpenSlide reads, file scanning).
- `asyncio.Semaphore` limits: 8 concurrent thumbnails, 12 concurrent tiles.
- Request cancellation via `active_requests` dict + disconnect watcher coroutine.
- Frontend limits to 4 concurrent thumbnail fetches (`thumbSlots: 4`).

## NFS Considerations
- NFS health probe at startup and on `/health` endpoint.
- Bounded directory walks (max depth 5, max 50k files per search).
- Optimistic fallback when NFS is unresponsive: empty tree results are not cached.
- Timeouts on all executor operations (10-60s depending on endpoint).

## Configuration
- YAML config file (default: `config.yml`, override via `WSI_CONFIG` env var).
- Configured roots (slide directories), file extensions, exclusions, cache/Redis settings, thumbnail options, CORS origins.
- See `config.example.yml` for the full schema.

## Common Tasks

### Adding a new API endpoint
1. Add the route handler in `app/main.py`.
2. Use `run_with_timeout` for blocking I/O.
3. Add ETag support for cacheable responses.
4. Add the route to the frontend if needed.

### Adding a new slide format
1. Add the extension to `config.example.yml` and your `config.yml`.
2. If OpenSlide supports it, no code changes needed.
3. For custom backends, add a new class in `deepzoom_backends/` and register it in `factory.py`.

### Modifying the frontend
- All UI code is in `app/templates/index.html`.
- Vue 3 app starts at line ~588 (`createApp`).
- The ruler/scale bar is the `attachRuler()` function at the bottom.
- CSS variables are at the top of the `<style>` block.

### Debugging
- Enable the debug panel by setting `debug: true` in the Vue data (line ~623).
- Check `/health` for NFS and Redis status.
- Redis cache keys use the format `wsi:|tree/thumb/tile|...` with hashing for long keys.

## Running

```bash
# Development
uv run uvicorn app.main:app --host 0.0.0.0 --port 8010 --reload

# Docker
docker-compose build && docker-compose up -d
```

## Testing & Linting

```bash
uv run pytest
uv run ruff check app/
uv run mypy app/
```
