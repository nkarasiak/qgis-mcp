"""Transport parity: plugin vs headless for the same operation.

If headless is more than 3x slower than plugin for the same render, that's a flag.
Both modes use the SAME plugin handler (per v0.4 architecture); difference is
purely socket-vs-stdin and subprocess vs in-process.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.benchmarks.conftest import requires_headless
from tests.conftest import requires_plugin

TINY_TRAJ = Path(__file__).resolve().parents[1] / "fixtures" / "tiny_trajectory.csv"


def _render_via_executor(executor, out_path: Path) -> None:
    from qgis_mcp_workflows import executors as _ex

    _ex.set_executor(executor)
    try:
        from qgis_mcp_workflows.server import qgis_render_trajectory

        qgis_render_trajectory(
            input_path=str(TINY_TRAJ), output_png=str(out_path), render_mode="lines"
        )
    finally:
        _ex.set_executor(None)


@pytest.mark.bench
@requires_plugin
def test_plugin_render_trajectory(benchmark, tmp_path):
    from qgis_mcp_workflows.executors.plugin import PluginExecutor
    from qgis_mcp_workflows.helpers import DEFAULT_HOST, DEFAULT_PORT

    executor = PluginExecutor(host=DEFAULT_HOST, port=DEFAULT_PORT)
    benchmark(_render_via_executor, executor, tmp_path / "plugin.png")


@pytest.mark.bench
@requires_headless
def test_headless_render_trajectory(benchmark, tmp_path):
    from qgis_mcp_workflows.executors.headless import HeadlessExecutor

    executor = HeadlessExecutor()
    benchmark(_render_via_executor, executor, tmp_path / "headless.png")
