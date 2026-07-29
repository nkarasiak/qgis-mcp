"""Unit tests for MCP server tools with mocked socket connection."""

import contextlib
import json
import os
import sys
import threading
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mcp_compat import make_mcp_error

from qgis_mcp.helpers import HEADER_STRUCT, get_auth_token
from qgis_mcp.server import QgisMCPClient, _ConfirmSchema, _send_sync

# --- Fixtures ---


@pytest.fixture
def mock_connection():
    """Provide a mocked QgisMCPClient that returns configurable responses."""
    mock_client = MagicMock(spec=QgisMCPClient)
    mock_client.socket = MagicMock()
    mock_client.socket.getpeername.return_value = ("localhost", 9876)
    with patch("qgis_mcp.server.get_qgis_connection", return_value=mock_client):
        yield mock_client


def _make_ctx(*, elicitation="confirm"):
    """Create a mock Context with async methods.

    elicitation: "confirm" (default) — user confirms destructive ops.
                 "decline" — user refuses.
                 "unsupported" — client doesn't support elicitation (raises McpError).

    The responses mirror the real SDK: `data` is a model instance (not a dict),
    and an unsupported client raises McpError — mocking a bare Exception with a
    dict payload is what let #27 hide.
    """
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    ctx.info = AsyncMock()
    ctx.warning = AsyncMock()
    ctx.error = AsyncMock()
    if elicitation == "unsupported":
        ctx.elicit = AsyncMock(side_effect=make_mcp_error())
    else:
        elicit_response = MagicMock()
        elicit_response.action = "accept" if elicitation == "confirm" else "decline"
        elicit_response.data = _ConfirmSchema(confirm=elicitation == "confirm")
        ctx.elicit = AsyncMock(return_value=elicit_response)
    return ctx


# --- _send_sync() helper tests ---


def test_send_unwraps_success_envelope(mock_connection):
    mock_connection.send_command.return_value = {"status": "success", "result": {"pong": True}}
    result = _send_sync("ping")
    assert result == {"pong": True}
    mock_connection.send_command.assert_called_once_with("ping", None, timeout=30)


def test_send_raises_on_error(mock_connection):
    mock_connection.send_command.return_value = {"status": "error", "message": "Layer not found"}
    with pytest.raises(RuntimeError, match="Layer not found"):
        _send_sync("get_layer_features", {"layer_id": "bad_id"})


def test_send_raises_on_none_response(mock_connection):
    mock_connection.send_command.return_value = None
    with pytest.raises(RuntimeError, match="No response"):
        _send_sync("ping")


def test_send_passes_timeout(mock_connection):
    mock_connection.send_command.return_value = {"status": "success", "result": {}}
    _send_sync("execute_processing", {"algorithm": "test"}, timeout=60)
    mock_connection.send_command.assert_called_once_with(
        "execute_processing", {"algorithm": "test"}, timeout=60
    )


def test_send_empty_result(mock_connection):
    mock_connection.send_command.return_value = {"status": "success"}
    result = _send_sync("ping")
    assert result == {}


def test_send_retries_on_broken_pipe():
    """When send_command raises BrokenPipeError, _send_sync reconnects and retries."""
    import qgis_mcp.server as srv

    first_client = MagicMock(spec=QgisMCPClient)
    first_client.socket = MagicMock()
    first_client.socket.getpeername.return_value = ("localhost", 9876)
    first_client.send_command.side_effect = BrokenPipeError("[Errno 32] Broken pipe")

    second_client = MagicMock(spec=QgisMCPClient)
    second_client.socket = MagicMock()
    second_client.socket.getpeername.return_value = ("localhost", 9876)
    second_client.send_command.return_value = {"status": "success", "result": {"pong": True}}

    # Simulate already-connected state (shorter retry schedule)
    srv._first_connected.add("default")
    try:
        with (
            patch("qgis_mcp.server.get_qgis_connection", side_effect=[first_client, second_client]),
            patch("qgis_mcp.server._invalidate_connection"),
            patch("qgis_mcp.server.time.sleep"),
        ):
            result = _send_sync("ping")
    finally:
        srv._first_connected.discard("default")

    assert result == {"pong": True}
    first_client.send_command.assert_called_once()
    second_client.send_command.assert_called_once()


def test_send_raises_after_retry_fails():
    """When all retry attempts raise connection errors, the last propagates."""
    import qgis_mcp.server as srv

    client = MagicMock(spec=QgisMCPClient)
    client.socket = MagicMock()
    client.socket.getpeername.return_value = ("localhost", 9876)
    client.send_command.side_effect = ConnectionResetError("Connection reset")

    # Simulate already-connected state (3 retries)
    srv._first_connected.add("default")
    try:
        with (
            patch("qgis_mcp.server.get_qgis_connection", return_value=client),
            patch("qgis_mcp.server._invalidate_connection"),
            patch("qgis_mcp.server.time.sleep"),
            pytest.raises(ConnectionResetError),
        ):
            _send_sync("ping")
    finally:
        srv._first_connected.discard("default")

    assert client.send_command.call_count == 3  # 3 attempts with backoff


def test_first_connect_uses_patient_retries():
    """First connection attempt uses more retries (5) with longer delays."""
    import qgis_mcp.server as srv

    client = MagicMock(spec=QgisMCPClient)
    client.socket = MagicMock()
    client.socket.getpeername.return_value = ("localhost", 9876)
    client.send_command.side_effect = ConnectionRefusedError("Connection refused")

    srv._first_connected.discard("default")
    try:
        with (
            patch("qgis_mcp.server.get_qgis_connection", return_value=client),
            patch("qgis_mcp.server._invalidate_connection"),
            patch("qgis_mcp.server.time.sleep") as mock_sleep,
            pytest.raises(ConnectionRefusedError),
        ):
            _send_sync("ping")
    finally:
        srv._first_connected.discard("default")

    assert client.send_command.call_count == 5  # 5 patient retries
    # Verify escalating delays: 1.0, 2.0, 3.0, 5.0
    delays = [call.args[0] for call in mock_sleep.call_args_list]
    assert delays == [1.0, 2.0, 3.0, 5.0]


# --- Tool-level tests (all async) ---


@pytest.mark.asyncio
async def test_ping_tool_returns_dict(mock_connection):
    mock_connection.send_command.return_value = {"status": "success", "result": {"pong": True}}
    from qgis_mcp.server import ping

    ctx = _make_ctx()
    output = await ping(ctx)
    assert isinstance(output, dict)
    assert output == {"pong": True}


@pytest.mark.asyncio
async def test_get_layers_passes_pagination(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"layers": [], "total_count": 0, "offset": 5, "limit": 10},
    }
    from qgis_mcp.server import get_layers

    ctx = _make_ctx()
    output = await get_layers(ctx, limit=10, offset=5)
    assert isinstance(output, dict)
    assert output["total_count"] == 0
    mock_connection.send_command.assert_called_once_with(
        "get_layers", {"limit": 10, "offset": 5}, timeout=30
    )


@pytest.mark.asyncio
async def test_get_layer_features_enforces_max_limit(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"features": [], "feature_count": 0, "fields": []},
    }
    from qgis_mcp.server import get_layer_features

    ctx = _make_ctx()
    await get_layer_features(ctx, layer_id="test", limit=100)
    # Should have been capped to 50
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["limit"] == 50


@pytest.mark.asyncio
async def test_get_layer_features_with_expression(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"features": [], "feature_count": 0, "fields": []},
    }
    from qgis_mcp.server import get_layer_features

    ctx = _make_ctx()
    await get_layer_features(ctx, layer_id="test", expression="name = 'Berlin'")
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["expression"] == "name = 'Berlin'"


@pytest.mark.asyncio
async def test_get_layer_features_no_expression_omitted(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"features": [], "feature_count": 0, "fields": []},
    }
    from qgis_mcp.server import get_layer_features

    ctx = _make_ctx()
    await get_layer_features(ctx, layer_id="test")
    call_params = mock_connection.send_command.call_args[0][1]
    assert "expression" not in call_params


@pytest.mark.asyncio
async def test_batch_commands_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": [
            {"status": "success", "result": {"pong": True}},
            {"status": "success", "result": {"layers": [], "total_count": 0}},
        ],
    }
    from qgis_mcp.server import batch_commands

    ctx = _make_ctx()
    output = await batch_commands(
        ctx,
        commands=[
            {"type": "ping", "params": {}},
            {"type": "get_layers", "params": {}},
        ],
    )
    assert isinstance(output, dict | list)
    assert len(output) == 2


@pytest.mark.asyncio
async def test_execute_processing_uses_long_timeout(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"algorithm": "test", "result": {}},
    }
    from qgis_mcp.server import execute_processing

    ctx = _make_ctx()
    await execute_processing(ctx, algorithm="native:buffer", parameters={"INPUT": "layer"})
    mock_connection.send_command.assert_called_once_with(
        "execute_processing",
        {"algorithm": "native:buffer", "parameters": {"INPUT": "layer"}},
        timeout=60,
    )
    ctx.info.assert_awaited_once_with("Running algorithm: native:buffer")


@pytest.mark.asyncio
async def test_render_map_returns_image_content(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"base64_data": "iVBOR==", "mime_type": "image/png", "width": 800, "height": 600},
    }
    from qgis_mcp.server import render_map

    ctx = _make_ctx()
    result = await render_map(ctx, width=800, height=600)
    assert isinstance(result, list)
    assert result[0].type == "image"
    assert result[0].data == "iVBOR=="
    ctx.info.assert_awaited_once_with("Rendering map...")


@pytest.mark.asyncio
async def test_execute_code_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"stdout": "hello", "stderr": ""},
    }
    from qgis_mcp.server import execute_code

    ctx = _make_ctx()
    result = await execute_code(ctx, code="print('hello')")
    assert result["stdout"] == "hello"
    ctx.info.assert_awaited_once_with("Executing PyQGIS code...")


# --- QgisMCPClient tests ---


