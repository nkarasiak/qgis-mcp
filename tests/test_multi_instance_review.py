"""Regression tests for the multi-instance follow-up fixes.

Covers, in order: the schema cost paid by single-instance users, the retry
schedule that made a call to a closed instance cost ~21s every time, the
_probe_instance shortcut that read a pooled socket instead of the peer, and the
missing connect timeout that let a routable-but-dead host stall for minutes.
"""

import os
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import SOCKET_BACKED_RESOURCES, make_ctx
from mcp_compat import schema, uri_template

import qgis_mcp.client as client_module
import qgis_mcp.server as srv
from qgis_mcp.client import QgisMCPClient

# --- Schema cost for single-instance users ---


@pytest.mark.asyncio
async def test_instance_absent_from_schemas_with_one_instance():
    """One instance means nothing to route, so `instance` must not be advertised.

    The suite runs without QGIS_MCP_INSTANCES, i.e. the single-instance default
    that most users have.
    """
    tools = await srv.mcp.list_tools()
    offenders = [
        tool.name for tool in tools if "instance" in (schema(tool).get("properties") or {})
    ]
    assert offenders == [], f"instance advertised with a single instance: {offenders}"


@pytest.mark.asyncio
async def test_instance_still_accepted_as_an_argument_when_stripped():
    """Stripping the schema must not stop the parameter working."""
    with patch("qgis_mcp.server._send_sync", return_value={"pong": True}) as send:
        await srv.ping(make_ctx(), instance="default")

    assert send.call_args[0][3] == "default"


def test_strip_instance_param_is_a_no_op_with_several_instances():
    tool = next(iter(srv.mcp._tool_manager._tools.values()))
    properties = tool.parameters.setdefault("properties", {})
    properties["instance"] = {"type": "string"}
    try:
        with patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,b=9877"}, clear=True):
            srv._strip_instance_param()
        assert "instance" in tool.parameters["properties"]
    finally:
        tool.parameters["properties"].pop("instance", None)


# --- Instance identity (ports are not identity) ---


@pytest.mark.asyncio
async def test_listing_reports_which_qgis_answered():
    """Two windows can share a version and a profile - pid and title separate them."""
    info = {
        "qgis_version": "4.0.2-Norrköping",
        "profile_folder": "C:/Users/x/AppData/Roaming/QGIS/QGIS4\\profiles\\default/",
        "plugins_count": 5,
        "pid": 11440,
        "window_title": "city.qgz - QGIS",
    }
    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,b=9877"}, clear=True),
        patch("qgis_mcp.server._probe_instance", side_effect=[True, False]),
        patch("qgis_mcp.server._send", AsyncMock(return_value=info)),
    ):
        result = await srv.list_qgis_instances(make_ctx())

    reachable, unreachable = result["instances"]
    assert reachable["pid"] == 11440
    assert reachable["window_title"] == "city.qgz - QGIS"
    assert reachable["qgis_version"] == "4.0.2-Norrköping"
    # Basename for humans, full path to separate same-named profiles under
    # different roots. Windows mixes separators, hence the normalisation.
    assert reachable["profile"] == "default"
    assert reachable["profile_folder"] == info["profile_folder"]
    # An unreachable instance is not interrogated at all.
    assert unreachable == {"name": "b", "host": "localhost", "port": 9877, "reachable": False}


@pytest.mark.asyncio
async def test_identity_is_optional_on_older_plugins():
    """pid and window_title arrived in 0.9.0; older plugins just omit them."""
    old_plugin = {"qgis_version": "3.40.15-Bratislava", "profile_folder": "/p/default/"}
    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876"}, clear=True),
        patch("qgis_mcp.server._probe_instance", return_value=True),
        patch("qgis_mcp.server._send", AsyncMock(return_value=old_plugin)),
    ):
        result = await srv.list_qgis_instances(make_ctx())

    entry = result["instances"][0]
    assert entry["qgis_version"] == "3.40.15-Bratislava"
    assert "pid" not in entry and "window_title" not in entry


