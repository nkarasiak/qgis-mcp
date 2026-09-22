"""Unit tests for the MCP server's per-domain tools, against a mocked socket."""

import inspect
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import SOCKET_BACKED_RESOURCES, TOOL_COUNT, make_ctx
from mcp.types import ElicitResult
from mcp_compat import connect

import qgis_mcp.server as srv
from qgis_mcp.helpers import BATCH_BLOCKED_COMMANDS
from qgis_mcp.server import NoBackChannelError, ToolError, _ConfirmSchema


@pytest.mark.asyncio
async def test_get_layers_passes_pagination(mock_connection):
    mock_connection.returns({"layers": [], "total_count": 0, "offset": 5, "limit": 10})

    ctx = make_ctx()
    output = await srv.get_layers(ctx, limit=10, offset=5)
    assert output["total_count"] == 0
    mock_connection.send_command.assert_called_once_with(
        "get_layers", {"limit": 10, "offset": 5}, timeout=30
    )


@pytest.mark.asyncio
async def test_get_layer_features_enforces_max_limit(mock_connection):
    mock_connection.returns({"features": [], "feature_count": 0, "fields": []})

    ctx = make_ctx()
    await srv.get_layer_features(ctx, layer_id="test", limit=100)
    # Should have been capped to 50
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["limit"] == 50


@pytest.mark.asyncio
async def test_get_layer_features_with_expression(mock_connection):
    mock_connection.returns({"features": [], "feature_count": 0, "fields": []})

    ctx = make_ctx()
    await srv.get_layer_features(ctx, layer_id="test", expression="name = 'Berlin'")
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["expression"] == "name = 'Berlin'"


@pytest.mark.asyncio
async def test_get_layer_features_no_expression_omitted(mock_connection):
    mock_connection.returns({"features": [], "feature_count": 0, "fields": []})

    ctx = make_ctx()
    await srv.get_layer_features(ctx, layer_id="test")
    call_params = mock_connection.send_command.call_args[0][1]
    assert "expression" not in call_params


@pytest.mark.asyncio
async def test_get_layer_features_caches_on_result_size_not_limit(mock_connection):
    """A high limit that matched three features is not a large result."""
    ctx = make_ctx()
    mock_connection.returns({"features": [{"_fid": i} for i in range(3)], "feature_count": 3})
    small = await srv.get_layer_features(ctx, layer_id="test", limit=50)
    assert "features_resource" not in small
    assert "_hint" not in small

    mock_connection.returns({"features": [{"_fid": i} for i in range(21)], "feature_count": 21})
    large = await srv.get_layer_features(ctx, layer_id="test", limit=50)
    assert large["features_resource"].startswith("qgis://cache/")


@pytest.mark.asyncio
async def test_batch_commands_tool(mock_connection):
    mock_connection.returns(
        [
            {"status": "success", "result": {"pong": True}},
            {"status": "success", "result": {"layers": [], "total_count": 0}},
        ]
    )

    ctx = make_ctx()
    output = await srv.batch_commands(
        ctx,
        commands=[
            {"type": "ping", "params": {}},
            {"type": "get_layers", "params": {}},
        ],
    )
    assert len(output) == 2


@pytest.mark.asyncio
async def test_execute_processing_uses_long_timeout(mock_connection):
    mock_connection.returns({"algorithm": "test", "result": {}})

    ctx = make_ctx()
    await srv.execute_processing(ctx, algorithm="native:buffer", parameters={"INPUT": "layer"})
    mock_connection.send_command.assert_called_once_with(
        "execute_processing",
        {"algorithm": "native:buffer", "parameters": {"INPUT": "layer"}},
        timeout=60,
    )
    ctx.info.assert_awaited_once_with("Running algorithm: native:buffer")


@pytest.mark.asyncio
async def test_execute_processing_default_sends_no_plugin_timeout(mock_connection):
    """Without an explicit timeout the plugin applies its own default."""
    mock_connection.returns({"algorithm": "test", "result": {}})

    await srv.execute_processing(make_ctx(), algorithm="native:buffer", parameters={})
    sent_params = mock_connection.send_command.call_args[0][1]
    assert "timeout" not in sent_params


@pytest.mark.asyncio
async def test_execute_processing_forwards_ellipsoid_only_when_given(mock_connection):
    """Unset stays off the wire so an older plugin, which would refuse it, keeps working."""
    mock_connection.returns({"algorithm": "test", "result": {}})

    await srv.execute_processing(make_ctx(), algorithm="native:buffer", parameters={})
    assert "ellipsoid" not in mock_connection.send_command.call_args[0][1]

    await srv.execute_processing(
        make_ctx(), algorithm="native:buffer", parameters={}, ellipsoid="EPSG:7030"
    )
    assert mock_connection.send_command.call_args[0][1]["ellipsoid"] == "EPSG:7030"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("execute_processing_batch", {"algorithm": "native:buffer", "parameters_list": [{}]}),
        ("run_model", {"model": "model:m"}),
    ],
)
async def test_batch_and_model_forward_ellipsoid_only_when_given(mock_connection, tool, kwargs):
    mock_connection.returns({"results": [], "result": {}})

    await getattr(srv, tool)(make_ctx(), **kwargs)
    assert "ellipsoid" not in mock_connection.send_command.call_args[0][1]

    await getattr(srv, tool)(make_ctx(), **kwargs, ellipsoid="EPSG:7030")
    assert mock_connection.send_command.call_args[0][1]["ellipsoid"] == "EPSG:7030"


@pytest.mark.asyncio
async def test_execute_processing_custom_timeout_outlives_plugin_deadline(mock_connection):
    """The socket must outlast the plugin's deadline.

    If the client gave up first, a slow algorithm would surface as an opaque
    socket timeout while QGIS kept working on an orphaned job.
    """
    mock_connection.returns({"algorithm": "test", "result": {}})

    await srv.execute_processing(make_ctx(), algorithm="native:buffer", parameters={}, timeout=300)
    args, kwargs = mock_connection.send_command.call_args
    assert args[1]["timeout"] == 300, "plugin must receive the algorithm deadline"
    assert kwargs["timeout"] > 300, "socket timeout must outlast the plugin deadline"


