# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`qgis-mcp-north` (v0.4.0) is a focused fork of `nkarasiak/qgis-mcp` for transportation-research figure pipelines (PFLOW, GUFM). It exposes QGIS to Claude over MCP via **two transports**: a TCP-socket plugin running in QGIS Desktop, and a long-lived PyQGIS subprocess (headless mode) for cron / CI / unattended renders. The fork rationale, full tool surface, response shapes, error model, and roadmap live in [`docs/DESIGN.md`](docs/DESIGN.md) — that document is the spec; if code disagrees with it, update the doc first.

Key differences from upstream:
- 12 workflow tools + 1 escape hatch (`qgis_eval`), not 51 PyQGIS-mirroring tools.
- Two transports: `plugin` (TCP socket → running QGIS) and `headless` (PyQGIS subprocess), selected via `--transport=auto|plugin|headless`.
- Plugin folder: `qgis_mcp_north_plugin/`. Python package: `qgis_mcp_north`. Default socket port: **9877** (vs upstream 9876). Both servers can run side-by-side.

## Architecture

```
┌───────────────────────────────────────┐
│        MCP Server (FastMCP)           │
│   src/qgis_mcp_north/server.py        │
└───────────────┬───────────────────────┘
                │
   transport=plugin           transport=headless
                │                   │
                ▼                   ▼
   PluginExecutor          HeadlessExecutor
   (TCP socket :9877)      (subprocess Popen)
                │                   │
                ▼                   ▼
   QGIS Desktop plugin       headless_runner.py
   (QTimer poll loop,        (stdin/stdout JSON,
   QgisMCPServer class)      same QgisMCPServer class)
                │                   │
                └────────┬──────────┘
                         ▼
                    PyQGIS API
```

Both transports execute the **same** `QgisMCPServer.execute_command` from `qgis_mcp_north_plugin/plugin.py`. The headless runner instantiates the class with a stub `iface` (`_StubIface`) that no-ops UI calls and raises loudly for genuine Desktop-only operations. This keeps plugin and headless on one codebase — every command handler that doesn't touch `self.iface` works in both transports for free.

**Tools never speak transport directly.** They call `executors.get_executor().dispatch(command, params)`. The `Executor` Protocol is in `src/qgis_mcp_north/executors/__init__.py`; concrete implementations are `executors/plugin.py` and `executors/headless.py`. Tests inject a `FakeExecutor` via `executors.set_executor()`.

**Wire protocol.** Length-prefixed JSON. 4-byte big-endian uint32 length header + JSON payload. Both TCP (plugin) and stdin/stdout (headless) use the same framing — `qgis_mcp_north.helpers.HEADER_STRUCT`.

## Commands

