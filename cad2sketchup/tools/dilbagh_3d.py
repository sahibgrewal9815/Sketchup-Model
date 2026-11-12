#!/usr/bin/env python3
"""
Build a 3D SketchUp model of the Dilbagh ground-floor plan.

The DWG keeps walls on layer "0" together with everything else, so walls are found
geometrically: every pair of parallel lines 3"-15" apart is a wall strip. Window
frames (layers TALLO, 4, Layer13) inside wall gaps get a sill wall and a lintel.

Usage:  python tools/dilbagh_3d.py input/Dilbagh.dwg output/
Writes: Dilbagh_3D.dae / .rb / .png / .json

All sizes in inches (the drawing is in inches even though it says feet).
Change the numbers in SETTINGS to adjust the model.
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "converter"))

import cad2skp as C  # noqa: E402
import dwgjson  # noqa: E402
from ezdxf import disassemble  # noqa: E402
from ezdxf.math import Vec3  # noqa: E402
from shapely.geometry import LineString, Point, Polygon, box  # noqa: E402
from shapely.ops import unary_union  # noqa: E402

SETTINGS = {
    "wall_height": 120,        # 10'-0"
    "boundary_wall_height": 60,  # 5'-0" compound wall
    "sill_height": 36,         # 3'-0"
    "lintel_height": 84,       # 7'-0" top of doors / windows
    "slab_thickness": 5,
    "parapet_height": 36,
    "parapet_thickness": 4.5,
    "wall_min": 3, "wall_max": 15,   # wall thickness range used to detect walls
    # plan A on the sheet (ground floor) in drawing coordinates
    "plan_window": (58262, -41175, 58919, -40210),
}

# House zones (drawing coordinates). Inside these, walls are full height and get a roof.
HOUSE_ZONES = [
    box(58280, -40345, 58900, -40230),   # back rooms band (uses the plot wall)
    box(58355, -40805, 58900, -40335),   # main house
    box(58660, -41150, 58900, -40945),   # garage
]
PORCH_SLAB = box(58348, -40857, 58571, -40788)


def load_lines(dwg: Path, window):
    with tempfile.TemporaryDirectory() as tmp:
        data = dwgjson.dump_json(dwg, Path(tmp))
    doc = dwgjson.build_doc(data, [])
    x0, y0, x1, y1 = window
    by = defaultdict(list)
    for p in disassemble.to_primitives(disassemble.recursive_decompose(doc.modelspace())):
        if p.path is None:
            continue
        v = [(q.x, q.y) for q in p.path.flattening(0.5)]
        if len(v) >= 2 and all(x0 - 30 <= x <= x1 + 30 and y0 - 30 <= y <= y1 + 30 for x, y in v):
            e = p.entity
            by[(e.dxf.layer, e.dxf.get("color", 256))].append(v)
    return by


def segments(lines):
    out = []
    for c in lines:
        for a, b in zip(c, c[1:]):
            L = math.dist(a, b)
            if L >= 3:
                out.append((a, b, L))
    return out


def wall_strips(sg, wmin, wmax, minov=3):
    rects = []
    strips.clear()
    for i, (a, b, L) in enumerate(sg):
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        for c, e, M in sg[i + 1:]:
            vx, vy = (e[0] - c[0]) / M, (e[1] - c[1]) / M
            if abs(ux * vy - uy * vx) > 0.02:
                continue
            d1 = (c[0] - a[0]) * -uy + (c[1] - a[1]) * ux
            d2 = (e[0] - a[0]) * -uy + (e[1] - a[1]) * ux
            if not (wmin <= abs(d1) <= wmax) or abs(d2 - d1) > 0.5:
                continue
            s = sorted([(c[0] - a[0]) * ux + (c[1] - a[1]) * uy, (e[0] - a[0]) * ux + (e[1] - a[1]) * uy])
            lo, hi = max(0, s[0]), min(L, s[1])
            if hi - lo < minov:
                continue
            P = lambda t, o: (a[0] + ux * t - uy * o, a[1] + uy * t + ux * o)
            rects.append(Polygon([P(lo, 0), P(hi, 0), P(hi, d1), P(lo, d1)]))
            strips.append(_strip_record(P(lo, 0), P(hi, 0), P(hi, d1)))
    u = unary_union([r.buffer(0.05, join_style=2) for r in rects])
    # close the small missing squares at corners and T-junctions (gaps < ~14")
    return u.buffer(7, join_style=2).buffer(-7, join_style=2)


strips: list[tuple] = []


def _strip_record(p0, p1, p2):
    """Normalised description of a wall strip: direction, offset band, length range."""
    ux, uy = p1[0] - p0[0], p1[1] - p0[1]
    L = math.hypot(ux, uy); ux, uy = ux / L, uy / L
    if ux < -1e-9 or (abs(ux) < 1e-9 and uy < 0):
        ux, uy = -ux, -uy
    off = lambda p: -uy * p[0] + ux * p[1]
    along = lambda p: ux * p[0] + uy * p[1]
    o = sorted([off(p0), off(p2)])
    t = sorted([along(p0), along(p1)])
    return (ux, uy, o[0], o[1], t[0], t[1])


def wall_openings(max_gap=160, min_gap=12):
    """Rectangles that bridge two wall strips lying end to end on the same wall line."""
    out = []
    for i, (ux, uy, o1, o2, t1, t2) in enumerate(strips):
        for vx, vy, p1, p2, s1, s2 in strips[i + 1:]:
            if abs(ux * vy - uy * vx) > 0.02 or abs(o1 - p1) > 1.5 or abs(o2 - p2) > 1.5:
                continue
            if s1 >= t2:
                g0, g1 = t2, s1
            elif t1 >= s2:
                g0, g1 = s2, t1
            else:
                continue
            if not (min_gap <= g1 - g0 <= max_gap):
                continue
            lo, hi = max(o1, p1), min(o2, p2)
            P = lambda t, o: (ux * t - uy * o, uy * t + ux * o)
            out.append(Polygon([P(g0, lo), P(g1, lo), P(g1, hi), P(g0, hi)]))
    return out


def parts(g):
    if g.is_empty:
        return []
    return [p for p in getattr(g, "geoms", [g]) if p.geom_type == "Polygon" and p.area > 1]


def rect_dims(p):
    r = p.minimum_rotated_rectangle
    xs = list(r.exterior.coords)
    a, b = math.dist(xs[0], xs[1]), math.dist(xs[1], xs[2])
    return min(a, b), max(a, b), r


def to_poly(p: Polygon, z: float, h: float, ox, oy) -> C.Polygon:
    ring = lambda cs: [Vec3(x - ox, y - oy, z) for x, y in list(cs)[:-1]]
    return C.Polygon(ring(p.exterior.coords), [ring(i.coords) for i in p.interiors], h)


def cylinder(cx, cy, r, z, h, ox, oy, n=24) -> C.Polygon:
    pts = [Vec3(cx - ox + r * math.cos(t), cy - oy + r * math.sin(t), z)
           for t in (i * math.tau / n for i in range(n))]
    return C.Polygon(pts, [], h)


def build(dwg: Path, outdir: Path):
    S = SETTINGS
    by = load_lines(dwg, S["plan_window"])
    walls = wall_strips(segments(by[("0", 256)]), S["wall_min"], S["wall_max"])
    house = unary_union(HOUSE_ZONES)

    # --- openings: gaps between wall ends (up to ~12 ft wide) --------------------
    # A gap holding window-frame lines (layers TALLO, 4, Layer13) is a window,
    # otherwise it is a door opening and only gets a lintel.
    frame_lines = unary_union([LineString(c) for k in [("TALLO", 256), ("4", 256), ("Layer13", 256)]
                               for c in by.get(k, [])])
    # keep only the shortest bridge across each break (no bridging over a wall piece)
    gaps = unary_union([g.buffer(0.05, join_style=2) for g in wall_openings()])
    gaps = gaps.difference(walls.buffer(0.5, join_style=2)).intersection(house)
    windows, doors = [], []
    for g in parts(gaps):
        short, long_, _ = rect_dims(g)
        if short > 16 or long_ < 12:
            continue
        inside = frame_lines.intersection(g.buffer(-0.5, join_style=2))
        (windows if inside.length > long_ * 0.8 else doors).append(g)

    # --- columns: the four 16" round columns of the porch ------------------------
    cols = []
    for c in by.get(("0", 251), []):
        g = LineString(c)
        b = g.bounds
        if 12 <= b[2] - b[0] <= 20 and 12 <= b[3] - b[1] <= 20:
            cols.append(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, (b[2] - b[0]) / 2))
    cols = list({(round(x), round(y)): (x, y, r) for x, y, r in cols}.values())

    # --- plot outline & roof ------------------------------------------------------
    plot = walls.convex_hull
    # roof covers each house zone, trimmed to the plot and to where walls actually are
    roof = unary_union([z.intersection(walls.buffer(0.5).convex_hull) for z in HOUSE_ZONES])
    roof = parts(roof.intersection(plot))
    roof = unary_union([r.intersection(unary_union(parts(walls)).envelope) for r in roof])

    ox, oy = plot.bounds[0], plot.bounds[1]   # move model so the plot corner is at the origin
    H, BH = S["wall_height"], S["boundary_wall_height"]
    L = {name: C.LayerGeom(name, rgb) for name, rgb in [
        ("Walls", (236, 230, 218)), ("Boundary wall", (205, 190, 165)), ("Window sills & lintels", (236, 230, 218)),
        ("Door lintels", (236, 230, 218)), ("Glass", (150, 200, 230)), ("Roof slab", (190, 190, 190)),
        ("Parapet", (225, 220, 210)), ("Porch", (200, 200, 200)), ("Columns", (245, 245, 245)),
        ("Plot", (170, 190, 140)), ("Plan lines", (90, 90, 90))]}

    for w in parts(walls):
        inside, outside = w.intersection(house), w.difference(house)
        for p in parts(inside):
            L["Walls"].polygons.append(to_poly(p, 0, H, ox, oy))
        for p in parts(outside):
            L["Boundary wall"].polygons.append(to_poly(p, 0, BH, ox, oy))
    for w in windows:
        L["Window sills & lintels"].polygons.append(to_poly(w, 0, S["sill_height"], ox, oy))
        L["Window sills & lintels"].polygons.append(to_poly(w, S["lintel_height"], H - S["lintel_height"], ox, oy))
        glass = w.buffer(-max(0.0, rect_dims(w)[0] / 2 - 0.5), join_style=2)
        if not glass.is_empty:
            for g in parts(glass):
                L["Glass"].polygons.append(to_poly(g, S["sill_height"], S["lintel_height"] - S["sill_height"], ox, oy))
    for d in doors:
        L["Door lintels"].polygons.append(to_poly(d, S["lintel_height"], H - S["lintel_height"], ox, oy))
    for p in parts(roof):
        L["Roof slab"].polygons.append(to_poly(p, H, S["slab_thickness"], ox, oy))
        ring = p.difference(p.buffer(-S["parapet_thickness"], join_style=2))
        for q in parts(ring):
            L["Parapet"].polygons.append(to_poly(q, H + S["slab_thickness"], S["parapet_height"], ox, oy))
    L["Porch"].polygons.append(to_poly(PORCH_SLAB, H, S["slab_thickness"], ox, oy))
    for x, y, r in cols:
        L["Columns"].polygons.append(cylinder(x, y, r, 0, H, ox, oy))
    L["Plot"].polygons.append(to_poly(plot, -0.5, 0, ox, oy))
    for k, ls in by.items():
        for c in ls:
            L["Plan lines"].edges.append([Vec3(x - ox, y - oy, 0.1) for x, y in c])

    layers = {k: v for k, v in L.items() if v.polygons or v.edges}
    outdir.mkdir(parents=True, exist_ok=True)
    stem = "Dilbagh_3D"
    C.write_dae(layers, outdir / f"{stem}.dae", C.INCH, "inches")
    C.write_ruby(layers, outdir / f"{stem}.rb", 1.0, stem)
    solid = {k: v for k, v in layers.items() if k not in ("Plan lines", "Plot")}  # ground hides walls in matplotlib
    C.write_preview(solid, outdir / f"{stem}.png", "Dilbagh ground floor — 3D (front)", elev=28, azim=-115)
    C.write_preview({k: v for k, v in solid.items() if k not in ("Roof slab", "Parapet", "Porch")},
                    outdir / f"{stem}_roof_off.png", "Dilbagh ground floor — roof removed", elev=48, azim=-115)
    report = {"source": dwg.name, "units": "inches", "settings": S,
              "counts": {k: len(v.polygons) for k, v in layers.items()},
              "plot_size_ft": [round((plot.bounds[2] - plot.bounds[0]) / 12, 1),
                               round((plot.bounds[3] - plot.bounds[1]) / 12, 1)],
              "note": "Walls detected automatically from parallel lines; check doors/windows and fix in SketchUp."}
    (outdir / f"{stem}.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report["counts"]), "plot ft:", report["plot_size_ft"])


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "input/Dilbagh.dwg"),
          Path(sys.argv[2] if len(sys.argv) > 2 else "output"))
