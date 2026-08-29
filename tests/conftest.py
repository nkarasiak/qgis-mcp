"""Shared fixtures: integration tests (running QGIS plugin) and the stubbed-qgis handler tests."""

import os
import sys
import types
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qgis_mcp.client import QgisMCPClient

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "qgis_mcp_plugin"
# Every module the handler package imports at module level from outside the plugin.
QGIS_MODULES = (
    "processing",
    "qgis",
    "qgis._3d",
    "qgis.analysis",
    "qgis.core",
    "qgis.utils",
    "qgis.PyQt",
    "qgis.PyQt.QtCore",
    "qgis.PyQt.QtGui",
    "qgis.PyQt.QtWidgets",
)


class FakeQObject:
    """Subclassable stand-in for a qgis base class: every method is a no-op.

    Subclassing a MagicMock *instance* turns the subclass into a mock whose side_effect is
    the bases tuple, so the second instantiation raises StopIteration.
    """

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FakeCredentials(FakeQObject):
    current = "gui-dialog"

    @staticmethod
    def instance():
        return FakeCredentials.current

    def setInstance(self, instance):
        FakeCredentials.current = instance


@pytest.fixture(scope="session")
def plugin_handlers():
    """The plugin's handler package imported against a stubbed qgis (no QGIS needed)."""
    saved = {name: sys.modules.get(name) for name in QGIS_MODULES}
    for name in QGIS_MODULES:
        sys.modules[name] = MagicMock()
    sys.modules["qgis.core"].QgsCredentials = FakeCredentials
    sys.modules["qgis.core"].QgsProcessingFeedback = FakeQObject
    # A bare package: the real __init__ imports plugin.py, which needs a live QGIS.
    package = types.ModuleType("qgis_mcp_plugin")
    package.__path__ = [str(PLUGIN_DIR)]
    sys.modules["qgis_mcp_plugin"] = package
    import qgis_mcp_plugin.handlers as handlers

    yield handlers
    for name in [
        m for m in sys.modules if m == "qgis_mcp_plugin" or m.startswith("qgis_mcp_plugin.")
    ]:
        del sys.modules[name]
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


@pytest.fixture(autouse=True)
def elicit_confirmations(monkeypatch):
    """Exercise the elicitation path, which is off by default in production.

    `_confirm_destructive` only elicits when QGIS_MCP_AUTO_CONFIRM is falsy, so
    without this every confirmation test would assert against a no-op. Tests for
    the default (skip) delete the var themselves.
    """
    monkeypatch.setenv("QGIS_MCP_AUTO_CONFIRM", "0")


# ---------------------------------------------------------------------------
# City fixtures - reusable across test modules
# ---------------------------------------------------------------------------

CITIES = [
    {
        "attributes": {"name": "Paris", "population": 2161000, "country": "France"},
        "geometry_wkt": "POINT(2.35 48.86)",
    },
    {
        "attributes": {"name": "Berlin", "population": 3645000, "country": "Germany"},
        "geometry_wkt": "POINT(13.40 52.52)",
    },
    {
        "attributes": {"name": "London", "population": 8982000, "country": "UK"},
        "geometry_wkt": "POINT(-0.12 51.51)",
    },
    {
        "attributes": {"name": "Madrid", "population": 3223000, "country": "Spain"},
        "geometry_wkt": "POINT(-3.70 40.42)",
    },
    {
        "attributes": {"name": "Rome", "population": 2873000, "country": "Italy"},
        "geometry_wkt": "POINT(12.50 41.90)",
    },
    {
        "attributes": {"name": "Tokyo", "population": 13960000, "country": "Japan"},
        "geometry_wkt": "POINT(139.69 35.69)",
    },
    {
        "attributes": {"name": "New York", "population": 8336000, "country": "USA"},
        "geometry_wkt": "POINT(-74.01 40.71)",
    },
    {
        "attributes": {"name": "São Paulo", "population": 12330000, "country": "Brazil"},
        "geometry_wkt": "POINT(-46.63 -23.55)",
    },
    {
        "attributes": {"name": "Mumbai", "population": 12440000, "country": "India"},
        "geometry_wkt": "POINT(72.88 19.08)",
    },
    {
        "attributes": {"name": "Cairo", "population": 9540000, "country": "Egypt"},
        "geometry_wkt": "POINT(31.24 30.04)",
    },
    {
        "attributes": {"name": "Sydney", "population": 5312000, "country": "Australia"},
        "geometry_wkt": "POINT(151.21 -33.87)",
    },
    {
        "attributes": {"name": "Lagos", "population": 15400000, "country": "Nigeria"},
        "geometry_wkt": "POINT(3.39 6.52)",
    },
    {
        "attributes": {"name": "Moscow", "population": 12500000, "country": "Russia"},
        "geometry_wkt": "POINT(37.62 55.76)",
    },
    {
        "attributes": {"name": "Beijing", "population": 21540000, "country": "China"},
        "geometry_wkt": "POINT(116.40 39.90)",
    },
    {
        "attributes": {"name": "Mexico City", "population": 9210000, "country": "Mexico"},
        "geometry_wkt": "POINT(-99.13 19.43)",
    },
    {
        "attributes": {"name": "Toronto", "population": 2930000, "country": "Canada"},
        "geometry_wkt": "POINT(-79.38 43.65)",
    },
    {
        "attributes": {"name": "Nairobi", "population": 4397000, "country": "Kenya"},
        "geometry_wkt": "POINT(36.82 -1.29)",
    },
    {
        "attributes": {"name": "Buenos Aires", "population": 3076000, "country": "Argentina"},
        "geometry_wkt": "POINT(-58.38 -34.60)",
    },
    {
        "attributes": {"name": "Bangkok", "population": 10540000, "country": "Thailand"},
        "geometry_wkt": "POINT(100.50 13.76)",
    },
    {
        "attributes": {"name": "Istanbul", "population": 15460000, "country": "Turkey"},
        "geometry_wkt": "POINT(28.98 41.01)",
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_client():
    """Create and connect a fresh QgisMCPClient."""
    c = QgisMCPClient()
    assert c.connect(), "Failed to connect to QGIS MCP plugin"
    return c


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def client():
    """Single client connection shared across all test modules."""
    c = QgisMCPClient()
    if not c.connect():
        pytest.skip("QGIS MCP Server is not running on localhost:9876")
    yield c
    c.disconnect()


@pytest.fixture(scope="session")
def test_project(client):
    """Create a fresh project for the entire test session."""
    path = f"/tmp/mcp_test_{uuid.uuid4().hex[:8]}.qgz"
    resp = client.send_command("create_new_project", {"path": path})
    assert resp["status"] == "success"
    yield path


@pytest.fixture(scope="session")
def cities_layer(client, test_project):
    """Create a memory layer with 20 world cities, shared across modules."""
    resp = client.send_command(
        "create_memory_layer",
        {
            "name": f"test_cities_{uuid.uuid4().hex[:6]}",
            "geometry_type": "Point",
            "crs": "EPSG:4326",
            "fields": [
                {"name": "name", "type": "string"},
                {"name": "population", "type": "integer"},
                {"name": "country", "type": "string"},
            ],
        },
    )
    assert resp["status"] == "success"
    layer_id = resp["result"]["id"]

    resp = client.send_command("add_features", {"layer_id": layer_id, "features": CITIES})
    assert resp["status"] == "success"
    assert resp["result"]["added"] == 20

    yield layer_id

    client.send_command("remove_layer", {"layer_id": layer_id})
