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
# Per client: MAX_CLIENTS (10) stalled peers would hold 640 MB in the worst
# case, which is why the connection count is capped too.
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

# Commands left out of the session journal that export_session replays: the
# read-only ones (a test pins them to the MCP tools' readOnlyHint), plus the
# bookkeeping that makes no sense in a replay. `batch` is out because each
# command in it is journaled on its own; start_processing_job is journaled
# when the job finishes, as the execute_processing call a replay can wait on.
UNRECORDED_COMMANDS = frozenset(
    {
        # read-only
        "diagnose",
        "evaluate_expression",
        "find_layer",
        "get_3d_screenshot",
        "get_active_layer",
        "get_algorithm_help",
        "get_bookmarks",
        "get_canvas_extent",
        "get_canvas_scale",
        "get_canvas_screenshot",
        "get_edit_status",
        "get_field_statistics",
        "get_layer_crs",
        "get_layer_extent",
        "get_layer_features",
        "get_layer_labeling",
        "get_layer_tree",
        "get_layers",
        "get_layout_info",
        "get_map_themes",
        "get_message_log",
        "get_plugin_info",
        "get_processing_job",
        "get_processing_providers",
        "get_project_info",
        "get_project_variables",
        "get_qgis_info",
        "get_raster_info",
        "get_selection",
        "get_setting",
        "get_unique_values",
        "identify_features",
        "list_checkpoints",
        "list_connection_tables",
        "list_connections",
        "list_layouts",
        "list_plugins",
        "list_processing_algorithms",
        "list_processing_models",
        "ping",
        "sample_raster_values",
        "transform_coordinates",
        "validate_expression",
        # bookkeeping
        "batch",
        "cancel_processing_job",
        "create_checkpoint",
        "export_session",
        "restore_checkpoint",
        "start_processing_job",
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
        # How much of _buf has already been written. Dropping the written
        # prefix after every partial send would copy the whole remainder each
        # time, which is quadratic on exactly the payloads that need several
        # sends (multi-MB renders through a small kernel buffer).
        self._start = 0
        self._max_bytes = max_bytes

    def __len__(self) -> int:
        return len(self._buf) - self._start

    @property
    def pending(self) -> bool:
        """True when bytes are still waiting to be written."""
        return len(self._buf) > self._start

    def append(self, data: bytes) -> None:
        """Queue *data*, raising :class:`OutboundOverflow` past the cap."""
        if len(self) + len(data) > self._max_bytes:
            raise OutboundOverflow(
                f"outbound backlog would exceed {self._max_bytes} bytes "
                f"({len(self)} queued, {len(data)} more) - client is not reading"
            )
        self._buf.extend(data)

    def flush(self, sock) -> bool:
        """Write as much as the socket accepts. True when fully drained.

        Partial writes and would-block conditions leave the remainder queued
        for the next call. Real socket errors propagate.
        """
        while self._start < len(self._buf):
            start = self._start
            try:
                sent = sock.send(memoryview(self._buf)[start:])
            except BlockingIOError:
                sent = 0
            except OSError as exc:
                if exc.errno not in _WOULD_BLOCK:
                    raise
                sent = 0
            if sent <= 0:
                # No progress is possible right now (would-block, or a
                # non-blocking send returning 0): stop rather than spin.
                # Reclaim the written prefix once it is half the buffer, so a
                # peer that reads slowly does not hold the sent bytes as well.
                if self._start > len(self._buf) // 2:
                    del self._buf[: self._start]
                    self._start = 0
                return False
            self._start += sent
        del self._buf[:]  # fully drained
        self._start = 0
        return True
