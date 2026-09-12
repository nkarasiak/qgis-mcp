"""Multi-instance configuration (QGIS_MCP_INSTANCES): parsing, resolution, pool and routing.

Everything keyed by instance name: parse_instances/get_instances/resolve_instance,
the per-instance connection pool and locks, _probe_instance, and the end-to-end
check that two instances really talk over two sockets.
"""

import contextlib
import inspect
import json
import os
import socket
import threading
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from conftest import TOOL_COUNT, make_ctx

import qgis_mcp.server as srv
from qgis_mcp.helpers import HEADER_STRUCT
from qgis_mcp.server import QgisMCPClient, _send_sync


def test_parse_instances_name_port():
    """`name=port` entries take the default host."""
    assert srv.parse_instances("default=9876,b=9877") == {
        "default": ("localhost", 9876),
        "b": ("localhost", 9877),
    }


def test_parse_instances_host_port():
    """`name=host:port` entries carry their own host."""
    assert srv.parse_instances("lab=192.168.1.5:9876, local = 9877 ") == {
        "lab": ("192.168.1.5", 9876),
        "local": ("localhost", 9877),
    }


def test_parse_instances_uses_supplied_default_host():
    """A hostless entry inherits the QGIS_MCP_HOST-derived default."""
    assert srv.parse_instances("a=9876", default_host="10.0.0.2") == {"a": ("10.0.0.2", 9876)}


def test_parse_instances_preserves_order():
    assert list(srv.parse_instances("z=9876,a=9877,m=9878")) == ["z", "a", "m"]


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
    with pytest.raises(ValueError, match=match):
        srv.parse_instances(spec)


def test_get_instances_unset_env_is_backward_compatible():
    """No QGIS_MCP_INSTANCES → exactly one 'default' instance on the old defaults."""
    with patch.dict(os.environ, {}, clear=True):
        assert srv.get_instances() == {"default": ("localhost", 9876)}


def test_get_instances_unset_env_honours_host_port():
    with patch.dict(os.environ, {"QGIS_MCP_HOST": "10.0.0.9", "QGIS_MCP_PORT": "9999"}, clear=True):
        assert srv.get_instances() == {"default": ("10.0.0.9", 9999)}


def test_get_instances_rejects_bad_port_env():
    with (
        patch.dict(os.environ, {"QGIS_MCP_PORT": "abc"}, clear=True),
        pytest.raises(ValueError, match="QGIS_MCP_PORT must be an integer 1-65535"),
    ):
        srv.get_instances()


def test_get_instances_reads_instances_env():
    with patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,b=lab:9877"}, clear=True):
        assert srv.get_instances() == {"a": ("localhost", 9876), "b": ("lab", 9877)}


def test_resolve_instance_defaults_to_default():
    with patch.dict(os.environ, {}, clear=True):
        assert srv.resolve_instance(None) == "default"
        assert srv.resolve_instance("default") == "default"


def test_unknown_instance_error_lists_configured_names():
    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,b=9877"}, clear=True),
        pytest.raises(
            ValueError, match=r"Unknown QGIS instance: 'nope'.*Configured instances: a, b"
        ),
    ):
        srv.resolve_instance("nope")


def test_instance_less_call_falls_back_to_first_entry():
    """Without a 'default' entry, an instance-less call uses the FIRST configured one.

    Requiring an entry literally named 'default' would break the natural config
    'a=9876,b=9877' for every instance-less call, which is how tools are
    overwhelmingly invoked.
    """
    with patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,b=9877"}, clear=True):
        assert srv.resolve_instance(None) == "a"
    # Written in the other order, the other entry wins - insertion order, not sorted.
    with patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "zeta=9877,alpha=9876"}, clear=True):
        assert srv.resolve_instance(None) == "zeta"


def test_explicit_default_entry_wins_over_first():
    """'default' is preferred wherever it appears in the spec, not just first."""
    with patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,default=9877"}, clear=True):
        assert srv.resolve_instance(None) == "default"


