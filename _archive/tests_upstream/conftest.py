"""Shared fixtures for all integration tests (require running QGIS plugin).

Unit tests (test_mcp_tools.py) use mocked sockets and don't need these.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qgis_mcp.client import QgisMCPClient  # noqa: E402

# ---------------------------------------------------------------------------
# City fixtures — reusable across test modules
# ---------------------------------------------------------------------------

CITIES = [
    {"attributes": {"name": "Paris", "population": 2161000, "country": "France"}, "geometry_wkt": "POINT(2.35 48.86)"},
    {"attributes": {"name": "Berlin", "population": 3645000, "country": "Germany"}, "geometry_wkt": "POINT(13.40 52.52)"},
    {"attributes": {"name": "London", "population": 8982000, "country": "UK"}, "geometry_wkt": "POINT(-0.12 51.51)"},
    {"attributes": {"name": "Madrid", "population": 3223000, "country": "Spain"}, "geometry_wkt": "POINT(-3.70 40.42)"},
    {"attributes": {"name": "Rome", "population": 2873000, "country": "Italy"}, "geometry_wkt": "POINT(12.50 41.90)"},
    {"attributes": {"name": "Tokyo", "population": 13960000, "country": "Japan"}, "geometry_wkt": "POINT(139.69 35.69)"},
    {"attributes": {"name": "New York", "population": 8336000, "country": "USA"}, "geometry_wkt": "POINT(-74.01 40.71)"},
    {"attributes": {"name": "São Paulo", "population": 12330000, "country": "Brazil"}, "geometry_wkt": "POINT(-46.63 -23.55)"},
    {"attributes": {"name": "Mumbai", "population": 12440000, "country": "India"}, "geometry_wkt": "POINT(72.88 19.08)"},
    {"attributes": {"name": "Cairo", "population": 9540000, "country": "Egypt"}, "geometry_wkt": "POINT(31.24 30.04)"},
    {"attributes": {"name": "Sydney", "population": 5312000, "country": "Australia"}, "geometry_wkt": "POINT(151.21 -33.87)"},
    {"attributes": {"name": "Lagos", "population": 15400000, "country": "Nigeria"}, "geometry_wkt": "POINT(3.39 6.52)"},
    {"attributes": {"name": "Moscow", "population": 12500000, "country": "Russia"}, "geometry_wkt": "POINT(37.62 55.76)"},
    {"attributes": {"name": "Beijing", "population": 21540000, "country": "China"}, "geometry_wkt": "POINT(116.40 39.90)"},
    {"attributes": {"name": "Mexico City", "population": 9210000, "country": "Mexico"}, "geometry_wkt": "POINT(-99.13 19.43)"},
    {"attributes": {"name": "Toronto", "population": 2930000, "country": "Canada"}, "geometry_wkt": "POINT(-79.38 43.65)"},
    {"attributes": {"name": "Nairobi", "population": 4397000, "country": "Kenya"}, "geometry_wkt": "POINT(36.82 -1.29)"},
    {"attributes": {"name": "Buenos Aires", "population": 3076000, "country": "Argentina"}, "geometry_wkt": "POINT(-58.38 -34.60)"},
    {"attributes": {"name": "Bangkok", "population": 10540000, "country": "Thailand"}, "geometry_wkt": "POINT(100.50 13.76)"},
    {"attributes": {"name": "Istanbul", "population": 15460000, "country": "Turkey"}, "geometry_wkt": "POINT(28.98 41.01)"},
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
