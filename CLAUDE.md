# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

QGIS MCP connects QGIS to Claude AI through the Model Context Protocol (MCP), enabling Claude to directly control QGIS via socket-based communication. Includes a multi-client installer (`install.py`) for easy setup.

## Architecture

The system has two components that communicate over a TCP socket (default `localhost:9876`, configurable via env vars):

1. **QGIS Plugin** (`qgis_mcp_plugin/`) - Runs inside QGIS (3.28–4.x). `server.py`'s `QgisMCPServer` creates a non-blocking TCP socket server using a `QTimer` (25ms poll interval) to accept connections and process JSON commands within QGIS's event loop; it owns the socket, framing, the auth gate and dispatch, and nothing else. The commands themselves are mixins under `handlers/` (one module per domain: `system`, `project`, `layers`, `features`, `style`, `canvas`, `processing`, `layout`, `connections`, plus `base` for the shared layer lookups), combined onto `QgisMCPServer`. `plugin.py` holds only `QgisMCPPlugin` and `classFactory` (the QGIS entry point, toolbar and menu); `configurator.py` holds the MCP-client configuration dialog. Supporting modules, all stdlib-only and `qgis`-free: `wire.py` (framing), `errors.py` (`CommandError` and friends), `registry.py` (the `@command` decorator), `constants.py` (defaults, settings prefix, `PLUGIN_DIR`). `compat.py` provides enum compatibility between QGIS 3.x and 4.x (see below).

   **Adding a command:** write the method on the mixin for its domain, decorate it with `@command` from `registry`, and add the matching `@mcp.tool` in `src/qgis_mcp/server.py`. There is no dispatch table to update - `@command` registers the name and `_dispatch` resolves it. `tests/test_plugin_structure.py` enforces both directions of that parity, plus the rule that `handlers/` modules import package-root modules with `..` and never import each other.

2. **MCP Server** (`src/qgis_mcp/server.py`) - Runs outside QGIS as a standalone Python process. Uses `FastMCP` from the `mcp` library to expose QGIS operations as MCP tools, resources, and prompts. A `_send()` helper unwraps the response envelope and raises on errors. All 118 tools are `async` with `title=` for human-readable names. Uses `ToolAnnotations` for read-only/destructive/idempotent hints. Long-running tools use `ctx.info()` for MCP logging. Destructive tools call `_confirm_destructive`, which only elicits when asked to (see `QGIS_MCP_AUTO_CONFIRM` below). An optional compound tool mode (`src/qgis_mcp/compound_tools.py`) groups tools into 27 compound tools for reduced schema overhead.

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

# Run with compound tool mode (reduces 118 tools to 27 grouped tools)
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
| `QGIS_MCP_TOKEN` | _(unset)_ | Optional shared secret. When set, the plugin requires a matching `token` on every command (constant-time compare); the client attaches it automatically. Unset = no auth (default, backward-compatible). Required when the plugin binds a non-loopback address - the plugin refuses to start otherwise. |
| `QGIS_MCP_TRANSPORT` | `stdio` | MCP transport: `stdio` or `streamable-http` |
| `QGIS_MCP_LOG_FILE` | `~/.local/share/qgis-mcp/server.log` | Log file path (empty to disable file logging) |
| `QGIS_MCP_LOG_LEVEL` | `INFO` | File log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `QGIS_MCP_TOOL_MODE` | `granular` | Tool registration mode: `granular` (118 tools) or `compound` (27 grouped tools) |
| `QGIS_MCP_AUTO_CONFIRM` | on | `0`/`false`/`no`/`off` makes `_confirm_destructive` elicit; anything else (including unset) skips it. Default-skip because MCP clients gate destructive tool calls themselves - see the confirmation note in Key Details. |

## MCP Tools, Resources, Prompts, Protocol Features

See the `qgis-mcp-tools` skill (`.claude/skills/qgis-mcp-tools/SKILL.md`) for the full tool table (118 tools), MCP resources, prompts, and protocol features (logging, elicitation, completions, annotations, compound mode).

## Key Details

