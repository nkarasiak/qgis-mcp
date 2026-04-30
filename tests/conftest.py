"""Shared pytest fixtures."""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Any

import pytest

from qgis_mcp_north.helpers import DEFAULT_HOST, DEFAULT_PORT


def _plugin_reachable(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


PLUGIN_AVAILABLE = _plugin_reachable()
requires_plugin = pytest.mark.skipif(
    not PLUGIN_AVAILABLE,
    reason=f"QGIS plugin not reachable at {DEFAULT_HOST}:{DEFAULT_PORT} — start QGIS with the plugin enabled",
)


class FakeExecutor:
    """Records dispatch calls and returns scripted responses.

    Usage:
        fake = FakeExecutor()
        fake.responses["add_vector_layer"] = {"layer_id": "L1", ...}
        # ...run tool that calls dispatch...
        assert fake.calls[0] == ("add_vector_layer", {"path": "..."})
    """

    def __init__(self) -> None:
        self.responses: dict[str, Any | Callable[[dict | None], Any]] = {}
        self.calls: list[tuple[str, dict | None]] = []

    def dispatch(self, command: str, params: dict | None = None, timeout: int | None = None) -> Any:
        self.calls.append((command, params))
        if command not in self.responses:
            raise AssertionError(
                f"FakeExecutor: unexpected command {command!r}. "
                f"Set fake.responses[{command!r}] = ... in your test."
            )
        resp = self.responses[command]
        if callable(resp):
            return resp(params)
        return resp


@pytest.fixture
def fake_executor() -> FakeExecutor:
    """Install a FakeExecutor as the active executor; reset on teardown."""
    from qgis_mcp_north import executors

    fake = FakeExecutor()
    executors.set_executor(fake)
    try:
        yield fake
    finally:
        executors.set_executor(None)
