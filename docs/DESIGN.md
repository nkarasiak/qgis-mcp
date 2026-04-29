# qgis-mcp-north — Design Doc

Status: **draft v0.1**, 2026-04-30
Owner: North
Upstream: forked from `nkarasiak/qgis-mcp` @ v0.2.1

This document is the spec. Implementation follows. If a tool's signature, response, or behavior diverges from this doc, the doc wins until updated. The doc is short on purpose — anything not stated is upstream behavior.

---

## 1. Why this fork exists

Three problems with upstream that the fork solves:

**Surface bloat.** Upstream ships 51 tools that mirror the PyQGIS API one-to-one. That's the wrong abstraction layer for an LLM. We cut to **12 workflow tools + 1 escape hatch (`qgis_eval`)**. Every remaining tool encapsulates an end-to-end action a user actually takes (render a choropleth, drop figures into a deck), not a single API call.

**No headless mode.** Upstream requires QGIS Desktop running with the plugin enabled. That's incompatible with scheduled overnight runs, CI, or any automation. We add a **PyQGIS-subprocess transport** alongside the existing plugin transport. Same tools, two backends, selected by config or CLI flag.

**No transportation primitives.** PFLOW choropleths, GUFM trajectories, and OD flow maps are the visualizations that actually appear in our weekly decks. Upstream gives you `set_layer_style` + `render_map` and tells you to compose. We add **`qgis_render_choropleth`**, **`qgis_render_trajectory`**, **`qgis_render_od_flows`**, **`qgis_batch_render`**, and **`qgis_figures_to_pptx`** as opinionated workflow tools.

Out of scope: replacing upstream as a general-purpose QGIS MCP. We intentionally do less. Both servers are installable side-by-side; reach for upstream when you need the long tail of primitives, reach for ours for the workflows we cover.

---

## 2. Architecture

```
                    ┌───────────────────────────────────────┐
                    │          MCP Server (FastMCP)         │
                    │   src/qgis_mcp_north/server.py        │
                    └───────────────┬───────────────────────┘
                                    │
                       ┌────────────┴────────────┐
                       │                         │
                   transport=plugin        transport=headless
                       │                         │
                       ▼                         ▼
        ┌───────────────────────┐   ┌──────────────────────────────┐
        │ TCP socket → port 9877│   │ subprocess → qgis_process or │
        │ → QGIS plugin (event  │   │   PyQGIS in standalone Python│
        │   loop, QTimer poller)│   │   with QGIS app initialized  │
        │   port-9877!          │   │   in offscreen render mode   │
        └───────────────────────┘   └──────────────────────────────┘
                       │                         │
                       ▼                         ▼
                  PyQGIS API                PyQGIS API (same)
```

**Two transports, one tool surface.** The MCP server exposes the same 13 tools regardless of transport. A thin executor abstraction (`src/qgis_mcp_north/executors/{plugin,headless}.py`) hides the difference. Tools are written against the executor interface; they never directly speak socket or PyQGIS.

**Transport selection.** CLI flag `--transport={plugin,headless,auto}`. `auto` (default) checks for a running plugin on port 9877 and falls back to headless. Config file in `~/.config/qgis-mcp-north/config.toml` can override per-machine.

**Headless runtime.** Standalone PyQGIS via `qgis_process` (CLI) for simple ops, with a long-lived Python subprocess holding `QgsApplication` for complex ops. Headless mode forces `QT_QPA_PLATFORM=offscreen` and disables any GUI calls. Render only — no project save, no plugin install, no UI mutation.

**Co-existence with upstream.** To run side-by-side with `nkarasiak/qgis-mcp`:
- Plugin folder: `qgis_mcp_north_plugin/` (vs upstream `qgis_mcp_plugin/`)
- Default socket port: **9877** (upstream uses 9876)
- Python package: `qgis_mcp_north` (vs upstream `qgis_mcp`)
- pyproject `name`: `qgis-mcp-north`
- Server entry: `qgis-mcp-north-server` console script

Upstream's plugin and server stay untouched. If the user installs both, Claude Desktop sees two MCP servers and the LLM picks per request based on tool descriptions.

---

## 3. Naming conventions

