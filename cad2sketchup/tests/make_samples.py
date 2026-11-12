"""Generate sample DXF drawings used to test the converter."""
import sys
from pathlib import Path

import ezdxf
from ezdxf.enums import TextEntityAlignment

out = Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
out.mkdir(parents=True, exist_ok=True)

# --- 1. Floor plan in millimetres -------------------------------------------
doc = ezdxf.new("R2018", setup=True)
doc.header["$INSUNITS"] = 4  # mm
for name, color in [("A-WALL", 7), ("A-COLS", 1), ("A-DOOR", 3), ("A-FLOR", 8),
                    ("A-GLAZ", 4), ("A-FURN", 30), ("A-ANNO-TEXT", 2), ("A-DIMS", 2)]:
    doc.layers.add(name, color=color)
msp = doc.modelspace()

# Exterior wall ring: outer 10m x 7m, inner offset 230 mm -> becomes a hollow ring
msp.add_lwpolyline([(0, 0), (10000, 0), (10000, 7000), (0, 7000)], close=True, dxfattribs={"layer": "A-WALL"})
msp.add_lwpolyline([(230, 230), (9770, 230), (9770, 6770), (230, 6770)], close=True, dxfattribs={"layer": "A-WALL"})
# Interior partition (closed, 115 mm thick)
msp.add_lwpolyline([(6000, 230), (6115, 230), (6115, 4000), (6000, 4000)], close=True, dxfattribs={"layer": "A-WALL"})
# Floor slab as a hatch
h = msp.add_hatch(color=8, dxfattribs={"layer": "A-FLOR"})
h.paths.add_polyline_path([(0, 0), (10000, 0), (10000, 7000), (0, 7000)], is_closed=True)

# Column as a block, inserted 4 times
blk = doc.blocks.new("COLUMN")
blk.add_lwpolyline([(-150, -150), (150, -150), (150, 150), (-150, 150)], close=True, dxfattribs={"layer": "0"})
for x, y in [(3000, 2500), (3000, 4500), (8000, 2500), (8000, 4500)]:
    msp.add_blockref("COLUMN", (x, y), dxfattribs={"layer": "A-COLS"})

# Door swings (open arcs + leaf lines) -> stay as edges
msp.add_line((6000, 4000), (6000, 4900), dxfattribs={"layer": "A-DOOR"})
msp.add_arc((6000, 4000), 900, 90, 180, dxfattribs={"layer": "A-DOOR"})
# Window: line with 'thickness' -> vertical glass strip
msp.add_line((2000, 115), (4000, 115), dxfattribs={"layer": "A-GLAZ", "thickness": 1500})
# Round table (circle) on furniture layer
msp.add_circle((8000, 5800), 500, dxfattribs={"layer": "A-FURN"})
# Annotation that should be skipped
msp.add_text("LIVING", dxfattribs={"layer": "A-ANNO-TEXT", "height": 250}).set_placement((2000, 3500))
d = msp.add_linear_dim(base=(0, -600), p1=(0, 0), p2=(10000, 0), dxfattribs={"layer": "A-DIMS"})
d.render()
doc.saveas(out / "floorplan_mm.dxf")

# --- 2. A tiny 3D drawing in inches (3DFACE + polyface mesh) ------------------
doc = ezdxf.new("R2018"); doc.header["$INSUNITS"] = 1
msp = doc.modelspace()
msp.add_3dface([(0, 0, 0), (48, 0, 0), (48, 24, 0), (0, 24, 0)], dxfattribs={"layer": "TOP", "color": 5})
pf = msp.add_polyface()
pf.append_face([(0, 0, 30), (48, 0, 30), (48, 24, 30), (0, 24, 30)])
pf.append_face([(0, 0, 0), (48, 0, 0), (48, 0, 30), (0, 0, 30)])
pf.optimize()
pf.dxf.layer = "BOX"
msp.add_spline([(0, 30, 0), (20, 50, 0), (40, 30, 0), (60, 60, 0)], dxfattribs={"layer": "CURVES"})
doc.saveas(out / "desk_3d_inches.dxf")
print("samples written to", out)
