"""Unit tests for the plugin-side wire helpers (no QGIS required).

Covers the non-blocking outbound path: on a non-blocking socket a large
response cannot be written with sendall(), and a partial write that is not
re-queued desyncs the client's length-prefixed framing permanently.
"""

import importlib.util
import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qgis_mcp.client import QgisMCPClient
from qgis_mcp.protocol import BATCH_BLOCKED_COMMANDS as CLIENT_BLOCKED


def _load_wire():
    """Import wire.py by path.

    ``qgis_mcp_plugin/__init__.py`` imports the plugin (and therefore ``qgis``),
    so the package cannot be imported outside QGIS. Loading the module file
    directly is what keeps these helpers unit-testable - and asserts they stay
    free of QGIS imports.
    """
    path = os.path.join(os.path.dirname(__file__), "..", "qgis_mcp_plugin", "wire.py")
    spec = importlib.util.spec_from_file_location("qgis_mcp_plugin_wire", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wire = _load_wire()

HEADER_STRUCT = wire.HEADER_STRUCT
BATCH_BLOCKED_COMMANDS = wire.BATCH_BLOCKED_COMMANDS
OutboundBuffer = wire.OutboundBuffer
OutboundOverflow = wire.OutboundOverflow
frame = wire.frame


class FakeSocket:
    """Non-blocking socket accepting a bounded number of bytes per send()."""

    def __init__(self, chunk=None, capacity=None, error=None):
        self.chunk = chunk  # max bytes accepted per send() call
        self.capacity = capacity  # total bytes before raising BlockingIOError
        self.error = error  # exception to raise on send()
        self.sent = bytearray()

    def send(self, view):
        if self.error is not None:
            raise self.error
        n = len(view)
        if self.chunk is not None:
            n = min(n, self.chunk)
        if self.capacity is not None:
            room = self.capacity - len(self.sent)
            if room <= 0:
                raise BlockingIOError("send buffer full")
            n = min(n, room)
        self.sent.extend(bytes(view[:n]))
        return n


def test_frame_prefixes_length():
    assert frame(b"abc") == HEADER_STRUCT.pack(3) + b"abc"


def test_flush_drains_when_socket_accepts_everything():
    buf = OutboundBuffer()
    sock = FakeSocket()
    buf.append(frame(b"hello"))
    assert buf.flush(sock) is True
    assert buf.pending is False
    assert bytes(sock.sent) == frame(b"hello")


def test_partial_writes_are_requeued_and_resumed():
    """The core regression: a 1 MB payload written 1 KB at a time must arrive intact."""
    payload = frame(b"x" * (1024 * 1024))
    buf = OutboundBuffer()
    buf.append(payload)
    sock = FakeSocket(chunk=1024)

    assert buf.flush(sock) is True
    assert bytes(sock.sent) == payload


def test_would_block_leaves_remainder_queued_then_resumes():
    """A full kernel buffer must not lose bytes - the frame resumes exactly where it stopped."""
    payload = frame(b"y" * 5000)
    buf = OutboundBuffer()
    buf.append(payload)

    sock = FakeSocket(capacity=2000)
    assert buf.flush(sock) is False, "should report not-drained when the socket blocks"
    assert buf.pending is True
    assert len(sock.sent) == 2000

    # Peer drains; the rest must complete the frame without duplication or loss.
    sock.capacity = None
    assert buf.flush(sock) is True
    assert bytes(sock.sent) == payload


def test_appends_after_a_partial_send_resume_from_the_read_offset():
    """Bytes queued while a frame is half-written must land after it, exactly once."""
    buf = OutboundBuffer()
    buf.append(frame(b"one"))
    sock = FakeSocket(capacity=5)
    assert buf.flush(sock) is False
    assert len(buf) == len(frame(b"one")) - 5, "len() reports what is still unsent"

    buf.append(frame(b"two"))
    sock.capacity = None
    assert buf.flush(sock) is True
    assert bytes(sock.sent) == frame(b"one") + frame(b"two")
    assert buf.pending is False


def test_compaction_does_not_lose_the_unsent_remainder():
    """The written prefix is dropped once it passes half the buffer."""
    buf = OutboundBuffer()
    payload = frame(b"z" * 100)
    buf.append(payload)
    sock = FakeSocket(capacity=len(payload) - 10)  # well past half

    assert buf.flush(sock) is False
    assert len(buf) == 10
    assert len(buf._buf) == 10, "the written prefix is not still held"

    sock.capacity = None
    assert buf.flush(sock) is True
    assert bytes(sock.sent) == payload


def test_overflow_counts_only_unsent_bytes():
    buf = OutboundBuffer(max_bytes=100)
    buf.append(b"z" * 100)
    buf.flush(FakeSocket())  # drained, so the cap is free again
    buf.append(b"z" * 100)
    assert len(buf) == 100


def test_zero_return_is_treated_as_would_block():
    buf = OutboundBuffer()
    buf.append(b"data")
    sock = FakeSocket(chunk=0)
    assert buf.flush(sock) is False
    assert buf.pending is True


def test_eagain_oserror_is_treated_as_would_block():
    import errno

    buf = OutboundBuffer()
    buf.append(b"data")
    sock = FakeSocket(error=OSError(errno.EAGAIN, "temporarily unavailable"))
    assert buf.flush(sock) is False
    assert buf.pending is True


def test_real_socket_error_propagates():
    import errno

    buf = OutboundBuffer()
    buf.append(b"data")
    sock = FakeSocket(error=OSError(errno.EPIPE, "broken pipe"))
    with pytest.raises(OSError):
        buf.flush(sock)


def test_multiple_frames_queue_in_order():
    buf = OutboundBuffer()
    buf.append(frame(b"one"))
    buf.append(frame(b"two"))
    sock = FakeSocket(chunk=3)
    assert buf.flush(sock) is True
    assert bytes(sock.sent) == frame(b"one") + frame(b"two")


def test_overflow_raises_when_client_stops_reading():
    buf = OutboundBuffer(max_bytes=100)
    buf.append(b"z" * 90)
    with pytest.raises(OutboundOverflow):
        buf.append(b"z" * 20)


def test_overflow_boundary_is_inclusive():
    buf = OutboundBuffer(max_bytes=100)
    buf.append(b"z" * 100)  # exactly at cap is allowed
    assert len(buf) == 100


def test_plugin_and_client_batch_blocklists_match():
    """Both sides must refuse the same commands, or the guard is only advisory."""
    assert BATCH_BLOCKED_COMMANDS == CLIENT_BLOCKED


# ---------------------------------------------------------------------------
# End-to-end: the real client against a server loop mirroring the plugin's
# ---------------------------------------------------------------------------


def _serve_once(listener, payload_sizes, ready):
    """Mimic the plugin's non-blocking accept/read/dispatch/write loop.

    Mirrors QgisMCPServer.process_server: non-blocking sockets throughout,
    responses queued through OutboundBuffer and drained across iterations. It is
    a stand-in, not the subject: these two tests check the real client's framing
    against a server that writes the way the plugin does. test_plugin_server.py
    is what drives the real QgisMCPServer.process_server.
    """
    import json
    import select

    clients = {}
    outbound = {}
    ready.set()
    replies_left = len(payload_sizes)
    sizes = list(payload_sizes)
    deadline = time.monotonic() + 30

    while time.monotonic() < deadline:
        # Drain pending writes first, exactly as _flush_outbound does.
        for sock in list(outbound):
            buf = outbound[sock]
            if buf.flush(sock):
                outbound.pop(sock, None)

        readable, _, _ = select.select([listener, *clients], [], [], 0.01)
        for sock in readable:
            if sock is listener:
                conn, _ = listener.accept()
                conn.setblocking(False)
                clients[conn] = b""
                continue
            try:
                data = sock.recv(RECV_CHUNK)
            except BlockingIOError:
                continue
            if not data:
                clients.pop(sock, None)
                continue
            buf = clients[sock] + data
            while len(buf) >= 4:
                msg_len = HEADER_STRUCT.unpack(buf[:4])[0]
                if len(buf) < 4 + msg_len:
                    break
                json.loads(buf[4 : 4 + msg_len].decode("utf-8"))
                buf = buf[4 + msg_len :]
                body = json.dumps(
                    {"status": "success", "result": {"blob": "A" * sizes.pop(0)}}
                ).encode("utf-8")
                out = outbound.setdefault(sock, OutboundBuffer())
                out.append(frame(body))
                out.flush(sock)
                replies_left -= 1
            clients[sock] = buf

        if replies_left == 0 and not any(b.pending for b in outbound.values()):
            break

    for sock in clients:
        sock.close()


RECV_CHUNK = 65536


def test_large_response_survives_non_blocking_socket():
    """An 8 MB response must arrive intact through a non-blocking server socket.

    This is the regression the fix exists for: ``sendall`` on a non-blocking
    socket raises BlockingIOError partway through a payload this size, leaving a
    truncated frame and a client whose framing never recovers.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.setblocking(False)
    port = listener.getsockname()[1]

    blob_size = 8 * 1024 * 1024
    ready = threading.Event()
    thread = threading.Thread(target=_serve_once, args=(listener, [blob_size], ready), daemon=True)
    thread.start()
    ready.wait(5)

    client = QgisMCPClient(host="127.0.0.1", port=port)
    try:
        assert client.connect(), "client failed to connect"
        response = client.send_command("ping", timeout=30)
        assert response["status"] == "success"
        assert len(response["result"]["blob"]) == blob_size
    finally:
        client.disconnect()
        thread.join(timeout=5)
        listener.close()


def test_framing_stays_aligned_across_successive_large_responses():
    """Consecutive big frames must not bleed into each other.

    A partial write that is dropped rather than re-queued desyncs every
    subsequent message, so the second response is the one that exposes it.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.setblocking(False)
    port = listener.getsockname()[1]

    sizes = [3 * 1024 * 1024, 2 * 1024 * 1024, 1024]
    ready = threading.Event()
    thread = threading.Thread(target=_serve_once, args=(listener, sizes, ready), daemon=True)
    thread.start()
    ready.wait(5)

    client = QgisMCPClient(host="127.0.0.1", port=port)
    try:
        assert client.connect()
        for expected in sizes:
            response = client.send_command("ping", timeout=30)
            assert response["status"] == "success"
            assert len(response["result"]["blob"]) == expected
    finally:
        client.disconnect()
        thread.join(timeout=5)
        listener.close()


def test_zip_strict_matches_builtin_behaviour():
    assert list(wire.zip_strict([1, 2], "ab")) == [(1, "a"), (2, "b")]


def test_zip_strict_rejects_length_mismatch():
    """Mirrors zip(strict=True): silently truncating is the bug it prevents."""
    with pytest.raises(ValueError):
        list(wire.zip_strict([1, 2, 3], "ab"))
