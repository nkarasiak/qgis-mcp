"""Handlers for saved data source connections (the Browser panel entries).

Connection URIs are password-redacted before they leave the plugin, and
PostgreSQL connections are created from an external credential source: QGIS
Authentication Manager or a libpq, never a raw or otherwised exposed password.
"""

import contextlib
import re
from typing import ClassVar

from qgis.core import (
    QgsAbstractDatabaseProviderConnection,
    QgsApplication,
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

    def _connect_and_save_postgresql(self, metadata, uri, name, service=None):
        try:
            connection = metadata.createConnection(uri.uri(False), {})
            connection.executeSql("SELECT 1")
        except Exception as exc:
            if service:
                raise CommandError(
                    f"Failed to connect to PostgreSQL via service {service!r}: {exc}"
                ) from exc
            raise CommandError(f"Failed to connect to PostgreSQL: {exc}") from exc
        metadata.saveConnection(connection, name)

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

    @command
    def create_postgresql_connection(
        self,
        name,
        connection_mode=None,
        host=None,
        port=None,
        database=None,
        auth_config_id=None,
        ssl_mode="prefer",
        service=None,
        **kwargs,
    ):
        """Create a new PostgreSQL Browser connection; persist it after validation.

        Only allowing password-less connections; three modes:
          A) Explicit endpoint with QGIS Auth Manager: connection_mode=qgis_auth_manager;
             name + host + port + database + auth_config_id (all required).
          B) Service with QGIS Auth Manager: connection_mode=qgis_auth_manager;
             name + service + auth_config_id (all required).
          C) Only Service: connection_mode=service_file; name + service (required);
             authentication from service file (pg_service.conf).

        In paths B/C, database is an optional override for the service-file dbname. When it's missing from the config file, it normally defaults to the user name.
        """

        normalized_ssl_mode = str(ssl_mode).strip().lower().replace("_", "-")
        ssl_value = self._POSTGRESQL_SSL_MODES.get(normalized_ssl_mode)
        if ssl_value is None:
            allowed = ", ".join(self._POSTGRESQL_SSL_MODES)
            raise CommandError(
                f"Unknown SSL mode {ssl_mode!r}; expected one of: {allowed}"
            )

        metadata = QgsProviderRegistry.instance().providerMetadata("postgres")
        if metadata is None:
            raise CommandError(
                "The PostgreSQL provider is not available in this QGIS installation"
            )
        try:
            existing = metadata.connections(False)
        except Exception as exc:
            raise CommandError(
                f"PostgreSQL saved connections are unavailable: {exc}"
            ) from exc
        if name in existing:
            raise CommandError(
                f"A saved PostgreSQL connection named {name!r} already exists"
            )

        uri = QgsDataSourceUri()

        # Mode A
        if connection_mode == "endpoint_using_auth_manager":
            auth_manager = QgsApplication.authManager()
            if auth_config_id not in auth_manager.configIds():
                raise CommandError(
                    f"Authentication configuration {auth_config_id!r} does not exist"
                )

            uri.setConnection(
                host,
                str(port),
                database,
                "",
                "",
                ssl_value,
                "auth_config_id",
            )
            self._connect_and_save_postgresql(metadata, uri, name)
            return {
                "ok": True,
                "provider": "postgres",
                "name": name,
                "connection_mode": connection_mode,
                "host": host,
                "port": port,
                "database": database,
                "auth_config_id": auth_config_id,
                "ssl_mode": normalized_ssl_mode,
                "validated": True,
            }

        # Mode B
        if connection_mode == "service_using_auth_manager":
            auth_manager = QgsApplication.authManager()
            if auth_config_id not in auth_manager.configIds():
                raise CommandError(
                    f"Authentication configuration {auth_config_id!r} does not exist"
                )

            db_override = database or ""
            uri.setConnection(
                service,
                db_override,
                "",
                "",
                ssl_value,
                auth_config_id,
            )
            self._connect_and_save_postgresql(metadata, uri, name, service=service)
            return {
                "ok": True,
                "provider": "postgres",
                "name": name,
                "connection_mode": connection_mode,
                "service": service,
                "database_override": db_override or None,
                "auth_config_id": auth_config_id,
                "ssl_mode": normalized_ssl_mode,
                "validated": True,
            }

        # Mode C
        if connection_mode == "service_only":
            db_override = database or ""
            uri.setConnection(service, db_override, "", "", ssl_value)
            self._connect_and_save_postgresql(metadata, uri, name, service=service)
            return {
                "ok": True,
                "provider": "postgres",
                "name": name,
                "connection_mode": connection_mode,
                "service": service,
                "database_override": db_override or None,
                "auth_config_id": auth_config_id,
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