@pytest.mark.asyncio
async def test_execute_code_tool(mock_connection):
    mock_connection.returns({"stdout": "hello", "stderr": ""})

    ctx = make_ctx()
    result = await srv.execute_code(ctx, code="print('hello')")
    assert result["stdout"] == "hello"
    ctx.info.assert_awaited_once_with("Executing PyQGIS code...")


@pytest.mark.asyncio
async def test_execute_code_timeout_keeps_the_plugin_deadline_first(mock_connection):
    """#43: like execute_processing, the plugin must give up 5s before the socket does."""
    mock_connection.returns({"executed": True})

    await srv.execute_code(make_ctx(), code="x = 1")
    mock_connection.send_command.assert_called_once_with(
        "execute_code", {"code": "x = 1"}, timeout=60
    )
    mock_connection.send_command.reset_mock()
    await srv.execute_code(make_ctx(), code="x = 1", timeout=300)
    mock_connection.send_command.assert_called_once_with(
        "execute_code", {"code": "x = 1", "timeout": 300}, timeout=305
    )


@pytest.mark.asyncio
async def test_execute_processing_batch_timeout_bounds_the_whole_batch(mock_connection):
    mock_connection.returns({"results": []})

    await srv.execute_processing_batch(make_ctx(), algorithm="a", parameters_list=[{}], timeout=120)
    mock_connection.send_command.assert_called_once_with(
        "execute_processing_batch",
        {"algorithm": "a", "parameters_list": [{}], "timeout": 120},
        timeout=125,
    )


@pytest.mark.asyncio
async def test_delete_features_by_fids(mock_connection):
    mock_connection.returns({"deleted": 2})

    ctx = make_ctx()
    output = await srv.delete_features(ctx, layer_id="test", fids=[1, 2])
    assert output == {"deleted": 2}
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["fids"] == [1, 2]


@pytest.mark.asyncio
async def test_delete_features_by_expression(mock_connection):
    mock_connection.returns({"deleted": 3})

    ctx = make_ctx()
    await srv.delete_features(ctx, layer_id="test", expression="id > 5")
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["expression"] == "id > 5"


@pytest.mark.asyncio
async def test_delete_features_confirmation_names_empty_fids(mock_connection):
    """fids=[] must not be described as an expression the caller never passed."""
    mock_connection.returns({"deleted": 0})
    confirm = AsyncMock(return_value=True)
    with patch("qgis_mcp.server._confirm_destructive", confirm):
        await srv.delete_features(make_ctx(), layer_id="test", fids=[])
    message = confirm.call_args[0][1]
    assert "fids=[]" in message
    assert "expression" not in message


@pytest.mark.asyncio
async def test_set_layer_style_tool(mock_connection):
    mock_connection.returns({"ok": True})

    ctx = make_ctx()
    output = await srv.set_layer_style(
        ctx,
        layer_id="test",
        style_type="categorized",
        field="name",
        classes=5,
        color_ramp="Spectral",
    )
    assert output == {"ok": True}
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["style_type"] == "categorized"
    assert call_params["field"] == "name"


@pytest.mark.asyncio
async def test_select_features_tool(mock_connection):
    mock_connection.returns({"selected": 3})

    ctx = make_ctx()
    output = await srv.select_features(ctx, layer_id="test", expression="value > 100")
    assert output == {"selected": 3}
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "select_features"
    assert params["expression"] == "value > 100"


@pytest.mark.asyncio
async def test_create_processing_model_tool(mock_connection):
    mock_connection.returns(
        {
            "ok": True,
            "name": "buffer_centroids_2",
            "requested_name": "buffer_centroids",
            "path": "/home/u/.local/share/QGIS/QGIS3/profiles/default/processing/models/buffer_centroids_2.model3",
            "registered": True,
            "input_count": 2,
            "step_count": 2,
            "output_count": 1,
        }
    )

    ctx = make_ctx()
    inputs = [
        {"name": "input_layer", "type": "vector", "description": "Input vector layer"},
        {"name": "distance", "type": "distance", "default": 100},
    ]
    steps = [
        {
            "id": "buffer",
            "algorithm": "native:buffer",
            "parameters": {"INPUT": "@input_layer", "DISTANCE": "@distance", "DISSOLVE": False},
        },
        {
            "id": "centroids",
            "algorithm": "native:centroids",
            "parameters": {"INPUT": "$buffer.OUTPUT", "ALL_PARTS": False},
        },
    ]
    outputs = [{"name": "Centroids", "from_step": "centroids", "from_output": "OUTPUT"}]

    output = await srv.create_processing_model(
        ctx,
        name="buffer_centroids",
        steps=steps,
        inputs=inputs,
        outputs=outputs,
    )
    assert output["ok"] is True
    assert output["step_count"] == 2
    assert output["registered"] is True
    assert output["requested_name"] == "buffer_centroids"

    # Long timeout, full payload forwarded - no path/register/overwrite fields anymore
    mock_connection.send_command.assert_called_once()
    call_args = mock_connection.send_command.call_args
    assert call_args[0][0] == "create_processing_model"
    sent = call_args[0][1]
    assert sent["name"] == "buffer_centroids"
    assert sent["steps"] == steps
    assert sent["inputs"] == inputs
    assert sent["outputs"] == outputs
    assert "path" not in sent
    assert "register" not in sent
    assert "overwrite" not in sent
    # Uses the long timeout for processing operations
    assert call_args[1]["timeout"] == 60


@pytest.mark.asyncio
async def test_export_layout_tool(mock_connection):
    mock_connection.returns({"ok": True, "path": "/tmp/layout.pdf"})

    ctx = make_ctx()
    output = await srv.export_layout(ctx, layout_name="Map1", path="/tmp/layout.pdf")
    assert output["ok"] is True
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["format"] == "pdf"
    assert call_params["dpi"] == 300


@pytest.mark.asyncio
async def test_get_message_log_tool(mock_connection):
    mock_connection.returns(
        {
            "messages": [
                {
                    "tag": "QGIS MCP",
                    "message": "test",
                    "level": "info",
                    "timestamp": "2026-03-07T12:00:00",
                }
            ],
            "count": 1,
        }
    )

    ctx = make_ctx()
    output = await srv.get_message_log(ctx, limit=50)
    assert output["count"] == 1
    mock_connection.send_command.assert_called_once_with(
        "get_message_log", {"limit": 50}, timeout=30
    )