```bash
# Run the MCP server with auto-detected transport (probes :9877, falls back to headless)
uv run --no-sync qgis-mcp-north-server

# Force a specific transport
uv run --no-sync qgis-mcp-north-server --transport=plugin
uv run --no-sync qgis-mcp-north-server --transport=headless

# Or via env (CLI takes precedence)
QGIS_MCP_NORTH_TRANSPORT=headless uv run --no-sync qgis-mcp-north-server

# Override the QGIS Python launcher (default: auto-detect from common Windows / OSGeo4W locations)
QGIS_MCP_NORTH_QGIS_LAUNCHER='M:\QGIS LTR\bin\python-qgis-ltr.bat' uv run --no-sync qgis-mcp-north-server --transport=headless

# Run the multi-client installer (plugin symlink + MCP client config)
python install.py

# Unit tests (no QGIS needed — mocked executor)
uv run --no-sync pytest tests/ -v

# Lint
uv tool run ruff check src/ tests/
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `QGIS_MCP_NORTH_HOST` | `localhost` | Plugin transport: host of the QGIS plugin socket |
| `QGIS_MCP_NORTH_PORT` | `9877` | Plugin transport: port of the QGIS plugin socket |
| `QGIS_MCP_NORTH_TRANSPORT` | `auto` | `auto` / `plugin` / `headless`. CLI `--transport` overrides |
| `QGIS_MCP_NORTH_QGIS_LAUNCHER` | (auto-detected) | Headless transport: full path to `python-qgis(-ltr).bat` (Windows) or PyQGIS Python (Linux/macOS) |
| `QGIS_MCP_NORTH_REPO_ROOT` | (auto-derived) | Headless transport: repo root the runner adds to `sys.path` so it can import `qgis_mcp_north_plugin` |
| `QGIS_MCP_NORTH_LOG_FILE` | `~/.local/share/qgis-mcp-north/server.log` | Rotating log file (5MB × 3) — empty disables file logging |
| `QGIS_MCP_NORTH_LOG_LEVEL` | `INFO` | File log level. Console (stderr) is always WARNING+ |

## MCP Tools (13 total — see `docs/DESIGN.md` §4 for full signatures)

| Tool | Status | Notes |
|---|---|---|
| `qgis_layer_inspect` | ✅ v0.3 | Read-only metadata; loads + removes transiently |
| `qgis_load_layer` | ✅ v0.3 / v0.4 | `crs=` override (v0.4) wired through `set_layer_crs` with rollback |
| `qgis_project_load` | ⏳ v0.5 | NotImplementedError stub |
| `qgis_style_categorized` | ⏳ v0.5 | NotImplementedError stub |
| `qgis_style_graduated` | ⏳ v0.5 | NotImplementedError stub |
| `qgis_render_map` | ✅ v0.3 | Plugin handler: `render_layers_to_path` |
| `qgis_render_choropleth` | ✅ v0.3 | Plugin handler: `render_choropleth` (atomic load+style+render+cleanup) |
| `qgis_render_trajectory` | ⏳ v0.5 | NotImplementedError stub |
| `qgis_render_od_flows` | ⏳ v0.5 | NotImplementedError stub |
| `qgis_export_layout` | ⏳ v0.5 | NotImplementedError stub |
| `qgis_batch_render` | ⏳ v0.5 | NotImplementedError stub |
| `qgis_figures_to_pptx` | ✅ v0.3 | Pure python-pptx; `two_column` + `title_image_caption` degrade to `title_only` |
| `qgis_eval` | ⏳ v0.5 | NotImplementedError stub — escape hatch for the long tail |

## Key Details

- **Python**: 3.12. Package manager: `uv` (pyproject.toml).
- **Main deps**: `mcp[cli]>=1.20.0`, `pydantic>=2.7`. Optional: `python-pptx` (`pptx` extra), `movingpandas` (`trajectory` extra).
- **Tools are sync `def`** (not async — the v0.4 dispatch path is synchronous; FastMCP supports both).
- **All response paths return `output_path` (absolute)**. No tool returns base64. No tool returns relative paths.
- **Errors must be actionable.** Every typed exception in `src/qgis_mcp_north/errors.py` ends with `Next: <suggested tool call>`. Add new error classes when a recovery hint changes.
- **Headless caveats**: handlers that genuinely need `iface` (canvas extent/refresh, layer-tree-view manipulation, message bar) raise loudly from the stub; switch to plugin transport for those. The v0.3 + v0.5 workflow tools shouldn't hit any of them.
- **Subprocess lifecycle**: `HeadlessExecutor` lazy-spawns on first dispatch, holds the process open across the MCP session, sends `{"type": "shutdown"}` on `__del__`. `initQgis` costs ~1-2s per spawn — never restart it per call.

## Version Management

Two version files must stay in sync:
- `pyproject.toml` → `version = "X.Y.Z"` (MCP server / package)
- `qgis_mcp_north_plugin/metadata.txt` → `version=X.Y.Z` (QGIS plugin repository)

The QGIS plugin repository rejects re-uploads at the same version, so always bump both together.

## Plugin Installation

`python install.py` symlinks `qgis_mcp_north_plugin/` into the active QGIS profile and configures MCP clients. After QGIS restart, enable via the Plugins menu.

## Co-existence with upstream `nkarasiak/qgis-mcp`

Both can run side-by-side: different plugin folders, different ports (9877 vs 9876), different package names. Claude Desktop sees two MCP servers; the LLM picks per request based on tool descriptions. Use upstream when you need the long tail of PyQGIS primitives (feature editing, processing algorithms, layer-tree groups). Use this fork for the workflow tools above.
