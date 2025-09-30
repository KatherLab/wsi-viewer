from __future__ import annotations
from pathlib import Path
import os
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
import asyncio
from .models import Node

log = logging.getLogger(__name__)

# Cache for stat operations to avoid repeated filesystem calls
stat_cache = {}

def stable_id_from_path(p: Path) -> str:
    """Deterministic 16‑char ID derived from the absolute path."""
    return hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:16]

def should_skip(name: str, exclude: list[str]) -> bool:
    lname = name.lower()
    for ex in exclude:
        exl = ex.lower()
        if exl.startswith("*") or exl.startswith(".") or any(c in exl for c in "*?[]"):
            if exl.strip("*") in lname:
                return True
        elif exl in lname:
            return True
    return False

def get_cached_stat(path: Path):
    """Get cached stat result to avoid repeated filesystem calls."""
    path_str = str(path)
    if path_str not in stat_cache:
        try:
            stat_cache[path_str] = path.stat()
        except:
            stat_cache[path_str] = None
    return stat_cache[path_str]

def scan_directory_shallow_optimized(dirp: Path, extensions: list[str], exclude: list[str]) -> tuple[list[Node], int]:
    """
    Optimized directory scan using os.scandir which is more efficient than Path.iterdir()
    for NFS as it makes fewer system calls.
    """
    children = []
    slide_count = 0
    
    try:
        # os.scandir is more efficient as it gets stat info in one call
        with os.scandir(dirp) as entries:
            # Convert to list immediately to avoid keeping directory handle open
            entry_list = list(entries)
            
        for entry in entry_list:
            try:
                if should_skip(entry.name, exclude):
                    continue
                
                # Use entry.is_dir() which uses cached stat from scandir
                if entry.is_dir(follow_symlinks=False):
                    # For directories, just check if it exists, don't count slides yet
                    child_node = Node(
                        id=stable_id_from_path(Path(entry.path)),
                        name=entry.name,
                        path=entry.path,
                        is_dir=True,
                        children=None,
                        slide_count=-1,  # -1 indicates not counted yet
                        has_children=None  # Will be determined on demand
                    )
                    children.append(child_node)
                    
                elif entry.is_file(follow_symlinks=False):
                    # Only check extension, avoid full stat unless needed
                    name_lower = entry.name.lower()
                    for ext in extensions:
                        if name_lower.endswith(ext):
                            slide_count += 1
                            break
                            
            except (PermissionError, OSError):
                continue
                
    except (PermissionError, OSError) as e:
        log.debug(f"Cannot list directory {dirp}: {e}")
    
    return children, slide_count

def quick_has_children(dirp: Path, exclude: list[str]) -> bool | None:
    """
    Very quick check if directory has subdirectories.
    Returns None if unknown (to avoid slow operations).
    """
    try:
        # Just check first few entries, don't scan everything
        with os.scandir(dirp) as entries:
            for i, entry in enumerate(entries):
                if i > 10:  # Only check first 10 entries
                    return True  # Assume it has children if many entries
                if entry.is_dir(follow_symlinks=False) and not should_skip(entry.name, exclude):
                    return True
        return False
    except:
        return None