def test_client_send_command_no_socket():
    client = QgisMCPClient()
    with pytest.raises(ConnectionError):
        client.send_command("ping")


# --- Phase 2 new tool tests ---


@pytest.mark.asyncio
async def test_add_features_tool(mock_connection):
    mock_connection.send_command.return_value = {"status": "success", "result": {"added": 2}}
    from qgis_mcp.server import add_features

    ctx = _make_ctx()
    output = await add_features(
        ctx,
        layer_id="test",
        features=[
            {"attributes": {"name": "A"}, "geometry_wkt": "POINT(0 0)"},
            {"attributes": {"name": "B"}, "geometry_wkt": "POINT(1 1)"},
        ],
    )
    assert output == {"added": 2}


@pytest.mark.asyncio
async def test_update_features_tool(mock_connection):
    mock_connection.send_command.return_value = {"status": "success", "result": {"updated": 1}}
    from qgis_mcp.server import update_features

    ctx = _make_ctx()
    output = await update_features(
        ctx,
        layer_id="test",
        updates=[
            {"fid": 1, "attributes": {"name": "Updated"}},
        ],
    )
    assert output == {"updated": 1}


@pytest.mark.asyncio
async def test_delete_features_by_fids(mock_connection):
    mock_connection.send_command.return_value = {"status": "success", "result": {"deleted": 2}}
    from qgis_mcp.server import delete_features

    ctx = _make_ctx()
    output = await delete_features(ctx, layer_id="test", fids=[1, 2])
    assert output == {"deleted": 2}
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["fids"] == [1, 2]


@pytest.mark.asyncio
async def test_delete_features_by_expression(mock_connection):
    mock_connection.send_command.return_value = {"status": "success", "result": {"deleted": 3}}
    from qgis_mcp.server import delete_features

    ctx = _make_ctx()
    await delete_features(ctx, layer_id="test", expression="id > 5")
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["expression"] == "id > 5"


@pytest.mark.asyncio
async def test_set_layer_style_tool(mock_connection):
    mock_connection.send_command.return_value = {"status": "success", "result": {"ok": True}}
    from qgis_mcp.server import set_layer_style

    ctx = _make_ctx()
    output = await set_layer_style(
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
    mock_connection.send_command.return_value = {"status": "success", "result": {"selected": 3}}
    from qgis_mcp.server import select_features

    ctx = _make_ctx()
    output = await select_features(ctx, layer_id="test", expression="value > 100")
    assert output == {"selected": 3}


@pytest.mark.asyncio
async def test_get_selection_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"fids": [1, 2, 3], "count": 3},
    }
    from qgis_mcp.server import get_selection

    ctx = _make_ctx()
    output = await get_selection(ctx, layer_id="test")
    assert output["count"] == 3
    assert output["fids"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_clear_selection_tool(mock_connection):
    mock_connection.send_command.return_value = {"status": "success", "result": {"ok": True}}
    from qgis_mcp.server import clear_selection

    ctx = _make_ctx()
    output = await clear_selection(ctx, layer_id="test")
    assert output == {"ok": True}


@pytest.mark.asyncio
async def test_create_memory_layer_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"id": "mem_123", "name": "test_layer", "type": "vector_0", "feature_count": 0},
    }
    from qgis_mcp.server import create_memory_layer

    ctx = _make_ctx()
    output = await create_memory_layer(
        ctx, name="test_layer", geometry_type="Point", fields=[{"name": "id", "type": "integer"}]
    )
    assert isinstance(output, list)
    assert output[0].type == "text"
    assert '"mem_123"' in output[0].text
    assert output[1].type == "resource_link"
    assert "mem_123" in str(output[1].uri)
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["geometry_type"] == "Point"
    assert call_params["fields"] == [{"name": "id", "type": "integer"}]


@pytest.mark.asyncio
async def test_list_processing_algorithms_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "algorithms": [{"id": "native:buffer", "name": "Buffer", "provider": "native"}],
            "count": 1,
        },
    }
    from qgis_mcp.server import list_processing_algorithms

    ctx = _make_ctx()
    output = await list_processing_algorithms(ctx, search="buffer")
    assert output["count"] == 1
    assert output["algorithms"][0]["id"] == "native:buffer"


@pytest.mark.asyncio
async def test_create_processing_model_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "ok": True,
            "name": "buffer_centroids_2",
            "requested_name": "buffer_centroids",
            "path": "/home/u/.local/share/QGIS/QGIS3/profiles/default/processing/models/buffer_centroids_2.model3",
            "registered": True,
            "input_count": 2,
            "step_count": 2,
            "output_count": 1,
        },
    }
    from qgis_mcp.server import create_processing_model

    ctx = _make_ctx()
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

    output = await create_processing_model(
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

    # Long timeout, full payload forwarded — no path/register/overwrite fields anymore
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
async def test_get_algorithm_help_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "id": "native:buffer",
            "name": "Buffer",
            "parameters": [],
            "outputs": [],
            "description": "",
            "provider": "native",
        },
    }
    from qgis_mcp.server import get_algorithm_help

    ctx = _make_ctx()
    output = await get_algorithm_help(ctx, algorithm_id="native:buffer")
    assert output["id"] == "native:buffer"


@pytest.mark.asyncio
async def test_find_layer_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"layers": [{"id": "l1", "name": "roads", "type": "vector_1"}], "count": 1},
    }
    from qgis_mcp.server import find_layer

    ctx = _make_ctx()
    output = await find_layer(ctx, name_pattern="road*")
    assert output["count"] == 1


@pytest.mark.asyncio
async def test_list_layouts_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"layouts": [{"name": "Map1", "page_count": 1}], "count": 1},
    }
    from qgis_mcp.server import list_layouts

    ctx = _make_ctx()
    output = await list_layouts(ctx)
    assert output["count"] == 1


@pytest.mark.asyncio
async def test_export_layout_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "path": "/tmp/layout.pdf"},
    }
    from qgis_mcp.server import export_layout

    ctx = _make_ctx()
    output = await export_layout(ctx, layout_name="Map1", path="/tmp/layout.pdf")
    assert output["ok"] is True
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["format"] == "pdf"
    assert call_params["dpi"] == 300


# --- Phase 3 new tool tests ---


@pytest.mark.asyncio
async def test_get_message_log_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "messages": [
                {
                    "tag": "QGIS MCP",
                    "message": "test",
                    "level": "info",
                    "timestamp": "2026-03-07T12:00:00",
                }
            ],
            "count": 1,
        },
    }
    from qgis_mcp.server import get_message_log

    ctx = _make_ctx()
    output = await get_message_log(ctx, limit=50)
    assert output["count"] == 1
    mock_connection.send_command.assert_called_once_with(
        "get_message_log", {"limit": 50}, timeout=30
    )


@pytest.mark.asyncio
async def test_get_message_log_with_filters(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"messages": [], "count": 0},
    }
    from qgis_mcp.server import get_message_log

    ctx = _make_ctx()
    await get_message_log(ctx, level="warning", tag="MyPlugin", limit=10)
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["level"] == "warning"
    assert call_params["tag"] == "MyPlugin"
    assert call_params["limit"] == 10


@pytest.mark.asyncio
async def test_list_plugins_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "plugins": [
                {
                    "name": "qgis_mcp_plugin",
                    "enabled": True,
                    "version": "0.3.0",
                    "path": "/plugins/qgis_mcp_plugin",
                }
            ],
            "count": 1,
        },
    }
    from qgis_mcp.server import list_plugins

    ctx = _make_ctx()
    output = await list_plugins(ctx, enabled_only=True)
    assert output["count"] == 1
    assert output["plugins"][0]["name"] == "qgis_mcp_plugin"


@pytest.mark.asyncio
async def test_get_plugin_info_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "name": "qgis_mcp_plugin",
            "enabled": True,
            "version": "0.3.0",
            "description": "MCP Plugin",
            "author": "Test",
            "path": "/plugins/qgis_mcp_plugin",
        },
    }
    from qgis_mcp.server import get_plugin_info

    ctx = _make_ctx()
    output = await get_plugin_info(ctx, plugin_name="qgis_mcp_plugin")
    assert output["name"] == "qgis_mcp_plugin"
    assert output["enabled"] is True


@pytest.mark.asyncio
async def test_reload_plugin_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"reloaded": "my_plugin", "ok": True},
    }
    from qgis_mcp.server import reload_plugin

    ctx = _make_ctx()
    output = await reload_plugin(ctx, plugin_name="my_plugin")
    assert output["ok"] is True
    assert output["reloaded"] == "my_plugin"
    ctx.info.assert_awaited_once_with("Reloading plugin: my_plugin")


@pytest.mark.asyncio
async def test_reload_plugin_self_blocked(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "error",
        "message": "Cannot reload MCP plugin (would break the connection)",
    }
    from qgis_mcp.server import reload_plugin

    ctx = _make_ctx()
    with pytest.raises(RuntimeError, match="Cannot reload MCP plugin"):
        await reload_plugin(ctx, plugin_name="qgis_mcp_plugin")


@pytest.mark.asyncio
async def test_get_layer_tree_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "children": [
                {
                    "type": "group",
                    "name": "Base Maps",
                    "visible": True,
                    "children": [
                        {
                            "type": "layer",
                            "name": "OSM",
                            "visible": True,
                            "layer_id": "osm_123",
                            "layer_type": "raster",
                        }
                    ],
                },
                {
                    "type": "layer",
                    "name": "Roads",
                    "visible": True,
                    "layer_id": "roads_456",
                    "layer_type": "vector_1",
                },
            ]
        },
    }
    from qgis_mcp.server import get_layer_tree

    ctx = _make_ctx()
    output = await get_layer_tree(ctx)
    assert len(output["children"]) == 2
    assert output["children"][0]["type"] == "group"
    assert output["children"][0]["children"][0]["type"] == "layer"


