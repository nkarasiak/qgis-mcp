# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

QGIS MCP (v0.2.0) connects QGIS to Claude AI through the Model Context Protocol (MCP), enabling Claude to directly control QGIS via socket-based communication. Includes a multi-client installer (`install.py`) for easy setup.

## Architecture

The system has two components that communicate over a TCP socket (default `localhost:9876`, configurable via env vars):

1. **QGIS Plugin** (`qgis_mcp_plugin/plugin.py`) — Runs inside QGIS (3.28–4.x). A `QgisMCPServer` class creates a non-blocking TCP socket server using a `QTimer` (25ms poll interval) to accept connections and process JSON commands within QGIS's event loop. Includes a `QgisMCPDockWidget` UI for start/stop control, and `QgisMCPPlugin` as the standard QGIS plugin entry point (`classFactory`). All command handlers live in this file. A companion `compat.py` module provides enum compatibility between QGIS 3.x and 4.x (see below).

2. **MCP Server** (`src/qgis_mcp/server.py`) — Runs outside QGIS as a standalone Python process. Uses `FastMCP` from the `mcp` library to expose QGIS operations as MCP tools, resources, and prompts. A `_send()` helper unwraps the response envelope and raises on errors. All 51 tools are `async` with `title=` for human-readable names. Uses `ToolAnnotations` for read-only/destructive/idempotent hints. Long-running tools use `ctx.info()` for MCP logging. Destructive tools use `ctx.elicit()` for user confirmation (with graceful fallback). An optional compound tool mode (`src/qgis_mcp/compound_tools.py`) groups tools into 25 compound tools for reduced schema overhead.

**Data flow:** Claude → MCP Server (FastMCP) → TCP socket → QGIS Plugin (QTimer loop) → PyQGIS API → response back through socket.

There is also a standalone socket client at `src/qgis_mcp/client.py` (`QgisMCPClient` class) used for direct testing without MCP.

## Commands

