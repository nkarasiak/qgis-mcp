#!/usr/bin/env python3
"""
QGIS MCP Server - Exposes QGIS operations as MCP tools, resources, and prompts.
"""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import sys
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.prompts.base import UserMessage
from mcp.types import (
    Annotations,
    Completion,
    CompletionArgument,
    ImageContent,
    ToolAnnotations,
)

from qgis_mcp.client import QgisMCPClient
from qgis_mcp.helpers import (
    BATCH_BLOCKED_COMMANDS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    TIMEOUT_DEFAULT,
    TIMEOUT_LONG,
    enrich_diagnose,
    make_layer_response,
    make_project_response,
    make_render_response,
)


def _setup_logging() -> logging.Logger:
    """Configure structured logging with stderr + optional rotating file handler."""
    _logger = logging.getLogger("QgisMCPServer")
    _logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    # stderr handler at WARNING+ to keep MCP stdio transport clean
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(fmt)
    _logger.addHandler(stderr_handler)

    # File handler (configurable via env vars)
    _default_log_file = os.path.join("~", ".local", "share", "qgis-mcp", "server.log")
    log_file_raw = os.environ.get("QGIS_MCP_LOG_FILE", _default_log_file)
    log_level_name = os.environ.get("QGIS_MCP_LOG_LEVEL", "INFO").upper()
    file_level = getattr(logging, log_level_name, logging.INFO)

    if log_file_raw != "":
        log_file = os.path.expanduser(log_file_raw)
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3)
        file_handler.setLevel(file_level)
        file_handler.setFormatter(fmt)
        _logger.addHandler(file_handler)
        # Set logger level to the minimum of both handler levels
        _logger.setLevel(min(logging.WARNING, file_level))
        _logger.info(f"Log file: {log_file}")
    else:
        _logger.setLevel(logging.WARNING)

    return _logger


logger = _setup_logging()


# ---------------------------------------------------------------------------
# Persistent connection management
# ---------------------------------------------------------------------------

_qgis_connection: QgisMCPClient | None = None
_connection_validated_at: float = 0.0
_CONNECTION_TTL: float = 5.0  # seconds between getpeername() validations
_qgis_lock = threading.Lock()  # serialize all socket access (asyncio.to_thread is concurrent)


def get_qgis_connection() -> QgisMCPClient:
    """Get or create a persistent QGIS connection.

    Uses a TTL cache for connection validation: getpeername() is only
    called at most once per _CONNECTION_TTL seconds, avoiding a syscall
    on every tool invocation.
    """
    global _qgis_connection, _connection_validated_at

    if _qgis_connection is not None:
        now = time.monotonic()
        if now - _connection_validated_at < _CONNECTION_TTL:
            return _qgis_connection
        try:
            _qgis_connection.socket.getpeername()
            _connection_validated_at = now
            return _qgis_connection
        except Exception:
            logger.warning("Existing connection is no longer valid, reconnecting")
            with contextlib.suppress(Exception):
                _qgis_connection.disconnect()
            _qgis_connection = None
            _connection_validated_at = 0.0

    host = os.environ.get("QGIS_MCP_HOST", DEFAULT_HOST)
    port_str = os.environ.get("QGIS_MCP_PORT", str(DEFAULT_PORT))
    try:
        port = int(port_str)
        if not 1 <= port <= 65535:
            raise ValueError("out of range")
    except ValueError as exc:
        raise ValueError(f"QGIS_MCP_PORT must be an integer 1-65535, got: {port_str!r}") from exc
    _qgis_connection = QgisMCPClient(host=host, port=port)
    if not _qgis_connection.connect():
        _qgis_connection = None
        raise ConnectionError("Could not connect to QGIS. Make sure the QGIS plugin is running.")
    _connection_validated_at = time.monotonic()
    logger.info(f"Created new persistent connection to QGIS at {host}:{port}")
    return _qgis_connection


# ---------------------------------------------------------------------------
# Helper: send command, unwrap envelope, raise on error
# ---------------------------------------------------------------------------


def _invalidate_connection() -> None:
    """Force-close the cached connection so the next call reconnects."""
    global _qgis_connection, _connection_validated_at
    if _qgis_connection is not None:
        with contextlib.suppress(Exception):
            _qgis_connection.disconnect()
        _qgis_connection = None
        _connection_validated_at = 0.0


_CONNECTION_ERRORS = (OSError, ConnectionError)
_MAX_RETRIES = 3
_RETRY_DELAYS = (0.5, 1.0)  # seconds between retries (last retry has no delay after)
# First-connect retries: more patient since QGIS/plugin may still be starting
_FIRST_CONNECT_RETRIES = 5
_FIRST_CONNECT_DELAYS = (1.0, 2.0, 3.0, 5.0)  # escalating backoff
_first_successful_connection = False


