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
    file_size: int | None = None  # Add this field
