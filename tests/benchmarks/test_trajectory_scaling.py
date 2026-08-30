"""Trajectory render scaling: 1k → 10k → 100k → 500k rows.

Verifies the big-data claim from DESIGN.md §4 (max_points=500_000 hard ceiling).
Per-stage timings let us spot non-linear regressions in CSV parse or sampling.
"""

from __future__ import annotations

import tracemalloc
from pathlib import Path

import pytest

from tests.benchmarks.conftest import requires_headless


def _render_trajectory(path: Path, output: Path) -> dict:
    """Helper: call qgis_render_trajectory in lines mode with default sampling."""
    from qgis_mcp_workflows.server import qgis_render_trajectory

    return qgis_render_trajectory(
        input_path=str(path), output_png=str(output), render_mode="lines"
    )


@pytest.mark.bench
@requires_headless
def test_trajectory_1k(benchmark, trajectory_1k, tmp_path):
    benchmark(_render_trajectory, trajectory_1k, tmp_path / "1k.png")


@pytest.mark.bench
@requires_headless
def test_trajectory_10k(benchmark, trajectory_10k, tmp_path):
    benchmark(_render_trajectory, trajectory_10k, tmp_path / "10k.png")


@pytest.mark.bench
@requires_headless
def test_trajectory_100k(benchmark, trajectory_100k, tmp_path):
    benchmark(_render_trajectory, trajectory_100k, tmp_path / "100k.png")


@pytest.mark.bench
@requires_headless
def test_trajectory_500k(benchmark, trajectory_500k, tmp_path):
    benchmark(_render_trajectory, trajectory_500k, tmp_path / "500k.png")


@pytest.mark.bench
@requires_headless
def test_trajectory_100k_memory(trajectory_100k, tmp_path):
    """Peak RAM under tracemalloc for 100k-row render (1-shot, not a benchmark.timeit)."""
    tracemalloc.start()
    _render_trajectory(trajectory_100k, tmp_path / "100k_mem.png")
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # 100k features * ~150 bytes/feat ≈ 15 MB upper bound; allow 2x for overhead.
    assert peak < 50 * 1024 * 1024, f"100k trajectory peaked at {peak / 1024 / 1024:.1f} MB"
