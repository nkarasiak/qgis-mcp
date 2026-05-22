import base64
import contextlib
import fnmatch
import io
import json
import os
import socket
import struct
import sys
import traceback
from collections import deque
from datetime import UTC, datetime
from typing import ClassVar

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsClassificationEqualInterval,
    QgsClassificationJenks,
    QgsClassificationPrettyBreaks,
    QgsClassificationQuantile,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsGraduatedSymbolRenderer,
    QgsHeatmapRenderer,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsLayoutExporter,
    QgsLineSymbol,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsMarkerSymbol,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    QgsProperty,
    QgsRasterLayer,
    QgsRectangle,
    QgsRendererCategory,
    QgsSettings,
    QgsSingleSymbolRenderer,
    QgsStyle,
    QgsSymbol,
    QgsSymbolLayer,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QBuffer, QByteArray, QObject, QSize, QTimer, QUrl, QVariant
from qgis.PyQt.QtGui import QColor, QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import (
    QAction,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)
from qgis.utils import active_plugins, available_plugins, pluginMetadata, reloadPlugin

from .compat import (
    AGG_ARRAY,
    AGG_COUNT,
    AGG_MAX,
    AGG_MEAN,
    AGG_MIN,
    AGG_STDEV,
    AGG_SUM,
    GEOM_LINE,
    GEOM_POLYGON,
    IODEVICE_WRITEONLY,
    LAYER_RASTER,
    LAYER_VECTOR,
    LAYOUT_SUCCESS,
    MSG_CRITICAL,
    MSG_INFO,
    MSG_WARNING,
    PROCESSING_OPTIONAL,
    RASTER_STATS_ALL,
    TOOLBUTTON_ICON_ONLY,
    TOOLBUTTON_MENU_POPUP,
)


_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = 9877  # qgis-mcp-north uses 9877 (vs upstream nkarasiak on 9876)
_RECV_CHUNK_SIZE = 65536
_MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10 MB
_HEADER_STRUCT = struct.Struct(">I")


