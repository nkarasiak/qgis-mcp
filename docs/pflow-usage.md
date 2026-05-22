# Using qgis-mcp-workflows for PFLOW figures

Concrete recipes for the canonical PFLOW (and GUFM) figure types. All paths in this
guide reference files that exist only on the user's local machine — the synthetic
fixtures committed in `tests/fixtures/` are for testing the tool surface, not for
publication-ready figures.

## Setup

```powershell
# Plugin mode (interactive — recommended for iteration)
# 1. Start QGIS Desktop, enable the "QGIS MCP North" plugin from Plugins menu.
# 2. Click "Start Server" in the MCP dock widget (listens on :9877).
# 3. From any MCP client (Claude Desktop, Cursor, etc.), call the tools.

# Headless mode (cron / unattended renders)
$env:QGIS_MCP_WORKFLOWS_TRANSPORT='headless'
$env:QGIS_MCP_WORKFLOWS_QGIS_LAUNCHER='M:\QGIS LTR\bin\python-qgis-ltr.bat'
uv run --no-sync qgis-mcp-workflows-server
```

## 1. Prefecture-level choropleth from PFLOW `zone_trips.csv`

The W17-deck pattern: aggregate trips per prefecture, color by total.

```python
qgis_render_choropleth(
    zones_path=r"H:\Dropbox\PFLOW\Pseudo-PFLOW\src\shared\gm-jp\polbnda_jpn_new.shp",
    value_csv=r"H:\Dropbox\PFLOW\output (Selective Sync Conflict)\trips\truck\run_20260422_215727\zone_trips.csv",
    value_field="total_trips",
    join_field="zone_id",  # MFS-coded in zone_trips.csv; prefecture-aggregated in zones
    output_png=r"C:\temp\choropleth_truck.png",
    n_classes=5,
    mode="quantile",
    palette="YlOrRd",
    title="Truck trips by prefecture (run_20260422_215727)",
    basemap_paths=[
        r"H:\Dropbox\PFLOW\Pseudo-PFLOW\src\shared\gm-jp\coastl_jpn.shp",
        r"H:\Dropbox\PFLOW\Pseudo-PFLOW\src\shared\gm-jp\riverl_jpn.shp",
    ],
)
```

Response includes `breaks` (class boundaries), `n_matched` / `n_unmatched` (join
diagnostics), `min_value`, `max_value`. If `n_matched == 0`, the tool raises
`JoinError` with a hint to call `qgis_layer_inspect` on the polygon layer.

## 2. OD flow map from `od_flows.csv`

```python
qgis_render_od_flows(
    od_csv=r"H:\Dropbox\PFLOW\output (Selective Sync Conflict)\trips\truck\run_20260422_215727\od_flows.csv",
    zones_layer_path=r"H:\Dropbox\PFLOW\Pseudo-PFLOW\src\shared\gm-jp\polbnda_jpn_new.shp",
    output_png=r"C:\temp\od_truck.png",
    origin_col="origin",
    dest_col="destination",
    value_col="trip_count",
    zone_id_field="zone_id",  # match the polygon's id field (PRF_CODE for prefecture, etc.)
    top_n=100,  # render only the 100 strongest flows
)
```

If origin or destination IDs don't match the zones layer, response surfaces
`n_unmatched_origins` / `n_unmatched_destinations` — loud, not silent zero-flow
renders. Common cause: `od_flows.csv` uses `PRF##` but zones use `MFS##`.

## 3. Trajectory heatmap from `trajectory_*.csv`

PFLOW trajectory files are 3M+ rows (~1 GB each). Always use `sample_rate` or
`max_points` to keep renders fast.

```python
qgis_render_trajectory(
    input_path=r"H:\Dropbox\PFLOW\output (Selective Sync Conflict)\trajectory\taxi\osaka\trajectory_0000.csv",
    output_png=r"C:\temp\traj_taxi_osaka.png",
    render_mode="heatmap",     # density-style; alternatives: "lines", "points"
    sample_rate=0.01,           # every 100th row (~30k points from 3M)
    max_points=500_000,         # hard cap; deterministic stride-sampled
    extent=[135.4, 34.55, 135.6, 34.75],  # clip to central Osaka
)
```

With `[trajectory]` extra installed (`uv sync --extra trajectory`):

