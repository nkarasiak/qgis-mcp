"""Live integration tests for HeadlessExecutor.

Skipped unless a working QGIS Python launcher is reachable. Set
``QGIS_MCP_NORTH_QGIS_LAUNCHER=...`` to override the auto-detection (default
on Windows is ``M:\\QGIS LTR\\bin\\python-qgis-ltr.bat``).

These tests spawn a real PyQGIS subprocess each, so they're slow (~5-10s of
``initQgis`` per test instance). Use ``-k headless`` to run only this file
during quick iteration.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from qgis_mcp_north.errors import HeadlessUnavailableError
from qgis_mcp_north.executors.headless import HeadlessExecutor


def _launcher_resolvable() -> bool:
    try:
        HeadlessExecutor._resolve_launcher()
    except HeadlessUnavailableError:
        return False
    return True


requires_qgis_python = pytest.mark.skipif(
    not _launcher_resolvable(),
    reason=(
        "QGIS Python launcher not found — set QGIS_MCP_NORTH_QGIS_LAUNCHER, install OSGeo4W, "
        "or skip these tests."
    ),
)


@pytest.fixture
def headless_executor():
    """A live HeadlessExecutor; tears down the subprocess on teardown."""
    ex = HeadlessExecutor()
    try:
        yield ex
    finally:
        ex.shutdown()


@requires_qgis_python
def test_ping_round_trips(headless_executor):
    """Smoke test: subprocess starts, dispatches ping, returns the right shape."""
    result = headless_executor.dispatch("ping")
    assert result == {"pong": True}


@requires_qgis_python
def test_subprocess_stays_alive_across_dispatches(headless_executor):
    """``initQgis`` is expensive; we hold the subprocess open. Verify two pings hit one process."""
    headless_executor.dispatch("ping")
    pid_before = headless_executor._proc.pid
    headless_executor.dispatch("ping")
    pid_after = headless_executor._proc.pid
    assert pid_before == pid_after, "subprocess respawned mid-session — the long-lived contract is broken"


@requires_qgis_python
def test_add_vector_layer_returns_layer_id(headless_executor):
    """End-to-end: write a GeoJSON, load it, get its layer_id, remove it."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "tiny.geojson")
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                '{"type":"FeatureCollection","crs":{"type":"name",'
                '"properties":{"name":"urn:ogc:def:crs:EPSG::4326"}},'
                '"features":[{"type":"Feature","properties":{"name":"a"},'
                '"geometry":{"type":"Point","coordinates":[139.0,35.0]}}]}'
            )
        result = headless_executor.dispatch("add_vector_layer", {"path": path})
        layer_id = result["id"]
        assert layer_id  # non-empty
        info = headless_executor.dispatch("get_layer_info", {"layer_id": layer_id})
        assert info["crs"] == "EPSG:4326"
        # cleanup so a subsequent test doesn't see this layer
        headless_executor.dispatch("remove_layer", {"layer_id": layer_id})
