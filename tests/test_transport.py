"""The socket transport: _send_sync's retry schedule, framing, the shared secret, version drift."""

import json
import re
from unittest.mock import MagicMock, patch

import pytest
from conftest import make_ctx

import qgis_mcp.server as srv
from qgis_mcp import protocol
from qgis_mcp.helpers import HEADER_STRUCT, enrich_diagnose, get_auth_token
from qgis_mcp.protocol import CommandTimeout, get_client_version
from qgis_mcp.server import QgisMCPClient, ToolError, _send_sync


def test_send_unwraps_success_envelope(mock_connection):
    mock_connection.returns({"pong": True})
    result = _send_sync("ping")
    assert result == {"pong": True}
    mock_connection.send_command.assert_called_once_with("ping", None, timeout=30)


def test_send_raises_on_error(mock_connection):
    mock_connection.send_command.return_value = {"status": "error", "message": "Layer not found"}
    with pytest.raises(ToolError, match="Layer not found"):
        _send_sync("get_layer_features", {"layer_id": "bad_id"})


def test_send_raises_on_none_response(mock_connection):
    mock_connection.send_command.return_value = None
    with pytest.raises(ToolError, match="No response"):
        _send_sync("ping")


def test_send_passes_timeout(mock_connection):
    mock_connection.returns({})
    _send_sync("execute_processing", {"algorithm": "test"}, timeout=60)
    mock_connection.send_command.assert_called_once_with(
        "execute_processing", {"algorithm": "test"}, timeout=60
    )


def test_send_empty_result(mock_connection):
    mock_connection.send_command.return_value = {"status": "success"}
    result = _send_sync("ping")
    assert result == {}


def test_send_retries_on_broken_pipe(connected):
    """When send_command raises BrokenPipeError, _send_sync reconnects and retries."""
    first_client = MagicMock(spec=QgisMCPClient)
    first_client.socket = MagicMock()
    first_client.socket.getpeername.return_value = ("localhost", 9876)
    first_client.send_command.side_effect = BrokenPipeError("[Errno 32] Broken pipe")

    second_client = MagicMock(spec=QgisMCPClient)
    second_client.socket = MagicMock()
    second_client.socket.getpeername.return_value = ("localhost", 9876)
    second_client.send_command.return_value = {"status": "success", "result": {"pong": True}}

    connected("default")  # already-connected state: the shorter retry schedule
    with (
        patch("qgis_mcp.server.get_qgis_connection", side_effect=[first_client, second_client]),
        patch("qgis_mcp.server._invalidate_connection"),
        patch("qgis_mcp.server.time.sleep"),
    ):
        result = _send_sync("ping")

    assert result == {"pong": True}
    first_client.send_command.assert_called_once()
    second_client.send_command.assert_called_once()


def test_send_raises_after_retry_fails(connected):
    """When all retry attempts raise connection errors, the last propagates."""
    client = MagicMock(spec=QgisMCPClient)
    client.socket = MagicMock()
    client.socket.getpeername.return_value = ("localhost", 9876)
    client.send_command.side_effect = ConnectionResetError("Connection reset")

    connected("default")  # already-connected state: 3 retries
    with (
        patch("qgis_mcp.server.get_qgis_connection", return_value=client),
        patch("qgis_mcp.server._invalidate_connection"),
        patch("qgis_mcp.server.time.sleep"),
        pytest.raises(ConnectionResetError),
    ):
        _send_sync("ping")

    assert client.send_command.call_count == 3  # 3 attempts with backoff


def test_first_connect_uses_patient_retries(cold_start):
    """First connection attempt uses more retries (5) with longer delays."""
    client = MagicMock(spec=QgisMCPClient)
    client.socket = MagicMock()
    client.socket.getpeername.return_value = ("localhost", 9876)
    client.send_command.side_effect = ConnectionRefusedError("Connection refused")

    with (
        patch("qgis_mcp.server.get_qgis_connection", return_value=client),
        patch("qgis_mcp.server._invalidate_connection"),
        patch("qgis_mcp.server.time.sleep") as mock_sleep,
        pytest.raises(ConnectionRefusedError),
    ):
        _send_sync("ping")

    assert client.send_command.call_count == 5  # 5 patient retries
    # Verify escalating delays: 1.0, 2.0, 3.0, 5.0
    delays = [call.args[0] for call in mock_sleep.call_args_list]
    assert delays == [1.0, 2.0, 3.0, 5.0]


