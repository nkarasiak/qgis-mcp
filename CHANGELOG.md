# Changelog

All notable changes to qgis-mcp-north are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## v1.2.0 — 2026-05-22 — qgis_render_link_density

New tool for DRM-link traffic-density choropleths from PFLOW trajectories.

Added:
- `qgis_render_link_density(trajectory_csvs, drm_network_path, output_png, ...)` — streams trajectory CSVs row-by-row, aggregates per-link counts/sums, renders graduated line layer. Big-data ready (works on multi-GB inputs without loading them fully).
- `scripts/build_drm_network.py` — one-time prep script. Reads `drm_*.tsv` (47 prefecture shards), writes `assets/drm_network.gpkg` indexed by `link_id`. Requires `[drm]` extra (`pyogrio` + `geopandas`).
- `LinkDensityResult` pydantic model. `DRMNetworkNotFoundError` exception.
- `[drm]` optional extra (`pyogrio>=0.7`, `geopandas>=0.14`) — used only by the prep script; tool runtime has no new deps.
- Plugin handler `render_link_density` (graduated line symbology over the DRM GeoPackage). Uses existing `QgsGraduatedSymbolRenderer(field)` + `_CLASSIFICATION_METHODS` pattern for consistency with `render_choropleth`.
- W17 demo `--with-link-density` flag (`scripts/demo_w17.py`).
- Unit tests: `tests/test_render_link_density.py` (7), `tests/test_link_density_aggregate.py` (6), `tests/test_build_drm_network.py` (5), `tests/test_errors_drm.py` (1). Slow integration: `tests/test_build_drm_network_gpkg.py` (2).
- `pythonpath = ["."]` added to `[tool.pytest.ini_options]` so `from scripts.build_drm_network import ...` works in test discovery.

Unchanged:
- Existing 13 tools, response shapes, error taxonomy, transports.
- No new runtime deps for `qgis-mcp-workflows` itself — pyogrio + geopandas are only the prep-script's deps.

Resolves DESIGN.md §8 open question #8.

## v1.1.0 — 2026-05-22 — Rename to qgis-mcp-workflows

Pure rename release. The package, plugin, console script, env vars, and MCP-client config key all change to make the fork's positioning ("workflow tools, not 51 PyQGIS primitives") the literal name. No behavior changes; 99-test suite green before and after.

Renames:
- Package: `qgis-mcp-north` → `qgis-mcp-workflows`
- Plugin folder: `qgis_mcp_north_plugin/` → `qgis_mcp_workflows_plugin/`
- Console script: `qgis-mcp-north-server` → `qgis-mcp-workflows-server`
- Python package: `qgis_mcp_north` → `qgis_mcp_workflows`
- Env vars: `QGIS_MCP_NORTH_*` → `QGIS_MCP_WORKFLOWS_*` (HOST, PORT, TRANSPORT, TOOL_MODE, QGIS_LAUNCHER, REPO_ROOT, LOG_FILE, LOG_LEVEL)
- MCP-client config key in installer: `qgis-north` → `qgis-workflows`
- Error base class: `QgisMcpNorthError` → `QgisMcpWorkflowsError`
- Logger: `QgisMcpNorthServer` → `QgisMcpWorkflowsServer`
- Plugin LOG_TAG: `MCP-NORTH` → `MCP-WORKFLOWS`