- Tool prefix: `qgis_` (consistent with mcp-builder guidance, action-oriented).
- Tool names: `qgis_<action>_<object>` where the action is a verb (`render`, `load`, `style`, `inspect`, `batch`, `export`). Two exceptions: `qgis_eval` (escape hatch, intentionally short) and `qgis_figures_to_pptx` (the underscore-`to` is a deliberate workflow signal).
- Field names in tool responses: `snake_case`, never PyQGIS PascalCase. Translate at the executor boundary.
- Output paths: always absolute. Tools do not write relative paths. If the input is relative, resolve against the user's `cwd` or an explicit `output_dir` parameter.

---

## 4. Tool surface (12 workflow + 1 escape hatch)

For each tool: signature, what it does, response shape, and the typical chain.

### Inspection & Loading (3)

#### `qgis_layer_inspect(path: str) → LayerInfo`

Read-only metadata for a vector or raster file on disk. **Does not load** the layer into any project. Use this before `render_*` so the LLM knows which fields exist.

```python
LayerInfo = {
    "path": str,
    "geometry_type": "point" | "line" | "polygon" | "raster" | "no_geom",
    "crs": str,            # EPSG code or proj string
    "n_features": int,
    "extent": [xmin, ymin, xmax, ymax],
    "fields": [{"name": str, "type": str, "n_unique": int | None}],
}
```

Chains into: `qgis_load_layer`, `qgis_render_choropleth`, `qgis_render_trajectory`.

#### `qgis_load_layer(path: str, name: str | None = None, crs: str | None = None) → LoadedLayer`

Load a layer into the active project (plugin mode) or the headless QGIS app. Returns a layer_id used by styling and render tools. `crs` overrides the file's CRS if specified (e.g., for shapefiles missing `.prj`).

```python
LoadedLayer = LayerInfo | {"layer_id": str}
```

Chains into: `qgis_style_*`, `qgis_render_map`.

#### `qgis_project_load(qgz_path: str) → ProjectInfo`

Load a saved `.qgz`/`.qgs`. All layers and styling come along. Useful for the W17 weekly deck pattern where the QGIS project is the source of truth and we just want to export figures.

```python
ProjectInfo = {
    "project_path": str,
    "crs": str,
    "extent": [xmin, ymin, xmax, ymax],
    "layers": [{"layer_id": str, "name": str, "geometry_type": str, "visible": bool}],
    "layouts": [{"name": str}],   # print composer layouts available for export
}
```

Chains into: `qgis_export_layout`, `qgis_render_map`, `qgis_batch_render`.

### Styling (2)

#### `qgis_style_categorized(layer_id: str, field: str, palette: str = "Set2", classes: list[str] | None = None) → StyleResult`

Categorical symbology — one color per unique value of `field`. `palette` is a colorbrewer name. `classes` optionally restricts to a subset/order.

```python
StyleResult = {
    "layer_id": str,
    "n_classes": int,
    "classes": [{"value": str, "color": str, "n_features": int}],
}
```

#### `qgis_style_graduated(layer_id: str, field: str, n_classes: int = 5, mode: str = "quantile", palette: str = "YlOrRd") → GraduatedStyleResult`

Numeric symbology — value-based color ramp. `mode ∈ {"quantile", "equal_interval", "natural_breaks", "pretty"}`. This is the **primitive under `qgis_render_choropleth`**; expose it directly for cases where the user wants styling without immediately rendering.

```python
GraduatedStyleResult = StyleResult | {"breaks": [float, ...], "mode": str}
```

### Rendering (4)

#### `qgis_render_map(layer_ids: list[str], output_png: str, width: int = 1600, height: int = 1200, dpi: int = 150, extent: list[float] | None = None, background: str = "white") → RenderResult`

Generic render. Renders the listed layers (in order, bottom→top) to a PNG. If `extent` is omitted, uses the union of layer extents with 5% padding.

```python
RenderResult = {
    "output_path": str,
    "width": int, "height": int, "dpi": int,
    "extent": [xmin, ymin, xmax, ymax],
    "crs": str,
    "n_layers": int,
}
```

#### `qgis_render_choropleth(zones_path: str, value_field: str, output_png: str, value_csv: str | None = None, join_field: str = "zone_id", n_classes: int = 5, mode: str = "quantile", palette: str = "YlOrRd", title: str | None = None, legend: bool = True, basemap_paths: list[str] | None = None, width: int = 1600, height: int = 1200, dpi: int = 150) → ChoroplethResult`

**Workflow tool.** Renders a zone-level choropleth in one call.