- **Python version**: 3.12
- **Package manager**: uv (pyproject.toml based)
- **Main dependency**: `mcp[cli]>=1.20.0,<3` (v1.26.0 locked). mcp 2.0 renamed `mcp.server.fastmcp` → `mcp.server.mcpserver` (`FastMCP` → `MCPServer`), so `server.py` and `compound_tools.py` import it through a try/except shim and the rest of the code keeps the `FastMCP` name. `Tool.inputSchema` became `Tool.input_schema`, and `mcp.shared.memory.create_connected_server_and_client_session` is gone - `tests/mcp_compat.py` papers over both for tests. CI runs the unit suite against both the locked mcp and the newest release (issue #25).
- **Dev dependencies**: `pytest>=7.0`, `pytest-asyncio>=0.23`
- **Socket protocol**: Length-prefixed framing over TCP. Each message: 4-byte big-endian uint32 length header + JSON payload bytes. Client sends `{"type": "<command>", "params": {...}}`, server responds `{"status": "success"|"error", "result": ...}`. Plugin-side framing helpers live in `qgis_mcp_plugin/wire.py` (stdlib-only, no `qgis` imports, must stay Python 3.9-compatible): responses queue through an `OutboundBuffer` drained across timer ticks because the client sockets are non-blocking and `sendall` would truncate large frames. `tests/test_py39_compat.py` is an AST guard against 3.10-only constructs in the plugin (PEP 604 unions in `isinstance`, `zip(strict=)` - use the tuple form and `wire.zip_strict()`).
- **Connection management**: MCP server validates connection via `getpeername()`. Host/port configurable via `QGIS_MCP_HOST`/`QGIS_MCP_PORT` env vars. QGIS plugin supports up to 10 concurrent clients (e.g. multiple Claude Code instances each spawning their own MCP server process).
- **Multi-instance**: `QGIS_MCP_INSTANCES` lets one server address several running QGIS windows. `get_instances()` resolves the config from the environment on every call; connections, TTL validation timestamps, first-connect retry state and locks are all keyed by instance name (`_qgis_connections`, `_connection_validated_at`, `_first_connected`, `_qgis_locks`), so two instances never serialize against each other. `_send_sync()`/`_send()` take a trailing `instance` argument and every `@mcp.tool` function forwards its own `instance: str | None = None` parameter (`test_every_tool_forwards_instance` enforces this for all of them). Unset env = one instance named `default`, identical to the previous behaviour. Instance-less calls resolve via `implicit_instance()`: the entry named `default` when present, otherwise the first entry in insertion order. Compound tool mode (`QGIS_MCP_TOOL_MODE=compound`) exposes no instance selection, so configuring more than one instance in that mode refuses to start (`SystemExit`) rather than routing every call to one QGIS.
- **All tools async**: Every tool function is `async def` to enable `await ctx.info()`, `ctx.elicit()`, etc. The `_send_sync()` helper stays synchronous (blocking socket call - acceptable since responses are fast).
- **Feature format**: Flat dicts with `_fid` (feature ID) and attributes at top level. Geometry in `_geometry` key when requested.
- **Edit sessions**: `start_editing`/`commit_edits`/`rollback_edits`/`undo_edits`/`redo_edits` drive `QgsVectorLayer`'s edit buffer and undo stack. `add_features`, `update_features`, `delete_features` and `update_feature_geometry` check `layer.isEditable()` and use the layer-level API when a session is open (writes land in the buffer, undoable, discarded by rollback) and `dataProvider()` otherwise; every one of them reports which path it took via `buffered` in the response. Writing to the provider under an open session would land beneath the buffer and be lost.
- **Database connections**: `list_connections`/`create_postgresql_connection`/`list_connection_tables`/`add_layer_from_connection`/`import_layer_to_connection`/`execute_connection_sql` wrap `QgsProviderRegistry.providerMetadata(...).connections()` and `QgsAbstractDatabaseProviderConnection` - the Browser panel's saved connections (PostgreSQL, GeoPackage, SpatiaLite, MS SQL, ...). `create_postgresql_connection` accepts either QGIS Authentication Manager configuration ID or service configuration (never a password), validates the conenction before persistence, and rejects duplicate names. Connection URIs are password-redacted (`_redact_uri`) before leaving the plugin. Import builds an OGR uri (container file + `layerName` option) for `ogr` connections and a `QgsDataSourceUri` for database providers, then calls `QgsVectorLayerExporter.exportLayer`.
- **Raster styling**: `set_raster_style` covers `singleband_pseudocolor`, `singleband_gray`, `multiband_color` and `hillshade`; `set_layer_style` stays vector-only. Unset `min_value`/`max_value` fall back to `bandStatistics`.
- **`get_layer_features` limit**: MCP tool caps at 50 features (default 10). Supports `expression` for server-side filtering.
- **Destructive-tool confirmation**: `_confirm_destructive` (9 call sites, `execute_code` among them) returns True without eliciting unless `QGIS_MCP_AUTO_CONFIRM` is `0`/`false`/`no`/`off`. It was never a safety boundary - it already failed open when the client lacked elicitation support (#27) - and MCP puts human-in-the-loop on the client, which the `destructiveHint` annotation feeds. Two prompts per call trained click-through, weakening the client's own gate. Keep the `destructiveHint` annotations: they are what the client's gate reads.
- **Batch support**: `batch` command type executes multiple commands in sequence, returns array of results. `BATCH_BLOCKED_COMMANDS` (elicit-always destructive commands) is enforced on both sides: in the MCP server and in the plugin (`wire.BATCH_BLOCKED_COMMANDS`, pinned equal to `qgis_mcp.protocol`'s copy by a test), so a direct socket client cannot bypass confirmation by wrapping them in a batch.
- **Configurable timeouts**: `execute_processing`, `render_map`, `execute_code` use 60s; others default to 30s. `execute_processing` also takes an optional `timeout` (algorithm deadline, default 55s enforced in the plugin via a `QgsProcessingFeedback` that pumps the Qt event loop and cancels past the deadline; the socket timeout is kept 5s above it so the plugin fails first with a real message).
- **render_map**: Returns inline `ImageContent` (base64 PNG) so Claude can see the map directly. Optional `path` param also saves to disk.
- **get_canvas_screenshot**: Fast canvas widget grab via `QWidget.grab()` - no re-render, returns inline `ImageContent`.
- **transform_coordinates**: Uses `QgsCoordinateTransform` for point(s) and bbox CRS conversion.
- **Token optimizations**: Tools return dicts (no double JSON serialization). Plugin strips redundant metadata from responses. Features use flat format.
- **Message log capture**: Plugin connects to `QgsApplication.messageLog().messageReceived` on start, stores up to 1000 entries in a `deque`. Disconnects on stop.
- **Plugin error model**: handlers raise `CommandError` (or its `LayerNotFound` / `WrongLayerType` subclasses) from `errors.py` for anything the caller can act on - bad parameters, missing layer, unsupported option. `_dispatch` returns those as `{"status": "error", "message": ...}` and logs them at WARNING. Any *other* exception is treated as a plugin defect: the traceback is logged at CRITICAL and the response carries `"internal": True`. Never `raise Exception(...)` in a handler - it becomes indistinguishable from a bug. `_dispatch` also validates the caller's parameters against the handler signature (`inspect.signature(...).bind`, cached per command) *before* calling, so a missing or misspelled argument comes back as an ordinary error instead of a CRITICAL traceback in the user's QGIS log.
- **Version drift between the two halves**: they update by different mechanisms (QGIS Plugin Manager for the plugin, the uvx cache for the MCP server, which pins whatever it cached for the branch archive URL), so drift is the default rather than an accident. The client attaches `client_version` to every envelope (`protocol.get_client_version()`, from the installed dist metadata); the plugin records it after the auth gate (`_record_client_version`, bounded by `MAX_VERSION_LENGTH` and `MAX_TRACKED_VERSIONS` since it reaches the user), reports it in `diagnose`'s `client_versions` check, shows it in the configurator, and logs one line per newly seen version to the `MCP` tab of the Log Messages panel (WARNING with the fix command when it differs from the plugin, INFO when it matches). Nothing goes to the message bar over the canvas: drift is advisory and the user did not do anything to trigger it. `enrich_diagnose` adds `fix` (the command for the detected install type) and `note` to a `version_match` mismatch. Mismatches are advisory, not fatal - only tools added since the older half was built are missing - so nothing in the UI is styled as a failure.
- **Table-driven handlers**: options with a fixed set of values are class-level dicts, not if/elif chains (`_LAYER_PROPERTIES`, `_LAYOUT_EXPORTS`, `_WEB_SERVICES`, `_INPUT_CLASSES`/`_INPUT_BUILDERS`, `_VECTOR_STYLES`, `_RASTER_STYLES`, `_SHADER_*`, `_CONTRAST_ALGORITHMS`). `_pick(mapping, key, label)` does the lookup and raises `Unknown <label>: <key>. Use one of [...]` generated from the table's keys, so the accepted-values list in the error can never drift from the code. Values that need attribute lookup on the running QGIS (settings classes, setters, builder methods) are stored as *names* and resolved with `getattr` - also because a `staticmethod` object is not callable on Python 3.9.
- **QGIS 3.x/4.x compat**: `qgis_mcp_plugin/compat.py` resolves deprecated enum forms at import time via try/except (e.g. `QgsMapLayer.VectorLayer` → `Qgis.LayerType.Vector`). The plugin imports constants like `LAYER_VECTOR`, `MSG_WARNING`, `AGG_COUNT` from `compat` instead of using raw enum values. When adding new enum usages, add the compat constant to `compat.py` first.

## Release, Linting, and Plugin Installation

See the `release-checklist` skill (`.claude/skills/release-checklist/SKILL.md`) for version-sync steps (pyproject.toml/metadata.txt/uv.lock + changelog), the dev→main fast-forward requirement before tagging, pre-upload linting (ruff + flake8), and plugin installation.