Unchanged:
- Socket port `9877` (still distinct from upstream's 9876)
- Co-existence guarantee with upstream `nkarasiak/qgis-mcp`
- Plugin class name `QgisMCPServer` (internal; inherited from upstream)
- Tool names (`qgis_layer_inspect`, `qgis_render_choropleth`, etc.)
- Tool response shapes, error message format, escape-hatch behavior
- Historical completion-report docs (`docs/v0.3-*`, `docs/v0.4-*`, `docs/v0.5-*`, `docs/v1.0-*`) — frozen snapshots

Migration: see [README §v1.1.0 rename migration](README.md#v110-rename-migration).

Latent-bug fix folded in: `src/qgis_mcp_workflows/helpers.py:38`'s `importlib.metadata.version("qgis-mcp")` (looked up upstream's package name) is now `version("qgis-mcp-workflows")` — pre-existing diagnose mismatch.

## [1.0.0] — 2026-05-14

### Added
- **Tool surface complete.** Final 3 stubs shipped:
  - `qgis_style_categorized` — categorical (one color per unique value) symbology with per-class feature counts.
  - `qgis_style_graduated` — graduated (value-binned) symbology with `mode ∈ {quantile, equal_interval, natural_breaks, pretty}` and explicit breaks array.
  - `qgis_eval` — arbitrary PyQGIS escape hatch with `return_vars` capture (unbound names omitted; complex types repr-ified via `_json_safe`).
- **Compound mode.** `QGIS_MCP_NORTH_TOOL_MODE=compound` env var collapses 13 standalone tools to 5 grouped tools (`qgis_inspect`, `qgis_style`, `qgis_render`, `qgis_export`, `qgis_eval`) for token-constrained LLMs.
- **Benchmarks scaffolding.** `tests/benchmarks/` with cold-start, trajectory-scaling (1k/10k/100k/500k), choropleth, and transport-parity benchmarks. Opt-in via `pytest -m bench`. `[bench]` extra adds `pytest-benchmark>=4.0`.
- **End-to-end W17 demo.** `scripts/demo_w17.py` runnable standalone, plus `tests/integration/test_w17_demo.py` with fake / plugin / headless modes.
- **Installer tests.** 13 tests covering `qgis_plugins_dir`, `install_plugin`, `uninstall_plugin`, `configure_client`, and CLI arg parsing.
- **Docs.** `README.md` full rewrite (no longer describes upstream's 51-tool surface). New: `docs/v0.5-completion-report.md`, `docs/v1.0-completion-report.md`, `docs/pflow-usage.md`, `docs/benchmarks-v0.5.md`, `CHANGELOG.md`. `CLAUDE.md` updated to reflect 13/13 tools + compound mode.
- **134-zone synthetic fixture** at `tests/benchmarks/fixtures/scaled_zones_134.geojson` (built by `scripts/build_scaled_zones.py`).
- **`assets/screenshots/w17_demo.png`** — produced by the W17 demo; embedded in README.

### Changed
- `qgis_mcp_north_plugin/plugin.py`:
  - `set_layer_style` now returns rich response (`n_classes`, `classes:[{value, color, n_features}]`, plus `breaks` + `mode` for graduated). Honors `mode` arg via existing `_CLASSIFICATION_METHODS` dict.
  - `execute_code` accepts `return_vars` + uses new `_json_safe()` helper.
  - Both handlers gracefully degrade when running in headless transport (no `iface.layerTreeView()` call).
- `install.py` — added missing `main()` invocation under `if __name__ == "__main__":` (caught by the new test suite).

### Tested
- 99 unit tests pass + 3 skips (movingpandas, plugin E2E, headless E2E — gated on tooling availability) + 11 deselected (benchmarks).
- Ruff clean across `src/`, `tests/`, `scripts/`, `install.py`.

### Support claim
- **Windows-only for v1.0.** Linux/macOS may work via PyQGIS-on-PATH but is unverified. Refinement deferred until a non-Windows user reports.

### Out of scope (deferred to v2+)
- DuckDB integration (`qgis_render_from_duckdb`).
- DRM-link aggregation (`qgis_render_link_density`).
- WMS/WFS map servers.
- deck.gl JSON-spec output.

## [0.5.0] — 2026-05-14

### Added
- 5 of 8 stubbed v0.5 tools shipped:
  - `qgis_render_trajectory` — lines/points/heatmap from PFLOW CSV (or GPX). Stride sampling + `max_points` ceiling. Optional `movingpandas` speed-binned line rendering via `[trajectory]` extra.
  - `qgis_render_od_flows` — centroid arcs over a zones layer with data-defined stroke width. Unmatched origin/destination counts surface in response.
  - `qgis_project_load` — loads `.qgz`/`.qgs`, returns layers + layouts; stateful for downstream tools.
  - `qgis_export_layout` — PNG/PDF/SVG via `QgsLayoutExporter`. Loads `qgz_path` internally if not already loaded.
  - `qgis_batch_render` — fan-out per attribute value. Active-layer convention (saved-active → first vector fallback). Manifest + per-value errors. `subset_string` reset in `finally`.
- New typed errors: `EmptyAfterFilterError`, `ProjectLoadError`, `LayoutNotFoundError` — each ending in "Next: …" recovery hints.
- 37 new unit tests (66 total).
- Synthetic test fixtures: `tests/fixtures/tiny_trajectory.csv` (30 rows × 3 trips), `tiny_od.csv` (6 OD pairs), `tiny_zones.geojson` (4 polygons).

### Changed
- All 5 new plugin handlers ship with both plugin AND headless transports for free, per v0.4's stub-iface architecture.

## [0.4.0] — 2026-05-01

### Added
- Headless transport. PyQGIS subprocess executor (`src/qgis_mcp_north/executors/headless.py`) re-uses the plugin's `QgisMCPServer.execute_command` via a stub `iface`.
- `--transport={plugin,headless,auto}` CLI flag with auto-probe of port 9877.
- `qgis_load_layer(crs=...)` wired through plugin's `set_layer_crs` with rollback on failure.
- New errors: `HeadlessUnavailableError`, `CrsMismatchError`.
- 4 new integration tests for headless executor.

## [0.3.0] — 2026-04-30

### Added
- v0.3 MVP — 5 of 13 MCP tools implemented end-to-end against the plugin transport:
  - `qgis_layer_inspect`, `qgis_load_layer`, `qgis_render_map`, `qgis_render_choropleth`, `qgis_figures_to_pptx`.
- Plugin: `render_layers_to_path` and `render_choropleth` (atomic load+style+render+cleanup).
- Executor abstraction at `src/qgis_mcp_north/executors/plugin.py`.
- Typed errors module with 5 initial classes.
- 25 unit tests.

## [0.1.0] — 2026-04-30

### Added
- Initial fork from `nkarasiak/qgis-mcp` v0.2.1.
- Cut to 12 workflow tools + 1 escape hatch (`qgis_eval`) — see `docs/DESIGN.md`.
- Renamed: package `qgis_mcp_north`, plugin folder `qgis_mcp_north_plugin`, default port 9877 (vs upstream 9876).
- 13 tool stubs registered with FastMCP.