Two data shapes supported:
- `value_field` is already an attribute on `zones_path` → render directly.
- `value_csv` is provided → join `value_csv[value_field]` to `zones_path` on `join_field` (left join), then render.

The CSV-join path is the PFLOW reality: `zone_trips.csv` (columns `zone_id, origin_trips, dest_trips, total_trips`) joined to a polygon layer keyed by `zone_id`. Zone IDs in PFLOW are string-typed (`MFS01`–`MFS111`, `Z01`–`Z65`, `PRF01`–`PRF47`); `join_field` is whatever the polygon layer uses.

`basemap_paths` lets the caller supply Japan basemap layers (e.g., `polbnda_jpn_new.shp`, `coastl_jpn.shp`) drawn under the choropleth. Optional, but the W17 deck uses them.

```python
ChoroplethResult = RenderResult | {
    "field": str,
    "n_classes": int,
    "breaks": [float, ...],
    "mode": str,
    "min_value": float, "max_value": float,
    "n_features": int,
    "join": {"csv": str, "field": str, "n_matched": int, "n_unmatched": int} | None,
}
```

Chains into: `qgis_figures_to_pptx`, `qgis_batch_render`.

#### `qgis_render_trajectory(input_path: str, output_png: str, lon_col: str = "lon", lat_col: str = "lat", time_col: str = "datetime", id_col: str = "trip_id", mode_col: str | None = None, render_mode: str = "lines", sample_rate: float = 1.0, max_points: int = 500_000, basemap_paths: list[str] | None = None, extent: list[float] | None = None, width: int = 1600, height: int = 1200, dpi: int = 150) → TrajectoryResult`

**Workflow tool.** Render trajectory data from CSV (with `lon, lat, datetime, trip_id` columns by default — matches PFLOW trajectory CSV schema) or GPX. `render_mode ∈ {"lines", "points", "heatmap"}`.

Big-data discipline. PFLOW trajectory CSVs are 3M+ rows each (74 GB total). Defaults:
- `sample_rate=1.0` keeps every point. Set to `0.01` to render every 100th waypoint.
- `max_points=500_000` is a hard ceiling; exceeded → automatic downsampling with a warning in the response.
- `extent=[lon_min, lat_min, lon_max, lat_max]` (EPSG:4326) clips before rendering.

If `movingpandas` and Trajectools plugin are installed, automatically uses them for richer rendering (speed bins, stop detection); otherwise plain line/point rendering.

```python
TrajectoryResult = RenderResult | {
    "n_trajectories": int,
    "n_points_total": int,
    "n_points_rendered": int,
    "downsampled": bool,
    "time_range": [iso_str, iso_str] | None,
    "modes": [str] | None,
    "used_movingpandas": bool,
}
```

#### `qgis_render_od_flows(od_csv: str, zones_layer_path: str, output_png: str, origin_col: str = "origin", dest_col: str = "destination", value_col: str = "trip_count", zone_id_field: str = "zone_id", top_n: int | None = None, basemap_paths: list[str] | None = None, width: int = 1600, height: int = 1200, dpi: int = 150) → ODFlowResult`

**Workflow tool.** Render origin-destination arcs over a zones layer. Defaults match PFLOW's `od_flows.csv` schema (`origin, destination, trip_count, avg_distance_km`). `top_n` limits to the strongest N flows. Arc width scales with `value_col`.

Note: PFLOW uses **multiple zone-id systems in different files** (`MFS##`, `PRF##`, `Z##`). The caller must ensure `od_csv[origin_col/dest_col]` and `zones_layer[zone_id_field]` use the same system. Mismatches surface as `n_unmatched_origins` / `n_unmatched_destinations` in the response, not as a silent zero-flow render.

```python
ODFlowResult = RenderResult | {
    "n_flows": int,
    "n_flows_rendered": int,
    "n_zones": int,
    "max_flow": float,
    "min_flow_rendered": float,
    "n_unmatched_origins": int,
    "n_unmatched_destinations": int,
}
```

### Export & Batch & Delivery (3)

#### `qgis_export_layout(qgz_path: str, layout_name: str, output_path: str, format: str = "png", dpi: int = 300) → ExportResult`

Wraps upstream's print-composer layout export. `format ∈ {"png", "pdf", "svg"}`. Use this when the user has a designed `.qgz` with a print layout and just wants to export it.

```python
ExportResult = {
    "output_path": str,
    "format": str,
    "n_pages": int,
    "layout_name": str,
}
```