def _send_sync(command_type: str, params: dict | None = None, timeout: int = TIMEOUT_DEFAULT) -> dict:
    """Send a command synchronously and return the unwrapped result.

    Holds _qgis_lock for the entire send+recv cycle so that concurrent
    asyncio.to_thread calls cannot interleave frames on the shared socket.

    Retries on connection/socket errors with increasing delays. Uses a more
    patient retry schedule for the first connection (QGIS may still be starting),
    then shorter retries for subsequent reconnections (stale socket, plugin restart).
    """
    global _first_successful_connection
    last_exc: Exception | None = None

    if _first_successful_connection:
        max_retries = _MAX_RETRIES
        delays = _RETRY_DELAYS
    else:
        max_retries = _FIRST_CONNECT_RETRIES
        delays = _FIRST_CONNECT_DELAYS

    with _qgis_lock:
        for attempt in range(max_retries):
            try:
                qgis = get_qgis_connection()
                result = qgis.send_command(command_type, params, timeout=timeout)
                _first_successful_connection = True
                break
            except _CONNECTION_ERRORS as exc:
                last_exc = exc
                _invalidate_connection()
                if attempt < max_retries - 1:
                    delay = delays[min(attempt, len(delays) - 1)]
                    logger.warning(
                        "Connection error (%s), retrying in %.1fs (attempt %d/%d)",
                        exc,
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    time.sleep(delay)
                else:
                    logger.error("Connection failed after %d attempts: %s", max_retries, exc)
                    raise
        else:
            raise last_exc  # type: ignore[misc]  # unreachable, but satisfies type checker

    if not result or result.get("status") == "error":
        raise RuntimeError(result.get("message", "Command failed") if result else "No response")
    return result.get("result", {})


def _get_error_hint(message: str) -> str | None:
    """Return a helpful hint based on common QGIS/MCP error messages."""
    msg = message.lower()
    if "not found" in msg and "layer" in msg:
        return "Try calling 'get_layers' to see all valid layer IDs."
    if "field" in msg and "not found" in msg:
        return "Check the layer schema using 'qgis://layers/{layer_id}/schema'."
    if "crs" in msg or "projection" in msg:
        return "Verify CRS strings (e.g., 'EPSG:4326') or use 'transform_coordinates'."
    if "connection" in msg or "refused" in msg:
        return "Ensure the QGIS MCP plugin is started (Plugins > QGIS MCP > Start Server)."
    if "timeout" in msg:
        return "The operation took too long. For large renders or processing, this is expected."
    return None


async def _send(command_type: str, params: dict | None = None, timeout: int = 30) -> dict:
    """Send a command via asyncio.to_thread to avoid blocking the event loop."""
    try:
        return await asyncio.to_thread(_send_sync, command_type, params, timeout)
    except Exception as exc:
        message = str(exc)
        hint = _get_error_hint(message)
        if hint:
            logger.warning(f"Error hint added for: {message}")
            raise RuntimeError(f"{message}\n\nHINT: {hint}") from exc
        raise


# ---------------------------------------------------------------------------
# Helper: elicit confirmation for destructive operations
# ---------------------------------------------------------------------------


async def _confirm_destructive(ctx: Context, message: str) -> bool:
    """Ask user for confirmation before destructive operation.

    Returns True if client doesn't support elicitation (fail-open), since
    the tool is already marked destructive via ToolAnnotations and the client
    can gate execution at the tool-call level.
    """
    try:
        response = await ctx.elicit(
            message=message,
            schema={
                "type": "object",
                "properties": {
                    "confirm": {
                        "type": "boolean",
                        "description": "Confirm this operation",
                    },
                },
                "required": ["confirm"],
            },
        )
        return response.action == "accept" and bool(response.data.get("confirm"))
    except Exception:
        # Client doesn't support elicitation — proceed (fail-open).
        # The destructive ToolAnnotations hint lets clients gate at call time.
        logger.info("Elicitation not supported by client, proceeding with operation")
        return True


# ---------------------------------------------------------------------------
# Server lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Manage server startup and shutdown lifecycle.

    Uses lazy connection: does NOT connect to QGIS on startup.
    The first tool call triggers connection via _send_sync()'s retry loop,
    which is more robust (handles QGIS still starting, plugin not yet enabled).
    """
    host = os.environ.get("QGIS_MCP_HOST", DEFAULT_HOST)
    port = os.environ.get("QGIS_MCP_PORT", str(DEFAULT_PORT))
    logger.info(f"QgisMCPServer starting up (will connect to QGIS at {host}:{port} on first call)")
    try:
        yield {}
    finally:
        if _qgis_connection:
            logger.info("Disconnecting from QGIS on shutdown")
            _invalidate_connection()
        logger.info("QgisMCPServer shut down")


mcp = FastMCP(
    name="Qgis_mcp",
    instructions="QGIS integration through the Model Context Protocol. "
    "Use tools for actions, resources for read-only data, prompts for workflows.",
    lifespan=server_lifespan,
)


# ---------------------------------------------------------------------------
# Resource Cache for large results
# ---------------------------------------------------------------------------

_resource_cache: dict[str, str] = {}


def _cache_as_resource(data: Any, name_hint: str = "cache") -> str:
    """Generate a random ID, store data as JSON, and return a URI."""
    cache_id = secrets.token_hex(8)
    _resource_cache[cache_id] = json.dumps(data)
    return f"qgis://cache/{cache_id}"


@mcp.resource("qgis://cache/{cache_id}", name="cached_resource", description="Cached large result")
def cached_resource(cache_id: str) -> str:
    """Register an MCP resource handler for cached data."""
    if cache_id not in _resource_cache:
        raise ValueError(f"Cache ID not found: {cache_id}")
    return _resource_cache[cache_id]


# ===========================================================================
# MCP TOOLS (52 total)
# ===========================================================================

# --- Connectivity & Info ---


@mcp.tool(
    title="Ping",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Check connectivity to the QGIS plugin server. Returns pong if connected.",
    structured_output=True,
)
async def ping(ctx: Context) -> dict[str, Any]:
    return await _send("ping")


@mcp.tool(
    title="Diagnose",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Run diagnostic checks on the QGIS MCP stack. Reports QGIS version, "
    "plugin/server version match, processing providers, connected clients, and project status.",
    structured_output=True,
)
async def diagnose(ctx: Context) -> dict[str, Any]:
    """Check health of the full MCP ↔ QGIS chain."""
    await ctx.info("Running diagnostics...")
    result = await _send("diagnose")
    return enrich_diagnose(result)


@mcp.tool(
    title="Get QGIS Info",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get QGIS version, profile path, and plugin count.",
    structured_output=True,
)
async def get_qgis_info(ctx: Context) -> dict[str, Any]:
    return await _send("get_qgis_info")


@mcp.tool(
    title="Get Project Info",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get current project metadata: filename, title, CRS, layer count, and summary of layers.",
    structured_output=True,
)
async def get_project_info(ctx: Context) -> dict[str, Any]:
    return await _send("get_project_info")


# --- Project Management ---


@mcp.tool(title="Load Project", description="Load a QGIS project from a .qgs/.qgz file path.")
async def load_project(ctx: Context, path: str) -> list:
    await ctx.info(f"Loading project: {path}")
    result = await _send("load_project", {"path": path})
    return make_project_response(result)


@mcp.tool(
    title="Create New Project",
    description="Create a new empty QGIS project and save it to the given path.",
)
async def create_new_project(ctx: Context, path: str) -> list:
    result = await _send("create_new_project", {"path": path})
    return make_project_response(result)


@mcp.tool(
    title="Save Project",
    annotations=ToolAnnotations(idempotentHint=True),
    description="Save the current project. Optionally specify a new path.",
)
async def save_project(ctx: Context, path: str | None = None) -> dict:
    params = {}
    if path:
        params["path"] = path
    return await _send("save_project", params)


# --- Layer Management ---


@mcp.tool(
    title="Get Layers",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="List layers in the current project with IDs, names, types, visibility, and type-specific info. "
    "Use limit/offset for pagination. Response includes total_count.",
    structured_output=True,
)
async def get_layers(ctx: Context, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    return await _send("get_layers", {"limit": limit, "offset": offset})


@mcp.tool(
    title="Add Vector Layer",
    description="Add a vector layer (shapefile, GeoJSON, GeoPackage, etc.) to the project.",
)
async def add_vector_layer(
    ctx: Context, path: str, provider: str = "ogr", name: str | None = None
) -> list:
    params = {"path": path, "provider": provider}
    if name:
        params["name"] = name
    result = await _send("add_vector_layer", params)
    return make_layer_response(result)


@mcp.tool(
    title="Add Raster Layer", description="Add a raster layer (GeoTIFF, etc.) to the project."
)
async def add_raster_layer(
    ctx: Context, path: str, provider: str = "gdal", name: str | None = None
) -> list:
    params = {"path": path, "provider": provider}
    if name:
        params["name"] = name
    result = await _send("add_raster_layer", params)
    return make_layer_response(result)


@mcp.tool(
    title="Remove Layer",
    annotations=ToolAnnotations(destructiveHint=True),
    description="Remove a layer from the project by its layer ID. This is irreversible.",
)
async def remove_layer(ctx: Context, layer_id: str) -> dict:
    if not await _confirm_destructive(ctx, f"Remove layer {layer_id}? This cannot be undone."):
        return {"ok": False, "message": "Cancelled by user"}
    return await _send("remove_layer", {"layer_id": layer_id})


@mcp.tool(
    title="Find Layer",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Find layers by name pattern. Supports fnmatch wildcards (e.g. 'roads*') "
    "and substring matching.",
    structured_output=True,
)
async def find_layer(ctx: Context, name_pattern: str) -> dict[str, Any]:
    return await _send("find_layer", {"name_pattern": name_pattern})


@mcp.tool(
    title="Create Memory Layer",
    description="Create a new in-memory vector layer. geometry_type: Point, LineString, Polygon, "
    "MultiPoint, MultiLineString, MultiPolygon. fields: [{name, type}] where "
    "type is integer, double, string, date, datetime.",
)
async def create_memory_layer(
    ctx: Context,
    name: str,
    geometry_type: str,
    crs: str = "EPSG:4326",
    fields: list[dict] | None = None,
) -> list:
    params = {"name": name, "geometry_type": geometry_type, "crs": crs}
    if fields:
        params["fields"] = fields
    result = await _send("create_memory_layer", params)
    return make_layer_response(result, fallback_name=name)


# --- Layer Visibility & Navigation ---


@mcp.tool(
    title="Set Layer Visibility",
    annotations=ToolAnnotations(idempotentHint=True),
    description="Set a layer's visibility in the layer tree (show/hide on map).",
)
async def set_layer_visibility(ctx: Context, layer_id: str, visible: bool) -> dict:
    return await _send("set_layer_visibility", {"layer_id": layer_id, "visible": visible})


@mcp.tool(
    title="Zoom to Layer",
    annotations=ToolAnnotations(idempotentHint=True),
    description="Zoom the map canvas to the full extent of the specified layer.",
)
async def zoom_to_layer(ctx: Context, layer_id: str) -> dict:
    return await _send("zoom_to_layer", {"layer_id": layer_id})


# --- Feature Access ---


@mcp.tool(
    title="Get Layer Features",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get features from a vector layer. Flat dicts: _fid + attributes at top level. "
    "expression filter (QGIS, e.g. "
    '"name = \'Berlin\'", "population > 1000000"), limit (max 50, default 10), offset for paging, '
    "optional geometry in _geometry key.",
    structured_output=True,
)
async def get_layer_features(
    ctx: Context,
    layer_id: str,
    limit: int = 10,
    offset: int = 0,
    expression: str | None = None,
    include_geometry: bool = False,
) -> dict[str, Any]:
    if limit > 50:
        limit = 50
    params = {
        "layer_id": layer_id,
        "limit": limit,
        "offset": offset,
        "include_geometry": include_geometry,
    }
    if expression:
        params["expression"] = expression
    result = await _send("get_layer_features", params)

    # Large Results to Resources (Task 9)
    if limit > 20 and "features" in result:
        uri = _cache_as_resource(result["features"], f"{layer_id}_features")
        result["features_resource"] = uri
        result["_hint"] = f"Result contains many features. You can also access them via {uri}"

    return result


@mcp.tool(
    title="Get Field Statistics",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Compute aggregate statistics (count, sum, mean, min, max, stdev) for a numeric field. "
    "For non-numeric fields returns count and distinct values.",
    structured_output=True,
)
async def get_field_statistics(ctx: Context, layer_id: str, field_name: str) -> dict[str, Any]:
    return await _send("get_field_statistics", {"layer_id": layer_id, "field_name": field_name})


# --- Feature Editing ---


@mcp.tool(
    title="Add Features",
    annotations=ToolAnnotations(destructiveHint=True),
    description="Add features to a vector layer. Each feature: {attributes: {field: value}, "
    "geometry_wkt: 'POINT(1 2)'}. Returns count of added features.",
)
async def add_features(ctx: Context, layer_id: str, features: list[dict]) -> dict:
    return await _send("add_features", {"layer_id": layer_id, "features": features})


@mcp.tool(
    title="Update Features",
    annotations=ToolAnnotations(destructiveHint=True),
    description="Update feature attributes. updates: [{fid: 1, attributes: {field: value}}]. "
    "Returns count of updated features.",
)
async def update_features(ctx: Context, layer_id: str, updates: list[dict]) -> dict:
    return await _send("update_features", {"layer_id": layer_id, "updates": updates})


@mcp.tool(
    title="Delete Features",
    annotations=ToolAnnotations(destructiveHint=True),
    description="Delete features by feature IDs or expression filter. "
    "Provide either fids (list of ints) or expression (string), not both.",
)
async def delete_features(
    ctx: Context,
    layer_id: str,
    fids: list[int] | None = None,
    expression: str | None = None,
) -> dict:
    target = f"fids={fids}" if fids else f"expression='{expression}'"
    if not await _confirm_destructive(ctx, f"Delete features from layer {layer_id} ({target})?"):
        return {"ok": False, "message": "Cancelled by user"}
    params = {"layer_id": layer_id}
    if fids is not None:
        params["fids"] = fids
    if expression:
        params["expression"] = expression
    return await _send("delete_features", params)


# --- Selection ---


@mcp.tool(
    title="Select Features",
    annotations=ToolAnnotations(idempotentHint=True),
    description="Select features in a layer by expression or feature IDs.",
)
async def select_features(
    ctx: Context,
    layer_id: str,
    expression: str | None = None,
    fids: list[int] | None = None,
) -> dict:
    params = {"layer_id": layer_id}
    if expression:
        params["expression"] = expression
    if fids is not None:
        params["fids"] = fids
    return await _send("select_features", params)


@mcp.tool(
    title="Get Selection",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get the current selection for a layer. Returns feature IDs and count.",
    structured_output=True,
)
async def get_selection(ctx: Context, layer_id: str) -> dict[str, Any]:
    return await _send("get_selection", {"layer_id": layer_id})


@mcp.tool(
    title="Clear Selection",
    annotations=ToolAnnotations(idempotentHint=True),
    description="Clear the selection on a layer.",
)
async def clear_selection(ctx: Context, layer_id: str) -> dict:
    return await _send("clear_selection", {"layer_id": layer_id})


# --- Symbology ---


@mcp.tool(
    title="Set Layer Style",
    description="Set symbology. style_type: 'single' (one symbol), 'categorized' (unique values), "
    "'graduated' (numeric ranges). field required for categorized/graduated. "
    "color_ramp: QGIS ramp name (e.g. 'Spectral', 'Viridis', 'Blues'). "
    "classes: graduated class count (default 5).",
)
async def set_layer_style(
    ctx: Context,
    layer_id: str,
    style_type: str,
    field: str | None = None,
    classes: int = 5,
    color_ramp: str = "Spectral",
) -> dict:
    params = {
        "layer_id": layer_id,
        "style_type": style_type,
        "classes": classes,
        "color_ramp": color_ramp,
    }
    if field:
        params["field"] = field
    return await _send("set_layer_style", params)


# --- Canvas ---


@mcp.tool(
    title="Get Canvas Extent",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get the current map canvas extent and CRS.",
    structured_output=True,
)
async def get_canvas_extent(ctx: Context) -> dict[str, Any]:
    return await _send("get_canvas_extent")


@mcp.tool(
    title="Set Canvas Extent",
    annotations=ToolAnnotations(idempotentHint=True),
    description="Set the map canvas extent. Coordinates should be in the specified CRS (default: project CRS).",
)
async def set_canvas_extent(
    ctx: Context, xmin: float, ymin: float, xmax: float, ymax: float, crs: str | None = None
) -> dict:
    params = {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}
    if crs:
        params["crs"] = crs
    return await _send("set_canvas_extent", params)


@mcp.tool(
    title="Get Canvas Screenshot",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Grab a fast screenshot of the current map canvas widget (no re-render). "
    "Returns the image inline. Much faster than render_map.",
)
async def get_canvas_screenshot(ctx: Context) -> list:
    result = await _send("get_canvas_screenshot")
    return [
        ImageContent(
            type="image",
            data=result["base64_data"],
            mimeType="image/png",
            annotations=Annotations(audience=["user", "assistant"], priority=1.0),
        )
    ]


@mcp.tool(
    title="Get 3D Screenshot",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Capture an OPEN 3D Map View as an inline image. Reuses the open view's "
    "scene + camera, rendered via a print-layout 3D map item (the 2D get_canvas_screenshot "
    "cannot capture the OpenGL 3D view). Requires a 3D map view to be open in QGIS "
    "(View > 3D Map Views > New 3D Map View). view_index selects which view when several "
    "are open; dpi controls output resolution. Optional camera overrides (applied to the "
    "capture only, leaving the live view unchanged): pitch (0 = straight down/top-down, "
    "90 = horizontal/edge-on; ~45 = balanced oblique), heading (compass degrees), "
    "distance (metres).",
)
async def get_3d_screenshot(
    ctx: Context,
    view_index: int = 0,
    dpi: int = 96,
    pitch: float | None = None,
    distance: float | None = None,
    heading: float | None = None,
) -> list:
    params: dict[str, Any] = {"view_index": view_index, "dpi": dpi}
    if pitch is not None:
        params["pitch"] = pitch
    if distance is not None:
        params["distance"] = distance
    if heading is not None:
        params["heading"] = heading
    result = await _send("get_3d_screenshot", params)
    return [
        ImageContent(
            type="image",
            data=result["base64_data"],
            mimeType="image/png",
            annotations=Annotations(audience=["user", "assistant"], priority=1.0),
        )
    ]


# --- Raster ---


@mcp.tool(
    title="Get Raster Info",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get raster layer info: band count, dimensions, CRS, extent, per-band statistics, nodata values.",
    structured_output=True,
)
async def get_raster_info(ctx: Context, layer_id: str) -> dict[str, Any]:
    return await _send("get_raster_info", {"layer_id": layer_id})


# --- Processing ---


@mcp.tool(
    title="Execute Processing",
    description="Execute a QGIS Processing algorithm. Use get_algorithm_help to discover parameters. "
    "Layer params accept layer IDs or file paths. Set OUTPUT to 'memory:' for temp layers.",
)
async def execute_processing(ctx: Context, algorithm: str, parameters: dict) -> dict:
    await ctx.info(f"Running algorithm: {algorithm}")
    await ctx.report_progress(0, 100)
    result = await _send(
        "execute_processing", {"algorithm": algorithm, "parameters": parameters}, timeout=TIMEOUT_LONG
    )
    await ctx.report_progress(100, 100)
    return result


@mcp.tool(
    title="List Processing Algorithms",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Search for processing algorithms by keyword and/or provider. "
    "Returns id, name, provider for each match.",
    structured_output=True,
)
async def list_processing_algorithms(
    ctx: Context,
    search: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    params = {}
    if search:
        params["search"] = search
    if provider:
        params["provider"] = provider
    return await _send("list_processing_algorithms", params)


@mcp.tool(
    title="Get Algorithm Help",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get detailed help for a processing algorithm: parameters (name, type, optional, default), "
    "outputs, and description.",
    structured_output=True,
)
async def get_algorithm_help(ctx: Context, algorithm_id: str) -> dict[str, Any]:
    return await _send("get_algorithm_help", {"algorithm_id": algorithm_id})


@mcp.tool(
    title="Create Processing Model",
    description=(
        "Build QGIS Processing Model (.model3) from structured spec; save to user models folder, "
        "register in Processing Toolbox. Only call needed: algorithm discovery + param validation "
        "run in the plugin against the live registry, so do NOT call list_processing_algorithms "
        "or get_algorithm_help first. Pass keyword ('buffer') or full id ('native:buffer'); "
        "handler resolves it. Ambiguous hint returns candidate list to refine and retry. Bad "
        "param/output names reported with the valid set.\n\n"
        "Spec:\n"
        "  inputs: [{name, type, description?, default?, optional?, parent_layer? (for field/distance), "
        "options? (for enum)}]. Types: vector, feature_source, raster, field, number, integer, "
        "distance, string, boolean, extent, crs, point, file, folder, enum, multiple_layers.\n"
        "  steps: [{id, algorithm, description?, parameters: {ALG_PARAM: value}}]. algorithm = "
        "keyword or full id. Param values:\n"
        "    '@input_name'     = model input value\n"
        "    '$step_id.OUTPUT' = earlier step output\n"
        "    '=expression'     = QGIS expression at run time\n"
        "    else              = static literal (number/bool/string/list)\n"
        "  outputs: [{name, from_step, from_output, description?}] = exposed outputs; omit to "
        "expose the last step OUTPUT as 'Result'.\n\n"
        "Name collision appends a suffix (name_2.model3, ...). Response returns the actual "
        "'name', the 'requested_name', and 'resolved_steps' (which algorithm each hint mapped to)."
    ),
    structured_output=True,
)
async def create_processing_model(
    ctx: Context,
    name: str,
    steps: list[dict],
    inputs: list[dict] | None = None,
    outputs: list[dict] | None = None,
    description: str = "",
    group: str = "Models",
) -> dict[str, Any]:
    await ctx.info(f"Building Processing model: {name} ({len(steps)} step(s))")
    params: dict[str, Any] = {
        "name": name,
        "steps": steps,
        "description": description,
        "group": group,
    }
    if inputs is not None:
        params["inputs"] = inputs
    if outputs is not None:
        params["outputs"] = outputs
    return await _send("create_processing_model", params, timeout=TIMEOUT_LONG)


@mcp.tool(
    title="List Processing Models",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="List registered Processing models (the 'model' provider). "
    "Returns id, name, group for each. Use run_model to execute one.",
    structured_output=True,
)
async def list_processing_models(ctx: Context) -> dict[str, Any]:
    return await _send("list_processing_models")


@mcp.tool(
    title="Run Model",
    description="Run a Processing model by registered id (e.g. 'model:myflow') or by a "
    ".model3 file path. 'parameters' maps the model's input names to values "
    "(layer ids/paths, numbers, etc.).",
)
async def run_model(ctx: Context, model: str, parameters: dict | None = None) -> dict:
    await ctx.info(f"Running model: {model}")
    await ctx.report_progress(0, 100)
    result = await _send(
        "run_model", {"model": model, "parameters": parameters or {}}, timeout=TIMEOUT_LONG
    )
    await ctx.report_progress(100, 100)
    return result


@mcp.tool(
    title="Get Processing Providers",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="List Processing providers (native, gdal, grass, saga, model, ...) with "
    "algorithm counts and active status. Use to diagnose missing algorithms.",
    structured_output=True,
)
async def get_processing_providers(ctx: Context) -> dict[str, Any]:
    return await _send("get_processing_providers")


@mcp.tool(
    title="Execute Processing Batch",
    description="Run one algorithm once per parameter dict in 'parameters_list'. "
    "Returns a per-run result with index and success/error status. Use for applying "
    "the same operation over many inputs in a single round-trip.",
)
async def execute_processing_batch(
    ctx: Context, algorithm: str, parameters_list: list[dict]
) -> dict:
    await ctx.info(f"Batch processing {algorithm}: {len(parameters_list)} run(s)")
    return await _send(
        "execute_processing_batch",
        {"algorithm": algorithm, "parameters_list": parameters_list},
        timeout=TIMEOUT_LONG,
    )


# --- Raster compute ---


@mcp.tool(
    title="Raster Calculator",
    description="Band math via the QGIS raster calculator. Reference loaded raster layers "
    "in the expression as 'LayerName@band' (e.g. '(\"dem@1\" > 1000) * 1'). Writes a GeoTIFF "
    "to output_path. Output grid/extent taken from reference_layer (layer id or name), "
    "defaulting to the first loaded raster.",
)
async def raster_calculator(
    ctx: Context, expression: str, output_path: str, reference_layer: str | None = None
) -> dict:
    await ctx.info("Computing raster expression...")
    return await _send(
        "raster_calculator",
        {"expression": expression, "output_path": output_path, "reference_layer": reference_layer},
        timeout=TIMEOUT_LONG,
    )


@mcp.tool(
    title="Zonal Statistics",
    description="Per-polygon stats from a raster (native:zonalstatisticsfb). "
    "stats int codes: 0=count 1=sum 2=mean 3=median 4=stdev 5=min 6=max "
    "7=range 8=minority 9=majority 10=variety 11=variance (default [0,1,2]). New columns "
    "prefixed by 'prefix'. No output_path = in-memory layer.",
)
async def zonal_statistics(
    ctx: Context,
    polygon_layer: str,
    raster_layer: str,
    band: int = 1,
    prefix: str = "_",
    stats: list[int] | None = None,
    output_path: str | None = None,
) -> dict:
    await ctx.info("Computing zonal statistics...")
    return await _send(
        "zonal_statistics",
        {
            "polygon_layer": polygon_layer,
            "raster_layer": raster_layer,
            "band": band,
            "prefix": prefix,
            "stats": stats,
            "output_path": output_path,
        },
        timeout=TIMEOUT_LONG,
    )


@mcp.tool(
    title="Sample Raster Values",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Sample raster pixel values at points. 'points' is a list of [x, y] in the "
    "raster's CRS. Omit 'band' to sample all bands. Use transform_coordinates first if your "
    "points are in a different CRS.",
)
async def sample_raster_values(
    ctx: Context, raster_layer: str, points: list[list[float]], band: int | None = None
) -> dict[str, Any]:
    return await _send(
        "sample_raster_values",
        {"raster_layer": raster_layer, "points": points, "band": band},
    )


# --- Export ---


@mcp.tool(
    title="Export Layer",
    annotations=ToolAnnotations(idempotentHint=True),
    description="Export vector/raster to disk; format from output_path extension "
    "(.gpkg, .shp, .geojson, .tif, ...). target_crs (e.g. 'EPSG:4326') reprojects on export. "
    "filter_expression (vector only) exports a subset matching a QGIS expression.",
)
async def export_layer(
    ctx: Context,
    layer_id: str,
    output_path: str,
    target_crs: str | None = None,
    filter_expression: str | None = None,
) -> dict:
    await ctx.info(f"Exporting layer to {output_path}")
    return await _send(
        "export_layer",
        {
            "layer_id": layer_id,
            "output_path": output_path,
            "target_crs": target_crs,
            "filter_expression": filter_expression,
        },
        timeout=TIMEOUT_LONG,
    )


# --- Vector analysis ---


@mcp.tool(
    title="Field Calculator",
    description="Add (if missing) + populate a field from a QGIS expression, per feature, in-place. "
    "field_type: string|int|double|bool|date|datetime (default double). "
    "Example: expression='$area', field_name='area_m2'. Returns updated feature count.",
)
async def field_calculator(
    ctx: Context,
    layer_id: str,
    field_name: str,
    expression: str,
    field_type: str = "double",
    length: int = 0,
    precision: int = 0,
) -> dict:
    return await _send(
        "field_calculator",
        {
            "layer_id": layer_id,
            "field_name": field_name,
            "expression": expression,
            "field_type": field_type,
            "length": length,
            "precision": precision,
        },
    )


@mcp.tool(
    title="Get Unique Values",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Return the distinct values of a field. Use 'limit' to cap results "
    "(-1 for all). Useful before building categorized symbology or filters.",
)
async def get_unique_values(
    ctx: Context, layer_id: str, field: str, limit: int = 1000
) -> dict[str, Any]:
    return await _send(
        "get_unique_values", {"layer_id": layer_id, "field": field, "limit": limit}
    )


@mcp.tool(
    title="Spatial Join",
    description="Join attributes by location (native:joinattributesbylocation). "
    "predicates int list: 0=intersects 1=contains 2=equals 3=touches 4=overlaps "
    "5=within 6=crosses (default [0]). method: 0=one-to-many 1=first match (default) "
    "2=largest overlap. join_fields = copied columns (default all). "
    "No output_path = in-memory layer.",
)
async def spatial_join(
    ctx: Context,
    target_layer: str,
    join_layer: str,
    predicates: list[int] | None = None,
    join_fields: list[str] | None = None,
    method: int = 1,
    prefix: str = "",
    output_path: str | None = None,
) -> dict:
    await ctx.info("Joining attributes by location...")
    return await _send(
        "spatial_join",
        {
            "target_layer": target_layer,
            "join_layer": join_layer,
            "predicates": predicates,
            "join_fields": join_fields,
            "method": method,
            "prefix": prefix,
            "output_path": output_path,
        },
        timeout=TIMEOUT_LONG,
    )


# --- Rendering ---


@mcp.tool(
    title="Render Map",
    annotations=ToolAnnotations(idempotentHint=True),
    description="Render the current map canvas to an image. Returns the image inline so you can see it. "
    "Optionally saves to a file path on disk.",
)
async def render_map(
    ctx: Context, width: int = 800, height: int = 600, path: str | None = None
) -> list:
    await ctx.info("Rendering map...")
    await ctx.report_progress(0, 100)
    params = {"width": width, "height": height}
    if path:
        params["path"] = path
    result = await _send("render_map_base64", params, timeout=TIMEOUT_LONG)
    await ctx.report_progress(100, 100)

    return make_render_response(result, width, height, path)


# --- Code Execution ---


@mcp.tool(
    title="Execute Code",
    annotations=ToolAnnotations(destructiveHint=True),
    description="Execute arbitrary PyQGIS code. Use for operations not covered by other tools. "
    "Has access to QgsProject, iface, and core QGIS classes. Returns stdout/stderr.",
)
async def execute_code(ctx: Context, code: str) -> dict:
    if not await _confirm_destructive(
        ctx, "Execute arbitrary PyQGIS code? This can modify your project and system."
    ):
        return {"ok": False, "message": "Cancelled by user"}
    await ctx.info("Executing PyQGIS code...")
    await ctx.report_progress(0, 100)
    result = await _send("execute_code", {"code": code}, timeout=TIMEOUT_LONG)
    await ctx.report_progress(100, 100)
    return result


# --- High-Value Capabilities ---


@mcp.tool(
    title="Get Active Layer",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get the currently active (selected) layer in the QGIS layer panel.",
    structured_output=True,
)
async def get_active_layer(ctx: Context) -> dict[str, Any]:
    return await _send("get_active_layer")


@mcp.tool(
    title="Set Active Layer",
    annotations=ToolAnnotations(idempotentHint=True),
    description="Set the active layer in the QGIS layer panel by layer ID.",
)
async def set_active_layer(ctx: Context, layer_id: str) -> dict:
    return await _send("set_active_layer", {"layer_id": layer_id})


@mcp.tool(
    title="Get Canvas Scale",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get the current map canvas scale, rotation, and magnification factor.",
    structured_output=True,
)
async def get_canvas_scale(ctx: Context) -> dict[str, Any]:
    return await _send("get_canvas_scale")


@mcp.tool(
    title="Set Canvas Scale",
    annotations=ToolAnnotations(idempotentHint=True),
    description="Set the map canvas scale and/or rotation. Provide scale as denominator "
    "(e.g. 50000 for 1:50000). Rotation in degrees (0-360).",
)
async def set_canvas_scale(
    ctx: Context, scale: float | None = None, rotation: float | None = None
) -> dict:
    params: dict[str, Any] = {}
    if scale is not None:
        params["scale"] = scale
    if rotation is not None:
        params["rotation"] = rotation
    return await _send("set_canvas_scale", params)


@mcp.tool(
    title="Get Layer Labeling",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get the labeling configuration of a vector layer: enabled state, field, font size, color.",
    structured_output=True,
)
async def get_layer_labeling(ctx: Context, layer_id: str) -> dict[str, Any]:
    return await _send("get_layer_labeling", {"layer_id": layer_id})


@mcp.tool(
    title="Set Layer Labeling",
    description="Configure labeling for a vector layer. Set enabled=false to disable labels. "
    "Set field_name to the attribute field to label with. Optional: font_size (float), "
    "color (hex like '#000000').",
)
async def set_layer_labeling(
    ctx: Context,
    layer_id: str,
    enabled: bool = True,
    field_name: str | None = None,
    font_size: float | None = None,
    color: str | None = None,
) -> dict:
    params: dict[str, Any] = {"layer_id": layer_id, "enabled": enabled}
    if field_name is not None:
        params["field_name"] = field_name
    if font_size is not None:
        params["font_size"] = font_size
    if color is not None:
        params["color"] = color
    return await _send("set_layer_labeling", params)


@mcp.tool(
    title="Get Layer CRS",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get the coordinate reference system (CRS) of a layer: EPSG code, description, "
    "whether geographic, and PROJ4 string.",
    structured_output=True,
)
async def get_layer_crs(ctx: Context, layer_id: str) -> dict[str, Any]:
    return await _send("get_layer_crs", {"layer_id": layer_id})


@mcp.tool(
    title="Set Layer CRS",
    description="Set the CRS of a layer (e.g. 'EPSG:4326'). This does NOT reproject data — "
    "it only changes how the layer's coordinates are interpreted.",
)
async def set_layer_crs(ctx: Context, layer_id: str, crs: str) -> dict:
    return await _send("set_layer_crs", {"layer_id": layer_id, "crs": crs})


@mcp.tool(
    title="Get Bookmarks",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get spatial bookmarks from the project. Each bookmark has a name, group, "
    "extent (xmin/ymin/xmax/ymax), and CRS.",
    structured_output=True,
)
async def get_bookmarks(ctx: Context) -> dict[str, Any]:
    return await _send("get_bookmarks")


@mcp.tool(
    title="Add Bookmark",
    description="Add a spatial bookmark to the project for quick navigation. "
    "Provide a name and extent (xmin/ymin/xmax/ymax) with CRS.",
)
async def add_bookmark(
    ctx: Context,
    name: str,
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    crs: str = "EPSG:4326",
    group: str = "",
) -> dict:
    return await _send(
        "add_bookmark",
        {
            "name": name,
            "xmin": xmin,
            "ymin": ymin,
            "xmax": xmax,
            "ymax": ymax,
            "crs": crs,
            "group": group,
        },
    )


@mcp.tool(
    title="Remove Bookmark",
    annotations=ToolAnnotations(destructiveHint=True),
    description="Remove a spatial bookmark by its ID.",
)
async def remove_bookmark(ctx: Context, bookmark_id: str) -> dict:
    return await _send("remove_bookmark", {"bookmark_id": bookmark_id})


@mcp.tool(
    title="Get Map Themes",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get map themes (visibility presets). Each theme stores which layers are visible.",
    structured_output=True,
)
async def get_map_themes(ctx: Context) -> dict[str, Any]:
    return await _send("get_map_themes")


@mcp.tool(
    title="Add Map Theme",
    description="Create a map theme from the current layer visibility state. "
    "If a theme with this name exists, it will be updated.",
)
async def add_map_theme(ctx: Context, name: str) -> dict:
    return await _send("add_map_theme", {"name": name})


@mcp.tool(
    title="Remove Map Theme",
    annotations=ToolAnnotations(destructiveHint=True),
    description="Remove a map theme by name.",
)
async def remove_map_theme(ctx: Context, name: str) -> dict:
    return await _send("remove_map_theme", {"name": name})


@mcp.tool(
    title="Apply Map Theme",
    annotations=ToolAnnotations(idempotentHint=True),
    description="Apply a map theme — restores the layer visibility state saved in the theme.",
)
async def apply_map_theme(ctx: Context, name: str) -> dict:
    return await _send("apply_map_theme", {"name": name})


@mcp.tool(
    title="Set Project CRS",
    description="Set the project coordinate reference system (e.g. 'EPSG:4326', 'EPSG:3857'). "
    "This changes how layers are projected on the map canvas.",
)
async def set_project_crs(ctx: Context, crs: str) -> list:
    result = await _send("set_project_crs", {"crs": crs})
    return make_project_response(result)


# --- Batch ---


@mcp.tool(
    title="Batch Commands",
    description="Execute multiple commands in a single round-trip. Each command is "
    '{"type": "<command_name>", "params": {...}}. Destructive commands '
    "(execute_code, remove_layer, delete_features, set_setting, reload_plugin) "
    "are not allowed in batch — use them individually.",
)
async def batch_commands(ctx: Context, commands: list[dict]) -> dict:
    for cmd in commands:
        cmd_type = cmd.get("type", "")
        if cmd_type in BATCH_BLOCKED_COMMANDS:
            raise ValueError(
                f"Command {cmd_type!r} is not allowed in batch — "
                "call it individually so confirmation can be requested"
            )
    return await _send("batch", {"commands": commands}, timeout=TIMEOUT_LONG)


# --- Print Layouts ---


@mcp.tool(
    title="List Layouts",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="List all print layouts in the current project with names and page counts.",
    structured_output=True,
)
async def list_layouts(ctx: Context) -> dict[str, Any]:
    return await _send("list_layouts")


@mcp.tool(
    title="Export Layout",
    annotations=ToolAnnotations(idempotentHint=True),
    description="Export a print layout to file. format: 'pdf', 'png', 'jpg', 'svg'. "
    "dpi: resolution (default 300).",
)
async def export_layout(
    ctx: Context,
    layout_name: str,
    path: str,
    format: str = "pdf",
    dpi: int = 300,
) -> dict:
    return await _send(
        "export_layout",
        {
            "layout_name": layout_name,
            "path": path,
            "format": format,
            "dpi": dpi,
        },
    )


# --- Message Log & Debugging ---


@mcp.tool(
    title="Get Message Log",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get QGIS message log entries. Filter by level ('info', 'warning', 'critical') "
    "and/or tag (e.g. 'QGIS MCP'). Returns newest first.",
    structured_output=True,
)
async def get_message_log(
    ctx: Context, level: str | None = None, tag: str | None = None, limit: int = 100
) -> dict[str, Any]:
    params = {"limit": limit}
    if level:
        params["level"] = level
    if tag:
        params["tag"] = tag
    return await _send("get_message_log", params)


# --- Plugin Management ---


@mcp.tool(
    title="List Plugins",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="List installed QGIS plugins with name, enabled status, and version. "
    "Set enabled_only=true to list only active plugins.",
    structured_output=True,
)
async def list_plugins(ctx: Context, enabled_only: bool = False) -> dict[str, Any]:
    return await _send("list_plugins", {"enabled_only": enabled_only})


@mcp.tool(
    title="Get Plugin Info",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get detailed info for a specific plugin: name, enabled, version, description, author, path.",
    structured_output=True,
)
async def get_plugin_info(ctx: Context, plugin_name: str) -> dict[str, Any]:
    return await _send("get_plugin_info", {"plugin_name": plugin_name})


@mcp.tool(
    title="Reload Plugin",
    annotations=ToolAnnotations(destructiveHint=True),
    description="Reload a QGIS plugin by name. Cannot reload the MCP plugin itself. "
    "Useful during plugin development.",
)
async def reload_plugin(ctx: Context, plugin_name: str) -> dict:
    await ctx.info(f"Reloading plugin: {plugin_name}")
    return await _send("reload_plugin", {"plugin_name": plugin_name})


# --- Layer Tree ---


@mcp.tool(
    title="Get Layer Tree",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get the full layer tree structure with groups and layers. "
    "Returns recursive tree with type, name, visibility, and children.",
    structured_output=True,
)
async def get_layer_tree(ctx: Context) -> dict[str, Any]:
    return await _send("get_layer_tree")


@mcp.tool(
    title="Create Layer Group",
    description="Create a new layer group in the layer tree. "
    "Optionally specify a parent group name.",
)
async def create_layer_group(ctx: Context, name: str, parent: str | None = None) -> dict:
    params = {"name": name}
    if parent:
        params["parent"] = parent
    return await _send("create_layer_group", params)


@mcp.tool(title="Move Layer to Group", description="Move a layer into a layer group by group name.")
async def move_layer_to_group(ctx: Context, layer_id: str, group_name: str) -> dict:
    return await _send("move_layer_to_group", {"layer_id": layer_id, "group_name": group_name})


# --- Layer Properties ---


@mcp.tool(
    title="Set Layer Property",
    annotations=ToolAnnotations(idempotentHint=True),
    description="Set a layer property. Supported properties: opacity (0.0-1.0), name (string), "
    "min_scale, max_scale (float), scale_visibility (bool).",
)
async def set_layer_property(ctx: Context, layer_id: str, property: str, value: str) -> dict:
    return await _send(
        "set_layer_property", {"layer_id": layer_id, "property": property, "value": value}
    )


@mcp.tool(
    title="Get Layer Extent",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get the spatial extent (bounding box) and CRS of a layer.",
    structured_output=True,
)
async def get_layer_extent(ctx: Context, layer_id: str) -> dict[str, Any]:
    return await _send("get_layer_extent", {"layer_id": layer_id})


# --- Project Variables ---


@mcp.tool(
    title="Get Project Variables",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Get all project-level variables (key-value pairs set in Project Properties).",
    structured_output=True,
)
async def get_project_variables(ctx: Context) -> dict[str, Any]:
    return await _send("get_project_variables")


@mcp.tool(
    title="Set Project Variable",
    annotations=ToolAnnotations(idempotentHint=True),
    description="Set a project-level variable. Variables are accessible in expressions as @key.",
)
async def set_project_variable(ctx: Context, key: str, value: str) -> dict:
    return await _send("set_project_variable", {"key": key, "value": value})


# --- Expression Validation ---


@mcp.tool(
    title="Validate Expression",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Validate a QGIS expression. Returns whether it's valid, any parse errors, "
    "and referenced column names. Optionally test against a layer's fields.",
    structured_output=True,
)
async def validate_expression(
    ctx: Context, expression: str, layer_id: str | None = None
) -> dict[str, Any]:
    params = {"expression": expression}
    if layer_id:
        params["layer_id"] = layer_id
    return await _send("validate_expression", params)


# --- Settings ---


@mcp.tool(
    title="Get Setting",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Read a QGIS setting by key path (e.g. 'qgis/sketching/sketching_enabled').",
    structured_output=True,
)
async def get_setting(ctx: Context, key: str) -> dict[str, Any]:
    return await _send("get_setting", {"key": key})


@mcp.tool(
    title="Set Setting",
    annotations=ToolAnnotations(destructiveHint=True),
    description="Write a QGIS setting. Use with care — incorrect settings can affect QGIS behavior.",
)
async def set_setting(ctx: Context, key: str, value: str) -> dict:
    if not await _confirm_destructive(
        ctx, f"Set QGIS setting '{key}'? Incorrect settings can affect behavior."
    ):
        return {"ok": False, "message": "Cancelled by user"}
    return await _send("set_setting", {"key": key, "value": value})


# --- CRS Transformation ---


@mcp.tool(
    title="Transform Coordinates",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Transform coordinates between CRS. Accepts a point {x, y}, "
    "a point list [{x, y}, ...], or a bbox {xmin, ymin, xmax, ymax}. "
    "Returns the same format.",
    structured_output=True,
)
async def transform_coordinates(
    ctx: Context,
    source_crs: str,
    target_crs: str,
    point: dict | None = None,
    points: list[dict] | None = None,
    bbox: dict | None = None,
) -> dict[str, Any]:
    params = {"source_crs": source_crs, "target_crs": target_crs}
    if point:
        params["point"] = point
    if points:
        params["points"] = points
    if bbox:
        params["bbox"] = bbox
    return await _send("transform_coordinates", params)


@mcp.tool(
    title="Add Web Layer",
    description="Add a web layer (XYZ, WMS, WFS) to the project. service: 'xyz', 'wms', 'wfs'.",
)
async def add_web_layer(
    ctx: Context, url: str, service: str, name: str | None = None, crs: str = "EPSG:3857"
) -> list:
    params = {"url": url, "service": service, "crs": crs}
    if name:
        params["name"] = name
    result = await _send("add_web_layer", params)
    return make_layer_response(result)


@mcp.tool(
    title="Add Table Join",
    description="Add a table join to a vector layer.",
)
async def add_table_join(
    ctx: Context,
    target_layer_id: str,
    join_layer_id: str,
    target_field: str,
    join_field: str,
    prefix: str = "",
) -> dict:
    params = {
        "target_layer_id": target_layer_id,
        "join_layer_id": join_layer_id,
        "target_field": target_field,
        "join_field": join_field,
        "prefix": prefix,
    }
    return await _send("add_table_join", params)


@mcp.tool(
    title="Add Field",
    description="Add a new field to a vector layer. field_type: 'string', 'int', 'double', 'bool', 'date', 'datetime'.",
)
async def add_field(
    ctx: Context,
    layer_id: str,
    field_name: str,
    field_type: str,
    length: int | None = None,
    precision: int | None = None,
) -> dict:
    params = {
        "layer_id": layer_id,
        "field_name": field_name,
        "field_type": field_type,
    }
    if length is not None:
        params["length"] = length
    if precision is not None:
        params["precision"] = precision
    return await _send("add_field", params)


@mcp.tool(
    title="Delete Field",
    annotations=ToolAnnotations(destructiveHint=True),
    description="Delete a field from a vector layer.",
)
async def delete_field(ctx: Context, layer_id: str, field_name: str) -> dict:
    if not await _confirm_destructive(ctx, f"Delete field '{field_name}' from layer {layer_id}?"):
        return {"ok": False, "message": "Cancelled by user"}
    return await _send("delete_field", {"layer_id": layer_id, "field_name": field_name})


@mcp.tool(
    title="Rename Field",
    description="Rename a field in a vector layer.",
)
async def rename_field(ctx: Context, layer_id: str, old_name: str, new_name: str) -> dict:
    return await _send(
        "rename_field", {"layer_id": layer_id, "old_name": old_name, "new_name": new_name}
    )


@mcp.tool(
    title="Apply Style QML",
    description="Apply a QGIS QML style file to a layer.",
)
async def apply_style_qml(ctx: Context, layer_id: str, path: str) -> dict:
    return await _send("apply_style_qml", {"layer_id": layer_id, "path": path})


@mcp.tool(
    title="Save Style QML",
    description="Save a layer's style to a QGIS QML file.",
)
async def save_style_qml(ctx: Context, layer_id: str, path: str) -> dict:
    return await _send("save_style_qml", {"layer_id": layer_id, "path": path})


@mcp.tool(
    title="Create Layout",
    description="Create a new print layout.",
)
async def create_layout(ctx: Context, name: str) -> dict:
    return await _send("create_layout", {"name": name})


@mcp.tool(
    title="Add Layout Map",
    description="Add a map item to a print layout at specified position and size (in millimeters).",
)
async def add_layout_map(
    ctx: Context, layout_name: str, x: float, y: float, width: float, height: float
) -> dict:
    return await _send(
        "add_layout_map",
        {"layout_name": layout_name, "x": x, "y": y, "width": width, "height": height},
    )


@mcp.tool(
    title="Get Layout Info",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="List items in a print layout (type, id, uuid, position, size) and page count.",
    structured_output=True,
)
async def get_layout_info(ctx: Context, layout_name: str) -> dict[str, Any]:
    return await _send("get_layout_info", {"layout_name": layout_name})


@mcp.tool(
    title="Add Layout Label",
    description="Add a text label to a print layout (mm). text may contain [% expression %] "
    "for dynamic content. color is hex (e.g. '#000000').",
)
async def add_layout_label(
    ctx: Context,
    layout_name: str,
    text: str,
    x: float = 10,
    y: float = 10,
    width: float = 100,
    height: float = 20,
    font_size: int = 12,
    color: str = "#000000",
) -> dict:
    return await _send(
        "add_layout_label",
        {
            "layout_name": layout_name,
            "text": text,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "font_size": font_size,
            "color": color,
        },
    )


@mcp.tool(
    title="Add Layout Legend",
    description="Add a legend to a print layout, linked to a map item (defaults to the first "
    "map item). Position/size in mm.",
)
async def add_layout_legend(
    ctx: Context,
    layout_name: str,
    map_item_id: str | None = None,
    x: float = 10,
    y: float = 10,
    width: float = 80,
    height: float = 100,
    title: str = "Legend",
) -> dict:
    return await _send(
        "add_layout_legend",
        {
            "layout_name": layout_name,
            "map_item_id": map_item_id,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "title": title,
        },
    )


@mcp.tool(
    title="Add Layout Scale Bar",
    description="Add a scale bar to a print layout, linked to a map item. style e.g. "
    "'Single Box', 'Double Box', 'Line Ticks Up', 'Numeric'.",
)
async def add_layout_scalebar(
    ctx: Context,
    layout_name: str,
    map_item_id: str | None = None,
    x: float = 10,
    y: float = 180,
    width: float = 80,
    height: float = 20,
    style: str = "Single Box",
) -> dict:
    return await _send(
        "add_layout_scalebar",
        {
            "layout_name": layout_name,
            "map_item_id": map_item_id,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "style": style,
        },
    )


@mcp.tool(
    title="Add Layout Picture",
    description="Add a picture or SVG (logo, north arrow) to a print layout. path is an image "
    "or SVG file path. Position/size in mm.",
)
async def add_layout_picture(
    ctx: Context,
    layout_name: str,
    path: str,
    x: float = 10,
    y: float = 10,
    width: float = 30,
    height: float = 30,
) -> dict:
    return await _send(
        "add_layout_picture",
        {
            "layout_name": layout_name,
            "path": path,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
        },
    )


@mcp.tool(
    title="Add Layout Table",
    description="Add an attribute table for a vector layer to a print layout. "
    "max_rows caps the number of features shown. Position/size in mm.",
)
async def add_layout_table(
    ctx: Context,
    layout_name: str,
    layer_id: str,
    x: float = 10,
    y: float = 10,
    width: float = 180,
    height: float = 80,
    max_rows: int = 20,
) -> dict:
    return await _send(
        "add_layout_table",
        {
            "layout_name": layout_name,
            "layer_id": layer_id,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "max_rows": max_rows,
        },
    )


@mcp.tool(
    title="Configure Atlas",
    description="Configure a print layout's atlas: coverage_layer (vector layer id) drives one "
    "page per feature. Optional page_name_expression, filter_expression, sort_expression.",
)
async def configure_atlas(
    ctx: Context,
    layout_name: str,
    coverage_layer: str,
    enabled: bool = True,
    page_name_expression: str | None = None,
    filter_expression: str | None = None,
    sort_expression: str | None = None,
) -> dict:
    return await _send(
        "configure_atlas",
        {
            "layout_name": layout_name,
            "coverage_layer": coverage_layer,
            "enabled": enabled,
            "page_name_expression": page_name_expression,
            "filter_expression": filter_expression,
            "sort_expression": sort_expression,
        },
    )


@mcp.tool(
    title="Export Atlas",
    annotations=ToolAnnotations(idempotentHint=True),
    description="Export a configured atlas. format 'pdf' writes a single multi-page file at "
    "output_path; image formats ('png','jpg','tif') write one file per feature into the "
    "output_path directory. Call configure_atlas first.",
)
async def export_atlas(
    ctx: Context,
    layout_name: str,
    output_path: str,
    format: str = "pdf",
    dpi: int = 300,
) -> dict:
    await ctx.info(f"Exporting atlas '{layout_name}' as {format} to {output_path}")
    return await _send(
        "export_atlas",
        {
            "layout_name": layout_name,
            "output_path": output_path,
            "format": format,
            "dpi": dpi,
        },
        timeout=TIMEOUT_LONG,
    )


@mcp.tool(
    title="Remove Layout",
    annotations=ToolAnnotations(destructiveHint=True),
    description="Remove a print layout from the project.",
)
async def remove_layout(ctx: Context, layout_name: str) -> dict:
    if not await _confirm_destructive(ctx, f"Remove layout '{layout_name}'?"):
        return {"ok": False, "message": "Cancelled by user"}
    return await _send("remove_layout", {"layout_name": layout_name})


@mcp.tool(
    title="Execute SQL",
    description="SQL across loaded layers via a virtual layer; reference layers by name in "
    "FROM/JOIN. as_layer=True registers the result as a new layer (set geometry_field for "
    "spatial output); else returns rows inline (max 1000). layers limits sources by layer id.",
)
async def execute_sql(
    ctx: Context,
    query: str,
    layers: list[str] | None = None,
    as_layer: bool = False,
    layer_name: str = "sql_result",
    geometry_field: str | None = None,
    uid_field: str | None = None,
) -> dict:
    return await _send(
        "execute_sql",
        {
            "query": query,
            "layers": layers,
            "as_layer": as_layer,
            "layer_name": layer_name,
            "geometry_field": geometry_field,
            "uid_field": uid_field,
        },
        timeout=TIMEOUT_LONG,
    )


@mcp.tool(
    title="Evaluate Expression",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Evaluate a standalone QGIS expression to a scalar value (e.g. "
    "aggregate('layer','sum','field'), @project_var, now()). Optional layer_id adds layer "
    "scope. Distinct from validate_expression (validate only) and field_calculator (per-feature).",
)
async def evaluate_expression(
    ctx: Context, expression: str, layer_id: str | None = None
) -> dict:
    return await _send(
        "evaluate_expression", {"expression": expression, "layer_id": layer_id}
    )


@mcp.tool(
    title="Identify Features",
    annotations=ToolAnnotations(readOnlyHint=True),
    description="Identify features at a point [x, y] in project CRS across layers (map-click "
    "analogue). tolerance (map units) expands the search; 0 = exact hit. layer_ids limits the "
    "search (default: visible vector layers). limit caps features per layer.",
)
async def identify_features(
    ctx: Context,
    point: list[float],
    tolerance: float = 0.0,
    layer_ids: list[str] | None = None,
    limit: int = 10,
) -> dict:
    return await _send(
        "identify_features",
        {
            "point": point,
            "tolerance": tolerance,
            "layer_ids": layer_ids,
            "limit": limit,
        },
    )


@mcp.tool(
    title="Duplicate Layer",
    description="Duplicate a layer (including its style) under a new name.",
)
async def duplicate_layer(
    ctx: Context, layer_id: str, new_name: str | None = None
) -> dict:
    return await _send(
        "duplicate_layer", {"layer_id": layer_id, "new_name": new_name}
    )


@mcp.tool(
    title="Set Layer Order",
    annotations=ToolAnnotations(idempotentHint=True),
    description="Set the explicit layer draw order in the tree. layer_ids is the ordered list "
    "of layer ids from top (drawn last) to bottom.",
)
async def set_layer_order(ctx: Context, layer_ids: list[str]) -> dict:
    return await _send("set_layer_order", {"layer_ids": layer_ids})


# ---------------------------------------------------------------------------
# Compound tool mode (opt-in via QGIS_MCP_TOOL_MODE=compound)
# ---------------------------------------------------------------------------

_tool_mode = os.environ.get("QGIS_MCP_TOOL_MODE", "granular")
if _tool_mode == "compound":
    from qgis_mcp.compound_tools import register_compound_tools

    # Replace granular tools with compound tools
    mcp._tool_manager._tools.clear()
    register_compound_tools(mcp, _send, _confirm_destructive)
    logger.info(f"Compound tool mode: {len(mcp._tool_manager._tools)} tools registered")


def _strip_schema_titles() -> None:
    """Drop redundant auto-generated 'title' keys from tool input schemas.

    Pydantic adds a display 'title' to the schema and every property (e.g. the
    param ``layer_id`` gets ``"title": "Layer Id"``) that just restates the name.
    It is sent to the client on every turn but carries no information, so removing
    it trims ~2k tokens off the granular tool schema with no behavior change.
    """

    def clean(node: object) -> None:
        if isinstance(node, dict):
            node.pop("title", None)
            for key in ("properties", "$defs", "definitions"):
                for sub in node.get(key, {}).values():
                    clean(sub)
            for key in ("items", "additionalProperties"):
                if isinstance(node.get(key), dict):
                    clean(node[key])
            for key in ("anyOf", "allOf", "oneOf"):
                for sub in node.get(key, []):
                    clean(sub)

    for tool in mcp._tool_manager._tools.values():
        clean(tool.parameters)


_strip_schema_titles()


# ===========================================================================
# MCP COMPLETIONS
# ===========================================================================

_completion_cache: list[str] = []
_completion_cache_at: float = 0.0
_COMPLETION_TTL: float = 10.0  # seconds — avoids hitting QGIS on every keystroke


@mcp.completion()
async def handle_completion(ref, argument: CompletionArgument, context=None):
    """Auto-complete layer_id arguments with available layer IDs.

    Uses a TTL cache to avoid querying QGIS on every keystroke.
    """
    global _completion_cache, _completion_cache_at

    if argument.name == "layer_id":
        try:
            now = time.monotonic()
            if now - _completion_cache_at >= _COMPLETION_TTL or not _completion_cache:
                result = await _send("get_layers", {"limit": 200, "offset": 0})
                layers = result.get("layers", [])
                _completion_cache = [layer["id"] for layer in layers]
                _completion_cache_at = now
            ids = _completion_cache
            if argument.value:
                prefix = argument.value.lower()
                ids = [lid for lid in ids if prefix in lid.lower()]
            return Completion(values=ids[:50])
        except Exception:
            return None
    return None


# ===========================================================================
# MCP RESOURCES
# ===========================================================================


@mcp.resource(
    "qgis://info", name="qgis_info", description="QGIS version, profile, and plugin count"
)
def qgis_info_resource() -> str:
    return json.dumps(_send_sync("get_qgis_info"))


@mcp.resource(
    "qgis://project",
    name="project_info",
    description="Current project metadata, CRS, layer count, layer summary",
)
def project_info_resource() -> str:
    return json.dumps(_send_sync("get_project_info"))


@mcp.resource(
    "qgis://layers", name="layer_list", description="All layers with IDs, names, types, visibility"
)
def layers_resource() -> str:
    return json.dumps(_send_sync("get_layers"))


@mcp.resource(
    "qgis://layers/{layer_id}/info",
    name="layer_info",
    description="Detailed layer info: CRS, extent, fields, feature count, source, provider",
)
def layer_info_resource(layer_id: str) -> str:
    return json.dumps(_send_sync("get_layer_info", {"layer_id": layer_id}))


@mcp.resource(
    "qgis://layers/{layer_id}/features",
    name="layer_features",
    description="Sample features (first 10) from a vector layer",
)
def layer_features_resource(layer_id: str) -> str:
    return json.dumps(_send_sync("get_layer_features", {"layer_id": layer_id, "limit": 10}))


@mcp.resource(
    "qgis://layers/{layer_id}/schema",
    name="layer_schema",
    description="Field names, types, and lengths for a vector layer",
)
def layer_schema_resource(layer_id: str) -> str:
    return json.dumps(_send_sync("get_layer_schema", {"layer_id": layer_id}))


@mcp.resource(
    "qgis://llms.txt",
    name="llms_context",
    description="Capabilities summary for LLM context — lists all tools, resources, and usage tips",
)
def llms_context_resource() -> str:
    return """# QGIS MCP — LLM Context

## Overview
QGIS MCP connects QGIS Desktop to LLMs via the Model Context Protocol.
67 tools for project management, layer operations, feature editing, styling, processing, and more.

## Quick Start
1. `ping` — verify connectivity
2. `diagnose` — check full stack health (versions, providers, clients)
3. `get_project_info` — understand current project
4. `get_layers` — list available layers
5. `get_layer_features` — inspect data (expression filtering, pagination)
6. `render_map` or `get_canvas_screenshot` — see the map

## Tool Categories
- **Info**: ping, diagnose, get_qgis_info, get_project_info
- **Project**: load_project, create_new_project, save_project, set_project_crs
- **Layers**: get_layers, add_vector_layer, add_raster_layer, add_web_layer, remove_layer, find_layer, create_memory_layer
- **Active Layer**: get_active_layer, set_active_layer
- **Visibility**: set_layer_visibility, zoom_to_layer
- **Features**: get_layer_features (max 50, filter with expressions), get_field_statistics, add_table_join
- **Fields**: add_field, delete_field, rename_field
- **Editing**: add_features, update_features, delete_features
- **Selection**: select_features, get_selection, clear_selection
- **Styling**: set_layer_style (single/categorized/graduated), apply_style_qml, save_style_qml
- **Labeling**: get_layer_labeling, set_layer_labeling (field, font_size, color)
- **Canvas**: get_canvas_extent, set_canvas_extent, get_canvas_screenshot, get_canvas_scale, set_canvas_scale
- **Raster**: get_raster_info
- **Processing**: execute_processing, list_processing_algorithms, get_algorithm_help, create_processing_model
- **Rendering**: render_map (re-render to image), get_canvas_screenshot (fast grab)
- **Code**: execute_code (arbitrary PyQGIS)
- **Batch**: batch_commands (multiple commands in one round-trip)
- **Layouts**: list_layouts, export_layout, create_layout, add_layout_map, add_layout_label, add_layout_legend, add_layout_scalebar, add_layout_picture, add_layout_table, get_layout_info, remove_layout
- **Atlas**: configure_atlas (coverage layer), export_atlas (one page per feature)
- **Query**: execute_sql (SQL across layers via virtual layer), evaluate_expression (scalar/aggregate), identify_features (features at a point)
- **Layer mgmt**: duplicate_layer, set_layer_order
- **Logging**: get_message_log
- **Plugins**: list_plugins, get_plugin_info, reload_plugin
- **Layer Tree**: get_layer_tree, create_layer_group, move_layer_to_group
- **Properties**: set_layer_property, get_layer_extent
- **CRS**: get_layer_crs, set_layer_crs, transform_coordinates
- **Variables**: get_project_variables, set_project_variable
- **Expression**: validate_expression, evaluate_expression
- **Settings**: get_setting, set_setting
- **Bookmarks**: get_bookmarks, add_bookmark, remove_bookmark
- **Map Themes**: get_map_themes, add_map_theme, remove_map_theme, apply_map_theme

## Tips
- **World basemap**: QGIS ships with a built-in world map. In the QGIS UI, \
type "world" in the locator bar (bottom of screen) to find and open it. Via MCP: \
use `execute_code` to resolve `QgsApplication.pkgDataPath() + "/resources/data/world_map.gpkg"`, \
then pass that path to `add_vector_layer` to load it as a background for spatial context.
- **Map themes**: save/restore layer visibility presets — useful for toggling between views.
- **Bookmarks**: save named extents for quick navigation to areas of interest.

## Key Patterns
- Layer IDs are used to reference layers (get them from get_layers or find_layer)
- Features are flat dicts: {"_fid": 1, "name": "Berlin", "_geometry": "POINT(...)"}
- Use expressions for server-side filtering: "population > 1000000"
- Processing algorithms: search with list_processing_algorithms, get params with get_algorithm_help
- render_map returns inline images; get_canvas_screenshot is faster (no re-render)
- Destructive operations (remove_layer, delete_features, set_setting) may ask for confirmation
- Use diagnose to troubleshoot connection or version issues

## Resources (read-only data)
- qgis://info — QGIS version info
- qgis://project — project metadata
- qgis://layers — all layers
- qgis://layers/{id}/info — layer details
- qgis://layers/{id}/features — sample features
- qgis://layers/{id}/schema — field schema
- qgis://llms.txt — this context file

## Environment Variables
- QGIS_MCP_HOST — server host (default: localhost)
- QGIS_MCP_PORT — server port (default: 9876)
- QGIS_MCP_TOKEN — optional shared secret; when set, must match the plugin's value (default: unset = no auth)
- QGIS_MCP_TRANSPORT — "stdio" (default) or "streamable-http"
- QGIS_MCP_TOOL_MODE — "granular" (default, 51 tools) or "compound" (~19 grouped tools)
- QGIS_MCP_LOG_FILE — log file path (default: ~/.local/share/qgis-mcp/server.log)
- QGIS_MCP_LOG_LEVEL — file log level (default: INFO)
"""


# ===========================================================================
# MCP PROMPTS
# ===========================================================================


@mcp.prompt(
    name="analyze_layer",
    description="Deeply inspect a layer's schema, sample data, and compute detailed field statistics",
)
def analyze_layer_prompt(layer_id: str) -> list[UserMessage]:
    return [
        UserMessage(
            content=f"Perform a comprehensive analysis of the layer with ID '{layer_id}':\n"
            f"1. Read resource qgis://layers/{layer_id}/info for general metadata (CRS, extent, count)\n"
            f"2. Read resource qgis://layers/{layer_id}/schema to understand field types and constraints\n"
            f"3. Read resource qgis://layers/{layer_id}/features to inspect representative sample data\n"
            f"4. For each numeric field, call get_field_statistics to understand the data distribution (min, max, mean, etc.)\n"
            f"5. For categorical fields, identify unique values and their prevalence\n"
            f"6. Provide a detailed summary including: geometry validity, projection suitability, data quality, and potential analysis use cases"
        )
    ]


@mcp.prompt(
    name="spatial_analysis",
    description="Run a spatial operation between two layers with CRS validation",
)
def spatial_analysis_prompt(
    input_layer: str, overlay_layer: str, operation: str
) -> list[UserMessage]:
    return [
        UserMessage(
            content=f"Perform a spatial {operation} between layers:\n"
            f"- Input: {input_layer}\n"
            f"- Overlay: {overlay_layer}\n"
            f"Steps:\n"
            f"1. Get info for both layers (get_layers or qgis://layers/ID/info)\n"
            f"2. Verify both layers are vector layers with compatible geometry types\n"
            f"3. Check that CRS matches; if not, reproject one layer first\n"
            f"4. Use execute_processing with the appropriate algorithm (e.g. native:intersection, native:union)\n"
            f"5. Report the result layer's feature count and fields"
        )
    ]


@mcp.prompt(
    name="create_processing_model",
    description="Translate a natural-language workflow description into a saved QGIS Processing Model",
)
def create_processing_model_prompt(description: str) -> list[UserMessage]:
    return [
        UserMessage(
            content=(
                "Build a QGIS Processing Model that implements this workflow:\n\n"
                f"\"{description}\"\n\n"
                "Call the `create_processing_model` tool ONCE. Algorithm lookup and parameter "
                "validation happen inside the plugin against QGIS's Processing registry — do NOT "
                "call `list_processing_algorithms` or `get_algorithm_help`. For each step pass a "
                "concise `algorithm` keyword (e.g. 'buffer', 'centroids', 'clip') or a full id; "
                "if a keyword is ambiguous the tool returns the candidate list so you can retry "
                "with a more specific hint. Reference model inputs as '@name', earlier step "
                "outputs as '$step_id.OUTPUT', and QGIS expressions as '=expr'. "
                "The model is always saved into the QGIS user models folder and registered in the "
                "Processing Toolbox; if the requested name is taken the tool appends a numeric "
                "suffix and returns the actual filename used. "
                "After the call, summarize the resolved_steps it returned and tell the user the "
                "final model name so they can find it in the toolbox."
            )
        )
    ]


@mcp.prompt(
    name="style_map", description="Create a thematic map with categorized or graduated symbology"
)
def style_map_prompt(layer_id: str, field: str) -> list[UserMessage]:
    return [
        UserMessage(
            content=f"Style layer '{layer_id}' based on field '{field}':\n"
            f"1. Get the layer schema and sample data to understand the field values\n"
            f"2. Call get_field_statistics for '{field}' to understand the data distribution\n"
            f"3. If the field is categorical, use set_layer_style with style_type='categorized'\n"
            f"4. If the field is numeric, use set_layer_style with style_type='graduated'\n"
            f"5. Refresh the canvas and render a preview image with render_map"
        )
    ]


# ===========================================================================
# Entry point
# ===========================================================================


def main():
    transport = os.environ.get("QGIS_MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