@pytest.mark.asyncio
async def test_create_layer_group_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"name": "My Group", "ok": True},
    }
    from qgis_mcp.server import create_layer_group

    ctx = _make_ctx()
    output = await create_layer_group(ctx, name="My Group")
    assert output["ok"] is True
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["name"] == "My Group"
    assert "parent" not in call_params


@pytest.mark.asyncio
async def test_create_layer_group_with_parent(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"name": "Sub Group", "ok": True},
    }
    from qgis_mcp.server import create_layer_group

    ctx = _make_ctx()
    await create_layer_group(ctx, name="Sub Group", parent="Parent Group")
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["parent"] == "Parent Group"


@pytest.mark.asyncio
async def test_move_layer_to_group_tool(mock_connection):
    mock_connection.send_command.return_value = {"status": "success", "result": {"ok": True}}
    from qgis_mcp.server import move_layer_to_group

    ctx = _make_ctx()
    output = await move_layer_to_group(ctx, layer_id="layer_123", group_name="My Group")
    assert output["ok"] is True


@pytest.mark.asyncio
async def test_set_layer_property_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "property": "opacity", "value": "0.5"},
    }
    from qgis_mcp.server import set_layer_property

    ctx = _make_ctx()
    output = await set_layer_property(ctx, layer_id="test", property="opacity", value="0.5")
    assert output["ok"] is True
    assert output["property"] == "opacity"


@pytest.mark.asyncio
async def test_get_layer_extent_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"xmin": 0.0, "ymin": 0.0, "xmax": 10.0, "ymax": 10.0, "crs": "EPSG:4326"},
    }
    from qgis_mcp.server import get_layer_extent

    ctx = _make_ctx()
    output = await get_layer_extent(ctx, layer_id="test")
    assert output["crs"] == "EPSG:4326"
    assert output["xmax"] == 10.0


@pytest.mark.asyncio
async def test_get_project_variables_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"variables": {"project_title": "Test", "custom_var": "42"}},
    }
    from qgis_mcp.server import get_project_variables

    ctx = _make_ctx()
    output = await get_project_variables(ctx)
    assert "project_title" in output["variables"]


@pytest.mark.asyncio
async def test_set_project_variable_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "key": "my_var", "value": "hello"},
    }
    from qgis_mcp.server import set_project_variable

    ctx = _make_ctx()
    output = await set_project_variable(ctx, key="my_var", value="hello")
    assert output["ok"] is True
    assert output["key"] == "my_var"


@pytest.mark.asyncio
async def test_validate_expression_with_layer(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"valid": True, "referenced_columns": ["name"]},
    }
    from qgis_mcp.server import validate_expression

    ctx = _make_ctx()
    await validate_expression(ctx, expression="\"name\" = 'Berlin'", layer_id="test_layer")
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["layer_id"] == "test_layer"
    assert call_params["expression"] == "\"name\" = 'Berlin'"


@pytest.mark.asyncio
async def test_validate_expression_without_layer(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"valid": True, "referenced_columns": []},
    }
    from qgis_mcp.server import validate_expression

    ctx = _make_ctx()
    await validate_expression(ctx, expression="1 + 1")
    call_params = mock_connection.send_command.call_args[0][1]
    assert "layer_id" not in call_params


@pytest.mark.asyncio
async def test_get_setting_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"key": "qgis/sketching/sketching_enabled", "value": True, "exists": True},
    }
    from qgis_mcp.server import get_setting

    ctx = _make_ctx()
    output = await get_setting(ctx, key="qgis/sketching/sketching_enabled")
    assert output["exists"] is True


@pytest.mark.asyncio
async def test_set_setting_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "key": "qgis/sketching/sketching_enabled"},
    }
    from qgis_mcp.server import set_setting

    ctx = _make_ctx()
    output = await set_setting(ctx, key="qgis/sketching/sketching_enabled", value="true")
    assert output["ok"] is True


# --- Phase 4 new tool tests ---


@pytest.mark.asyncio
async def test_get_canvas_screenshot_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "base64_data": "iVBOR==",
            "mime_type": "image/png",
            "width": 1024,
            "height": 768,
        },
    }
    from qgis_mcp.server import get_canvas_screenshot

    ctx = _make_ctx()
    result = await get_canvas_screenshot(ctx)
    assert isinstance(result, list)
    assert result[0].type == "image"
    assert result[0].data == "iVBOR=="


@pytest.mark.asyncio
async def test_get_3d_screenshot_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "base64_data": "iVBOR==",
            "mime_type": "image/png",
            "width": 800,
            "height": 600,
            "view_index": 0,
            "open_3d_views": 1,
        },
    }
    from qgis_mcp.server import get_3d_screenshot

    ctx = _make_ctx()
    result = await get_3d_screenshot(ctx, view_index=0, dpi=96)
    assert isinstance(result, list)
    assert result[0].type == "image"
    assert result[0].data == "iVBOR=="


@pytest.mark.asyncio
async def test_get_3d_screenshot_camera_params(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "base64_data": "iVBOR==",
            "mime_type": "image/png",
            "width": 800,
            "height": 600,
            "view_index": 1,
            "open_3d_views": 2,
        },
    }
    from qgis_mcp.server import get_3d_screenshot

    ctx = _make_ctx()
    result = await get_3d_screenshot(
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
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "source_crs": "EPSG:4326",
            "target_crs": "EPSG:3857",
            "point": {"x": 1113194.91, "y": 0.0},
        },
    }
    from qgis_mcp.server import transform_coordinates

    ctx = _make_ctx()
    output = await transform_coordinates(
        ctx, source_crs="EPSG:4326", target_crs="EPSG:3857", point={"x": 10.0, "y": 0.0}
    )
    assert output["point"]["x"] == 1113194.91
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["source_crs"] == "EPSG:4326"
    assert call_params["target_crs"] == "EPSG:3857"
    assert call_params["point"] == {"x": 10.0, "y": 0.0}


@pytest.mark.asyncio
async def test_transform_coordinates_bbox(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "source_crs": "EPSG:4326",
            "target_crs": "EPSG:3857",
            "bbox": {"xmin": 0.0, "ymin": 0.0, "xmax": 1113194.91, "ymax": 1118889.97},
        },
    }
    from qgis_mcp.server import transform_coordinates

    ctx = _make_ctx()
    output = await transform_coordinates(
        ctx,
        source_crs="EPSG:4326",
        target_crs="EPSG:3857",
        bbox={"xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10},
    )
    assert "bbox" in output
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["bbox"] == {"xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10}


# --- Elicitation tests ---


@pytest.mark.asyncio
async def test_remove_layer_proceeds_without_elicitation(mock_connection):
    """When elicitation not supported (raises), tool proceeds (fail-open)."""
    mock_connection.send_command.return_value = {"status": "success", "result": {"ok": True}}
    from qgis_mcp.server import remove_layer

    ctx = _make_ctx(elicitation="unsupported")
    output = await remove_layer(ctx, layer_id="test_layer")
    assert output == {"ok": True}
    mock_connection.send_command.assert_called_once()


@pytest.mark.asyncio
async def test_remove_layer_cancelled_by_user(mock_connection):
    """When user declines elicitation, tool returns cancelled."""
    from qgis_mcp.server import remove_layer

    ctx = _make_ctx()
    elicit_response = MagicMock()
    elicit_response.action = "accept"
    elicit_response.data = _ConfirmSchema(confirm=False)
    ctx.elicit = AsyncMock(return_value=elicit_response)
    output = await remove_layer(ctx, layer_id="test_layer")
    assert output == {"ok": False, "message": "Cancelled by user"}
    mock_connection.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_remove_layer_confirmed_by_user(mock_connection):
    """When user confirms elicitation, tool proceeds."""
    mock_connection.send_command.return_value = {"status": "success", "result": {"ok": True}}
    from qgis_mcp.server import remove_layer

    ctx = _make_ctx()
    elicit_response = MagicMock()
    elicit_response.action = "accept"
    elicit_response.data = _ConfirmSchema(confirm=True)
    ctx.elicit = AsyncMock(return_value=elicit_response)
    output = await remove_layer(ctx, layer_id="test_layer")
    assert output == {"ok": True}


# --- Env var configuration tests ---


def test_env_var_host_port():
    """Test that get_qgis_connection uses QGIS_MCP_HOST/PORT env vars."""
    with (
        patch.dict(os.environ, {"QGIS_MCP_HOST": "192.168.1.100", "QGIS_MCP_PORT": "9999"}),
        patch("qgis_mcp.server.QgisMCPClient") as mock_client_cls,
    ):
        mock_instance = MagicMock()
        mock_instance.connect.return_value = True
        mock_instance.socket = MagicMock()
        mock_client_cls.return_value = mock_instance

        import qgis_mcp.server as srv

        srv._qgis_connections.clear()
        try:
            srv.get_qgis_connection()
            mock_client_cls.assert_called_once_with(host="192.168.1.100", port=9999)
        finally:
            srv._qgis_connections.clear()
            srv._connection_validated_at.clear()


@pytest.mark.asyncio
async def test_load_project_logs_info(mock_connection):
    """Test that load_project sends ctx.info() and returns ResourceLink."""
    mock_connection.send_command.return_value = {"status": "success", "result": {"ok": True}}
    from qgis_mcp.server import load_project

    ctx = _make_ctx()
    output = await load_project(ctx, path="/tmp/test.qgz")
    ctx.info.assert_awaited_once_with("Loading project: /tmp/test.qgz")
    assert isinstance(output, list)
    assert output[0].type == "text"
    assert output[1].type == "resource_link"
    assert "qgis://project" in str(output[1].uri)


# --- Resource link tests for mutating tools ---


@pytest.mark.asyncio
async def test_add_vector_layer_returns_resource_link(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"layer_id": "vec_123", "name": "roads", "type": "vector"},
    }
    from qgis_mcp.server import add_vector_layer

    ctx = _make_ctx()
    output = await add_vector_layer(ctx, path="/tmp/roads.shp")
    assert isinstance(output, list)
    assert output[0].type == "text"
    assert '"vec_123"' in output[0].text
    assert output[1].type == "resource_link"
    assert "vec_123" in str(output[1].uri)


@pytest.mark.asyncio
async def test_add_raster_layer_returns_resource_link(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"layer_id": "ras_456", "name": "dem", "type": "raster"},
    }
    from qgis_mcp.server import add_raster_layer

    ctx = _make_ctx()
    output = await add_raster_layer(ctx, path="/tmp/dem.tif")
    assert isinstance(output, list)
    assert output[0].type == "text"
    assert '"ras_456"' in output[0].text
    assert output[1].type == "resource_link"
    assert "ras_456" in str(output[1].uri)