@pytest.mark.asyncio
async def test_get_message_log_with_filters(mock_connection):
    mock_connection.returns({"messages": [], "count": 0})

    ctx = make_ctx()
    await srv.get_message_log(ctx, level="warning", tag="MyPlugin", limit=10)
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["level"] == "warning"
    assert call_params["tag"] == "MyPlugin"
    assert call_params["limit"] == 10


@pytest.mark.asyncio
async def test_reload_plugin_tool(mock_connection):
    mock_connection.returns({"reloaded": "my_plugin", "ok": True})

    ctx = make_ctx()
    output = await srv.reload_plugin(ctx, plugin_name="my_plugin")
    assert output["ok"] is True
    assert output["reloaded"] == "my_plugin"
    ctx.info.assert_awaited_once_with("Reloading plugin: my_plugin")


@pytest.mark.asyncio
async def test_reload_plugin_self_blocked(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "error",
        "message": "Cannot reload MCP plugin (would break the connection)",
    }

    ctx = make_ctx()
    with pytest.raises(ToolError, match="Cannot reload MCP plugin"):
        await srv.reload_plugin(ctx, plugin_name="qgis_mcp_plugin")


@pytest.mark.asyncio
async def test_create_layer_group_tool(mock_connection):
    mock_connection.returns({"name": "My Group", "ok": True})

    ctx = make_ctx()
    output = await srv.create_layer_group(ctx, name="My Group")
    assert output["ok"] is True
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["name"] == "My Group"
    assert "parent" not in call_params


@pytest.mark.asyncio
async def test_create_layer_group_with_parent(mock_connection):
    mock_connection.returns({"name": "Sub Group", "ok": True})

    ctx = make_ctx()
    await srv.create_layer_group(ctx, name="Sub Group", parent="Parent Group")
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["parent"] == "Parent Group"


@pytest.mark.asyncio
async def test_validate_expression_with_layer(mock_connection):
    mock_connection.returns({"valid": True, "referenced_columns": ["name"]})

    ctx = make_ctx()
    await srv.validate_expression(ctx, expression="\"name\" = 'Berlin'", layer_id="test_layer")
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["layer_id"] == "test_layer"
    assert call_params["expression"] == "\"name\" = 'Berlin'"


@pytest.mark.asyncio
async def test_validate_expression_without_layer(mock_connection):
    mock_connection.returns({"valid": True, "referenced_columns": []})

    ctx = make_ctx()
    await srv.validate_expression(ctx, expression="1 + 1")
    call_params = mock_connection.send_command.call_args[0][1]
    assert "layer_id" not in call_params


IMAGE_TOOLS = [
    ("render_map", {"width": 800, "height": 600}, "Rendering map..."),
    ("get_canvas_screenshot", {}, None),
    ("get_3d_screenshot", {"view_index": 0, "dpi": 96}, None),
]


@pytest.mark.parametrize(("tool_name", "kwargs", "info"), IMAGE_TOOLS)
@pytest.mark.asyncio
async def test_image_tools_return_inline_image_content(mock_connection, tool_name, kwargs, info):
    """Claude has to see the picture, so these answer with ImageContent, not a path."""
    mock_connection.returns(
        {"base64_data": "iVBOR==", "mime_type": "image/png", "width": 800, "height": 600}
    )
    ctx = make_ctx()
    result = await getattr(srv, tool_name)(ctx, **kwargs)
    assert [block.type for block in result] == ["image"]
    assert result[0].data == "iVBOR=="
    if info:
        ctx.info.assert_awaited_once_with(info)


LAYER_RESULT = {"layer_id": "vec_123", "name": "roads", "type": "vector"}
RESOURCE_LINK_TOOLS = [
    (
        "add_vector_layer",
        {"path": "/tmp/roads.shp"},
        LAYER_RESULT,
        "qgis://layers/vec_123/info",
        None,
    ),
    (
        "add_raster_layer",
        {"path": "/tmp/dem.tif"},
        {"layer_id": "ras_456", "name": "dem", "type": "raster"},
        "qgis://layers/ras_456/info",
        None,
    ),
    (
        "create_memory_layer",
        {
            "name": "test_layer",
            "geometry_type": "Point",
            "fields": [{"name": "id", "type": "integer"}],
        },
        {"id": "mem_123", "name": "test_layer", "type": "vector_0", "feature_count": 0},
        "qgis://layers/mem_123/info",
        None,
    ),
    (
        "add_layer_from_connection",
        {"provider": "postgres", "connection": "gis", "table": "roads", "schema": "public"},
        {"id": "abc", "name": "roads", "type": "vector"},
        "qgis://layers/abc/info",
        None,
    ),
    (
        "create_new_project",
        {"path": "/tmp/new.qgz"},
        {"ok": True, "path": "/tmp/new.qgz"},
        "qgis://project",
        None,
    ),
    (
        "load_project",
        {"path": "/tmp/test.qgz"},
        {"ok": True},
        "qgis://project",
        "Loading project: /tmp/test.qgz",
    ),
    (
        "set_project_crs",
        {"crs": "EPSG:3857"},
        {"ok": True, "crs": "EPSG:3857", "description": "WGS 84 / Pseudo-Mercator"},
        "qgis://project",
        None,
    ),
]


@pytest.mark.parametrize(("tool_name", "kwargs", "result", "uri", "info"), RESOURCE_LINK_TOOLS)
@pytest.mark.asyncio
async def test_mutating_tools_return_text_plus_a_resource_link(
    mock_connection, tool_name, kwargs, result, uri, info
):
    """A tool that creates something hands back the payload and a link to read it."""
    mock_connection.returns(result)
    ctx = make_ctx()
    output = await getattr(srv, tool_name)(ctx, **kwargs)
    assert [block.type for block in output] == ["text", "resource_link"]
    assert json.loads(output[0].text) == result
    assert str(output[1].uri) == uri
    if info:
        ctx.info.assert_awaited_once_with(info)