def test_pool_keys_connections_by_instance(clean_pool):
    """Two instance names produce two distinct, separately cached clients."""

    def make_client(host, port):
        m = MagicMock(spec=QgisMCPClient)
        m.connect.return_value = True
        m.socket = MagicMock()
        return m

    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,b=9877"}, clear=True),
        patch("qgis_mcp.server.QgisMCPClient", side_effect=make_client) as mock_cls,
    ):
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


def test_get_qgis_connection_rejects_unknown_instance():
    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876"}, clear=True),
        pytest.raises(ValueError, match="Unknown QGIS instance: 'ghost'"),
    ):
        srv.get_qgis_connection("ghost")


def test_invalidate_connection_only_drops_its_own_instance(clean_pool):
    client_a = MagicMock(spec=QgisMCPClient)
    client_b = MagicMock(spec=QgisMCPClient)
    srv._qgis_connections.update({"a": client_a, "b": client_b})
    srv._connection_validated_at.update({"a": 1.0, "b": 2.0})

    srv._invalidate_connection("a")

    assert srv._qgis_connections == {"b": client_b}
    assert srv._connection_validated_at == {"b": 2.0}
    client_a.disconnect.assert_called_once()
    client_b.disconnect.assert_not_called()


def test_locks_are_per_instance(connected):
    """A call in flight on one instance must not serialize calls to another.

    Holds instance 'a's lock (as a live send would) and checks that a send to
    'b' completes while a second send to 'a' blocks until the lock is released.
    """
    client = MagicMock(spec=QgisMCPClient)
    client.socket = MagicMock()
    client.send_command.return_value = {"status": "success", "result": {"pong": True}}

    assert srv._get_instance_lock("a") is not srv._get_instance_lock("b")
    assert srv._get_instance_lock("a") is srv._get_instance_lock("a")

    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,b=9877"}, clear=True),
        patch("qgis_mcp.server.get_qgis_connection", return_value=client),
    ):
        connected("a", "b")
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


def test_send_sync_resolves_instance_and_uses_its_connection(connected):
    client = MagicMock(spec=QgisMCPClient)
    client.socket = MagicMock()
    client.send_command.return_value = {"status": "success", "result": {"ok": True}}

    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,b=9877"}, clear=True),
        patch("qgis_mcp.server.get_qgis_connection", return_value=client) as mock_get,
    ):
        connected("a", "b")
        assert _send_sync("ping", instance="b") == {"ok": True}
    mock_get.assert_called_once_with("b")


def test_send_sync_rejects_unknown_instance(mock_connection):
    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876"}, clear=True),
        pytest.raises(ValueError, match="Unknown QGIS instance: 'b'"),
    ):
        _send_sync("ping", instance="b")
    mock_connection.send_command.assert_not_called()


def test_send_sync_defaults_to_default_instance(mock_connection, connected):
    """No instance argument keeps the pre-multi-instance behaviour."""
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("qgis_mcp.server.get_qgis_connection", return_value=mock_connection) as mock_get,
    ):
        connected("default")
        mock_connection.returns({"pong": True})
        assert _send_sync("ping") == {"pong": True}
    mock_get.assert_called_once_with("default")


@pytest.mark.asyncio
async def test_every_tool_forwards_instance():
    """Every @mcp.tool function must pass its `instance` argument to _send_sync.

    Calls each registered tool with instance='probe' and a stubbed _send_sync,
    then asserts the stub only ever saw 'probe'. A tool that drops the argument
    (or forgets to declare it) silently talks to the wrong QGIS window, which no
    per-tool assertion would catch.
    """
    seen: list = []

    def fake_send_sync(command_type, params=None, timeout=30, instance=None, retries=None):
        # Mirror _send_sync's real signature: _send passes every argument
        # positionally, so a stub that is one parameter short raises TypeError and
        # the suppress() below would hide it as "never reached _send_sync".
        seen.append((command_type, instance))
        return {}

    def dummy(annotation):
        text = str(annotation)
        for needle, value in (
            ("list", []),
            ("dict", {}),
            ("bool", False),
            ("float", 1.0),
            ("int", 1),
        ):
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
            with contextlib.suppress(KeyError, TypeError):
                # Some tools post-process the (empty) stub result and raise a
                # KeyError on a missing field, or a TypeError on None where a
                # number was expected; the forwarding is already recorded by then.
                # Anything else is a real defect and must not be swallowed.
                await fn(make_ctx(), instance="probe", **kwargs)
            assert seen, f"{fn.__name__} never reached _send_sync"
            assert all(i == "probe" for _, i in seen), f"{fn.__name__} dropped instance: {seen}"
            checked += 1

    assert checked == TOOL_COUNT - len(skipped), f"only {checked} of {TOOL_COUNT} tools exercised"
    assert skipped == ["list_qgis_instances"], f"tools missing an instance parameter: {skipped}"


