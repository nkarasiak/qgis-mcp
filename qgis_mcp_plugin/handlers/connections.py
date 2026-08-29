"""Handlers for saved data source connections (the Browser panel entries).

Connection URIs are password-redacted before they leave the plugin, and
PostgreSQL connections are created from a QGIS Authentication Manager config id,
from the libpq service file (pg_service.conf), or both, never from a password.
"""

import contextlib
import re
from typing import ClassVar

from qgis.core import (
    QgsAbstractDatabaseProviderConnection,
    QgsApplication,
    QgsCredentials,
    QgsDataSourceUri,
    QgsProject,
    QgsProviderRegistry,
    QgsVectorLayer,
    QgsVectorLayerExporter,
)

from ..compat import (
    CONN_CAP_EXECUTE_SQL,
    CONN_CAP_SCHEMAS,
    CONN_CAP_SQL_LAYERS,
    CONN_TABLE_ASPATIAL,
    CONN_TABLE_RASTER,
    CONN_TABLE_VECTOR,
    CONN_TABLE_VIEW,
    EXPORT_SUCCESS,
    URI_SSL_ALLOW,
    URI_SSL_DISABLE,
    URI_SSL_PREFER,
    URI_SSL_REQUIRE,
    URI_SSL_VERIFY_CA,
    URI_SSL_VERIFY_FULL,
)
from ..errors import CommandError
from ..registry import command


class _QuietCredentials(QgsCredentials):
    """Refuse every credential request instead of opening the Enter Credentials dialog."""

    def request(self, realm, username, password, message=""):
        return False, username, password

    def requestMasterPassword(self, password, stored=False):
        return False, password