```python
qgis_render_trajectory(
    input_path="...",
    output_png="...",
    render_mode="lines",
    # mode_col=None and movingpandas available → speed-binned graduated symbology
)
# Response.used_movingpandas == True; lines colored by Spectral ramp on speed_kmh.
```

## 4. Three figures → weekly deck

Closes the W17 loop in 4 calls.

```python
choro = qgis_render_choropleth(
    zones_path=POLBNDA, value_csv=ZONE_TRIPS, value_field="total_trips",
    output_png=r"C:\temp\01_choropleth.png",
)
traj = qgis_render_trajectory(
    input_path=TRAJECTORY_CSV, output_png=r"C:\temp\02_traj.png",
    render_mode="heatmap", sample_rate=0.01,
)
od = qgis_render_od_flows(
    od_csv=OD_FLOWS, zones_layer_path=POLBNDA, output_png=r"C:\temp\03_od.png",
    top_n=100,
)

qgis_figures_to_pptx(
    figure_paths=[choro.output_path, traj.output_path, od.output_path],
    pptx_path=r"C:\temp\w17_2026-05-14.pptx",
    layout="title_and_image",
    captions=[
        "Truck trips by prefecture",
        "Taxi trajectory density (Osaka)",
        "Top-100 truck flows",
    ],
    template_pptx=r"H:\Dropbox\PFLOW\templates\w17_template.pptx",  # optional
)
```

## 5. Compound mode (token-constrained LLMs)

When running on a smaller model (e.g., Haiku), set:

```powershell
$env:QGIS_MCP_WORKFLOWS_TOOL_MODE='compound'
uv run --no-sync qgis-mcp-workflows-server
```

The MCP exposes 5 grouped tools instead of 13. The same workflow becomes:

```python
qgis_render(mode="choropleth", zones_path=POLBNDA, value_csv=ZONE_TRIPS,
             value_field="total_trips", output_png="...")
qgis_render(mode="trajectory", input_path=TRAJECTORY_CSV, output_png="...",
             render_mode="heatmap", sample_rate=0.01)
qgis_render(mode="od_flows", od_csv=OD_FLOWS, zones_path=POLBNDA, output_png="...",
             top_n=100)
qgis_export(kind="pptx", figure_paths=[...], output_path="...",
             layout="title_and_image", captions=[...])
```

Behavior is identical; only the tool surface changes.

## 6. `qgis_eval` escape hatch

When no workflow tool fits — e.g., a custom symbology, a processing algorithm,
or interactive debugging:

```python
result = qgis_eval(
    code="""
from qgis.core import QgsExpression, QgsFeatureRequest
project = QgsProject.instance()
layer = project.mapLayersByName('polbnda_jpn_new')[0]
expr = QgsExpression('"PRF_CODE" = \\'27\\'')  # Osaka prefecture
request = QgsFeatureRequest(expr)
osaka_features = list(layer.getFeatures(request))
n_osaka = len(osaka_features)
osaka_area = sum(f.geometry().area() for f in osaka_features)
""",
    return_vars=["n_osaka", "osaka_area"],
)
print(result.return_values)
# {"n_osaka": 1, "osaka_area": 1.97}
```

`return_values` is JSON-serialized; complex PyQGIS objects (`QgsGeometry`,
`QgsVectorLayer`) fall back to their `repr()` strings. Unbound names in
`return_vars` are omitted from the response.

## Common gotchas

- **CRS mismatches.** PFLOW files are EPSG:4326 (lon/lat). If `qgis_layer_inspect`
  shows a different CRS, pass `crs="EPSG:4326"` to `qgis_load_layer` to override.
- **Zone-id systems coexist.** `zone_trips.csv` uses `MFS##`, `od_flows.csv` uses
  `PRF##` for truck (prefectures) and `MFS##` for taxi. Always confirm
  `qgis_layer_inspect` on the polygon layer to see which field has matching values.
- **Trajectory big-data.** 1 GB CSVs without `sample_rate` will use ~3 GB RAM.
  Start with `sample_rate=0.001` for exploration.
- **Print composer requires plugin transport for layout iteration.** `qgis_export_layout`
  works in headless, but if you want to *design* the layout interactively, that's
  QGIS Desktop only.