@pytest.mark.asyncio
async def test_create_memory_layer_forwards_the_field_definitions(mock_connection):
    mock_connection.returns({"id": "mem_123", "name": "test_layer"})
    await srv.create_memory_layer(
        make_ctx(),
        name="test_layer",
        geometry_type="Point",
        fields=[{"name": "id", "type": "integer"}],
    )
    params = mock_connection.send_command.call_args[0][1]
    assert params["geometry_type"] == "Point"
    assert params["fields"] == [{"name": "id", "type": "integer"}]


@pytest.mark.asyncio
async def test_get_3d_screenshot_camera_params(mock_connection):
    mock_connection.returns(
        {
            "base64_data": "iVBOR==",
            "mime_type": "image/png",
            "width": 800,
            "height": 600,
            "view_index": 1,
            "open_3d_views": 2,
        }
    )

    ctx = make_ctx()
    result = await srv.get_3d_screenshot(
        ctx, view_index=1, dpi=120, pitch=45, distance=2000, heading=30
    )
    assert result[0].type == "image"
    # camera overrides are forwarded only when provided
    mock_connection.send_command.assert_called_once_with(
        "get_3d_screenshot",
        {"view_index": 1, "dpi": 120, "pitch": 45, "distance": 2000, "heading": 30},
        timeout=30,
    )


@pytest.mark.asyncio
async def test_transform_coordinates_point(mock_connection):
    mock_connection.returns(
        {
            "source_crs": "EPSG:4326",
            "target_crs": "EPSG:3857",
            "point": {"x": 1113194.91, "y": 0.0},
        }
    )

    ctx = make_ctx()
    output = await srv.transform_coordinates(
        ctx, source_crs="EPSG:4326", target_crs="EPSG:3857", point={"x": 10.0, "y": 0.0}
    )
    assert output["point"]["x"] == 1113194.91
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["source_crs"] == "EPSG:4326"
    assert call_params["target_crs"] == "EPSG:3857"
    assert call_params["point"] == {"x": 10.0, "y": 0.0}


@pytest.mark.asyncio
async def test_transform_coordinates_bbox(mock_connection):
    mock_connection.returns(
        {
            "source_crs": "EPSG:4326",
            "target_crs": "EPSG:3857",
            "bbox": {"xmin": 0.0, "ymin": 0.0, "xmax": 1113194.91, "ymax": 1118889.97},
        }
    )

    ctx = make_ctx()
    output = await srv.transform_coordinates(
        ctx,
        source_crs="EPSG:4326",
        target_crs="EPSG:3857",
        bbox={"xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10},
    )
    assert "bbox" in output
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["bbox"] == {"xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10}


@pytest.mark.asyncio
async def test_remove_layer_proceeds_without_elicitation(mock_connection):
    """When elicitation not supported (raises), tool proceeds (fail-open)."""
    mock_connection.returns({"ok": True})

    ctx = make_ctx(elicitation="unsupported")
    output = await srv.remove_layer(ctx, layer_id="test_layer")
    assert output == {"ok": True}
    mock_connection.send_command.assert_called_once()


# Every tool that calls _confirm_destructive. import_layer_to_connection only
# asks when it would overwrite, hence the flag in its kwargs.
CONFIRMATION_GATED_TOOLS = [
    ("remove_layer", {"layer_id": "L1"}),
    ("delete_features", {"layer_id": "L1", "fids": [1]}),
    ("delete_field", {"layer_id": "L1", "field_name": "obsolete"}),
    ("rollback_edits", {"layer_id": "L1"}),
    ("remove_layout", {"layout_name": "Map1"}),
    ("set_setting", {"key": "qgis/x", "value": "1"}),
    ("execute_code", {"code": "x = 1"}),
    (
        "execute_connection_sql",
        {"provider": "postgres", "connection": "gis", "sql": "DROP TABLE t"},
    ),
    (
        "import_layer_to_connection",
        {"layer_id": "L1", "provider": "ogr", "connection": "db", "table": "t", "overwrite": True},
    ),
]


@pytest.mark.parametrize(("tool_name", "kwargs"), CONFIRMATION_GATED_TOOLS)
@pytest.mark.asyncio
async def test_declined_confirmation_never_reaches_the_plugin(mock_connection, tool_name, kwargs):
    """Refusing the prompt must abort, not fall through to the fail-open path."""
    output = await getattr(srv, tool_name)(make_ctx(elicitation="decline"), **kwargs)
    assert output == {"ok": False, "message": "Cancelled by user"}
    mock_connection.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_accepted_prompt_with_confirm_false_is_still_a_refusal(mock_connection):
    """action="accept" only carries the answer; the answer itself can still be no."""
    mock_connection.returns({"ok": True})
    ctx = make_ctx()
    elicit_response = MagicMock()
    elicit_response.action = "accept"
    elicit_response.data = _ConfirmSchema(confirm=False)
    ctx.elicit = AsyncMock(return_value=elicit_response)
    output = await srv.remove_layer(ctx, layer_id="test_layer")
    assert output == {"ok": False, "message": "Cancelled by user"}
    mock_connection.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_remove_layer_confirmed_by_user(mock_connection):
    """When user confirms elicitation, tool proceeds."""
    mock_connection.returns({"ok": True})

    ctx = make_ctx()
    elicit_response = MagicMock()
    elicit_response.action = "accept"
    elicit_response.data = _ConfirmSchema(confirm=True)
    ctx.elicit = AsyncMock(return_value=elicit_response)
    output = await srv.remove_layer(ctx, layer_id="test_layer")
    assert output == {"ok": True}


@pytest.mark.asyncio
async def test_unset_auto_confirm_skips_elicitation(mock_connection):
    """Default (var unset): proceed without ever eliciting."""
    mock_connection.returns({"ok": True})

    ctx = make_ctx(elicitation="decline")
    with patch.dict(os.environ, clear=False):
        os.environ.pop("QGIS_MCP_AUTO_CONFIRM", None)
        output = await srv.remove_layer(ctx, layer_id="test_layer")
    assert output == {"ok": True}
    ctx.elicit.assert_not_called()
    mock_connection.send_command.assert_called_once()


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "", "banana"])
@pytest.mark.asyncio
async def test_non_falsy_auto_confirm_skips_elicitation(mock_connection, value):
    """Only 0/false/no/off re-enable the prompt - anything else skips it."""
    mock_connection.returns({"ok": True})

    ctx = make_ctx(elicitation="decline")
    with patch.dict(os.environ, {"QGIS_MCP_AUTO_CONFIRM": value}):
        output = await srv.remove_layer(ctx, layer_id="test_layer")
    assert output == {"ok": True}
    ctx.elicit.assert_not_called()