class ConnectionHandlers:
    """Saved data source connections - the Browser panel entries."""

    # Connection URIs carry saved credentials; never hand those to a client.
    _URI_SECRET_RE = re.compile(r"\b(password|pass|pwd)=('[^']*'|\"[^\"]*\"|\S*)", re.IGNORECASE)

    _CONN_TABLE_FLAG_NAMES: ClassVar[tuple] = (
        ("vector", CONN_TABLE_VECTOR),
        ("raster", CONN_TABLE_RASTER),
        ("view", CONN_TABLE_VIEW),
        ("aspatial", CONN_TABLE_ASPATIAL),
    )

    _POSTGRESQL_SSL_MODES: ClassVar[dict] = {
        "prefer": URI_SSL_PREFER,
        "disable": URI_SSL_DISABLE,
        "allow": URI_SSL_ALLOW,
        "require": URI_SSL_REQUIRE,
        "verify-ca": URI_SSL_VERIFY_CA,
        "verify-full": URI_SSL_VERIFY_FULL,
    }

    @classmethod
    def _redact_uri(cls, uri):
        return cls._URI_SECRET_RE.sub(r"\1=***", uri or "")

    def _connection(self, provider, connection):
        """Look up a saved provider connection by name, or raise."""
        metadata = QgsProviderRegistry.instance().providerMetadata(provider)
        if metadata is None:
            raise CommandError(f"Unknown data provider: {provider!r}")
        try:
            connections = metadata.connections(False)
        except Exception as e:
            raise CommandError(f"Provider {provider!r} has no saved-connection support: {e}") from e
        if connection not in connections:
            raise CommandError(
                f"No saved {provider!r} connection named {connection!r} "
                f"(available: {sorted(connections)})"
            )
        return connections[connection]

    @command
    def list_connections(self, provider=None, **kwargs):
        """List saved data source connections (PostgreSQL, GeoPackage, ...)."""
        registry = QgsProviderRegistry.instance()
        providers = [provider] if provider else registry.providerList()
        entries = []
        for name in providers:
            metadata = registry.providerMetadata(name)
            if metadata is None:
                if provider:
                    raise CommandError(f"Unknown data provider: {name!r}")
                continue
            try:
                connections = metadata.connections(False)
            except Exception:
                connections = {}  # provider has no saved-connection support
            for conn_name, conn in connections.items():
                entry = {"provider": name, "name": conn_name}
                with contextlib.suppress(Exception):
                    entry["uri"] = self._redact_uri(conn.uri())
                entries.append(entry)
        return {"connections": entries, "count": len(entries)}

    # connection_mode -> the parameters that mode requires. `name` is always required and
    # the service modes take `database` as an optional override of the service file's dbname.
    _POSTGRESQL_MODES: ClassVar[dict] = {
        "endpoint_using_auth_manager": ("host", "port", "database", "auth_config_id"),
        "service_using_auth_manager": ("service", "auth_config_id"),
        "service_only": ("service",),
    }

    @command
    def create_postgresql_connection(
        self,
        name,
        connection_mode="endpoint_using_auth_manager",
        host=None,
        port=None,
        database=None,
        auth_config_id=None,
        ssl_mode="prefer",
        service=None,
        **kwargs,
    ):
        """Validate and persist a password-free PostgreSQL Browser connection.

        Credentials come from a QGIS Authentication Manager configuration, from the libpq
        service file (pg_service.conf), or both; a password is never accepted.
        `connection_mode` selects which parameters are required (`_POSTGRESQL_MODES`) and
        anything else that was passed is rejected rather than silently ignored.
        """
        required = self._pick(self._POSTGRESQL_MODES, connection_mode, "connection mode")
        given = {
            "name": name,
            "host": host,
            "port": port,
            "database": database,
            "auth_config_id": auth_config_id,
            "service": service,
        }
        given = {key: "" if value is None else str(value).strip() for key, value in given.items()}
        for key in ("name", *required):
            if not given[key]:
                raise CommandError(f"{key} is required for connection_mode {connection_mode!r}")
        allowed = {"name", "database", *required}
        for key, value in given.items():
            if value and key not in allowed:
                raise CommandError(f"{key} is not used by connection_mode {connection_mode!r}")
        name, host, database = given["name"], given["host"], given["database"]
        auth_config_id, service = given["auth_config_id"], given["service"]
        port_error = "PostgreSQL port must be an integer from 1 to 65535"
        if "port" in required:
            try:
                port = int(given["port"])
            except ValueError as exc:
                raise CommandError(port_error) from exc
            if not 1 <= port <= 65535:
                raise CommandError(port_error)

        normalized_ssl_mode = str(ssl_mode).strip().lower().replace("_", "-")
        ssl_value = self._pick(self._POSTGRESQL_SSL_MODES, normalized_ssl_mode, "SSL mode")

        if auth_config_id and auth_config_id not in QgsApplication.authManager().configIds():
            raise CommandError(f"Authentication configuration {auth_config_id!r} does not exist")

        metadata = QgsProviderRegistry.instance().providerMetadata("postgres")
        if metadata is None:
            raise CommandError("The PostgreSQL provider is not available in this QGIS installation")
        try:
            existing = metadata.connections(False)
        except Exception as exc:
            raise CommandError(f"PostgreSQL saved connections are unavailable: {exc}") from exc
        if name in existing:
            raise CommandError(f"A saved PostgreSQL connection named {name!r} already exists")

        uri = QgsDataSourceUri()
        if service:
            uri.setConnection(service, database, "", "", ssl_value, auth_config_id)
            target = f"service {service!r}"
        else:
            uri.setConnection(host, str(port), database, "", "", ssl_value, auth_config_id)
            target = f"{host}:{port}/{database}"
        # QgsPostgresConn asks QgsCredentials for a username and password whenever libpq
        # refuses the connection. In the GUI that is a modal dialog, which stalls the event
        # loop this server runs on until someone clicks Cancel (#37): answer "no" instead.
        previous = QgsCredentials.instance()
        quiet = _QuietCredentials()
        quiet.setInstance(quiet)
        try:
            connection = metadata.createConnection(uri.uri(False), {})
            connection.executeSql("SELECT 1")
        except Exception as exc:
            raise CommandError(f"Failed to connect to PostgreSQL ({target}): {exc}") from exc
        finally:
            quiet.setInstance(previous)

        metadata.saveConnection(connection, name)
        details = {
            key: given[key]
            for key in ("host", "database", "auth_config_id", "service")
            if given[key]
        }
        if "port" in required:
            details["port"] = port
        return {
            "ok": True,
            "provider": "postgres",
            "name": name,
            "connection_mode": connection_mode,
            **details,
            "ssl_mode": normalized_ssl_mode,
            "validated": True,
        }

    @command
    def list_connection_tables(self, provider, connection, schema=None, **kwargs):
        """List schemas and tables reachable through a saved connection."""
        conn = self._connection(provider, connection)
        schemas = []
        if conn.capabilities() & CONN_CAP_SCHEMAS:
            with contextlib.suppress(Exception):
                schemas = list(conn.schemas())
        if schema is None and schemas:
            return {
                "provider": provider,
                "connection": connection,
                "schemas": schemas,
                "message": "Pass schema= to list the tables of one of these schemas",
            }

        tables = []
        for table in conn.tables(schema or ""):
            flags = table.flags()
            crs_list = []
            with contextlib.suppress(Exception):
                crs_list = [c.authid() for c in table.crsList() if c.authid()]
            tables.append(
                {
                    "name": table.tableName(),
                    "schema": table.schema() or None,
                    "geometry_column": table.geometryColumn() or None,
                    "primary_key": list(table.primaryKeyColumns()),
                    "comment": table.comment() or None,
                    "crs": crs_list,
                    "kinds": [name for name, flag in self._CONN_TABLE_FLAG_NAMES if flags & flag],
                }
            )
        return {
            "provider": provider,
            "connection": connection,
            "schema": schema,
            "schemas": schemas,
            "tables": tables,
            "count": len(tables),
        }

    @command
    def add_layer_from_connection(
        self,
        provider,
        connection,
        table=None,
        schema=None,
        sql=None,
        geometry_column=None,
        primary_key=None,
        name=None,
        **kwargs,
    ):
        """Load a connection table (or a SQL query against it) as a project layer."""
        conn = self._connection(provider, connection)
        if sql:
            if not conn.capabilities() & CONN_CAP_SQL_LAYERS:
                raise CommandError(f"Provider {provider!r} cannot build layers from SQL queries")
            options = QgsAbstractDatabaseProviderConnection.SqlVectorLayerOptions()
            options.sql = sql
            options.layerName = name or "query"
            if geometry_column:
                options.geometryColumn = geometry_column
            if primary_key:
                options.primaryKeyColumns = [primary_key]
            layer = conn.createSqlVectorLayer(options)
        elif table:
            uri = conn.tableUri(schema or "", table)
            layer = QgsVectorLayer(uri, name or table, conn.providerKey())
        else:
            raise CommandError("Either table or sql must be provided")

        if layer is None or not layer.isValid():
            target = f"query {sql!r}" if sql else f"table {table!r}"
            error = layer.dataProvider().error().summary() if layer else ""
            raise CommandError(f"Failed to load {target} from {connection!r}: {error}")

        QgsProject.instance().addMapLayer(layer)
        return {
            "id": layer.id(),
            "name": layer.name(),
            "type": self._get_layer_type(layer),
            "feature_count": layer.featureCount(),
            "crs": layer.crs().authid(),
        }

    @command
    def import_layer_to_connection(
        self, layer_id, provider, connection, table, schema=None, overwrite=False, **kwargs
    ):
        """Write a loaded vector layer into a database/GeoPackage connection."""
        layer = self._get_vector_layer(layer_id)
        conn = self._connection(provider, connection)
        provider_key = conn.providerKey()

        exists = False
        with contextlib.suppress(Exception):
            exists = conn.tableExists(schema or "", table)
        if exists and not overwrite:
            raise CommandError(
                f"Table {table!r} already exists in {connection!r}; "
                "pass overwrite=true to replace it"
            )

        if provider_key == "ogr":
            # GeoPackage-style connections: the URI is the container file and the
            # table name rides in the options.
            uri = conn.uri()
            options = {"layerName": table, "update": True, "overwrite": bool(overwrite)}
        else:
            ds_uri = QgsDataSourceUri(conn.uri())
            ds_uri.setDataSource(
                schema or "",
                table,
                "geom" if layer.isSpatial() else "",
            )
            uri = ds_uri.uri(False)
            options = {"overwrite": bool(overwrite)}

        result, error = QgsVectorLayerExporter.exportLayer(
            layer, uri, provider_key, layer.crs(), False, options
        )
        if result != EXPORT_SUCCESS:
            raise CommandError(f"Import failed ({result}): {error}")
        return {
            "ok": True,
            "provider": provider,
            "connection": connection,
            "schema": schema,
            "table": table,
            "features": layer.featureCount(),
        }

    @command
    def execute_connection_sql(self, provider, connection, sql, limit=100, **kwargs):
        """Run SQL directly on the database behind a saved connection."""
        conn = self._connection(provider, connection)
        if not conn.capabilities() & CONN_CAP_EXECUTE_SQL:
            raise CommandError(f"Provider {provider!r} cannot execute SQL")
        rows = conn.executeSql(sql) or []
        limit = int(limit)
        truncated = limit >= 0 and len(rows) > limit
        if truncated:
            rows = rows[:limit]
        return {
            "rows": [[self._to_json_safe(v) for v in row] for row in rows],
            "count": len(rows),
            "truncated": truncated,
        }
