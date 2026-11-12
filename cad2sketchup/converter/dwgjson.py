"""
Fallback DWG reader: rebuild an ezdxf document from LibreDWG's JSON dump.

LibreDWG decodes AutoCAD 2007-2009 (AC1021) files well, but its DXF writer often
emits an empty ENTITIES section for them. `dwgread -O json` still contains every
entity, so we recreate the geometry from that. Entities are identified by their
fixed DWG type number because LibreDWG leaves the names blank for these files.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import ezdxf

# DWG fixed object types (subset we turn into geometry)
T_TEXT, T_INSERT, T_MINSERT = 1, 7, 8
T_VERTEX_2D, T_VERTEX_3D, T_VERTEX_PFACE, T_VERTEX_PFACE_FACE = 10, 11, 13, 14
T_POLYLINE_2D, T_POLYLINE_3D, T_POLYLINE_PFACE = 15, 16, 29
T_ARC, T_CIRCLE, T_LINE = 17, 18, 19
T_POINT, T_3DFACE, T_SOLID, T_TRACE = 27, 28, 31, 32
T_ELLIPSE, T_SPLINE = 35, 36
T_BLOCK_HEADER, T_LAYER = 49, 51
T_LWPOLYLINE, T_HATCH = 77, 78


def _h(ref) -> int | None:
    """LibreDWG handle ref [code, size, value, absolute] -> absolute handle."""
    if isinstance(ref, list) and ref:
        return ref[-1] if len(ref) >= 4 else ref[-1]
    return None


def _hid(obj) -> int | None:
    h = obj.get("handle")
    return h[-1] if isinstance(h, list) and h else None


def _xyz(p, z=0.0):
    if p is None:
        return (0.0, 0.0, z)
    return (float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else z)


def dump_json(dwg: Path, workdir: Path) -> dict:
    exe = shutil.which("dwgread")
    if not exe:
        raise RuntimeError("dwgread (LibreDWG) not found")
    out = workdir / (dwg.stem + ".json")
    subprocess.run([exe, "-O", "json", "-o", str(out), str(dwg)], capture_output=True)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("dwgread produced no JSON")
    # LibreDWG writes raw codepage bytes in strings; latin-1 never fails to decode
    return json.loads(out.read_text(encoding="latin-1"))


def build_doc(data: dict, warnings: list[str]):
    objs = data.get("OBJECTS", [])
    by_handle = {}
    for o in objs:
        hid = _hid(o)
        if hid is not None:
            by_handle[hid] = o

    doc = ezdxf.new("R2018")
    hdr = data.get("HEADER", {})
    if isinstance(hdr.get("INSUNITS"), int):
        doc.header["$INSUNITS"] = hdr["INSUNITS"]

    # Layers
    layer_name = {}
    for o in objs:
        if o.get("type") == T_LAYER and "object" in o:
            name = (o.get("name") or "").strip() or f"LAYER_{_hid(o)}"
            layer_name[_hid(o)] = name
            if name not in doc.layers:
                col = (o.get("color") or {}).get("index", 7)
                lay = doc.layers.add(name)
                try:
                    lay.color = abs(int(col)) if 0 < abs(int(col)) < 256 else 7
                except Exception:
                    pass
                flag0 = int(o.get("flag0", 0) or 0)
                if o.get("frozen") or flag0 & 1:
                    lay.freeze()
                if o.get("off") or (isinstance(col, int) and col < 0):
                    lay.off()

    # Block headers
    blocks = {}       # handle -> block record json
    for o in objs:
        if o.get("type") == T_BLOCK_HEADER and "object" in o:
            blocks[_hid(o)] = o
    model_hid = next((h for h, b in blocks.items()
                      if (b.get("name") or "").lower() == "*model_space"), None)

    # Owned entities per block (BLOCK_HEADER.entities, else entity.ownerhandle)
    owned: dict[int, list[dict]] = {h: [] for h in blocks}
    listed = set()
    for h, b in blocks.items():
        for ref in b.get("entities") or []:
            e = by_handle.get(_h(ref))
            if e is not None and "entity" in e:
                owned[h].append(e); listed.add(_hid(e))
    for o in objs:
        if "entity" in o and _hid(o) not in listed:
            own = _h(o.get("ownerhandle"))
            if own in owned:
                owned[own].append(o)

    # Create ezdxf block definitions for every non-layout block
    block_name = {}
    for h, b in blocks.items():
        name = (b.get("name") or "").strip()
        if not name or name.lower().startswith(("*model_space", "*paper_space")):
            continue
        safe = name if not name.startswith("*") else f"ANON{h}"
        block_name[h] = safe
        if safe not in doc.blocks:
            bp = _xyz(b.get("base_pt"))
            doc.blocks.new(safe, base_point=bp)

    stats = {"ok": 0, "unsupported": {}}

    def attribs(e):
        return {"layer": layer_name.get(_h(e.get("layer")), "0"),
                "color": int((e.get("color") or {}).get("index", 256) or 256)
                if 0 <= int((e.get("color") or {}).get("index", 256) or 256) <= 256 else 256}

    def add(layout, e):
        t = e.get("type")
        a = attribs(e)
        ext = _xyz(e.get("extrusion"), 1.0) if e.get("extrusion") else (0, 0, 1)
        if ext != (0, 0, 1):
            a["extrusion"] = ext
        th = float(e.get("thickness", 0) or 0)
        if th and t in (T_LINE, T_CIRCLE, T_ARC, T_LWPOLYLINE, T_SOLID, T_TRACE):
            a["thickness"] = th
        try:
            if t == T_LINE:
                layout.add_line(_xyz(e["start"]), _xyz(e["end"]), dxfattribs=a)
            elif t == T_CIRCLE:
                layout.add_circle(_xyz(e["center"]), e["radius"], dxfattribs=a)
            elif t == T_ARC:
                layout.add_arc(_xyz(e["center"]), e["radius"], math.degrees(e["start_angle"]),
                               math.degrees(e["end_angle"]), dxfattribs=a)
            elif t == T_LWPOLYLINE:
                pts = e.get("points") or []
                bul = e.get("bulges") or []
                rows = [(p[0], p[1], 0, 0, bul[i] if i < len(bul) else 0) for i, p in enumerate(pts)]
                if len(rows) < 2:
                    return
                flag = int(e.get("flag", 0) or 0)
                closed = bool(flag & 512) or bool(flag & 1 and not flag & 4)
                if "elevation" in e:
                    a["elevation"] = float(e["elevation"] or 0)
                if e.get("const_width"):
                    a["const_width"] = float(e["const_width"])
                layout.add_lwpolyline(rows, format="xyseb", close=closed, dxfattribs=a)
            elif t == T_ELLIPSE:
                layout.add_ellipse(_xyz(e["center"]), _xyz(e["sm_axis"]), e.get("axis_ratio", 1.0),
                                   e.get("start_angle", 0.0), e.get("end_angle", math.tau), dxfattribs=a)
            elif t == T_SPLINE:
                ctrl = [_xyz(p) for p in (e.get("ctrl_pts") or [])]
                fit = [_xyz(p) for p in (e.get("fit_pts") or [])]
                if fit and not ctrl:
                    layout.add_spline(fit, dxfattribs=a)
                elif ctrl:
                    ctrl = [_xyz(p[:3] if isinstance(p, list) else
                                 (p.get("x"), p.get("y"), p.get("z", 0))) for p in (e.get("ctrl_pts") or [])]
                    layout.add_open_spline(ctrl, degree=int(e.get("degree", 3)),
                                           knots=e.get("knots") or None, dxfattribs=a)
            elif t in (T_3DFACE,):
                layout.add_3dface([_xyz(e.get(k)) for k in ("corner1", "corner2", "corner3", "corner4")],
                                  dxfattribs=a)
            elif t in (T_SOLID, T_TRACE):
                pts = [_xyz(e.get(k), e.get("elevation", 0)) for k in ("corner1", "corner2", "corner3", "corner4")]
                layout.add_solid(pts, dxfattribs=a)
            elif t in (T_POLYLINE_2D, T_POLYLINE_3D):
                verts = []
                for ref in e.get("vertex") or []:
                    v = by_handle.get(_h(ref))
                    if v and "point" in v:
                        verts.append(_xyz(v["point"], e.get("elevation", 0) or 0))
                if len(verts) >= 2:
                    closed = bool(int(e.get("flag", 0) or 0) & 1)
                    if t == T_POLYLINE_3D:
                        layout.add_polyline3d(verts, close=closed, dxfattribs=a)
                    else:
                        layout.add_polyline2d([v[:2] for v in verts], close=closed, dxfattribs=a)
            elif t == T_POLYLINE_PFACE:
                vs, fs = [], []
                for ref in e.get("vertex") or []:
                    v = by_handle.get(_h(ref))
                    if not v:
                        continue
                    if v.get("type") == T_VERTEX_PFACE:
                        vs.append(_xyz(v.get("point")))
                    elif v.get("type") == T_VERTEX_PFACE_FACE:
                        idx = [abs(int(i)) - 1 for i in (v.get("vertind") or []) if i]
                        if len(idx) >= 3:
                            fs.append(idx)
                if vs and fs:
                    pf = layout.add_polyface(dxfattribs=a)
                    for f in fs:
                        if all(i < len(vs) for i in f):
                            pf.append_face([vs[i] for i in f])
            elif t == T_HATCH:
                h = layout.add_hatch(color=a.get("color", 256), dxfattribs={"layer": a["layer"]})
                el = float(e.get("elevation", 0) or 0)
                if el:
                    h.dxf.elevation = (0, 0, el)
                if ext != (0, 0, 1):
                    h.dxf.extrusion = ext
                for p in e.get("paths") or []:
                    if p.get("polyline_paths"):
                        pts = [(q["point"][0], q["point"][1], q.get("bulge", 0)) for q in p["polyline_paths"]]
                        h.paths.add_polyline_path(pts, is_closed=True)
                    elif p.get("segs"):
                        ep = h.paths.add_edge_path()
                        for s in p["segs"]:
                            ct = s.get("curve_type")
                            if ct == 1:
                                ep.add_line(s["first_endpoint"], s["second_endpoint"])
                            elif ct == 2:
                                sa, ea = math.degrees(s["start_angle"]), math.degrees(s["end_angle"])
                                ep.add_arc(s["center"], s["radius"], sa, ea, bool(s.get("is_ccw", 1)))
                            elif ct == 3:
                                ep.add_ellipse(s["center"], s["endpoint"], s.get("minor_major_ratio", 1),
                                               math.degrees(s["start_angle"]), math.degrees(s["end_angle"]),
                                               bool(s.get("is_ccw", 1)))
                if not h.paths:
                    layout.delete_entity(h); return
            elif t in (T_INSERT, T_MINSERT):
                bn = block_name.get(_h(e.get("block_header")))
                if not bn:
                    return
                sc = e.get("scale") or [1, 1, 1]
                a.update(xscale=sc[0] or 1, yscale=sc[1] or 1, zscale=(sc[2] if len(sc) > 2 else 1) or 1,
                         rotation=math.degrees(e.get("rotation", 0) or 0))
                layout.add_blockref(bn, _xyz(e.get("ins_pt")), dxfattribs=a)
            elif t in (T_TEXT, T_POINT, 4, 5, 44, 45) or t in (20, 21, 22, 23, 24, 25, 26):
                return  # text, mtext, points, dimensions: not geometry
            else:
                stats["unsupported"][t] = stats["unsupported"].get(t, 0) + 1
                return
            stats["ok"] += 1
        except Exception as ex:  # one bad entity should not sink the drawing
            stats["unsupported"][f"{t}:err"] = stats["unsupported"].get(f"{t}:err", 0) + 1

    # Fill blocks first (inserts reference them), then model space
    for h, bn in block_name.items():
        blk = doc.blocks.get(bn)
        for e in owned.get(h, []):
            add(blk, e)
    msp = doc.modelspace()
    for e in owned.get(model_hid, []) if model_hid else []:
        add(msp, e)

    if stats["unsupported"]:
        warnings.append(f"DWG fallback reader skipped entity types {stats['unsupported']}")
    warnings.append(f"read DWG via LibreDWG JSON fallback ({stats['ok']} entities)")
    return doc