@pytest.mark.asyncio
async def test_list_qgis_instances_reports_configuration_and_reachability():
    identity = {"qgis_version": "3.40.15-Bratislava", "profile": "default", "pid": 4242}
    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,b=lab:9877"}, clear=True),
        patch("qgis_mcp.server._probe_instance", side_effect=[True, False]),
        # The reachable instance answers with an identity, which must be merged
        # into its entry; the unreachable one is never asked. Stubbed at this
        # level so the report is tested without socket I/O.
        patch("qgis_mcp.server._instance_identity", AsyncMock(return_value=identity)),
    ):
        result = await srv.list_qgis_instances(make_ctx())

    assert result == {
        "instances": [
            {"name": "a", "host": "localhost", "port": 9876, "reachable": True, **identity},
            {"name": "b", "host": "lab", "port": 9877, "reachable": False},
        ],
        # No entry is named 'default', so instance-less calls land on 'a' -
        # reporting the constant "default" here would misdirect every caller
        # that reads this field to find out where its calls go.
        "implicit_instance": "a",
        "count": 2,
    }


@pytest.mark.asyncio
async def test_list_instances_reports_default_when_one_is_named_default():
    """With an explicit 'default' entry, that is what instance-less calls use."""
    with (
        patch.dict(os.environ, {"QGIS_MCP_INSTANCES": "a=9876,default=9877"}, clear=True),
        patch("qgis_mcp.server._probe_instance", side_effect=[True, True]),
        patch("qgis_mcp.server._instance_identity", AsyncMock(return_value={})),
    ):
        result = await srv.list_qgis_instances(make_ctx())

    assert result["implicit_instance"] == "default"


def test_probe_instance_reports_unreachable_port():
    """An unused port probes False (and does not take the retry-loop path)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        free_port = sock.getsockname()[1]
    assert srv._probe_instance("gone", "127.0.0.1", free_port, timeout=0.5) is False


def test_probe_instance_reports_reachable_listener():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        port = listener.getsockname()[1]
        assert srv._probe_instance("live", "127.0.0.1", port, timeout=0.5) is True
    finally:
        listener.close()


def _stub_plugin_server(label, stop):
    """Minimal length-prefixed echo server standing in for the QGIS plugin.

    Returns (port, received) - `received` collects the decoded commands so a
    test can prove which socket a call actually reached.
    """
    received: list = []
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
                threading.Thread(target=handle, args=(conn,), daemon=True).start()
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


def test_two_instances_route_over_their_own_sockets(clean_pool, cold_start):
    """End-to-end over real sockets: each instance reaches only its own server.

    Mocked-client tests cannot show that the pool actually opens two distinct
    TCP connections and sends each command to the right one.
    """
    stop = threading.Event()
    port_a, received_a = _stub_plugin_server("alpha", stop)
    port_b, received_b = _stub_plugin_server("beta", stop)

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
        stop.set()


def test_probe_instance_reports_a_closed_peer_as_unreachable():
    """getpeername() on the pooled socket answers long after the peer is gone."""
    ours, theirs = socket.socketpair()
    with socket.socket() as closed:
        closed.bind(("localhost", 0))
        port = closed.getsockname()[1]

    conn = MagicMock()
    conn.socket = ours
    srv._qgis_connections["closed-peer"] = conn
    try:
        theirs.close()  # the QGIS side went away, the pooled socket has not noticed
        assert srv._probe_instance("closed-peer", "localhost", port) is False
    finally:
        srv._qgis_connections.pop("closed-peer", None)
        ours.close()
