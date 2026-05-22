"""Cold-start benchmarks for plugin + headless transports.

The headless cold-start is the key claim ("production-grade for cron"). The plugin
cold-start measures the socket-connect + ping cost when QGIS Desktop is already running.

Run:
    uv run --no-sync --extra bench pytest tests/benchmarks/test_cold_start.py -m bench --benchmark-only
"""

from __future__ import annotations

import pytest

from tests.benchmarks.conftest import requires_headless
from tests.conftest import requires_plugin


@pytest.mark.bench
@requires_headless
def test_headless_cold_start(benchmark):
    """Spawn HeadlessExecutor, dispatch ping, shutdown — wall time of one full cycle."""
    from qgis_mcp_workflows.executors.headless import HeadlessExecutor

    def cycle():
        ex = HeadlessExecutor()
        ex.dispatch("ping", {})
        del ex

    benchmark(cycle)


@pytest.mark.bench
@requires_plugin
def test_plugin_ping(benchmark):
    """Plugin transport: socket connect + ping. No spawn cost (QGIS already running)."""
    from qgis_mcp_workflows.executors.plugin import PluginExecutor
    from qgis_mcp_workflows.helpers import DEFAULT_HOST, DEFAULT_PORT

    def cycle():
        ex = PluginExecutor(host=DEFAULT_HOST, port=DEFAULT_PORT)
        ex.dispatch("ping", {})

    benchmark(cycle)
