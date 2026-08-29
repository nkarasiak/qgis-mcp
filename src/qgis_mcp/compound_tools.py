"""Compound tool registrations for QGIS MCP.

When QGIS_MCP_TOOL_MODE=compound, these 27 grouped tools replace the
granular tools, reducing context window overhead for LLMs with limited tool
slots.

Each compound tool takes an ``action`` string as its first parameter and
dispatches to the same ``_send()`` logic used by the granular tools.
"""

from typing import Any

try:
    from mcp.server.fastmcp import Context, FastMCP
except ModuleNotFoundError:  # mcp >= 2.0 renamed fastmcp -> mcpserver
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver import MCPServer as FastMCP
from mcp.types import Annotations, ImageContent, ToolAnnotations

from qgis_mcp.helpers import (
    BATCH_BLOCKED_COMMANDS,
    TIMEOUT_LONG,
    enrich_diagnose,
    make_layer_response,
    make_project_response,
    make_render_response,
)

# Appended to every compound tool description so agents know where the
# per-action parameters go (they are NOT top-level tool arguments).
_PARAMS_NOTE = (
    "\nAll action parameters go inside the `params` object, e.g. "
    '{"action": "load", "params": {"path": "/tmp/x.qgz"}}. '
    "Omit `params` for actions that take none."
)

