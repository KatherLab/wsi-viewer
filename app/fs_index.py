from __future__ import annotations
from pathlib import Path
import os
import hashlib
import logging
from .models import Node

log = logging.getLogger(__name__)

def stable_id_from_path(p: Path) -> str:
    """Deterministic 16‑char ID derived from the absolute path."""
    return hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:16]

def should_skip(name: str, exclude: list[str]) -> bool:
    lname = name.lower()
    for ex in exclude:
        exl = ex.lower()
        # Very simple glob‑ish handling – treat anything with *
        if exl.startswith("*") or exl.startswith(".") or any(c in exl for c in "*?[]"):
            if exl.strip("*") in lname:
                return True
        elif exl in lname:
            return True
    return False

def build_tree(root_path: Path, extensions: list[str], exclude: list[str]) -> Node:
    root_path = root_path.resolve()
    
    def walk(dirp: Path, depth: int = 0, max_depth: int = 20) -> Node:
        # Prevent infinite recursion
        if depth > max_depth:
            log.warning(f"Max depth {max_depth} reached at {dirp}")
            return Node(
                id=stable_id_from_path(dirp),
                name=dirp.name or str(dirp),
                path=str(dirp),
                is_dir=True,
                children=[],
                slide_count=0,
            )
        
        children = []
        slide_count = 0
        
        try:
            # Use iterdir() instead of scandir() for better compatibility
            entries = sorted(dirp.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
            
            for entry_path in entries:
                try:
                    # Get the name for filtering
                    entry_name = entry_path.name
                    
                    if should_skip(entry_name, exclude):
                        continue
                    
                    # Check if it's a directory or file
                    if entry_path.is_dir():
                        # Recursively process subdirectory
                        child_node = walk(entry_path, depth + 1, max_depth)
                        # Only add directories that contain slides or have children
                        if child_node.children or child_node.slide_count > 0:
                            children.append(child_node)
                            slide_count += child_node.slide_count
                    elif entry_path.is_file():
                        # Check if it's a slide file
                        if entry_path.suffix.lower() in extensions:
                            slide_count += 1
                            
                except (PermissionError, OSError) as e:
                    log.debug(f"Cannot access {entry_path}: {e}")
                    continue
                    
        except (PermissionError, OSError) as e:
            log.warning(f"Cannot list directory {dirp}: {e}")
        
        node = Node(
            id=stable_id_from_path(dirp),
            name=dirp.name or str(dirp),
            path=str(dirp),
            is_dir=True,
            children=sorted(
                children,
                key=lambda n: (n.slide_count == 0, n.name.lower()),
            ),
            slide_count=slide_count,
        )
        
        return node
    
    return walk(root_path)
