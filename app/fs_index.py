from __future__ import annotations
from pathlib import Path
import os
import hashlib
import logging
import fnmatch
from .models import Node

log = logging.getLogger(__name__)

def stable_id_from_path(p: Path) -> str:
    """Deterministic 16-char ID derived from the absolute path."""
    return hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:16]

def should_skip(name: str, exclude: list[str]) -> bool:
    """
    Check if a file/folder name should be excluded.
    
    ✅ P3: Plain patterns (no glob chars) now match as exact names, not substrings.
           Old behavior: exclude "tmp" would also skip "attempt_p1" and "comptmp_data".
           New behavior: exclude "tmp" only skips entries named exactly "tmp".
           Glob patterns like "*.zip" still work via fnmatch.
    """
    lname = name.lower()
    for ex in exclude:
        exl = ex.lower()
        if any(ch in exl for ch in "*?[]"):
            # Glob pattern — use fnmatch
            if fnmatch.fnmatch(lname, exl):
                return True
        else:
            # Plain string — exact match only (case-insensitive)
            if lname == exl:
                return True
    return False


def nfs_probe(root: Path) -> bool:
    """Quick check that a directory (NFS mount) is responsive."""
    try:
        root.stat()
        with os.scandir(root) as it:
            next(it, None)
        return True
    except Exception as e:
        log.error(f"NFS probe FAILED for {root}: {e}")
        return False

def scan_directory_shallow_optimized(dirp: Path, extensions: list[str], exclude: list[str]) -> tuple[list[Node], int]:
    """
    Optimized directory scan using os.scandir for better NFS performance.
    """
    children = []
    slide_count = 0

    try:
        with os.scandir(dirp) as entries:
            entry_list = list(entries)

        for entry in entry_list:
            try:
                if should_skip(entry.name, exclude):
                    continue

                if entry.is_dir(follow_symlinks=False):
                    has_children = quick_has_subdirs(Path(entry.path), exclude)

                    child_node = Node(
                        id=stable_id_from_path(Path(entry.path)),
                        name=entry.name,
                        path=entry.path,
                        is_dir=True,
                        children=None,
                        slide_count=0,
                        has_children=has_children
                    )
                    children.append(child_node)

                elif entry.is_file(follow_symlinks=False):
                    name_lower = entry.name.lower()
                    for ext in extensions:
                        if name_lower.endswith(ext):
                            slide_count += 1
                            break

            except (PermissionError, OSError):
                continue

    # ✅ P1: Elevated from log.debug → log.warning so NFS errors are visible
    except (PermissionError, OSError) as e:
        log.warning(f"Cannot list directory {dirp}: {e}")

    return children, slide_count

def quick_has_subdirs(dirp: Path, exclude: list[str]) -> bool:
    """
    Quick check if directory has subdirectories.
    ✅ P1: On ANY error, returns True (optimistic) instead of False,
           so folders aren't incorrectly hidden when NFS hiccups.
    ✅ P1: Bare except replaced with explicit OSError catches.
    """
    try:
        with os.scandir(dirp) as entries:
            for i, entry in enumerate(entries):
                if i > 10:
                    return True
                try:
                    if entry.is_dir(follow_symlinks=False) and not should_skip(entry.name, exclude):
                        return True
                except OSError as e:
                    log.warning(f"quick_has_subdirs: entry check failed in {dirp}: {e}")
                    continue
        return False
    except OSError as e:
        log.warning(f"quick_has_subdirs: scandir failed for {dirp}: {e}")
        return True  # ✅ Optimistic default — assume children exist

def build_tree_shallow(root_path: Path, extensions: list[str], exclude: list[str]) -> Node:
    """Build only the top level of the tree."""
    root_path = root_path.resolve()

    children, slide_count = scan_directory_shallow_optimized(root_path, extensions, exclude)

    children.sort(key=lambda n: (n.slide_count == 0, n.name.lower()))

    return Node(
        id=stable_id_from_path(root_path),
        name=root_path.name or str(root_path),
        path=str(root_path),
        is_dir=True,
        children=children if children else None,
        slide_count=slide_count,
        has_children=len(children) > 0
    )

def build_tree(root_path: Path, extensions: list[str], exclude: list[str]) -> Node:
    """Full recursive tree building (fallback)."""
    root_path = root_path.resolve()

    def walk(dirp: Path, depth: int = 0, max_depth: int = 20) -> Node:
        if depth > max_depth:
            log.warning(f"Max depth {max_depth} reached at {dirp}")
            return Node(
                id=stable_id_from_path(dirp),
                name=dirp.name or str(dirp),
                path=str(dirp),
                is_dir=True,
                children=None,
                slide_count=0,
                has_children=False
            )

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
                        child_node = walk(entry_path, depth + 1, max_depth)
                        if child_node.children or child_node.slide_count > 0:
                            children.append(child_node)
                            slide_count += child_node.slide_count
                    elif entry_path.is_file():
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
            children=children if children else None,
            slide_count=slide_count,
            has_children=len(children) > 0
        )

        return node

    return walk(root_path)