@pytest.mark.asyncio
async def test_create_new_project_returns_resource_link(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "path": "/tmp/new.qgz"},
    }
    from qgis_mcp.server import create_new_project

    ctx = _make_ctx()
    output = await create_new_project(ctx, path="/tmp/new.qgz")
    assert isinstance(output, list)
    assert output[0].type == "text"
    assert output[1].type == "resource_link"
    assert "qgis://project" in str(output[1].uri)


@pytest.mark.asyncio
async def test_diagnose_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "status": "healthy",
            "checks": [
                {
                    "name": "qgis",
                    "status": "ok",
                    "detail": {
                        "qgis_version": "3.34.0",
                        "python_version": "3.12.0",
                        "qt_version": "5.15.2",
                    },
                },
                {"name": "plugin_version", "status": "ok", "detail": "0.1.3"},
                {"name": "connected_clients", "status": "ok", "detail": 1},
                {"name": "processing_providers", "status": "ok", "detail": ["native", "gdal"]},
                {
                    "name": "project",
                    "status": "ok",
                    "detail": {"loaded": True, "path": "/tmp/test.qgz", "layer_count": 3},
                },
            ],
        },
    }
    from qgis_mcp.server import diagnose

    ctx = _make_ctx()
    with patch("qgis_mcp.helpers.importlib.metadata.version", return_value="0.1.3"):
        output = await diagnose(ctx)
    assert output["status"] == "healthy"
    # Should have added version_match check
    names = [c["name"] for c in output["checks"]]
    assert "version_match" in names
    ctx.info.assert_awaited_once_with("Running diagnostics...")


@pytest.mark.asyncio
async def test_diagnose_version_mismatch(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "status": "healthy",
            "checks": [
                {"name": "plugin_version", "status": "ok", "detail": "0.1.2"},
            ],
        },
    }
    from qgis_mcp.server import diagnose

    ctx = _make_ctx()
    with patch("qgis_mcp.helpers.importlib.metadata.version", return_value="0.1.3"):
        output = await diagnose(ctx)
    assert output["status"] == "degraded"
    version_check = next(c for c in output["checks"] if c["name"] == "version_match")
    assert version_check["status"] == "mismatch"


# --- Phase 5: High-value capability tests ---


@pytest.mark.asyncio
async def test_get_active_layer_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"active": True, "layer_id": "layer_123", "name": "roads", "type": "vector_1"},
    }
    from qgis_mcp.server import get_active_layer

    ctx = _make_ctx()
    output = await get_active_layer(ctx)
    assert output["active"] is True
    assert output["layer_id"] == "layer_123"


@pytest.mark.asyncio
async def test_get_active_layer_none(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"active": False, "layer_id": None, "name": None, "type": None},
    }
    from qgis_mcp.server import get_active_layer

    ctx = _make_ctx()
    output = await get_active_layer(ctx)
    assert output["active"] is False


@pytest.mark.asyncio
async def test_set_active_layer_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "layer_id": "layer_123", "name": "roads"},
    }
    from qgis_mcp.server import set_active_layer

    ctx = _make_ctx()
    output = await set_active_layer(ctx, layer_id="layer_123")
    assert output["ok"] is True
    mock_connection.send_command.assert_called_once_with(
        "set_active_layer", {"layer_id": "layer_123"}, timeout=30
    )


@pytest.mark.asyncio
async def test_get_canvas_scale_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"scale": 50000.0, "rotation": 0.0, "magnification": 1.0},
    }
    from qgis_mcp.server import get_canvas_scale

    ctx = _make_ctx()
    output = await get_canvas_scale(ctx)
    assert output["scale"] == 50000.0
    assert output["rotation"] == 0.0


@pytest.mark.asyncio
async def test_set_canvas_scale_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "scale": 25000.0, "rotation": 45.0},
    }
    from qgis_mcp.server import set_canvas_scale

    ctx = _make_ctx()
    output = await set_canvas_scale(ctx, scale=25000.0, rotation=45.0)
    assert output["ok"] is True
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["scale"] == 25000.0
    assert call_params["rotation"] == 45.0


@pytest.mark.asyncio
async def test_set_canvas_scale_only_scale(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "scale": 100000.0, "rotation": 0.0},
    }
    from qgis_mcp.server import set_canvas_scale

    ctx = _make_ctx()
    await set_canvas_scale(ctx, scale=100000.0)
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["scale"] == 100000.0
    assert "rotation" not in call_params


@pytest.mark.asyncio
async def test_get_layer_labeling_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "layer_id": "layer_123",
            "enabled": True,
            "field_name": "name",
            "is_expression": False,
            "font_size": 10.0,
            "color": "#000000",
        },
    }
    from qgis_mcp.server import get_layer_labeling

    ctx = _make_ctx()
    output = await get_layer_labeling(ctx, layer_id="layer_123")
    assert output["enabled"] is True
    assert output["field_name"] == "name"


@pytest.mark.asyncio
async def test_set_layer_labeling_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "layer_id": "layer_123", "enabled": True, "field_name": "name"},
    }
    from qgis_mcp.server import set_layer_labeling

    ctx = _make_ctx()
    output = await set_layer_labeling(
        ctx, layer_id="layer_123", field_name="name", font_size=12.0, color="#FF0000"
    )
    assert output["ok"] is True
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["field_name"] == "name"
    assert call_params["font_size"] == 12.0
    assert call_params["color"] == "#FF0000"


@pytest.mark.asyncio
async def test_set_layer_labeling_disable(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "layer_id": "layer_123", "enabled": False},
    }
    from qgis_mcp.server import set_layer_labeling

    ctx = _make_ctx()
    output = await set_layer_labeling(ctx, layer_id="layer_123", enabled=False)
    assert output["enabled"] is False
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["enabled"] is False


@pytest.mark.asyncio
async def test_get_layer_crs_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "layer_id": "layer_123",
            "authid": "EPSG:4326",
            "description": "WGS 84",
            "is_geographic": True,
            "proj4": "+proj=longlat +datum=WGS84 +no_defs",
        },
    }
    from qgis_mcp.server import get_layer_crs

    ctx = _make_ctx()
    output = await get_layer_crs(ctx, layer_id="layer_123")
    assert output["authid"] == "EPSG:4326"
    assert output["is_geographic"] is True


@pytest.mark.asyncio
async def test_set_layer_crs_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "layer_id": "layer_123", "crs": "EPSG:3857"},
    }
    from qgis_mcp.server import set_layer_crs

    ctx = _make_ctx()
    output = await set_layer_crs(ctx, layer_id="layer_123", crs="EPSG:3857")
    assert output["ok"] is True
    assert output["crs"] == "EPSG:3857"


@pytest.mark.asyncio
async def test_get_bookmarks_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "bookmarks": [
                {
                    "id": "bm1",
                    "name": "Berlin",
                    "group": "",
                    "extent": {"xmin": 13.0, "ymin": 52.0, "xmax": 13.8, "ymax": 52.7},
                    "crs": "EPSG:4326",
                }
            ],
            "count": 1,
        },
    }
    from qgis_mcp.server import get_bookmarks

    ctx = _make_ctx()
    output = await get_bookmarks(ctx)
    assert output["count"] == 1
    assert output["bookmarks"][0]["name"] == "Berlin"


@pytest.mark.asyncio
async def test_add_bookmark_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "id": "bm2", "name": "Munich"},
    }
    from qgis_mcp.server import add_bookmark

    ctx = _make_ctx()
    output = await add_bookmark(
        ctx, name="Munich", xmin=11.3, ymin=48.0, xmax=11.8, ymax=48.3
    )
    assert output["ok"] is True
    call_params = mock_connection.send_command.call_args[0][1]
    assert call_params["name"] == "Munich"
    assert call_params["crs"] == "EPSG:4326"


@pytest.mark.asyncio
async def test_remove_bookmark_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "id": "bm1"},
    }
    from qgis_mcp.server import remove_bookmark

    ctx = _make_ctx()
    output = await remove_bookmark(ctx, bookmark_id="bm1")
    assert output["ok"] is True


@pytest.mark.asyncio
async def test_get_map_themes_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {
            "themes": [
                {"name": "Base Map", "visible_layer_count": 2, "visible_layer_ids": ["l1", "l2"]}
            ],
            "count": 1,
        },
    }
    from qgis_mcp.server import get_map_themes

    ctx = _make_ctx()
    output = await get_map_themes(ctx)
    assert output["count"] == 1
    assert output["themes"][0]["name"] == "Base Map"


@pytest.mark.asyncio
async def test_add_map_theme_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "name": "My Theme", "action": "created"},
    }
    from qgis_mcp.server import add_map_theme

    ctx = _make_ctx()
    output = await add_map_theme(ctx, name="My Theme")
    assert output["ok"] is True
    assert output["action"] == "created"


@pytest.mark.asyncio
async def test_remove_map_theme_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "name": "My Theme"},
    }
    from qgis_mcp.server import remove_map_theme

    ctx = _make_ctx()
    output = await remove_map_theme(ctx, name="My Theme")
    assert output["ok"] is True


@pytest.mark.asyncio
async def test_apply_map_theme_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "name": "My Theme"},
    }
    from qgis_mcp.server import apply_map_theme

    ctx = _make_ctx()
    output = await apply_map_theme(ctx, name="My Theme")
    assert output["ok"] is True


