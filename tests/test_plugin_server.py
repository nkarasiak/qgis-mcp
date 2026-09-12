"""QgisMCPServer's read loop against a stubbed qgis, driven over a socketpair.

Covers the paths that never reach a handler: the auth lockout, batch shape checks,
and a handler result json cannot encode. Each used to drop the connection or leak
a traceback instead of answering.
"""

import json
import socket

import pytest
from conftest import FakeQVariant

from qgis_mcp.protocol import HEADER_STRUCT


@pytest.fixture
def server_module(plugin_handlers):
    import qgis_mcp_plugin.server as mod

    return mod


@pytest.fixture
def server(server_module):
    srv = server_module.QgisMCPServer()
    srv.running = True
    return srv


@pytest.fixture
def peer(server):
    """The client end of a connected pair; the server end is registered as a client."""
    ours, theirs = socket.socketpair()
    ours.setblocking(False)
    theirs.settimeout(1)
    server.clients[ours] = bytearray()
    yield theirs
    ours.close()
    theirs.close()


def _frame(obj):
    data = json.dumps(obj).encode("utf-8")
    return HEADER_STRUCT.pack(len(data)) + data


def _read_frame(sock):
    (length,) = HEADER_STRUCT.unpack(sock.recv(4))
    return json.loads(sock.recv(length))


def test_repeated_bad_tokens_disconnect_the_client(server, server_module, peer, monkeypatch):
    monkeypatch.setenv("QGIS_MCP_TOKEN", "right")
    peer.sendall(_frame({"type": "ping", "token": "wrong"}) * server.MAX_AUTH_FAILURES)

    server.process_server()

    assert server.clients == {} and server._auth_failures == {}
    for _ in range(server.MAX_AUTH_FAILURES - 1):
        assert _read_frame(peer)["message"] == server_module.AUTH_FAILED_MESSAGE
    assert peer.recv(4) == b"", "the server closed its end"


def test_accepted_command_resets_the_failure_count(server, peer, monkeypatch):
    monkeypatch.setenv("QGIS_MCP_TOKEN", "right")
    bad = _frame({"type": "ping", "token": "wrong"})
    good = _frame({"type": "ping", "token": "right"})
    peer.sendall(bad * (server.MAX_AUTH_FAILURES - 1) + good + bad)

    server.process_server()

    assert len(server.clients) == 1
    assert list(server._auth_failures.values()) == [1]


def test_batch_rejects_malformed_shapes(server, server_module):
    with pytest.raises(server_module.CommandError):
        server.batch("ping")

    results = server.batch([42, {"type": "batch", "params": {"commands": []}}])

    assert [r["status"] for r in results] == ["error", "error"]
    assert "object" in results[0]["message"]
    assert "'batch'" in results[1]["message"]


def test_unserializable_result_is_answered_not_dropped(server, peer):
    (ours,) = server.clients

    server._send_response(ours, {"status": "success", "result": {1, 2}})

    reply = _read_frame(peer)
    assert reply["status"] == "error" and reply["internal"] is True
    assert "serializable" in reply["message"]
    assert ours in server.clients


def test_misspelled_parameter_is_rejected_not_silently_ignored(server):
    """Nearly every handler ends in **kwargs, which swallows a typo otherwise."""
    response = server._dispatch({"type": "get_layers", "params": {"limits": 5}})

    assert response["status"] == "error"
    assert "limits" in response["message"]
    assert "limit, offset" in response["message"], "the accepted names are the hint"
    assert "internal" not in response, "a caller mistake, not a plugin defect"


def test_known_parameters_still_reach_the_handler(server):
    assert server._dispatch({"type": "ping", "params": {}}) == {
        "status": "success",
        "result": {"pong": True},
    }
    assert server._dispatch({"type": "ping"})["status"] == "success"


def test_frames_split_across_recv_chunks_are_reassembled(server, peer):
    """One request arriving in pieces must be answered once, not dropped."""
    (ours,) = server.clients
    payload = _frame({"type": "ping"})
    peer.sendall(payload[:3])

    server.process_server()
    assert bytes(server.clients[ours]) == payload[:3], "held until the frame completes"

    peer.sendall(payload[3:] + _frame({"type": "ping"}))
    server.process_server()

    assert _read_frame(peer)["result"] == {"pong": True}
    assert _read_frame(peer)["result"] == {"pong": True}
    assert bytes(server.clients[ours]) == b"", "consumed bytes are dropped"


def test_convert_attribute_turns_qt_dates_into_iso_strings(server):
    import datetime

    class FakeQDate:
        def toPyDate(self):
            return datetime.date(2020, 1, 2)

    class FakeQDateTime:
        def toPyDateTime(self):
            return datetime.datetime(2021, 3, 4, 5, 6, 7)

    class NullVariant(FakeQVariant):
        def isNull(self):
            return True

    assert server._convert_attribute(NullVariant()) is None
    assert server._convert_attribute(FakeQDate()) == "2020-01-02"
    assert server._convert_attribute(FakeQDateTime()) == "2021-03-04T05:06:07"
    assert server._convert_attribute(datetime.date(2020, 1, 2)) == "2020-01-02"
    assert server._convert_attribute(3.5) == 3.5