#### `qgis_batch_render(template_qgz: str, attribute: str, values: list[str], output_dir: str, layout_name: str | None = None, filename_template: str = "{value}.png") → BatchRenderResult`

**Workflow tool.** Fan-out: open the template project, iterate `values`, filter the active layer by `attribute = value`, render to `output_dir`. Used for "render the OD map for each scenario" or "render the choropleth for each timestep."

```python
BatchRenderResult = {
    "output_dir": str,
    "n_rendered": int,
    "manifest": [{"value": str, "output_path": str, "extent": [...]}],
    "errors": [{"value": str, "error": str}],
}
```

#### `qgis_figures_to_pptx(figure_paths: list[str], pptx_path: str, layout: str = "title_and_image", captions: list[str] | None = None, template_pptx: str | None = None) → PptxResult`

**Delivery tool.** Drop the listed PNGs into a `.pptx`, one per slide. Uses `python-pptx`. `layout ∈ {"title_and_image", "image_only", "two_column", "title_image_caption"}`. If `template_pptx` is given, slides are added to that deck rather than a new one.

```python
PptxResult = {
    "pptx_path": str,
    "n_slides_added": int,
    "n_slides_total": int,
    "slide_titles": [str | None],
}
```

Closes the W17 loop in one call.

### Escape hatch (1)

#### `qgis_eval(code: str, return_vars: list[str] | None = None) → EvalResult`

Execute arbitrary PyQGIS. Available in plugin transport (executes in QGIS); in headless transport, runs in the standalone PyQGIS subprocess. `return_vars` lists local variables to capture and return JSON-serialized.

```python
EvalResult = {
    "stdout": str,
    "stderr": str,
    "return_values": dict[str, Any],   # only populated if return_vars given
    "exception": str | None,
}
```

Use for the long tail. Document explicitly that this is an escape hatch, not a first choice.

---

## 5. Cross-cutting

**Errors.** Every tool raises a typed exception class that maps to an MCP error response with a `next_action` hint. Examples:

- `LayerNotFoundError(path)` → suggest `qgis_layer_inspect(path)` or check path
- `FieldNotFoundError(field, available_fields)` → list available fields in the message
- `CrsMismatchError(layer_crs, project_crs)` → suggest passing `crs=...`
- `HeadlessUnavailableError` → suggest installing PyQGIS or switching transport

The mcp-builder skill emphasizes "actionable error messages." Every error message ends with one suggested next tool call.

**Idempotency annotations.** All `render_*`, `export_*`, `figures_to_pptx`, and `batch_render` are non-idempotent (they write files). Annotate accordingly. `layer_inspect`, `style_*` (in headless mode, where styling is in-memory) are read-only or idempotent.

**Output discipline.** Every tool that writes a file returns the absolute path in `output_path`. No tool returns base64-encoded image bytes. Inline preview is the host's job, not the MCP's.

**Logging.** Server logs to `~/.local/share/qgis-mcp-north/logs/server-{date}.log`. Plugin logs go to QGIS Message Log under tab `qgis-mcp-north`.

---

## 6. Out of scope for v1

- Editing features (add/delete/modify). Upstream covers this.
- Running processing algorithms (`qgis_run_processing`). Upstream covers this.
- Layer tree groups, plugin management, system tools (`transform_coordinates`, etc.). Use upstream or `qgis_eval`.
- Real-time canvas mirroring. Render to PNG only.
- 3D view. Not on the roadmap.
- Map server / WMS / WFS. Not on the roadmap.

If a v1 user needs any of these, the answer is: install upstream alongside, or write the call inside `qgis_eval`.

---

## 7. Milestones

**v0.1 — design doc** (this file). ✅

**v0.2 — scaffolding.** Rename plugin/package to `qgis_mcp_north_plugin` / `qgis_mcp_north`, change socket port to 9877, update pyproject. Strip the 51 upstream tools from `server.py`. Stub the 13 new tool signatures with `NotImplementedError`. Get `qgis-mcp-north-server` console script running, registering 13 stubs with FastMCP.

**v0.3 — plugin transport, MVP tools.** Implement `qgis_layer_inspect`, `qgis_load_layer`, `qgis_render_map`, `qgis_render_choropleth`, `qgis_figures_to_pptx`. End-to-end test against a real PFLOW zone shapefile.

**v0.4 — headless transport.** Implement the PyQGIS-subprocess executor. Verify all v0.3 tools work in both transports.

