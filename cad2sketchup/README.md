# CAD → SketchUp converter (GitHub Action)

Drop an AutoCAD **DWG or DXF** drawing into `input/`, push, and GitHub Actions turns it into
a SketchUp model. 2D floor plans become 3D using simple per-layer rules
(e.g. *everything on `A-WALL` → extrude 3 m*).

```
input/plan.dwg  ──push──▶  Action  ──▶  output/plan.dae   import into SketchUp Pro
                                        output/plan.rb    rebuild as native SketchUp geometry
                                        output/plan.png   preview (shows in the commit)
                                        output/plan.json  report: units, layers, warnings
```

## Quick start

1. Create a new GitHub repo and upload the contents of this folder (keep the `.github` folder).
2. Settings → Actions → General → *Workflow permissions* → **Read and write**.
3. Put a `.dwg` or `.dxf` in `input/` and commit. The **CAD → SketchUp** workflow runs.
4. Open `output/` (or download the *sketchup-models* artifact from the run).

You can also run it manually: **Actions → CAD → SketchUp → Run workflow**.

## Getting the model into SketchUp Pro

You get two options per drawing — use whichever suits you:

| File | How | Result |
|---|---|---|
| `plan.dae` | **File → Import**, choose *COLLADA (\*.dae)* | One group per CAD layer, coloured materials, correct scale. Fastest. |
| `plan.rb` | **Extensions → Developer → Ruby Console**, then `load 'C:/path/to/plan.rb'` | Native SketchUp geometry: real faces that were push/pulled, holes kept, **one Tag per CAD layer**, single undo step. Best for editing. |

> SketchUp Pro can also import DWG directly, but it only brings in flat 2D lines. This tool
> adds the 3D step (walls, columns, slabs) and handles DXF files from any CAD program.

## Layer rules — `config/layers.yml`

```yaml
layers:
  "*WALL*":  { closed: extrude, height: 3000, color: "#E8E2D6" }
  "*COL*":   { closed: extrude, height: 3000 }
  "*FLOR*":  { closed: face }
  "*FURN*":  { closed: extrude, height: 750 }
```

- `closed`: `face` | `extrude` | `edges` | `skip` — closed polylines, circles, hatches
- `open`: `edges` | `extrude` (vertical strip) | `skip` — lines, arcs, open polylines
- `height`, `elevation` in drawing units; negative height extrudes downward
- First matching pattern wins; `skip_layers` lists layers never imported (dims, text…)
- An outline drawn inside another on the same layer becomes a **hole** — so a wall's outer
  and inner line becomes a hollow wall ring, not a solid block.
- Entities that already carry a CAD *thickness* are extruded by it automatically.

Units come from the drawing's `$INSUNITS`; set `units: mm` (or `cm`, `m`, `in`, `ft`) if the
drawing is unitless or wrong.

## What converts

| CAD entity | Becomes |
|---|---|
| LWPOLYLINE / POLYLINE, CIRCLE, ELLIPSE, closed SPLINE, HATCH | face or push/pulled solid (per layer rule) |
| LINE, ARC, open polyline/spline | edges, or vertical strip if extruded / has thickness |
| 3DFACE, SOLID, polyface & MESH | faces |
| Block references (INSERT), nested | exploded in place; layer-0 contents inherit the insert's layer |
| TEXT, MTEXT, DIMENSION, LEADER | skipped |
| 3DSOLID / REGION / BODY (ACIS) | **not supported** — counted in the report |

Frozen/off layers are skipped by default.

## DWG support

The Action builds [LibreDWG](https://github.com/LibreDWG/libredwg) once (≈5 min, then cached) and
uses `dwg2dxf`. It handles most drawings up to AutoCAD 2018 format. If a DWG fails, either save
it as DXF from your CAD program, or set `ODA_CONVERTER` to the path of the free ODA File Converter
on a self-hosted runner.

## Run locally

```bash
pip install -r converter/requirements.txt
python converter/cad2skp.py input/ --outdir output       # DXF works out of the box
python tests/make_samples.py samples                      # generate test drawings
```

## Layout

```
.github/workflows/convert.yml   the Action
converter/cad2skp.py            converter (DXF/DWG → DAE + SketchUp Ruby + preview)
config/layers.yml               2D → 3D layer rules
tests/make_samples.py           sample drawing generator
tests/sketchup_stub.rb          fake SketchUp API used to smoke-test .rb output in CI
input/                          put drawings here
output/                         results are committed here
```

## Dilbagh 3D model

`tools/dilbagh_3d.py` turns the ground-floor plan in `input/Dilbagh.dwg` into a 3D house
(`output/Dilbagh_3D.*`). Walls are on layer "0" with everything else in that drawing, so they
are detected geometrically: two parallel lines 3"–15" apart form a wall. Breaks along a wall
become openings — a window (sill + lintel + glass) if window-frame lines are inside it, otherwise
a door (lintel only). Heights are set in `SETTINGS` at the top of the script:

| Setting | Default |
|---|---|
| wall_height | 120" (10'-0") |
| boundary_wall_height | 60" (5'-0") |
| sill_height / lintel_height | 36" / 84" |
| slab_thickness, parapet_height | 5", 36" |

Edit those numbers on GitHub and commit — the Action rebuilds the model.