@pytest.mark.asyncio
async def test_listing_survives_an_instance_that_stops_answering():
    """Probed reachable, then failed: still reported, just without identity."""
    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876"}, clear=True),
        patch("qgis_mcp.server._probe_instance", return_value=True),
        patch("qgis_mcp.server._send", AsyncMock(side_effect=ConnectionError("gone"))),
    ):
        result = await srv.list_qgis_instances(make_ctx())

    assert result["instances"] == [
        {"name": "a", "host": "localhost", "port": 9876, "reachable": True}
    ]


@pytest.mark.asyncio
async def test_identity_does_not_retry():
    """Listing must stay quick; the retry schedule would make it cost ~11s."""
    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876"}, clear=True),
        patch("qgis_mcp.server._probe_instance", return_value=True),
        patch("qgis_mcp.server._send", AsyncMock(return_value={})) as send,
    ):
        await srv.list_qgis_instances(make_ctx())

    assert send.await_args.kwargs["retries"] == 1


def test_retries_override_beats_the_cold_start_schedule(cold_start):
    """retries=1 must win even when nothing has connected yet."""
    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876"}, clear=True),
        patch("qgis_mcp.server.get_qgis_connection", side_effect=ConnectionError("nope")) as conn,
        patch("qgis_mcp.server.time.sleep") as sleep,
        pytest.raises(ConnectionError),
    ):
        srv._send_sync("get_qgis_info", retries=1)

    assert conn.call_count == 1
    assert sleep.call_count == 0


# --- Retry schedule ---


def test_closed_instance_uses_the_short_schedule_once_anything_connected(connected):
    """A closed window is not a slow start.

    Before the fix an instance that never connected stayed on the 5-attempt
    patient schedule forever, so every call to a closed QGIS cost ~21s.
    """
    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "live=9876,closed=9877"}, clear=True),
        patch("qgis_mcp.server.get_qgis_connection", side_effect=ConnectionError("refused")),
        patch("qgis_mcp.server.time.sleep") as sleep,
    ):
        connected("live")  # something has answered
        with pytest.raises(ConnectionError):
            srv._send_sync("ping", instance="closed")

    assert sleep.call_count == srv._MAX_RETRIES - 1
    assert [call.args[0] for call in sleep.call_args_list] == list(srv._RETRY_DELAYS)


def test_refusal_is_not_retried_once_anything_connected(connected):
    """Refused means nothing is listening - repeating the call cannot change that.

    Each attempt costs the OS's refusal latency (~2s on Windows loopback), so
    retrying a closed instance three times just triples a known answer.
    """
    refused = ConnectionError("could not connect")
    refused.__cause__ = ConnectionRefusedError(10061, "actively refused")

    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "live=9876,closed=9878"}, clear=True),
        patch("qgis_mcp.server.get_qgis_connection", side_effect=refused) as connect,
        patch("qgis_mcp.server.time.sleep") as sleep,
    ):
        connected("live")
        with pytest.raises(ConnectionError):
            srv._send_sync("ping", instance="closed")

    assert connect.call_count == 1, "a refusal must not be retried"
    assert sleep.call_count == 0


def test_refusal_is_still_retried_during_cold_start(cold_start):
    """Before anything has answered, a refusal may just be QGIS still starting."""
    refused = ConnectionError("could not connect")
    refused.__cause__ = ConnectionRefusedError(10061, "actively refused")

    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "only=9876"}, clear=True),
        patch("qgis_mcp.server.get_qgis_connection", side_effect=refused) as connect,
        patch("qgis_mcp.server.time.sleep"),
        pytest.raises(ConnectionError),
    ):
        srv._send_sync("ping", instance="only")

    assert connect.call_count == srv._FIRST_CONNECT_RETRIES


def test_timeouts_are_still_retried(connected):
    """A timeout is not a refusal - a slow host deserves the retries."""
    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "live=9876,slow=9878"}, clear=True),
        patch("qgis_mcp.server.get_qgis_connection", side_effect=TimeoutError("timed out")),
        patch("qgis_mcp.server.time.sleep") as sleep,
    ):
        connected("live")
        with pytest.raises(OSError):
            srv._send_sync("ping", instance="slow")

    assert sleep.call_count == srv._MAX_RETRIES - 1