@pytest.mark.asyncio
async def test_set_project_crs_tool(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "crs": "EPSG:3857", "description": "WGS 84 / Pseudo-Mercator"},
    }
    from qgis_mcp.server import set_project_crs

    ctx = _make_ctx()
    output = await set_project_crs(ctx, crs="EPSG:3857")
    assert isinstance(output, list)
    assert output[0].type == "text"
    assert '"EPSG:3857"' in output[0].text
    assert output[1].type == "resource_link"
    assert "qgis://project" in str(output[1].uri)


@pytest.mark.asyncio
async def test_run_model_uses_long_timeout(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"model": "model:flow", "result": {}},
    }
    from qgis_mcp.server import run_model

    ctx = _make_ctx()
    await run_model(ctx, model="model:flow", parameters={"INPUT": "lyr"})
    mock_connection.send_command.assert_called_once_with(
        "run_model", {"model": "model:flow", "parameters": {"INPUT": "lyr"}}, timeout=60
    )


@pytest.mark.asyncio
async def test_run_model_defaults_empty_parameters(mock_connection):
    mock_connection.send_command.return_value = {"status": "success", "result": {}}
    from qgis_mcp.server import run_model

    await run_model(_make_ctx(), model="model:flow")
    mock_connection.send_command.assert_called_once_with(
        "run_model", {"model": "model:flow", "parameters": {}}, timeout=60
    )


@pytest.mark.asyncio
async def test_list_processing_models(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"models": [{"id": "model:a"}], "count": 1},
    }
    from qgis_mcp.server import list_processing_models

    result = await list_processing_models(_make_ctx())
    assert result["count"] == 1
    mock_connection.send_command.assert_called_once_with(
        "list_processing_models", None, timeout=30
    )


@pytest.mark.asyncio
async def test_execute_processing_batch_long_timeout(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"results": [], "count": 0},
    }
    from qgis_mcp.server import execute_processing_batch

    plist = [{"INPUT": "a"}, {"INPUT": "b"}]
    await execute_processing_batch(_make_ctx(), algorithm="native:buffer", parameters_list=plist)
    mock_connection.send_command.assert_called_once_with(
        "execute_processing_batch",
        {"algorithm": "native:buffer", "parameters_list": plist},
        timeout=60,
    )


@pytest.mark.asyncio
async def test_zonal_statistics_passes_defaults(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"output_layer_id": "x"},
    }
    from qgis_mcp.server import zonal_statistics

    await zonal_statistics(_make_ctx(), polygon_layer="poly", raster_layer="dem")
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
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"samples": [], "count": 0},
    }
    from qgis_mcp.server import sample_raster_values

    await sample_raster_values(_make_ctx(), raster_layer="dem", points=[[1.0, 2.0]])
    mock_connection.send_command.assert_called_once_with(
        "sample_raster_values",
        {"raster_layer": "dem", "points": [[1.0, 2.0]], "band": None},
        timeout=30,
    )


@pytest.mark.asyncio
async def test_export_layer_with_reproject(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "output": "out.gpkg"},
    }
    from qgis_mcp.server import export_layer

    await export_layer(
        _make_ctx(), layer_id="lyr", output_path="out.gpkg", target_crs="EPSG:4326"
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
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"ok": True, "updated": 5, "created": True},
    }
    from qgis_mcp.server import field_calculator

    result = await field_calculator(
        _make_ctx(), layer_id="lyr", field_name="area_m2", expression="$area"
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
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"field": "type", "values": ["a", "b"], "count": 2},
    }
    from qgis_mcp.server import get_unique_values

    result = await get_unique_values(_make_ctx(), layer_id="lyr", field="type")
    assert result["count"] == 2
    mock_connection.send_command.assert_called_once_with(
        "get_unique_values", {"layer_id": "lyr", "field": "type", "limit": 1000}, timeout=30
    )


@pytest.mark.asyncio
async def test_spatial_join_long_timeout(mock_connection):
    mock_connection.send_command.return_value = {
        "status": "success",
        "result": {"output_layer_id": "j"},
    }
    from qgis_mcp.server import spatial_join

    await spatial_join(_make_ctx(), target_layer="t", join_layer="j")
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


# --- Phase 8: layout/atlas authoring, query & management tool tests ---


def _ok(result):
    return {"status": "success", "result": result}


@pytest.mark.asyncio
async def test_get_layout_info_tool(mock_connection):
    mock_connection.send_command.return_value = _ok({"items": [], "count": 0})
    from qgis_mcp.server import get_layout_info

    await get_layout_info(_make_ctx(), layout_name="Map1")
    assert mock_connection.send_command.call_args[0][0] == "get_layout_info"
    assert mock_connection.send_command.call_args[0][1] == {"layout_name": "Map1"}


@pytest.mark.asyncio
async def test_add_layout_label_tool(mock_connection):
    mock_connection.send_command.return_value = _ok({"ok": True, "uuid": "x"})
    from qgis_mcp.server import add_layout_label

    await add_layout_label(_make_ctx(), layout_name="Map1", text="Title", font_size=18)
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "add_layout_label"
    assert params["text"] == "Title"
    assert params["font_size"] == 18


@pytest.mark.asyncio
async def test_add_layout_legend_tool(mock_connection):
    mock_connection.send_command.return_value = _ok({"ok": True, "uuid": "x"})
    from qgis_mcp.server import add_layout_legend

    await add_layout_legend(_make_ctx(), layout_name="Map1", title="Key")
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "add_layout_legend"
    assert params["title"] == "Key"


@pytest.mark.asyncio
async def test_add_layout_scalebar_tool(mock_connection):
    mock_connection.send_command.return_value = _ok({"ok": True, "uuid": "x"})
    from qgis_mcp.server import add_layout_scalebar

    await add_layout_scalebar(_make_ctx(), layout_name="Map1", style="Numeric")
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "add_layout_scalebar"
    assert params["style"] == "Numeric"


@pytest.mark.asyncio
async def test_add_layout_picture_tool(mock_connection):
    mock_connection.send_command.return_value = _ok({"ok": True, "uuid": "x"})
    from qgis_mcp.server import add_layout_picture

    await add_layout_picture(_make_ctx(), layout_name="Map1", path="/logo.svg")
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "add_layout_picture"
    assert params["path"] == "/logo.svg"


@pytest.mark.asyncio
async def test_add_layout_table_tool(mock_connection):
    mock_connection.send_command.return_value = _ok({"ok": True, "uuid": "x"})
    from qgis_mcp.server import add_layout_table

    await add_layout_table(_make_ctx(), layout_name="Map1", layer_id="L1", max_rows=5)
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "add_layout_table"
    assert params["layer_id"] == "L1"
    assert params["max_rows"] == 5


@pytest.mark.asyncio
async def test_configure_atlas_tool(mock_connection):
    mock_connection.send_command.return_value = _ok({"ok": True, "count": 3})
    from qgis_mcp.server import configure_atlas

    await configure_atlas(
        _make_ctx(), layout_name="Map1", coverage_layer="L1", filter_expression="pop > 0"
    )
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "configure_atlas"
    assert params["coverage_layer"] == "L1"
    assert params["filter_expression"] == "pop > 0"


@pytest.mark.asyncio
async def test_export_atlas_tool(mock_connection):
    mock_connection.send_command.return_value = _ok({"ok": True, "count": 3})
    from qgis_mcp.server import export_atlas

    await export_atlas(_make_ctx(), layout_name="Map1", output_path="/out.pdf")
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "export_atlas"
    assert params["output_path"] == "/out.pdf"
    assert mock_connection.send_command.call_args[1]["timeout"] == 60


@pytest.mark.asyncio
async def test_remove_layout_tool_confirms(mock_connection):
    mock_connection.send_command.return_value = _ok({"ok": True, "removed": "Map1"})
    from qgis_mcp.server import remove_layout

    output = await remove_layout(_make_ctx(), layout_name="Map1")
    assert output["ok"] is True
    assert mock_connection.send_command.call_args[0][0] == "remove_layout"


@pytest.mark.asyncio
async def test_remove_layout_tool_fail_open(mock_connection):
    # elicitation unsupported should still proceed (fail-open, like other destructive tools)
    mock_connection.send_command.return_value = _ok({"ok": True, "removed": "Map1"})
    from qgis_mcp.server import remove_layout

    output = await remove_layout(_make_ctx(elicitation="unsupported"), layout_name="Map1")
    assert output["ok"] is True
    assert mock_connection.send_command.call_args[0][0] == "remove_layout"


@pytest.mark.asyncio
async def test_execute_sql_tool(mock_connection):
    mock_connection.send_command.return_value = _ok({"fields": ["a"], "rows": [], "count": 0})
    from qgis_mcp.server import execute_sql

    await execute_sql(_make_ctx(), query="select * from roads")
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "execute_sql"
    assert params["query"] == "select * from roads"
    assert mock_connection.send_command.call_args[1]["timeout"] == 60


@pytest.mark.asyncio
async def test_evaluate_expression_tool(mock_connection):
    mock_connection.send_command.return_value = _ok({"result": 42})
    from qgis_mcp.server import evaluate_expression

    output = await evaluate_expression(_make_ctx(), expression="1 + 41")
    assert output["result"] == 42
    assert mock_connection.send_command.call_args[0][0] == "evaluate_expression"


@pytest.mark.asyncio
async def test_identify_features_tool(mock_connection):
    mock_connection.send_command.return_value = _ok({"point": [1, 2], "results": []})
    from qgis_mcp.server import identify_features

    await identify_features(_make_ctx(), point=[1.0, 2.0], tolerance=5.0)
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "identify_features"
    assert params["point"] == [1.0, 2.0]
    assert params["tolerance"] == 5.0


