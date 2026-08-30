"""Shared fixtures for benchmark suite.

Benchmarks are excluded from the default pytest run via `addopts = -m 'not bench'`
in pyproject.toml. Run explicitly:

    uv run --no-sync --extra bench pytest tests/benchmarks/ -m bench --benchmark-only

Each test should be decorated with @pytest.mark.bench and one of
@requires_plugin / @requires_headless / @requires_local_qgis as appropriate.
"""

from __future__ import annotations

import csv
import os
import random
import shutil
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SCALED_ZONES_134 = FIXTURE_DIR / "scaled_zones_134.geojson"


requires_headless = pytest.mark.skipif(
    not os.environ.get("QGIS_MCP_WORKFLOWS_QGIS_LAUNCHER"),
    reason="Set QGIS_MCP_WORKFLOWS_QGIS_LAUNCHER to a python-qgis(-ltr).bat to run headless benchmarks.",
)


def _generate_synthetic_trajectory(n_rows: int, out: Path) -> None:
    """Deterministic synthetic trajectory CSV with n_rows points across 10 trip_ids."""
    rng = random.Random(42)
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["trip_id", "datetime", "lon", "lat", "transport_mode"])
        for i in range(n_rows):
            trip = f"T{i % 10:02d}"
            lon = 139.5 + rng.random() * 0.3
            lat = 35.6 + rng.random() * 0.25
            ts = f"2026-01-01 {(i // 60) % 24:02d}:{i % 60:02d}:00"
            writer.writerow([trip, ts, f"{lon:.6f}", f"{lat:.6f}", "taxi"])


@pytest.fixture(scope="session")
def trajectory_1k(tmp_path_factory) -> Path:
    """1k-row synthetic trajectory CSV — fits in <30 ms render budget."""
    p = tmp_path_factory.mktemp("bench") / "trajectory_1k.csv"
    _generate_synthetic_trajectory(1_000, p)
    return p


@pytest.fixture(scope="session")
def trajectory_10k(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("bench") / "trajectory_10k.csv"
    _generate_synthetic_trajectory(10_000, p)
    return p


@pytest.fixture(scope="session")
def trajectory_100k(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("bench") / "trajectory_100k.csv"
    _generate_synthetic_trajectory(100_000, p)
    return p


@pytest.fixture(scope="session")
def trajectory_500k(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("bench") / "trajectory_500k.csv"
    _generate_synthetic_trajectory(500_000, p)
    return p


@pytest.fixture(scope="session")
def scaled_zones() -> Path:
    """134-polygon synthetic grid. Built once via scripts/build_scaled_zones.py."""
    if not SCALED_ZONES_134.exists():
        pytest.skip(
            f"{SCALED_ZONES_134} missing; run scripts/build_scaled_zones.py first."
        )
    return SCALED_ZONES_134


@pytest.fixture(scope="session")
def scaled_choropleth_csv(tmp_path_factory) -> Path:
    """CSV with total_trips values matching the 134 zone IDs in scaled_zones_134."""
    p = tmp_path_factory.mktemp("bench") / "scaled_values.csv"
    with p.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["zone_id", "total_trips"])
        for i in range(134):
            writer.writerow([f"S{i:03d}", (i * 37) % 10_000])
    return p


@pytest.fixture(autouse=True, scope="function")
def _bench_only(request):
    """Bench tests get @pytest.mark.bench; the addopts excludes them by default."""
    if "bench" not in request.keywords:
        pytest.skip("not a benchmark test (use pytest -m bench to opt-in)")


# Reuse the fake_executor + requires_plugin fixtures from the parent conftest.
@pytest.fixture(autouse=False)
def _bench_workdir(tmp_path):
    """A throwaway directory for output PNGs; cleaned per-test."""
    yield tmp_path
    shutil.rmtree(tmp_path, ignore_errors=True)
