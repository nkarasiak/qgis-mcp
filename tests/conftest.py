"""Shared fixtures: integration tests (running QGIS plugin) and the stubbed-qgis handler tests."""

import sys
import types
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_compat import make_mcp_error

import qgis_mcp.server as srv
from qgis_mcp.client import QgisMCPClient
from qgis_mcp.server import _ConfirmSchema

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
    "qgis.PyQt.QtXml",
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


class _AnyClassAttr(type):
    """Metaclass: unknown class attributes resolve to mocks, so compat._enum finds its members."""

    def __getattr__(cls, name):
        return MagicMock()


class FakeQVariant(FakeQObject, metaclass=_AnyClassAttr):
    """A real class so isinstance(value, QVariant) works in handler converters."""


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
    sys.modules["qgis.PyQt.QtCore"].QObject = FakeQObject  # QgisMCPServer subclasses it
    sys.modules["qgis.PyQt.QtCore"].QVariant = FakeQVariant
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
    if not c.connect():
        pytest.skip("QGIS MCP Server is not running on localhost:9876")
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


# ---------------------------------------------------------------------------
# MCP server fixtures (mocked socket, no QGIS)
# ---------------------------------------------------------------------------

# The registry is the source of truth; test_plugin_structure pins the plugin
# side of the parity, so these two numbers only move on a deliberate change.
TOOL_COUNT = 125
COMPOUND_TOOL_COUNT = 27

# The resources that read through _send_sync, and therefore land on the
# implicit instance. Both the coroutine guard and the multi-instance
# documentation check count them.
SOCKET_BACKED_RESOURCES = (
    "qgis_info_resource",
    "project_info_resource",
    "layers_resource",
    "layer_info_resource",
    "layer_features_resource",
    "layer_schema_resource",
)


@pytest.fixture
def mock_connection():
    """A mocked QgisMCPClient standing in for the pooled connection.

    ``client.returns(payload)`` wraps *payload* in the success envelope the
    plugin really sends, which is otherwise hand-rolled in every test.
    """
    client = MagicMock(spec=QgisMCPClient)
    client.socket = MagicMock()
    client.socket.getpeername.return_value = ("localhost", 9876)

    def returns(result):
        client.send_command.return_value = {"status": "success", "result": result}

    client.returns = returns
    with patch("qgis_mcp.server.get_qgis_connection", return_value=client):
        yield client


def make_ctx(*, elicitation="confirm"):
    """Create a mock Context with async methods.

    elicitation: "confirm" (default) - user confirms destructive ops.
                 "decline" - user refuses.
                 "unsupported" - client doesn't support elicitation (raises McpError).

    The responses mirror the real SDK: `data` is a model instance (not a dict),
    and an unsupported client raises McpError - mocking a bare Exception with a
    dict payload is what let #27 hide.
    """
    ctx = MagicMock()
    for name in ("info", "warning", "error", "report_progress"):
        setattr(ctx, name, AsyncMock())
    if elicitation == "unsupported":
        ctx.elicit = AsyncMock(side_effect=make_mcp_error())
    else:
        elicit_response = MagicMock()
        elicit_response.action = "accept" if elicitation == "confirm" else "decline"
        elicit_response.data = _ConfirmSchema(confirm=elicitation == "confirm")
        ctx.elicit = AsyncMock(return_value=elicit_response)
    return ctx


@pytest.fixture
def connected():
    """Mark instances as already connected, so _send_sync uses the short schedule.

    Yields a callable taking instance names; whatever it added is removed again
    on teardown, leaving instances that were already there alone.
    """
    added = set()

    def mark(*names):
        new = set(names) - srv._first_connected
        srv._first_connected.update(new)
        added.update(new)

    yield mark
    srv._first_connected.difference_update(added)


@pytest.fixture
def cold_start():
    """Nothing has connected yet, so _send_sync takes the patient first-connect schedule."""
    previously = set(srv._first_connected)
    srv._first_connected.clear()
    yield
    srv._first_connected.update(previously)


@pytest.fixture
def clean_pool():
    """Run with an empty connection pool and leave it empty (and closed) afterwards."""
    srv._qgis_connections.clear()
    srv._connection_validated_at.clear()
    yield srv
    for name in list(srv._qgis_connections):
        srv._invalidate_connection(name)
    srv._connection_validated_at.clear()