def test_command_timeout_is_not_retried(connected):
    """A timed-out command already reached QGIS, so retrying would run it twice."""
    client = MagicMock(spec=QgisMCPClient)
    client.socket = MagicMock()
    client.socket.getpeername.return_value = ("localhost", 9876)
    client.send_command.side_effect = CommandTimeout("Socket operation timed out after 30s")

    connected("default")
    with (
        patch("qgis_mcp.server.get_qgis_connection", return_value=client),
        patch("qgis_mcp.server._invalidate_connection") as mock_invalidate,
        patch("qgis_mcp.server.time.sleep") as mock_sleep,
        pytest.raises(CommandTimeout),
    ):
        _send_sync("add_raster_layer", {"path": "/tmp/x.tif"})

    assert client.send_command.call_count == 1
    mock_sleep.assert_not_called()
    # The socket still has an abandoned response coming, so it must go.
    mock_invalidate.assert_called_once_with("default")


def test_connect_timeout_still_retries(cold_start):
    """A slow *connect* leaves nothing running in QGIS, so patience still applies."""
    failure = ConnectionError("Could not connect to QGIS instance 'default'")
    failure.__cause__ = TimeoutError("timed out")

    with (
        patch("qgis_mcp.server.get_qgis_connection", side_effect=failure) as mock_connect,
        patch("qgis_mcp.server._invalidate_connection"),
        patch("qgis_mcp.server.time.sleep"),
        pytest.raises(ConnectionError),
    ):
        _send_sync("ping")

    assert mock_connect.call_count == 5


def test_client_send_command_no_socket():
    client = QgisMCPClient()
    with pytest.raises(ConnectionError):
        client.send_command("ping")


@pytest.mark.asyncio
async def test_diagnose_tool(mock_connection):
    mock_connection.returns(
        {
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
        }
    )

    ctx = make_ctx()
    with patch("qgis_mcp.helpers.get_client_version", return_value="0.1.3"):
        output = await srv.diagnose(ctx)
    assert output["status"] == "healthy"
    # Should have added version_match check
    names = [c["name"] for c in output["checks"]]
    assert "version_match" in names
    ctx.info.assert_awaited_once_with("Running diagnostics...")


@pytest.mark.asyncio
async def test_diagnose_version_mismatch(mock_connection):
    mock_connection.returns(
        {
            "status": "healthy",
            "checks": [
                {"name": "plugin_version", "status": "ok", "detail": "0.1.2"},
            ],
        }
    )

    ctx = make_ctx()
    with patch("qgis_mcp.helpers.get_client_version", return_value="0.1.3"):
        output = await srv.diagnose(ctx)
    assert output["status"] == "degraded"
    version_check = next(c for c in output["checks"] if c["name"] == "version_match")
    assert version_check["status"] == "mismatch"


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


def test_client_announces_its_version_on_every_command():
    """The plugin can only warn about drift if the client says who it is."""
    client, sent = _client_capturing_send({"status": "success", "result": {}})
    client.send_command("ping")

    assert _sent_command(sent)["client_version"] == get_client_version()


def test_diagnose_mismatch_carries_an_actionable_fix():
    """A reported mismatch is useless unless it says what to run."""
    result = {"status": "healthy", "checks": [{"name": "plugin_version", "detail": "9.9.9"}]}
    with patch("qgis_mcp.helpers.get_client_version", return_value="0.1.0"):
        enriched = enrich_diagnose(result)
    check = next(c for c in enriched["checks"] if c["name"] == "version_match")
    assert check["status"] == "mismatch"
    fix = check["detail"]["fix"]
    assert fix, check
    # Whichever install type is detected, the command must be runnable as-is.
    assert fix.startswith("uv "), fix
    assert "cache clean" in fix or "sync" in fix, fix
    # Never tell someone to move their own working tree around. Matched on a git
    # *invocation*, not the substring: a repo path may well contain "git".
    assert "git pull" not in fix, fix
    assert not re.search(r"(^|\s|&&)\s*git\s", fix), fix
    # A mismatch is advisory, and the report has to say so.
    assert "recommended" in check["detail"]["note"], check


def test_diagnose_match_has_no_fix_field():
    """No mismatch, no instruction - the field is the signal, not decoration."""
    result = {"status": "healthy", "checks": [{"name": "plugin_version", "detail": "1.2.3"}]}
    with patch("qgis_mcp.helpers.get_client_version", return_value="1.2.3"):
        enriched = enrich_diagnose(result)
    check = next(c for c in enriched["checks"] if c["name"] == "version_match")
    assert check["status"] == "ok"
    assert "fix" not in check["detail"]


def test_client_version_is_length_capped():
    """It reaches a QGIS user, so it is bounded before it goes on the wire."""
    original = protocol._client_version
    try:
        protocol._client_version = None
        with patch("qgis_mcp.protocol.importlib.metadata.version", return_value="x" * 200):
            assert len(protocol.get_client_version()) == protocol.MAX_VERSION_LENGTH
    finally:
        protocol._client_version = original
