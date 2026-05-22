"""Plugin transport executor — TCP socket to the QGIS plugin on port 9877.

Wraps ``QgisMCPClient`` with:
- env-var driven host/port (``QGIS_MCP_WORKFLOWS_HOST``, ``QGIS_MCP_WORKFLOWS_PORT``)
- per-call connect/disconnect (simple, fits MCP's per-tool-call lifecycle)
- response-envelope unwrapping (return ``result`` on success, raise on error)
- error-message → typed-exception mapping
"""

from __future__ import annotations

import os

from qgis_mcp_workflows.client import QgisMCPClient
from qgis_mcp_workflows.errors import (
    ExecutorError,
    LayerNotFoundError,
    PluginUnavailableError,
)
from qgis_mcp_workflows.helpers import DEFAULT_HOST, DEFAULT_PORT, TIMEOUT_DEFAULT


class PluginExecutor:
    """Dispatch commands to the QGIS plugin over TCP."""

    def __init__(self, host: str | None = None, port: int | None = None) -> None:
        self.host = host or os.environ.get("QGIS_MCP_WORKFLOWS_HOST", DEFAULT_HOST)
        self.port = port or int(os.environ.get("QGIS_MCP_WORKFLOWS_PORT", str(DEFAULT_PORT)))

    def dispatch(self, command: str, params: dict | None = None, timeout: int | None = None) -> dict:
        client = QgisMCPClient(host=self.host, port=self.port)
        if not client.connect():
            raise PluginUnavailableError(self.host, self.port)
        try:
            response = client.send_command(command, params or {}, timeout=timeout or TIMEOUT_DEFAULT)
        finally:
            client.disconnect()

        status = response.get("status")
        if status == "success":
            return response.get("result", {})

        message = response.get("message", "(no message)")
        raise _map_error(command, message)


def _map_error(command: str, message: str) -> Exception:
    """Heuristic: plugin error strings → typed exceptions."""
    lowered = message.lower()
    if "not found" in lowered and ("layer" in lowered or "path" in lowered or "file" in lowered):
        return LayerNotFoundError(message)
    return ExecutorError(command, message)


_default: PluginExecutor | None = None


def get_default_executor() -> PluginExecutor:
    """Module-level cached executor — avoids re-reading env on every tool call."""
    global _default
    if _default is None:
        _default = PluginExecutor()
    return _default