class QgisMCPServer(QObject):
    """Server class to handle socket connections and execute QGIS commands"""

    LOG_TAG: ClassVar[str] = "MCP-NORTH"
    MAX_CLIENTS: ClassVar[int] = 10

    def __init__(self, host=_DEFAULT_HOST, port=_DEFAULT_PORT, iface=None):
        super().__init__()
        self.host = host
        self.port = port
        self.iface = iface
        self.running = False
        self.socket = None
        self.clients: dict[socket.socket, bytes] = {}
        self.timer = None
        self._message_log = deque(maxlen=1000)

    def start(self):
        """Start the server"""
        self.running = True
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.socket.bind((self.host, self.port))
            self.socket.listen(5)
            self.socket.setblocking(False)

            self.timer = QTimer()
            self.timer.timeout.connect(self.process_server)
            self.timer.start(25)  # 25ms interval

            msg_log = QgsApplication.messageLog()
            if hasattr(msg_log, "messageReceivedWithFormat"):
                msg_log.messageReceivedWithFormat.connect(self._capture_message)
            else:
                msg_log.messageReceived.connect(self._capture_message)
            QgsMessageLog.logMessage(
                f"QGIS MCP server started on {self.host}:{self.port}", self.LOG_TAG, MSG_INFO
            )
            return True
        except Exception as e:
            QgsMessageLog.logMessage(f"Failed to start server: {e!s}", self.LOG_TAG, MSG_CRITICAL)
            self.stop()
            return False

    def stop(self):
        """Stop the server"""
        self.running = False

        with contextlib.suppress(Exception):
            msg_log = QgsApplication.messageLog()
            if hasattr(msg_log, "messageReceivedWithFormat"):
                msg_log.messageReceivedWithFormat.disconnect(self._capture_message)
            else:
                msg_log.messageReceived.disconnect(self._capture_message)

        if self.timer:
            self.timer.stop()
            self.timer = None

        if self.socket:
            self.socket.close()
        for client_sock in list(self.clients):
            with contextlib.suppress(Exception):
                client_sock.close()
        self.clients.clear()

        self.socket = None
        QgsMessageLog.logMessage("QGIS MCP server stopped", self.LOG_TAG, MSG_INFO)

    def _disconnect_client(self, client_sock, message="Client disconnected", level=MSG_INFO):
        """Close and remove a client socket."""
        with contextlib.suppress(Exception):
            client_sock.close()
        self.clients.pop(client_sock, None)
        QgsMessageLog.logMessage(f"{message} ({len(self.clients)} active)", self.LOG_TAG, level)

    def _send_response(self, client_sock, response):
        """Send a length-prefixed JSON response to a client."""
        resp_bytes = json.dumps(response).encode("utf-8")
        header = _HEADER_STRUCT.pack(len(resp_bytes))
        client_sock.sendall(header + resp_bytes)

    def process_server(self):
        """Process server operations (called by timer)"""
        if not self.running:
            return

        try:
            # Accept new connections (loop until no pending or at capacity)
            if self.socket:
                while len(self.clients) < self.MAX_CLIENTS:
                    try:
                        client_sock, address = self.socket.accept()
                        client_sock.setblocking(False)
                        self.clients[client_sock] = b""
                        QgsMessageLog.logMessage(
                            f"Connected to client: {address} ({len(self.clients)} active)",
                            self.LOG_TAG,
                            MSG_INFO,
                        )
                    except BlockingIOError:
                        break
                    except Exception as e:
                        QgsMessageLog.logMessage(
                            f"Error accepting connection: {e!s}", self.LOG_TAG, MSG_WARNING
                        )
                        break

            # Process each connected client
            for client_sock in list(self.clients):
                try:
                    data = client_sock.recv(_RECV_CHUNK_SIZE)
                    if data:
                        buf = self.clients[client_sock] + data
                        if len(buf) > _MAX_MESSAGE_SIZE:
                            raise ValueError("Buffer exceeded 10 MB limit")
                        # Process complete length-prefixed messages
                        while len(buf) >= 4:
                            msg_len = _HEADER_STRUCT.unpack(buf[:4])[0]
                            if msg_len > _MAX_MESSAGE_SIZE:
                                raise ValueError(f"Message too large: {msg_len} bytes")
                            if len(buf) < 4 + msg_len:
                                break  # Incomplete message
                            msg_bytes = buf[4 : 4 + msg_len]
                            buf = buf[4 + msg_len :]
                            try:
                                command = json.loads(msg_bytes.decode("utf-8"))
                            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                                QgsMessageLog.logMessage(
                                    f"Malformed request: {e!s}", self.LOG_TAG, MSG_WARNING
                                )
                                self._send_response(
                                    client_sock,
                                    {"status": "error", "message": f"Invalid JSON: {e!s}"},
                                )
                                continue
                            response = self.execute_command(command)
                            self._send_response(client_sock, response)
                        self.clients[client_sock] = buf
                    else:
                        self._disconnect_client(client_sock)
                except BlockingIOError:
                    pass
                except Exception as e:
                    self._disconnect_client(client_sock, f"Error with client: {e!s}", MSG_WARNING)

        except Exception as e:
            QgsMessageLog.logMessage(f"Server error: {e!s}", self.LOG_TAG, MSG_CRITICAL)

    def execute_command(self, command):
        """Execute a command"""
        try:
            cmd_type = command.get("type")
            params = command.get("params", {})

            handlers = {
                "ping": self.ping,
                "get_qgis_info": self.get_qgis_info,
                "load_project": self.load_project,
                "get_project_info": self.get_project_info,
                "execute_code": self.execute_code,
                "add_vector_layer": self.add_vector_layer,
                "add_raster_layer": self.add_raster_layer,
                "get_layers": self.get_layers,
                "remove_layer": self.remove_layer,
                "zoom_to_layer": self.zoom_to_layer,
                "get_layer_features": self.get_layer_features,
                "execute_processing": self.execute_processing,
                "save_project": self.save_project,
                "render_map_base64": self.render_map_base64,
                "render_layers_to_path": self.render_layers_to_path,
                "render_choropleth": self.render_choropleth,
                "render_trajectory": self.render_trajectory,
                "render_od_flows": self.render_od_flows,
                "project_load": self.project_load,
                "batch_render": self.batch_render,
                "create_new_project": self.create_new_project,
                "get_field_statistics": self.get_field_statistics,
                "set_layer_visibility": self.set_layer_visibility,
                "get_canvas_extent": self.get_canvas_extent,
                "set_canvas_extent": self.set_canvas_extent,
                "get_raster_info": self.get_raster_info,
                "get_layer_info": self.get_layer_info,
                "get_layer_schema": self.get_layer_schema,
                "batch": self.batch,
                # Phase 2 new handlers
                "add_features": self.add_features,
                "update_features": self.update_features,
                "delete_features": self.delete_features,
                "set_layer_style": self.set_layer_style,
                "select_features": self.select_features,
                "get_selection": self.get_selection,
                "clear_selection": self.clear_selection,
                "create_memory_layer": self.create_memory_layer,
                "list_processing_algorithms": self.list_processing_algorithms,
                "get_algorithm_help": self.get_algorithm_help,
                "find_layer": self.find_layer,
                "list_layouts": self.list_layouts,
                "export_layout": self.export_layout,
                # Phase 3 — Plugin development & system management
                "get_message_log": self.get_message_log,
                "list_plugins": self.list_plugins,
                "get_plugin_info": self.get_plugin_info,
                "reload_plugin": self.reload_plugin,
                "get_layer_tree": self.get_layer_tree,
                "create_layer_group": self.create_layer_group,
                "move_layer_to_group": self.move_layer_to_group,
                "set_layer_property": self.set_layer_property,
                "get_layer_extent": self.get_layer_extent,
                "get_project_variables": self.get_project_variables,
                "set_project_variable": self.set_project_variable,
                "validate_expression": self.validate_expression,
                "get_setting": self.get_setting,
                "set_setting": self.set_setting,
                # Phase 4 — MCP modernization
                "get_canvas_screenshot": self.get_canvas_screenshot,
                "transform_coordinates": self.transform_coordinates,
                "diagnose": self.diagnose,
                # Phase 5 — High-value capabilities
                "get_active_layer": self.get_active_layer,
                "set_active_layer": self.set_active_layer,
                "get_canvas_scale": self.get_canvas_scale,
                "set_canvas_scale": self.set_canvas_scale,
                "get_layer_labeling": self.get_layer_labeling,
                "set_layer_labeling": self.set_layer_labeling,
                "get_layer_crs": self.get_layer_crs,
                "set_layer_crs": self.set_layer_crs,
                "get_bookmarks": self.get_bookmarks,
                "add_bookmark": self.add_bookmark,
                "remove_bookmark": self.remove_bookmark,
                "get_map_themes": self.get_map_themes,
                "add_map_theme": self.add_map_theme,
                "remove_map_theme": self.remove_map_theme,
                "apply_map_theme": self.apply_map_theme,
                "set_project_crs": self.set_project_crs,
            }

            handler = handlers.get(cmd_type)
            if handler:
                try:
                    QgsMessageLog.logMessage(f"Executing: {cmd_type}", self.LOG_TAG, MSG_INFO)
                    result = handler(**params)
                    return {"status": "success", "result": result}
                except Exception as e:
                    QgsMessageLog.logMessage(
                        f"Error in {cmd_type}: {e!s}", self.LOG_TAG, MSG_CRITICAL
                    )
                    return {"status": "error", "message": str(e)}
            else:
                QgsMessageLog.logMessage(f"Unknown command: {cmd_type}", self.LOG_TAG, MSG_WARNING)
                return {"status": "error", "message": f"Unknown command type: {cmd_type}"}

        except Exception as e:
            QgsMessageLog.logMessage(f"Error executing command: {e!s}", self.LOG_TAG, MSG_CRITICAL)
            return {"status": "error", "message": str(e)}

    # -----------------------------------------------------------------------
    # Command handlers
    # -----------------------------------------------------------------------

    def ping(self, **kwargs):
        return {"pong": True}

    def diagnose(self, **kwargs):
        """Run diagnostic checks and return health status."""
        checks = []
        overall = "healthy"

        # 1. QGIS info
        try:
            from qgis.PyQt.QtCore import QT_VERSION_STR as qt_ver

            info = {
                "qgis_version": Qgis.version(),
                "python_version": sys.version.split()[0],
                "qt_version": qt_ver,
            }
            checks.append({"name": "qgis", "status": "ok", "detail": info})
        except Exception as e:
            checks.append({"name": "qgis", "status": "error", "detail": str(e)})
            overall = "error"

        # 2. Plugin version
        try:
            import configparser

            metadata_path = os.path.join(os.path.dirname(__file__), "metadata.txt")
            config = configparser.ConfigParser()
            config.read(metadata_path)
            plugin_version = config.get("general", "version", fallback="unknown")
            checks.append({"name": "plugin_version", "status": "ok", "detail": plugin_version})
        except Exception as e:
            checks.append({"name": "plugin_version", "status": "error", "detail": str(e)})
            overall = "degraded" if overall == "healthy" else overall

        # 3. Connected clients
        client_count = len(self.clients)
        checks.append({"name": "connected_clients", "status": "ok", "detail": client_count})

        # 4. Processing providers
        try:
            registry = QgsApplication.processingRegistry()
            providers = [p.id() for p in registry.providers() if p.isActive()]
            checks.append({"name": "processing_providers", "status": "ok", "detail": providers})
        except Exception as e:
            checks.append({"name": "processing_providers", "status": "degraded", "detail": str(e)})
            overall = "degraded" if overall == "healthy" else overall

        # 5. Project status
        try:
            project = QgsProject.instance()
            checks.append(
                {
                    "name": "project",
                    "status": "ok",
                    "detail": {
                        "loaded": bool(project.fileName()),
                        "path": project.fileName() or None,
                        "layer_count": len(project.mapLayers()),
                    },
                }
            )
        except Exception as e:
            checks.append({"name": "project", "status": "error", "detail": str(e)})
            overall = "degraded" if overall == "healthy" else overall

        return {"status": overall, "checks": checks}

    def get_qgis_info(self, **kwargs):
        return {
            "qgis_version": Qgis.version(),
            "profile_folder": QgsApplication.qgisSettingsDirPath(),
            "plugins_count": len(active_plugins),
        }

    def get_project_info(self, **kwargs):
        project = QgsProject.instance()

        info = {
            "filename": project.fileName(),
            "title": project.title(),
            "layer_count": len(project.mapLayers()),
            "crs": project.crs().authid(),
            "layers": [],
        }

        layers = list(project.mapLayers().values())
        for layer in layers[:10]:
            layer_info = {
                "id": layer.id(),
                "name": layer.name(),
                "type": self._get_layer_type(layer),
                "visible": (
                    layer.isValid() and project.layerTreeRoot().findLayer(layer.id()).isVisible()
                ),
            }
            info["layers"].append(layer_info)

        return info

    def _get_layer_type(self, layer):
        if layer.type() == LAYER_VECTOR:
            return f"vector_{layer.geometryType()}"
        elif layer.type() == LAYER_RASTER:
            return "raster"
        else:
            return str(layer.type())

    def _convert_to_python_type(self, qvariant):
        if qvariant.isNull():
            return None
        value = qvariant.value()
        if isinstance(value, int | float | str | bool | type(None)):
            return value
        elif hasattr(value, "toPyDate"):
            return value.toPyDate().isoformat()
        elif hasattr(value, "toPyDateTime"):
            return value.toPyDateTime().isoformat()
        else:
            try:
                return str(value)
            except Exception:
                return None

    def _convert_attribute(self, value):
        """Convert a feature attribute value to a JSON-serializable type."""
        if isinstance(value, QVariant):
            return self._convert_to_python_type(value)
        if isinstance(value, int | float | str | bool | type(None)):
            return value
        try:
            return str(value)
        except Exception:
            return None

    def execute_code(self, code, return_vars=None, **kwargs):
        """Execute arbitrary PyQGIS. Optionally capture named local-scope variables
        and return them JSON-serialized (falling back to repr() for non-serializable
        PyQGIS objects like QgsGeometry / QgsVectorLayer).

        Unbound variable names in return_vars are omitted from the response (a
        deliberate choice — None would be ambiguous with "var was assigned None").
        """
        QgsMessageLog.logMessage(f"Executing code ({len(code)} chars)", self.LOG_TAG, MSG_INFO)
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        original_stdout = sys.stdout
        original_stderr = sys.stderr

        try:
            sys.stdout = stdout_capture
            sys.stderr = stderr_capture

            namespace = {
                "qgis": Qgis,
                "QgsProject": QgsProject,
                "iface": self.iface,
                "QgsApplication": QgsApplication,
                "QgsVectorLayer": QgsVectorLayer,
                "QgsRasterLayer": QgsRasterLayer,
                "QgsCoordinateReferenceSystem": QgsCoordinateReferenceSystem,
            }

            exec(code, namespace)  # nosec B102 — intentional: MCP execute_code tool

            captured = {}
            if return_vars:
                for var in return_vars:
                    if var in namespace:
                        captured[var] = self._json_safe(namespace[var])

            response = {
                "executed": True,
                "stdout": stdout_capture.getvalue(),
                "stderr": stderr_capture.getvalue(),
            }
            if return_vars:
                response["return_values"] = captured
            return response
        except Exception as e:
            error_traceback = traceback.format_exc()
            return {
                "executed": False,
                "error": str(e),
                "traceback": error_traceback,
                "stdout": stdout_capture.getvalue(),
                "stderr": stderr_capture.getvalue(),
            }
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr

    @staticmethod
    def _json_safe(value):
        """Best-effort JSON-serializable form of a Python or PyQGIS value.

        Tries json.dumps round-trip; on failure, falls back to repr(). This lets
        callers capture QgsGeometry / QgsVectorLayer / QgsRectangle as their
        debugging repr() string rather than failing the whole call.
        """
        try:
            json.dumps(value)
            return value
        except (TypeError, ValueError):
            return repr(value)

    def add_vector_layer(self, path, name=None, provider="ogr", **kwargs):
        if not name:
            name = os.path.basename(path)

        layer = QgsVectorLayer(path, name, provider)
        if not layer.isValid():
            raise Exception(f"Layer is not valid: {path}")

        QgsProject.instance().addMapLayer(layer)
        QgsMessageLog.logMessage(f"Vector layer added: {name}", self.LOG_TAG, MSG_INFO)

        return {
            "id": layer.id(),
            "name": layer.name(),
            "type": self._get_layer_type(layer),
            "feature_count": layer.featureCount(),
        }

    def add_raster_layer(self, path, name=None, provider="gdal", **kwargs):
        if not name:
            name = os.path.basename(path)

        layer = QgsRasterLayer(path, name, provider)
        if not layer.isValid():
            raise Exception(f"Layer is not valid: {path}")

        QgsProject.instance().addMapLayer(layer)
        QgsMessageLog.logMessage(f"Raster layer added: {name}", self.LOG_TAG, MSG_INFO)

        return {
            "id": layer.id(),
            "name": layer.name(),
            "type": "raster",
            "width": layer.width(),
            "height": layer.height(),
        }

    def get_layers(self, limit=50, offset=0, **kwargs):
        project = QgsProject.instance()
        all_layers = list(project.mapLayers().items())
        total_count = len(all_layers)
        page = all_layers[offset : offset + limit]

        layers = []
        for layer_id, layer in page:
            layer_info = {
                "id": layer_id,
                "name": layer.name(),
                "type": self._get_layer_type(layer),
                "visible": project.layerTreeRoot().findLayer(layer_id).isVisible(),
            }

            if layer.type() == LAYER_VECTOR:
                layer_info.update(
                    {"feature_count": layer.featureCount(), "geometry_type": layer.geometryType()}
                )
            elif layer.type() == LAYER_RASTER:
                layer_info.update({"width": layer.width(), "height": layer.height()})

            layers.append(layer_info)

        return {"layers": layers, "total_count": total_count, "offset": offset, "limit": limit}

    def remove_layer(self, layer_id, **kwargs):
        project = QgsProject.instance()
        if layer_id in project.mapLayers():
            layer_name = project.mapLayer(layer_id).name()
            project.removeMapLayer(layer_id)
            QgsMessageLog.logMessage(f"Layer removed: {layer_name}", self.LOG_TAG, MSG_INFO)
            return {"ok": True}
        else:
            raise Exception(f"Layer not found: {layer_id}")

    def zoom_to_layer(self, layer_id, **kwargs):
        project = QgsProject.instance()
        if layer_id in project.mapLayers():
            layer = project.mapLayer(layer_id)
            self.iface.setActiveLayer(layer)
            self.iface.zoomToActiveLayer()
            return {"ok": True}
        else:
            raise Exception(f"Layer not found: {layer_id}")

    def get_layer_features(
        self, layer_id, limit=10, offset=0, expression=None, include_geometry=False, **kwargs
    ):
        project = QgsProject.instance()

        if layer_id not in project.mapLayers():
            raise Exception(f"Layer not found: {layer_id}")

        layer = project.mapLayer(layer_id)
        if layer.type() != LAYER_VECTOR:
            raise Exception(f"Layer is not a vector layer: {layer_id}")

        field_names = [field.name() for field in layer.fields()]
        feature_count = layer.featureCount()

        request = QgsFeatureRequest()
        if expression:
            request.setFilterExpression(expression)

        features = []
        skipped = 0
        for feature in layer.getFeatures(request):
            if skipped < offset:
                skipped += 1
                continue
            if len(features) >= limit:
                break

            # Phase 1C: Flatten to {"_fid": id, ...attrs} instead of nested "attributes"
            feature_obj = {"_fid": feature.id()}
            for field in layer.fields():
                feature_obj[field.name()] = self._convert_attribute(feature.attribute(field.name()))

            if include_geometry and feature.hasGeometry():
                geom = feature.geometry()
                geom_type = geom.type()

                wkb_type_name = QgsWkbTypes.displayString(geom.wkbType())

                if geom_type in [GEOM_POLYGON, GEOM_LINE]:
                    simplified_geom = geom.simplify(0.001)
                    points_count = len(simplified_geom.asWkt().split(","))
                    geom_obj = {
                        "type": geom_type,
                        "wkb_type": wkb_type_name,
                        "wkt_summary": f"{wkb_type_name} with {points_count} points",
                        "bbox": [
                            geom.boundingBox().xMinimum(),
                            geom.boundingBox().yMinimum(),
                            geom.boundingBox().xMaximum(),
                            geom.boundingBox().yMaximum(),
                        ],
                    }
                else:
                    geom_obj = {
                        "type": geom_type,
                        "wkb_type": wkb_type_name,
                        "wkt": geom.asWkt(precision=3),
                    }

                feature_obj["_geometry"] = geom_obj

            features.append(feature_obj)

        # Phase 1B: Stripped layer_id, layer_name, geometry_included
        return {
            "feature_count": feature_count,
            "fields": field_names,
            "features": features,
        }

    def get_field_statistics(self, layer_id, field_name, **kwargs):
        project = QgsProject.instance()
        if layer_id not in project.mapLayers():
            raise Exception(f"Layer not found: {layer_id}")

        layer = project.mapLayer(layer_id)
        if layer.type() != LAYER_VECTOR:
            raise Exception(f"Layer is not a vector layer: {layer_id}")

        field_idx = layer.fields().indexOf(field_name)
        if field_idx < 0:
            raise Exception(f"Field not found: {field_name}")

        field = layer.fields().at(field_idx)
        is_numeric = field.isNumeric()

        # Phase 1B: Stripped layer_id, field_name
        stats = {"is_numeric": is_numeric}

        if is_numeric:
            for stat_name, stat_enum in [
                ("count", AGG_COUNT),
                ("sum", AGG_SUM),
                ("mean", AGG_MEAN),
                ("min", AGG_MIN),
                ("max", AGG_MAX),
                ("stdev", AGG_STDEV),
            ]:
                val, ok = layer.aggregate(stat_enum, field_name)
                if ok:
                    stats[stat_name] = val
        else:
            count_val, ok = layer.aggregate(AGG_COUNT, field_name)
            if ok:
                stats["count"] = count_val
            distinct_val, ok = layer.aggregate(AGG_ARRAY, field_name)
            if ok and isinstance(distinct_val, list):
                unique = list(set(str(v) for v in distinct_val if v is not None))
                stats["distinct_count"] = len(unique)
                stats["distinct_values"] = unique[:50]

        return stats

    def set_layer_visibility(self, layer_id, visible, **kwargs):
        project = QgsProject.instance()
        if layer_id not in project.mapLayers():
            raise Exception(f"Layer not found: {layer_id}")

        tree_layer = project.layerTreeRoot().findLayer(layer_id)
        if tree_layer is None:
            raise Exception(f"Layer not found in layer tree: {layer_id}")

        tree_layer.setItemVisibilityChecked(visible)
        # Phase 1B: Stripped layer_id, return only visible state
        return {"visible": visible}

    def get_canvas_extent(self, **kwargs):
        canvas = self.iface.mapCanvas()
        extent = canvas.extent()
        crs = canvas.mapSettings().destinationCrs()
        return {
            "xmin": extent.xMinimum(),
            "ymin": extent.yMinimum(),
            "xmax": extent.xMaximum(),
            "ymax": extent.yMaximum(),
            "crs": crs.authid(),
            "width": canvas.width(),
            "height": canvas.height(),
        }

    def set_canvas_extent(self, xmin, ymin, xmax, ymax, crs=None, **kwargs):
        canvas = self.iface.mapCanvas()
        rect = QgsRectangle(xmin, ymin, xmax, ymax)

        if crs:
            src_crs = QgsCoordinateReferenceSystem(crs)
            dst_crs = canvas.mapSettings().destinationCrs()
            if src_crs != dst_crs:
                transform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
                rect = transform.transformBoundingBox(rect)

        canvas.setExtent(rect)
        canvas.refresh()
        return {"extent": [rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()]}

    def get_raster_info(self, layer_id, **kwargs):
        project = QgsProject.instance()
        if layer_id not in project.mapLayers():
            raise Exception(f"Layer not found: {layer_id}")

        layer = project.mapLayer(layer_id)
        if layer.type() != LAYER_RASTER:
            raise Exception(f"Layer is not a raster layer: {layer_id}")

        dp = layer.dataProvider()
        extent = layer.extent()

        # Phase 1B: Stripped layer_id, name
        info = {
            "width": layer.width(),
            "height": layer.height(),
            "band_count": layer.bandCount(),
            "crs": layer.crs().authid(),
            "extent": {
                "xmin": extent.xMinimum(),
                "ymin": extent.yMinimum(),
                "xmax": extent.xMaximum(),
                "ymax": extent.yMaximum(),
            },
            "bands": [],
        }

        for band in range(1, layer.bandCount() + 1):
            band_info = {"band": band}
            try:
                stats = dp.bandStatistics(band, RASTER_STATS_ALL)
                band_info.update(
                    {
                        "min": stats.minimumValue,
                        "max": stats.maximumValue,
                        "mean": stats.mean,
                        "stdev": stats.stdDev,
                    }
                )
            except Exception as e:
                QgsMessageLog.logMessage(
                    f"Could not compute stats for band {band}: {e}", self.LOG_TAG, MSG_WARNING
                )
            nodata = dp.sourceNoDataValue(band)
            if nodata is not None:
                band_info["nodata"] = nodata
            info["bands"].append(band_info)

        return info

    def get_layer_info(self, layer_id, **kwargs):
        project = QgsProject.instance()
        if layer_id not in project.mapLayers():
            raise Exception(f"Layer not found: {layer_id}")

        layer = project.mapLayer(layer_id)
        extent = layer.extent()

        info = {
            "id": layer.id(),
            "name": layer.name(),
            "type": self._get_layer_type(layer),
            "crs": layer.crs().authid(),
            "extent": {
                "xmin": extent.xMinimum(),
                "ymin": extent.yMinimum(),
                "xmax": extent.xMaximum(),
                "ymax": extent.yMaximum(),
            },
            "source": layer.source(),
            "provider": layer.providerType(),
            "is_valid": layer.isValid(),
        }

        if layer.type() == LAYER_VECTOR:
            info["feature_count"] = layer.featureCount()
            info["geometry_type"] = layer.geometryType()
            info["fields"] = [
                {"name": f.name(), "type": f.typeName(), "length": f.length()}
                for f in layer.fields()
            ]
        elif layer.type() == LAYER_RASTER:
            info["width"] = layer.width()
            info["height"] = layer.height()
            info["band_count"] = layer.bandCount()

        return info

    def get_layer_schema(self, layer_id, **kwargs):
        project = QgsProject.instance()
        if layer_id not in project.mapLayers():
            raise Exception(f"Layer not found: {layer_id}")

        layer = project.mapLayer(layer_id)
        if layer.type() != LAYER_VECTOR:
            raise Exception(f"Layer is not a vector layer: {layer_id}")

        # Phase 1B: Stripped layer_id, layer_name
        return {
            "geometry_type": layer.geometryType(),
            "crs": layer.crs().authid(),
            "fields": [
                {
                    "name": f.name(),
                    "type": f.typeName(),
                    "length": f.length(),
                    "precision": f.precision(),
                    "is_numeric": f.isNumeric(),
                }
                for f in layer.fields()
            ],
        }

    def batch(self, commands, **kwargs):
        """Execute multiple commands in sequence, return array of results."""
        results = []
        for cmd in commands:
            cmd_type = cmd.get("type")
            params = cmd.get("params", {})
            result = self.execute_command({"type": cmd_type, "params": params})
            results.append(result)
        return results

    def execute_processing(self, algorithm, parameters, **kwargs):
        try:
            import processing

            QgsMessageLog.logMessage(f"Processing: {algorithm}", self.LOG_TAG, MSG_INFO)
            result = processing.run(algorithm, parameters)
            return {"algorithm": algorithm, "result": {k: str(v) for k, v in result.items()}}
        except Exception as e:
            raise Exception(f"Processing error: {e!s}") from e

    def save_project(self, path=None, **kwargs):
        project = QgsProject.instance()

        if not path and not project.fileName():
            raise Exception("No project path specified and no current project path")

        save_path = path if path else project.fileName()
        if project.write(save_path):
            QgsMessageLog.logMessage(f"Project saved: {save_path}", self.LOG_TAG, MSG_INFO)
            return {"saved": save_path}
        else:
            raise Exception(f"Failed to save project to {save_path}")

    def load_project(self, path, **kwargs):
        project = QgsProject.instance()
        if project.read(path):
            self.iface.mapCanvas().refresh()
            QgsMessageLog.logMessage(f"Project loaded: {path}", self.LOG_TAG, MSG_INFO)
            return {"loaded": path, "layer_count": len(project.mapLayers())}
        else:
            raise Exception(f"Failed to load project from {path}")

    def create_new_project(self, path, **kwargs):
        project = QgsProject.instance()
        if project.fileName():
            project.clear()
        project.setFileName(path)
        self.iface.mapCanvas().refresh()
        if project.write():
            QgsMessageLog.logMessage(f"Project created: {path}", self.LOG_TAG, MSG_INFO)
            return {
                "created": f"Project created and saved successfully at: {path}",
                "layer_count": len(project.mapLayers()),
            }
        else:
            raise Exception(f"Failed to save project to {path}")

    def render_map_base64(self, width=800, height=600, path=None, **kwargs):
        """Render the map and return base64-encoded PNG data."""
        try:
            ms = QgsMapSettings()
            layers = list(QgsProject.instance().mapLayers().values())
            ms.setLayers(layers)
            rect = self.iface.mapCanvas().extent()
            ms.setExtent(rect)
            ms.setOutputSize(QSize(width, height))
            ms.setBackgroundColor(QColor(255, 255, 255))
            ms.setOutputDpi(96)

            render = QgsMapRendererParallelJob(ms)
            render.start()
            render.waitForFinished()

            img = render.renderedImage()

            if path:
                img.save(path)

            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(IODEVICE_WRITEONLY)
            img.save(buf, "PNG")
            buf.close()
            b64 = base64.b64encode(bytes(ba)).decode("utf-8")

            return {"base64_data": b64, "mime_type": "image/png", "width": width, "height": height}

        except Exception as e:
            raise Exception(f"Render error: {e!s}") from e

    def render_layers_to_path(
        self,
        layer_ids,
        output_png,
        width=1600,
        height=1200,
        dpi=150,
        extent=None,
        background="white",
        **kwargs,
    ):
        """Render specified layers to a PNG file. Returns metadata only (no base64).

        v0.3 addition for qgis-mcp-north qgis_render_map. Differs from
        render_map_base64: explicit layer list, configurable DPI/background,
        path-only output, extent inferred from union of layer extents (5%
        padding) when not provided. No base64 in the response — DESIGN.md §5.
        """
        project = QgsProject.instance()
        layers = []
        for layer_id in layer_ids:
            if layer_id not in project.mapLayers():
                raise Exception(f"Layer not found: {layer_id}")
            layers.append(project.mapLayer(layer_id))
        if not layers:
            raise Exception("render_layers_to_path requires at least one layer")

        if extent is not None:
            rect = QgsRectangle(float(extent[0]), float(extent[1]), float(extent[2]), float(extent[3]))
        else:
            rect = QgsRectangle(layers[0].extent())
            for layer in layers[1:]:
                rect.combineExtentWith(layer.extent())
            dx = rect.width() * 0.05
            dy = rect.height() * 0.05
            rect = QgsRectangle(
                rect.xMinimum() - dx, rect.yMinimum() - dy,
                rect.xMaximum() + dx, rect.yMaximum() + dy,
            )

        ms = QgsMapSettings()
        # QGIS renders ms.setLayers in TOP→BOTTOM order; our spec is bottom→top.
        ms.setLayers(list(reversed(layers)))
        ms.setExtent(rect)
        ms.setOutputSize(QSize(int(width), int(height)))
        ms.setOutputDpi(int(dpi))
        ms.setDestinationCrs(layers[0].crs())

        color = QColor(background)
        if not color.isValid():
            color = QColor(255, 255, 255)
        ms.setBackgroundColor(color)

        render = QgsMapRendererParallelJob(ms)
        render.start()
        render.waitForFinished()
        img = render.renderedImage()
        if not img.save(output_png):
            raise Exception(f"Failed to save render to {output_png}")

        return {
            "output_path": output_png,
            "width": int(width),
            "height": int(height),
            "dpi": int(dpi),
            "extent": [rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()],
            "crs": layers[0].crs().authid(),
            "n_layers": len(layers),
        }

    _CLASSIFICATION_METHODS: ClassVar[dict] = {
        "quantile": QgsClassificationQuantile,
        "equal_interval": QgsClassificationEqualInterval,
        "natural_breaks": QgsClassificationJenks,
        "pretty": QgsClassificationPrettyBreaks,
    }

    def render_choropleth(
        self,
        zones_path,
        value_field,
        output_png,
        value_dict=None,
        join_field="zone_id",
        n_classes=5,
        mode="quantile",
        palette="YlOrRd",
        basemap_paths=None,
        width=1600,
        height=1200,
        dpi=150,
        background="white",
        **kwargs,
    ):
        """Render a zone-level choropleth in one call. v0.3 workflow primitive.

        Algorithm: load zones (transient) → memory-layer rebuild with optional
        CSV-merged value column → graduated style → render with optional
        basemaps → cleanup. The user's project sees no surviving state changes.
        """
        method_cls = self._CLASSIFICATION_METHODS.get(mode)
        if method_cls is None:
            raise Exception(
                f"Unknown mode {mode!r}. Use one of: {list(self._CLASSIFICATION_METHODS)}."
            )

        project = QgsProject.instance()
        transient_ids = []  # everything we need to remove on cleanup

        try:
            zones = QgsVectorLayer(zones_path, "_zones_in", "ogr")
            if not zones.isValid():
                raise Exception(f"Layer not found or invalid: {zones_path}")
            project.addMapLayer(zones)
            transient_ids.append(zones.id())

            if zones.geometryType() != GEOM_POLYGON:
                raise Exception(
                    f"render_choropleth requires polygon zones, got geometryType={zones.geometryType()}"
                )

            zone_field_names = [f.name() for f in zones.fields()]
            if value_dict is not None:
                if join_field not in zone_field_names:
                    raise Exception(
                        f"Field {join_field!r} not found on zones; available: {zone_field_names}"
                    )
            elif value_field not in zone_field_names:
                raise Exception(
                    f"Field {value_field!r} not found on zones; available: {zone_field_names}"
                )

            crs_authid = zones.crs().authid() or "EPSG:4326"
            uri = f"Polygon?crs={crs_authid}&field={join_field}:string&field={value_field}:double"
            mem = QgsVectorLayer(uri, "_choropleth", "memory")
            if not mem.isValid():
                raise Exception(f"Failed to create memory layer: {uri}")
            project.addMapLayer(mem)
            transient_ids.append(mem.id())

            mem_provider = mem.dataProvider()
            join_idx_mem = mem.fields().indexOf(join_field)
            value_idx_mem = mem.fields().indexOf(value_field)
            n_matched = 0
            n_unmatched = 0
            sample_layer_keys: list[str] = []
            new_features = []
            for f in zones.getFeatures():
                key = f.attribute(join_field) if value_dict is not None else None
                if value_dict is not None:
                    key_str = str(key) if key is not None else ""
                    if len(sample_layer_keys) < 5:
                        sample_layer_keys.append(key_str)
                    val = value_dict.get(key_str)
                    if val is None:
                        n_unmatched += 1
                    else:
                        n_matched += 1
                else:
                    key_str = ""
                    val = f.attribute(value_field)
                    n_matched += 1

                new_f = QgsFeature(mem.fields())
                new_f.setGeometry(QgsGeometry(f.geometry()))
                new_f.setAttribute(join_idx_mem, key_str)
                new_f.setAttribute(value_idx_mem, val if val is not None else None)
                new_features.append(new_f)

            ok, _ = mem_provider.addFeatures(new_features)
            if not ok:
                raise Exception("Failed to add features to memory layer")
            mem.updateExtents()

            if value_dict is not None and n_matched == 0:
                csv_sample = list(value_dict.keys())[:5]
                raise Exception(
                    f"JOIN_NO_MATCH on {join_field}: 0 matches. "
                    f"Sample CSV keys: {csv_sample}; sample layer keys: {sample_layer_keys}"
                )

            symbol = QgsSymbol.defaultSymbol(mem.geometryType())
            ramp = QgsStyle.defaultStyle().colorRamp(palette)
            if not ramp:
                ramp = QgsStyle.defaultStyle().colorRamp("Spectral")
            renderer = QgsGraduatedSymbolRenderer(value_field)
            renderer.setSourceSymbol(symbol.clone())
            renderer.setSourceColorRamp(ramp)
            renderer.setClassificationMethod(method_cls())
            renderer.updateClasses(mem, int(n_classes))
            mem.setRenderer(renderer)

            ranges = list(renderer.ranges())
            breaks: list[float] = []
            if ranges:
                breaks.append(float(ranges[0].lowerValue()))
                for r in ranges:
                    breaks.append(float(r.upperValue()))

            values_only = []
            for feat in mem.getFeatures():
                v = feat.attribute(value_field)
                if v is not None:
                    try:
                        values_only.append(float(v))
                    except (TypeError, ValueError):
                        pass
            min_value = min(values_only) if values_only else 0.0
            max_value = max(values_only) if values_only else 0.0

            basemap_layers = []
            for bm_path in basemap_paths or []:
                bm = QgsVectorLayer(bm_path, os.path.basename(bm_path), "ogr")
                if bm.isValid():
                    project.addMapLayer(bm)
                    transient_ids.append(bm.id())
                    basemap_layers.append(bm)
                else:
                    QgsMessageLog.logMessage(
                        f"Basemap skipped (invalid): {bm_path}", self.LOG_TAG, MSG_WARNING
                    )

            ordered_layers = [mem, *basemap_layers]  # top→bottom for setLayers

            extent_rect = QgsRectangle(mem.extent())
            for bm in basemap_layers:
                extent_rect.combineExtentWith(bm.extent())
            dx = extent_rect.width() * 0.05
            dy = extent_rect.height() * 0.05
            extent_rect = QgsRectangle(
                extent_rect.xMinimum() - dx, extent_rect.yMinimum() - dy,
                extent_rect.xMaximum() + dx, extent_rect.yMaximum() + dy,
            )

            ms = QgsMapSettings()
            ms.setLayers(ordered_layers)
            ms.setExtent(extent_rect)
            ms.setOutputSize(QSize(int(width), int(height)))
            ms.setOutputDpi(int(dpi))
            ms.setDestinationCrs(mem.crs())
            color = QColor(background)
            if not color.isValid():
                color = QColor(255, 255, 255)
            ms.setBackgroundColor(color)

            job = QgsMapRendererParallelJob(ms)
            job.start()
            job.waitForFinished()
            img = job.renderedImage()
            if not img.save(output_png):
                raise Exception(f"Failed to save render to {output_png}")

            return {
                "output_path": output_png,
                "width": int(width),
                "height": int(height),
                "dpi": int(dpi),
                "extent": [
                    extent_rect.xMinimum(), extent_rect.yMinimum(),
                    extent_rect.xMaximum(), extent_rect.yMaximum(),
                ],
                "crs": mem.crs().authid(),
                "n_layers": len(ordered_layers),
                "field": value_field,
                "n_classes": len(ranges),
                "breaks": breaks,
                "mode": mode,
                "min_value": min_value,
                "max_value": max_value,
                "n_features": len(values_only),
                "n_matched": n_matched,
                "n_unmatched": n_unmatched,
            }
        finally:
            for tid in transient_ids:
                try:
                    project.removeMapLayer(tid)
                except Exception:
                    pass

    # -----------------------------------------------------------------------
    # v0.5 workflow handlers — trajectory + OD flows
    # -----------------------------------------------------------------------

    def render_trajectory(
        self,
        output_png,
        render_mode="lines",
        features=None,
        input_path=None,
        basemap_paths=None,
        extent=None,
        width=1600,
        height=1200,
        dpi=150,
        mode_col=None,
        used_movingpandas=False,
        speed_field=None,
        background="white",
        **kwargs,
    ):
        """Render trajectories from a parsed feature list (CSV path) or a GPX file.

        ``features`` is a list of dicts ``{trip_id, lon, lat, datetime, mode?, speed_kmh?}``
        already sampled / clipped by the MCP layer. ``input_path`` is set instead when
        the source was a .gpx file; then we go through OGR directly.

        render_mode ∈ {"lines", "points", "heatmap"}.
        """
        project = QgsProject.instance()
        transient_ids = []

        try:
            if features is not None:
                traj_layer, n_trajectories = self._build_trajectory_layer(
                    features, render_mode, mode_col, speed_field
                )
            else:
                if not input_path:
                    raise Exception("render_trajectory: features or input_path required")
                traj_layer = QgsVectorLayer(input_path, "_traj_gpx", "ogr")
                if not traj_layer.isValid():
                    raise Exception(f"GPX layer not readable: {input_path}")
                n_trajectories = traj_layer.featureCount()

            project.addMapLayer(traj_layer)
            transient_ids.append(traj_layer.id())

            self._apply_trajectory_renderer(
                traj_layer, render_mode, mode_col, speed_field
            )

            basemap_layers = []
            for bm_path in basemap_paths or []:
                bm = QgsVectorLayer(bm_path, os.path.basename(bm_path), "ogr")
                if bm.isValid():
                    project.addMapLayer(bm)
                    transient_ids.append(bm.id())
                    basemap_layers.append(bm)
                else:
                    QgsMessageLog.logMessage(
                        f"Basemap skipped (invalid): {bm_path}", self.LOG_TAG, MSG_WARNING
                    )

            ordered_layers = [traj_layer, *basemap_layers]

            if extent is not None:
                xmin, ymin, xmax, ymax = extent
                extent_rect = QgsRectangle(xmin, ymin, xmax, ymax)
            else:
                extent_rect = QgsRectangle(traj_layer.extent())
                for bm in basemap_layers:
                    extent_rect.combineExtentWith(bm.extent())
                dx = extent_rect.width() * 0.05
                dy = extent_rect.height() * 0.05
                extent_rect = QgsRectangle(
                    extent_rect.xMinimum() - dx, extent_rect.yMinimum() - dy,
                    extent_rect.xMaximum() + dx, extent_rect.yMaximum() + dy,
                )

            ms = QgsMapSettings()
            ms.setLayers(ordered_layers)
            ms.setExtent(extent_rect)
            ms.setOutputSize(QSize(int(width), int(height)))
            ms.setOutputDpi(int(dpi))
            ms.setDestinationCrs(traj_layer.crs())
            color = QColor(background)
            if not color.isValid():
                color = QColor(255, 255, 255)
            ms.setBackgroundColor(color)

            job = QgsMapRendererParallelJob(ms)
            job.start()
            job.waitForFinished()
            img = job.renderedImage()
            if not img.save(output_png):
                raise Exception(f"Failed to save render to {output_png}")

            n_points_rendered = (
                len(features) if features is not None
                else traj_layer.featureCount()
            )
            modes = sorted({f.get("mode") for f in (features or []) if f.get("mode")}) or None

            return {
                "output_path": output_png,
                "width": int(width),
                "height": int(height),
                "dpi": int(dpi),
                "extent": [
                    extent_rect.xMinimum(), extent_rect.yMinimum(),
                    extent_rect.xMaximum(), extent_rect.yMaximum(),
                ],
                "crs": traj_layer.crs().authid() or "EPSG:4326",
                "n_layers": len(ordered_layers),
                "n_trajectories": int(n_trajectories),
                "n_points_total": int(n_points_rendered),
                "n_points_rendered": int(n_points_rendered),
                "downsampled": False,
                "time_range": None,
                "modes": modes,
                "used_movingpandas": bool(used_movingpandas),
            }
        finally:
            for tid in transient_ids:
                try:
                    project.removeMapLayer(tid)
                except Exception:
                    pass

    def _build_trajectory_layer(self, features, render_mode, mode_col, speed_field):
        """Build a transient point or line memory layer from the features list.

        For lines mode, groups by trip_id and emits one polyline per trajectory.
        For points/heatmap, emits one point per feature.

        Returns (layer, n_trajectories).
        """
        if render_mode == "lines":
            fields = "field=trip_id:string"
            if speed_field:
                fields += "&field=speed_kmh:double"
            if mode_col:
                fields += "&field=mode:string"
            uri = f"LineString?crs=EPSG:4326&{fields}"
            mem = QgsVectorLayer(uri, "_trajectory", "memory")
            if not mem.isValid():
                raise Exception(f"Failed to create memory layer: {uri}")
            provider = mem.dataProvider()

            # Group features by trip_id, preserving order.
            buckets: dict[str, list[dict]] = {}
            order: list[str] = []
            for f in features:
                tid = str(f["trip_id"])
                if tid not in buckets:
                    buckets[tid] = []
                    order.append(tid)
                buckets[tid].append(f)

            new_features = []
            for tid in order:
                points = [QgsPointXY(float(p["lon"]), float(p["lat"])) for p in buckets[tid]]
                if len(points) < 2:
                    continue  # need at least 2 points for a line
                feat = QgsFeature(mem.fields())
                feat.setGeometry(QgsGeometry.fromPolylineXY(points))
                attrs = [tid]
                if speed_field:
                    # Average speed across the trajectory's points; per-segment
                    # rendering would need cutting into segments, which we defer.
                    speeds = [float(p.get("speed_kmh", 0.0)) for p in buckets[tid]]
                    attrs.append(sum(speeds) / max(1, len(speeds)))
                if mode_col:
                    # Most common mode in the trajectory
                    modes = [p.get("mode", "") for p in buckets[tid]]
                    attrs.append(max(set(modes), key=modes.count) if modes else "")
                feat.setAttributes(attrs)
                new_features.append(feat)
            provider.addFeatures(new_features)
            mem.updateExtents()
            return mem, len(order)

        # points / heatmap → one point per feature
        fields = "field=trip_id:string"
        if mode_col:
            fields += "&field=mode:string"
        uri = f"Point?crs=EPSG:4326&{fields}"
        mem = QgsVectorLayer(uri, "_trajectory", "memory")
        if not mem.isValid():
            raise Exception(f"Failed to create memory layer: {uri}")
        provider = mem.dataProvider()
        new_features = []
        seen_trips = set()
        for p in features:
            seen_trips.add(str(p["trip_id"]))
            feat = QgsFeature(mem.fields())
            feat.setGeometry(QgsGeometry.fromPointXY(
                QgsPointXY(float(p["lon"]), float(p["lat"]))
            ))
            attrs = [str(p["trip_id"])]
            if mode_col:
                attrs.append(p.get("mode", ""))
            feat.setAttributes(attrs)
            new_features.append(feat)
        provider.addFeatures(new_features)
        mem.updateExtents()
        return mem, len(seen_trips)

    def _apply_trajectory_renderer(self, layer, render_mode, mode_col, speed_field):
        """Attach a renderer suited to the chosen mode + available fields."""
        if render_mode == "heatmap":
            renderer = QgsHeatmapRenderer()
            renderer.setRadius(15.0)
            renderer.setRadiusUnit(Qgis.RenderUnit.Pixels if hasattr(Qgis, "RenderUnit") else 1)
            ramp = QgsStyle.defaultStyle().colorRamp("Spectral")
            if ramp:
                inverted = ramp.clone()
                try:
                    inverted.invert()
                except Exception:
                    pass
                renderer.setColorRamp(inverted)
            layer.setRenderer(renderer)
            return

        if render_mode == "lines":
            if speed_field:
                symbol = QgsLineSymbol.createSimple({"line_color": "#1f78b4", "line_width": "0.6"})
                ramp = QgsStyle.defaultStyle().colorRamp("Spectral")
                if not ramp:
                    ramp = QgsStyle.defaultStyle().colorRamp("Viridis")
                renderer = QgsGraduatedSymbolRenderer("speed_kmh")
                renderer.setSourceSymbol(symbol.clone())
                renderer.setSourceColorRamp(ramp)
                renderer.setClassificationMethod(QgsClassificationQuantile())
                renderer.updateClasses(layer, 5)
                layer.setRenderer(renderer)
                return
            if mode_col:
                self._apply_categorical_by_mode(layer, "mode")
                return
            symbol = QgsLineSymbol.createSimple({"line_color": "#1f78b4", "line_width": "0.6"})
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
            return

        # points mode
        if mode_col:
            self._apply_categorical_by_mode(layer, "mode")
            return
        symbol = QgsMarkerSymbol.createSimple(
            {"name": "circle", "color": "#1f78b4", "size": "1.5", "outline_style": "no"}
        )
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))

    def _apply_categorical_by_mode(self, layer, field):
        """Build a categorized renderer one color per unique value of field."""
        unique = sorted({str(f.attribute(field)) for f in layer.getFeatures()})
        ramp = QgsStyle.defaultStyle().colorRamp("Set2")
        categories = []
        for i, val in enumerate(unique):
            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            if ramp:
                color = ramp.color(i / max(1, len(unique) - 1)) if len(unique) > 1 else ramp.color(0.5)
                symbol.setColor(color)
            categories.append(QgsRendererCategory(val, symbol, val))
        renderer = QgsCategorizedSymbolRenderer(field, categories)
        layer.setRenderer(renderer)

    def render_od_flows(
        self,
        output_png,
        zones_path,
        zone_id_field="zone_id",
        flows=None,
        basemap_paths=None,
        width=1600,
        height=1200,
        dpi=150,
        background="white",
        **kwargs,
    ):
        """Render origin-destination arcs over a polygon zones layer.

        ``flows`` is a list of dicts ``{origin, destination, value}`` already sorted /
        truncated by the MCP layer. Centroid lookup happens here so we don't ship
        polygon geometries over the socket.
        """
        if flows is None:
            raise Exception("render_od_flows: flows list required")

        project = QgsProject.instance()
        transient_ids = []

        try:
            zones = QgsVectorLayer(zones_path, "_od_zones", "ogr")
            if not zones.isValid():
                raise Exception(f"Zones layer not readable: {zones_path}")
            project.addMapLayer(zones)
            transient_ids.append(zones.id())

            zone_field_names = [f.name() for f in zones.fields()]
            if zone_id_field not in zone_field_names:
                raise Exception(
                    f"Field {zone_id_field!r} not found on zones; available: {zone_field_names}"
                )

            centroids: dict[str, QgsPointXY] = {}
            for f in zones.getFeatures():
                key = str(f.attribute(zone_id_field))
                centroid = f.geometry().centroid().asPoint()
                centroids[key] = QgsPointXY(centroid.x(), centroid.y())

            crs_authid = zones.crs().authid() or "EPSG:4326"
            uri = (
                f"LineString?crs={crs_authid}"
                "&field=origin:string&field=destination:string&field=trip_count:double"
            )
            mem = QgsVectorLayer(uri, "_od_flows", "memory")
            if not mem.isValid():
                raise Exception(f"Failed to create memory layer: {uri}")
            project.addMapLayer(mem)
            transient_ids.append(mem.id())

            unmatched_o = 0
            unmatched_d = 0
            rendered_flows = []
            for flow in flows:
                o_key = str(flow["origin"])
                d_key = str(flow["destination"])
                o_pt = centroids.get(o_key)
                d_pt = centroids.get(d_key)
                if o_pt is None:
                    unmatched_o += 1
                    continue
                if d_pt is None:
                    unmatched_d += 1
                    continue
                rendered_flows.append((o_key, d_key, float(flow["value"]), o_pt, d_pt))

            new_features = []
            max_flow = max((f[2] for f in rendered_flows), default=1.0)
            min_flow = min((f[2] for f in rendered_flows), default=0.0)
            for o, d, v, op, dp in rendered_flows:
                feat = QgsFeature(mem.fields())
                feat.setGeometry(QgsGeometry.fromPolylineXY([op, dp]))
                feat.setAttributes([o, d, v])
                new_features.append(feat)
            mem.dataProvider().addFeatures(new_features)
            mem.updateExtents()

            # Line symbol with data-defined stroke width based on trip_count.
            symbol = QgsLineSymbol.createSimple({"line_color": "#1f78b4", "line_width": "0.4"})
            symbol_layer = symbol.symbolLayer(0)
            width_expr = f'"trip_count" / {max(max_flow, 1e-9)} * 4.0 + 0.3'
            try:
                symbol_layer.setDataDefinedProperty(
                    QgsSymbolLayer.PropertyStrokeWidth,
                    QgsProperty.fromExpression(width_expr),
                )
            except Exception:
                # Older QGIS API surface; fall back to a uniform medium width.
                QgsMessageLog.logMessage(
                    "data-defined stroke width unavailable; using uniform 1.0",
                    self.LOG_TAG, MSG_WARNING,
                )
            mem.setRenderer(QgsSingleSymbolRenderer(symbol))

            basemap_layers = []
            for bm_path in basemap_paths or []:
                bm = QgsVectorLayer(bm_path, os.path.basename(bm_path), "ogr")
                if bm.isValid():
                    project.addMapLayer(bm)
                    transient_ids.append(bm.id())
                    basemap_layers.append(bm)

            ordered_layers = [mem, zones, *basemap_layers]

            extent_rect = QgsRectangle(zones.extent())
            dx = extent_rect.width() * 0.05
            dy = extent_rect.height() * 0.05
            extent_rect = QgsRectangle(
                extent_rect.xMinimum() - dx, extent_rect.yMinimum() - dy,
                extent_rect.xMaximum() + dx, extent_rect.yMaximum() + dy,
            )

            ms = QgsMapSettings()
            ms.setLayers(ordered_layers)
            ms.setExtent(extent_rect)
            ms.setOutputSize(QSize(int(width), int(height)))
            ms.setOutputDpi(int(dpi))
            ms.setDestinationCrs(zones.crs())
            color = QColor(background)
            if not color.isValid():
                color = QColor(255, 255, 255)
            ms.setBackgroundColor(color)

            job = QgsMapRendererParallelJob(ms)
            job.start()
            job.waitForFinished()
            img = job.renderedImage()
            if not img.save(output_png):
                raise Exception(f"Failed to save render to {output_png}")

            return {
                "output_path": output_png,
                "width": int(width),
                "height": int(height),
                "dpi": int(dpi),
                "extent": [
                    extent_rect.xMinimum(), extent_rect.yMinimum(),
                    extent_rect.xMaximum(), extent_rect.yMaximum(),
                ],
                "crs": zones.crs().authid() or "EPSG:4326",
                "n_layers": len(ordered_layers),
                "n_flows": len(flows),
                "n_flows_rendered": len(rendered_flows),
                "n_zones": len(centroids),
                "max_flow": float(max_flow) if rendered_flows else 0.0,
                "min_flow_rendered": float(min_flow) if rendered_flows else 0.0,
                "n_unmatched_origins": unmatched_o,
                "n_unmatched_destinations": unmatched_d,
            }
        finally:
            for tid in transient_ids:
                try:
                    project.removeMapLayer(tid)
                except Exception:
                    pass

    # -----------------------------------------------------------------------
    # Phase 2 new handlers
    # -----------------------------------------------------------------------

    def _get_vector_layer(self, layer_id):
        """Helper: get a vector layer or raise."""
        project = QgsProject.instance()
        if layer_id not in project.mapLayers():
            raise Exception(f"Layer not found: {layer_id}")
        layer = project.mapLayer(layer_id)
        if layer.type() != LAYER_VECTOR:
            raise Exception(f"Not a vector layer: {layer_id}")
        return layer

    def add_features(self, layer_id, features, **kwargs):
        layer = self._get_vector_layer(layer_id)
        dp = layer.dataProvider()
        qgs_features = []
        for feat_data in features:
            f = QgsFeature(layer.fields())
            attrs = feat_data.get("attributes", {})
            for field_name, value in attrs.items():
                idx = layer.fields().indexOf(field_name)
                if idx >= 0:
                    f.setAttribute(idx, value)
            wkt = feat_data.get("geometry_wkt")
            if wkt:
                f.setGeometry(QgsGeometry.fromWkt(wkt))
            qgs_features.append(f)

        ok, added = dp.addFeatures(qgs_features)
        if not ok:
            raise Exception("Failed to add features")
        layer.updateExtents()
        return {"added": len(added)}

    def update_features(self, layer_id, updates, **kwargs):
        layer = self._get_vector_layer(layer_id)
        dp = layer.dataProvider()
        attr_map = {}
        for upd in updates:
            fid = upd["fid"]
            attrs = upd.get("attributes", {})
            field_map = {}
            for field_name, value in attrs.items():
                idx = layer.fields().indexOf(field_name)
                if idx >= 0:
                    field_map[idx] = value
            if field_map:
                attr_map[fid] = field_map

        if attr_map:
            ok = dp.changeAttributeValues(attr_map)
            if not ok:
                raise Exception("Failed to update features")
        return {"updated": len(attr_map)}

    def delete_features(self, layer_id, fids=None, expression=None, **kwargs):
        layer = self._get_vector_layer(layer_id)
        dp = layer.dataProvider()

        if fids is not None:
            target_fids = fids
        elif expression:
            request = QgsFeatureRequest().setFilterExpression(expression)
            request.setNoAttributes()
            target_fids = [f.id() for f in layer.getFeatures(request)]
        else:
            raise Exception("Either fids or expression must be provided")

        ok = dp.deleteFeatures(target_fids)
        if not ok:
            raise Exception("Failed to delete features")
        layer.updateExtents()
        return {"deleted": len(target_fids)}

    def set_layer_style(
        self,
        layer_id,
        style_type,
        field=None,
        classes=5,
        color_ramp="Spectral",
        mode="equal_interval",
        **kwargs,
    ):
        """Apply categorical / graduated / single-symbol style to a vector layer.

        v0.5 augmentation: returns rich response shapes matching DESIGN.md §4 for
        both `categorized` (n_classes + per-class color + n_features) and
        `graduated` (breaks + mode). `mode` arg honors {quantile, equal_interval,
        natural_breaks, pretty} for graduated style.
        """
        layer = self._get_vector_layer(layer_id)

        if style_type == "single":
            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            renderer = QgsSingleSymbolRenderer(symbol)
            layer.setRenderer(renderer)
            response = {"ok": True, "n_classes": 1, "classes": []}

        elif style_type == "categorized":
            if not field:
                raise Exception("field is required for categorized style")
            idx = layer.fields().indexOf(field)
            if idx < 0:
                raise Exception(f"Field not found: {field}")

            unique_values = sorted(
                layer.uniqueValues(idx), key=lambda x: str(x) if x is not None else ""
            )
            ramp = QgsStyle.defaultStyle().colorRamp(color_ramp)
            if not ramp:
                ramp = QgsStyle.defaultStyle().colorRamp("Spectral")

            # Count features per value in a single pass (cheap for typical N).
            counts: dict = {}
            for f in layer.getFeatures():
                v = f.attribute(field)
                counts[v] = counts.get(v, 0) + 1

            categories = []
            class_entries = []
            n = max(len(unique_values) - 1, 1)
            for i, value in enumerate(unique_values):
                symbol = QgsSymbol.defaultSymbol(layer.geometryType())
                color = ramp.color(i / n)
                symbol.setColor(color)
                label = str(value) if value is not None else "NULL"
                categories.append(QgsRendererCategory(value, symbol, label))
                class_entries.append({
                    "value": label,
                    "color": color.name(),  # hex string
                    "n_features": int(counts.get(value, 0)),
                })

            renderer = QgsCategorizedSymbolRenderer(field, categories)
            layer.setRenderer(renderer)
            response = {
                "ok": True,
                "n_classes": len(class_entries),
                "classes": class_entries,
            }

        elif style_type == "graduated":
            if not field:
                raise Exception("field is required for graduated style")
            idx = layer.fields().indexOf(field)
            if idx < 0:
                raise Exception(f"Field not found: {field}")

            method_cls = self._CLASSIFICATION_METHODS.get(mode)
            if method_cls is None:
                raise Exception(
                    f"Unknown mode {mode!r}. Use one of: {list(self._CLASSIFICATION_METHODS)}."
                )

            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            ramp = QgsStyle.defaultStyle().colorRamp(color_ramp)
            if not ramp:
                ramp = QgsStyle.defaultStyle().colorRamp("Spectral")

            renderer = QgsGraduatedSymbolRenderer(field)
            renderer.setSourceSymbol(symbol.clone())
            renderer.setSourceColorRamp(ramp)
            renderer.setClassificationMethod(method_cls())
            renderer.updateClasses(layer, int(classes))
            layer.setRenderer(renderer)

            ranges = list(renderer.ranges())
            breaks = []
            if ranges:
                breaks.append(float(ranges[0].lowerValue()))
                for r in ranges:
                    breaks.append(float(r.upperValue()))

            # Per-range feature counts via in-memory iteration (no QGIS API for this).
            class_entries = []
            for r in ranges:
                lo, hi = r.lowerValue(), r.upperValue()
                n_in = 0
                for f in layer.getFeatures():
                    val = f.attribute(field)
                    try:
                        v = float(val)
                    except (TypeError, ValueError):
                        continue
                    if lo <= v <= hi:
                        n_in += 1
                color = r.symbol().color().name() if r.symbol() else "#000000"
                class_entries.append({
                    "value": f"{lo}-{hi}",
                    "color": color,
                    "n_features": int(n_in),
                })

            response = {
                "ok": True,
                "n_classes": len(class_entries),
                "classes": class_entries,
                "breaks": breaks,
                "mode": mode,
            }
        else:
            raise Exception(
                f"Unknown style_type: {style_type}. Use 'single', 'categorized', or 'graduated'"
            )

        layer.triggerRepaint()
        try:
            if self.iface is not None and hasattr(self.iface, "layerTreeView"):
                self.iface.layerTreeView().refreshLayerSymbology(layer.id())
        except Exception:
            pass  # headless stub iface — no UI to refresh
        return response

    def select_features(self, layer_id, expression=None, fids=None, **kwargs):
        layer = self._get_vector_layer(layer_id)

        if fids is not None:
            layer.selectByIds(fids)
        elif expression:
            layer.selectByExpression(expression)
        else:
            raise Exception("Either fids or expression must be provided")

        return {"selected": layer.selectedFeatureCount()}

    def get_selection(self, layer_id, **kwargs):
        layer = self._get_vector_layer(layer_id)
        return {
            "fids": list(layer.selectedFeatureIds()),
            "count": layer.selectedFeatureCount(),
        }

    def clear_selection(self, layer_id, **kwargs):
        layer = self._get_vector_layer(layer_id)
        layer.removeSelection()
        return {"ok": True}

    def create_memory_layer(self, name, geometry_type, crs="EPSG:4326", fields=None, **kwargs):
        field_parts = []
        if fields:
            for f in fields:
                field_parts.append(f"field={f['name']}:{f['type']}")

        uri = f"{geometry_type}?crs={crs}"
        if field_parts:
            uri += "&" + "&".join(field_parts)

        layer = QgsVectorLayer(uri, name, "memory")
        if not layer.isValid():
            raise Exception(f"Failed to create memory layer: {uri}")

        QgsProject.instance().addMapLayer(layer)
        return {
            "id": layer.id(),
            "name": layer.name(),
            "type": self._get_layer_type(layer),
            "feature_count": 0,
        }

    def list_processing_algorithms(self, search=None, provider=None, **kwargs):
        registry = QgsApplication.processingRegistry()
        algorithms = []

        for alg in registry.algorithms():
            if provider and alg.provider().id() != provider:
                continue
            if search:
                search_lower = search.lower()
                if (
                    search_lower not in alg.id().lower()
                    and search_lower not in alg.displayName().lower()
                ):
                    continue
            algorithms.append(
                {
                    "id": alg.id(),
                    "name": alg.displayName(),
                    "provider": alg.provider().id(),
                }
            )

        return {"algorithms": algorithms, "count": len(algorithms)}

    def get_algorithm_help(self, algorithm_id, **kwargs):
        registry = QgsApplication.processingRegistry()
        alg = registry.algorithmById(algorithm_id)
        if not alg:
            raise Exception(f"Algorithm not found: {algorithm_id}")

        params = []
        for param in alg.parameterDefinitions():
            param_info = {
                "name": param.name(),
                "description": param.description(),
                "type": param.type(),
                "optional": bool(param.flags() & PROCESSING_OPTIONAL),
            }
            try:
                default = param.defaultValue()
                if default is not None:
                    param_info["default"] = str(default)
            except Exception:
                pass
            params.append(param_info)

        outputs = []
        for out in alg.outputDefinitions():
            outputs.append(
                {
                    "name": out.name(),
                    "description": out.description(),
                    "type": out.type(),
                }
            )

        return {
            "id": alg.id(),
            "name": alg.displayName(),
            "description": alg.shortDescription() or "",
            "provider": alg.provider().id(),
            "parameters": params,
            "outputs": outputs,
        }

    def find_layer(self, name_pattern, **kwargs):
        project = QgsProject.instance()
        matches = []
        pattern_lower = name_pattern.lower()
        for layer_id, layer in project.mapLayers().items():
            name_lower = layer.name().lower()
            if fnmatch.fnmatch(name_lower, pattern_lower) or pattern_lower in name_lower:
                matches.append(
                    {
                        "id": layer_id,
                        "name": layer.name(),
                        "type": self._get_layer_type(layer),
                    }
                )
        return {"layers": matches, "count": len(matches)}

    def list_layouts(self, **kwargs):
        manager = QgsProject.instance().layoutManager()
        layouts = []
        for layout in manager.layouts():
            layouts.append(
                {
                    "name": layout.name(),
                    "page_count": layout.pageCollection().pageCount(),
                }
            )
        return {"layouts": layouts, "count": len(layouts)}

    def export_layout(
        self,
        layout_name,
        format="pdf",
        dpi=300,
        qgz_path=None,
        output_path=None,
        path=None,
        **kwargs,
    ):
        """Export a print-composer layout. v0.5 signature accepts qgz_path + output_path;
        legacy upstream signature used positional ``path``. We support both for
        backward compatibility with existing callers.
        """
        if qgz_path:
            project = QgsProject.instance()
            current = project.fileName()
            if current != qgz_path:
                project.clear()
                if not project.read(qgz_path):
                    raise Exception(f"Failed to load project from {qgz_path}")

        out = output_path or path
        if not out:
            raise Exception("export_layout: output_path required")

        manager = QgsProject.instance().layoutManager()
        layout = manager.layoutByName(layout_name)
        if not layout:
            available = [lo.name() for lo in manager.layouts()]
            raise Exception(
                f"LAYOUT_NOT_FOUND: {layout_name!r}. Available: {available}"
            )

        exporter = QgsLayoutExporter(layout)
        fmt = format.lower()

        if fmt == "pdf":
            settings = QgsLayoutExporter.PdfExportSettings()
            settings.dpi = dpi
            result = exporter.exportToPdf(out, settings)
        elif fmt in ("png", "jpg", "jpeg", "tif", "tiff", "bmp"):
            settings = QgsLayoutExporter.ImageExportSettings()
            settings.dpi = dpi
            result = exporter.exportToImage(out, settings)
        elif fmt == "svg":
            settings = QgsLayoutExporter.SvgExportSettings()
            settings.dpi = dpi
            result = exporter.exportToSvg(out, settings)
        else:
            raise Exception(f"Unsupported format: {format}")

        if result != LAYOUT_SUCCESS:
            raise Exception(f"Export failed with code: {result}")

        n_pages = 1
        try:
            n_pages = layout.pageCollection().pageCount()
        except Exception:
            pass

        return {
            "ok": True,
            "path": out,
            "output_path": out,
            "format": fmt,
            "n_pages": int(n_pages),
            "layout_name": layout_name,
        }

    def project_load(self, qgz_path, **kwargs):
        """Load a saved .qgz/.qgs and return its layers + layouts in one call.

        Stateful — leaves the project loaded so subsequent export_layout or
        batch_render see the same project state.
        """
        project = QgsProject.instance()
        project.clear()
        if not project.read(qgz_path):
            raise Exception(f"Failed to read project: {qgz_path}")

        try:
            if self.iface is not None and hasattr(self.iface, "mapCanvas"):
                self.iface.mapCanvas().refresh()
        except Exception:
            pass

        layers_info = []
        layer_tree = project.layerTreeRoot()
        for layer_id, layer in project.mapLayers().items():
            geom_type = self._get_layer_type(layer)
            visible = True
            try:
                node = layer_tree.findLayer(layer_id)
                if node is not None:
                    visible = bool(node.isVisible())
            except Exception:
                pass
            # Translate plugin's "vector_{0,1,2}" → DESIGN-doc geometry enum.
            if geom_type == "raster":
                geom_out = "raster"
            elif geom_type.startswith("vector_"):
                idx = geom_type.split("_", 1)[1]
                geom_out = {"0": "point", "1": "line", "2": "polygon"}.get(idx, "no_geom")
            else:
                geom_out = "no_geom"
            layers_info.append(
                {
                    "layer_id": layer_id,
                    "name": layer.name(),
                    "geometry_type": geom_out,
                    "visible": visible,
                }
            )

        layouts = [{"name": lo.name()} for lo in project.layoutManager().layouts()]

        extent = QgsRectangle()
        layers = list(project.mapLayers().values())
        if layers:
            extent = QgsRectangle(layers[0].extent())
            for la in layers[1:]:
                extent.combineExtentWith(la.extent())

        return {
            "project_path": qgz_path,
            "crs": project.crs().authid() or "EPSG:4326",
            "extent": [
                extent.xMinimum(), extent.yMinimum(),
                extent.xMaximum(), extent.yMaximum(),
            ],
            "layers": layers_info,
            "layouts": layouts,
        }

    def batch_render(
        self,
        template_qgz,
        attribute,
        values,
        output_dir,
        layout_name=None,
        filename_template="{value}.png",
        width=1600,
        height=1200,
        dpi=150,
        background="white",
        **kwargs,
    ):
        """Fan-out: open template_qgz, iterate values, filter the active layer,
        render each to output_dir. Returns a manifest + errors list.

        Active-layer convention: project's saved active layer if set; else the
        first vector layer. Documented loudly in the MCP tool docstring.
        """
        project = QgsProject.instance()
        if project.fileName() != template_qgz:
            project.clear()
            if not project.read(template_qgz):
                raise Exception(f"Failed to read template: {template_qgz}")

        # Resolve target layer.
        target = None
        try:
            target = project.mapLayer(project.readPath("ActiveLayerID"))
        except Exception:
            target = None
        if target is None or target.type() != LAYER_VECTOR:
            for la in project.mapLayers().values():
                if la.type() == LAYER_VECTOR:
                    target = la
                    QgsMessageLog.logMessage(
                        f"batch_render: using first vector layer as filter target: {la.name()}",
                        self.LOG_TAG, MSG_INFO,
                    )
                    break
        if target is None:
            raise Exception("batch_render: no vector layer found in template")

        field_names = [f.name() for f in target.fields()]
        if attribute not in field_names:
            raise Exception(
                f"FIELD_NOT_FOUND: {attribute!r}. Available: {field_names}"
            )

        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception as err:
            raise Exception(f"Failed to create output_dir: {err}") from err

        manifest = []
        errors = []

        try:
            for value in values:
                safe_value = str(value).replace("'", "''")
                expr = f'"{attribute}" = \'{safe_value}\''
                try:
                    target.setSubsetString(expr)
                    if target.featureCount() == 0:
                        errors.append({"value": str(value), "error": "No features match filter"})
                        continue

                    filename = filename_template.format(value=value)
                    out_path = os.path.join(output_dir, filename)

                    extent_rect = QgsRectangle(target.extent())
                    dx = extent_rect.width() * 0.05
                    dy = extent_rect.height() * 0.05
                    extent_rect = QgsRectangle(
                        extent_rect.xMinimum() - dx, extent_rect.yMinimum() - dy,
                        extent_rect.xMaximum() + dx, extent_rect.yMaximum() + dy,
                    )

                    if layout_name:
                        layout = project.layoutManager().layoutByName(layout_name)
                        if not layout:
                            available = [lo.name() for lo in project.layoutManager().layouts()]
                            raise Exception(
                                f"LAYOUT_NOT_FOUND: {layout_name!r}. Available: {available}"
                            )
                        exporter = QgsLayoutExporter(layout)
                        settings = QgsLayoutExporter.ImageExportSettings()
                        settings.dpi = dpi
                        result = exporter.exportToImage(out_path, settings)
                        if result != LAYOUT_SUCCESS:
                            errors.append({"value": str(value), "error": f"Export code {result}"})
                            continue
                    else:
                        ms = QgsMapSettings()
                        ms.setLayers(list(project.mapLayers().values()))
                        ms.setExtent(extent_rect)
                        ms.setOutputSize(QSize(int(width), int(height)))
                        ms.setOutputDpi(int(dpi))
                        ms.setDestinationCrs(target.crs())
                        color = QColor(background)
                        if not color.isValid():
                            color = QColor(255, 255, 255)
                        ms.setBackgroundColor(color)

                        job = QgsMapRendererParallelJob(ms)
                        job.start()
                        job.waitForFinished()
                        img = job.renderedImage()
                        if not img.save(out_path):
                            errors.append({"value": str(value), "error": "Image save failed"})
                            continue

                    manifest.append(
                        {
                            "value": str(value),
                            "output_path": out_path,
                            "extent": [
                                extent_rect.xMinimum(), extent_rect.yMinimum(),
                                extent_rect.xMaximum(), extent_rect.yMaximum(),
                            ],
                        }
                    )
                except Exception as err:
                    errors.append({"value": str(value), "error": str(err)})
        finally:
            try:
                target.setSubsetString("")
            except Exception:
                pass

        return {
            "output_dir": output_dir,
            "n_rendered": len(manifest),
            "manifest": manifest,
            "errors": errors,
        }

    # -----------------------------------------------------------------------
    # Phase 3 — Plugin development & system management handlers
    # -----------------------------------------------------------------------

    _LEVEL_MAP: ClassVar[dict[int, str]] = {0: "info", 1: "warning", 2: "critical", 3: "success"}

    def _capture_message(self, message, tag, level, *_extra):
        """Capture a message log entry into the deque."""
        self._message_log.append(
            {
                "tag": tag,
                "message": message,
                "level": self._LEVEL_MAP.get(level, str(level)),
                "timestamp": datetime.now(tz=UTC).isoformat(),
            }
        )

    def get_message_log(self, level=None, tag=None, limit=100, **kwargs):
        entries = list(self._message_log)
        entries.reverse()  # newest first
        if level:
            entries = [e for e in entries if e["level"] == level]
        if tag:
            entries = [e for e in entries if e["tag"] == tag]
        entries = entries[:limit]
        return {"messages": entries, "count": len(entries)}

    def list_plugins(self, enabled_only=False, **kwargs):
        result = []
        names = list(active_plugins) if enabled_only else list(available_plugins)
        for name in sorted(names):
            result.append(
                {
                    "name": name,
                    "enabled": name in active_plugins,
                    "version": pluginMetadata(name, "version") or "",
                    "path": pluginMetadata(name, "path") or "",
                }
            )
        return {"plugins": result, "count": len(result)}

    def get_plugin_info(self, plugin_name, **kwargs):
        if plugin_name not in available_plugins and plugin_name not in active_plugins:
            raise Exception(f"Plugin not found: {plugin_name}")
        return {
            "name": plugin_name,
            "enabled": plugin_name in active_plugins,
            "version": pluginMetadata(plugin_name, "version") or "",
            "description": pluginMetadata(plugin_name, "description") or "",
            "author": pluginMetadata(plugin_name, "author") or "",
            "path": pluginMetadata(plugin_name, "path") or "",
        }

    def reload_plugin(self, plugin_name, **kwargs):
        if plugin_name == "qgis_mcp_plugin":
            raise Exception("Cannot reload MCP plugin (would break the connection)")
        if plugin_name not in active_plugins:
            raise Exception(f"Plugin not active: {plugin_name}")
        reloadPlugin(plugin_name)
        return {"reloaded": plugin_name, "ok": True}

    def _layer_tree_node(self, node):
        """Recursively build a dict for a layer tree node."""
        if isinstance(node, QgsLayerTreeGroup):
            children = [self._layer_tree_node(c) for c in node.children()]
            result = {
                "type": "group",
                "name": node.name(),
                "visible": node.isVisible(),
                "children": children,
            }
            return result
        elif isinstance(node, QgsLayerTreeLayer):
            layer = node.layer()
            result = {
                "type": "layer",
                "name": node.name(),
                "visible": node.isVisible(),
            }
            if layer:
                result["layer_id"] = layer.id()
                result["layer_type"] = self._get_layer_type(layer)
            return result
        return {"type": "unknown", "name": str(node)}

    def get_layer_tree(self, **kwargs):
        root = QgsProject.instance().layerTreeRoot()
        children = [self._layer_tree_node(c) for c in root.children()]
        return {"children": children}

    def create_layer_group(self, name, parent=None, **kwargs):
        root = QgsProject.instance().layerTreeRoot()
        if parent:
            target = root.findGroup(parent)
            if target is None:
                raise Exception(f"Parent group not found: {parent}")
        else:
            target = root
        target.addGroup(name)
        return {"name": name, "ok": True}

    def move_layer_to_group(self, layer_id, group_name, **kwargs):
        project = QgsProject.instance()
        root = project.layerTreeRoot()

        node = root.findLayer(layer_id)
        if node is None:
            raise Exception(f"Layer not found in tree: {layer_id}")

        target = root.findGroup(group_name)
        if target is None:
            raise Exception(f"Group not found: {group_name}")

        clone = node.clone()
        target.addChildNode(clone)
        node.parent().removeChildNode(node)
        return {"ok": True}

    def set_layer_property(self, layer_id, property, value, **kwargs):
        project = QgsProject.instance()
        if layer_id not in project.mapLayers():
            raise Exception(f"Layer not found: {layer_id}")

        layer = project.mapLayer(layer_id)

        if property == "opacity":
            layer.setOpacity(float(value))
        elif property == "name":
            layer.setName(str(value))
        elif property == "scale_visibility":
            layer.setScaleBasedVisibility(bool(value))
        elif property == "min_scale":
            layer.setMinimumScale(float(value))
        elif property == "max_scale":
            layer.setMaximumScale(float(value))
        else:
            raise Exception(
                f"Unknown property: {property}. "
                "Supported: opacity, name, min_scale, max_scale, scale_visibility"
            )

        self.iface.mapCanvas().refresh()
        return {"ok": True, "property": property, "value": value}

    def get_layer_extent(self, layer_id, **kwargs):
        project = QgsProject.instance()
        if layer_id not in project.mapLayers():
            raise Exception(f"Layer not found: {layer_id}")

        layer = project.mapLayer(layer_id)
        extent = layer.extent()
        return {
            "xmin": extent.xMinimum(),
            "ymin": extent.yMinimum(),
            "xmax": extent.xMaximum(),
            "ymax": extent.yMaximum(),
            "crs": layer.crs().authid(),
        }

    @staticmethod
    def _to_json_safe(val):
        """Convert a QVariant / Qt value to a JSON-serializable Python type."""
        if isinstance(val, QVariant):
            if val.isNull():
                return None
            val = val.value()
        # Qt date/time types → ISO string
        if hasattr(val, "toString"):
            try:
                return val.toString(1)  # Qt.ISODate == 1
            except Exception:
                return str(val)
        if isinstance(val, (str, int, float, bool, type(None))):
            return val
        return str(val)

    def get_project_variables(self, **kwargs):
        scope = QgsExpressionContextUtils.projectScope(QgsProject.instance())
        variables = {}
        for name in scope.variableNames():
            variables[name] = self._to_json_safe(scope.variable(name))
        return {"variables": variables}

    def set_project_variable(self, key, value, **kwargs):
        QgsExpressionContextUtils.setProjectVariable(QgsProject.instance(), key, value)
        return {"ok": True, "key": key, "value": value}

    def validate_expression(self, expression, layer_id=None, **kwargs):
        expr = QgsExpression(expression)
        result = {
            "valid": not expr.hasParserError(),
            "referenced_columns": list(expr.referencedColumns()),
        }
        if expr.hasParserError():
            result["error"] = expr.parserErrorString()

        if layer_id:
            project = QgsProject.instance()
            if layer_id in project.mapLayers():
                layer = project.mapLayer(layer_id)
                if layer.type() == LAYER_VECTOR:
                    context = QgsExpressionContext()
                    context.appendScope(QgsExpressionContextUtils.layerScope(layer))
                    expr.prepare(context)
                    if expr.hasEvalError():
                        result["eval_error"] = expr.evalErrorString()

        return result

    def get_setting(self, key, **kwargs):
        settings = QgsSettings()
        value = settings.value(key)
        return {
            "key": key,
            "value": value,
            "exists": settings.contains(key),
        }

    def set_setting(self, key, value, **kwargs):
        settings = QgsSettings()
        settings.setValue(key, value)
        return {"ok": True, "key": key}

    # -----------------------------------------------------------------------
    # Phase 4 — MCP modernization handlers
    # -----------------------------------------------------------------------

    def get_canvas_screenshot(self, **kwargs):
        """Grab the current map canvas as a fast screenshot (no re-render)."""
        canvas = self.iface.mapCanvas()
        pixmap = canvas.grab()
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(IODEVICE_WRITEONLY)
        pixmap.save(buf, "PNG")
        buf.close()
        b64 = base64.b64encode(ba.data()).decode("ascii")
        return {
            "base64_data": b64,
            "mime_type": "image/png",
            "width": pixmap.width(),
            "height": pixmap.height(),
        }

    def transform_coordinates(
        self, source_crs, target_crs, point=None, points=None, bbox=None, **kwargs
    ):
        """Transform coordinates between coordinate reference systems."""
        src = QgsCoordinateReferenceSystem(source_crs)
        dst = QgsCoordinateReferenceSystem(target_crs)
        if not src.isValid():
            raise Exception(f"Invalid source CRS: {source_crs}")
        if not dst.isValid():
            raise Exception(f"Invalid target CRS: {target_crs}")

        xform = QgsCoordinateTransform(src, dst, QgsProject.instance())
        result = {"source_crs": source_crs, "target_crs": target_crs}

        if point:
            pt = xform.transform(QgsPointXY(point["x"], point["y"]))
            result["point"] = {"x": pt.x(), "y": pt.y()}

        if points:
            transformed = []
            for p in points:
                pt = xform.transform(QgsPointXY(p["x"], p["y"]))
                transformed.append({"x": pt.x(), "y": pt.y()})
            result["points"] = transformed

        if bbox:
            rect = QgsRectangle(bbox["xmin"], bbox["ymin"], bbox["xmax"], bbox["ymax"])
            transformed_rect = xform.transformBoundingBox(rect)
            result["bbox"] = {
                "xmin": transformed_rect.xMinimum(),
                "ymin": transformed_rect.yMinimum(),
                "xmax": transformed_rect.xMaximum(),
                "ymax": transformed_rect.yMaximum(),
            }

        return result

    # -----------------------------------------------------------------------
    # Phase 5 — High-value capability handlers
    # -----------------------------------------------------------------------

    def get_active_layer(self, **kwargs):
        """Get the currently active (selected) layer in the layer panel."""
        layer = self.iface.activeLayer()
        if not layer:
            return {"active": False, "layer_id": None, "name": None, "type": None}
        return {
            "active": True,
            "layer_id": layer.id(),
            "name": layer.name(),
            "type": self._get_layer_type(layer),
        }

    def set_active_layer(self, layer_id, **kwargs):
        """Set the active layer by ID."""
        project = QgsProject.instance()
        layer = project.mapLayer(layer_id)
        if not layer:
            raise ValueError(f"Layer not found: {layer_id}")
        self.iface.setActiveLayer(layer)
        return {"ok": True, "layer_id": layer_id, "name": layer.name()}

    def get_canvas_scale(self, **kwargs):
        """Get map canvas scale, rotation, and magnification."""
        canvas = self.iface.mapCanvas()
        return {
            "scale": canvas.scale(),
            "rotation": canvas.rotation(),
            "magnification": canvas.magnificationFactor(),
        }

    def set_canvas_scale(self, scale=None, rotation=None, **kwargs):
        """Set map canvas scale and/or rotation."""
        canvas = self.iface.mapCanvas()
        if scale is not None:
            canvas.zoomScale(scale)
        if rotation is not None:
            canvas.setRotation(rotation)
        canvas.refresh()
        return {
            "ok": True,
            "scale": canvas.scale(),
            "rotation": canvas.rotation(),
        }

    def get_layer_labeling(self, layer_id, **kwargs):
        """Get labeling configuration for a vector layer."""
        layer = self._get_vector_layer(layer_id)
        result = {
            "layer_id": layer_id,
            "enabled": layer.labelsEnabled(),
        }
        labeling = layer.labeling()
        if labeling:
            settings = labeling.settings()
            result["field_name"] = settings.fieldName
            result["is_expression"] = settings.isExpression
            result["font_size"] = settings.format().size()
            result["color"] = settings.format().color().name()
            result["placement"] = str(settings.placement)
        return result

    def set_layer_labeling(self, layer_id, enabled=True, field_name=None, font_size=None, color=None, **kwargs):
        """Configure labeling for a vector layer."""
        from qgis.core import QgsPalLayerSettings, QgsTextFormat, QgsVectorLayerSimpleLabeling

        layer = self._get_vector_layer(layer_id)

        if not enabled:
            layer.setLabelsEnabled(False)
            layer.triggerRepaint()
            return {"ok": True, "layer_id": layer_id, "enabled": False}

        settings = QgsPalLayerSettings()
        if field_name:
            settings.fieldName = field_name
            settings.isExpression = False

        text_format = QgsTextFormat()
        if font_size:
            text_format.setSize(font_size)
        if color:
            text_format.setColor(QColor(color))
        settings.setFormat(text_format)

        labeling = QgsVectorLayerSimpleLabeling(settings)
        layer.setLabeling(labeling)
        layer.setLabelsEnabled(True)
        layer.triggerRepaint()
        return {"ok": True, "layer_id": layer_id, "enabled": True, "field_name": field_name}

    def get_layer_crs(self, layer_id, **kwargs):
        """Get the CRS of a layer."""
        project = QgsProject.instance()
        layer = project.mapLayer(layer_id)
        if not layer:
            raise ValueError(f"Layer not found: {layer_id}")
        crs = layer.crs()
        return {
            "layer_id": layer_id,
            "authid": crs.authid(),
            "description": crs.description(),
            "is_geographic": crs.isGeographic(),
            "proj4": crs.toProj4(),
        }

    def set_layer_crs(self, layer_id, crs, **kwargs):
        """Set the CRS of a layer."""
        project = QgsProject.instance()
        layer = project.mapLayer(layer_id)
        if not layer:
            raise ValueError(f"Layer not found: {layer_id}")
        new_crs = QgsCoordinateReferenceSystem(crs)
        if not new_crs.isValid():
            raise ValueError(f"Invalid CRS: {crs}")
        layer.setCrs(new_crs)
        return {"ok": True, "layer_id": layer_id, "crs": new_crs.authid()}

    def get_bookmarks(self, **kwargs):
        """Get spatial bookmarks from the project."""
        bm = QgsProject.instance().bookmarkManager()
        bookmarks = []
        for b in bm.bookmarks():
            extent = b.extent()
            bookmarks.append({
                "id": b.id(),
                "name": b.name(),
                "group": b.group(),
                "extent": {
                    "xmin": extent.xMinimum(),
                    "ymin": extent.yMinimum(),
                    "xmax": extent.xMaximum(),
                    "ymax": extent.yMaximum(),
                },
                "crs": extent.crs().authid() if extent.crs().isValid() else None,
            })
        return {"bookmarks": bookmarks, "count": len(bookmarks)}

    def add_bookmark(self, name, xmin, ymin, xmax, ymax, crs="EPSG:4326", group="", **kwargs):
        """Add a spatial bookmark to the project."""
        from qgis.core import QgsBookmark, QgsReferencedRectangle

        crs_obj = QgsCoordinateReferenceSystem(crs)
        if not crs_obj.isValid():
            raise ValueError(f"Invalid CRS: {crs}")
        extent = QgsReferencedRectangle(QgsRectangle(xmin, ymin, xmax, ymax), crs_obj)
        bookmark = QgsBookmark()
        bookmark.setName(name)
        bookmark.setGroup(group)
        bookmark.setExtent(extent)
        result = QgsProject.instance().bookmarkManager().addBookmark(bookmark)
        # addBookmark returns (id, success) tuple in QGIS 3.x+
        bookmark_id = result[0] if isinstance(result, (list, tuple)) else result
        return {"ok": True, "id": bookmark_id, "name": name}

    def remove_bookmark(self, bookmark_id, **kwargs):
        """Remove a spatial bookmark by ID."""
        bm = QgsProject.instance().bookmarkManager()
        bm.removeBookmark(bookmark_id)
        return {"ok": True, "id": bookmark_id}

    def get_map_themes(self, **kwargs):
        """Get map themes (visibility presets)."""
        collection = QgsProject.instance().mapThemeCollection()
        themes = collection.mapThemes()
        result = []
        for name in themes:
            layer_ids = collection.mapThemeVisibleLayerIds(name)
            result.append({
                "name": name,
                "visible_layer_count": len(layer_ids),
                "visible_layer_ids": layer_ids,
            })
        return {"themes": result, "count": len(result)}

    def add_map_theme(self, name, **kwargs):
        """Create a map theme from the current layer visibility state."""
        from qgis.core import QgsMapThemeCollection

        collection = QgsProject.instance().mapThemeCollection()
        root = QgsProject.instance().layerTreeRoot()
        model = self.iface.layerTreeView().layerTreeModel()
        record = QgsMapThemeCollection.createThemeFromCurrentState(root, model)
        if collection.hasMapTheme(name):
            collection.update(name, record)
            return {"ok": True, "name": name, "action": "updated"}
        else:
            collection.insert(name, record)
            return {"ok": True, "name": name, "action": "created"}

    def remove_map_theme(self, name, **kwargs):
        """Remove a map theme."""
        collection = QgsProject.instance().mapThemeCollection()
        if not collection.hasMapTheme(name):
            raise ValueError(f"Map theme not found: {name}")
        collection.removeMapTheme(name)
        return {"ok": True, "name": name}

    def apply_map_theme(self, name, **kwargs):
        """Apply a map theme (restore its layer visibility state)."""
        collection = QgsProject.instance().mapThemeCollection()
        if not collection.hasMapTheme(name):
            raise ValueError(f"Map theme not found: {name}")
        root = QgsProject.instance().layerTreeRoot()
        model = self.iface.layerTreeView().layerTreeModel()
        collection.applyTheme(name, root, model)
        self.iface.mapCanvas().refresh()
        return {"ok": True, "name": name}

    def set_project_crs(self, crs, **kwargs):
        """Set the project CRS."""
        new_crs = QgsCoordinateReferenceSystem(crs)
        if not new_crs.isValid():
            raise ValueError(f"Invalid CRS: {crs}")
        QgsProject.instance().setCrs(new_crs)
        return {"ok": True, "crs": new_crs.authid(), "description": new_crs.description()}


