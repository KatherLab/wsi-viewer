# Quick test of QptiffDZ against the real file
# This is a temporary testing script and will be deleted.

import sys
sys.path.insert(0, ".")

from app.deepzoom_backends.qptiff_dz import QptiffDZ

INPUT_QPTIFF = "/Users/wolffabia/Downloads/slides/E-21-15175_Scan1.unmixed.qptiff"
dz = QptiffDZ(INPUT_QPTIFF)

print(f"Width: {dz.width}")
print(f"Height: {dz.height}")
print(f"Native levels: {dz._native_levels}")
print(f"Max DZI level: {dz.max_dzi_level}")
print(f"DZI level count: {dz.level_count}")
print(f"Markers: {dz.markers}")
print(f"Colors: {dz.colors}")

print("\n=== DZI -> Native mapping ===")
for l in range(dz.level_count):
    native_lvl, down = dz._dzi_to_native_level(l)
    print(f"  DZI {l:2d} -> native {native_lvl} (downsample={down}x)")

# Try reading tiles at various levels
print("\n=== Tile reads ===")
for lvl in range(5):
    for tx in range(2):
        for ty in range(2):
            try:
                data = dz.tile_jpeg(lvl, tx, ty)
                print(f"  DZI {lvl}, tile ({tx},{ty}): {len(data)} bytes OK")
            except Exception as e:
                err = str(e).split('\n')[0]
                print(f"  DZI {lvl}, tile ({tx},{ty}): FAILED - {err}")

print("\n=== Thumbnail ===")
thumb = dz.thumbnail_jpeg(512)
print(f"  {len(thumb)} bytes")

print("\n=== DONE ===")
