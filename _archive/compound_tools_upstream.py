"""Compound tool registrations for QGIS MCP.

When QGIS_MCP_TOOL_MODE=compound, these ~22 grouped tools replace the
granular tools, reducing context window overhead for LLMs with limited tool
slots.

Each compound tool takes an ``action`` string as its first parameter and
dispatches to the same ``_send()`` logic used by the granular tools.
"""

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import Annotations, ImageContent, ToolAnnotations

from qgis_mcp.helpers import (
    BATCH_BLOCKED_COMMANDS,
    TIMEOUT_LONG,
    enrich_diagnose,
    make_layer_response,
    make_project_response,
    make_render_response,
)


def register_compound_tools(mcp: FastMCP, _send, _confirm_destructive):
    """Register compound tools on the MCP server instance."""

    # ------------------------------------------------------------------
    # 1. system
    # ------------------------------------------------------------------

    @mcp.tool(
        title="System",
        description=(
            "System operations.\n"
            "Actions: ping, diagnose, get_qgis_info\n"
            "- ping: no params\n"
            "- diagnose: no params\n"
            "- get_qgis_info: no params"
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=True,
    )
    async def system(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "ping":
            return await _send("ping")
        elif action == "diagnose":
            await ctx.info("Running diagnostics...")
            result = await _send("diagnose")
            return enrich_diagnose(result)
        elif action == "get_qgis_info":
            return await _send("get_qgis_info")
        else:
            raise ValueError(f"Unknown system action: {action}")

    # ------------------------------------------------------------------
    # 2. project
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Project",
        description=(
            "Project management.\n"
            "Actions: get_info, load, create, save, set_crs\n"
            "- get_info: no params\n"
            "- load: path (str)\n"
            "- create: path (str)\n"
            "- save: path (str, optional)\n"
            "- set_crs: crs (str)"
        ),
        structured_output=True,
    )
    async def project(ctx: Context, action: str, **kwargs) -> dict[str, Any] | list:
        if action == "get_info":
            return await _send("get_project_info")
        elif action == "load":
            path = kwargs["path"]
            await ctx.info(f"Loading project: {path}")
            result = await _send("load_project", {"path": path})
            return make_project_response(result)
        elif action == "create":
            result = await _send("create_new_project", {"path": kwargs["path"]})
            return make_project_response(result)
        elif action == "save":
            params = {}
            if "path" in kwargs:
                params["path"] = kwargs["path"]
            return await _send("save_project", params)
        elif action == "set_crs":
            result = await _send("set_project_crs", {"crs": kwargs["crs"]})
            return make_project_response(result)
        else:
            raise ValueError(f"Unknown project action: {action}")

    # ------------------------------------------------------------------
    # 3. layer
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Layer",
        description=(
            "Layer management.\n"
            "Actions: list, add_vector, add_raster, remove, find, create_memory, "
            "set_visibility, zoom_to, get_info, get_schema, get_extent, get_raster_info, "
            "get_crs, set_crs, get_labeling, set_labeling\n"
            "- list: limit (int, default 50), offset (int, default 0)\n"
            "- add_vector: path (str), provider (str, default 'ogr'), name (str, optional)\n"
            "- add_raster: path (str), provider (str, default 'gdal'), name (str, optional)\n"
            "- remove: layer_id (str) — destructive, requires confirmation\n"
            "- find: name_pattern (str)\n"
            "- create_memory: name (str), geometry_type (str), crs (str, default 'EPSG:4326'), "
            "fields (list[dict], optional)\n"
            "- set_visibility: layer_id (str), visible (bool)\n"
            "- zoom_to: layer_id (str)\n"
            "- get_info: layer_id (str)\n"
            "- get_schema: layer_id (str)\n"
            "- get_extent: layer_id (str)\n"
            "- get_raster_info: layer_id (str)\n"
            "- get_crs: layer_id (str)\n"
            "- set_crs: layer_id (str), crs (str)\n"
            "- get_labeling: layer_id (str)\n"
            "- set_labeling: layer_id (str), enabled (bool, default true), "
            "field_name (str, optional), font_size (float, optional), color (str, optional)"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def layer(ctx: Context, action: str, **kwargs) -> dict[str, Any] | list:
        if action == "list":
            return await _send(
                "get_layers",
                {
                    "limit": kwargs.get("limit", 50),
                    "offset": kwargs.get("offset", 0),
                },
            )
        elif action == "add_vector":
            params = {"path": kwargs["path"], "provider": kwargs.get("provider", "ogr")}
            if "name" in kwargs:
                params["name"] = kwargs["name"]
            result = await _send("add_vector_layer", params)
            return make_layer_response(result)
        elif action == "add_raster":
            params = {"path": kwargs["path"], "provider": kwargs.get("provider", "gdal")}
            if "name" in kwargs:
                params["name"] = kwargs["name"]
            result = await _send("add_raster_layer", params)
            return make_layer_response(result)
        elif action == "remove":
            layer_id = kwargs["layer_id"]
            if not await _confirm_destructive(
                ctx, f"Remove layer {layer_id}? This cannot be undone."
            ):
                return {"ok": False, "message": "Cancelled by user"}
            return await _send("remove_layer", {"layer_id": layer_id})
        elif action == "find":
            return await _send("find_layer", {"name_pattern": kwargs["name_pattern"]})
        elif action == "create_memory":
            params = {
                "name": kwargs["name"],
                "geometry_type": kwargs["geometry_type"],
                "crs": kwargs.get("crs", "EPSG:4326"),
            }
            if "fields" in kwargs:
                params["fields"] = kwargs["fields"]
            result = await _send("create_memory_layer", params)
            return make_layer_response(result, fallback_name=kwargs["name"])
        elif action == "set_visibility":
            return await _send(
                "set_layer_visibility",
                {
                    "layer_id": kwargs["layer_id"],
                    "visible": kwargs["visible"],
                },
            )
        elif action == "zoom_to":
            return await _send("zoom_to_layer", {"layer_id": kwargs["layer_id"]})
        elif action == "get_info":
            return await _send("get_layer_info", {"layer_id": kwargs["layer_id"]})
        elif action == "get_schema":
            return await _send("get_layer_schema", {"layer_id": kwargs["layer_id"]})
        elif action == "get_extent":
            return await _send("get_layer_extent", {"layer_id": kwargs["layer_id"]})
        elif action == "get_raster_info":
            return await _send("get_raster_info", {"layer_id": kwargs["layer_id"]})
        elif action == "get_crs":
            return await _send("get_layer_crs", {"layer_id": kwargs["layer_id"]})
        elif action == "set_crs":
            return await _send("set_layer_crs", {"layer_id": kwargs["layer_id"], "crs": kwargs["crs"]})
        elif action == "get_labeling":
            return await _send("get_layer_labeling", {"layer_id": kwargs["layer_id"]})
        elif action == "set_labeling":
            params: dict[str, Any] = {"layer_id": kwargs["layer_id"], "enabled": kwargs.get("enabled", True)}
            if "field_name" in kwargs:
                params["field_name"] = kwargs["field_name"]
            if "font_size" in kwargs:
                params["font_size"] = kwargs["font_size"]
            if "color" in kwargs:
                params["color"] = kwargs["color"]
            return await _send("set_layer_labeling", params)
        else:
            raise ValueError(f"Unknown layer action: {action}")

    # ------------------------------------------------------------------
    # 4. features
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Features",
        description=(
            "Feature access and editing.\n"
            "Actions: get, get_statistics, add, update, delete\n"
            "- get: layer_id (str), limit (int, default 10, max 50), offset (int, default 0), "
            "expression (str, optional), include_geometry (bool, default false)\n"
            "- get_statistics: layer_id (str), field_name (str)\n"
            "- add: layer_id (str), features (list[dict]) — destructive\n"
            "- update: layer_id (str), updates (list[dict]) — destructive\n"
            "- delete: layer_id (str), fids (list[int], optional), expression (str, optional) "
            "— destructive, requires confirmation"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def features(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "get":
            limit = min(kwargs.get("limit", 10), 50)
            params = {
                "layer_id": kwargs["layer_id"],
                "limit": limit,
                "offset": kwargs.get("offset", 0),
                "include_geometry": kwargs.get("include_geometry", False),
            }
            if "expression" in kwargs:
                params["expression"] = kwargs["expression"]
            return await _send("get_layer_features", params)
        elif action == "get_statistics":
            return await _send(
                "get_field_statistics",
                {
                    "layer_id": kwargs["layer_id"],
                    "field_name": kwargs["field_name"],
                },
            )
        elif action == "add":
            return await _send(
                "add_features",
                {
                    "layer_id": kwargs["layer_id"],
                    "features": kwargs["features"],
                },
            )
        elif action == "update":
            return await _send(
                "update_features",
                {
                    "layer_id": kwargs["layer_id"],
                    "updates": kwargs["updates"],
                },
            )
        elif action == "delete":
            layer_id = kwargs["layer_id"]
            fids = kwargs.get("fids")
            expression = kwargs.get("expression")
            target = f"fids={fids}" if fids else f"expression='{expression}'"
            if not await _confirm_destructive(
                ctx, f"Delete features from layer {layer_id} ({target})?"
            ):
                return {"ok": False, "message": "Cancelled by user"}
            params: dict[str, Any] = {"layer_id": layer_id}
            if fids is not None:
                params["fids"] = fids
            if expression:
                params["expression"] = expression
            return await _send("delete_features", params)
        else:
            raise ValueError(f"Unknown features action: {action}")

    # ------------------------------------------------------------------
    # 5. selection
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Selection",
        description=(
            "Feature selection.\n"
            "Actions: select, get, clear\n"
            "- select: layer_id (str), expression (str, optional), fids (list[int], optional)\n"
            "- get: layer_id (str)\n"
            "- clear: layer_id (str)"
        ),
        annotations=ToolAnnotations(idempotentHint=True),
    )
    async def selection(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "select":
            params: dict[str, Any] = {"layer_id": kwargs["layer_id"]}
            if "expression" in kwargs:
                params["expression"] = kwargs["expression"]
            if "fids" in kwargs:
                params["fids"] = kwargs["fids"]
            return await _send("select_features", params)
        elif action == "get":
            return await _send("get_selection", {"layer_id": kwargs["layer_id"]})
        elif action == "clear":
            return await _send("clear_selection", {"layer_id": kwargs["layer_id"]})
        else:
            raise ValueError(f"Unknown selection action: {action}")

    # ------------------------------------------------------------------
    # 6. style
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Style",
        description=(
            "Layer symbology.\n"
            "Actions: set\n"
            "- set: layer_id (str), style_type (str: 'single', 'categorized', 'graduated'), "
            "field (str, optional — required for categorized/graduated), "
            "classes (int, default 5), color_ramp (str, default 'Spectral')"
        ),
    )
    async def style(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "set":
            params = {
                "layer_id": kwargs["layer_id"],
                "style_type": kwargs["style_type"],
                "classes": kwargs.get("classes", 5),
                "color_ramp": kwargs.get("color_ramp", "Spectral"),
            }
            if "field" in kwargs:
                params["field"] = kwargs["field"]
            return await _send("set_layer_style", params)
        else:
            raise ValueError(f"Unknown style action: {action}")

    # ------------------------------------------------------------------
    # 7. canvas
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Canvas",
        description=(
            "Map canvas operations.\n"
            "Actions: get_extent, set_extent, screenshot, get_scale, set_scale\n"
            "- get_extent: no params\n"
            "- set_extent: xmin (float), ymin (float), xmax (float), ymax (float), "
            "crs (str, optional)\n"
            "- screenshot: no params — returns inline image\n"
            "- get_scale: no params\n"
            "- set_scale: scale (float, optional), rotation (float, optional)"
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def canvas(ctx: Context, action: str, **kwargs) -> dict[str, Any] | list:
        if action == "get_extent":
            return await _send("get_canvas_extent")
        elif action == "set_extent":
            params = {
                "xmin": kwargs["xmin"],
                "ymin": kwargs["ymin"],
                "xmax": kwargs["xmax"],
                "ymax": kwargs["ymax"],
            }
            if "crs" in kwargs:
                params["crs"] = kwargs["crs"]
            return await _send("set_canvas_extent", params)
        elif action == "screenshot":
            result = await _send("get_canvas_screenshot")
            return [
                ImageContent(
                    type="image",
                    data=result["base64_data"],
                    mimeType="image/png",
                    annotations=Annotations(audience=["user", "assistant"], priority=1.0),
                )
            ]
        elif action == "get_scale":
            return await _send("get_canvas_scale")
        elif action == "set_scale":
            params: dict[str, Any] = {}
            if "scale" in kwargs:
                params["scale"] = kwargs["scale"]
            if "rotation" in kwargs:
                params["rotation"] = kwargs["rotation"]
            return await _send("set_canvas_scale", params)
        else:
            raise ValueError(f"Unknown canvas action: {action}")

    # ------------------------------------------------------------------
    # 8. render
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Render",
        description=(
            "Rendering and layout export.\n"
            "Actions: map, export_layout, list_layouts\n"
            "- map: width (int, default 800), height (int, default 600), "
            "path (str, optional) — returns inline image\n"
            "- export_layout: layout_name (str), path (str), format (str, default 'pdf'), "
            "dpi (int, default 300)\n"
            "- list_layouts: no params"
        ),
        annotations=ToolAnnotations(idempotentHint=True),
    )
    async def render(ctx: Context, action: str, **kwargs) -> dict[str, Any] | list:
        if action == "map":
            await ctx.info("Rendering map...")
            await ctx.report_progress(0, 100)
            params = {
                "width": kwargs.get("width", 800),
                "height": kwargs.get("height", 600),
            }
            path = kwargs.get("path")
            if path:
                params["path"] = path
            result = await _send("render_map_base64", params, timeout=TIMEOUT_LONG)
            await ctx.report_progress(100, 100)
            return make_render_response(result, params["width"], params["height"], path)
        elif action == "export_layout":
            return await _send(
                "export_layout",
                {
                    "layout_name": kwargs["layout_name"],
                    "path": kwargs["path"],
                    "format": kwargs.get("format", "pdf"),
                    "dpi": kwargs.get("dpi", 300),
                },
            )
        elif action == "list_layouts":
            return await _send("list_layouts")
        else:
            raise ValueError(f"Unknown render action: {action}")

    # ------------------------------------------------------------------
    # 9. processing
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Processing",
        description=(
            "QGIS Processing framework.\n"
            "Actions: execute, list_algorithms, get_help\n"
            "- execute: algorithm (str), parameters (dict)\n"
            "- list_algorithms: search (str, optional), provider (str, optional)\n"
            "- get_help: algorithm_id (str)"
        ),
    )
    async def processing(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "execute":
            await ctx.info(f"Running algorithm: {kwargs['algorithm']}")
            await ctx.report_progress(0, 100)
            result = await _send(
                "execute_processing",
                {"algorithm": kwargs["algorithm"], "parameters": kwargs["parameters"]},
                timeout=TIMEOUT_LONG,
            )
            await ctx.report_progress(100, 100)
            return result
        elif action == "list_algorithms":
            params = {}
            if "search" in kwargs:
                params["search"] = kwargs["search"]
            if "provider" in kwargs:
                params["provider"] = kwargs["provider"]
            return await _send("list_processing_algorithms", params)
        elif action == "get_help":
            return await _send("get_algorithm_help", {"algorithm_id": kwargs["algorithm_id"]})
        else:
            raise ValueError(f"Unknown processing action: {action}")

    # ------------------------------------------------------------------
    # 10. code
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Code",
        description=(
            "Execute arbitrary PyQGIS code.\n"
            "Actions: execute\n"
            "- execute: code (str) — destructive, requires confirmation"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def code(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "execute":
            if not await _confirm_destructive(
                ctx, "Execute arbitrary PyQGIS code? This can modify your project and system."
            ):
                return {"ok": False, "message": "Cancelled by user"}
            await ctx.info("Executing PyQGIS code...")
            await ctx.report_progress(0, 100)
            result = await _send("execute_code", {"code": kwargs["code"]}, timeout=TIMEOUT_LONG)
            await ctx.report_progress(100, 100)
            return result
        else:
            raise ValueError(f"Unknown code action: {action}")

    # ------------------------------------------------------------------
    # 11. batch
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Batch",
        description=(
            "Execute multiple commands in a single round-trip.\n"
            "Actions: execute\n"
            "- execute: commands (list[dict]) — each {'type': '<command>', 'params': {...}}. "
            "Destructive commands (execute_code, remove_layer, delete_features, set_setting, "
            "reload_plugin) are not allowed in batch."
        ),
    )
    async def batch(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "execute":
            commands = kwargs["commands"]
            for cmd in commands:
                cmd_type = cmd.get("type", "")
                if cmd_type in BATCH_BLOCKED_COMMANDS:
                    raise ValueError(
                        f"Command {cmd_type!r} is not allowed in batch — "
                        "call it individually so confirmation can be requested"
                    )
            return await _send("batch", {"commands": commands}, timeout=TIMEOUT_LONG)
        else:
            raise ValueError(f"Unknown batch action: {action}")

    # ------------------------------------------------------------------
    # 12. layer_tree
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Layer Tree",
        description=(
            "Layer tree structure.\n"
            "Actions: get, create_group, move_to_group\n"
            "- get: no params\n"
            "- create_group: name (str), parent (str, optional)\n"
            "- move_to_group: layer_id (str), group_name (str)"
        ),
    )
    async def layer_tree(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "get":
            return await _send("get_layer_tree")
        elif action == "create_group":
            params = {"name": kwargs["name"]}
            if "parent" in kwargs:
                params["parent"] = kwargs["parent"]
            return await _send("create_layer_group", params)
        elif action == "move_to_group":
            return await _send(
                "move_layer_to_group",
                {
                    "layer_id": kwargs["layer_id"],
                    "group_name": kwargs["group_name"],
                },
            )
        else:
            raise ValueError(f"Unknown layer_tree action: {action}")

    # ------------------------------------------------------------------
    # 13. plugins
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Plugins",
        description=(
            "Plugin management.\n"
            "Actions: list, get_info, reload\n"
            "- list: enabled_only (bool, default false)\n"
            "- get_info: plugin_name (str)\n"
            "- reload: plugin_name (str) — destructive"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def plugins(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "list":
            return await _send(
                "list_plugins",
                {
                    "enabled_only": kwargs.get("enabled_only", False),
                },
            )
        elif action == "get_info":
            return await _send("get_plugin_info", {"plugin_name": kwargs["plugin_name"]})
        elif action == "reload":
            await ctx.info(f"Reloading plugin: {kwargs['plugin_name']}")
            return await _send("reload_plugin", {"plugin_name": kwargs["plugin_name"]})
        else:
            raise ValueError(f"Unknown plugins action: {action}")

    # ------------------------------------------------------------------
    # 14. variables
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Variables",
        description=(
            "Project variables.\nActions: get, set\n- get: no params\n- set: key (str), value (str)"
        ),
        annotations=ToolAnnotations(idempotentHint=True),
    )
    async def variables(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "get":
            return await _send("get_project_variables")
        elif action == "set":
            return await _send(
                "set_project_variable",
                {
                    "key": kwargs["key"],
                    "value": kwargs["value"],
                },
            )
        else:
            raise ValueError(f"Unknown variables action: {action}")

    # ------------------------------------------------------------------
    # 15. settings
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Settings",
        description=(
            "QGIS settings.\n"
            "Actions: get, set\n"
            "- get: key (str)\n"
            "- set: key (str), value (str) — destructive, requires confirmation"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def settings(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "get":
            return await _send("get_setting", {"key": kwargs["key"]})
        elif action == "set":
            key = kwargs["key"]
            if not await _confirm_destructive(
                ctx, f"Set QGIS setting '{key}'? Incorrect settings can affect behavior."
            ):
                return {"ok": False, "message": "Cancelled by user"}
            return await _send("set_setting", {"key": key, "value": kwargs["value"]})
        else:
            raise ValueError(f"Unknown settings action: {action}")

    # ------------------------------------------------------------------
    # 16. additional tools that don't fit neatly into groups above
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Expression",
        description=(
            "Expression validation.\n"
            "Actions: validate\n"
            "- validate: expression (str), layer_id (str, optional)"
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=True,
    )
    async def expression(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "validate":
            params = {"expression": kwargs["expression"]}
            if "layer_id" in kwargs:
                params["layer_id"] = kwargs["layer_id"]
            return await _send("validate_expression", params)
        else:
            raise ValueError(f"Unknown expression action: {action}")

    @mcp.tool(
        title="Transform",
        description=(
            "CRS coordinate transformation.\n"
            "Actions: coordinates\n"
            "- coordinates: source_crs (str), target_crs (str), point (dict, optional), "
            "points (list[dict], optional), bbox (dict, optional)"
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=True,
    )
    async def transform(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "coordinates":
            params = {
                "source_crs": kwargs["source_crs"],
                "target_crs": kwargs["target_crs"],
            }
            if "point" in kwargs:
                params["point"] = kwargs["point"]
            if "points" in kwargs:
                params["points"] = kwargs["points"]
            if "bbox" in kwargs:
                params["bbox"] = kwargs["bbox"]
            return await _send("transform_coordinates", params)
        else:
            raise ValueError(f"Unknown transform action: {action}")

    @mcp.tool(
        title="Message Log",
        description=(
            "QGIS message log.\n"
            "Actions: get\n"
            "- get: level (str, optional: 'info', 'warning', 'critical'), "
            "tag (str, optional), limit (int, default 100)"
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=True,
    )
    async def message_log(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "get":
            params: dict[str, Any] = {"limit": kwargs.get("limit", 100)}
            if "level" in kwargs:
                params["level"] = kwargs["level"]
            if "tag" in kwargs:
                params["tag"] = kwargs["tag"]
            return await _send("get_message_log", params)
        else:
            raise ValueError(f"Unknown message_log action: {action}")

    @mcp.tool(
        title="Layer Property",
        description=(
            "Layer properties.\n"
            "Actions: set\n"
            "- set: layer_id (str), property (str), value (str) — "
            "supported: opacity, name, min_scale, max_scale, scale_visibility"
        ),
        annotations=ToolAnnotations(idempotentHint=True),
    )
    async def layer_property(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "set":
            return await _send(
                "set_layer_property",
                {
                    "layer_id": kwargs["layer_id"],
                    "property": kwargs["property"],
                    "value": kwargs["value"],
                },
            )
        else:
            raise ValueError(f"Unknown layer_property action: {action}")

    # ------------------------------------------------------------------
    # 20. bookmarks
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Bookmarks",
        description=(
            "Spatial bookmarks for quick navigation.\n"
            "Actions: list, add, remove\n"
            "- list: no params\n"
            "- add: name (str), xmin (float), ymin (float), xmax (float), ymax (float), "
            "crs (str, default 'EPSG:4326'), group (str, optional)\n"
            "- remove: bookmark_id (str) — destructive"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def bookmarks(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "list":
            return await _send("get_bookmarks")
        elif action == "add":
            return await _send(
                "add_bookmark",
                {
                    "name": kwargs["name"],
                    "xmin": kwargs["xmin"],
                    "ymin": kwargs["ymin"],
                    "xmax": kwargs["xmax"],
                    "ymax": kwargs["ymax"],
                    "crs": kwargs.get("crs", "EPSG:4326"),
                    "group": kwargs.get("group", ""),
                },
            )
        elif action == "remove":
            return await _send("remove_bookmark", {"bookmark_id": kwargs["bookmark_id"]})
        else:
            raise ValueError(f"Unknown bookmarks action: {action}")

    # ------------------------------------------------------------------
    # 21. map_themes
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Map Themes",
        description=(
            "Map themes (visibility presets).\n"
            "Actions: list, add, remove, apply\n"
            "- list: no params\n"
            "- add: name (str) — saves current visibility state\n"
            "- remove: name (str) — destructive\n"
            "- apply: name (str) — restores saved visibility state"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def map_themes(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "list":
            return await _send("get_map_themes")
        elif action == "add":
            return await _send("add_map_theme", {"name": kwargs["name"]})
        elif action == "remove":
            return await _send("remove_map_theme", {"name": kwargs["name"]})
        elif action == "apply":
            return await _send("apply_map_theme", {"name": kwargs["name"]})
        else:
            raise ValueError(f"Unknown map_themes action: {action}")

    # ------------------------------------------------------------------
    # 22. active_layer
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Active Layer",
        description=(
            "Active layer management.\n"
            "Actions: get, set\n"
            "- get: no params\n"
            "- set: layer_id (str)"
        ),
        annotations=ToolAnnotations(idempotentHint=True),
    )
    async def active_layer(ctx: Context, action: str, **kwargs) -> dict[str, Any]:
        if action == "get":
            return await _send("get_active_layer")
        elif action == "set":
            return await _send("set_active_layer", {"layer_id": kwargs["layer_id"]})
        else:
            raise ValueError(f"Unknown active_layer action: {action}")
