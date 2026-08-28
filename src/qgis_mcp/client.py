#!/usr/bin/env python3
"""
QGIS MCP Client - Simple client to connect to the QGIS MCP server.

Uses length-prefixed framing: each message is preceded by a 4-byte
big-endian unsigned int indicating the JSON payload size in bytes.
"""

import contextlib
import json
import logging
import socket

from qgis_mcp.protocol import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    HEADER_STRUCT,
    RECV_CHUNK_SIZE,
    TIMEOUT_DEFAULT,
    TIMEOUT_LONG,
    CommandTimeout,
    get_auth_token,
    get_client_version,
    get_install_info,
)

logger = logging.getLogger("QgisMCPClient")

# Cap the TCP handshake. Without it connect() inherits the OS default, so a host
# that routes but has nothing listening (easy to configure now that instances
# carry their own host) stalls ~21s per attempt - times the retry schedule, that
# is minutes for one tool call. A local refusal is immediate either way.
_CONNECT_TIMEOUT = 3.0


class QgisMCPClient:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self.host = host
        self.port = port
        self.socket = None
        self._current_timeout = None  # Track timeout to skip redundant syscalls
        # Why the last connect() failed. Callers need the distinction: a refusal
        # means nothing is listening and retrying cannot help, while a timeout
        # may just be a slow host.
        self.last_error = None

    def connect(self, timeout=_CONNECT_TIMEOUT):
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Disable Nagle's algorithm: send small packets immediately instead
            # of waiting up to 40ms to coalesce. Our request payloads are small
            # (typically <1KB) and we always want them sent without delay.
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.socket.settimeout(timeout)
            self.socket.connect((self.host, self.port))
            self.socket.settimeout(None)  # back to blocking; send_command sets its own
            self._current_timeout = None
            self.last_error = None
            return True
        except Exception as exc:
            # Not logger.exception: a probe of a closed instance is routine, and a
            # full traceback per retry per instance buries the actual failure.
            logger.warning("Could not connect to %s:%s (%s)", self.host, self.port, exc)
            self.last_error = exc
            if self.socket is not None:
                with contextlib.suppress(OSError):
                    self.socket.close()
                self.socket = None
            return False

    def disconnect(self):
        if self.socket:
            self.socket.close()
            self.socket = None
            self._current_timeout = None

    _MAX_RESPONSE_SIZE = 100 * 1024 * 1024  # 100 MB

    def _recv_exact(self, n):
        """Read exactly n bytes from the socket.

        Uses a pre-allocated buffer with recv_into() to avoid intermediate
        copies from bytearray.extend(). The benefit is reduced p99 latency
        variance for large payloads (5MB+), though median throughput is
        similar to the extend approach on loopback.
        """
        if n > self._MAX_RESPONSE_SIZE:
            raise ValueError(f"Response too large: {n} bytes (max {self._MAX_RESPONSE_SIZE})")
        buf = bytearray(n)
        view = memoryview(buf)
        pos = 0
        while pos < n:
            nbytes = self.socket.recv_into(view[pos:], min(n - pos, RECV_CHUNK_SIZE))
            if nbytes == 0:
                raise ConnectionError("Connection closed")
            pos += nbytes
        return bytes(buf)

    def _set_timeout(self, timeout):
        """Set socket timeout only when the value actually changes."""
        if self._current_timeout != timeout:
            self.socket.settimeout(timeout)
            self._current_timeout = timeout

    def send_command(self, command_type, params=None, timeout=TIMEOUT_DEFAULT):
        if not self.socket:
            raise ConnectionError("Not connected to server")

        # The plugin reads `client_version` to tell the user when the two halves
        # have drifted apart, a real risk since the uvx cache pins the server
        # while QGIS keeps upgrading the plugin. `client_install` tells it which
        # update command to name: only this side can tell a uvx launch from a
        # source checkout, so without it the plugin can report the drift but not
        # what to do about it. The command itself is never sent, only the kind
        # of install, because the plugin puts it in front of the user as
        # something to run. An older plugin ignores the extra keys, so this is
        # safe in both directions.
        install_kind, install_root = get_install_info()
        command = {
            "type": command_type,
            "params": params or {},
            "client_version": get_client_version(),
            "client_install": install_kind,
        }
        if install_root:
            command["client_root"] = install_root

        # Attach the shared-secret token when QGIS_MCP_TOKEN is configured. No-op
        # when auth is disabled, so existing setups are unaffected.
        token = get_auth_token()
        if token:
            command["token"] = token

        try:
            data = json.dumps(command).encode("utf-8")
            header = HEADER_STRUCT.pack(len(data))
            # Two separate sendall() calls avoid allocating a header+data copy.
            # Benchmarks show this is ~2x faster for 1MB payloads (159us vs 312us)
            # and slightly faster even for small payloads. TCP_NODELAY ensures the
            # header isn't delayed waiting for the data send.
            self.socket.sendall(header)
            self.socket.sendall(data)

            self._set_timeout(timeout)

            resp_header = self._recv_exact(4)
            resp_len = HEADER_STRUCT.unpack(resp_header)[0]
            resp_data = self._recv_exact(resp_len)

            self._set_timeout(None)
            return json.loads(resp_data)

        except TimeoutError as err:
            # Frame state is unrecoverable: a delayed response may still
            # arrive and pollute the recv buffer for the next call. Close
            # the socket so the connection cache reconnects on the next call.
            logger.warning("Socket operation timed out after %ds", timeout)
            self.disconnect()
            raise CommandTimeout(f"Socket operation timed out after {timeout}s") from err
        except ValueError as err:
            # Protocol framing error (e.g. "Response too large") - the socket
            # buffer is now out of sync, so close it and let callers reconnect.
            self.disconnect()
            raise ConnectionError("Protocol framing error, connection reset") from err
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ConnectionError):
            self.disconnect()
            raise  # Let callers handle reconnection
        except Exception as e:
            # Any other exception during framed I/O (e.g. _recv_exact's
            # "Response too large" guard, json errors, struct errors) leaves
            # the socket in an unknown state - close it so we don't reuse a
            # desynced stream on the next call.
            logger.exception("Error sending command")
            self.disconnect()
            raise ConnectionError(f"Send command failed: {e}") from e

    # --- Convenience methods (existing) ---

    def ping(self):
        return self.send_command("ping")

    def get_qgis_info(self):
        return self.send_command("get_qgis_info")

    def get_project_info(self):
        return self.send_command("get_project_info")

    def execute_code(self, code):
        return self.send_command("execute_code", {"code": code}, timeout=TIMEOUT_LONG)

    def add_vector_layer(self, path, name=None, provider="ogr"):
        params = {"path": path, "provider": provider}
        if name:
            params["name"] = name
        return self.send_command("add_vector_layer", params)

    def add_raster_layer(self, path, name=None, provider="gdal"):
        params = {"path": path, "provider": provider}
        if name:
            params["name"] = name
        return self.send_command("add_raster_layer", params)

    def get_layers(self, limit=50, offset=0):
        return self.send_command("get_layers", {"limit": limit, "offset": offset})

    def remove_layer(self, layer_id):
        return self.send_command("remove_layer", {"layer_id": layer_id})

    def zoom_to_layer(self, layer_id):
        return self.send_command("zoom_to_layer", {"layer_id": layer_id})

    def get_layer_features(
        self, layer_id, limit=10, offset=0, expression=None, include_geometry=False
    ):
        params = {
            "layer_id": layer_id,
            "limit": limit,
            "offset": offset,
            "include_geometry": include_geometry,
        }
        if expression:
            params["expression"] = expression
        return self.send_command("get_layer_features", params)

    def get_field_statistics(self, layer_id, field_name):
        return self.send_command(
            "get_field_statistics", {"layer_id": layer_id, "field_name": field_name}
        )

    def set_layer_visibility(self, layer_id, visible):
        return self.send_command("set_layer_visibility", {"layer_id": layer_id, "visible": visible})

    def get_canvas_extent(self):
        return self.send_command("get_canvas_extent")

    def set_canvas_extent(self, xmin, ymin, xmax, ymax, crs=None):
        params = {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}
        if crs:
            params["crs"] = crs
        return self.send_command("set_canvas_extent", params)

    def get_raster_info(self, layer_id):
        return self.send_command("get_raster_info", {"layer_id": layer_id})

    def get_layer_info(self, layer_id):
        return self.send_command("get_layer_info", {"layer_id": layer_id})

    def get_layer_schema(self, layer_id):
        return self.send_command("get_layer_schema", {"layer_id": layer_id})

    def execute_processing(self, algorithm, parameters):
        return self.send_command(
            "execute_processing",
            {"algorithm": algorithm, "parameters": parameters},
            timeout=TIMEOUT_LONG,
        )

    def save_project(self, path=None):
        params = {}
        if path:
            params["path"] = path
        return self.send_command("save_project", params)

    def load_project(self, path):
        return self.send_command("load_project", {"path": path})

    def render_map(self, path=None, width=800, height=600):
        params = {"width": width, "height": height}
        if path:
            params["path"] = path
        return self.send_command("render_map_base64", params, timeout=TIMEOUT_LONG)

    def batch(self, commands):
        return self.send_command("batch", {"commands": commands}, timeout=TIMEOUT_LONG)

    # --- Phase 2 new convenience methods ---

    def add_features(self, layer_id, features):
        return self.send_command("add_features", {"layer_id": layer_id, "features": features})

    def update_features(self, layer_id, updates):
        return self.send_command("update_features", {"layer_id": layer_id, "updates": updates})

    def delete_features(self, layer_id, fids=None, expression=None):
        params = {"layer_id": layer_id}
        if fids is not None:
            params["fids"] = fids
        if expression:
            params["expression"] = expression
        return self.send_command("delete_features", params)

    def set_layer_style(self, layer_id, style_type, field=None, classes=5, color_ramp="Spectral"):
        params = {
            "layer_id": layer_id,
            "style_type": style_type,
            "classes": classes,
            "color_ramp": color_ramp,
        }
        if field:
            params["field"] = field
        return self.send_command("set_layer_style", params)

    def select_features(self, layer_id, expression=None, fids=None):
        params = {"layer_id": layer_id}
        if expression:
            params["expression"] = expression
        if fids is not None:
            params["fids"] = fids
        return self.send_command("select_features", params)

    def get_selection(self, layer_id):
        return self.send_command("get_selection", {"layer_id": layer_id})

    def clear_selection(self, layer_id):
        return self.send_command("clear_selection", {"layer_id": layer_id})

    def create_memory_layer(self, name, geometry_type, crs="EPSG:4326", fields=None):
        params = {"name": name, "geometry_type": geometry_type, "crs": crs}
        if fields:
            params["fields"] = fields
        return self.send_command("create_memory_layer", params)

    def list_processing_algorithms(self, search=None, provider=None):
        params = {}
        if search:
            params["search"] = search
        if provider:
            params["provider"] = provider
        return self.send_command("list_processing_algorithms", params)

    def get_algorithm_help(self, algorithm_id):
        return self.send_command("get_algorithm_help", {"algorithm_id": algorithm_id})

    def find_layer(self, name_pattern):
        return self.send_command("find_layer", {"name_pattern": name_pattern})

    def list_layouts(self):
        return self.send_command("list_layouts")

    def export_layout(self, layout_name, path, format="pdf", dpi=300):
        return self.send_command(
            "export_layout",
            {
                "layout_name": layout_name,
                "path": path,
                "format": format,
                "dpi": dpi,
            },
        )

    # --- Phase 3 new convenience methods ---

    def get_message_log(self, level=None, tag=None, limit=100):
        params = {"limit": limit}
        if level:
            params["level"] = level
        if tag:
            params["tag"] = tag
        return self.send_command("get_message_log", params)

    def list_plugins(self, enabled_only=False):
        return self.send_command("list_plugins", {"enabled_only": enabled_only})

    def get_plugin_info(self, plugin_name):
        return self.send_command("get_plugin_info", {"plugin_name": plugin_name})

    def reload_plugin(self, plugin_name):
        return self.send_command("reload_plugin", {"plugin_name": plugin_name})

    def get_layer_tree(self):
        return self.send_command("get_layer_tree")

    def create_layer_group(self, name, parent=None):
        params = {"name": name}
        if parent:
            params["parent"] = parent
        return self.send_command("create_layer_group", params)

    def move_layer_to_group(self, layer_id, group_name):
        return self.send_command(
            "move_layer_to_group", {"layer_id": layer_id, "group_name": group_name}
        )

    def set_layer_property(self, layer_id, property, value):
        return self.send_command(
            "set_layer_property", {"layer_id": layer_id, "property": property, "value": value}
        )

    def get_layer_extent(self, layer_id):
        return self.send_command("get_layer_extent", {"layer_id": layer_id})

    def get_project_variables(self):
        return self.send_command("get_project_variables")

    def set_project_variable(self, key, value):
        return self.send_command("set_project_variable", {"key": key, "value": value})

    def validate_expression(self, expression, layer_id=None):
        params = {"expression": expression}
        if layer_id:
            params["layer_id"] = layer_id
        return self.send_command("validate_expression", params)

    def get_setting(self, key):
        return self.send_command("get_setting", {"key": key})

    def set_setting(self, key, value):
        return self.send_command("set_setting", {"key": key, "value": value})

    # --- Phase 4 new convenience methods ---

    def get_canvas_screenshot(self):
        return self.send_command("get_canvas_screenshot")

    def transform_coordinates(self, source_crs, target_crs, point=None, points=None, bbox=None):
        params = {"source_crs": source_crs, "target_crs": target_crs}
        if point:
            params["point"] = point
        if points:
            params["points"] = points
        if bbox:
            params["bbox"] = bbox
        return self.send_command("transform_coordinates", params)


def print_json(data):
    print(json.dumps(data, indent=2))


def main():
    client = QgisMCPClient()
    if not client.connect():
        logger.error("Could not connect to QGIS MCP server")
        return

    try:
        logger.info("Checking connection...")
        response = client.ping()
        if response and response.get("status") == "success":
            logger.info("Connected successfully")
        else:
            logger.error("Connection error")
            return

        print("\nQGIS Info:")
        print_json(client.get_qgis_info())

        print("\nProject Info:")
        print_json(client.get_project_info())

    except Exception:
        logger.exception("Error running commands")


if __name__ == "__main__":
    main()