@pytest.mark.parametrize("value", ["0", "false", "NO", " off "])
@pytest.mark.asyncio
async def test_falsy_auto_confirm_elicits(mock_connection, value):
    """QGIS_MCP_AUTO_CONFIRM=0/false/no/off restores the confirmation prompt."""
    ctx = make_ctx(elicitation="decline")
    with patch.dict(os.environ, {"QGIS_MCP_AUTO_CONFIRM": value}):
        output = await srv.remove_layer(ctx, layer_id="test_layer")
    assert output == {"ok": False, "message": "Cancelled by user"}
    ctx.elicit.assert_called_once()
    mock_connection.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_set_active_layer_tool(mock_connection):
    mock_connection.returns({"ok": True, "layer_id": "layer_123", "name": "roads"})

    ctx = make_ctx()
    output = await srv.set_active_layer(ctx, layer_id="layer_123")
    assert output["ok"] is True
    mock_connection.send_command.assert_called_once_with(
        "set_active_layer", {"layer_id": "layer_123"}, timeout=30
    )


@pytest.mark.asyncio
async def test_set_canvas_scale_tool(mock_connection):
    mock_connection.returns({"ok": True, "scale": 25000.0, "rotation": 45.0})

    ctx = make_ctx()
    output = await srv.set_canvas_scale(ctx, scale=25000.0, rotation=45.0)
    assert output["ok"] is True
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["scale"] == 25000.0
    assert call_params["rotation"] == 45.0


@pytest.mark.asyncio
async def test_set_canvas_scale_only_scale(mock_connection):
    mock_connection.returns({"ok": True, "scale": 100000.0, "rotation": 0.0})

    ctx = make_ctx()
    await srv.set_canvas_scale(ctx, scale=100000.0)
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["scale"] == 100000.0
    assert "rotation" not in call_params


@pytest.mark.asyncio
async def test_set_layer_labeling_tool(mock_connection):
    mock_connection.returns(
        {"ok": True, "layer_id": "layer_123", "enabled": True, "field_name": "name"}
    )

    ctx = make_ctx()
    output = await srv.set_layer_labeling(
        ctx, layer_id="layer_123", field_name="name", font_size=12.0, color="#FF0000"
    )
    assert output["ok"] is True
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["field_name"] == "name"
    assert call_params["font_size"] == 12.0
    assert call_params["color"] == "#FF0000"


@pytest.mark.asyncio
async def test_set_layer_labeling_disable(mock_connection):
    mock_connection.returns({"ok": True, "layer_id": "layer_123", "enabled": False})

    ctx = make_ctx()
    output = await srv.set_layer_labeling(ctx, layer_id="layer_123", enabled=False)
    assert output["enabled"] is False
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["enabled"] is False


@pytest.mark.asyncio
async def test_add_bookmark_tool(mock_connection):
    mock_connection.returns({"ok": True, "id": "bm2", "name": "Munich"})

    ctx = make_ctx()
    output = await srv.add_bookmark(ctx, name="Munich", xmin=11.3, ymin=48.0, xmax=11.8, ymax=48.3)
    assert output["ok"] is True
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["name"] == "Munich"
    # No crs: the plugin uses the project CRS (EPSG:4326 was assumed before).
    assert "crs" not in call_params

    await srv.add_bookmark(
        ctx, name="Munich", xmin=11.3, ymin=48.0, xmax=11.8, ymax=48.3, crs="EPSG:4326"
    )
    assert mock_connection.send_command.call_args[0][1]["crs"] == "EPSG:4326"


@pytest.mark.asyncio
async def test_run_model_uses_long_timeout(mock_connection):
    mock_connection.returns({"model": "model:flow", "result": {}})

    ctx = make_ctx()
    await srv.run_model(ctx, model="model:flow", parameters={"INPUT": "lyr"})
    mock_connection.send_command.assert_called_once_with(
        "run_model", {"model": "model:flow", "parameters": {"INPUT": "lyr"}}, timeout=60
    )


@pytest.mark.asyncio
async def test_run_model_defaults_empty_parameters(mock_connection):
    mock_connection.returns({})

    await srv.run_model(make_ctx(), model="model:flow")
    mock_connection.send_command.assert_called_once_with(
        "run_model", {"model": "model:flow", "parameters": {}}, timeout=60
    )


@pytest.mark.asyncio
async def test_list_processing_models(mock_connection):
    mock_connection.returns({"models": [{"id": "model:a"}], "count": 1})

    result = await srv.list_processing_models(make_ctx())
    assert result["count"] == 1
    mock_connection.send_command.assert_called_once_with("list_processing_models", None, timeout=30)


@pytest.mark.asyncio
async def test_execute_processing_batch_long_timeout(mock_connection):
    mock_connection.returns({"results": [], "count": 0})

    plist = [{"INPUT": "a"}, {"INPUT": "b"}]
    await srv.execute_processing_batch(make_ctx(), algorithm="native:buffer", parameters_list=plist)
    mock_connection.send_command.assert_called_once_with(
        "execute_processing_batch",
        {"algorithm": "native:buffer", "parameters_list": plist},
        timeout=60,
    )


@pytest.mark.asyncio
async def test_zonal_statistics_passes_defaults(mock_connection):
    mock_connection.returns({"output_layer_id": "x"})

    await srv.zonal_statistics(make_ctx(), polygon_layer="poly", raster_layer="dem")
    mock_connection.send_command.assert_called_once_with(
        "zonal_statistics",
        {
            "polygon_layer": "poly",
            "raster_layer": "dem",
            "band": 1,
            "prefix": "_",
            "stats": None,
            "output_path": None,
        },
        timeout=60,
    )