@pytest.mark.asyncio
async def test_duplicate_layer_tool(mock_connection):
    mock_connection.send_command.return_value = _ok({"ok": True, "output_layer_id": "L2"})
    from qgis_mcp.server import duplicate_layer

    await duplicate_layer(_make_ctx(), layer_id="L1", new_name="copy")
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "duplicate_layer"
    assert params == {"layer_id": "L1", "new_name": "copy"}


@pytest.mark.asyncio
async def test_set_layer_order_tool(mock_connection):
    mock_connection.send_command.return_value = _ok({"ok": True, "order": ["L1", "L2"]})
    from qgis_mcp.server import set_layer_order

    await set_layer_order(_make_ctx(), layer_ids=["L1", "L2"])
    cmd, params = mock_connection.send_command.call_args[0][:2]
    assert cmd == "set_layer_order"
    assert params == {"layer_ids": ["L1", "L2"]}


def test_compound_tools_register():
    """Test that compound tools can be registered."""
    from qgis_mcp.compound_tools import register_compound_tools

    mock_mcp = MagicMock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)

    register_compound_tools(
        mock_mcp,
        _send=AsyncMock(),
        _confirm_destructive=AsyncMock(return_value=True),
    )

    # Should have registered ~19 compound tools (15 main + 4 additional)
    assert mock_mcp.tool.call_count >= 14


@pytest.mark.asyncio
async def test_destructive_tool_actually_elicits(mock_connection):
    """Regression for #27: the confirmation prompt must reach the client.

    A dict passed where `ctx.elicit` wants a pydantic model raised before any
    request was sent, and the blanket `except Exception` reported that as
    "client doesn't support elicitation" and proceeded anyway.
    """
    from mcp.types import ElicitResult
    from mcp_compat import connect

    from qgis_mcp.server import mcp

    mock_connection.send_command.return_value = {"status": "success", "result": {"ok": True}}
    asked = []

    async def elicitation_callback(context, params):
        asked.append(params.message)
        return ElicitResult(action="accept", content={"confirm": False})  # refuse

    async with connect(mcp, elicitation_callback=elicitation_callback) as client:
        result = await client.call_tool("remove_layer", {"layer_id": "L1"})

    assert len(asked) == 1, "client was never asked to confirm"
    assert "L1" in asked[0]
    # Refusing must stop the command from reaching QGIS.
    assert mock_connection.send_command.call_count == 0
    assert "cancelled" in result.content[0].text.lower()


@pytest.mark.asyncio
async def test_confirm_destructive_declined_blocks_send(mock_connection):
    """A declined confirmation must abort rather than fall through to fail-open."""
    from qgis_mcp.server import remove_layer

    mock_connection.send_command.return_value = {"status": "success", "result": {"ok": True}}
    output = await remove_layer(_make_ctx(elicitation="decline"), layer_id="L1")
    assert output["ok"] is False
    assert mock_connection.send_command.call_count == 0


@pytest.mark.asyncio
async def test_confirm_destructive_reraises_non_mcp_errors(mock_connection):
    """Only McpError means "unsupported"; anything else must surface (#27)."""
    from qgis_mcp.server import _confirm_destructive

    ctx = _make_ctx()
    ctx.elicit = AsyncMock(side_effect=AttributeError("'dict' object has no attribute ..."))
    with pytest.raises(AttributeError):
        await _confirm_destructive(ctx, "Remove layer?")


@pytest.mark.asyncio
async def test_compound_tools_expose_params_object():
    """Regression for #24: compound schemas must carry an object-typed `params`.

    A `**kwargs` signature degenerates into a required string named `kwargs`,
    which makes every parameterised action uncallable.
    """
    from qgis_mcp.compound_tools import FastMCP, register_compound_tools

    mcp = FastMCP("compound-schema-test")
    register_compound_tools(
        mcp,
        _send=AsyncMock(return_value={}),
        _confirm_destructive=AsyncMock(return_value=True),
    )

    tools = await mcp.list_tools()
    assert tools
    for tool in tools:
        # mcp >= 2.0 renamed Tool.inputSchema -> Tool.input_schema
        schema = getattr(tool, "input_schema", None) or tool.inputSchema
        props = schema["properties"]
        assert "kwargs" not in props, f"{tool.name} still exposes **kwargs"
        assert set(props) == {"action", "params"}, tool.name
        assert schema.get("required") == ["action"], tool.name
        # params must accept an arbitrary object (nullable, defaulted)
        variants = props["params"].get("anyOf", [props["params"]])
        assert any(v.get("type") == "object" for v in variants), tool.name


@pytest.mark.asyncio
async def test_compound_tool_forwards_params_to_send():
    """Parameters passed inside `params` must reach the underlying command."""
    from qgis_mcp.compound_tools import FastMCP, register_compound_tools

    send = AsyncMock(return_value={"expression": "2+3", "result": 5})
    mcp = FastMCP("compound-call-test")
    register_compound_tools(mcp, _send=send, _confirm_destructive=AsyncMock(return_value=True))

    await mcp.call_tool("expression", {"action": "evaluate", "params": {"expression": "2+3"}})
    cmd, params = send.call_args[0][:2]
    assert cmd == "evaluate_expression"
    assert params["expression"] == "2+3"


# --- MCP server tool-discovery (no QGIS required) ---


@pytest.mark.asyncio
async def test_server_advertises_tools():
    """MCP server must expose a non-empty tool list to any MCP client.

    This validates generic MCP interoperability: the server module correctly
    registers all tools via FastMCP so that agents (Claude Code, Codex CLI,
    Nous/Hermes-style clients, etc.) can discover them without a live QGIS
    connection.
    """
    from qgis_mcp.server import mcp

    tools = await mcp.list_tools()
    tool_names = [t.name for t in tools]

    # Core tools that every MCP client depends on for basic interoperability
    assert "ping" in tool_names, "ping tool missing — clients use it to verify connectivity"
    assert "get_layers" in tool_names, "get_layers tool missing"
    assert "render_map" in tool_names, "render_map tool missing"

    # Sanity-check that the full tool suite is registered (not just a stub).
    # The server currently exposes 103 granular tools; allow some headroom for
    # future additions/removals while still catching gross omissions.
    assert len(tools) >= 100, f"Expected at least 100 tools, got {len(tools)}"


@pytest.mark.asyncio
async def test_server_tool_schemas_are_valid():
    """Every registered tool must have a non-empty name and description.

    Generic MCP clients (including Nous/Hermes agents) rely on tool metadata
    to build system prompts and tool-call payloads — missing descriptions
    degrade agent reasoning quality.
    """
    from qgis_mcp.server import mcp

    tools = await mcp.list_tools()
    for tool in tools:
        assert tool.name, f"Tool has empty name: {tool!r}"
        assert tool.description, f"Tool '{tool.name}' has no description"


# --- Optional shared-secret auth (QGIS_MCP_TOKEN) ---


def test_get_auth_token_disabled_by_default(monkeypatch):
    monkeypatch.delenv("QGIS_MCP_TOKEN", raising=False)
    assert get_auth_token() is None
    # Whitespace-only is treated as unset.
    monkeypatch.setenv("QGIS_MCP_TOKEN", "   ")
    assert get_auth_token() is None


def test_get_auth_token_when_set(monkeypatch):
    monkeypatch.setenv("QGIS_MCP_TOKEN", "s3cr3t")
    assert get_auth_token() == "s3cr3t"


def _client_capturing_send(response):
    """A QgisMCPClient whose socket captures sent bytes and returns a framed
    response, so we can inspect the exact command payload that was sent."""
    client = QgisMCPClient()
    client.socket = MagicMock()
    sent = bytearray()
    client.socket.sendall.side_effect = lambda b: sent.extend(b)

    resp_bytes = json.dumps(response).encode("utf-8")
    frames = [HEADER_STRUCT.pack(len(resp_bytes)), resp_bytes]

    def fake_recv_exact(n):
        return frames.pop(0)

    client._recv_exact = fake_recv_exact  # type: ignore[method-assign]
    return client, sent


def _sent_command(sent):
    """Decode the JSON command from captured (header + payload) bytes."""
    return json.loads(bytes(sent)[4:].decode("utf-8"))


def test_send_command_attaches_token_when_set(monkeypatch):
    monkeypatch.setenv("QGIS_MCP_TOKEN", "tok123")
    client, sent = _client_capturing_send({"status": "success", "result": {}})
    client.send_command("ping")
    assert _sent_command(sent)["token"] == "tok123"


def test_send_command_omits_token_when_unset(monkeypatch):
    monkeypatch.delenv("QGIS_MCP_TOKEN", raising=False)
    client, sent = _client_capturing_send({"status": "success", "result": {}})
    client.send_command("ping")
    assert "token" not in _sent_command(sent)


def test_compound_mode_covers_every_granular_command():
    """Compound mode must reach every plugin command the granular tools expose.

    Compound mode is meant to be a re-packaging of the same surface, not a
    subset — an action missing here means the feature is unreachable for any
    client running with QGIS_MCP_TOOL_MODE=compound.
    """
    import re
    from pathlib import Path

    src_dir = Path(__file__).resolve().parent.parent / "src" / "qgis_mcp"
    granular_src = (src_dir / "server.py").read_text()
    compound_src = (src_dir / "compound_tools.py").read_text()

    def sent_commands(text):
        return set(re.findall(r'_send\(\s*"([a-z0-9_]+)"', text))

    granular = sent_commands(granular_src)
    compound = sent_commands(compound_src)
    # Commands reached through indirection rather than a literal _send() call.
    compound |= set(re.findall(r'"\w+":\s*"([a-z0-9_]+)"', compound_src.split("def register")[0]))
    compound |= {"validate_expression", "evaluate_expression"}  # chosen via a ternary

    missing = sorted(granular - compound)
    assert not missing, f"commands unreachable in compound mode: {missing}"