# Map render-group layout actions to their underlying plugin commands.
_LAYOUT_ITEM_COMMANDS = {
    "add_map": "add_layout_map",
    "add_label": "add_layout_label",
    "add_legend": "add_layout_legend",
    "add_scalebar": "add_layout_scalebar",
    "add_picture": "add_layout_picture",
    "add_table": "add_layout_table",
    "configure_atlas": "configure_atlas",
}


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
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=True,
    )
    async def system(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
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
            f"{_PARAMS_NOTE}"
        ),
        structured_output=True,
    )
    async def project(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list:
        kwargs = params or {}
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
            "Actions: list, add_vector, add_raster, add_web, remove, find, create_memory, "
            "set_visibility, zoom_to, get_info, get_schema, get_extent, get_raster_info, "
            "get_crs, set_crs, get_labeling, set_labeling, duplicate, set_order, export, "
            "save_style, apply_style, add_join\n"
            "- list: limit (int, default 50), offset (int, default 0)\n"
            "- add_vector: path (str), provider (str, default 'ogr'), name (str, optional)\n"
            "- add_raster: path (str), provider (str, default 'gdal'), name (str, optional)\n"
            "- remove: layer_id (str) - destructive, requires confirmation\n"
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
            "field_name (str, optional), font_size (float, optional), color (str, optional)\n"
            "- duplicate: layer_id (str), new_name (str, optional)\n"
            "- set_order: layer_ids (list[str]) - top to bottom\n"
            "- add_web: url (str), service (str: 'xyz', 'wms', 'wfs'), name (str, optional), "
            "crs (str, optional - only for wms/wfs; XYZ tiles are always EPSG:3857 and "
            "requesting another CRS is an error)\n"
            "- export: layer_id (str), output_path (str) - format from extension "
            "(.gpkg/.shp/.geojson/.tif); target_crs (str, optional) reprojects, "
            "filter_expression (str, optional) exports a subset\n"
            "- save_style: layer_id (str), path (str) - write a .qml\n"
            "- apply_style: layer_id (str), path (str) - load a .qml\n"
            "- add_join: target_layer_id (str), join_layer_id (str), target_field (str), "
            "join_field (str), prefix (str, default '')"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def layer(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list:
        kwargs = params or {}
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
            return await _send(
                "set_layer_crs", {"layer_id": kwargs["layer_id"], "crs": kwargs["crs"]}
            )
        elif action == "get_labeling":
            return await _send("get_layer_labeling", {"layer_id": kwargs["layer_id"]})
        elif action == "set_labeling":
            params: dict[str, Any] = {
                "layer_id": kwargs["layer_id"],
                "enabled": kwargs.get("enabled", True),
            }
            if "field_name" in kwargs:
                params["field_name"] = kwargs["field_name"]
            if "font_size" in kwargs:
                params["font_size"] = kwargs["font_size"]
            if "color" in kwargs:
                params["color"] = kwargs["color"]
            return await _send("set_layer_labeling", params)
        elif action == "duplicate":
            dup_params: dict[str, Any] = {"layer_id": kwargs["layer_id"]}
            if "new_name" in kwargs:
                dup_params["new_name"] = kwargs["new_name"]
            result = await _send("duplicate_layer", dup_params)
            return make_layer_response(result)
        elif action == "set_order":
            return await _send("set_layer_order", {"layer_ids": kwargs["layer_ids"]})
        elif action == "add_web":
            web_params: dict[str, Any] = {
                "url": kwargs["url"],
                "service": kwargs["service"],
            }
            for key in ("crs", "name"):
                if kwargs.get(key):
                    web_params[key] = kwargs[key]
            result = await _send("add_web_layer", web_params)
            return make_layer_response(result)
        elif action == "export":
            await ctx.info(f"Exporting layer to {kwargs['output_path']}")
            return await _send(
                "export_layer",
                {
                    "layer_id": kwargs["layer_id"],
                    "output_path": kwargs["output_path"],
                    "target_crs": kwargs.get("target_crs"),
                    "filter_expression": kwargs.get("filter_expression"),
                },
                timeout=TIMEOUT_LONG,
            )
        elif action == "save_style":
            return await _send(
                "save_style_qml", {"layer_id": kwargs["layer_id"], "path": kwargs["path"]}
            )
        elif action == "apply_style":
            return await _send(
                "apply_style_qml", {"layer_id": kwargs["layer_id"], "path": kwargs["path"]}
            )
        elif action == "add_join":
            return await _send(
                "add_table_join",
                {
                    "target_layer_id": kwargs["target_layer_id"],
                    "join_layer_id": kwargs["join_layer_id"],
                    "target_field": kwargs["target_field"],
                    "join_field": kwargs["join_field"],
                    "prefix": kwargs.get("prefix", ""),
                },
            )
        else:
            raise ValueError(f"Unknown layer action: {action}")

    # ------------------------------------------------------------------
    # 4. features
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Features",
        description=(
            "Feature access and editing.\n"
            "Actions: get, get_statistics, add, update, update_geometry, delete\n"
            "- get: layer_id (str), limit (int, default 10, max 50), offset (int, default 0), "
            "expression (str, optional), include_geometry (bool, default false)\n"
            "- get_statistics: layer_id (str), field_name (str)\n"
            "- add: layer_id (str), features (list[dict]) - destructive\n"
            "- update: layer_id (str), updates (list[dict]) - destructive\n"
            "- update_geometry: layer_id (str), updates (list[dict], "
            "[{fid, geometry_wkt}]) - destructive\n"
            "- delete: layer_id (str), fids (list[int], optional), expression (str, optional) "
            "- destructive, requires confirmation"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def features(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
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
        elif action == "update_geometry":
            return await _send(
                "update_feature_geometry",
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
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(idempotentHint=True),
    )
    async def selection(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
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
    # 5b. editing
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Editing",
        description=(
            "Vector layer edit sessions. While a session is open, feature add/update/delete "
            "goes to an undoable buffer instead of the data source.\n"
            "Actions: start, commit, rollback, status, undo, redo\n"
            "- start: layer_id (str)\n"
            "- commit: layer_id (str) - writes the buffer to the data source\n"
            "- rollback: layer_id (str) - discards it, requires confirmation\n"
            "- status: layer_id (str)\n"
            "- undo: layer_id (str), steps (int, default 1)\n"
            "- redo: layer_id (str), steps (int, default 1)"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def editing(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
        layer_id = kwargs["layer_id"]
        if action == "start":
            return await _send("start_editing", {"layer_id": layer_id})
        elif action == "commit":
            await ctx.info(f"Committing edits on layer {layer_id}")
            return await _send("commit_edits", {"layer_id": layer_id})
        elif action == "rollback":
            if not await _confirm_destructive(
                ctx, f"Discard all uncommitted edits on layer {layer_id}? This cannot be undone."
            ):
                return {"ok": False, "message": "Cancelled by user"}
            return await _send("rollback_edits", {"layer_id": layer_id})
        elif action == "status":
            return await _send("get_edit_status", {"layer_id": layer_id})
        elif action == "undo":
            return await _send(
                "undo_edits", {"layer_id": layer_id, "steps": kwargs.get("steps", 1)}
            )
        elif action == "redo":
            return await _send(
                "redo_edits", {"layer_id": layer_id, "steps": kwargs.get("steps", 1)}
            )
        else:
            raise ValueError(f"Unknown editing action: {action}")

    # ------------------------------------------------------------------
    # 5c. connection
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Connection",
        description=(
            "Saved data source connections (PostgreSQL, GeoPackage, SpatiaLite, MS SQL, ...) - "
            "the QGIS Browser panel entries.\n"
            "Actions: list, create, list_tables, add_layer, import_layer, execute_sql\n"
            "- list: provider (str, optional filter, e.g. 'postgres', 'ogr')\n"
            "- create: PostgreSQL only, password-free. name (str) and connection_mode (str) are "
            "required; connection_mode selects the other required parameters, ask the user which "
            "applies when unclear: endpoint_using_auth_manager needs host, port (the real database "
            "port, never an assumed 5432), database, auth_config_id; service_using_auth_manager "
            "needs service (pg_service.conf name) and auth_config_id, database optionally overrides "
            "dbname; service_only needs service, database optionally overrides dbname. ssl_mode "
            "(str, default 'prefer': prefer|disable|allow|require|verify-ca|verify-full). Validates "
            "the connection before saving\n"
            "- list_tables: provider (str), connection (str), schema (str, optional - omit on "
            "schema-aware providers to get the schema list first)\n"
            "- add_layer: provider (str), connection (str), table (str) + schema (str, optional), "
            "OR sql (str) for a database-side query layer; geometry_column (str, optional), "
            "primary_key (str, optional), name (str, optional)\n"
            "- import_layer: layer_id (str), provider (str), connection (str), table (str), "
            "schema (str, optional), overwrite (bool, default false) - destructive\n"
            "- execute_sql: provider (str), connection (str), sql (str), limit (int, default 100, "
            "-1 for all) - runs server-side, can modify the database, requires confirmation"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def connection(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list:
        kwargs = params or {}
        if action == "list":
            return await _send("list_connections", {"provider": kwargs.get("provider")})
        elif action == "create":
            return await _send(
                "create_postgresql_connection",
                {
                    "name": kwargs["name"],
                    "connection_mode": kwargs["connection_mode"],
                    "host": kwargs.get("host"),
                    "port": kwargs.get("port"),
                    "database": kwargs.get("database"),
                    "auth_config_id": kwargs.get("auth_config_id"),
                    "ssl_mode": kwargs.get("ssl_mode", "prefer"),
                    "service": kwargs.get("service"),
                },
                timeout=TIMEOUT_LONG,
            )
        elif action == "list_tables":
            return await _send(
                "list_connection_tables",
                {
                    "provider": kwargs["provider"],
                    "connection": kwargs["connection"],
                    "schema": kwargs.get("schema"),
                },
            )
        elif action == "add_layer":
            result = await _send(
                "add_layer_from_connection",
                {
                    "provider": kwargs["provider"],
                    "connection": kwargs["connection"],
                    "table": kwargs.get("table"),
                    "schema": kwargs.get("schema"),
                    "sql": kwargs.get("sql"),
                    "geometry_column": kwargs.get("geometry_column"),
                    "primary_key": kwargs.get("primary_key"),
                    "name": kwargs.get("name"),
                },
                timeout=TIMEOUT_LONG,
            )
            return make_layer_response(result)
        elif action == "import_layer":
            overwrite = kwargs.get("overwrite", False)
            table = kwargs["table"]
            if overwrite and not await _confirm_destructive(
                ctx,
                f"Overwrite table '{table}' in connection '{kwargs['connection']}'? "
                "This cannot be undone.",
            ):
                return {"ok": False, "message": "Cancelled by user"}
            return await _send(
                "import_layer_to_connection",
                {
                    "layer_id": kwargs["layer_id"],
                    "provider": kwargs["provider"],
                    "connection": kwargs["connection"],
                    "table": table,
                    "schema": kwargs.get("schema"),
                    "overwrite": overwrite,
                },
                timeout=TIMEOUT_LONG,
            )
        elif action == "execute_sql":
            sql = kwargs["sql"]
            if not await _confirm_destructive(
                ctx, f"Run SQL on connection '{kwargs['connection']}'?\n\n{sql}"
            ):
                return {"ok": False, "message": "Cancelled by user"}
            return await _send(
                "execute_connection_sql",
                {
                    "provider": kwargs["provider"],
                    "connection": kwargs["connection"],
                    "sql": sql,
                    "limit": kwargs.get("limit", 100),
                },
                timeout=TIMEOUT_LONG,
            )
        else:
            raise ValueError(f"Unknown connection action: {action}")

    # ------------------------------------------------------------------
    # 6. style
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Style",
        description=(
            "Layer symbology.\n"
            "Actions: set, set_raster\n"
            "- set: layer_id (str), style_type (str: 'single', 'categorized', 'graduated'), "
            "field (str, optional - required for categorized/graduated), "
            "classes (int, default 5), color_ramp (str, default 'Spectral')\n"
            "- set_raster: layer_id (str), style_type (str: 'singleband_pseudocolor', "
            "'singleband_gray', 'multiband_color', 'hillshade'), band (int, default 1), "
            "color_ramp (str, default 'Viridis'), classes (int, default 5), "
            "min_value/max_value (float, optional - default to band statistics), "
            "classification (str: continuous|equal_interval|quantile), "
            "interpolation (str: interpolated|discrete|exact), "
            "gradient (str: black_to_white|white_to_black), "
            "contrast (str: none|stretch|clip|stretch_clip), "
            "red_band/green_band/blue_band (int, multiband_color), "
            "azimuth/altitude/z_factor (float, hillshade)"
            f"{_PARAMS_NOTE}"
        ),
    )
    async def style(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
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
        elif action == "set_raster":
            params = {
                "layer_id": kwargs["layer_id"],
                "style_type": kwargs["style_type"],
                "band": kwargs.get("band", 1),
                "color_ramp": kwargs.get("color_ramp", "Viridis"),
                "classes": kwargs.get("classes", 5),
                "min_value": kwargs.get("min_value"),
                "max_value": kwargs.get("max_value"),
                "classification": kwargs.get("classification", "continuous"),
                "interpolation": kwargs.get("interpolation", "interpolated"),
                "gradient": kwargs.get("gradient", "black_to_white"),
                "contrast": kwargs.get("contrast", "stretch"),
                "red_band": kwargs.get("red_band", 1),
                "green_band": kwargs.get("green_band", 2),
                "blue_band": kwargs.get("blue_band", 3),
                "azimuth": kwargs.get("azimuth", 315.0),
                "altitude": kwargs.get("altitude", 45.0),
                "z_factor": kwargs.get("z_factor", 1.0),
            }
            return await _send("set_raster_style", params)
        else:
            raise ValueError(f"Unknown style action: {action}")

    # ------------------------------------------------------------------
    # 7. canvas
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Canvas",
        description=(
            "Map canvas operations.\n"
            "Actions: get_extent, set_extent, screenshot, screenshot_3d, get_scale, set_scale\n"
            "- get_extent: no params\n"
            "- set_extent: xmin (float), ymin (float), xmax (float), ymax (float), "
            "crs (str, optional)\n"
            "- screenshot: no params - returns inline image\n"
            "- screenshot_3d: view_index (int, optional), dpi (int, optional), "
            "pitch (float, optional: 0=top-down, 90=edge-on), distance (float, optional), "
            "heading (float, optional) - capture an open 3D map view as an inline image\n"
            "- get_scale: no params\n"
            "- set_scale: scale (float, optional), rotation (float, optional)"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def canvas(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list:
        kwargs = params or {}
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
        elif action == "screenshot_3d":
            params = {
                k: kwargs[k]
                for k in ("view_index", "dpi", "pitch", "distance", "heading")
                if k in kwargs
            }
            result = await _send("get_3d_screenshot", params)
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
            "Rendering, layout authoring and atlas export.\n"
            "Actions: map, list_layouts, create_layout, get_layout_info, remove_layout, "
            "add_map, add_label, add_legend, add_scalebar, add_picture, add_table, "
            "configure_atlas, export_layout, export_atlas\n"
            "- map: width (int, default 800), height (int, default 600), "
            "path (str, optional) - returns inline image\n"
            "- list_layouts: no params\n"
            "- create_layout: name (str)\n"
            "- get_layout_info: layout_name (str)\n"
            "- remove_layout: layout_name (str) - destructive\n"
            "- add_map: layout_name (str), x, y, width, height (float, mm)\n"
            "- add_label: layout_name (str), text (str), x, y, width, height, font_size (int), color (hex)\n"
            "- add_legend: layout_name (str), map_item_id (str, optional), x, y, width, height, title (str)\n"
            "- add_scalebar: layout_name (str), map_item_id (str, optional), x, y, width, height, style (str)\n"
            "- add_picture: layout_name (str), path (str), x, y, width, height\n"
            "- add_table: layout_name (str), layer_id (str), x, y, width, height, max_rows (int)\n"
            "- configure_atlas: layout_name (str), coverage_layer (str), enabled (bool), "
            "page_name_expression/filter_expression/sort_expression (str, optional)\n"
            "- export_layout: layout_name (str), path (str), format (str, default 'pdf'), dpi (int, default 300)\n"
            "- export_atlas: layout_name (str), output_path (str), format (str, default 'pdf'), dpi (int, default 300)"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(idempotentHint=True),
    )
    async def render(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list:
        kwargs = params or {}
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
        elif action == "list_layouts":
            return await _send("list_layouts")
        elif action == "create_layout":
            return await _send("create_layout", {"name": kwargs["name"]})
        elif action == "get_layout_info":
            return await _send("get_layout_info", {"layout_name": kwargs["layout_name"]})
        elif action == "remove_layout":
            name = kwargs["layout_name"]
            if not await _confirm_destructive(ctx, f"Remove layout '{name}'?"):
                return {"ok": False, "message": "Cancelled by user"}
            return await _send("remove_layout", {"layout_name": name})
        elif action in _LAYOUT_ITEM_COMMANDS:
            return await _send(_LAYOUT_ITEM_COMMANDS[action], kwargs)
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
        elif action == "export_atlas":
            await ctx.info(f"Exporting atlas '{kwargs['layout_name']}'")
            return await _send(
                "export_atlas",
                {
                    "layout_name": kwargs["layout_name"],
                    "output_path": kwargs["output_path"],
                    "format": kwargs.get("format", "pdf"),
                    "dpi": kwargs.get("dpi", 300),
                },
                timeout=TIMEOUT_LONG,
            )
        else:
            raise ValueError(f"Unknown render action: {action}")

    # ------------------------------------------------------------------
    # 9. processing
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Processing",
        description=(
            "QGIS Processing framework.\n"
            "Actions: execute, execute_batch, list_algorithms, get_help, get_providers, "
            "create_model, list_models, run_model\n"
            "- execute: algorithm (str), parameters (dict)\n"
            "- execute_batch: algorithm (str), parameters_list (list[dict]), timeout (int, optional, "
            "seconds for the whole batch, default 55) - one run per dict, per-run "
            "success/error/skipped status\n"
            "- list_algorithms: search (str, optional), provider (str, optional)\n"
            "- get_help: algorithm_id (str)\n"
            "- get_providers: no params - providers with algorithm counts and active status\n"
            "- list_models: no params - registered Processing models (id, name, group)\n"
            "- run_model: model (str: registered id like 'model:myflow', or a .model3 path), "
            "parameters (dict, optional) mapping the model's input names to values; missing "
            "output/sink parameters default to a temporary layer\n"
            "- create_model: name (str), steps (list[dict]), inputs (list[dict], optional), "
            "outputs (list[dict], optional), description (str, optional), group (str, optional).\n"
            "    inputs: [{name, type, description?, default?, optional?, parent_layer? (field/distance), "
            "options? (enum)}]. Types: vector, feature_source, raster, field, number, integer, distance, "
            "string, boolean, extent, crs, point, file, folder, enum, multiple_layers.\n"
            "    steps: [{id, algorithm, description?, parameters: {ALG_PARAM: value}}] - 'id' is REQUIRED "
            "and must be unique; 'algorithm' takes a keyword ('buffer') or a full id ('native:buffer').\n"
            "    step parameter values: '@input_name' = model input, '$step_id.OUTPUT' = earlier step "
            "output, '=expression' = QGIS expression, anything else = static literal.\n"
            "    outputs: [{name, from_step, from_output, description?}]; omit to expose the last step's "
            "OUTPUT as 'Result'.\n"
            "    The model is saved into the QGIS user models folder and registered; a numeric suffix is "
            "appended to the name on collision."
            f"{_PARAMS_NOTE}"
        ),
    )
    async def processing(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
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
        elif action == "execute_batch":
            runs = kwargs["parameters_list"]
            await ctx.info(f"Batch processing {kwargs['algorithm']}: {len(runs)} run(s)")
            params = {"algorithm": kwargs["algorithm"], "parameters_list": runs}
            socket_timeout = TIMEOUT_LONG
            if kwargs.get("timeout") is not None:
                params["timeout"] = kwargs["timeout"]
                socket_timeout = int(kwargs["timeout"]) + 5
            return await _send("execute_processing_batch", params, timeout=socket_timeout)
        elif action == "list_algorithms":
            params = {}
            if "search" in kwargs:
                params["search"] = kwargs["search"]
            if "provider" in kwargs:
                params["provider"] = kwargs["provider"]
            return await _send("list_processing_algorithms", params)
        elif action == "get_providers":
            return await _send("get_processing_providers")
        elif action == "list_models":
            return await _send("list_processing_models")
        elif action == "run_model":
            await ctx.info(f"Running model: {kwargs['model']}")
            await ctx.report_progress(0, 100)
            result = await _send(
                "run_model",
                {"model": kwargs["model"], "parameters": kwargs.get("parameters") or {}},
                timeout=TIMEOUT_LONG,
            )
            await ctx.report_progress(100, 100)
            return result
        elif action == "get_help":
            return await _send("get_algorithm_help", {"algorithm_id": kwargs["algorithm_id"]})
        elif action == "create_model":
            await ctx.info(
                f"Building Processing model: {kwargs['name']} ({len(kwargs['steps'])} step(s))"
            )
            params = {
                "name": kwargs["name"],
                "steps": kwargs["steps"],
                "description": kwargs.get("description", ""),
                "group": kwargs.get("group", "Models"),
            }
            for key in ("inputs", "outputs"):
                if key in kwargs and kwargs[key] is not None:
                    params[key] = kwargs[key]
            return await _send("create_processing_model", params, timeout=TIMEOUT_LONG)
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
            "- execute: code (str), timeout (int, optional, seconds before the script is cancelled, "
            "default 55; output so far is returned) - destructive, requires confirmation"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def code(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
        if action == "execute":
            if not await _confirm_destructive(
                ctx, "Execute arbitrary PyQGIS code? This can modify your project and system."
            ):
                return {"ok": False, "message": "Cancelled by user"}
            await ctx.info("Executing PyQGIS code...")
            await ctx.report_progress(0, 100)
            params = {"code": kwargs["code"]}
            socket_timeout = TIMEOUT_LONG
            if kwargs.get("timeout") is not None:
                params["timeout"] = kwargs["timeout"]
                socket_timeout = int(kwargs["timeout"]) + 5
            result = await _send("execute_code", params, timeout=socket_timeout)
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
            "- execute: commands (list[dict]) - each {'type': '<command>', 'params': {...}}. "
            "Destructive commands (execute_code, remove_layer, delete_features, set_setting, "
            "reload_plugin) are not allowed in batch."
            f"{_PARAMS_NOTE}"
        ),
    )
    async def batch(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list:
        kwargs = params or {}
        if action == "execute":
            commands = kwargs["commands"]
            for cmd in commands:
                cmd_type = cmd.get("type", "")
                if cmd_type in BATCH_BLOCKED_COMMANDS:
                    raise ValueError(
                        f"Command {cmd_type!r} is not allowed in batch - "
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
            f"{_PARAMS_NOTE}"
        ),
    )
    async def layer_tree(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
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
            "- reload: plugin_name (str) - destructive"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def plugins(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
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
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(idempotentHint=True),
    )
    async def variables(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
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
            "- set: key (str), value (str) - destructive, requires confirmation"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def settings(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
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
            "Expression validation and evaluation.\n"
            "Actions: validate, evaluate\n"
            "- validate: expression (str), layer_id (str, optional)\n"
            "- evaluate: expression (str), layer_id (str, optional) - returns scalar result"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=True,
    )
    async def expression(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
        if action in ("validate", "evaluate"):
            params = {"expression": kwargs["expression"]}
            if "layer_id" in kwargs:
                params["layer_id"] = kwargs["layer_id"]
            command = "validate_expression" if action == "validate" else "evaluate_expression"
            return await _send(command, params)
        else:
            raise ValueError(f"Unknown expression action: {action}")

    @mcp.tool(
        title="Query",
        description=(
            "Cross-layer query.\n"
            "Actions: sql, identify\n"
            "- sql: query (str), layers (list[str], optional), as_layer (bool, default false), "
            "layer_name (str), geometry_field (str, optional), uid_field (str, optional)\n"
            "- identify: point (list[float] [x,y]), tolerance (float, default 0), "
            "layer_ids (list[str], optional), limit (int, default 10)"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def query(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
        if action == "sql":
            params: dict[str, Any] = {"query": kwargs["query"]}
            for key in ("layers", "as_layer", "layer_name", "geometry_field", "uid_field"):
                if key in kwargs:
                    params[key] = kwargs[key]
            return await _send("execute_sql", params, timeout=TIMEOUT_LONG)
        elif action == "identify":
            params = {"point": kwargs["point"]}
            for key in ("tolerance", "layer_ids", "limit"):
                if key in kwargs:
                    params[key] = kwargs[key]
            return await _send("identify_features", params)
        else:
            raise ValueError(f"Unknown query action: {action}")

    @mcp.tool(
        title="Transform",
        description=(
            "CRS coordinate transformation.\n"
            "Actions: coordinates\n"
            "- coordinates: source_crs (str), target_crs (str), point (dict, optional), "
            "points (list[dict], optional), bbox (dict, optional)"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=True,
    )
    async def transform(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
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
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=True,
    )
    async def message_log(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
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
            "- set: layer_id (str), property (str), value (str) - "
            "supported: opacity, name, min_scale, max_scale, scale_visibility"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(idempotentHint=True),
    )
    async def layer_property(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
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
    # 19b. field - schema and attribute editing
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Field",
        description=(
            "Vector field (attribute column) management.\n"
            "Actions: add, delete, rename, calculate, unique_values\n"
            "- add: layer_id (str), field_name (str), field_type (str: 'string', 'int', "
            "'double', 'bool', 'date', 'datetime'), length (int, optional), "
            "precision (int, optional)\n"
            "- delete: layer_id (str), field_name (str) - destructive, requires confirmation\n"
            "- rename: layer_id (str), old_name (str), new_name (str)\n"
            "- calculate: layer_id (str), field_name (str), expression (str), "
            "field_type (str, default 'double'), length (int, default 0), "
            "precision (int, default 0) - creates the field if missing, then populates it\n"
            "- unique_values: layer_id (str), field (str), limit (int, default 1000, -1 for all)"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def field(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
        if action == "add":
            params = {
                "layer_id": kwargs["layer_id"],
                "field_name": kwargs["field_name"],
                "field_type": kwargs["field_type"],
            }
            for key in ("length", "precision"):
                if kwargs.get(key) is not None:
                    params[key] = kwargs[key]
            return await _send("add_field", params)
        elif action == "delete":
            field_name = kwargs["field_name"]
            layer_id = kwargs["layer_id"]
            if not await _confirm_destructive(
                ctx, f"Delete field '{field_name}' from layer {layer_id}?"
            ):
                return {"ok": False, "message": "Cancelled by user"}
            return await _send("delete_field", {"layer_id": layer_id, "field_name": field_name})
        elif action == "rename":
            return await _send(
                "rename_field",
                {
                    "layer_id": kwargs["layer_id"],
                    "old_name": kwargs["old_name"],
                    "new_name": kwargs["new_name"],
                },
            )
        elif action == "calculate":
            return await _send(
                "field_calculator",
                {
                    "layer_id": kwargs["layer_id"],
                    "field_name": kwargs["field_name"],
                    "expression": kwargs["expression"],
                    "field_type": kwargs.get("field_type", "double"),
                    "length": kwargs.get("length", 0),
                    "precision": kwargs.get("precision", 0),
                },
            )
        elif action == "unique_values":
            return await _send(
                "get_unique_values",
                {
                    "layer_id": kwargs["layer_id"],
                    "field": kwargs["field"],
                    "limit": kwargs.get("limit", 1000),
                },
            )
        else:
            raise ValueError(f"Unknown field action: {action}")

    # ------------------------------------------------------------------
    # 19c. analysis - vector/raster analysis operations
    # ------------------------------------------------------------------

    @mcp.tool(
        title="Analysis",
        description=(
            "Vector and raster analysis.\n"
            "Actions: spatial_join, zonal_statistics, raster_calculator, sample_raster\n"
            "- spatial_join: target_layer (str), join_layer (str), predicates (list[int], "
            "default [0]: 0=intersects 1=contains 2=equals 3=touches 4=overlaps 5=within "
            "6=crosses), join_fields (list[str], optional - default all), method (int, "
            "default 1: 0=one-to-many 1=first match 2=largest overlap), prefix (str, "
            "default ''), output_path (str, optional - omit for an in-memory layer)\n"
            "- zonal_statistics: polygon_layer (str), raster_layer (str), band (int, default 1), "
            "prefix (str, default '_'), stats (list[int], default [0,1,2]: 0=count 1=sum 2=mean "
            "3=median 4=stdev 5=min 6=max 7=range 8=minority 9=majority 10=variety 11=variance), "
            "output_path (str, optional - omit for an in-memory layer)\n"
            "- raster_calculator: expression (str, reference bands as 'LayerName@band'), "
            "output_path (str, GeoTIFF), reference_layer (str, optional - grid/extent source)\n"
            "- sample_raster: raster_layer (str), points (list[[x, y]] in the raster CRS), "
            "band (int, optional - omit to sample all bands)"
            f"{_PARAMS_NOTE}"
        ),
    )
    async def analysis(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
        if action == "spatial_join":
            await ctx.info("Joining attributes by location...")
            return await _send(
                "spatial_join",
                {
                    "target_layer": kwargs["target_layer"],
                    "join_layer": kwargs["join_layer"],
                    "predicates": kwargs.get("predicates"),
                    "join_fields": kwargs.get("join_fields"),
                    "method": kwargs.get("method", 1),
                    "prefix": kwargs.get("prefix", ""),
                    "output_path": kwargs.get("output_path"),
                },
                timeout=TIMEOUT_LONG,
            )
        elif action == "zonal_statistics":
            await ctx.info("Computing zonal statistics...")
            return await _send(
                "zonal_statistics",
                {
                    "polygon_layer": kwargs["polygon_layer"],
                    "raster_layer": kwargs["raster_layer"],
                    "band": kwargs.get("band", 1),
                    "prefix": kwargs.get("prefix", "_"),
                    "stats": kwargs.get("stats"),
                    "output_path": kwargs.get("output_path"),
                },
                timeout=TIMEOUT_LONG,
            )
        elif action == "raster_calculator":
            await ctx.info("Computing raster expression...")
            return await _send(
                "raster_calculator",
                {
                    "expression": kwargs["expression"],
                    "output_path": kwargs["output_path"],
                    "reference_layer": kwargs.get("reference_layer"),
                },
                timeout=TIMEOUT_LONG,
            )
        elif action == "sample_raster":
            return await _send(
                "sample_raster_values",
                {
                    "raster_layer": kwargs["raster_layer"],
                    "points": kwargs["points"],
                    "band": kwargs.get("band"),
                },
            )
        else:
            raise ValueError(f"Unknown analysis action: {action}")

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
            "- remove: bookmark_id (str) - destructive"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def bookmarks(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
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
            "- add: name (str) - saves current visibility state\n"
            "- remove: name (str) - destructive\n"
            "- apply: name (str) - restores saved visibility state"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def map_themes(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
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
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(idempotentHint=True),
    )
    async def active_layer(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        kwargs = params or {}
        if action == "get":
            return await _send("get_active_layer")
        elif action == "set":
            return await _send("set_active_layer", {"layer_id": kwargs["layer_id"]})
        else:
            raise ValueError(f"Unknown active_layer action: {action}")
