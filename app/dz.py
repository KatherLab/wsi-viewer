from __future__ import annotations
import io
from pathlib import Path
import openslide
from openslide.deepzoom import DeepZoomGenerator

class DZ:
    def __init__(self, slide: openslide.OpenSlide, tile_size=256, overlap=0):
        self.slide = slide
        self.dz = DeepZoomGenerator(slide, tile_size=tile_size, overlap=overlap, limit_bounds=True)
        self.tile_size = tile_size
        self.overlap = overlap

    def dzi_xml(self) -> str:
        w, h = self.slide.dimensions
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<Image xmlns="http://schemas.microsoft.com/deepzoom/2008" TileSize="{self.tile_size}" Overlap="{self.overlap}" Format="jpeg">
  <Size Width="{w}" Height="{h}"/>
</Image>'''

    def tile_jpeg(self, level: int, x: int, y: int) -> bytes:
        tile = self.dz.get_tile(level, (x, y))
        buf = io.BytesIO()
        tile.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
