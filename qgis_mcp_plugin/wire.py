"""Stdlib-only wire helpers for the plugin's socket server.

Deliberately free of ``qgis`` imports so the framing logic can be unit-tested
outside QGIS - the plugin-side mirror of ``qgis_mcp.protocol`` on the client
side. Must stay importable on Python 3.9 (QGIS 3.28 ships it).
"""

from __future__ import annotations

import errno
import struct

HEADER_STRUCT = struct.Struct(">I")  # 4-byte big-endian uint32 length prefix
RECV_CHUNK_SIZE = 65536
MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10 MB - inbound frame/buffer limit

# Cap on bytes queued for one client that has stopped reading. Renders and
# screenshots are base64 and can legitimately reach several MB, so this sits
# well above a single large response but still bounds a stalled peer.
MAX_OUTBOUND_BYTES = 64 * 1024 * 1024  # 64 MB

# Commands refused inside a `batch`. Mirrors qgis_mcp.protocol's frozenset of
# the same name; tests assert the two stay identical. Enforced here (plugin
# side) as well as in the MCP server so a direct socket client cannot slip a
# destructive command past the confirmation flow by wrapping it in a batch.
# `batch` itself is listed: nesting is refused, so no depth to bound.
BATCH_BLOCKED_COMMANDS = frozenset(
    {
        "batch",
        "execute_code",
        "remove_layer",
        "delete_features",
        "set_setting",
        "reload_plugin",
        "rollback_edits",
        "execute_connection_sql",
        "import_layer_to_connection",
    }
)

# Errnos meaning "socket buffer full, try again later" rather than a real error.
_WOULD_BLOCK = frozenset({errno.EAGAIN, errno.EWOULDBLOCK})


class OutboundOverflow(Exception):
    """Raised when a client's unsent backlog exceeds ``MAX_OUTBOUND_BYTES``."""


def frame(payload: bytes) -> bytes:
    """Return *payload* prefixed with its 4-byte big-endian length header."""
    return HEADER_STRUCT.pack(len(payload)) + payload


def zip_strict(*iterables):
    """``zip(*iterables, strict=True)`` that also works on Python 3.9.

    The ``strict`` keyword landed in 3.10, and QGIS bundles 3.9 well past the
    plugin's 3.28 minimum. Calling it there raises "zip() takes no keyword
    arguments" at call time - an error CI never sees, because the test suite
    runs on a newer interpreter.

    Inputs are materialised so length can be compared up front; every call
    site here passes short, already-realised sequences.
    """
    columns = [list(it) for it in iterables]
    if len({len(c) for c in columns}) > 1:
        raise ValueError(
            "zip_strict() argument lengths differ: " + ", ".join(str(len(c)) for c in columns)
        )
    return zip(*columns)


class OutboundBuffer:
    """Bytes queued for one non-blocking client socket.

    ``socket.sendall`` cannot be used on a non-blocking socket: when the kernel
    send buffer fills mid-payload it raises ``BlockingIOError`` having already
    written an unknown prefix, leaving a half-written frame on the wire and a
    permanently desynced peer. This queues the remainder instead and drains it
    on later event-loop ticks.
    """

    def __init__(self, max_bytes: int = MAX_OUTBOUND_BYTES) -> None:
        self._buf = bytearray()
        self._max_bytes = max_bytes

    def __len__(self) -> int:
        return len(self._buf)

    @property
    def pending(self) -> bool:
        """True when bytes are still waiting to be written."""
        return bool(self._buf)

    def append(self, data: bytes) -> None:
        """Queue *data*, raising :class:`OutboundOverflow` past the cap."""
        if len(self._buf) + len(data) > self._max_bytes:
            raise OutboundOverflow(
                f"outbound backlog would exceed {self._max_bytes} bytes "
                f"({len(self._buf)} queued, {len(data)} more) - client is not reading"
            )
        self._buf.extend(data)

    def flush(self, sock) -> bool:
        """Write as much as the socket accepts. True when fully drained.

        Partial writes and would-block conditions leave the remainder queued
        for the next call. Real socket errors propagate.
        """
        while self._buf:
            try:
                sent = sock.send(memoryview(self._buf))
            except BlockingIOError:
                return False
            except OSError as exc:
                if exc.errno in _WOULD_BLOCK:
                    return False
                raise
            if sent <= 0:
                # A non-blocking send returning 0 means no progress is possible
                # right now; treat it as would-block rather than spinning.
                return False
            del self._buf[:sent]
        return True