class QgisMCPPlugin:
    """Main plugin class for QGIS MCP"""

    REPO_URL = "https://github.com/nkarasiak/qgis-mcp"

    SETTINGS_PREFIX = "qgis_mcp"

    def __init__(self, iface):
        self.iface = iface
        self.server = None
        self.action = None
        self.help_action = None
        self.tool_button = None
        self._toolbar_action = None  # the action wrapping the tool button

    def _logo_icon(self):
        """Load the MCP logo from the plugin directory."""
        icon_path = os.path.join(os.path.dirname(__file__), "icons", "icon.png")
        return QIcon(icon_path)

    def initGui(self):
        toolbar = self.iface.pluginToolBar()

        # Main action (used for menu entry + click handler)
        self.action = QAction(self._logo_icon(), "Run MCP", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.setToolTip(f"Start MCP server on port {_DEFAULT_PORT}")
        self.action.triggered.connect(self.toggle_server)

        # Port config in dropdown menu
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(_DEFAULT_PORT)
        self.port_spin.setPrefix("Port: ")
        self.port_spin.valueChanged.connect(self._save_port)

        port_widget = QWidget()
        port_layout = QHBoxLayout()
        port_layout.setContentsMargins(6, 4, 6, 4)
        port_layout.addWidget(self.port_spin)
        port_widget.setLayout(port_layout)

        port_wa = QWidgetAction(self.iface.mainWindow())
        port_wa.setDefaultWidget(port_widget)

        # Auto-start checkbox
        self.autostart_cb = QCheckBox("Auto-start on startup")
        settings = QgsSettings()
        self.autostart_cb.setChecked(
            settings.value(f"{self.SETTINGS_PREFIX}/autostart", False, type=bool)
        )
        self.autostart_cb.toggled.connect(self._save_autostart)

        autostart_widget = QWidget()
        autostart_layout = QHBoxLayout()
        autostart_layout.setContentsMargins(6, 4, 6, 4)
        autostart_layout.addWidget(self.autostart_cb)
        autostart_widget.setLayout(autostart_layout)

        autostart_wa = QWidgetAction(self.iface.mainWindow())
        autostart_wa.setDefaultWidget(autostart_widget)

        menu = QMenu()
        menu.addAction(port_wa)
        menu.addAction(autostart_wa)

        # Tool button with dropdown (like Plugin Reloader)
        self.tool_button = QToolButton()
        self.tool_button.setDefaultAction(self.action)
        self.tool_button.setMenu(menu)
        self.tool_button.setPopupMode(TOOLBUTTON_MENU_POPUP)
        self.tool_button.setToolButtonStyle(TOOLBUTTON_ICON_ONLY)
        self._toolbar_action = toolbar.addWidget(self.tool_button)

        self.help_action = QAction("Help / Install MCP Server", self.iface.mainWindow())
        self.help_action.triggered.connect(self._show_help)

        self.iface.addPluginToMenu("QGIS MCP", self.action)
        self.iface.addPluginToMenu("QGIS MCP", self.help_action)

        # Restore saved port
        saved_port = settings.value(f"{self.SETTINGS_PREFIX}/port", _DEFAULT_PORT, type=int)
        self.port_spin.setValue(saved_port)

        # Auto-start if enabled
        if self.autostart_cb.isChecked():
            self.action.setChecked(True)
            self.toggle_server(True)

    def _save_autostart(self, checked):
        """Persist auto-start preference."""
        QgsSettings().setValue(f"{self.SETTINGS_PREFIX}/autostart", checked)

    def _save_port(self, port):
        """Persist port preference."""
        QgsSettings().setValue(f"{self.SETTINGS_PREFIX}/port", port)

    def _green_logo_icon(self):
        """Load the green MCP logo for active state."""
        icon_path = os.path.join(os.path.dirname(__file__), "icons", "icon_active.png")
        return QIcon(icon_path)

    def _show_help(self):
        """Show help dialog with MCP server installation instructions."""
        dlg = QDialog(self.iface.mainWindow())
        dlg.setWindowTitle("QGIS MCP — Setup Guide")
        dlg.setMinimumWidth(520)

        layout = QVBoxLayout()
        label = QLabel(
            "<p>This plugin is only one half of the setup. You also need an "
            "<b>MCP server</b> so that Claude (or another LLM) can talk to QGIS.</p>"
            "<p><b>Quick setup:</b> Run <code>python install.py</code> from the "
            "repository root to configure your MCP client(s) automatically.</p>"
            "<p>Full instructions are on the "
            f'<a href="{self.REPO_URL}#installation">GitHub repository</a>.</p>'
        )
        label.setWordWrap(True)
        label.setOpenExternalLinks(True)
        layout.addWidget(label)

        btn_layout = QHBoxLayout()
        github_btn = QToolButton()
        github_btn.setText("Open GitHub")
        github_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.REPO_URL)))
        btn_layout.addWidget(github_btn)
        btn_layout.addStretch()
        ok_btn = QToolButton()
        ok_btn.setText("OK")
        ok_btn.setMinimumWidth(80)
        ok_btn.clicked.connect(dlg.accept)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

        dlg.setLayout(layout)
        dlg.exec()

    def toggle_server(self, checked):
        if checked:
            port = self.port_spin.value()
            self.server = QgisMCPServer(port=port, iface=self.iface)
            if self.server.start():
                self.action.setIcon(self._green_logo_icon())
                self.action.setText(f"MCP :{port}")
                self.action.setToolTip(f"MCP server running on :{port} — click to stop")
                self.port_spin.setEnabled(False)
            else:
                self.server = None
                self.action.setChecked(False)
        else:
            if self.server:
                self.server.stop()
                self.server = None
            self.action.setIcon(self._logo_icon())
            self.action.setText("Run MCP")
            self.action.setToolTip("Start MCP server")
            self.port_spin.setEnabled(True)

    def unload(self):
        if self.server:
            self.server.stop()
            self.server = None
        if self.action:
            self.action.triggered.disconnect(self.toggle_server)
            self.iface.removePluginMenu("QGIS MCP", self.action)
            self.action = None
        if self.help_action:
            self.help_action.triggered.disconnect(self._show_help)
            self.iface.removePluginMenu("QGIS MCP", self.help_action)
            self.help_action = None
        if self._toolbar_action:
            self.iface.pluginToolBar().removeAction(self._toolbar_action)
            self._toolbar_action = None
        if hasattr(self, "port_spin"):
            self.port_spin.valueChanged.disconnect(self._save_port)
        if hasattr(self, "autostart_cb"):
            self.autostart_cb.toggled.disconnect(self._save_autostart)


# Plugin entry point
def classFactory(iface):
    return QgisMCPPlugin(iface)
