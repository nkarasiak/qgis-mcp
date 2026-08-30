"""Executor abstraction — hides the transport behind a single dispatch method.

v0.3 ships only ``PluginExecutor`` (TCP socket → QGIS plugin). v0.4 adds
``HeadlessExecutor`` (PyQGIS subprocess). Tools call ``get_executor().dispatch()``
and never speak transport directly; switching transports is config, not code.

Tests inject a fake via ``set_executor()``.
"""

from __future__ import annotations

from typing import Protocol


class Executor(Protocol):
    """Single seam between tools and transport.

    Implementations return the plugin's ``result`` payload on success and
    raise a ``QgisMcpWorkflowsError`` subclass on failure. The ``{status, result}``
    envelope never leaks past this boundary.
    """

    def dispatch(self, command: str, params: dict | None = None, timeout: int | None = None) -> dict:
        ...


_current: Executor | None = None


def get_executor() -> Executor:
    """Return the active executor; lazily creates a ``PluginExecutor`` default."""
    global _current
    if _current is None:
        from qgis_mcp_workflows.executors.plugin import PluginExecutor

        _current = PluginExecutor()
    return _current


def set_executor(executor: Executor | None) -> None:
    """Override the active executor (test hook). Pass None to reset."""
    global _current
    _current = executor


__all__ = ["Executor", "get_executor", "set_executor"]