**v0.5 — remaining workflow tools.** `qgis_render_trajectory` (with optional MovingPandas), `qgis_render_od_flows`, `qgis_batch_render`, `qgis_export_layout`, `qgis_project_load`, `qgis_style_categorized`, `qgis_style_graduated`, `qgis_eval`.

**v0.6 — vault ingest.** `/kb-ingest qgis-mcp-north`. Add `wiki/shared/qgis-mcp-north/` pages, applications notes in `wiki/pflow/` and `wiki/gufm/`. Wire a `/kb-report weekly` rendering pipeline through it.

**v1.0 — first real W17-style deck rendered end-to-end** from a single LLM prompt, using only `qgis-mcp-north` tools.

---

## 8. Resolved & open questions

Resolved (2026-04-30, against `H:\Dropbox\PFLOW\output (Selective Sync Conflict)\`):

1. ~~**PFLOW zone schema**~~ → Multiple coexisting zone systems: `MFS##` (mesh, ~111 zones in `zone_trips.csv`), `PRF##` (47 prefectures, used in `od_flows.csv`), `Z##` (used in trip-level `trips.csv`). All string-typed. No single "134-zone" file — caller must specify which system per render. CRS: **EPSG:4326** (lon/lat), not JGD2011.
2. ~~**PFLOW value-field conventions**~~ → `total_trips`, `origin_trips`, `dest_trips` (zone_trips.csv); `trip_count`, `avg_distance_km` (od_flows.csv). Tool defaults updated.
3. **~~GUFM trajectory schema~~ deferred** → GUFM data unavailable for v1. PFLOW trajectory schema is the v1 default: `lon, lat, datetime, trip_id, transport_mode, purpose, passenger_in, fare_yen, link_id`. Tool defaults updated.
4. ~~**OD CSV shape**~~ → Long format confirmed: `origin, destination, trip_count, avg_distance_km` (12,041 rows in `od_flows.csv`). Tool defaults updated.

Still open:

5. **No master MFS-zone polygon shapefile exists in PFLOW.** Confirmed against all four user-pointed locations (`/shared/gm-jp/`, `/truck/2024JPN_v25.04_100m/`, `/data/network/`, `/output/`). What exists:
    - `polbnda_jpn_new.shp` → administrative (prefectural) polygons, not MFS-coded.
    - `2024JPN_v25.04_100m.tif` → JAXA LULC raster, not zone codes.
    - `mfs/{kanto,osaka}/zone_mapping.py` → Python source defining MFS zone composition.

    **Decision (v0.3): use prefecture polygons directly** from `polbnda_jpn_new.shp` joined to a derived `prefecture → total_trips` aggregate. Demonstrates the choropleth tool end-to-end on a 47-zone case while postponing MFS construction.

    **Decision (v0.4): build MFS polygons** as a one-time prep script (`scripts/build_mfs_zones.py`) that reads `zone_mapping.py` and either dissolves `polbnda_jpn_new.shp` or constructs a mesh grid, writing `assets/zones_mfs.gpkg`. This becomes the canonical `zones_path` for `qgis_render_choropleth` PFLOW tests from v0.4 onward.

6. **PPTX template.** Is there a Chulalongkorn / Sekimoto-lab / W17 deck template `figures_to_pptx` should default to? If so, drop the path in `assets/` and we'll wire it as the default `template_pptx`.

7. **DuckDB integration.** `viz/pflow.duckdb` (8.8 GB) holds pre-built spatial data. Worth a v2 tool `qgis_render_from_duckdb(query, ...)` that runs a query and renders the result, bypassing CSV intermediates. **Not in v1 scope** but worth flagging.

8. **DRM-link aggregation (v2 candidate).** PFLOW trajectory CSVs include `link_id` referencing the DRM (Digital Road Map) network at `H:\Dropbox\PFLOW\data\network\drm_NN.tsv` (prefecture-sharded, ~14 GB total, EPSG:4326, WKT LINESTRING in column 14). Aggregating trajectories to link-level density and rendering as a graduated link layer would be a major visualization upgrade over raw GPS scatter — but the DRM ingest is itself a multi-step pipeline. Park as `qgis_render_link_density(traj_csvs[], drm_paths[], ...)` for v2.

9. **JAXA LULC raster as basemap.** `2024JPN_v25.04_100m.tif` (uint8, 15 categorical classes, EPSG:4326) loads through `qgis_load_layer` → `qgis_render_map` already, no new tool needed. Worth documenting as an optional `basemap_paths` entry for choropleth/trajectory renders that want land-cover context. Default styling: per-class palette matching JAXA's published legend (assets/jaxa_lulc_legend.png available).

---

## 9. References

- Upstream: `nkarasiak/qgis-mcp` v0.2.1
- mcp-builder skill: `~/skills/mcp-builder/SKILL.md`
- MCP best practices: <https://modelcontextprotocol.io/specification/draft.md>
- Vault context: `H:\Dropbox\obsidian-vault\CLAUDE.md` (PFLOW, GUFM, Karpathy guidelines)

---

## 10. Test data inventory (PFLOW)

Concrete files used for design and v0.3 integration tests. All under
`H:\Dropbox\PFLOW\output (Selective Sync Conflict)\` unless noted.

**Trajectory test file** (CSV, EPSG:4326, ~3M rows, ~1 GB)
`trajectory/taxi/osaka/trajectory_0000.csv`
Schema: `taxi_id, trip_id, unix_time_ms, datetime, lon, lat, transport_mode, purpose, passenger_in, fare_yen, is_night_trip, link_id, is_fallback`

**Trip-level CSV** (long-format, ~517k rows)
`trips/taxi/osaka/run_20260422_215846/trips.csv`
Schema: `trip_id, taxi_id, taxi_type, pickup_lon, pickup_lat, dropoff_lon, dropoff_lat, origin_zone, ..., destination_zone, ..., distance_km, fare_yen, ...`
Zone IDs: `Z##` strings.

**Zone-level aggregate** (110 rows, choropleth source)
`trips/truck/run_20260422_215727/zone_trips.csv`
Schema: `zone_id, origin_trips, dest_trips, total_trips`
Zone IDs: `MFS##` strings.

**OD flow matrix** (12,041 rows, long format)
`trips/truck/run_20260422_215727/od_flows.csv`
Schema: `origin, destination, trip_count, avg_distance_km`
Zone IDs: mix of `PRF##` (prefecture) and `MFS##` (mesh).

**Pre-rendered point GPKGs** (50,000-feature samples)
`H:\Dropbox\PFLOW\docs\maps\taxi_{city}_{type}_{pickups,dropoffs}.gpkg`
`H:\Dropbox\PFLOW\docs\maps\truck_{operation}_{origins,destinations}.gpkg`
Useful as "load_layer + render_map" smoke-test fixtures.

**Japan basemap shapefiles** — Global Map Japan v2 bundle (EPSG:4326)
`H:\Dropbox\PFLOW\Pseudo-PFLOW\src\shared\gm-jp\`
Contents: `polbnda_jpn_new.shp` (admin boundaries — primary `zones_path` for v0.3 prefecture-level choropleth), plus `coastl_jpn.shp`, `mainland_jpn.shp`, `roadl_jpn.shp`, `raill_jpn.shp`, `riverl_jpn.shp`, `inwatera_jpn.shp`, `builtupa_jpn.shp`, `airp_jpn.shp`, `portp_jpn.shp`, `rstatp_jpn.shp` (basemap layers — drop into `basemap_paths` lists).

**JAXA Land-Use / Land-Cover raster** (100m, 15 classes, EPSG:4326, 16 MB)
`H:\Dropbox\PFLOW\Pseudo-PFLOW\src\truck\2024JPN_v25.04_100m\2024JPN_v25.04_100m.tif`
Categories: water bodies, built-up, paddy, cropland, grassland, DBF, DNF, EBF, ENF, bare, bamboo, solar panel, wetland, greenhouse, rock-reef-tidal-flat. Optional basemap underlay; loads through `qgis_load_layer` → `qgis_render_map` directly, no special tool.

**DRM road network** (TSV, prefecture-sharded, ~14 GB total, EPSG:4326)
`H:\Dropbox\PFLOW\data\network\drm_NN.tsv` (NN = 01..47 by prefecture)
Schema (tab-separated, no header): `link_id, from_node, to_node, road_class_code, c5, c6, c7, c8, from_lon, from_lat, to_lon, to_lat, wkt_linestring`. The `link_id` joins to `trajectory_*.csv[link_id]` — this is the v2 link-density aggregation source. Not in v1.

**Spatial database** (8.8 GB, v2 tool target)
`viz/pflow.duckdb`
