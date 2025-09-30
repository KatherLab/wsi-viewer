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

def scan_directory_shallow(dirp: Path, extensions: list[str], exclude: list[str]) -> tuple[list[Node], int]:
    """
    Scan a directory one level deep only.
    Returns (child_nodes, total_slide_count)
    """
    children = []
    slide_count = 0
    
    try:
        entries = list(dirp.iterdir())
        entries.sort(key=lambda x: (x.is_file(), x.name.lower()))
        
        for entry_path in entries:
            try:
                entry_name = entry_path.name
                
                if should_skip(entry_name, exclude):
                    continue
                
                if entry_path.is_dir():
                    # For directories, just count slides in this dir (not recursive)
                    dir_slide_count = count_slides_in_directory(entry_path, extensions, exclude, recursive=False)
                    
                    # Create a node with hasChildren flag but no actual children yet
                    child_node = Node(
                        id=stable_id_from_path(entry_path),
                        name=entry_name,
                        path=str(entry_path),
                        is_dir=True,
                        children=None,  # Will be loaded on demand
                        slide_count=dir_slide_count,
                        has_children=check_has_subdirs(entry_path, exclude)
                    )
                    
                    children.append(child_node)
                    slide_count += dir_slide_count
                    
                elif entry_path.is_file():
                    # Check if it's a slide file
                    if entry_path.suffix.lower() in extensions:
                        slide_count += 1
                        
            except (PermissionError, OSError) as e:
                log.debug(f"Cannot access {entry_path}: {e}")
                continue
                
    except (PermissionError, OSError) as e:
        log.warning(f"Cannot list directory {dirp}: {e}")
    
    return children, slide_count

def check_has_subdirs(dirp: Path, exclude: list[str]) -> bool:
    """Quick check if a directory has any subdirectories."""
    try:
        for entry in dirp.iterdir():
            if entry.is_dir() and not should_skip(entry.name, exclude):
                return True
    except (PermissionError, OSError):
        pass
    return False

def count_slides_in_directory(dirp: Path, extensions: list[str], exclude: list[str], recursive: bool = True) -> int:
    """Count slide files in a directory (optionally recursive)."""
    count = 0
    
    try:
        if recursive:
            for root, dirs, files in os.walk(dirp):
                # Filter out excluded directories
                dirs[:] = [d for d in dirs if not should_skip(d, exclude)]
                
                for file in files:
                    if not should_skip(file, exclude):
                        if Path(file).suffix.lower() in extensions:
                            count += 1
        else:
            # Non-recursive: just count files in this directory
            for entry in dirp.iterdir():
                if entry.is_file() and not should_skip(entry.name, exclude):
                    if entry.suffix.lower() in extensions:
                        count += 1
                        
    except (PermissionError, OSError) as e:
        log.debug(f"Cannot count slides in {dirp}: {e}")
    
    return count

def build_tree_shallow(root_path: Path, extensions: list[str], exclude: list[str]) -> Node:
    """Build only the top level of the tree."""
    root_path = root_path.resolve()
    
    children, slide_count = scan_directory_shallow(root_path, extensions, exclude)
    
    # Sort children: directories with slides first, then by name
    children.sort(key=lambda n: (n.slide_count == 0, n.name.lower()))
    
    return Node(
        id=stable_id_from_path(root_path),
        name=root_path.name or str(root_path),
        path=str(root_path),
        is_dir=True,
        children=children,
        slide_count=slide_count,
        has_children=len(children) > 0
    )