def test_connect_records_why_it_failed():
    """_send_sync needs the reason, which connect() -> bool would otherwise lose."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]

    client = QgisMCPClient(host="127.0.0.1", port=closed_port)
    assert client.connect() is False
    assert isinstance(client.last_error, ConnectionRefusedError)


def test_cold_start_keeps_the_patient_schedule(cold_start):
    """Nothing has answered yet, so QGIS may still be coming up - stay patient."""
    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "only=9876"}, clear=True),
        patch("qgis_mcp.server.get_qgis_connection", side_effect=ConnectionError("refused")),
        patch("qgis_mcp.server.time.sleep") as sleep,
        pytest.raises(ConnectionError),
    ):
        srv._send_sync("ping", instance="only")

    assert sleep.call_count == srv._FIRST_CONNECT_RETRIES - 1
    assert [call.args[0] for call in sleep.call_args_list] == list(srv._FIRST_CONNECT_DELAYS)


# --- _probe_instance race ---


class _VanishingSocket:
    """Reads as a live socket once, then None - a disconnect landing mid-probe."""

    def __init__(self):
        self.reads = 0

    @property
    def socket(self):
        self.reads += 1
        if self.reads > 1:
            return None
        live = MagicMock()
        live.getpeername.return_value = ("localhost", 9876)
        return live


def test_probe_instance_ignores_the_pooled_socket(clean_pool):
    """The pooled connection is never consulted, so no race can reach it.

    It could not answer the question either: getpeername() keeps succeeding
    after the peer closed, which reported a dead instance as reachable.
    """
    with socket.socket() as closed:
        closed.bind(("localhost", 0))
        port = closed.getsockname()[1]

    conn = _VanishingSocket()
    srv._qgis_connections["racy"] = conn
    try:
        assert srv._probe_instance("racy", "localhost", port) is False
    finally:
        srv._qgis_connections.pop("racy", None)
    assert conn.reads == 0, "the pooled socket must not be consulted"


# --- Connect timeout ---


def test_connect_fails_and_leaks_no_socket():
    """A failed connect used to leave the dead socket assigned and unclosed."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]

    client = QgisMCPClient(host="127.0.0.1", port=closed_port)
    assert client.connect() is False
    assert client.socket is None, "the failed socket must be closed, not left assigned"


def test_connect_applies_a_timeout_then_restores_blocking():
    """No timeout meant a routable-but-dead host stalled for the OS SYN timeout."""
    assert client_module._CONNECT_TIMEOUT > 0
    timeouts = []

    class FakeSocket:
        def setsockopt(self, *a):
            pass

        def settimeout(self, value):
            timeouts.append(value)

        def connect(self, address):
            pass

        def close(self):
            pass

    with patch("qgis_mcp.client.socket.socket", return_value=FakeSocket()):
        client = client_module.QgisMCPClient(host="h", port=1)
        assert client.connect() is True

    assert timeouts[0] == client_module._CONNECT_TIMEOUT
    assert timeouts[-1] is None, "blocking mode must be restored after connect"


# --- Resources say which instance they read ---


@pytest.mark.asyncio
async def test_resource_descriptions_name_the_implicit_instance():
    """Resource URIs carry no instance, so the choice must not be silent."""
    # Templated URIs (qgis://layers/{layer_id}/...) are reported separately from
    # the static ones, and every one of them reads through _send_sync.
    static = [(str(r.uri), r.description) for r in await srv.mcp.list_resources()]
    templates = [
        (uri_template(t), t.description) for t in await srv.mcp.list_resource_templates()
    ]
    routed = [
        (uri, description)
        for uri, description in static + templates
        if uri.startswith("qgis://") and "llms" not in uri and "cache" not in uri
    ]
    undocumented = [
        uri for uri, description in routed if "implicit instance" not in (description or "")
    ]

    expected = len(SOCKET_BACKED_RESOURCES)
    assert len(routed) == expected, (
        f"expected {expected} instance-routed resources, got {[u for u, _ in routed]}"
    )
    assert undocumented == [], f"undocumented routing on: {undocumented}"
