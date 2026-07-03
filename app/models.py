from __future__ import annotations
from pathlib import Path
from pydantic import BaseModel


class Node(BaseModel):
    id: str
    name: str
    path: str
    is_dir: bool
    children: list["Node"] | None = None
    slide_count: int = 0
    has_children: bool = False  # Must be bool, not optional


class SlideMeta(BaseModel):
    id: str
    name: str
    path: str
    width: int
    height: int
    vendor: str | None = None
    objective_power: str | None = None
    level_count: int
    mpp_x: float | None = None
    mpp_y: float | None = None
    created_ts: float
    file_size: int | None = None
    # Derived from vendor properties (best-effort; None when unavailable)
    scan_date: str | None = None
    scanner_model: str | None = None
    slide_label: str | None = None
    quickhash: str | None = None
    pyramid: list[dict] | None = None
    # Coarsest DZI level safe to render (0 = no floor). >0 for single-level /
    # shallow-pyramid slides whose coarse tiles would decode gigabytes; the
    # frontend forbids zooming out below this level.
    min_safe_level: int = 0
