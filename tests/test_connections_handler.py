"""create_postgresql_connection handler against a stubbed qgis (no QGIS needed).

Regression for #37: the endpoint mode once passed the literal string "auth_config_id"
to QgsDataSourceUri.setConnection, which nothing short of a live database would catch.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "qgis_mcp_plugin"
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


@pytest.fixture(scope="module")
def handlers():
    saved = {name: sys.modules.get(name) for name in QGIS_MODULES}
    for name in QGIS_MODULES:
        sys.modules[name] = MagicMock()

    class FakeCredentials:
        """Subclassable stand-in: a MagicMock base turns the subclass into a one-shot mock."""

        current = "gui-dialog"

        @staticmethod
        def instance():
            return FakeCredentials.current

        def setInstance(self, instance):
            FakeCredentials.current = instance

    sys.modules["qgis.core"].QgsCredentials = FakeCredentials
    # A bare package: the real __init__ imports plugin.py, which needs a live QGIS.
    package = types.ModuleType("qgis_mcp_plugin")
    package.__path__ = [str(PLUGIN_DIR)]
    sys.modules["qgis_mcp_plugin"] = package
    from qgis_mcp_plugin.handlers import base, connections

    class Server(connections.ConnectionHandlers, base.HandlerBase):
        pass

    connections.Server = Server
    yield connections
    for name in [
        m for m in sys.modules if m == "qgis_mcp_plugin" or m.startswith("qgis_mcp_plugin.")
    ]:
        del sys.modules[name]
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


@pytest.fixture
def qgis(handlers):
    handlers.QgsApplication.authManager.return_value.configIds.return_value = ["authcfg1"]
    metadata = MagicMock()
    metadata.connections.return_value = {"taken": object()}
    handlers.QgsProviderRegistry.instance.return_value.providerMetadata.return_value = metadata
    uri = handlers.QgsDataSourceUri.return_value
    uri.reset_mock()
    return types.SimpleNamespace(
        server=handlers.Server(), metadata=metadata, uri=uri, error=handlers.CommandError
    )


def test_endpoint_mode_passes_the_auth_config_id(handlers, qgis):
    result = qgis.server.create_postgresql_connection(
        name=" warehouse ",
        connection_mode="endpoint_using_auth_manager",
        host="db.example.test",
        port=5433,
        database="gis",
        auth_config_id="authcfg1",
        ssl_mode="verify_full",
    )
    qgis.uri.setConnection.assert_called_once_with(
        "db.example.test", "5433", "gis", "", "", handlers.URI_SSL_VERIFY_FULL, "authcfg1"
    )
    qgis.metadata.saveConnection.assert_called_once_with(
        qgis.metadata.createConnection.return_value, "warehouse"
    )
    assert result["name"] == "warehouse"
    assert result["port"] == 5433
    assert result["ssl_mode"] == "verify-full"
    assert "service" not in result


def test_service_only_mode_uses_the_service_overload(qgis):
    result = qgis.server.create_postgresql_connection(
        name="prod", connection_mode="service_only", service="prod_gis", database="override"
    )
    qgis.uri.setConnection.assert_called_once_with(
        "prod_gis", "override", "", "", qgis.uri.setConnection.call_args[0][4], ""
    )
    assert result["service"] == "prod_gis"
    assert result["database"] == "override"
    assert "auth_config_id" not in result and "port" not in result


def test_service_with_auth_manager_checks_the_config_exists(qgis):
    with pytest.raises(qgis.error, match="does not exist"):
        qgis.server.create_postgresql_connection(
            name="prod",
            connection_mode="service_using_auth_manager",
            service="prod_gis",
            auth_config_id="nope",
        )


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"connection_mode": "magic"}, "Unknown connection mode"),
        ({"connection_mode": "service_only"}, "service is required"),
        (
            {"connection_mode": "service_using_auth_manager", "service": "s"},
            "auth_config_id is required",
        ),
        (
            {"connection_mode": "service_only", "service": "s", "auth_config_id": "authcfg1"},
            "not used by",
        ),
        ({"connection_mode": "service_only", "service": "s", "host": "h"}, "not used by"),
        (
            {"host": "h", "port": "abc", "database": "d", "auth_config_id": "authcfg1"},
            "port must be an integer",
        ),
        (
            {"host": "h", "port": 0, "database": "d", "auth_config_id": "authcfg1"},
            "port must be an integer",
        ),
        (
            {
                "host": "h",
                "port": 1,
                "database": "d",
                "auth_config_id": "authcfg1",
                "ssl_mode": "x",
            },
            "Unknown SSL",
        ),
    ],
)
def test_rejects_bad_parameters_before_touching_qgis(qgis, params, message):
    with pytest.raises(qgis.error, match=message):
        qgis.server.create_postgresql_connection(name="n", **params)
    qgis.uri.setConnection.assert_not_called()


def test_duplicate_name_and_connect_failure(qgis):
    with pytest.raises(qgis.error, match="already exists"):
        qgis.server.create_postgresql_connection(
            name="taken", connection_mode="service_only", service="s"
        )
    qgis.metadata.createConnection.side_effect = RuntimeError("boom")
    with pytest.raises(qgis.error, match=r"service 's'.*boom"):
        qgis.server.create_postgresql_connection(
            name="fresh", connection_mode="service_only", service="s"
        )
    qgis.metadata.saveConnection.assert_not_called()


def test_connection_test_never_opens_the_credentials_dialog(handlers, qgis):
    """QgsPostgresConn prompts on any libpq refusal; the quiet instance must be in place during the test."""
    seen = []

    def create_connection(*_):
        seen.append(handlers.QgsCredentials.instance())
        return MagicMock()

    qgis.metadata.createConnection.side_effect = create_connection
    qgis.server.create_postgresql_connection(
        name="fresh", connection_mode="service_only", service="s"
    )
    assert isinstance(seen[0], handlers._QuietCredentials)
    assert handlers.QgsCredentials.instance() == "gui-dialog"
    qgis.metadata.createConnection.side_effect = RuntimeError("refused")
    with pytest.raises(qgis.error, match="refused"):
        qgis.server.create_postgresql_connection(
            name="fresh", connection_mode="service_only", service="s"
        )
    assert handlers.QgsCredentials.instance() == "gui-dialog"