@pytest.mark.asyncio
async def test_sample_raster_values_readonly(mock_connection):
    mock_connection.returns({"samples": [], "count": 0})

    await srv.sample_raster_values(make_ctx(), raster_layer="dem", points=[[1.0, 2.0]])
    mock_connection.send_command.assert_called_once_with(
        "sample_raster_values",
        {"raster_layer": "dem", "points": [[1.0, 2.0]], "band": None},
        timeout=30,
    )


@pytest.mark.asyncio
async def test_export_layer_with_reproject(mock_connection):
    mock_connection.returns({"ok": True, "output": "out.gpkg"})

    await srv.export_layer(
        make_ctx(), layer_id="lyr", output_path="out.gpkg", target_crs="EPSG:4326"
    )
    mock_connection.send_command.assert_called_once_with(
        "export_layer",
        {
            "layer_id": "lyr",
            "output_path": "out.gpkg",
            "target_crs": "EPSG:4326",
            "filter_expression": None,
        },
        timeout=60,
    )


@pytest.mark.asyncio
async def test_field_calculator(mock_connection):
    mock_connection.returns({"ok": True, "updated": 5, "created": True})

    result = await srv.field_calculator(
        make_ctx(), layer_id="lyr", field_name="area_m2", expression="$area"
    )
    assert result["updated"] == 5
    mock_connection.send_command.assert_called_once_with(
        "field_calculator",
        {
            "layer_id": "lyr",
            "field_name": "area_m2",
            "expression": "$area",
            "field_type": "double",
            "length": 0,
            "precision": 0,
        },
        timeout=30,
    )


@pytest.mark.asyncio
async def test_get_unique_values(mock_connection):
    mock_connection.returns({"field": "type", "values": ["a", "b"], "count": 2})

    result = await srv.get_unique_values(make_ctx(), layer_id="lyr", field="type")
    assert result["count"] == 2
    mock_connection.send_command.assert_called_once_with(
        "get_unique_values", {"layer_id": "lyr", "field": "type", "limit": 1000}, timeout=30
    )


@pytest.mark.asyncio
async def test_spatial_join_long_timeout(mock_connection):
    mock_connection.returns({"output_layer_id": "j"})

    await srv.spatial_join(make_ctx(), target_layer="t", join_layer="j")
    mock_connection.send_command.assert_called_once_with(
        "spatial_join",
        {
            "target_layer": "t",
            "join_layer": "j",
            "predicates": None,
            "join_fields": None,
            "method": 1,
            "prefix": "",
            "output_path": None,
        },
        timeout=60,
    )


@pytest.mark.asyncio
async def test_get_layout_info_tool(mock_connection):
    mock_connection.returns({"items": [], "count": 0})

    await srv.get_layout_info(make_ctx(), layout_name="Map1")
    assert mock_connection.send_command.call_args[0][0] == "get_layout_info"
    assert mock_connection.send_command.call_args[0][1] == {"layout_name": "Map1"}


@pytest.mark.asyncio
async def test_add_layout_label_tool(mock_connection):
    mock_connection.returns({"ok": True, "uuid": "x"})

    await srv.add_layout_label(make_ctx(), layout_name="Map1", text="Title", font_size=18)
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "add_layout_label"
    assert params["text"] == "Title"
    assert params["font_size"] == 18


@pytest.mark.asyncio
async def test_add_layout_legend_tool(mock_connection):
    mock_connection.returns({"ok": True, "uuid": "x"})

    await srv.add_layout_legend(make_ctx(), layout_name="Map1", title="Key")
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "add_layout_legend"
    assert params["title"] == "Key"


@pytest.mark.asyncio
async def test_add_layout_scalebar_tool(mock_connection):
    mock_connection.returns({"ok": True, "uuid": "x"})

    await srv.add_layout_scalebar(make_ctx(), layout_name="Map1", style="Numeric")
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "add_layout_scalebar"
    assert params["style"] == "Numeric"


@pytest.mark.asyncio
async def test_add_layout_picture_tool(mock_connection):
    mock_connection.returns({"ok": True, "uuid": "x"})

    await srv.add_layout_picture(make_ctx(), layout_name="Map1", path="/logo.svg")
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "add_layout_picture"
    assert params["path"] == "/logo.svg"


@pytest.mark.asyncio
async def test_add_layout_table_tool(mock_connection):
    mock_connection.returns({"ok": True, "uuid": "x"})

    await srv.add_layout_table(make_ctx(), layout_name="Map1", layer_id="L1", max_rows=5)
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "add_layout_table"
    assert params["layer_id"] == "L1"
    assert params["max_rows"] == 5


@pytest.mark.asyncio
async def test_configure_atlas_tool(mock_connection):
    mock_connection.returns({"ok": True, "count": 3})

    await srv.configure_atlas(
        make_ctx(), layout_name="Map1", coverage_layer="L1", filter_expression="pop > 0"
    )
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "configure_atlas"
    assert params["coverage_layer"] == "L1"
    assert params["filter_expression"] == "pop > 0"


@pytest.mark.asyncio
async def test_export_atlas_tool(mock_connection):
    mock_connection.returns({"ok": True, "count": 3})

    await srv.export_atlas(make_ctx(), layout_name="Map1", output_path="/out.pdf")
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "export_atlas"
    assert params["output_path"] == "/out.pdf"
    assert mock_connection.send_command.call_args[1]["timeout"] == 60


@pytest.mark.asyncio
async def test_remove_layout_tool_confirms(mock_connection):
    mock_connection.returns({"ok": True, "removed": "Map1"})

    ctx = make_ctx()
    output = await srv.remove_layout(ctx, layout_name="Map1")
    ctx.elicit.assert_awaited_once()
    assert "Map1" in ctx.elicit.await_args.kwargs["message"]
    assert output["ok"] is True
    assert mock_connection.send_command.call_args[0][0] == "remove_layout"


@pytest.mark.asyncio
async def test_remove_layout_tool_fail_open(mock_connection):
    # elicitation unsupported should still proceed (fail-open, like other destructive tools)
    mock_connection.returns({"ok": True, "removed": "Map1"})

    output = await srv.remove_layout(make_ctx(elicitation="unsupported"), layout_name="Map1")
    assert output["ok"] is True
    assert mock_connection.send_command.call_args[0][0] == "remove_layout"


