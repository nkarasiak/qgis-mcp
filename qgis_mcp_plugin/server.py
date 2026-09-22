"""The socket server that carries commands into QGIS.

Owns the listening socket, the length-prefixed framing, the optional shared-secret
auth gate and command dispatch. The commands themselves are the mixins in
:mod:`qgis_mcp_plugin.handlers`; nothing in this module knows what any of them do.

Non-blocking sockets driven by a ``QTimer`` inside QGIS's own event loop: no
thread touches the PyQGIS API, which is not thread-safe.
"""

import contextlib
import copy
import inspect
import itertools
import json
import math
import os
import secrets
import shutil
import socket
import traceback
from collections import deque
from typing import ClassVar

from qgis.core import QgsApplication, QgsMessageLog, QgsProject
from qgis.PyQt.QtCore import QObject, QTimer

from .compat import MSG_CRITICAL, MSG_INFO, MSG_WARNING
from .constants import (
    ADDR_IN_USE,
    DEFAULT_HOST,
    DEFAULT_PORT,
    MAX_PATH_LENGTH,
    MAX_VERSION_LENGTH,
    plugin_version,
)
from .errors import CommandError
from .handlers import (
    CanvasHandlers,
    ConnectionHandlers,
    FeatureHandlers,
    HandlerBase,
    LayerHandlers,
    LayoutHandlers,
    ProcessingHandlers,
    ProjectHandlers,
    SessionHandlers,
    StyleHandlers,
    SystemHandlers,
)
from .registry import COMMANDS, command
from .wire import (
    BATCH_BLOCKED_COMMANDS,
    HEADER_STRUCT,
    MAX_MESSAGE_SIZE,
    RECV_CHUNK_SIZE,
    UNRECORDED_COMMANDS,
    OutboundBuffer,
    OutboundOverflow,
    frame,
)

# Returned by execute_command on a rejected token, and matched in
# process_server, which is where the client socket is in scope to count
# failures against it.
AUTH_FAILED_MESSAGE = "Authentication failed: missing or invalid token"