@pytest.mark.asyncio
async def test_compound_new_groups_registered():
    """The field/analysis groups and the extended layer/processing actions exist."""
    from qgis_mcp.compound_tools import FastMCP, register_compound_tools

    mcp = FastMCP("compound-groups-test")
    register_compound_tools(
        mcp, _send=AsyncMock(return_value={}), _confirm_destructive=AsyncMock(return_value=True)
    )
    tools = {t.name: t.description for t in await mcp.list_tools()}
    assert {"field", "analysis"} <= set(tools)
    for action in ("export", "add_web", "save_style", "apply_style", "add_join"):
        assert action in tools["layer"], f"layer.{action} undocumented"
    for action in ("execute_batch", "get_providers", "list_models", "run_model"):
        assert action in tools["processing"], f"processing.{action} undocumented"


@pytest.mark.asyncio
async def test_compound_field_and_analysis_dispatch():
    """New actions must forward their params to the right plugin command."""
    from mcp_compat import connect

    from qgis_mcp.compound_tools import FastMCP, register_compound_tools

    send = AsyncMock(return_value={"ok": True})
    mcp = FastMCP("compound-dispatch-test")
    register_compound_tools(mcp, _send=send, _confirm_destructive=AsyncMock(return_value=True))

    # ctx.info() on some actions needs a live request context.
    async with connect(mcp) as client:
        await client.call_tool(
            "field",
            {
                "action": "calculate",
                "params": {"layer_id": "L1", "field_name": "v2", "expression": '"v" * 2'},
            },
        )
        cmd, params = send.call_args[0][:2]
        assert cmd == "field_calculator"
        assert params["expression"] == '"v" * 2'
        assert params["field_type"] == "double"

        await client.call_tool(
            "analysis",
            {
                "action": "zonal_statistics",
                "params": {"polygon_layer": "P", "raster_layer": "R", "stats": [0, 2]},
            },
        )
        cmd, params = send.call_args[0][:2]
        assert cmd == "zonal_statistics"
        assert params["stats"] == [0, 2]
        assert params["band"] == 1

        await client.call_tool("processing", {"action": "run_model", "params": {"model": "model:x"}})
        cmd, params = send.call_args[0][:2]
        assert cmd == "run_model"
        assert params == {"model": "model:x", "parameters": {}}


# --- Multi-instance configuration (QGIS_MCP_INSTANCES) ---


def test_parse_instances_name_port():
    """`name=port` entries take the default host."""
    from qgis_mcp.server import parse_instances

    assert parse_instances("default=9876,b=9877") == {
        "default": ("localhost", 9876),
        "b": ("localhost", 9877),
    }


def test_parse_instances_host_port():
    """`name=host:port` entries carry their own host."""
    from qgis_mcp.server import parse_instances

    assert parse_instances("lab=192.168.1.5:9876, local = 9877 ") == {
        "lab": ("192.168.1.5", 9876),
        "local": ("localhost", 9877),
    }


def test_parse_instances_uses_supplied_default_host():
    """A hostless entry inherits the QGIS_MCP_HOST-derived default."""
    from qgis_mcp.server import parse_instances

    assert parse_instances("a=9876", default_host="10.0.0.2") == {"a": ("10.0.0.2", 9876)}


def test_parse_instances_preserves_order():
    from qgis_mcp.server import parse_instances

    assert list(parse_instances("z=9876,a=9877,m=9878")) == ["z", "a", "m"]


@pytest.mark.parametrize(
    ("spec", "match"),
    [
        ("9876", "must be 'name=port'"),
        ("a=", "must be 'name=port'"),
        ("bad name=9876", r"\[A-Za-z0-9_-\]\+"),
        ("a.b=9876", r"\[A-Za-z0-9_-\]\+"),
        ("a=9876,a=9877", "duplicate instance name"),
        ("a=notaport", "must be an integer 1-65535"),
        ("a=0", "must be an integer 1-65535"),
        ("a=70000", "must be an integer 1-65535"),
        (",  ,", "lists no instances"),
    ],
)
def test_parse_instances_rejects_invalid(spec, match):
    from qgis_mcp.server import parse_instances

    with pytest.raises(ValueError, match=match):
        parse_instances(spec)


def test_get_instances_unset_env_is_backward_compatible():
    """No QGIS_MCP_INSTANCES → exactly one 'default' instance on the old defaults."""
    from qgis_mcp.server import get_instances

    with patch.dict(os.environ, {}, clear=True):
        assert get_instances() == {"default": ("localhost", 9876)}


def test_get_instances_unset_env_honours_host_port():
    from qgis_mcp.server import get_instances

    with patch.dict(os.environ, {"QGIS_MCP_HOST": "10.0.0.9", "QGIS_MCP_PORT": "9999"}, clear=True):
        assert get_instances() == {"default": ("10.0.0.9", 9999)}


def test_get_instances_rejects_bad_port_env():
    from qgis_mcp.server import get_instances

    with (
        patch.dict(os.environ, {"QGIS_MCP_PORT": "abc"}, clear=True),
        pytest.raises(ValueError, match="QGIS_MCP_PORT must be an integer 1-65535"),
    ):
        get_instances()


def test_get_instances_reads_instances_env():
    from qgis_mcp.server import get_instances

    with patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,b=lab:9877"}, clear=True):
        assert get_instances() == {"a": ("localhost", 9876), "b": ("lab", 9877)}


def test_resolve_instance_defaults_to_default():
    from qgis_mcp.server import resolve_instance

    with patch.dict(os.environ, {}, clear=True):
        assert resolve_instance(None) == "default"
        assert resolve_instance("default") == "default"


def test_unknown_instance_error_lists_configured_names():
    from qgis_mcp.server import resolve_instance

    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,b=9877"}, clear=True),
        pytest.raises(ValueError, match=r"Unknown QGIS instance: 'nope'.*Configured instances: a, b"),
    ):
        resolve_instance("nope")


def test_instance_less_call_falls_back_to_first_entry():
    """Without a 'default' entry, an instance-less call uses the FIRST configured one.

    Requiring an entry literally named 'default' would break the natural config
    'a=9876,b=9877' for every instance-less call, which is how tools are
    overwhelmingly invoked.
    """
    from qgis_mcp.server import resolve_instance

    with patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,b=9877"}, clear=True):
        assert resolve_instance(None) == "a"
    # Written in the other order, the other entry wins — insertion order, not sorted.
    with patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "zeta=9877,alpha=9876"}, clear=True):
        assert resolve_instance(None) == "zeta"


def test_explicit_default_entry_wins_over_first():
    """'default' is preferred wherever it appears in the spec, not just first."""
    from qgis_mcp.server import resolve_instance

    with patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,default=9877"}, clear=True):
        assert resolve_instance(None) == "default"


def test_compound_mode_refuses_multiple_instances():
    """Compound tools cannot select an instance, so the combination must not start.

    Silently routing every compound call to one instance while the config
    advertises several is a wrong-instance write with no error — worse than
    refusing to boot.
    """
    import subprocess

    env = {
        **os.environ,
        "QGIS_MCP_TOOL_MODE": "compound",
        "QGIS_MCP_INSTANCES": "a=9876,b=9877",
        "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src"),
    }
    proc = subprocess.run(
        [sys.executable, "-c", "import qgis_mcp.server"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "does not support multiple instances" in proc.stderr


def test_compound_mode_allows_single_instance():
    """Single-instance compound mode is unaffected by the guard."""
    import subprocess

    env = {
        **os.environ,
        "QGIS_MCP_TOOL_MODE": "compound",
        "QGIS_MCP_INSTANCES": "solo=9876",
        "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src"),
    }
    proc = subprocess.run(
        [sys.executable, "-c", "import qgis_mcp.server"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_pool_keys_connections_by_instance():
    """Two instance names produce two distinct, separately cached clients."""
    import qgis_mcp.server as srv

    def make_client(host, port):
        m = MagicMock(spec=QgisMCPClient)
        m.connect.return_value = True
        m.socket = MagicMock()
        return m

    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,b=9877"}, clear=True),
        patch("qgis_mcp.server.QgisMCPClient", side_effect=make_client) as mock_cls,
    ):
        srv._qgis_connections.clear()
        srv._connection_validated_at.clear()
        try:
            client_a = srv.get_qgis_connection("a")
            client_b = srv.get_qgis_connection("b")

            assert client_a is not client_b
            assert srv._qgis_connections == {"a": client_a, "b": client_b}
            assert mock_cls.call_args_list == [
                call(host="localhost", port=9876),
                call(host="localhost", port=9877),
            ]
            # Within the TTL the pooled client is reused, not recreated.
            assert srv.get_qgis_connection("a") is client_a
            assert mock_cls.call_count == 2
        finally:
            srv._qgis_connections.clear()
            srv._connection_validated_at.clear()


def test_get_qgis_connection_rejects_unknown_instance():
    import qgis_mcp.server as srv

    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876"}, clear=True),
        pytest.raises(ValueError, match="Unknown QGIS instance: 'ghost'"),
    ):
        srv.get_qgis_connection("ghost")


def test_invalidate_connection_only_drops_its_own_instance():
    import qgis_mcp.server as srv

    client_a = MagicMock(spec=QgisMCPClient)
    client_b = MagicMock(spec=QgisMCPClient)
    srv._qgis_connections.update({"a": client_a, "b": client_b})
    srv._connection_validated_at.update({"a": 1.0, "b": 2.0})
    try:
        srv._invalidate_connection("a")
        assert srv._qgis_connections == {"b": client_b}
        assert srv._connection_validated_at == {"b": 2.0}
        client_a.disconnect.assert_called_once()
        client_b.disconnect.assert_not_called()
    finally:
        srv._qgis_connections.clear()
        srv._connection_validated_at.clear()


def test_locks_are_per_instance():
    """A call in flight on one instance must not serialize calls to another.

    Holds instance 'a's lock (as a live send would) and checks that a send to
    'b' completes while a second send to 'a' blocks until the lock is released.
    """
    import qgis_mcp.server as srv

    client = MagicMock(spec=QgisMCPClient)
    client.socket = MagicMock()
    client.send_command.return_value = {"status": "success", "result": {"pong": True}}

    assert srv._get_instance_lock("a") is not srv._get_instance_lock("b")
    assert srv._get_instance_lock("a") is srv._get_instance_lock("a")

    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,b=9877"}, clear=True),
        patch("qgis_mcp.server.get_qgis_connection", return_value=client),
    ):
        srv._first_connected.update({"a", "b"})
        done_b = threading.Event()

        def send_to_b():
            _send_sync("ping", instance="b")
            done_b.set()

        # Both threads are daemons and time-limited: with a single global lock
        # they would never finish, and the test must fail rather than hang.
        other = threading.Thread(target=send_to_b, daemon=True)
        blocked = threading.Thread(target=_send_sync, args=("ping", None, 30, "a"), daemon=True)
        try:
            with srv._get_instance_lock("a"):
                # 'b' is unaffected by 'a' being busy...
                other.start()
                assert done_b.wait(timeout=5), "call to 'b' serialized behind in-flight call to 'a'"
                # ...while a second call to 'a' really does wait
                blocked.start()
                blocked.join(timeout=0.3)
                assert blocked.is_alive(), "second call to 'a' was not serialized"
            blocked.join(timeout=5)
            assert not blocked.is_alive(), "call to 'a' did not proceed after the lock was released"
        finally:
            for thread in (other, blocked):
                if thread.ident is not None:  # started; a failed assert may skip one
                    thread.join(timeout=5)
            srv._first_connected.difference_update({"a", "b"})