```bash
# Run the MCP server (how Claude Desktop launches it)
uv run --no-sync src/qgis_mcp/server.py

# Run with custom host/port
QGIS_MCP_HOST=192.168.1.100 QGIS_MCP_PORT=9877 uv run --no-sync src/qgis_mcp/server.py

# Run against several QGIS instances from one server (tools take instance="b")
QGIS_MCP_INSTANCES=default=9876,b=9877 uv run --no-sync src/qgis_mcp/server.py

# Run with streamable HTTP transport (for remote/multi-client)
QGIS_MCP_TRANSPORT=streamable-http uv run --no-sync src/qgis_mcp/server.py

# Run with compound tool mode (reduces 104 tools to 25 grouped tools)
QGIS_MCP_TOOL_MODE=compound uv run --no-sync src/qgis_mcp/server.py

# Run the multi-client installer (plugin symlink + MCP client config)
python install.py

# Run unit tests (no QGIS needed - mocked socket)
uv run --no-sync pytest tests/test_mcp_tools.py -v

# Run integration tests (requires QGIS plugin server running on localhost:9876)
uv run --no-sync pytest tests/test_qgis_live.py -v

# Run all tests
uv run --no-sync pytest tests/ -v
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `QGIS_MCP_HOST` | `localhost` | Host for QGIS plugin socket connection |
| `QGIS_MCP_PORT` | `9876` | Port for QGIS plugin socket connection |
| `QGIS_MCP_INSTANCES` | _(unset)_ | Comma-separated `name=port` / `name=host:port` list of QGIS instances addressable from one server (e.g. `default=9876,b=9877`). Unset = a single instance named `default` from `QGIS_MCP_HOST`/`QGIS_MCP_PORT`. Names match `[A-Za-z0-9_-]+`. |
| `QGIS_MCP_TOKEN` | _(unset)_ | Optional shared secret. When set, the plugin requires a matching `token` on every command (constant-time compare); the client attaches it automatically. Unset = no auth (default, backward-compatible). |
| `QGIS_MCP_TRANSPORT` | `stdio` | MCP transport: `stdio` or `streamable-http` |
| `QGIS_MCP_LOG_FILE` | `~/.local/share/qgis-mcp/server.log` | Log file path (empty to disable file logging) |
| `QGIS_MCP_LOG_LEVEL` | `INFO` | File log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `QGIS_MCP_TOOL_MODE` | `granular` | Tool registration mode: `granular` (104 tools) or `compound` (25 grouped tools) |

## MCP Tools, Resources, Prompts, Protocol Features

See the `qgis-mcp-tools` skill (`.claude/skills/qgis-mcp-tools/SKILL.md`) for the full tool table (103 tools), MCP resources, prompts, and protocol features (logging, elicitation, completions, annotations, compound mode).

## Key Details

- **Python version**: 3.12
- **Package manager**: uv (pyproject.toml based)
- **Main dependency**: `mcp[cli]>=1.20.0,<3` (v1.26.0 locked). mcp 2.0 renamed `mcp.server.fastmcp` → `mcp.server.mcpserver` (`FastMCP` → `MCPServer`), so `server.py` and `compound_tools.py` import it through a try/except shim and the rest of the code keeps the `FastMCP` name. `Tool.inputSchema` became `Tool.input_schema`, and `mcp.shared.memory.create_connected_server_and_client_session` is gone — `tests/mcp_compat.py` papers over both for tests. CI runs the unit suite against both the locked mcp and the newest release (issue #25).
- **Dev dependencies**: `pytest>=7.0`, `pytest-asyncio>=0.23`
- **Socket protocol**: Length-prefixed framing over TCP. Each message: 4-byte big-endian uint32 length header + JSON payload bytes. Client sends `{"type": "<command>", "params": {...}}`, server responds `{"status": "success"|"error", "result": ...}`.
- **Connection management**: MCP server validates connection via `getpeername()`. Host/port configurable via `QGIS_MCP_HOST`/`QGIS_MCP_PORT` env vars. QGIS plugin supports up to 10 concurrent clients (e.g. multiple Claude Code instances each spawning their own MCP server process).
- **Multi-instance**: `QGIS_MCP_INSTANCES` lets one server address several running QGIS windows. `get_instances()` resolves the config from the environment on every call; connections, TTL validation timestamps, first-connect retry state and locks are all keyed by instance name (`_qgis_connections`, `_connection_validated_at`, `_first_connected`, `_qgis_locks`), so two instances never serialize against each other. `_send_sync()`/`_send()` take a trailing `instance` argument and every `@mcp.tool` function forwards its own `instance: str | None = None` parameter (`test_every_tool_forwards_instance` enforces this for all of them). Unset env = one instance named `default`, identical to the previous behaviour. Instance-less calls resolve via `implicit_instance()`: the entry named `default` when present, otherwise the first entry in insertion order. Compound tool mode (`QGIS_MCP_TOOL_MODE=compound`) exposes no instance selection, so configuring more than one instance in that mode refuses to start (`SystemExit`) rather than routing every call to one QGIS.
- **All tools async**: Every tool function is `async def` to enable `await ctx.info()`, `ctx.elicit()`, etc. The `_send_sync()` helper stays synchronous (blocking socket call — acceptable since responses are fast).
- **Feature format**: Flat dicts with `_fid` (feature ID) and attributes at top level. Geometry in `_geometry` key when requested.
- **`get_layer_features` limit**: MCP tool caps at 50 features (default 10). Supports `expression` for server-side filtering.
- **Batch support**: `batch` command type executes multiple commands in sequence, returns array of results.
- **Configurable timeouts**: `execute_processing`, `render_map`, `execute_code` use 60s; others default to 30s.
- **render_map**: Returns inline `ImageContent` (base64 PNG) so Claude can see the map directly. Optional `path` param also saves to disk.
- **get_canvas_screenshot**: Fast canvas widget grab via `QWidget.grab()` — no re-render, returns inline `ImageContent`.
- **transform_coordinates**: Uses `QgsCoordinateTransform` for point(s) and bbox CRS conversion.
- **Token optimizations**: Tools return dicts (no double JSON serialization). Plugin strips redundant metadata from responses. Features use flat format.
- **Message log capture**: Plugin connects to `QgsApplication.messageLog().messageReceived` on start, stores up to 1000 entries in a `deque`. Disconnects on stop.
- **QGIS 3.x/4.x compat**: `qgis_mcp_plugin/compat.py` resolves deprecated enum forms at import time via try/except (e.g. `QgsMapLayer.VectorLayer` → `Qgis.LayerType.Vector`). The plugin imports constants like `LAYER_VECTOR`, `MSG_WARNING`, `AGG_COUNT` from `compat` instead of using raw enum values. When adding new enum usages, add the compat constant to `compat.py` first.

## Release, Linting, and Plugin Installation

See the `release-checklist` skill (`.claude/skills/release-checklist/SKILL.md`) for version-sync steps (pyproject.toml/metadata.txt/uv.lock + changelog), the dev→main fast-forward requirement before tagging, pre-upload linting (ruff + flake8), and plugin installation.
