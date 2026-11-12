#!/usr/bin/env python3
"""
cad2skp — convert AutoCAD DWG/DXF drawings into SketchUp-ready models.

For every input drawing it writes:
  <name>.dae       COLLADA model -> SketchUp Pro: File > Import (keeps layers as tags/materials)
  <name>.rb        SketchUp Ruby script -> rebuilds the drawing as native SketchUp
                   geometry (faces, push/pulled solids, edges, one tag per CAD layer)
  <name>.png       Isometric preview of the result
  <name>.json      Conversion report (entity counts, warnings, units, extents)

2D plans are turned into 3D using per-layer rules in a YAML config
(e.g. "closed outlines on layer WALLS -> extrude 3000 mm").

DWG input is first converted to DXF with LibreDWG's `dwg2dxf` (or the
ODA File Converter if ODA_CONVERTER is set).
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import ezdxf
import yaml
from ezdxf import disassemble, recover
from ezdxf.math import Vec3, area as poly_area, is_point_in_polygon_2d, Vec2
from ezdxf.math.triangulation import mapbox_earcut_3d

# ----------------------------------------------------------------------------
# Units
# ----------------------------------------------------------------------------
# $INSUNITS code -> (name, metres per unit)
INSUNITS = {
    0: ("unitless", None), 1: ("inches", 0.0254), 2: ("feet", 0.3048),
    3: ("miles", 1609.344), 4: ("millimeters", 0.001), 5: ("centimeters", 0.01),
    6: ("meters", 1.0), 7: ("kilometers", 1000.0), 8: ("microinches", 2.54e-8),
    9: ("mils", 2.54e-5), 10: ("yards", 0.9144), 14: ("decimeters", 0.1),
}
UNIT_ALIASES = {"mm": 4, "cm": 5, "m": 6, "in": 1, "inch": 1, "inches": 1,
                "ft": 2, "feet": 2, "foot": 2, "millimeters": 4, "meters": 6,
                "centimeters": 5}
INCH = 0.0254

DEFAULT_CONFIG = {
    "units": "auto",             # auto | mm | cm | m | in | ft
    "fallback_units": "mm",      # used when the drawing says "unitless"
    "curve_segments_per_circle": 48,
    "skip_layers": ["Defpoints", "*DIM*", "*TEXT*", "*ANNO*", "*HATCH-PATTERN*"],
    "skip_frozen_or_off_layers": True,
    "default": {"closed": "face", "open": "edges", "height": 0, "elevation": 0},
    "layers": {},
}


@dataclass
class LayerRule:
    closed: str = "face"     # face | extrude | edges | skip
    open: str = "edges"      # edges | extrude | skip   (extrude open = vertical wall strip)
    height: float = 0.0      # extrusion height in drawing units
    elevation: float = 0.0   # added to Z, drawing units
    color: str | None = None  # "#RRGGBB" override
    tag: str | None = None    # SketchUp tag name override


def load_config(path: Path | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and path.exists():
        user = yaml.safe_load(path.read_text()) or {}
        for k, v in user.items():
            if k == "default" and isinstance(v, dict):
                cfg["default"].update(v)
            else:
                cfg[k] = v
    return cfg


def rule_for(layer: str, cfg: dict) -> LayerRule | None:
    for pat in cfg.get("skip_layers", []):
        if fnmatch.fnmatch(layer.upper(), str(pat).upper()):
            return None
    base = dict(cfg["default"])
    for pat, r in (cfg.get("layers") or {}).items():
        if fnmatch.fnmatch(layer.upper(), str(pat).upper()):
            base.update(r or {})
            break  # first matching pattern wins
    known = LayerRule.__dataclass_fields__
    rule = LayerRule(**{k: v for k, v in base.items() if k in known})
    if rule.closed == "skip" and rule.open == "skip":
        return None
    return rule


# ----------------------------------------------------------------------------
# ACI colour table (subset of the 256 AutoCAD colours, rest approximated)
# ----------------------------------------------------------------------------
ACI = {1: (255, 0, 0), 2: (255, 255, 0), 3: (0, 255, 0), 4: (0, 255, 255),
       5: (0, 0, 255), 6: (255, 0, 255), 7: (200, 200, 200), 8: (128, 128, 128),
       9: (192, 192, 192), 30: (255, 127, 0), 40: (255, 191, 0), 140: (0, 127, 255),
       250: (51, 51, 51), 251: (91, 91, 91), 252: (132, 132, 132), 253: (173, 173, 173),
       254: (214, 214, 214), 255: (255, 255, 255)}


def aci_to_rgb(i: int) -> tuple[int, int, int]:
    if i in ACI:
        return ACI[i]
    try:
        from ezdxf.colors import aci2rgb
        c = aci2rgb(i)
        return (c.r, c.g, c.b)
    except Exception:
        return (180, 180, 180)


def hex_to_rgb(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore


# ----------------------------------------------------------------------------
# Geometry model (unit-less, in drawing units)
# ----------------------------------------------------------------------------
@dataclass
class Polygon:
    outer: list[Vec3]
    holes: list[list[Vec3]] = field(default_factory=list)
    height: float = 0.0          # >0 -> extrude along normal / +Z


@dataclass
class LayerGeom:
    name: str
    rgb: tuple[int, int, int]
    polygons: list[Polygon] = field(default_factory=list)
    walls: list[tuple[list[Vec3], float]] = field(default_factory=list)  # open path + height
    edges: list[list[Vec3]] = field(default_factory=list)
    meshes: list[tuple[list[Vec3], list[list[int]]]] = field(default_factory=list)


EPS = 1e-6


def dedupe(pts: list[Vec3]) -> list[Vec3]:
    out: list[Vec3] = []
    for p in pts:
        if not out or not p.isclose(out[-1], abs_tol=EPS):
            out.append(p)
    return out


def is_closed(pts: list[Vec3]) -> bool:
    return len(pts) >= 4 and pts[0].isclose(pts[-1], abs_tol=1e-4)


def newell_normal(pts: list[Vec3]) -> Vec3:
    n = Vec3()
    for a, b in zip(pts, pts[1:] + pts[:1]):
        n += Vec3((a.y - b.y) * (a.z + b.z), (a.z - b.z) * (a.x + b.x), (a.x - b.x) * (a.y + b.y))
    return n.normalize() if n.magnitude > 1e-12 else Vec3(0, 0, 1)


def is_flat_xy(pts: list[Vec3]) -> bool:
    z = pts[0].z
    return all(abs(p.z - z) < 1e-6 for p in pts)


# ----------------------------------------------------------------------------
# DWG -> DXF
# ----------------------------------------------------------------------------
def dwg_to_dxf(src: Path, workdir: Path) -> Path:
    out = workdir / (src.stem + ".dxf")
    oda = os.environ.get("ODA_CONVERTER")
    if oda and shutil.which(oda):
        indir = workdir / "oda_in"; outdir = workdir / "oda_out"
        indir.mkdir(exist_ok=True); outdir.mkdir(exist_ok=True)
        shutil.copy(src, indir / src.name)
        subprocess.run([oda, str(indir), str(outdir), "ACAD2018", "DXF", "0", "1", "*.DWG"],
                       check=True, capture_output=True)
        res = outdir / (src.stem + ".dxf")
        if res.exists():
            return res
    exe = shutil.which("dwg2dxf")
    if not exe:
        raise RuntimeError("DWG input needs LibreDWG (dwg2dxf) or ODA File Converter. "
                           "Save the drawing as DXF instead, or install one of them.")
    p = subprocess.run([exe, "-y", "--as", "r2000", "-o", str(out), str(src)],
                       capture_output=True, text=True)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"dwg2dxf failed: {p.stderr.strip()[-500:]}")
    return out


def read_dxf(path: Path):
    # The recovery reader is used for everything: the strict reader silently drops
    # entities with duplicate handles, which DWG->DXF converters sometimes produce.
    import logging
    logging.getLogger("ezdxf").setLevel(logging.ERROR)
    doc, auditor = recover.readfile(path)
    msgs = []
    if auditor.has_errors:
        msgs.append(f"repaired {len(auditor.errors)} structural errors in the drawing")
    return doc, msgs


# ----------------------------------------------------------------------------
# Conversion
# ----------------------------------------------------------------------------
class Converter:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.warnings: list[str] = []
        self.counts: dict[str, int] = {}
        self.skipped: dict[str, int] = {}

    def resolve_units(self, doc) -> tuple[str, float]:
        u = str(self.cfg.get("units", "auto")).lower()
        if u != "auto":
            code = UNIT_ALIASES.get(u)
            if code is None:
                raise ValueError(f"unknown units '{u}'")
        else:
            code = doc.header.get("$INSUNITS", 0)
            if INSUNITS.get(code, (None, None))[1] is None:
                fb = str(self.cfg.get("fallback_units", "mm")).lower()
                code = UNIT_ALIASES.get(fb, 4)
                self.warnings.append(f"drawing has no units set; assuming {INSUNITS[code][0]} "
                                     f"(change 'units' in the config if wrong)")
        name, m = INSUNITS[code]
        return name, m

    def convert(self, doc) -> dict[str, LayerGeom]:
        msp = doc.modelspace()
        segs = int(self.cfg.get("curve_segments_per_circle", 48))
        hidden = set()
        if self.cfg.get("skip_frozen_or_off_layers", True):
            all_layers = list(doc.layers)
            off = [l for l in all_layers if l.is_off()]
            honour_off = len(off) < len(all_layers)
            if not honour_off and all_layers:
                self.warnings.append("every layer is marked 'off' (common after DWG conversion); "
                                     "importing them anyway")
            for layer in all_layers:
                if layer.is_frozen() or (honour_off and layer.is_off()):
                    hidden.add(layer.dxf.name.upper())

        layers: dict[str, LayerGeom] = {}
        closed_by_layer: dict[str, list[tuple[list[Vec3], float]]] = {}

        def geom_for(layer: str, entity) -> tuple[LayerGeom, LayerRule] | None:
            rule = rule_for(layer, self.cfg)
            if rule is None or layer.upper() in hidden:
                self.skipped[layer] = self.skipped.get(layer, 0) + 1
                return None
            if layer not in layers:
                if rule.color:
                    rgb = hex_to_rgb(rule.color)
                else:
                    try:
                        rgb = aci_to_rgb(abs(doc.layers.get(layer).dxf.color) or 7)
                    except Exception:
                        rgb = (180, 180, 180)
                layers[layer] = LayerGeom(rule.tag or layer, rgb)
            return layers[layer], rule

        # Explode blocks (INSERT) recursively, applying their transforms. Entities
        # drawn on layer "0" inside a block inherit the INSERT's layer, as in AutoCAD.
        entities = self._walk(msp, None, hidden)
        for prim in disassemble.to_primitives(entities, max_flattening_distance=None):
            e = prim.entity
            if e is None:
                continue
            etype = e.dxftype()
            if etype in ("TEXT", "MTEXT", "ATTRIB", "DIMENSION", "LEADER", "MLEADER",
                         "VIEWPORT", "IMAGE", "WIPEOUT", "POINT", "ATTDEF"):
                self.skipped[etype] = self.skipped.get(etype, 0) + 1
                continue
            if etype in ("3DSOLID", "BODY", "REGION", "SURFACE"):
                self.skipped[etype] = self.skipped.get(etype, 0) + 1
                continue
            layer = e.dxf.get("layer", "0")
            got = geom_for(layer, e)
            if not got:
                continue
            lg, rule = got
            self.counts[etype] = self.counts.get(etype, 0) + 1
            thickness = float(e.dxf.get("thickness", 0) or 0) if e.dxf.hasattr("thickness") else 0.0
            elev = Vec3(0, 0, rule.elevation)

            # --- true 3D mesh entities (3DFACE, MESH, polyface, SOLID, TRACE) ---
            mesh = prim.mesh
            if mesh is not None and etype != "HATCH":
                verts = [Vec3(v) + elev for v in mesh.vertices]
                faces = [list(f) for f in mesh.faces if len(f) >= 3]
                if verts and faces:
                    lg.meshes.append((verts, faces))
                continue

            # --- HATCH / filled areas: every boundary path is a closed loop ---
            if etype == "HATCH" or prim.path is None:
                paths = list(getattr(prim, "paths", []) or [])
                if prim.path is not None:
                    paths = [prim.path]
                for p in paths:
                    pts = dedupe([Vec3(v) + elev for v in p.flattening(self._flat_dist(p, segs))])
                    if len(pts) >= 3:
                        if not pts[0].isclose(pts[-1], abs_tol=1e-4):
                            pts.append(pts[0])
                        self._add_closed(closed_by_layer, lg, layer, pts, rule, thickness)
                continue

            path = prim.path
            pts = dedupe([Vec3(v) + elev for v in path.flattening(self._flat_dist(path, segs))])
            if len(pts) < 2:
                continue
            closed = is_closed(pts) or (path.is_closed if hasattr(path, "is_closed") else False)
            if etype in ("CIRCLE",) or (etype == "ELLIPSE" and abs(
                    e.dxf.get("end_param", math.tau) - e.dxf.get("start_param", 0)) >= math.tau - 1e-6):
                closed = True
            if closed and len(pts) >= 3:
                if not pts[0].isclose(pts[-1], abs_tol=1e-4):
                    pts.append(pts[0])
                self._add_closed(closed_by_layer, lg, layer, pts, rule, thickness)
            else:
                h = thickness or (rule.height if rule.open == "extrude" else 0)
                if h and rule.open != "skip":
                    lg.walls.append((pts, h))
                elif rule.open == "edges":
                    lg.edges.append(pts)

        # Resolve outlines into faces-with-holes per layer
        for layer, items in closed_by_layer.items():
            self._build_polygons(layers[layer], items)
        return layers

    ANNOTATION = {"DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION", "LEADER",
                  "MLEADER", "MULTILEADER", "TOLERANCE", "TEXT", "MTEXT", "ATTRIB", "ATTDEF"}

    def _walk(self, entities, parent_layer, hidden, depth=0):
        for e in entities:
            t = e.dxftype()
            layer = e.dxf.get("layer", "0")
            if parent_layer and layer == "0":
                layer = parent_layer
                try:
                    e.dxf.layer = layer
                except Exception:
                    pass
            if t in self.ANNOTATION:
                self.skipped[t] = self.skipped.get(t, 0) + 1
                continue
            if t == "INSERT":
                if rule_for(layer, self.cfg) is None or layer.upper() in hidden or depth > 20:
                    self.skipped["INSERT"] = self.skipped.get("INSERT", 0) + 1
                    continue
                try:
                    children = list(e.virtual_entities())
                except Exception as ex:
                    self.warnings.append(f"could not explode block '{e.dxf.name}': {ex}")
                    continue
                yield from self._walk(children, layer, hidden, depth + 1)
                # attributes are text -> skipped
                continue
            yield e

    @staticmethod
    def _flat_dist(path, segs: int) -> float:
        # chord-height tolerance that gives ~segs segments per full circle of the path's size
        try:
            bb = path.control_vertices()
            xs = [v.x for v in bb]; ys = [v.y for v in bb]
            r = max(max(xs) - min(xs), max(ys) - min(ys)) / 2 or 1.0
        except Exception:
            r = 1.0
        return max(r * (1 - math.cos(math.pi / segs)), 1e-6)

    def _add_closed(self, store, lg: LayerGeom, layer, pts, rule: LayerRule, thickness: float):
        if rule.closed == "skip":
            return
        if rule.closed == "edges":
            lg.edges.append(pts)
            return
        h = thickness or (rule.height if rule.closed == "extrude" else 0.0)
        store.setdefault(layer, []).append((pts[:-1], h))

    def _build_polygons(self, lg: LayerGeom, items):
        # Nest outlines: a loop fully inside another loop on the same plane with the
        # same height becomes a hole (even/odd nesting, like CAD hatching).
        flat = [(pts, h) for pts, h in items if len(pts) >= 3]
        loops = []
        for pts, h in flat:
            planar = is_flat_xy(pts)
            a = abs(poly_area([Vec2(p) for p in pts])) if planar else 0.0
            loops.append({"pts": pts, "h": h, "planar": planar, "area": a,
                          "z": pts[0].z, "parent": None, "depth": 0})
        order = sorted(range(len(loops)), key=lambda i: -loops[i]["area"])
        for idx_pos, i in enumerate(order):
            li = loops[i]
            if not li["planar"] or li["area"] < 1e-9:
                continue
            probe = Vec2(sum(p.x for p in li["pts"]) / len(li["pts"]),
                         sum(p.y for p in li["pts"]) / len(li["pts"]))
            if not self._inside(probe, li["pts"]):
                probe = Vec2(li["pts"][0])
            for j in reversed(order[:idx_pos]):  # smallest enclosing first
                lj = loops[j]
                if (lj["planar"] and abs(lj["z"] - li["z"]) < 1e-6 and lj["area"] > li["area"]
                        and abs(lj["h"] - li["h"]) < 1e-9
                        and all(self._inside(Vec2(p), lj["pts"]) for p in li["pts"][::max(1, len(li["pts"]) // 8)])):
                    li["parent"] = j
                    li["depth"] = lj["depth"] + 1
                    break
        polys: dict[int, Polygon] = {}
        for i in order:
            li = loops[i]
            if li["depth"] % 2 == 0:
                polys[i] = Polygon(li["pts"], [], li["h"])
            else:
                polys[li["parent"]].holes.append(li["pts"])
        lg.polygons.extend(polys[i] for i in order if i in polys)

    @staticmethod
    def _inside(p: Vec2, poly: list[Vec3]) -> bool:
        return is_point_in_polygon_2d(p, [Vec2(v) for v in poly], abs_tol=1e-9) >= 0


# ----------------------------------------------------------------------------
# Triangulated mesh output (shared by DAE and preview)
# ----------------------------------------------------------------------------
def polygon_solid(poly: Polygon) -> list[list[Vec3]]:
    """Return triangles for a (possibly extruded) polygon with holes."""
    tris: list[list[Vec3]] = []
    try:
        cap = [list(t) for t in mapbox_earcut_3d(poly.outer, poly.holes or None)]
    except Exception:
        cap = []
    if not cap:
        return tris
    n = newell_normal(poly.outer)
    if poly.height == 0:
        return cap
    up = Vec3(0, 0, 1) if abs(n.z) > 0.99 else n
    off = up * poly.height
    if n.dot(up) > 0:
        tris += [[t[0], t[2], t[1]] for t in cap]                     # bottom faces down
    else:
        tris += cap
    top = [[v + off for v in t] for t in cap]
    tris += [t if n.dot(up) > 0 else [t[0], t[2], t[1]] for t in top]
    for ring in [poly.outer] + poly.holes:
        for a, b in zip(ring, ring[1:] + ring[:1]):
            tris.append([a, b, b + off]); tris.append([a, b + off, a + off])
    return tris


def wall_strip(pts: list[Vec3], h: float) -> list[list[Vec3]]:
    off = Vec3(0, 0, h); tris = []
    for a, b in zip(pts, pts[1:]):
        tris.append([a, b, b + off]); tris.append([a, b + off, a + off])
    return tris


def layer_triangles(lg: LayerGeom) -> list[list[Vec3]]:
    tris = []
    for p in lg.polygons:
        tris += polygon_solid(p)
    for pts, h in lg.walls:
        tris += wall_strip(pts, h)
    for verts, faces in lg.meshes:
        for f in faces:
            for k in range(1, len(f) - 1):
                tris.append([verts[f[0]], verts[f[k]], verts[f[k + 1]]])
    return tris


# ----------------------------------------------------------------------------
# Writers
# ----------------------------------------------------------------------------
def safe_id(s: str) -> str:
    out = "".join(c if c.isalnum() or c in "_-" else "_" for c in s)
    return out if out and not out[0].isdigit() else "L_" + out


def write_dae(layers: dict[str, LayerGeom], path: Path, metres_per_unit: float, unit_name: str):
    import collada
    import numpy as np
    from collada import asset, geometry, material, scene, source

    mesh = collada.Collada()
    mesh.assetInfo.unitmeter = metres_per_unit
    mesh.assetInfo.unitname = unit_name
    mesh.assetInfo.upaxis = asset.UP_AXIS.Z_UP
    nodes = []
    for lname, lg in layers.items():
        lid = safe_id(lname)
        r, g, b = (c / 255 for c in lg.rgb)
        eff = material.Effect(f"{lid}-fx", [], "lambert", diffuse=(r, g, b, 1.0), double_sided=True)
        mat = material.Material(f"{lid}-mat", lname, eff)
        mesh.effects.append(eff); mesh.materials.append(mat)
        children = []
        tris = layer_triangles(lg)
        if tris:
            verts = np.array([[v.x, v.y, v.z] for t in tris for v in t], dtype=float)
            vsrc = source.FloatSource(f"{lid}-v", verts.ravel(), ("X", "Y", "Z"))
            geom = geometry.Geometry(mesh, f"{lid}-faces", lname, [vsrc])
            il = source.InputList(); il.addInput(0, "VERTEX", f"#{lid}-v")
            idx = np.arange(len(verts), dtype=int)
            geom.primitives.append(geom.createTriangleSet(idx, il, "m"))
            mesh.geometries.append(geom)
            children.append(scene.GeometryNode(geom, [scene.MaterialNode("m", mat, inputs=[])]))
        polylines = list(lg.edges)
        if polylines:
            pts = []; ind = []
            for pl in polylines:
                base = len(pts); pts += pl
                for k in range(len(pl) - 1):
                    ind += [base + k, base + k + 1]
            arr = np.array([[p.x, p.y, p.z] for p in pts], dtype=float)
            vsrc = source.FloatSource(f"{lid}-ev", arr.ravel(), ("X", "Y", "Z"))
            geom = geometry.Geometry(mesh, f"{lid}-edges", lname + " edges", [vsrc])
            il = source.InputList(); il.addInput(0, "VERTEX", f"#{lid}-ev")
            geom.primitives.append(geom.createLineSet(np.array(ind, dtype=int), il, "m"))
            mesh.geometries.append(geom)
            children.append(scene.GeometryNode(geom, [scene.MaterialNode("m", mat, inputs=[])]))
        if children:
            n = scene.Node(lid, children=children); n.name = lname  # type: ignore[attr-defined]
            nodes.append(n)
    sc = scene.Scene("scene", nodes)
    mesh.scenes.append(sc); mesh.scene = sc
    mesh.write(str(path))


def rb_pts(pts: Iterable[Vec3], k: float) -> str:
    return "[" + ",".join(f"[{p.x * k:.6f},{p.y * k:.6f},{p.z * k:.6f}]" for p in pts) + "]"


def write_ruby(layers: dict[str, LayerGeom], path: Path, k: float, title: str):
    """k converts drawing units -> inches (SketchUp's internal unit)."""
    L = [
        "# Generated by cad2skp — rebuilds the CAD drawing as native SketchUp geometry.",
        "# Run in SketchUp Pro:  Extensions > Developer > Ruby Console, then:",
        f"#   load '/full/path/to/{path.name}'",
        "# (Windows paths: use forward slashes, e.g. 'C:/Users/me/Downloads/plan.rb')",
        "model = Sketchup.active_model",
        f"model.start_operation('Import {title}', true)",
        "begin",
        "  ents = model.active_entities",
        f"  root = ents.add_group; root.name = {json.dumps(title)}",
        "  stats = Hash.new(0)",
        "  mk_face = lambda do |g, outer, holes|",
        "    f = g.entities.add_face(outer) rescue nil",
        "    return nil unless f",
        "    holes.each do |h|",
        "      hf = g.entities.add_face(h) rescue nil",
        "      hf.erase! if hf && hf.valid?",
        "    end",
        "    f.valid? ? f : g.entities.grep(Sketchup::Face).max_by(&:area)",
        "  end",
    ]
    for lname, lg in layers.items():
        r, g, b = lg.rgb
        L += [
            f"  tag = model.layers[{json.dumps(lname)}] || model.layers.add({json.dumps(lname)})",
            f"  mat = model.materials[{json.dumps(lname)}] || model.materials.add({json.dumps(lname)})",
            f"  mat.color = Sketchup::Color.new({r},{g},{b})",
            "  lg = root.entities.add_group; lg.name = " + json.dumps(lname) + "; lg.layer = tag",
        ]
        for poly in lg.polygons:
            holes = "[" + ",".join(rb_pts(h, k) for h in poly.holes) + "]"
            L += [
                "  g = lg.entities.add_group",
                f"  f = mk_face.call(g, {rb_pts(poly.outer, k)}, {holes})",
                "  if f",
            ]
            if poly.height:
                L += [
                    "    f.reverse! if f.normal.z < 0",
                    f"    f.pushpull({poly.height * k:.6f})",
                ]
            L += [
                "    g.entities.grep(Sketchup::Face).each { |x| x.material = mat }",
                "    stats[:faces] += 1",
                "  else",
                "    g.erase!; stats[:failed] += 1",
                "  end",
            ]
        for pts, h in lg.walls:
            for a, b2 in zip(pts, pts[1:]):
                quad = [a, b2, b2 + Vec3(0, 0, h), a + Vec3(0, 0, h)]
                L.append(f"  wf = (lg.entities.add_face({rb_pts(quad, k)}) rescue nil)")
                L.append("  wf ? (wf.material = mat) : (stats[:failed] += 1)")
        for verts, faces in lg.meshes:
            L.append("  pm = Geom::PolygonMesh.new")
            L.append(f"  vs = {rb_pts(verts, k)}.map {{ |p| pm.add_point(Geom::Point3d.new(*p)) }}")
            for f in faces:
                L.append("  pm.add_polygon(" + ",".join(f"vs[{i}]" for i in f) + ") rescue nil")
            L.append("  lg.entities.add_faces_from_mesh(pm, Geom::PolygonMesh::AUTO_SOFTEN, mat)")
        for pl in lg.edges:
            L.append(f"  lg.entities.add_edges({rb_pts(pl, k)}) rescue stats[:failed] += 1")
        L.append("  lg.erase! if lg.entities.length == 0")
    L += [
        "  model.commit_operation",
        "  model.active_view.zoom_extents",
        f"  puts \"cad2skp: imported {title} (#{{stats[:faces]}} solids/faces, #{{stats[:failed]}} skipped)\"",
        "rescue => e",
        "  model.abort_operation",
        "  raise",
        "end",
    ]
    path.write_text("\n".join(L) + "\n")


def write_preview(layers: dict[str, LayerGeom], path: Path, title: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Line3DCollection, Poly3DCollection

    fig = plt.figure(figsize=(9, 7), dpi=110)
    ax = fig.add_subplot(111, projection="3d")
    allp = []
    for lg in layers.values():
        col = tuple(c / 255 for c in lg.rgb)
        tris = layer_triangles(lg)
        if tris:
            pc = Poly3DCollection([[(v.x, v.y, v.z) for v in t] for t in tris],
                                  facecolor=col, edgecolor=(0, 0, 0, 0.08), linewidth=0.2, alpha=0.9)
            ax.add_collection3d(pc)
            allp += [v for t in tris for v in t]
        segs = [[(a.x, a.y, a.z), (b.x, b.y, b.z)] for pl in lg.edges for a, b in zip(pl, pl[1:])]
        if segs:
            ax.add_collection3d(Line3DCollection(segs, colors=[col], linewidths=0.6))
            allp += [p for pl in lg.edges for p in pl]
    if allp:
        xs = [p.x for p in allp]; ys = [p.y for p in allp]; zs = [p.z for p in allp]
        c = [(max(v) + min(v)) / 2 for v in (xs, ys, zs)]
        r = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) / 2 or 1
        ax.set_xlim(c[0] - r, c[0] + r); ax.set_ylim(c[1] - r, c[1] + r); ax.set_zlim(c[2] - r * 0.3, c[2] + r * 1.7)
    ax.view_init(elev=32, azim=-55)
    ax.set_axis_off()
    ax.set_title(title, fontsize=11)
    fig.tight_layout()
    fig.savefig(path, facecolor="white")
    plt.close(fig)


# ----------------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------------
def convert_file(src: Path, outdir: Path, cfg: dict, formats: set[str]) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    conv = Converter(cfg)
    with tempfile.TemporaryDirectory() as tmp:
        dxf = src
        if src.suffix.lower() == ".dwg":
            dxf = dwg_to_dxf(src, Path(tmp))
        doc, msgs = read_dxf(dxf)
        conv.warnings += msgs
        unit_name, m_per_unit = conv.resolve_units(doc)
        layers = conv.convert(doc)

    stem = src.stem
    to_in = m_per_unit / INCH
    written = []
    if not any(lg.polygons or lg.edges or lg.walls or lg.meshes for lg in layers.values()):
        conv.warnings.append("no convertible geometry found (3D ACIS solids and text are not supported)")
    if "dae" in formats:
        write_dae(layers, outdir / f"{stem}.dae", m_per_unit, unit_name); written.append(f"{stem}.dae")
    if "rb" in formats:
        write_ruby(layers, outdir / f"{stem}.rb", to_in, stem); written.append(f"{stem}.rb")
    if "png" in formats:
        write_preview(layers, outdir / f"{stem}.png", src.name); written.append(f"{stem}.png")

    allp = [p for lg in layers.values() for t in layer_triangles(lg) for p in t] + \
           [p for lg in layers.values() for pl in lg.edges for p in pl]
    ext = None
    if allp:
        ext = {a: [round(min(getattr(p, a) for p in allp), 3), round(max(getattr(p, a) for p in allp), 3)]
               for a in "xyz"}
    report = {
        "source": src.name, "units": unit_name, "metres_per_unit": m_per_unit,
        "extents": ext,
        "layers": {n: {"faces_or_solids": len(lg.polygons), "extruded_walls": len(lg.walls),
                       "edges": len(lg.edges), "meshes": len(lg.meshes)} for n, lg in layers.items()},
        "entities_converted": conv.counts, "skipped": conv.skipped,
        "warnings": conv.warnings, "outputs": written,
    }
    if "json" in formats:
        (outdir / f"{stem}.json").write_text(json.dumps(report, indent=2))
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="DWG/DXF files or folders")
    ap.add_argument("-o", "--outdir", default="output")
    ap.add_argument("-c", "--config", default=None, help="layer rules YAML (default: config/layers.yml if present)")
    ap.add_argument("--formats", default="dae,rb,png,json")
    ap.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"),
                    help="append a Markdown summary here (GitHub Actions sets this)")
    a = ap.parse_args(argv)

    cfg_path = Path(a.config) if a.config else Path("config/layers.yml")
    cfg = load_config(cfg_path if cfg_path.exists() else None)
    formats = {f.strip().lower() for f in a.formats.split(",")}

    files: list[Path] = []
    for i in a.inputs:
        p = Path(i)
        if p.is_dir():
            files += sorted(q for q in p.rglob("*") if q.suffix.lower() in (".dxf", ".dwg"))
        elif p.suffix.lower() in (".dxf", ".dwg") and p.exists():
            files.append(p)
    if not files:
        print("No .dwg/.dxf files found.")
        return 0

    rows, failed = [], 0
    for f in files:
        try:
            r = convert_file(f, Path(a.outdir), cfg, formats)
            nlay = len(r["layers"]); nsol = sum(v["faces_or_solids"] for v in r["layers"].values())
            print(f"OK   {f.name}: {nlay} layers, {nsol} faces/solids, units={r['units']}")
            for w in r["warnings"]:
                print(f"     warning: {w}")
            rows.append(f"| ✅ `{f.name}` | {r['units']} | {nlay} | {nsol} | "
                        + ("<br>".join(r["warnings"]) or "—") + " |")
        except Exception as ex:  # keep going with other files
            failed += 1
            print(f"FAIL {f.name}: {ex}", file=sys.stderr)
            rows.append(f"| ❌ `{f.name}` | — | — | — | {ex} |")

    if a.summary:
        with open(a.summary, "a") as fh:
            fh.write("## CAD → SketchUp conversion\n\n| File | Units | Layers | Faces/solids | Notes |\n"
                     "|---|---|---|---|---|\n" + "\n".join(rows) + "\n")
    return 1 if failed == len(files) else 0


if __name__ == "__main__":
    sys.exit(main())
