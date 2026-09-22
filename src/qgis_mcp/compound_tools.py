"""Compound tool registrations for QGIS MCP.

When QGIS_MCP_TOOL_MODE=compound, these 27 grouped tools replace the
granular tools, reducing context window overhead for LLMs with limited tool
slots.

Each compound tool takes an ``action`` string as its first parameter and
dispatches to the same ``_send()`` logic used by the granular tools.
"""

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

try:
    from mcp.server.fastmcp import Context, FastMCP
except ModuleNotFoundError:  # mcp >= 2.0 renamed fastmcp -> mcpserver
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver import MCPServer as FastMCP
try:
    from mcp.server.fastmcp.exceptions import ToolError
except ImportError:  # mcp >= 2.0; only ToolError text reaches the client on mcp >= 2.1
    from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import Annotations, ImageContent, ToolAnnotations

from qgis_mcp.helpers import (
    BATCH_BLOCKED_COMMANDS,
    TIMEOUT_LONG,
    code_failure_message,
    enrich_diagnose,
    feature_limit_error,
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


#: An action handler takes the request context and the caller's ``params`` dict.
_Action = Callable[[Context, dict[str, Any]], Awaitable[Any]]


class _Params(dict):
    """An action's params, where a missing or unused key is a ToolError.

    Handlers read required params as ``kwargs["x"]``; a bare KeyError reached
    the client as "Error executing tool <name>: 'x'", and mcp >= 2.1 masks
    anything that is not a ToolError entirely.

    Every lookup is recorded, so a key the handler never looked at - a typo
    such as "expresion" - can be refused (:meth:`check_all_read`) instead of
    silently dropped, which returned unfiltered results as if they were the
    answer. Granular mode refuses such keys in the plugin; compound handlers
    build their own payloads, so they never got that far.
    """

    def __init__(self, group: str, action: str, params: dict):
        super().__init__(params)
        self._where = f"{group} action '{action}'"
        self._read: set = set()

    def __getitem__(self, key):
        self._read.add(key)
        return super().__getitem__(key)

    def __missing__(self, key):
        raise ToolError(f"{self._where}: missing required parameter '{key}'")

    def get(self, key, default=None):
        self._read.add(key)
        return super().get(key, default)

    def __contains__(self, key):
        self._read.add(key)
        return super().__contains__(key)

    def forwarded(self) -> dict:
        """All params as a plain dict, for a handler that passes them through.

        The plugin then validates them against the command's signature itself.
        """
        self._read.update(self)
        return dict(self)

    def check_all_read(self):
        unused = sorted(set(self) - self._read)
        if unused:
            raise ToolError(
                f"{self._where}: unknown parameter(s) {unused}; "
                "see the tool description for the ones it takes"
            )


# The params of the action being dispatched, for the pre-send check below.
_current_params: ContextVar[_Params | None] = ContextVar("_current_params", default=None)


async def _dispatch(
    group: str, actions: dict[str, _Action], ctx: Context, action: str, params: dict | None
) -> Any:
    """Run the handler *action* names in *actions*, or report an unknown action.

    One lookup for every group, so the "Unknown action" message is written once
    rather than at the tail of each dispatch chain.
    """
    handler = actions.get(action)
    if handler is None:
        raise ToolError(f"Unknown {group} action: {action}")
    token = _current_params.set(_Params(group, action, params or {}))
    try:
        return await handler(ctx, _current_params.get())
    finally:
        _current_params.reset(token)


def _checked_send(send):
    """*send*, refusing first when the action left a parameter unread.

    Every handler reads its params while building the payload and sends once,
    so by the time it sends, a key it never looked at is one it ignores. The
    check runs before the command reaches QGIS, never after it has acted.
    """

    async def checked(*args, **kwargs):
        params = _current_params.get()
        if params is not None:
            params.check_all_read()
        return await send(*args, **kwargs)

    return checked


def register_compound_tools(mcp: FastMCP, _send, _confirm_destructive):  # noqa: C901
    """Register compound tools on the MCP server instance.

    One registration function by design: every handler closes over *_send* and
    *_confirm_destructive*. C901 is silenced because it charges this function
    for the branches of the nested handlers as well, and those are already as
    small as each action allows.
    """
    _send = _checked_send(_send)

    # ------------------------------------------------------------------
    # 1. system
    # ------------------------------------------------------------------

    async def system_diagnose(ctx, kwargs):
        await ctx.info("Running diagnostics...")
        return enrich_diagnose(await _send("diagnose"))

    system_actions: dict[str, _Action] = {
        "ping": lambda ctx, kwargs: _send("ping"),
        "diagnose": system_diagnose,
        "get_qgis_info": lambda ctx, kwargs: _send("get_qgis_info"),
    }

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
        return await _dispatch("system", system_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 2. project
    # ------------------------------------------------------------------

    async def project_load(ctx, kwargs):
        path = kwargs["path"]
        await ctx.info(f"Loading project: {path}")
        return make_project_response(await _send("load_project", {"path": path}))

    async def project_create(ctx, kwargs):
        return make_project_response(await _send("create_new_project", {"path": kwargs["path"]}))

    async def project_save(ctx, kwargs):
        payload = {}
        if "path" in kwargs:
            payload["path"] = kwargs["path"]
        return await _send("save_project", payload)

    async def project_set_crs(ctx, kwargs):
        return make_project_response(await _send("set_project_crs", {"crs": kwargs["crs"]}))

    async def project_create_checkpoint(ctx, kwargs):
        payload = {"name": kwargs["name"]} if kwargs.get("name") else {}
        return await _send("create_checkpoint", payload, timeout=TIMEOUT_LONG)

    async def project_export_session(ctx, kwargs):
        payload = {}
        if kwargs.get("path"):
            payload["path"] = kwargs["path"]
        if kwargs.get("clear"):
            payload["clear"] = True
        return await _send("export_session", payload)

    project_actions: dict[str, _Action] = {
        "get_info": lambda ctx, kwargs: _send("get_project_info"),
        "load": project_load,
        "create": project_create,
        "save": project_save,
        "set_crs": project_set_crs,
        "create_checkpoint": project_create_checkpoint,
        "list_checkpoints": lambda ctx, kwargs: _send("list_checkpoints"),
        "restore_checkpoint": lambda ctx, kwargs: _send(
            "restore_checkpoint", {"checkpoint_id": kwargs["checkpoint_id"]}, timeout=TIMEOUT_LONG
        ),
        "export_session": project_export_session,
    }

    @mcp.tool(
        title="Project",
        description=(
            "Project management.\n"
            "Actions: get_info, load, create, save, set_crs, create_checkpoint, "
            "list_checkpoints, restore_checkpoint, export_session\n"
            "- get_info: no params\n"
            "- load: path (str)\n"
            "- create: path (str)\n"
            "- save: path (str, optional)\n"
            "- set_crs: crs (str)\n"
            "- create_checkpoint: name (str, optional) - snapshot the whole project, memory "
            "layer features included, to undo a sequence of changes; uncommitted edits and "
            "file/database data are not held\n"
            "- list_checkpoints: no params\n"
            "- restore_checkpoint: checkpoint_id (str) - discard every change since it\n"
            "- export_session: path (str, optional), clear (bool, optional) - the commands "
            "that changed something, as a Python script replaying them from the QGIS console\n"
            "load, create and restore_checkpoint replace the open project; unsaved changes "
            "to it are lost."
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
        structured_output=True,
    )
    async def project(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list:
        return await _dispatch("project", project_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 3. layer
    # ------------------------------------------------------------------

    async def layer_add_vector(ctx, kwargs):
        payload = {"path": kwargs["path"], "provider": kwargs.get("provider", "ogr")}
        if "name" in kwargs:
            payload["name"] = kwargs["name"]
        return make_layer_response(await _send("add_vector_layer", payload))

    async def layer_add_raster(ctx, kwargs):
        payload = {"path": kwargs["path"], "provider": kwargs.get("provider", "gdal")}
        if "name" in kwargs:
            payload["name"] = kwargs["name"]
        return make_layer_response(await _send("add_raster_layer", payload))

    async def layer_remove(ctx, kwargs):
        layer_id = kwargs["layer_id"]
        if not await _confirm_destructive(ctx, f"Remove layer {layer_id}? This cannot be undone."):
            return {"ok": False, "message": "Cancelled by user"}
        return await _send("remove_layer", {"layer_id": layer_id})

    async def layer_create_memory(ctx, kwargs):
        payload = {
            "name": kwargs["name"],
            "geometry_type": kwargs["geometry_type"],
            "crs": kwargs.get("crs", "EPSG:4326"),
        }
        if "fields" in kwargs:
            payload["fields"] = kwargs["fields"]
        result = await _send("create_memory_layer", payload)
        return make_layer_response(result, fallback_name=kwargs["name"])

    async def layer_set_labeling(ctx, kwargs):
        payload: dict[str, Any] = {
            "layer_id": kwargs["layer_id"],
            "enabled": kwargs.get("enabled", True),
        }
        for key in ("field_name", "font_size", "color"):
            if key in kwargs:
                payload[key] = kwargs[key]
        return await _send("set_layer_labeling", payload)

    async def layer_duplicate(ctx, kwargs):
        payload: dict[str, Any] = {"layer_id": kwargs["layer_id"]}
        if "new_name" in kwargs:
            payload["new_name"] = kwargs["new_name"]
        return make_layer_response(await _send("duplicate_layer", payload))

    async def layer_add_web(ctx, kwargs):
        payload: dict[str, Any] = {"url": kwargs["url"], "service": kwargs["service"]}
        for key in ("crs", "name"):
            if kwargs.get(key):
                payload[key] = kwargs[key]
        return make_layer_response(await _send("add_web_layer", payload))

    async def layer_export(ctx, kwargs):
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

    layer_actions: dict[str, _Action] = {
        "list": lambda ctx, kwargs: _send(
            "get_layers",
            {"limit": kwargs.get("limit", 50), "offset": kwargs.get("offset", 0)},
        ),
        "add_vector": layer_add_vector,
        "add_raster": layer_add_raster,
        "remove": layer_remove,
        "find": lambda ctx, kwargs: _send("find_layer", {"name_pattern": kwargs["name_pattern"]}),
        "create_memory": layer_create_memory,
        "set_visibility": lambda ctx, kwargs: _send(
            "set_layer_visibility",
            {"layer_id": kwargs["layer_id"], "visible": kwargs["visible"]},
        ),
        "zoom_to": lambda ctx, kwargs: _send("zoom_to_layer", {"layer_id": kwargs["layer_id"]}),
        "get_info": lambda ctx, kwargs: _send("get_layer_info", {"layer_id": kwargs["layer_id"]}),
        "get_schema": lambda ctx, kwargs: _send(
            "get_layer_schema", {"layer_id": kwargs["layer_id"]}
        ),
        "get_extent": lambda ctx, kwargs: _send(
            "get_layer_extent", {"layer_id": kwargs["layer_id"]}
        ),
        "get_raster_info": lambda ctx, kwargs: _send(
            "get_raster_info", {"layer_id": kwargs["layer_id"]}
        ),
        "get_crs": lambda ctx, kwargs: _send("get_layer_crs", {"layer_id": kwargs["layer_id"]}),
        "set_crs": lambda ctx, kwargs: _send(
            "set_layer_crs", {"layer_id": kwargs["layer_id"], "crs": kwargs["crs"]}
        ),
        "get_labeling": lambda ctx, kwargs: _send(
            "get_layer_labeling", {"layer_id": kwargs["layer_id"]}
        ),
        "set_labeling": layer_set_labeling,
        "duplicate": layer_duplicate,
        "set_order": lambda ctx, kwargs: _send(
            "set_layer_order", {"layer_ids": kwargs["layer_ids"]}
        ),
        "add_web": layer_add_web,
        "export": layer_export,
        "save_style": lambda ctx, kwargs: _send(
            "save_style_qml", {"layer_id": kwargs["layer_id"], "path": kwargs["path"]}
        ),
        "apply_style": lambda ctx, kwargs: _send(
            "apply_style_qml", {k: kwargs[k] for k in ("layer_id", "path", "qml") if k in kwargs}
        ),
        "add_join": lambda ctx, kwargs: _send(
            "add_table_join",
            {
                "target_layer_id": kwargs["target_layer_id"],
                "join_layer_id": kwargs["join_layer_id"],
                "target_field": kwargs["target_field"],
                "join_field": kwargs["join_field"],
                "prefix": kwargs.get("prefix", ""),
            },
        ),
    }

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
            "- apply_style: layer_id (str), path (str) or qml (str, inline QML text) - "
            "apply a style; the previous one is restored if QGIS loads another renderer\n"
            "- add_join: target_layer_id (str), join_layer_id (str), target_field (str), "
            "join_field (str), prefix (str, default '')"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def layer(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list:
        return await _dispatch("layer", layer_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 4. features
    # ------------------------------------------------------------------

    async def features_get(ctx, kwargs):
        if refusal := feature_limit_error(kwargs.get("limit", 10)):
            raise ToolError(refusal)
        payload = {
            "layer_id": kwargs["layer_id"],
            "limit": kwargs.get("limit", 10),
            "offset": kwargs.get("offset", 0),
            "include_geometry": kwargs.get("include_geometry", False),
        }
        if "expression" in kwargs:
            payload["expression"] = kwargs["expression"]
        return await _send("get_layer_features", payload)

    async def features_delete(ctx, kwargs):
        layer_id = kwargs["layer_id"]
        fids = kwargs.get("fids")
        expression = kwargs.get("expression")
        target = f"fids={fids}" if fids is not None else f"expression='{expression}'"
        if not await _confirm_destructive(
            ctx, f"Delete features from layer {layer_id} ({target})?"
        ):
            return {"ok": False, "message": "Cancelled by user"}
        payload: dict[str, Any] = {"layer_id": layer_id}
        if fids is not None:
            payload["fids"] = fids
        if expression:
            payload["expression"] = expression
        return await _send("delete_features", payload)

    features_actions: dict[str, _Action] = {
        "get": features_get,
        "get_statistics": lambda ctx, kwargs: _send(
            "get_field_statistics",
            {"layer_id": kwargs["layer_id"], "field_name": kwargs["field_name"]},
        ),
        "add": lambda ctx, kwargs: _send(
            "add_features", picked(kwargs, ("layer_id", "features"), ("crs",))
        ),
        "update": lambda ctx, kwargs: _send(
            "update_features", {"layer_id": kwargs["layer_id"], "updates": kwargs["updates"]}
        ),
        "update_geometry": lambda ctx, kwargs: _send(
            "update_feature_geometry",
            {"layer_id": kwargs["layer_id"], "updates": kwargs["updates"]},
        ),
        "delete": features_delete,
    }

    @mcp.tool(
        title="Features",
        description=(
            "Feature access and editing.\n"
            "Actions: get, get_statistics, add, update, update_geometry, delete\n"
            "- get: layer_id (str), limit (int, default 10, max 50), offset (int, default 0), "
            "expression (str, optional), include_geometry (bool, default false)\n"
            "- get_statistics: layer_id (str), field_name (str)\n"
            "- add: layer_id (str), features (list[dict]), crs (str, optional - of "
            "geometry_wkt, default the layer CRS) - destructive\n"
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
        return await _dispatch("features", features_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 5. selection
    # ------------------------------------------------------------------

    async def selection_select(ctx, kwargs):
        payload: dict[str, Any] = {"layer_id": kwargs["layer_id"]}
        for key in ("expression", "fids"):
            if key in kwargs:
                payload[key] = kwargs[key]
        return await _send("select_features", payload)

    selection_actions: dict[str, _Action] = {
        "select": selection_select,
        "get": lambda ctx, kwargs: _send("get_selection", {"layer_id": kwargs["layer_id"]}),
        "clear": lambda ctx, kwargs: _send("clear_selection", {"layer_id": kwargs["layer_id"]}),
    }

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
        return await _dispatch("selection", selection_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 5b. editing
    # ------------------------------------------------------------------

    async def editing_commit(ctx, kwargs):
        layer_id = kwargs["layer_id"]
        await ctx.info(f"Committing edits on layer {layer_id}")
        return await _send("commit_edits", {"layer_id": layer_id})

    async def editing_rollback(ctx, kwargs):
        layer_id = kwargs["layer_id"]
        if not await _confirm_destructive(
            ctx, f"Discard all uncommitted edits on layer {layer_id}? This cannot be undone."
        ):
            return {"ok": False, "message": "Cancelled by user"}
        return await _send("rollback_edits", {"layer_id": layer_id})

    editing_actions: dict[str, _Action] = {
        "start": lambda ctx, kwargs: _send("start_editing", {"layer_id": kwargs["layer_id"]}),
        "commit": editing_commit,
        "rollback": editing_rollback,
        "status": lambda ctx, kwargs: _send("get_edit_status", {"layer_id": kwargs["layer_id"]}),
        "undo": lambda ctx, kwargs: _send(
            "undo_edits", {"layer_id": kwargs["layer_id"], "steps": kwargs.get("steps", 1)}
        ),
        "redo": lambda ctx, kwargs: _send(
            "redo_edits", {"layer_id": kwargs["layer_id"], "steps": kwargs.get("steps", 1)}
        ),
    }

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
        return await _dispatch("editing", editing_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 5c. connection
    # ------------------------------------------------------------------

    async def connection_add_layer(ctx, kwargs):
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

    async def connection_import_layer(ctx, kwargs):
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

    async def connection_execute_sql(ctx, kwargs):
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

    connection_actions: dict[str, _Action] = {
        "list": lambda ctx, kwargs: _send("list_connections", {"provider": kwargs.get("provider")}),
        "create": lambda ctx, kwargs: _send(
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
        ),
        "list_tables": lambda ctx, kwargs: _send(
            "list_connection_tables",
            {
                "provider": kwargs["provider"],
                "connection": kwargs["connection"],
                "schema": kwargs.get("schema"),
            },
        ),
        "add_layer": connection_add_layer,
        "import_layer": connection_import_layer,
        "execute_sql": connection_execute_sql,
    }

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
        return await _dispatch("connection", connection_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 6. style
    # ------------------------------------------------------------------

    async def style_set(ctx, kwargs):
        payload = {
            "layer_id": kwargs["layer_id"],
            "style_type": kwargs["style_type"],
            "classes": kwargs.get("classes", 5),
            "color_ramp": kwargs.get("color_ramp", "Spectral"),
        }
        if "field" in kwargs:
            payload["field"] = kwargs["field"]
        return await _send("set_layer_style", payload)

    style_actions: dict[str, _Action] = {
        "set": style_set,
        "set_raster": lambda ctx, kwargs: _send(
            "set_raster_style",
            {
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
            },
        ),
    }

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
        return await _dispatch("style", style_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 7. canvas
    # ------------------------------------------------------------------

    def inline_png(result):
        return [
            ImageContent(
                type="image",
                data=result["base64_data"],
                mimeType="image/png",
                annotations=Annotations(audience=["user", "assistant"], priority=1.0),
            )
        ]

    async def canvas_set_extent(ctx, kwargs):
        payload = {
            "xmin": kwargs["xmin"],
            "ymin": kwargs["ymin"],
            "xmax": kwargs["xmax"],
            "ymax": kwargs["ymax"],
        }
        if "crs" in kwargs:
            payload["crs"] = kwargs["crs"]
        return await _send("set_canvas_extent", payload)

    async def canvas_screenshot(ctx, kwargs):
        return inline_png(await _send("get_canvas_screenshot"))

    async def canvas_screenshot_3d(ctx, kwargs):
        payload = {
            key: kwargs[key]
            for key in ("view_index", "dpi", "pitch", "distance", "heading")
            if key in kwargs
        }
        return inline_png(await _send("get_3d_screenshot", payload))

    async def canvas_set_scale(ctx, kwargs):
        payload: dict[str, Any] = {}
        for key in ("scale", "rotation"):
            if key in kwargs:
                payload[key] = kwargs[key]
        return await _send("set_canvas_scale", payload)

    canvas_actions: dict[str, _Action] = {
        "get_extent": lambda ctx, kwargs: _send("get_canvas_extent"),
        "set_extent": canvas_set_extent,
        "screenshot": canvas_screenshot,
        "screenshot_3d": canvas_screenshot_3d,
        "get_scale": lambda ctx, kwargs: _send("get_canvas_scale"),
        "set_scale": canvas_set_scale,
    }

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
        return await _dispatch("canvas", canvas_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 8. render
    # ------------------------------------------------------------------

    async def render_map(ctx, kwargs):
        await ctx.info("Rendering map...")
        await ctx.report_progress(0, 100)
        payload = {"width": kwargs.get("width", 800), "height": kwargs.get("height", 600)}
        path = kwargs.get("path")
        if path:
            payload["path"] = path
        result = await _send("render_map_base64", payload, timeout=TIMEOUT_LONG)
        await ctx.report_progress(100, 100)
        return make_render_response(result, payload["width"], payload["height"], path)

    async def render_remove_layout(ctx, kwargs):
        name = kwargs["layout_name"]
        if not await _confirm_destructive(ctx, f"Remove layout '{name}'?"):
            return {"ok": False, "message": "Cancelled by user"}
        return await _send("remove_layout", {"layout_name": name})

    async def render_export_atlas(ctx, kwargs):
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

    render_actions: dict[str, _Action] = {
        "map": render_map,
        "list_layouts": lambda ctx, kwargs: _send("list_layouts"),
        "create_layout": lambda ctx, kwargs: _send("create_layout", {"name": kwargs["name"]}),
        "get_layout_info": lambda ctx, kwargs: _send(
            "get_layout_info", {"layout_name": kwargs["layout_name"]}
        ),
        "remove_layout": render_remove_layout,
        "export_layout": lambda ctx, kwargs: _send(
            "export_layout",
            {
                "layout_name": kwargs["layout_name"],
                "path": kwargs["path"],
                "format": kwargs.get("format", "pdf"),
                "dpi": kwargs.get("dpi", 300),
            },
        ),
        "export_atlas": render_export_atlas,
        # Layout items take the caller's params as-is, so one entry per command;
        # the plugin refuses any the command does not take.
        **{
            item_action: (lambda ctx, kwargs, command=command: _send(command, kwargs.forwarded()))
            for item_action, command in _LAYOUT_ITEM_COMMANDS.items()
        },
    }

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
        return await _dispatch("render", render_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 9. processing
    # ------------------------------------------------------------------

    def with_timeout(payload, kwargs):
        """Copy the caller's timeout onto *payload* and return the socket timeout.

        The socket waits 5s longer than the plugin-side deadline so the plugin
        fails first, with a real message.
        """
        if kwargs.get("timeout") is None:
            return TIMEOUT_LONG
        payload["timeout"] = kwargs["timeout"]
        return int(kwargs["timeout"]) + 5

    async def processing_execute(ctx, kwargs):
        await ctx.info(f"Running algorithm: {kwargs['algorithm']}")
        await ctx.report_progress(0, 100)
        payload = {"algorithm": kwargs["algorithm"], "parameters": kwargs["parameters"]}
        socket_timeout = with_timeout(payload, kwargs)
        if kwargs.get("load_results"):
            payload["load_results"] = True
        if kwargs.get("ellipsoid") is not None:
            payload["ellipsoid"] = kwargs["ellipsoid"]
        result = await _send("execute_processing", payload, timeout=socket_timeout)
        await ctx.report_progress(100, 100)
        return result

    async def processing_execute_batch(ctx, kwargs):
        runs = kwargs["parameters_list"]
        await ctx.info(f"Batch processing {kwargs['algorithm']}: {len(runs)} run(s)")
        payload = {"algorithm": kwargs["algorithm"], "parameters_list": runs}
        if kwargs.get("ellipsoid") is not None:
            payload["ellipsoid"] = kwargs["ellipsoid"]
        socket_timeout = with_timeout(payload, kwargs)
        return await _send("execute_processing_batch", payload, timeout=socket_timeout)

    async def processing_list_algorithms(ctx, kwargs):
        payload = {}
        for key in ("search", "provider"):
            if key in kwargs:
                payload[key] = kwargs[key]
        return await _send("list_processing_algorithms", payload)

    async def processing_run_model(ctx, kwargs):
        await ctx.info(f"Running model: {kwargs['model']}")
        await ctx.report_progress(0, 100)
        payload = {"model": kwargs["model"], "parameters": kwargs.get("parameters") or {}}
        if kwargs.get("ellipsoid") is not None:
            payload["ellipsoid"] = kwargs["ellipsoid"]
        result = await _send("run_model", payload, timeout=TIMEOUT_LONG)
        await ctx.report_progress(100, 100)
        return result

    async def processing_create_model(ctx, kwargs):
        await ctx.info(
            f"Building Processing model: {kwargs['name']} ({len(kwargs['steps'])} step(s))"
        )
        payload = {
            "name": kwargs["name"],
            "steps": kwargs["steps"],
            "description": kwargs.get("description", ""),
            "group": kwargs.get("group", "Models"),
        }
        for key in ("inputs", "outputs"):
            if kwargs.get(key) is not None:
                payload[key] = kwargs[key]
        return await _send("create_processing_model", payload, timeout=TIMEOUT_LONG)

    async def processing_start_job(ctx, kwargs):
        payload = {"algorithm": kwargs["algorithm"], "parameters": kwargs["parameters"]}
        if kwargs.get("load_results"):
            payload["load_results"] = True
        if kwargs.get("ellipsoid") is not None:
            payload["ellipsoid"] = kwargs["ellipsoid"]
        return await _send("start_processing_job", payload)

    processing_actions: dict[str, _Action] = {
        "execute": processing_execute,
        "execute_batch": processing_execute_batch,
        "list_algorithms": processing_list_algorithms,
        "get_providers": lambda ctx, kwargs: _send("get_processing_providers"),
        "list_models": lambda ctx, kwargs: _send("list_processing_models"),
        "run_model": processing_run_model,
        "get_help": lambda ctx, kwargs: _send(
            "get_algorithm_help", {"algorithm_id": kwargs["algorithm_id"]}
        ),
        "create_model": processing_create_model,
        "start_job": processing_start_job,
        "get_job": lambda ctx, kwargs: _send(
            "get_processing_job", {"job_id": kwargs["job_id"]} if kwargs.get("job_id") else {}
        ),
        "cancel_job": lambda ctx, kwargs: _send(
            "cancel_processing_job", {"job_id": kwargs["job_id"]}
        ),
    }

    @mcp.tool(
        title="Processing",
        description=(
            "QGIS Processing framework.\n"
            "Actions: execute, execute_batch, list_algorithms, get_help, get_providers, "
            "create_model, list_models, run_model, start_job, get_job, cancel_job\n"
            "- execute: algorithm (str), parameters (dict), timeout (int, optional, seconds "
            "before the algorithm is cancelled, default 55), load_results (bool, optional) - "
            "add the outputs to the project and list them in 'loaded_layers'; the only way to "
            "keep a 'TEMPORARY_OUTPUT'/'memory:' result, ellipsoid (str, optional) - "
            "measurement ellipsoid, e.g. 'EPSG:7030' (WGS 84); default is the project's\n"
            "- execute_batch: algorithm (str), parameters_list (list[dict]), timeout (int, optional, "
            "seconds for the whole batch, default 55), ellipsoid (str, optional) - one run per "
            "dict, per-run success/error/skipped status\n"
            "- list_algorithms: search (str, optional), provider (str, optional)\n"
            "- get_help: algorithm_id (str)\n"
            "- get_providers: no params - providers with algorithm counts and active status\n"
            "- list_models: no params - registered Processing models (id, name, group)\n"
            "- run_model: model (str: registered id like 'model:myflow', or a .model3 path), "
            "parameters (dict, optional) mapping the model's input names to values; missing "
            "output/sink parameters default to a temporary layer; ellipsoid (str, optional)\n"
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
            "appended to the name on collision.\n"
            "- start_job: algorithm (str), parameters (dict), load_results (bool, optional), "
            "ellipsoid (str, optional) - run as a background task with no time limit and return "
            "a job id at once; use it for anything that may take more than a minute. A "
            "'TEMPORARY_OUTPUT' feature sink needs load_results; temporary files are kept\n"
            "- get_job: job_id (str, optional) - omit it to list every job. The state is "
            "running with progress, succeeded with result, failed with error, or cancelled\n"
            "- cancel_job: job_id (str)"
            f"{_PARAMS_NOTE}"
        ),
    )
    async def processing(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await _dispatch("processing", processing_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 10. code
    # ------------------------------------------------------------------

    async def code_execute(ctx, kwargs):
        if not await _confirm_destructive(
            ctx, "Execute arbitrary PyQGIS code? This can modify your project and system."
        ):
            return {"ok": False, "message": "Cancelled by user"}
        await ctx.info("Executing PyQGIS code...")
        await ctx.report_progress(0, 100)
        payload = {"code": kwargs["code"]}
        socket_timeout = with_timeout(payload, kwargs)
        result = await _send("execute_code", payload, timeout=socket_timeout)
        await ctx.report_progress(100, 100)
        if failure := code_failure_message(result):
            raise ToolError(failure)
        return result

    code_actions: dict[str, _Action] = {"execute": code_execute}

    @mcp.tool(
        title="Code",
        description=(
            "Execute arbitrary PyQGIS code.\n"
            "Actions: execute\n"
            "- execute: code (str), timeout (int, optional, seconds before the script is cancelled, "
            "default 55); a script that raises or times out is a tool error with the traceback and "
            "output so far - destructive, requires confirmation"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def code(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await _dispatch("code", code_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 11. batch
    # ------------------------------------------------------------------

    async def batch_execute(ctx, kwargs):
        commands = kwargs["commands"]
        for cmd in commands:
            cmd_type = cmd.get("type", "")
            if cmd_type in BATCH_BLOCKED_COMMANDS:
                raise ToolError(
                    f"Command {cmd_type!r} is not allowed in batch, "
                    "call it individually so confirmation can be requested"
                )
        return await _send("batch", {"commands": commands}, timeout=TIMEOUT_LONG)

    batch_actions: dict[str, _Action] = {"execute": batch_execute}

    @mcp.tool(
        title="Batch",
        description=(
            "Execute multiple commands in a single round-trip.\n"
            "Actions: execute\n"
            "- execute: commands (list[dict]) - each {'type': '<command>', 'params': {...}}. "
            f"Destructive commands ({', '.join(sorted(BATCH_BLOCKED_COMMANDS))}) "
            "are not allowed in batch."
            f"{_PARAMS_NOTE}"
        ),
    )
    async def batch(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list:
        return await _dispatch("batch", batch_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 12. layer_tree
    # ------------------------------------------------------------------

    async def layer_tree_create_group(ctx, kwargs):
        payload = {"name": kwargs["name"]}
        if "parent" in kwargs:
            payload["parent"] = kwargs["parent"]
        return await _send("create_layer_group", payload)

    layer_tree_actions: dict[str, _Action] = {
        "get": lambda ctx, kwargs: _send("get_layer_tree"),
        "create_group": layer_tree_create_group,
        "move_to_group": lambda ctx, kwargs: _send(
            "move_layer_to_group",
            {"layer_id": kwargs["layer_id"], "group_name": kwargs["group_name"]},
        ),
    }

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
        return await _dispatch("layer_tree", layer_tree_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 13. plugins
    # ------------------------------------------------------------------

    async def plugins_reload(ctx, kwargs):
        await ctx.info(f"Reloading plugin: {kwargs['plugin_name']}")
        return await _send("reload_plugin", {"plugin_name": kwargs["plugin_name"]})

    plugins_actions: dict[str, _Action] = {
        "list": lambda ctx, kwargs: _send(
            "list_plugins", {"enabled_only": kwargs.get("enabled_only", False)}
        ),
        "get_info": lambda ctx, kwargs: _send(
            "get_plugin_info", {"plugin_name": kwargs["plugin_name"]}
        ),
        "reload": plugins_reload,
    }

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
        return await _dispatch("plugins", plugins_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 14. variables
    # ------------------------------------------------------------------

    variables_actions: dict[str, _Action] = {
        "get": lambda ctx, kwargs: _send("get_project_variables"),
        "set": lambda ctx, kwargs: _send(
            "set_project_variable", {"key": kwargs["key"], "value": kwargs["value"]}
        ),
    }

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
        return await _dispatch("variables", variables_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 15. settings
    # ------------------------------------------------------------------

    async def settings_set(ctx, kwargs):
        key = kwargs["key"]
        if not await _confirm_destructive(
            ctx, f"Set QGIS setting '{key}'? Incorrect settings can affect behavior."
        ):
            return {"ok": False, "message": "Cancelled by user"}
        return await _send("set_setting", {"key": key, "value": kwargs["value"]})

    settings_actions: dict[str, _Action] = {
        "get": lambda ctx, kwargs: _send("get_setting", {"key": kwargs["key"]}),
        "set": settings_set,
    }

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
        return await _dispatch("settings", settings_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 16. additional tools that don't fit neatly into groups above
    # ------------------------------------------------------------------

    def picked(kwargs, required, optional):
        """Payload with *required* keys taken as-is and *optional* ones when present."""
        payload: dict[str, Any] = {key: kwargs[key] for key in required}
        for key in optional:
            if key in kwargs:
                payload[key] = kwargs[key]
        return payload

    expression_actions: dict[str, _Action] = {
        "validate": lambda ctx, kwargs: _send(
            "validate_expression", picked(kwargs, ("expression",), ("layer_id",))
        ),
        "evaluate": lambda ctx, kwargs: _send(
            "evaluate_expression", picked(kwargs, ("expression",), ("layer_id",))
        ),
    }

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
        return await _dispatch("expression", expression_actions, ctx, action, params)

    query_actions: dict[str, _Action] = {
        "sql": lambda ctx, kwargs: _send(
            "execute_sql",
            picked(
                kwargs,
                ("query",),
                ("layers", "as_layer", "layer_name", "geometry_field", "uid_field", "limit"),
            ),
            timeout=TIMEOUT_LONG,
        ),
        "identify": lambda ctx, kwargs: _send(
            "identify_features",
            picked(kwargs, ("point",), ("tolerance", "layer_ids", "limit", "crs")),
        ),
    }

    @mcp.tool(
        title="Query",
        description=(
            "Cross-layer query.\n"
            "Actions: sql, identify\n"
            "- sql: query (str), layers (list[str], optional), as_layer (bool, default false), "
            "layer_name (str), geometry_field (str, optional), uid_field (str, optional), limit (int, default 1000, negative for all)\n"
            "- identify: point (list[float] [x,y], in crs or the project CRS), tolerance "
            "(float, same units, default 0), "
            "layer_ids (list[str], optional), limit (int, default 10), crs (str, optional)"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def query(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await _dispatch("query", query_actions, ctx, action, params)

    transform_actions: dict[str, _Action] = {
        "coordinates": lambda ctx, kwargs: _send(
            "transform_coordinates",
            picked(kwargs, ("source_crs", "target_crs"), ("point", "points", "bbox")),
        ),
    }

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
        return await _dispatch("transform", transform_actions, ctx, action, params)

    async def message_log_get(ctx, kwargs):
        payload: dict[str, Any] = {"limit": kwargs.get("limit", 100)}
        for key in ("level", "tag"):
            if key in kwargs:
                payload[key] = kwargs[key]
        return await _send("get_message_log", payload)

    message_log_actions: dict[str, _Action] = {"get": message_log_get}

    @mcp.tool(
        title="Message Log",
        description=(
            "QGIS message log.\n"
            "Actions: get\n"
            "- get: level (str, optional: 'info', 'warning', 'critical', 'success', 'none'), "
            "tag (str, optional), limit (int, default 100)"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=True,
    )
    async def message_log(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await _dispatch("message_log", message_log_actions, ctx, action, params)

    layer_property_actions: dict[str, _Action] = {
        "set": lambda ctx, kwargs: _send(
            "set_layer_property",
            {
                "layer_id": kwargs["layer_id"],
                "property": kwargs["property"],
                "value": kwargs["value"],
            },
        ),
    }

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
        return await _dispatch("layer_property", layer_property_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 19b. field - schema and attribute editing
    # ------------------------------------------------------------------

    async def field_add(ctx, kwargs):
        payload = {
            "layer_id": kwargs["layer_id"],
            "field_name": kwargs["field_name"],
            "field_type": kwargs["field_type"],
        }
        for key in ("length", "precision"):
            if kwargs.get(key) is not None:
                payload[key] = kwargs[key]
        return await _send("add_field", payload)

    async def field_delete(ctx, kwargs):
        field_name = kwargs["field_name"]
        layer_id = kwargs["layer_id"]
        if not await _confirm_destructive(
            ctx, f"Delete field '{field_name}' from layer {layer_id}?"
        ):
            return {"ok": False, "message": "Cancelled by user"}
        return await _send("delete_field", {"layer_id": layer_id, "field_name": field_name})

    field_actions: dict[str, _Action] = {
        "add": field_add,
        "delete": field_delete,
        "rename": lambda ctx, kwargs: _send(
            "rename_field",
            {
                "layer_id": kwargs["layer_id"],
                "old_name": kwargs["old_name"],
                "new_name": kwargs["new_name"],
            },
        ),
        "calculate": lambda ctx, kwargs: _send(
            "field_calculator",
            {
                "layer_id": kwargs["layer_id"],
                "field_name": kwargs["field_name"],
                "expression": kwargs["expression"],
                "field_type": kwargs.get("field_type", "double"),
                "length": kwargs.get("length", 0),
                "precision": kwargs.get("precision", 0),
            },
        ),
        "unique_values": lambda ctx, kwargs: _send(
            "get_unique_values",
            {
                "layer_id": kwargs["layer_id"],
                "field": kwargs["field"],
                "limit": kwargs.get("limit", 1000),
            },
        ),
    }

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
        return await _dispatch("field", field_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 19c. analysis - vector/raster analysis operations
    # ------------------------------------------------------------------

    async def analysis_spatial_join(ctx, kwargs):
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

    async def analysis_zonal_statistics(ctx, kwargs):
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

    async def analysis_raster_calculator(ctx, kwargs):
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

    analysis_actions: dict[str, _Action] = {
        "spatial_join": analysis_spatial_join,
        "zonal_statistics": analysis_zonal_statistics,
        "raster_calculator": analysis_raster_calculator,
        "sample_raster": lambda ctx, kwargs: _send(
            "sample_raster_values",
            {
                "raster_layer": kwargs["raster_layer"],
                "points": kwargs["points"],
                "band": kwargs.get("band"),
                **({"crs": kwargs["crs"]} if kwargs.get("crs") else {}),
            },
        ),
    }

    @mcp.tool(
        title="Analysis",
        description=(
            "Vector and raster analysis.\n"
            "Actions: spatial_join, zonal_statistics, raster_calculator, sample_raster\n"
            "- spatial_join: target_layer (str), join_layer (str), predicates (list[int], "
            "default [0]: 0=intersects 1=contains 2=equals 3=touches 4=overlaps 5=within "
            "6=crosses), join_fields (list[str], optional - default all), method (int, "
            "default 1: 0=one-to-many 1=first match - keeps one arbitrary match, drops the "
            "rest - 2=largest overlap), prefix (str, "
            "default ''), output_path (str, optional - omit for an in-memory layer)\n"
            "- zonal_statistics: polygon_layer (str), raster_layer (str), band (int, default 1), "
            "prefix (str, default '_'), stats (list[int], default [0,1,2]: 0=count 1=sum 2=mean "
            "3=median 4=stdev 5=min 6=max 7=range 8=minority 9=majority 10=variety 11=variance), "
            "output_path (str, optional - omit for an in-memory layer)\n"
            "- raster_calculator: expression (str, reference bands as 'LayerName@band'), "
            "output_path (str, GeoTIFF), reference_layer (str, optional - grid/extent source)\n"
            "- sample_raster: raster_layer (str), points (list[[x, y]] in crs or the raster "
            "CRS), band (int, optional - omit to sample all bands), crs (str, optional)"
            f"{_PARAMS_NOTE}"
        ),
    )
    async def analysis(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await _dispatch("analysis", analysis_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 20. bookmarks
    # ------------------------------------------------------------------

    bookmarks_actions: dict[str, _Action] = {
        "list": lambda ctx, kwargs: _send("get_bookmarks"),
        "add": lambda ctx, kwargs: _send(
            "add_bookmark",
            {
                "name": kwargs["name"],
                "xmin": kwargs["xmin"],
                "ymin": kwargs["ymin"],
                "xmax": kwargs["xmax"],
                "ymax": kwargs["ymax"],
                "group": kwargs.get("group", ""),
                **({"crs": kwargs["crs"]} if kwargs.get("crs") else {}),
            },
        ),
        "remove": lambda ctx, kwargs: _send(
            "remove_bookmark", {"bookmark_id": kwargs["bookmark_id"]}
        ),
    }

    @mcp.tool(
        title="Bookmarks",
        description=(
            "Spatial bookmarks for quick navigation.\n"
            "Actions: list, add, remove\n"
            "- list: no params\n"
            "- add: name (str), xmin (float), ymin (float), xmax (float), ymax (float), "
            "crs (str, optional - default the project CRS), group (str, optional)\n"
            "- remove: bookmark_id (str) - destructive"
            f"{_PARAMS_NOTE}"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def bookmarks(
        ctx: Context, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await _dispatch("bookmarks", bookmarks_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 21. map_themes
    # ------------------------------------------------------------------

    map_themes_actions: dict[str, _Action] = {
        "list": lambda ctx, kwargs: _send("get_map_themes"),
        "add": lambda ctx, kwargs: _send("add_map_theme", {"name": kwargs["name"]}),
        "remove": lambda ctx, kwargs: _send("remove_map_theme", {"name": kwargs["name"]}),
        "apply": lambda ctx, kwargs: _send("apply_map_theme", {"name": kwargs["name"]}),
    }

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
        return await _dispatch("map_themes", map_themes_actions, ctx, action, params)

    # ------------------------------------------------------------------
    # 22. active_layer
    # ------------------------------------------------------------------

    active_layer_actions: dict[str, _Action] = {
        "get": lambda ctx, kwargs: _send("get_active_layer"),
        "set": lambda ctx, kwargs: _send("set_active_layer", {"layer_id": kwargs["layer_id"]}),
    }

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
        return await _dispatch("active_layer", active_layer_actions, ctx, action, params)