@pytest.mark.asyncio
async def test_execute_sql_tool(mock_connection):
    mock_connection.returns({"fields": ["a"], "rows": [], "count": 0})

    await srv.execute_sql(make_ctx(), query="select * from roads")
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "execute_sql"
    assert params["query"] == "select * from roads"
    assert mock_connection.send_command.call_args[1]["timeout"] == 60


@pytest.mark.asyncio
async def test_evaluate_expression_tool(mock_connection):
    mock_connection.returns({"result": 42})

    output = await srv.evaluate_expression(make_ctx(), expression="1 + 41")
    assert output["result"] == 42
    assert mock_connection.send_command.call_args[0][0] == "evaluate_expression"


@pytest.mark.asyncio
async def test_identify_features_tool(mock_connection):
    mock_connection.returns({"point": [1, 2], "results": []})

    await srv.identify_features(make_ctx(), point=[1.0, 2.0], tolerance=5.0)
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "identify_features"
    assert params["point"] == [1.0, 2.0]
    assert params["tolerance"] == 5.0


@pytest.mark.asyncio
async def test_duplicate_layer_tool(mock_connection):
    mock_connection.returns({"ok": True, "output_layer_id": "L2"})

    await srv.duplicate_layer(make_ctx(), layer_id="L1", new_name="copy")
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "duplicate_layer"
    assert params == {"layer_id": "L1", "new_name": "copy"}


@pytest.mark.asyncio
async def test_set_layer_order_tool(mock_connection):
    mock_connection.returns({"ok": True, "order": ["L1", "L2"]})

    await srv.set_layer_order(make_ctx(), layer_ids=["L1", "L2"])
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "set_layer_order"
    assert params == {"layer_ids": ["L1", "L2"]}


@pytest.mark.asyncio
async def test_destructive_tool_actually_elicits(mock_connection):
    """Regression for #27: the confirmation prompt must reach the client.

    A dict passed where `ctx.elicit` wants a pydantic model raised before any
    request was sent, and the blanket `except Exception` reported that as
    "client doesn't support elicitation" and proceeded anyway.
    """
    mock_connection.returns({"ok": True})
    asked = []

    async def elicitation_callback(context, params):
        asked.append(params.message)
        return ElicitResult(action="accept", content={"confirm": False})  # refuse

    async with connect(srv.mcp, elicitation_callback=elicitation_callback) as client:
        result = await client.call_tool("remove_layer", {"layer_id": "L1"})

    assert len(asked) == 1, "client was never asked to confirm"
    assert "L1" in asked[0]
    # Refusing must stop the command from reaching QGIS.
    assert mock_connection.send_command.call_count == 0
    assert "cancelled" in result.content[0].text.lower()


@pytest.mark.asyncio
async def test_destructive_tool_fails_closed_without_back_channel(mock_connection):
    """A connection with no back-channel must fail closed, not open.

    Under mcp>=2.0's Client(mode="auto"), ctx.elicit() raises NoBackChannelError
    whatever the client's elicitation support, so the old fail-open ran
    destructive tools unconfirmed (#41). Same real-dispatch setup as
    test_destructive_tool_actually_elicits (#27).
    """
    pytest.importorskip(
        "mcp.client.client", reason='mode="auto" negotiation needs the mcp>=2.0 Client'
    )
    # Local: the module only exists on mcp>=2.0, which the skip above guards.
    from mcp.client.client import Client

    mock_connection.returns({"ok": True})

    client = Client(srv.mcp, mode="auto")
    async with client:
        result = await client.call_tool("remove_layer", {"layer_id": "L1"})

    # No back-channel to ask on, so the operation must not reach QGIS.
    assert mock_connection.send_command.call_count == 0
    assert result.is_error is True
    assert "back-channel" in result.content[0].text.lower()


@pytest.mark.asyncio
async def test_confirm_destructive_reraises_non_mcp_errors(mock_connection):
    """Only McpError means "unsupported"; anything else must surface (#27)."""
    ctx = make_ctx()
    ctx.elicit = AsyncMock(side_effect=AttributeError("'dict' object has no attribute ..."))
    with pytest.raises(AttributeError):
        await srv._confirm_destructive(ctx, "Remove layer?")


@pytest.mark.asyncio
async def test_confirm_destructive_fails_closed_on_no_back_channel(mock_connection):
    """A NoBackChannelError means we couldn't ask, not that the client can't answer - fail closed."""
    if not NoBackChannelError:
        pytest.skip("NoBackChannelError needs mcp>=2.0")

    ctx = make_ctx()
    ctx.elicit = AsyncMock(side_effect=NoBackChannelError("elicitation/create"))
    with pytest.raises(ToolError, match="back-channel"):
        await srv._confirm_destructive(ctx, "Remove layer?")


@pytest.mark.asyncio
async def test_server_advertises_tools():
    """MCP server must expose a non-empty tool list to any MCP client.

    This validates generic MCP interoperability: the server module correctly
    registers all tools via FastMCP so that agents (Claude Code, Codex CLI,
    Nous/Hermes-style clients, etc.) can discover them without a live QGIS
    connection.
    """
    tools = await srv.mcp.list_tools()
    tool_names = [t.name for t in tools]

    # Core tools that every MCP client depends on for basic interoperability
    assert "ping" in tool_names, "ping tool missing - clients use it to verify connectivity"
    assert "get_layers" in tool_names, "get_layers tool missing"
    assert "render_map" in tool_names, "render_map tool missing"

    # The whole suite must be registered, not a stub. test_plugin_structure pins
    # the plugin side of the parity, so this number only moves deliberately.
    assert len(tools) == TOOL_COUNT, f"Expected {TOOL_COUNT} tools, got {len(tools)}"


@pytest.mark.asyncio
async def test_server_tool_schemas_are_valid():
    """Every registered tool must have a non-empty name and description.

    Generic MCP clients (including Nous/Hermes agents) rely on tool metadata
    to build system prompts and tool-call payloads - missing descriptions
    degrade agent reasoning quality.
    """
    tools = await srv.mcp.list_tools()
    for tool in tools:
        assert tool.name, f"Tool has empty name: {tool!r}"
        assert tool.description, f"Tool '{tool.name}' has no description"