def _json_safe(value):
    """Replace non-finite floats with None so responses stay valid JSON.

    ``json.dumps`` emits bare ``NaN``/``Infinity`` tokens, which are not valid
    JSON and are rejected by strict parsers. Empty layers (extent of a layer
    with no features) and raster stats with no data both produce them.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


class QgisMCPServer(
    SystemHandlers,
    ProjectHandlers,
    LayerHandlers,
    FeatureHandlers,
    StyleHandlers,
    CanvasHandlers,
    ProcessingHandlers,
    LayoutHandlers,
    ConnectionHandlers,
    SessionHandlers,
    HandlerBase,
    QObject,
):
    """Socket server: accepts commands, dispatches them, frames the replies.

    The handler mixins contribute the commands; ``base`` (``HandlerBase``) is
    last so the domain mixins can override nothing of QObject's by accident.
    """

    LOG_TAG: ClassVar[str] = "MCP"

    MAX_CLIENTS: ClassVar[int] = 10

    # A client that keeps announcing new version strings must not grow this set
    # without bound, nor produce a warning per string.
    MAX_TRACKED_VERSIONS: ClassVar[int] = 10

    # Consecutive rejected tokens tolerated on one connection before it is
    # dropped, so a guessing peer has to pay for a new connection each time.
    MAX_AUTH_FAILURES: ClassVar[int] = 5

    # Commands kept for export_session; the oldest are dropped past this.
    MAX_JOURNAL: ClassVar[int] = 1000

    def __init__(
        self,
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        iface=None,
        on_clients_changed=None,
    ):
        super().__init__()
        self.host = host
        self.port = port
        self.iface = iface
        self.on_clients_changed = on_clients_changed
        # Versions announced by connected MCP servers this session. Reported by
        # `diagnose`, by the configurator dialog, and by one log line per new
        # version - never by the message bar, which is reserved for things the
        # user just did.
        self.client_versions = set()
        # version -> the update command that client announced for itself. Only
        # the client knows whether it was launched by uvx or from a checkout, so
        # this is the only way the plugin can name the right command.
        self.client_fixes = {}
        # cmd_type -> (inspect.Signature, accepted names), filled on first use
        self._signatures = {}
        self.running = False
        self.socket = None
        # Per client: the bytes received so far that do not yet form a whole
        # frame. A bytearray so _read_frames can extend it in place.
        self.clients: dict[socket.socket, bytearray] = {}
        # Unsent response bytes per client. Sockets are non-blocking, so a large
        # response may not fit the kernel buffer in one call; the remainder is
        # queued here and drained on later timer ticks.
        self.outbound: dict[socket.socket, OutboundBuffer] = {}
        # Consecutive auth failures per client, reset by any accepted command.
        self._auth_failures: dict[socket.socket, int] = {}
        self.timer = None
        self._message_log = deque(maxlen=1000)
        self.start_error = None  # why start() failed, for the UI to report
        # Guards against re-entrant dispatch. Long handlers (render_map, and
        # processing via its feedback hook) pump the Qt event loop so the GUI
        # stays responsive, which re-fires this server's timer. Without the
        # guard a second client's command could execute *inside* the first
        # one's handler, interleaving edit sessions and project state.
        self._in_dispatch = False
        # Background processing jobs by id (ProcessingHandlers).
        self._jobs = {}
        self._job_ids = itertools.count(1)
        # Every command that changed something, for export_session (_record).
        self._journal = deque(maxlen=self.MAX_JOURNAL)
        self._journal_seq = itertools.count(1)
        # Project snapshots by id (SessionHandlers), written under one temp dir.
        self._checkpoints = {}
        self._checkpoint_ids = itertools.count(1)
        self._checkpoint_dir = None

    def _notify_clients_changed(self):
        """Report the active client count to the UI (badge on the toolbar icon)."""
        if self.on_clients_changed:
            with contextlib.suppress(Exception):
                self.on_clients_changed(len(self.clients))

    # The update command per install kind, built here rather than accepted from
    # the peer: it is shown to the user as something to run, and a client is not
    # trusted to author that text. The client announces only which kind it is.
    _FIX_COMMANDS: ClassVar[dict] = {
        "uvx": "uv cache clean qgis-mcp",
        "source": 'uv --directory "{root}" sync',
    }
    # A path is all that is ever interpolated, so anything that could end the
    # quoting or chain a second command disqualifies it.
    _PATH_REJECTED: ClassVar[frozenset] = frozenset("\"'`$;&|<>\n\r")

    @staticmethod
    def _clean_announced(raw, limit):
        """Trim a peer-supplied string to one safe, bounded line, or ''.

        Everything announced by a client is shown to the user, so it is treated
        as untrusted: newlines would let a peer forge extra lines in a message
        bar, and an unbounded string would fill it.
        """
        if not isinstance(raw, str):
            return ""
        return " ".join(raw.split())[:limit]

    def _fix_command(self, kind, raw_root):
        """The update command for an announced install kind, or ''.

        Unknown kind, or a checkout path carrying anything shell-significant,
        yields no command at all: the UI then falls back to pointing at the
        configurator, which is worse advice than an exact command but is never
        attacker-authored.
        """
        template = self._FIX_COMMANDS.get(kind)
        if template is None:
            return ""
        if "{root}" not in template:
            return template
        root = self._clean_announced(raw_root, MAX_PATH_LENGTH)
        if not root or self._PATH_REJECTED & set(root):
            return ""
        return template.format(root=root)

    def _record_client_version(self, raw, kind=None, raw_root=None):
        """Note the version an MCP server announced, and how to update it.

        Only ever called on an authenticated command: an unauthenticated peer
        must not be able to put strings in front of the user or grow this set.
        """
        version = self._clean_announced(raw, MAX_VERSION_LENGTH)
        if not version or version in self.client_versions:
            return
        if len(self.client_versions) >= self.MAX_TRACKED_VERSIONS:
            return
        self.client_versions.add(version)
        fix = self._fix_command(kind, raw_root)
        if fix:
            self.client_fixes[version] = fix

        # Once per new version, and to the log only - never the message bar.
        # Drift is otherwise invisible unless someone runs `diagnose` or opens
        # the configurator, but it is advisory, and a banner over the canvas for
        # something the user did not do is out of proportion to that.
        mine = plugin_version()
        if version == mine:
            QgsMessageLog.logMessage(f"MCP server {version} connected", self.LOG_TAG, MSG_INFO)
            return
        message = (
            f"MCP server {version} connected, but this plugin is {mine}. "
            "Not fatal: tools added since the older half was built will be "
            "missing or refused."
        )
        if fix:
            message += f" To match them, run: {fix} (then restart your MCP client)."
        QgsMessageLog.logMessage(message, self.LOG_TAG, MSG_WARNING)

    # "" is deliberately absent: binding it means INADDR_ANY, every interface.
    _LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

    def start(self):
        """Start the server"""
        self.running = True
        self.start_error = None

        # Refuse to expose an unauthenticated execute_code endpoint beyond this
        # machine. On loopback the token is advisory (a same-user process can
        # read the environment anyway), but a non-loopback bind puts arbitrary
        # PyQGIS execution on the network, where anyone who can route to the
        # port gets it. That is not a default anyone should be able to reach by
        # accident, so it requires an explicit token.
        is_loopback = str(self.host).lower() in self._LOOPBACK_HOSTS
        has_token = bool(os.environ.get("QGIS_MCP_TOKEN", "").strip())
        if not is_loopback and not has_token:
            self.start_error = (
                f"Refusing to bind {self.host}: a non-loopback address exposes "
                "arbitrary code execution to the network. Set QGIS_MCP_TOKEN "
                "(in QGIS and in the MCP server) to enable this, or bind localhost."
            )
            QgsMessageLog.logMessage(self.start_error, self.LOG_TAG, MSG_CRITICAL)
            self.running = False
            return False

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR does not mean the same thing on both platforms. On Windows
        # it lets a second socket bind a port another socket already holds, so two
        # QGIS windows on one user profile (which share the saved port in
        # QgsSettings) would BOTH report "server started" on 9876 and one of them
        # would silently swallow every connection - the wrong window answering with
        # no error anywhere. SO_EXCLUSIVEADDRUSE is the Windows way to ask for what
        # SO_REUSEADDR already gives us on Unix, and it still allows an immediate
        # rebind after stop, which is why SO_REUSEADDR was here to begin with.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows only
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.socket.bind((self.host, self.port))
            self.socket.listen(5)
            self.socket.setblocking(False)

            self.timer = QTimer()
            self.timer.timeout.connect(self.process_server)
            self.timer.start(25)  # 25ms interval

            msg_log = QgsApplication.messageLog()
            # QGIS 4.x routes messages through messageReceivedWithFormat only;
            # messageReceived no longer fires.  Fall back for 3.x.
            if hasattr(msg_log, "messageReceivedWithFormat"):
                msg_log.messageReceivedWithFormat.connect(self._capture_message)
            else:
                msg_log.messageReceived.connect(self._capture_message)
            QgsMessageLog.logMessage(
                f"QGIS MCP server started on {self.host}:{self.port}", self.LOG_TAG, MSG_INFO
            )
            auth_on = bool(os.environ.get("QGIS_MCP_TOKEN", "").strip())
            QgsMessageLog.logMessage(
                f"Token authentication {'ENABLED' if auth_on else 'disabled (open)'}",
                self.LOG_TAG,
                MSG_INFO if auth_on else MSG_WARNING,
            )
            return True
        except Exception as e:
            self.start_error = str(e)
            if isinstance(e, OSError) and e.errno in ADDR_IN_USE:
                self.start_error = (
                    f"Port {self.port} is already in use - another QGIS window or program "
                    "is on it. Pick a different port for this window."
                )
            QgsMessageLog.logMessage(f"Failed to start server: {e!s}", self.LOG_TAG, MSG_CRITICAL)
            self.stop()
            return False

    def stop(self):
        """Stop the server"""
        self.running = False

        with contextlib.suppress(Exception):
            msg_log = QgsApplication.messageLog()
            if hasattr(msg_log, "messageReceivedWithFormat"):
                msg_log.messageReceivedWithFormat.disconnect(self._capture_message)
            else:
                msg_log.messageReceived.disconnect(self._capture_message)

        if self.timer:
            self.timer.stop()
            self.timer = None

        if self.socket:
            self.socket.close()
        for client_sock in list(self.clients):
            with contextlib.suppress(Exception):
                client_sock.close()
        self.clients.clear()
        self.outbound.clear()
        self._auth_failures.clear()
        self._notify_clients_changed()
        if self._checkpoint_dir:
            shutil.rmtree(self._checkpoint_dir, ignore_errors=True)
            self._checkpoint_dir = None
            self._checkpoints.clear()

        self.socket = None
        QgsMessageLog.logMessage("QGIS MCP server stopped", self.LOG_TAG, MSG_INFO)

    def _disconnect_client(self, client_sock, message="Client disconnected", level=MSG_INFO):
        """Close and remove a client socket."""
        with contextlib.suppress(Exception):
            client_sock.close()
        self.clients.pop(client_sock, None)
        self.outbound.pop(client_sock, None)
        self._auth_failures.pop(client_sock, None)
        QgsMessageLog.logMessage(f"{message} ({len(self.clients)} active)", self.LOG_TAG, level)
        self._notify_clients_changed()

    def _send_response(self, client_sock, response):
        """Queue a length-prefixed JSON response and write what the socket takes.

        Never uses ``sendall``: these sockets are non-blocking, so a response
        larger than the kernel send buffer (base64 renders and screenshots
        routinely are) would raise ``BlockingIOError`` having already written a
        partial frame. The caller's framing would then be desynced for every
        subsequent message. Anything not accepted now stays queued and is
        flushed by :meth:`_flush_outbound` on the next tick.
        """
        try:
            payload = json.dumps(_json_safe(response))
        except (TypeError, ValueError) as e:
            # A handler returned something json cannot encode. This runs outside
            # _dispatch's try, so letting it propagate would drop the connection
            # instead of answering. The fallback dict is always encodable.
            QgsMessageLog.logMessage(
                f"Response is not JSON serializable: {e!s}", self.LOG_TAG, MSG_CRITICAL
            )
            payload = json.dumps(
                {
                    "status": "error",
                    "message": f"Response is not JSON serializable: {e!s}",
                    "internal": True,
                }
            )
        resp_bytes = payload.encode("utf-8")
        buf = self.outbound.setdefault(client_sock, OutboundBuffer())
        buf.append(frame(resp_bytes))
        if buf.flush(client_sock):
            # Fully written - drop the entry so the common case leaves nothing
            # for _flush_outbound to walk on every tick.
            self.outbound.pop(client_sock, None)

    def _flush_outbound(self):
        """Drain queued responses for every client with pending bytes."""
        for client_sock, buf in list(self.outbound.items()):
            try:
                if buf.flush(client_sock):
                    self.outbound.pop(client_sock, None)
            except Exception as e:
                self._disconnect_client(client_sock, f"Error writing to client: {e!s}", MSG_WARNING)

    def _read_frames(self, client_sock, data):
        """Yield each complete message payload in this client's buffer.

        Consumes through an offset and trims once, rather than reslicing the
        buffer per message: a 10 MB request arriving in 64 KB chunks would
        otherwise copy everything received so far on every chunk.
        """
        buf = self.clients[client_sock]
        buf.extend(data)
        if len(buf) > MAX_MESSAGE_SIZE:
            raise ValueError("Buffer exceeded 10 MB limit")
        start = 0
        try:
            while len(buf) - start >= 4:
                msg_len = HEADER_STRUCT.unpack_from(buf, start)[0]
                if msg_len > MAX_MESSAGE_SIZE:
                    raise ValueError(f"Message too large: {msg_len} bytes")
                body = start + 4
                msg_end = body + msg_len
                if len(buf) < msg_end:
                    break  # Incomplete message
                yield bytes(buf[body:msg_end])
                start = msg_end
        finally:
            # Also on the caller's break (auth lockout) and on the raises
            # above, so a consumed message is never dispatched twice.
            del buf[:start]

    def _handle_request(self, client_sock, msg_bytes):
        """Dispatch one framed request and queue its response.

        Returns False when the client was disconnected and its remaining
        frames must not be processed.
        """
        try:
            request = json.loads(msg_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            QgsMessageLog.logMessage(f"Malformed request: {e!s}", self.LOG_TAG, MSG_WARNING)
            self._send_response(client_sock, {"status": "error", "message": f"Invalid JSON: {e!s}"})
            return True

        self._in_dispatch = True
        try:
            response = self.execute_command(request)
        finally:
            self._in_dispatch = False

        if response.get("message") == AUTH_FAILED_MESSAGE:
            failures = self._auth_failures.get(client_sock, 0) + 1
            self._auth_failures[client_sock] = failures
            if failures >= self.MAX_AUTH_FAILURES:
                self._disconnect_client(
                    client_sock, "Too many authentication failures", MSG_WARNING
                )
                return False
        else:
            self._auth_failures.pop(client_sock, None)
        self._send_response(client_sock, response)
        return True

    def process_server(self):
        """Process server operations (called by timer)"""
        if not self.running:
            return

        # A handler is on the stack and is pumping the event loop to keep the
        # GUI alive. Doing anything here would run a second command inside the
        # first; the outer handler resumes this loop when it returns.
        if self._in_dispatch:
            return

        try:
            # Drain any backlog first so a client that was mid-response gets the
            # rest of its frame before we take on more work.
            self._flush_outbound()

            # Accept new connections (loop until no pending or at capacity)
            if self.socket:
                while len(self.clients) < self.MAX_CLIENTS:
                    try:
                        client_sock, address = self.socket.accept()
                        client_sock.setblocking(False)
                        self.clients[client_sock] = bytearray()
                        QgsMessageLog.logMessage(
                            f"Connected to client: {address} ({len(self.clients)} active)",
                            self.LOG_TAG,
                            MSG_INFO,
                        )
                        self._notify_clients_changed()
                    except BlockingIOError:
                        break
                    except Exception as e:
                        QgsMessageLog.logMessage(
                            f"Error accepting connection: {e!s}", self.LOG_TAG, MSG_WARNING
                        )
                        break

            # Process each connected client
            for client_sock in list(self.clients):
                try:
                    data = client_sock.recv(RECV_CHUNK_SIZE)
                    if data:
                        for msg_bytes in self._read_frames(client_sock, data):
                            if not self._handle_request(client_sock, msg_bytes):
                                break
                    else:
                        self._disconnect_client(client_sock)
                except BlockingIOError:
                    pass
                except OutboundOverflow as e:
                    # The peer stopped reading while we kept producing. Nothing
                    # can be delivered, so drop it rather than grow without bound.
                    self._disconnect_client(client_sock, f"Client not reading: {e!s}", MSG_WARNING)
                except Exception as e:
                    self._disconnect_client(client_sock, f"Error with client: {e!s}", MSG_WARNING)

        except Exception as e:
            QgsMessageLog.logMessage(f"Server error: {e!s}", self.LOG_TAG, MSG_CRITICAL)

    def execute_command(self, command):
        """Execute a command.

        Optional shared-secret auth gate: when QGIS_MCP_TOKEN is set in the
        plugin's environment, every command must carry a matching token or it
        is rejected before any handler runs. When unset, behaviour is unchanged
        (open) for backward compatibility. Dispatch itself lives in _dispatch
        so internal callers (e.g. batch) reuse it without re-authenticating.
        """
        # This runs on untrusted socket input before auth - never trust shape.
        if not isinstance(command, dict):
            return {"status": "error", "message": "Invalid command: expected an object"}

        expected_token = os.environ.get("QGIS_MCP_TOKEN", "").strip()
        if expected_token:
            provided_token = str(command.get("token") or "")
            # Compare as bytes: secrets.compare_digest rejects non-ASCII str.
            if not secrets.compare_digest(
                provided_token.encode("utf-8"), expected_token.encode("utf-8")
            ):
                QgsMessageLog.logMessage(
                    "Rejected command with missing/invalid token", self.LOG_TAG, MSG_WARNING
                )
                return {
                    "status": "error",
                    "message": AUTH_FAILED_MESSAGE,
                }

        # Authenticated from here on, so the announced version can be trusted
        # enough to show the user. Absent on MCP servers older than 0.10.
        self._record_client_version(
            command.get("client_version"),
            command.get("client_install"),
            command.get("client_root"),
        )
        return self._dispatch(command)

    def _signature(self, cmd_type, handler):
        """Cached (signature, accepted names) of a handler, for validation.

        ``accepted`` is the handler's named parameters, or None when it has no
        ``**kwargs`` - ``bind()`` already rejects unknown names in that case.

        Cached because `batch` can dispatch many commands in one message and
        inspect.signature() is not free.
        """
        entry = self._signatures.get(cmd_type)
        if entry is None:
            signature = inspect.signature(handler)
            kinds = [p.kind for p in signature.parameters.values()]
            accepted = None
            if inspect.Parameter.VAR_KEYWORD in kinds:
                accepted = frozenset(
                    name
                    for name, p in signature.parameters.items()
                    if p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)
                )
            entry = (signature, accepted)
            self._signatures[cmd_type] = entry
        return entry

    def _dispatch(self, command):
        """Dispatch an already-authenticated command to its handler."""
        try:
            cmd_type = command.get("type")
            params = command.get("params", {})

            # Untrusted socket input: `type` may be any JSON value, so check it
            # is a string before the set lookup (an unhashable one would raise)
            # and only then resolve it. Membership in COMMANDS is what keeps
            # getattr from reaching non-handler attributes like `stop`.
            if not isinstance(cmd_type, str) or cmd_type not in COMMANDS:
                QgsMessageLog.logMessage(f"Unknown command: {cmd_type}", self.LOG_TAG, MSG_WARNING)
                return {"status": "error", "message": f"Unknown command type: {cmd_type}"}
            if not isinstance(params, dict):
                return {"status": "error", "message": "Invalid params: expected an object"}
            handler = getattr(self, cmd_type)

            # Wrong parameters are the caller's mistake, not a plugin defect, so
            # check them against the signature before calling. Without this the
            # TypeError raised by the call itself lands in the generic handler
            # below and dumps a CRITICAL traceback in the user's QGIS log for
            # what is really just a missing argument.
            signature, accepted = self._signature(cmd_type, handler)
            try:
                signature.bind(**params)
            except TypeError as exc:
                message = f"{cmd_type}: {exc}"
                QgsMessageLog.logMessage(f"Bad parameters: {message}", self.LOG_TAG, MSG_WARNING)
                return {"status": "error", "message": message}

            # Almost every handler ends in **kwargs, so bind() accepts a
            # misspelled name and the command then runs with the default for
            # the parameter the caller meant - silently doing the wrong thing.
            # Name what was not recognised instead. A newer client sending a
            # parameter this plugin predates is refused here too, which is the
            # point: ignoring it would return a result that does not match
            # what was asked for.
            if accepted is not None:
                unknown = sorted(set(params) - accepted)
                if unknown:
                    message = (
                        f"{cmd_type}: unexpected parameter(s) {', '.join(unknown)}. "
                        f"Accepted: {', '.join(sorted(accepted)) or 'none'}"
                    )
                    QgsMessageLog.logMessage(
                        f"Bad parameters: {message}", self.LOG_TAG, MSG_WARNING
                    )
                    return {"status": "error", "message": message}

            # Copied before the call: handlers may rewrite what they are given
            # (runAndLoadResults turns output paths into layer definitions).
            record = cmd_type not in UNRECORDED_COMMANDS
            if record:
                recorded = copy.deepcopy(params)
                layers_before = set(QgsProject.instance().mapLayers())

            try:
                QgsMessageLog.logMessage(f"Executing: {cmd_type}", self.LOG_TAG, MSG_INFO)
                result = handler(**params)
                # execute_code reports a script that raised as a success.
                if record and not (isinstance(result, dict) and result.get("executed") is False):
                    self._record(cmd_type, recorded, self._layers_added_since(layers_before))
                return {"status": "success", "result": result}
            except CommandError as e:
                # Expected failure the caller can act on - message only, no
                # traceback: it would bury real defects in log noise.
                QgsMessageLog.logMessage(f"Error in {cmd_type}: {e!s}", self.LOG_TAG, MSG_WARNING)
                return {"status": "error", "message": str(e)}
            except Exception as e:
                # Anything else is a plugin defect (TypeError from a signature
                # mismatch, AttributeError from a renamed PyQGIS method, ...).
                # The message alone is useless for those, so log the traceback
                # and flag the response so a client can tell them apart.
                QgsMessageLog.logMessage(
                    f"Unhandled error in {cmd_type}: {e!r}\n{traceback.format_exc()}",
                    self.LOG_TAG,
                    MSG_CRITICAL,
                )
                return {"status": "error", "message": str(e), "internal": True}

        except Exception as e:
            QgsMessageLog.logMessage(
                f"Error executing command: {e!r}\n{traceback.format_exc()}",
                self.LOG_TAG,
                MSG_CRITICAL,
            )
            return {"status": "error", "message": str(e), "internal": True}

    def _record(self, cmd_type, params, creates=()):
        """Journal a command that succeeded, for export_session to replay.

        *creates* is the ``(layer id, name)`` of each layer it added: ids are
        generated anew on every run, so the replay maps them onto the ones it
        gets by name.
        """
        self._journal.append(
            {
                "seq": next(self._journal_seq),
                "command": cmd_type,
                "params": params,
                "creates": list(creates),
            }
        )

    @staticmethod
    def _layers_added_since(before):
        project = QgsProject.instance()
        return [
            (lid, layer.name()) for lid, layer in project.mapLayers().items() if lid not in before
        ]

    @command
    def batch(self, commands, **kwargs):
        """Execute multiple commands in sequence, return array of results.

        Destructive commands are refused here, not only in the MCP server: the
        socket is reachable by any local process, so a guard that lives solely
        on the client side is advisory. Rejecting one command does not abort the
        batch - the refusal is reported in that command's slot.
        """
        if not isinstance(commands, list):
            raise CommandError("commands must be a list")
        results = []
        for cmd in commands:
            if not isinstance(cmd, dict):
                results.append({"status": "error", "message": "Batch entry must be an object"})
                continue
            cmd_type = cmd.get("type")
            if cmd_type in BATCH_BLOCKED_COMMANDS:
                QgsMessageLog.logMessage(
                    f"Refused {cmd_type!r} inside batch", self.LOG_TAG, MSG_WARNING
                )
                results.append(
                    {
                        "status": "error",
                        "message": (
                            f"Command {cmd_type!r} is not allowed in batch - "
                            "call it individually so confirmation can be requested"
                        ),
                    }
                )
                continue
            results.append(self._dispatch({"type": cmd_type, "params": cmd.get("params", {})}))
        return results