def test_send_sync_resolves_instance_and_uses_its_connection():
    import qgis_mcp.server as srv

    client = MagicMock(spec=QgisMCPClient)
    client.socket = MagicMock()
    client.send_command.return_value = {"status": "success", "result": {"ok": True}}

    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,b=9877"}, clear=True),
        patch("qgis_mcp.server.get_qgis_connection", return_value=client) as mock_get,
    ):
        srv._first_connected.update({"a", "b"})
        try:
            assert _send_sync("ping", instance="b") == {"ok": True}
        finally:
            srv._first_connected.difference_update({"a", "b"})
    mock_get.assert_called_once_with("b")


def test_send_sync_rejects_unknown_instance(mock_connection):
    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876"}, clear=True),
        pytest.raises(ValueError, match="Unknown QGIS instance: 'b'"),
    ):
        _send_sync("ping", instance="b")
    mock_connection.send_command.assert_not_called()


def test_send_sync_defaults_to_default_instance(mock_connection):
    """No instance argument keeps the pre-multi-instance behaviour."""
    import qgis_mcp.server as srv

    with (
        patch.dict(os.environ, {}, clear=True),
        patch("qgis_mcp.server.get_qgis_connection", return_value=mock_connection) as mock_get,
    ):
        srv._first_connected.add("default")
        mock_connection.send_command.return_value = {"status": "success", "result": {"pong": True}}
        try:
            assert _send_sync("ping") == {"pong": True}
        finally:
            srv._first_connected.discard("default")
    mock_get.assert_called_once_with("default")


@pytest.mark.asyncio
async def test_every_tool_forwards_instance():
    """Every @mcp.tool function must pass its `instance` argument to _send_sync.

    Calls each registered tool with instance='probe' and a stubbed _send_sync,
    then asserts the stub only ever saw 'probe'. A tool that drops the argument
    (or forgets to declare it) silently talks to the wrong QGIS window, which no
    per-tool assertion would catch.
    """
    import inspect

    import qgis_mcp.server as srv

    seen: list = []

    def fake_send_sync(command_type, params=None, timeout=30, instance=None):
        seen.append((command_type, instance))
        return {}

    def dummy(annotation):
        text = str(annotation)
        for needle, value in (("list", []), ("dict", {}), ("bool", False), ("float", 1.0), ("int", 1)):
            if needle in text:
                return value
        return "x"

    tools = await srv.mcp.list_tools()
    tool_fns = [getattr(srv, t.name) for t in tools if hasattr(srv, t.name)]
    assert len(tool_fns) == len(tools), "some tools are not module-level functions"

    checked, skipped = 0, []
    with patch("qgis_mcp.server._send_sync", fake_send_sync):
        for fn in tool_fns:
            sig = inspect.signature(fn)
            if "instance" not in sig.parameters:
                skipped.append(fn.__name__)
                continue
            kwargs = {
                name: dummy(p.annotation)
                for name, p in sig.parameters.items()
                if name not in ("ctx", "instance") and p.default is inspect.Parameter.empty
            }
            seen.clear()
            with contextlib.suppress(Exception):
                # Some tools post-process the (empty) stub result and raise; the
                # forwarding has already been recorded by then.
                await fn(_make_ctx(), instance="probe", **kwargs)
            assert seen, f"{fn.__name__} never reached _send_sync"
            assert all(i == "probe" for _, i in seen), f"{fn.__name__} dropped instance: {seen}"
            checked += 1

    assert checked >= 100, f"only {checked} tools exercised"
    assert skipped == ["list_qgis_instances"], f"tools missing an instance parameter: {skipped}"


@pytest.mark.asyncio
async def test_list_qgis_instances_reports_configuration_and_reachability():
    from qgis_mcp.server import list_qgis_instances

    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,b=lab:9877"}, clear=True),
        patch("qgis_mcp.server._probe_instance", side_effect=[True, False]),
    ):
        result = await list_qgis_instances(_make_ctx())

    assert result == {
        "instances": [
            {"name": "a", "host": "localhost", "port": 9876, "reachable": True},
            {"name": "b", "host": "lab", "port": 9877, "reachable": False},
        ],
        # No entry is named 'default', so instance-less calls land on 'a' —
        # reporting the constant "default" here would misdirect every caller
        # that reads this field to find out where its calls go.
        "implicit_instance": "a",
        "count": 2,
    }


@pytest.mark.asyncio
async def test_list_instances_reports_default_when_one_is_named_default():
    """With an explicit 'default' entry, that is what instance-less calls use."""
    from qgis_mcp.server import list_qgis_instances

    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,default=9877"}, clear=True),
        patch("qgis_mcp.server._probe_instance", side_effect=[True, True]),
    ):
        result = await list_qgis_instances(_make_ctx())

    assert result["implicit_instance"] == "default"


def test_probe_instance_reports_unreachable_port():
    """An unused port probes False (and does not take the retry-loop path)."""
    import socket as socket_mod

    from qgis_mcp.server import _probe_instance

    with socket_mod.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        free_port = sock.getsockname()[1]
    assert _probe_instance("gone", "127.0.0.1", free_port, timeout=0.5) is False


def test_probe_instance_reports_reachable_listener():
    import socket as socket_mod

    from qgis_mcp.server import _probe_instance

    listener = socket_mod.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        port = listener.getsockname()[1]
        assert _probe_instance("live", "127.0.0.1", port, timeout=0.5) is True
    finally:
        listener.close()


def _stub_plugin_server(label, stop):
    """Minimal length-prefixed echo server standing in for the QGIS plugin.

    Returns (port, received) — `received` collects the decoded commands so a
    test can prove which socket a call actually reached.
    """
    import socket as socket_mod
    import threading as threading_mod

    received: list = []
    listener = socket_mod.socket()
    listener.setsockopt(socket_mod.SOL_SOCKET, socket_mod.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]

    def serve():
        conns: list = []
        try:
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except TimeoutError:
                    continue
                conns.append(conn)
                threading_mod.Thread(target=handle, args=(conn,), daemon=True).start()
        finally:
            listener.close()
            for conn in conns:
                with contextlib.suppress(OSError):
                    conn.close()

    def handle(conn):
        conn.settimeout(0.2)
        while not stop.is_set():
            try:
                header = conn.recv(4)
            except TimeoutError:
                continue
            except OSError:
                return
            if len(header) < 4:
                return
            size = HEADER_STRUCT.unpack(header)[0]
            payload = b""
            while len(payload) < size:
                chunk = conn.recv(size - len(payload))
                if not chunk:
                    return
                payload += chunk
            command = json.loads(payload.decode("utf-8"))
            received.append(command)
            body = json.dumps(
                {"status": "success", "result": {"served_by": label, "type": command["type"]}}
            ).encode("utf-8")
            conn.sendall(HEADER_STRUCT.pack(len(body)) + body)

    threading.Thread(target=serve, daemon=True).start()
    return port, received


def test_two_instances_route_over_their_own_sockets():
    """End-to-end over real sockets: each instance reaches only its own server.

    Mocked-client tests cannot show that the pool actually opens two distinct
    TCP connections and sends each command to the right one.
    """
    import qgis_mcp.server as srv

    stop = threading.Event()
    port_a, received_a = _stub_plugin_server("alpha", stop)
    port_b, received_b = _stub_plugin_server("beta", stop)

    srv._qgis_connections.clear()
    srv._connection_validated_at.clear()
    try:
        with patch.dict(
            os.environ,
            {"QGIS_MCP_INSTANCES": f"default=127.0.0.1:{port_a},b=127.0.0.1:{port_b}"},
            clear=True,
        ):
            assert _send_sync("ping") == {"served_by": "alpha", "type": "ping"}
            assert _send_sync("get_layers", instance="b") == {
                "served_by": "beta",
                "type": "get_layers",
            }
            # Both connections stay pooled and independent.
            assert set(srv._qgis_connections) == {"default", "b"}
            assert srv._qgis_connections["default"] is not srv._qgis_connections["b"]
            assert srv._qgis_connections["default"].port == port_a
            assert srv._qgis_connections["b"].port == port_b
            # A second call reuses the same socket rather than reconnecting.
            assert _send_sync("ping", instance="b") == {"served_by": "beta", "type": "ping"}

        assert [c["type"] for c in received_a] == ["ping"]
        assert [c["type"] for c in received_b] == ["get_layers", "ping"]
    finally:
        for name in list(srv._qgis_connections):
            srv._invalidate_connection(name)
        srv._first_connected.difference_update({"default", "b"})
        stop.set()
