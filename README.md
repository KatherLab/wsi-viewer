# WSI Browser

A modern, high-performance whole-slide image (WSI) viewer for digital pathology, built with FastAPI, Vue.js, and OpenSeadragon.

![Python](https://img.shields.io/badge/python-3.13-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.104-green.svg)
![Vue](https://img.shields.io/badge/Vue.js-3-brightgreen.svg)
![Docker](https://img.shields.io/badge/Docker-ready-blue.svg)
![License](https://img.shields.io/badge/license-MIT-blue.svg)

## Features

- 🔬 **High-Performance Viewing**: Smooth pan/zoom of gigapixel pathology images using OpenSeadragon
- 📏 **Smart Scale Bar**: Automatic scale bar with µm/mm measurements based on slide metadata
- 🗂️ **File Browser**: Hierarchical folder navigation with search and filtering
- 🖼️ **Thumbnails**: Fast preview generation with intelligent caching
- 📊 **Metadata Display**: View slide properties, scanner info, resolution, and associated images
- 🚀 **Redis Caching**: Optional Redis backend for tile, thumbnail, and directory tree caching
- 🎯 **Modern Stack**: Python 3.13, FastAPI, Vue.js 3, and containerized deployment
- 🔒 **Production Ready**: Docker setup with health checks, non-root user, and optimized builds

## Screenshots

<details>
<summary>Click to view screenshots</summary>

### Grid View
Browse slides with thumbnails and file information

### Slide Viewer
Pan/zoom with scale bar, metadata panel, and associated images

### Directory Tree
Hierarchical navigation with slide counts

</details>

## Quick Start

### Using Docker (Recommended)

1. **Clone the repository**
```bash
git clone https://github.com/KatherLab/wsi-browser.git
cd wsi-browser
```

2. **Configure your slide directories**

Edit `docker-compose.yml` to mount your slide directories:
```yaml
volumes:
  - /path/to/your/slides:/path/to/your/slides:ro
```

Create a `config.yml` file (you can copy the content of `config.example.yml`) to reference the mounted paths:
```yaml
roots:
  - path: "/path/to/your/slides"
    label: "My Slides"
```

3. **Build and run**
```bash
docker-compose build
docker-compose up -d
```

4. **Access the application**
Open your browser to: `http://localhost:8010`

### Local Development

1. **Install dependencies with uv**
```bash
pip install uv
uv sync
```

2. **Configure `config.yml`**
```yaml
roots:
  - path: "/path/to/slides"
    label: "Slide Collection"
    
cache:
  enabled: true
  redis_url: "redis://localhost:6379/0"
```

3. **Run the application**
```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8010 --reload
```

## Configuration

### `config.yml` Reference

```yaml
# Slide directories to expose in the UI
roots:
  - path: "/data/slides"
    label: "Research Slides"
  - path: "/data/clinical"
    label: "Clinical Cases"

# Files/folders to exclude
exclude:
  - "__pycache__"
  - "*.tmp"
  - ".git"

# Supported slide formats
extensions:
  - ".svs"      # Aperio
  - ".tif"      # Generic TIFF
  - ".tiff"     
  - ".ndpi"     # Hamamatsu
  - ".scn"      # Leica
  - ".mrxs"     # Mirax
  - ".bif"      # Ventana

# Redis caching configuration
cache:
  enabled: true
  redis_url: "redis://redis:6379/0"  # Use "redis" hostname in Docker
  ttl_seconds:
    tree: 60        # Directory tree cache
    thumb: 86400    # Thumbnail cache (24h)
    tile: 3600      # Tile cache (1h)

# Thumbnail generation
thumbnails:
  max_px: 512               # Maximum thumbnail dimension
  prefer_associated: true   # Use embedded thumbnails when available

# CORS settings
cors_allow_origins: ["*"]   # Restrict in production
```

## Project Structure

```
wsi-poc-viewer/
├── app/
│   ├── __init__.py
│   ├── main.py           # FastAPI application
│   ├── cache.py          # Redis caching layer
│   ├── config.py         # Configuration management
│   ├── dz.py            # Deep Zoom tile generation
│   ├── fs_index.py      # File system indexing
│   ├── models.py        # Pydantic models
│   ├── thumbs.py        # Thumbnail generation
│   ├── templates/
│   │   └── index.html   # Vue.js frontend
│   └── static/
│       ├── logo.svg     # Optional branding
│       └── logo.png
├── Dockerfile           # Production container
├── docker-compose.yml   # Service orchestration
├── pyproject.toml      # Python dependencies
├── config.yml          # Application configuration
└── README.md
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Web UI |
| `GET /api/tree` | Directory tree structure |
| `GET /api/dir?path=...` | List slides in directory |
| `GET /api/thumb/{slide_id}` | Slide thumbnail |
| `GET /api/meta/{slide_id}` | Slide metadata |
| `GET /api/associated/{slide_id}` | List associated images |
| `GET /api/associated/{slide_id}/{name}` | Get associated image |
| `GET /dzi/{slide_id}.dzi` | Deep Zoom descriptor |
| `GET /dzi/{slide_id}_files/{z}/{x}_{y}.jpeg` | Deep Zoom tiles |
| `GET /health` | Health check |

## Supported Formats

The application supports all formats readable by OpenSlide:

- **Aperio** (.svs, .tif)
- **Hamamatsu** (.ndpi, .vms, .vmu)
- **Leica** (.scn)
- **MIRAX** (.mrxs)
- **Philips** (.tiff)
- **Sakura** (.svslide)
- **Trestle** (.tif)
- **Ventana** (.bif, .tif)
- **Generic tiled TIFF** (.tif, .tiff)

## Performance Optimization

### Caching Strategy
- **Redis**: Stores tiles, thumbnails, and directory trees
- **TTL Configuration**: Customizable expiration times
- **Memory Management**: 2GB Redis memory limit with LRU eviction

### Production Settings
- **Multiple Workers**: 4 Uvicorn workers by default
- **Read-only Mounts**: Slide directories mounted read-only
- **Health Checks**: Automated monitoring for both services
- **Non-root User**: Enhanced security in containers

## Troubleshooting

### Common Issues

**Slides not appearing**
- Check file extensions in `config.yml`
- Verify directory permissions
- Check Docker volume mounts

**Performance issues**
- Increase Redis memory limit in `docker-compose.yml`
- Adjust worker count based on CPU cores
- Check network latency to slide storage

**Connection errors**
```bash
# Check service status
docker-compose ps

# View logs
docker-compose logs -f

# Test Redis connection
docker-compose exec redis redis-cli ping

# Check application health
curl http://localhost:8010/health
```

## Development

### Adding Features

1. **Backend changes**: Modify files in `app/`
2. **Frontend changes**: Edit `app/templates/index.html`
3. **Rebuild container**: `docker-compose build`
4. **Restart services**: `docker-compose up -d`

### Running Tests
```bash
uv sync --dev
uv run pytest
```

### Code Quality
```bash
uv run ruff check app/
uv run mypy app/
```

## Deployment

### Production Checklist

- [ ] Restrict CORS origins in `config.yml`
- [ ] Use specific domains instead of `*`
- [ ] Set up SSL/TLS termination (nginx/traefik)
- [ ] Configure monitoring (Prometheus/Grafana)
- [ ] Set up log aggregation
- [ ] Implement authentication if needed
- [ ] Regular Redis backups (optional - cache only)

### Scaling

For large deployments:
- Use external Redis cluster
- Deploy multiple app instances behind load balancer
- Consider CDN for static assets
- Use distributed file system for slides (NFS/GlusterFS)

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- [OpenSlide](https://openslide.org/) - C library for reading WSI files
- [OpenSeadragon](https://openseadragon.github.io/) - Web-based viewer for high-resolution images
- [FastAPI](https://fastapi.tiangolo.com/) - Modern Python web framework
- [Vue.js](https://vuejs.org/) - Progressive JavaScript framework

## Support

For issues and questions:
- Open an issue on GitHub
- Check existing issues for solutions
- Provide logs and configuration when reporting bugs

---