@pytest.mark.asyncio
async def test_edit_session_lifecycle(mock_connection):
    """start/commit go straight through; rollback is confirmation-gated."""
    mock_connection.returns({"ok": True})
    ctx = make_ctx()
    assert await srv.start_editing(ctx, layer_id="lyr") == {"ok": True}
    assert await srv.commit_edits(ctx, layer_id="lyr") == {"ok": True}
    assert await srv.rollback_edits(ctx, layer_id="lyr") == {"ok": True}
    assert [c[0][0] for c in mock_connection.send_command.call_args_list] == [
        "start_editing",
        "commit_edits",
        "rollback_edits",
    ]


@pytest.mark.asyncio
async def test_undo_redo_edits(mock_connection):
    mock_connection.returns({"undone": 2})
    await srv.undo_edits(make_ctx(), layer_id="lyr", steps=2)
    mock_connection.send_command.assert_called_with(
        "undo_edits", {"layer_id": "lyr", "steps": 2}, timeout=30
    )
    await srv.redo_edits(make_ctx(), layer_id="lyr")
    mock_connection.send_command.assert_called_with(
        "redo_edits", {"layer_id": "lyr", "steps": 1}, timeout=30
    )


@pytest.mark.asyncio
async def test_update_feature_geometry_tool(mock_connection):
    mock_connection.returns({"updated": 1})
    output = await srv.update_feature_geometry(
        make_ctx(), layer_id="lyr", updates=[{"fid": 1, "geometry_wkt": "POINT(1 2)"}]
    )
    assert output == {"updated": 1}
    mock_connection.send_command.assert_called_once_with(
        "update_feature_geometry",
        {"layer_id": "lyr", "updates": [{"fid": 1, "geometry_wkt": "POINT(1 2)"}]},
        timeout=30,
    )


@pytest.mark.asyncio
async def test_set_raster_style_defaults(mock_connection):
    mock_connection.returns({"ok": True})
    await srv.set_raster_style(make_ctx(), layer_id="dem", style_type="singleband_pseudocolor")
    params = mock_connection.send_command.call_args[0][1]
    assert params["style_type"] == "singleband_pseudocolor"
    assert params["band"] == 1
    assert params["color_ramp"] == "Viridis"
    # Unset bounds must stay None so the plugin falls back to band statistics.
    assert params["min_value"] is None and params["max_value"] is None


@pytest.mark.asyncio
async def test_list_connections_filters_by_provider(mock_connection):
    mock_connection.returns({"connections": [{"provider": "postgres", "name": "gis"}], "count": 1})
    output = await srv.list_connections(make_ctx(), provider="postgres")
    assert output["count"] == 1
    mock_connection.send_command.assert_called_once_with(
        "list_connections", {"provider": "postgres"}, timeout=30
    )


@pytest.mark.asyncio
async def test_create_postgresql_connection_uses_auth_config_and_long_timeout(mock_connection):
    mock_connection.returns({"ok": True, "name": "warehouse", "validated": True})
    output = await srv.create_postgresql_connection(
        make_ctx(),
        name="warehouse",
        connection_mode="endpoint_using_auth_manager",
        host="db.example.test",
        port=5433,
        database="gis",
        auth_config_id="authcfg1",
        ssl_mode="verify-full",
    )
    assert output == {"ok": True, "name": "warehouse", "validated": True}
    mock_connection.send_command.assert_called_once_with(
        "create_postgresql_connection",
        {
            "name": "warehouse",
            "connection_mode": "endpoint_using_auth_manager",
            "host": "db.example.test",
            "port": 5433,
            "database": "gis",
            "auth_config_id": "authcfg1",
            "ssl_mode": "verify-full",
            "service": None,
        },
        timeout=60,
    )


@pytest.mark.asyncio
async def test_import_layer_to_connection_confirms_only_on_overwrite(mock_connection):
    mock_connection.returns({"ok": True})
    # No overwrite: nothing is destroyed, so no prompt is raised.
    ctx = make_ctx(elicitation="decline")
    assert await srv.import_layer_to_connection(
        ctx, layer_id="lyr", provider="ogr", connection="db", table="t"
    ) == {"ok": True}
    ctx.elicit.assert_not_called()
    # Overwrite replaces an existing table, so a decline must block the call.
    mock_connection.send_command.reset_mock()
    assert await srv.import_layer_to_connection(
        ctx, layer_id="lyr", provider="ogr", connection="db", table="t", overwrite=True
    ) == {"ok": False, "message": "Cancelled by user"}
    mock_connection.send_command.assert_not_called()


def test_confirmation_gated_commands_blocked_in_batch():
    """Batch must not be a way to skip the elicitation on the new destructive commands."""
    assert {
        "rollback_edits",
        "execute_connection_sql",
        "import_layer_to_connection",
    } <= BATCH_BLOCKED_COMMANDS


@pytest.mark.asyncio
async def test_blocked_command_in_batch_is_refused(mock_connection):
    """The guard must fire, and as ToolError: mcp >= 2.1 shows no other message."""
    with pytest.raises(ToolError, match="rollback_edits"):
        await srv.batch_commands(make_ctx(), [{"type": "rollback_edits"}])
    mock_connection.send_command.assert_not_called()


def test_socket_backed_resources_are_coroutines():
    """FunctionResource.read only awaits coroutines, so a sync handler would
    block the event loop for the whole socket round trip."""
    for name in SOCKET_BACKED_RESOURCES:
        assert inspect.iscoroutinefunction(getattr(srv, name)), name


def test_resource_cache_evicts_oldest_past_the_cap():
    """Nothing prunes these entries, so the cache has to bound itself."""
    srv._resource_cache.clear()
    uris = [srv._cache_as_resource([n]) for n in range(srv._CACHE_MAX_ENTRIES + 3)]
    assert len(srv._resource_cache) == srv._CACHE_MAX_ENTRIES
    oldest, newest = uris[0].rsplit("/", 1)[1], uris[-1].rsplit("/", 1)[1]
    assert oldest not in srv._resource_cache
    assert newest in srv._resource_cache
    srv._resource_cache.clear()
