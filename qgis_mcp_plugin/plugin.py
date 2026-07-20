import base64
import contextlib
import fnmatch
import io
import json
import os
import secrets
import shutil
import socket
import struct
import sys
import tempfile
import traceback
from collections import deque
from pathlib import Path
from typing import ClassVar

# Compatibility for different python versions
try:
    from datetime import UTC, datetime
except ImportError:
    from datetime import datetime, timezone

    UTC = timezone.utc  # noqa: UP017  (fallback path: datetime.UTC unavailable pre-3.11)

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsClassificationEqualInterval,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsGraduatedSymbolRenderer,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsLayoutExporter,
    QgsLayoutItemMap,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsMessageLog,
    QgsPointXY,
    QgsPrintLayout,
    QgsProcessingModelAlgorithm,
    QgsProcessingModelChildAlgorithm,
    QgsProcessingModelChildParameterSource,
    QgsProcessingModelOutput,
    QgsProcessingModelParameter,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterDistance,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterFile,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingParameterPoint,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsRendererCategory,
    QgsSettings,
    QgsSingleSymbolRenderer,
    QgsStyle,
    QgsSymbol,
    QgsVectorLayer,
    QgsVectorLayerJoinInfo,
    QgsVectorSimplifyMethod,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import (
    QBuffer,
    QByteArray,
    QEventLoop,
    QObject,
    QPointF,
    QSize,
    QTimer,
    QUrl,
    QVariant,
)
from qgis.PyQt.QtGui import QColor, QDesktopServices, QIcon, QImage, QPainter, QPen
from qgis.PyQt.QtWidgets import (
    QAction,
    QCheckBox,
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
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
    ALIGN_CENTER,
    GEOM_LINE,
    GEOM_POLYGON,
    IODEVICE_WRITEONLY,
    LAYER_RASTER,
    LAYER_VECTOR,
    LAYOUT_SUCCESS,
    MSG_CRITICAL,
    MSG_INFO,
    MSG_WARNING,
    MSGBOX_ACCEPT_ROLE,
    MSGBOX_QUESTION,
    MSGBOX_REJECT_ROLE,
    PAINTER_ANTIALIAS,
    PROC_FILE_FOLDER,
    PROC_NUM_INTEGER,
    PROCESSING_OPTIONAL,
    QVAR_BOOL,
    QVAR_DATE,
    QVAR_DATETIME,
    QVAR_DOUBLE,
    QVAR_INT,
    QVAR_STRING,
    RASTER_STATS_ALL,
    SIMPLIFY_ANTIALIAS,
    SIMPLIFY_GEOMETRY,
    TOOLBUTTON_ICON_ONLY,
    TOOLBUTTON_MENU_POPUP,
    WKB_NO_GEOMETRY,
)

_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = 9876
_RECV_CHUNK_SIZE = 65536
_MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10 MB
_HEADER_STRUCT = struct.Struct(">I")


class QgisMCPServer(QObject):
    """Server class to handle socket connections and execute QGIS commands"""

    LOG_TAG: ClassVar[str] = "MCP"
    MAX_CLIENTS: ClassVar[int] = 10

    def __init__(self, host=_DEFAULT_HOST, port=_DEFAULT_PORT, iface=None, on_clients_changed=None):
        super().__init__()
        self.host = host
        self.port = port
        self.iface = iface
        self.on_clients_changed = on_clients_changed
        self.running = False
        self.socket = None
        self.clients: dict[socket.socket, bytes] = {}
        self.timer = None
        self._message_log = deque(maxlen=1000)

    def _notify_clients_changed(self):
        """Report the active client count to the UI (badge on the toolbar icon)."""
        if self.on_clients_changed:
            with contextlib.suppress(Exception):
                self.on_clients_changed(len(self.clients))

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
            # QGIS 4.x routes messages through messageReceivedWithFormat only;
            # messageReceived no longer fires.  Fall back for 3.x.
            if hasattr(msg_log, "messageReceivedWithFormat"):
                msg_log.messageReceivedWithFormat.connect(self._capture_message)
            else:
                msg_log.messageReceived.connect(self._capture_message)
            QgsMessageLog.logMessage(
                f"QGIS MCP server started on {self.host}:{self.port}", self.LOG_TAG, MSG_INFO
            )
            auth_on = bool(os.environ.get("QGIS_MCP_TOKEN", "").strip())
            QgsMessageLog.logMessage(
                f"Token authentication {'ENABLED' if auth_on else 'disabled (open)'}",
                self.LOG_TAG,
                MSG_INFO if auth_on else MSG_WARNING,
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
        self._notify_clients_changed()

        self.socket = None
        QgsMessageLog.logMessage("QGIS MCP server stopped", self.LOG_TAG, MSG_INFO)

    def _disconnect_client(self, client_sock, message="Client disconnected", level=MSG_INFO):
        """Close and remove a client socket."""
        with contextlib.suppress(Exception):
            client_sock.close()
        self.clients.pop(client_sock, None)
        QgsMessageLog.logMessage(f"{message} ({len(self.clients)} active)", self.LOG_TAG, level)
        self._notify_clients_changed()

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
                        self._notify_clients_changed()
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
                            msg_bytes = buf[4:4 + msg_len]
                            buf = buf[4 + msg_len:]
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
        """Execute a command.

        Optional shared-secret auth gate: when QGIS_MCP_TOKEN is set in the
        plugin's environment, every command must carry a matching token or it
        is rejected before any handler runs. When unset, behaviour is unchanged
        (open) for backward compatibility. Dispatch itself lives in _dispatch
        so internal callers (e.g. batch) reuse it without re-authenticating.
        """
        # This runs on untrusted socket input before auth — never trust shape.
        if not isinstance(command, dict):
            return {"status": "error", "message": "Invalid command: expected an object"}

        expected_token = os.environ.get("QGIS_MCP_TOKEN", "").strip()
        if expected_token:
            provided_token = str(command.get("token") or "")
            # Compare as bytes: secrets.compare_digest rejects non-ASCII str.
            if not secrets.compare_digest(
                provided_token.encode("utf-8"), expected_token.encode("utf-8")
            ):
                QgsMessageLog.logMessage(
                    "Rejected command with missing/invalid token", self.LOG_TAG, MSG_WARNING
                )
                return {
                    "status": "error",
                    "message": "Authentication failed: missing or invalid token",
                }
        return self._dispatch(command)

    def _dispatch(self, command):
        """Dispatch an already-authenticated command to its handler."""
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
                "create_processing_model": self.create_processing_model,
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
                # Phase 6 — Extended capabilities
                "add_web_layer": self.add_web_layer,
                "add_table_join": self.add_table_join,
                "add_field": self.add_field,
                "delete_field": self.delete_field,
                "rename_field": self.rename_field,
                "apply_style_qml": self.apply_style_qml,
                "save_style_qml": self.save_style_qml,
                "create_layout": self.create_layout,
                "add_layout_map": self.add_layout_map,
                # Phase 7 — Processing framework + analysis
                "list_processing_models": self.list_processing_models,
                "run_model": self.run_model,
                "get_processing_providers": self.get_processing_providers,
                "execute_processing_batch": self.execute_processing_batch,
                "raster_calculator": self.raster_calculator,
                "zonal_statistics": self.zonal_statistics,
                "sample_raster_values": self.sample_raster_values,
                "export_layer": self.export_layer,
                "field_calculator": self.field_calculator,
                "get_unique_values": self.get_unique_values,
                "spatial_join": self.spatial_join,
                # Phase 8 — Layout/atlas authoring, query & management
                "get_layout_info": self.get_layout_info,
                "add_layout_label": self.add_layout_label,
                "add_layout_legend": self.add_layout_legend,
                "add_layout_scalebar": self.add_layout_scalebar,
                "add_layout_picture": self.add_layout_picture,
                "add_layout_table": self.add_layout_table,
                "configure_atlas": self.configure_atlas,
                "export_atlas": self.export_atlas,
                "remove_layout": self.remove_layout,
                "execute_sql": self.execute_sql,
                "evaluate_expression": self.evaluate_expression,
                "identify_features": self.identify_features,
                "duplicate_layer": self.duplicate_layer,
                "set_layer_order": self.set_layer_order,
                # Phase 9 — 3D
                "get_3d_screenshot": self.get_3d_screenshot,
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

    def execute_code(self, code, **kwargs):
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

            return {
                "executed": True,
                "stdout": stdout_capture.getvalue(),
                "stderr": stderr_capture.getvalue(),
            }
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
        page = all_layers[offset:offset + limit]

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
        return [
            self._dispatch({"type": cmd.get("type"), "params": cmd.get("params", {})})
            for cmd in commands
        ]

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

    _RENDER_TIMEOUT = 55  # seconds (below MCP's 60s TIMEOUT_LONG)

    def render_map_base64(self, width=800, height=600, path=None, **kwargs):
        """Render the map and return base64-encoded PNG data."""
        try:
            canvas = self.iface.mapCanvas()
            # Clone the canvas settings so the render reproduces what the user
            # sees: visible layers only, canvas/custom draw order, destination
            # CRS, transform context, labeling, style overrides and temporal
            # state. Rebuilding from the project registry
            # (QgsProject.mapLayers()) renders hidden layers in registry order
            # and can place an opaque hidden layer over the overlays (issue #21).
            src = canvas.mapSettings()
            try:
                ms = QgsMapSettings(src)  # copy constructor (preferred)
            except Exception:
                # Fallback if the copy constructor is unavailable on this QGIS:
                # at minimum honour visibility, draw order, CRS and transform.
                ms = QgsMapSettings()
                ms.setLayers(src.layers())
                ms.setDestinationCrs(src.destinationCrs())
                ms.setTransformContext(QgsProject.instance().transformContext())
            ms.setExtent(canvas.extent())
            ms.setOutputSize(QSize(width, height))
            ms.setBackgroundColor(QColor(255, 255, 255))
            ms.setOutputDpi(96)

            # Enable geometry simplification (matches QGIS canvas defaults).
            # Skips sub-pixel vertices — critical for large datasets at small scales.
            simplify = QgsVectorSimplifyMethod()
            simplify.setSimplifyHints(SIMPLIFY_GEOMETRY | SIMPLIFY_ANTIALIAS)
            simplify.setThreshold(1.0)  # 1 pixel
            simplify.setForceLocalOptimization(True)
            ms.setSimplifyMethod(simplify)

            render = QgsMapRendererParallelJob(ms)

            # Use QEventLoop + QTimer for non-blocking wait with timeout.
            # Keeps Qt event loop alive and allows cancellation.
            loop = QEventLoop()
            timed_out = []
            render.finished.connect(loop.quit)

            timeout_timer = QTimer()
            timeout_timer.setSingleShot(True)
            timeout_timer.timeout.connect(lambda: (timed_out.append(True), loop.quit()))
            timeout_timer.start(self._RENDER_TIMEOUT * 1000)

            render.start()
            loop.exec()

            timeout_timer.stop()
            if timed_out:
                render.cancelWithoutBlocking()
                render.waitForFinished()
                raise Exception(f"Render timed out after {self._RENDER_TIMEOUT}s")

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
        self, layer_id, style_type, field=None, classes=5, color_ramp="Spectral", **kwargs
    ):
        layer = self._get_vector_layer(layer_id)

        if style_type == "single":
            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            renderer = QgsSingleSymbolRenderer(symbol)
            layer.setRenderer(renderer)

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

            categories = []
            n = max(len(unique_values) - 1, 1)
            for i, value in enumerate(unique_values):
                symbol = QgsSymbol.defaultSymbol(layer.geometryType())
                symbol.setColor(ramp.color(i / n))
                label = str(value) if value is not None else "NULL"
                categories.append(QgsRendererCategory(value, symbol, label))

            renderer = QgsCategorizedSymbolRenderer(field, categories)
            layer.setRenderer(renderer)

        elif style_type == "graduated":
            if not field:
                raise Exception("field is required for graduated style")
            idx = layer.fields().indexOf(field)
            if idx < 0:
                raise Exception(f"Field not found: {field}")

            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            ramp = QgsStyle.defaultStyle().colorRamp(color_ramp)
            if not ramp:
                ramp = QgsStyle.defaultStyle().colorRamp("Spectral")

            renderer = QgsGraduatedSymbolRenderer(field)
            renderer.setSourceSymbol(symbol.clone())
            renderer.setSourceColorRamp(ramp)

            renderer.setClassificationMethod(QgsClassificationEqualInterval())
            renderer.updateClasses(layer, classes)

            layer.setRenderer(renderer)
        else:
            raise Exception(
                f"Unknown style_type: {style_type}. Use 'single', 'categorized', or 'graduated'"
            )

        layer.triggerRepaint()
        self.iface.layerTreeView().refreshLayerSymbology(layer.id())
        return {"ok": True}

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
                in_id = search_lower in alg.id().lower()
                in_name = search_lower in alg.displayName().lower()
                if not in_id and not in_name:
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
            with contextlib.suppress(Exception):
                default = param.defaultValue()
                if default is not None:
                    param_info["default"] = str(default)
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

    # ------------------------------------------------------------------
    # Processing Model construction
    # ------------------------------------------------------------------

    def _resolve_algorithm_id(self, hint, registry):
        """Resolve an algorithm hint to a fully-qualified id (e.g. 'native:buffer').

        Direct lookup against ``QgsApplication.processingRegistry()``: the LLM
        passes a keyword like ``"buffer"`` or a full id, and this matches it
        against algorithm ids, display names and tags. Falls back with a
        candidate list when the hint is ambiguous, so the caller can refine.
        """
        if not isinstance(hint, str) or not hint.strip():
            raise Exception("Algorithm hint must be a non-empty string")
        hint_clean = hint.strip()

        # 1. Exact id match (incl. fully qualified 'native:buffer').
        alg = registry.algorithmById(hint_clean)
        if alg is not None:
            return alg.id()

        hint_lower = hint_clean.lower()
        exact_name = []   # display name == hint
        suffix_id = []    # id suffix == hint (after ':')
        contains = []     # display name or id suffix contains hint
        for alg in registry.algorithms():
            alg_id = alg.id()
            id_suffix = alg_id.split(":", 1)[-1].lower()
            disp = alg.displayName().lower()
            if disp == hint_lower:
                exact_name.append(alg)
            elif id_suffix == hint_lower:
                suffix_id.append(alg)
            elif hint_lower in disp or hint_lower in id_suffix:
                contains.append(alg)

        def _pick(group):
            if len(group) == 1:
                return group[0].id()
            natives = [a for a in group if a.provider().id() == "native"]
            if len(natives) == 1:
                return natives[0].id()
            return None

        for group in (exact_name, suffix_id, contains):
            picked = _pick(group)
            if picked:
                return picked

        all_candidates = exact_name + suffix_id + contains
        if not all_candidates:
            raise Exception(
                f"No Processing algorithm matches '{hint_clean}'. "
                "Pass a keyword found in the algorithm name or its full id (e.g. 'native:buffer')."
            )
        # Show up to 8 candidates so the LLM can disambiguate next call.
        sample = ", ".join(
            f"{a.id()} ({a.displayName()})"
            for a in sorted(all_candidates, key=lambda a: (a.provider().id() != "native", len(a.id())))[:8]
        )
        raise Exception(
            f"Algorithm hint '{hint_clean}' is ambiguous. Candidates: {sample}. "
            "Use the full id."
        )

    def _build_param_source(self, value, defined_inputs, defined_steps):
        """Convert a JSON-friendly value into a QgsProcessingModelChildParameterSource.

        String prefixes:
          @name          -> reference to model input parameter
          $step.OUTPUT   -> reference to a previous step's output
          =expression    -> evaluated QGIS expression
        Lists are converted element-wise; everything else becomes a static value.
        """
        Src = QgsProcessingModelChildParameterSource

        if isinstance(value, list):
            return [self._build_param_source(v, defined_inputs, defined_steps)[0] for v in value]

        if isinstance(value, str):
            if value.startswith("@"):
                ref = value[1:]
                if ref not in defined_inputs:
                    raise Exception(
                        f"Parameter reference '{value}' points to undefined model input '{ref}'"
                    )
                return [Src.fromModelParameter(ref)]
            if value.startswith("$"):
                rest = value[1:]
                if "." not in rest:
                    raise Exception(
                        f"Step output reference '{value}' must be in '$step_id.OUTPUT_NAME' form"
                    )
                child_id, output_name = rest.split(".", 1)
                if child_id not in defined_steps:
                    raise Exception(
                        f"Step output reference '{value}' points to undefined step '{child_id}'"
                    )
                return [Src.fromChildOutput(child_id, output_name)]
            if value.startswith("="):
                return [Src.fromExpression(value[1:])]

        return [Src.fromStaticValue(value)]

    def _make_input_definition(self, spec):
        """Build a QgsProcessingParameterDefinition from a JSON spec dict."""
        type_name = (spec.get("type") or "string").lower()
        name = spec["name"]
        description = spec.get("description", name)
        default = spec.get("default", None)
        optional = bool(spec.get("optional", False))

        if type_name in ("vector", "vector_layer"):
            param = QgsProcessingParameterVectorLayer(name, description, defaultValue=default)
        elif type_name in ("feature_source", "source"):
            param = QgsProcessingParameterFeatureSource(name, description, defaultValue=default)
        elif type_name in ("raster", "raster_layer"):
            param = QgsProcessingParameterRasterLayer(name, description, defaultValue=default)
        elif type_name == "field":
            parent = spec.get("parent_layer")
            if not parent:
                raise Exception(f"Input '{name}' of type 'field' requires 'parent_layer'")
            param = QgsProcessingParameterField(
                name, description, parentLayerParameterName=parent, defaultValue=default
            )
        elif type_name in ("number", "int", "integer", "float", "double"):
            param = QgsProcessingParameterNumber(name, description, defaultValue=default)
            if type_name in ("int", "integer"):
                with contextlib.suppress(AttributeError):
                    param.setDataType(PROC_NUM_INTEGER)
        elif type_name == "distance":
            param = QgsProcessingParameterDistance(name, description, defaultValue=default)
            parent = spec.get("parent_layer")
            if parent:
                param.setParentParameterName(parent)
        elif type_name == "string":
            param = QgsProcessingParameterString(name, description, defaultValue=default)
        elif type_name in ("boolean", "bool"):
            param = QgsProcessingParameterBoolean(
                name, description, defaultValue=bool(default) if default is not None else False
            )
        elif type_name == "extent":
            param = QgsProcessingParameterExtent(name, description, defaultValue=default)
        elif type_name == "crs":
            param = QgsProcessingParameterCrs(
                name, description, defaultValue=default or "EPSG:4326"
            )
        elif type_name == "point":
            param = QgsProcessingParameterPoint(name, description, defaultValue=default)
        elif type_name == "file":
            param = QgsProcessingParameterFile(name, description, defaultValue=default)
        elif type_name == "folder":
            param = QgsProcessingParameterFile(name, description, defaultValue=default)
            with contextlib.suppress(AttributeError):
                param.setBehavior(PROC_FILE_FOLDER)
        elif type_name == "enum":
            options = spec.get("options") or []
            param = QgsProcessingParameterEnum(
                name, description, options=options, defaultValue=default
            )
        elif type_name in ("multiple_layers", "layers"):
            param = QgsProcessingParameterMultipleLayers(name, description, defaultValue=default)
        else:
            raise Exception(f"Unsupported input type '{type_name}' for input '{name}'")

        if optional:
            with contextlib.suppress(Exception):
                param.setFlags(param.flags() | PROCESSING_OPTIONAL)
        return param

    def create_processing_model(
        self,
        name,
        steps,
        inputs=None,
        outputs=None,
        description="",
        group="Models",
        **kwargs,
    ):
        """Build a Processing Model from a structured spec, save it into the
        QGIS user models folder under a unique name, and register it.

        Reference syntax in step parameter values:
          "@input_name"        – model input parameter
          "$step_id.OUTPUT"    – output of a previous step
          "=expression"        – QGIS expression
          anything else        – static literal (numbers, bools, strings, lists, ...)
        """
        if not name or not isinstance(name, str):
            raise Exception("Model 'name' is required")
        if not isinstance(steps, list) or not steps:
            raise Exception("'steps' must be a non-empty list")

        registry = QgsApplication.processingRegistry()

        # ---- Resolve models folder & pick a unique file name up front ----
        provider = registry.providerById("model")
        models_dir = None
        if provider is not None and hasattr(provider, "modelsFolder"):
            try:
                models_dir = provider.modelsFolder()
            except Exception:
                models_dir = None
        if models_dir is None:
            models_dir = os.path.join(
                QgsApplication.qgisSettingsDirPath(), "processing", "models"
            )
        os.makedirs(models_dir, exist_ok=True)

        final_name = name
        target_path = os.path.join(models_dir, f"{final_name}.model3")
        if os.path.exists(target_path):
            for suffix in range(2, 1001):
                candidate = f"{name}_{suffix}"
                candidate_path = os.path.join(models_dir, f"{candidate}.model3")
                if not os.path.exists(candidate_path):
                    final_name = candidate
                    target_path = candidate_path
                    break
            else:
                raise Exception(
                    f"Could not find a unique name for '{name}' in {models_dir} "
                    "(tried up to _1000)"
                )

        # ---- Build model skeleton ----
        model = QgsProcessingModelAlgorithm()
        model.setName(final_name)
        if group:
            model.setGroup(group)
        if description:
            with contextlib.suppress(Exception):
                model.setHelpContent({"ALG_DESC": description})

        # ---- Inputs ----
        defined_inputs = set()
        for idx, spec in enumerate(inputs or []):
            if not isinstance(spec, dict) or "name" not in spec:
                raise Exception(f"Input #{idx} must be a dict with at least 'name'")
            param_def = self._make_input_definition(spec)
            mp = QgsProcessingModelParameter(spec["name"])
            mp.setPosition(QPointF(50.0, 50.0 + idx * 100.0))
            model.addModelParameter(param_def, mp)
            defined_inputs.add(spec["name"])

        # ---- Steps ----
        # Validate shape & resolve algorithm hints up front so we never write
        # a half-built file. Each step entry is normalized to a fully-qualified
        # algorithm id stored under '_resolved_algorithm'.
        defined_steps: list[str] = []
        resolved: list[tuple[dict, str]] = []  # (step_spec, resolved_alg_id)
        seen_ids: set[str] = set()
        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                raise Exception(f"Step #{idx} must be a dict")
            for required in ("id", "algorithm"):
                if required not in step:
                    raise Exception(f"Step #{idx} missing required key '{required}'")
            if step["id"] in seen_ids:
                raise Exception(f"Duplicate step id '{step['id']}'")
            seen_ids.add(step["id"])
            try:
                alg_id = self._resolve_algorithm_id(step["algorithm"], registry)
            except Exception as e:
                raise Exception(f"Step '{step['id']}': {e}") from e
            alg = registry.algorithmById(alg_id)
            valid_params = {p.name() for p in alg.parameterDefinitions()}
            for pname in (step.get("parameters") or {}):
                if pname not in valid_params:
                    raise Exception(
                        f"Step '{step['id']}' (algorithm '{alg_id}'): unknown parameter "
                        f"'{pname}'. Valid parameters: {sorted(valid_params)}"
                    )
            resolved.append((step, alg_id))

        # Outputs may be marked on a per-step basis; collect them by step id
        outputs_by_step: dict[str, dict[str, dict]] = {}
        step_id_to_alg: dict[str, str] = {step["id"]: alg_id for step, alg_id in resolved}
        for out_idx, out_spec in enumerate(outputs or []):
            if not isinstance(out_spec, dict):
                raise Exception(f"Output #{out_idx} must be a dict")
            for required in ("name", "from_step", "from_output"):
                if required not in out_spec:
                    raise Exception(f"Output #{out_idx} missing required key '{required}'")
            from_step = out_spec["from_step"]
            if from_step not in step_id_to_alg:
                raise Exception(
                    f"Output '{out_spec['name']}': from_step '{from_step}' is not a defined step"
                )
            from_alg = registry.algorithmById(step_id_to_alg[from_step])
            valid_outputs = {o.name() for o in from_alg.outputDefinitions()}
            if out_spec["from_output"] not in valid_outputs:
                raise Exception(
                    f"Output '{out_spec['name']}': '{out_spec['from_output']}' is not an output "
                    f"of step '{from_step}' (algorithm '{step_id_to_alg[from_step]}'). "
                    f"Valid outputs: {sorted(valid_outputs)}"
                )
            outputs_by_step.setdefault(from_step, {})[out_spec["name"]] = out_spec

        for step_idx, (step, alg_id) in enumerate(resolved):
            child = QgsProcessingModelChildAlgorithm(alg_id)
            child.setChildId(step["id"])
            child.setDescription(step.get("description") or registry.algorithmById(alg_id).displayName())
            child.setPosition(QPointF(300.0 + step_idx * 250.0, 50.0))

            for pname, pvalue in (step.get("parameters") or {}).items():
                # Build sources, validating refs against already-defined inputs/steps.
                sources = self._build_param_source(pvalue, defined_inputs, set(defined_steps))
                child.addParameterSources(pname, sources)

            # Final outputs declared for this step
            step_outputs = outputs_by_step.get(step["id"], {})
            if step_outputs:
                model_outputs = {}
                for out_name, out_spec in step_outputs.items():
                    mo = QgsProcessingModelOutput(out_name)
                    mo.setChildId(step["id"])
                    mo.setChildOutputName(out_spec["from_output"])
                    mo.setDescription(out_spec.get("description") or out_name)
                    model_outputs[out_name] = mo
                child.setModelOutputs(model_outputs)

            model.addChildAlgorithm(child)
            defined_steps.append(step["id"])

        # If the user did not declare any outputs, expose the last step's OUTPUT
        # under a default name so the model produces something the user can save.
        if not outputs and defined_steps:
            last_step_id = defined_steps[-1]
            last_child = model.childAlgorithm(last_step_id)
            last_alg = registry.algorithmById(last_child.algorithmId())
            output_names = [o.name() for o in last_alg.outputDefinitions()] if last_alg else []
            preferred = "OUTPUT" if "OUTPUT" in output_names else (output_names[0] if output_names else None)
            if preferred:
                mo = QgsProcessingModelOutput("Result")
                mo.setChildId(last_step_id)
                mo.setChildOutputName(preferred)
                mo.setDescription("Result")
                last_child.setModelOutputs({"Result": mo})

        # ---- Write the .model3 file directly into the models folder ----
        if not model.toFile(target_path):
            raise Exception(f"Failed to write model to {target_path}")

        # ---- Register with the model provider so it shows up in the toolbox ----
        registered = False
        if provider is not None:
            try:
                provider.refreshAlgorithms()
                registered = True
            except Exception as e:
                QgsMessageLog.logMessage(
                    f"Model saved but provider refresh failed: {e}", self.LOG_TAG, MSG_WARNING
                )

        QgsMessageLog.logMessage(
            f"Processing model '{final_name}' saved to {target_path}", self.LOG_TAG, MSG_INFO
        )
        return {
            "ok": True,
            "name": final_name,
            "requested_name": name,
            "path": target_path,
            "registered": registered,
            "input_count": len(defined_inputs),
            "step_count": len(defined_steps),
            "output_count": sum(len(v) for v in outputs_by_step.values()) or (1 if defined_steps else 0),
            # Echo the resolved algorithm ids so the caller can confirm fuzzy matches.
            "resolved_steps": [
                {"id": step["id"], "algorithm": alg_id, "hint": step["algorithm"]}
                for step, alg_id in resolved
            ],
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

    def export_layout(self, layout_name, path, format="pdf", dpi=300, **kwargs):
        manager = QgsProject.instance().layoutManager()
        layout = manager.layoutByName(layout_name)
        if not layout:
            raise Exception(f"Layout not found: {layout_name}")

        exporter = QgsLayoutExporter(layout)
        fmt = format.lower()

        if fmt == "pdf":
            settings = QgsLayoutExporter.PdfExportSettings()
            settings.dpi = dpi
            result = exporter.exportToPdf(path, settings)
        elif fmt in ("png", "jpg", "jpeg", "tif", "tiff", "bmp"):
            settings = QgsLayoutExporter.ImageExportSettings()
            settings.dpi = dpi
            result = exporter.exportToImage(path, settings)
        elif fmt == "svg":
            settings = QgsLayoutExporter.SvgExportSettings()
            settings.dpi = dpi
            result = exporter.exportToSvg(path, settings)
        else:
            raise Exception(f"Unsupported format: {format}")

        if result != LAYOUT_SUCCESS:
            raise Exception(f"Export failed with code: {result}")

        return {"ok": True, "path": path}

    # -----------------------------------------------------------------------
    # Phase 3 — Plugin development & system management handlers
    # -----------------------------------------------------------------------

    _LEVEL_MAP: ClassVar[dict[int, str]] = {0: "info", 1: "warning", 2: "critical", 3: "success"}

    def _capture_message(self, message, tag, level, *_extra):
        """Capture a message log entry into the deque.

        QGIS 4.x messageReceivedWithFormat sends a 4th arg (StringFormat);
        *_extra absorbs it so the same handler works for both signals.
        """
        self._message_log.append(
            {
                "tag": tag,
                "message": message,
                "level": self._LEVEL_MAP.get(int(level), str(level)),
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

    def get_3d_screenshot(
        self, view_index=0, dpi=96, pitch=None, distance=None, heading=None, **kwargs
    ):
        """Capture an open 3D Map View as a PNG image.

        The 3D map view is an OpenGL ``QWindow`` that ``QWidget.grab()`` cannot
        read, so this reuses the open view's scene settings + camera pose and
        renders them through a print layout's 3D map item (whose offscreen 3D
        engine runs in C++). Requires a 3D map view to be open
        (View > 3D Map Views > New 3D Map View).

        Optional ``pitch`` (0 = straight down / top-down, 90 = horizontal /
        edge-on; ~45 is a balanced oblique), ``heading`` (compass degrees), and
        ``distance`` (metres) override the captured camera angle without changing
        the live view.
        """
        view_index = int(view_index)
        dpi = max(10, min(int(dpi), 600))  # clamp: bound render cost / memory use
        try:
            from qgis._3d import Qgs3DMapSettings, QgsLayoutItem3DMap
        except ImportError as e:
            raise Exception(
                f"3D support is unavailable in this QGIS build (qgis._3d import failed: {e})"
            ) from e

        views = self.iface.mapCanvases3D()
        if not views:
            raise Exception(
                "No 3D map view is open. Open one via "
                "View > 3D Map Views > New 3D Map View, frame your scene, then retry."
            )
        if view_index < 0 or view_index >= len(views):
            raise ValueError(
                f"view_index {view_index} out of range (open 3D views: {len(views)})"
            )
        canvas3d = views[view_index]

        # Clone the scene settings (copy constructor) so the live view is never
        # mutated or shared, and snapshot its current camera pose.
        map_settings = Qgs3DMapSettings(canvas3d.mapSettings())
        pose = canvas3d.cameraController().cameraPose()

        # Optional camera overrides — applied only to this captured copy, so the
        # live 3D view is left untouched.
        if pitch is not None:
            pose.setPitchAngle(max(0.0, min(float(pitch), 90.0)))
        if heading is not None:
            pose.setHeadingAngle(float(heading) % 360.0)
        if distance is not None and float(distance) > 0:
            pose.setDistanceFromCenterPoint(float(distance))

        # Match the output image to the 3D view's aspect ratio.
        size = canvas3d.size()
        view_w = max(1, size.width())
        view_h = max(1, size.height())
        page_w = 200.0
        page_h = page_w * view_h / view_w

        layout = QgsPrintLayout(QgsProject.instance())
        layout.initializeDefaults()
        layout.pageCollection().page(0).setPageSize(QgsLayoutSize(page_w, page_h))

        item = QgsLayoutItem3DMap(layout)
        item.attemptMove(QgsLayoutPoint(0, 0))
        item.attemptResize(QgsLayoutSize(page_w, page_h))
        item.setMapSettings(map_settings)
        item.setCameraPose(pose)
        layout.addLayoutItem(item)

        tmp = os.path.join(tempfile.gettempdir(), f"mcp_3d_{secrets.token_hex(6)}.png")
        try:
            exporter = QgsLayoutExporter(layout)
            export_settings = QgsLayoutExporter.ImageExportSettings()
            export_settings.dpi = dpi
            result = exporter.exportToImage(tmp, export_settings)
            if int(result) != int(LAYOUT_SUCCESS) or not os.path.exists(tmp):
                raise Exception(f"3D layout export failed (export code {int(result)})")
            with open(tmp, "rb") as fh:
                data = fh.read()
        finally:
            with contextlib.suppress(Exception):
                os.remove(tmp)

        img = QImage()
        img.loadFromData(data, "PNG")
        return {
            "base64_data": base64.b64encode(data).decode("ascii"),
            "mime_type": "image/png",
            "width": img.width(),
            "height": img.height(),
            "view_index": view_index,
            "open_3d_views": len(views),
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

    # -----------------------------------------------------------------------
    # Phase 6 — Extended capabilities
    # -----------------------------------------------------------------------

    def add_web_layer(self, url, service, name=None, crs="EPSG:3857", **kwargs):
        """Add a web layer (XYZ, WMS, WFS) to the project."""
        service = service.lower()
        if service == "xyz":
            uri = f"type=xyz&url={url}"
            layer = QgsRasterLayer(uri, name or "XYZ Layer", "wms")
        elif service == "wms":
            layer = QgsRasterLayer(url, name or "WMS Layer", "wms")
        elif service == "wfs":
            layer = QgsVectorLayer(url, name or "WFS Layer", "WFS")
        else:
            raise Exception(f"Unsupported web service: {service}. Use 'xyz', 'wms', or 'wfs'")

        if not layer.isValid():
            raise Exception(f"Layer is not valid: {url}")

        QgsProject.instance().addMapLayer(layer)
        return {"id": layer.id(), "name": layer.name(), "type": self._get_layer_type(layer)}

    def add_table_join(
        self, target_layer_id, join_layer_id, target_field, join_field, prefix="", **kwargs
    ):
        """Add a table join to a vector layer."""
        target_layer = self._get_vector_layer(target_layer_id)
        join_layer = self._get_vector_layer(join_layer_id)

        join_info = QgsVectorLayerJoinInfo()
        join_info.setTargetFieldName(target_field)
        join_info.setJoinLayerId(join_layer.id())
        join_info.setJoinFieldName(join_field)
        join_info.setUsingMemoryCache(True)
        if prefix:
            join_info.setPrefix(prefix)

        if target_layer.addJoin(join_info):
            return {"ok": True}
        else:
            raise Exception("Failed to add table join")

    def add_field(self, layer_id, field_name, field_type, length=None, precision=None, **kwargs):
        """Add a field to a vector layer."""
        layer = self._get_vector_layer(layer_id)

        type_map = {
            "string": QVAR_STRING,
            "int": QVAR_INT,
            "double": QVAR_DOUBLE,
            "bool": QVAR_BOOL,
            "date": QVAR_DATE,
            "datetime": QVAR_DATETIME,
        }
        v_type = type_map.get(field_type.lower(), QVAR_STRING)
        field = QgsField(field_name, v_type, field_type, length or 0, precision or 0)

        if layer.dataProvider().addAttributes([field]):
            layer.updateFields()
            return {"ok": True, "field_name": field_name}
        else:
            raise Exception(f"Failed to add field: {field_name}")

    def delete_field(self, layer_id, field_name, **kwargs):
        """Delete a field from a vector layer."""
        layer = self._get_vector_layer(layer_id)
        idx = layer.fields().indexOf(field_name)
        if idx < 0:
            raise Exception(f"Field not found: {field_name}")

        if layer.dataProvider().deleteAttributes([idx]):
            layer.updateFields()
            return {"ok": True, "field_name": field_name}
        else:
            raise Exception(f"Failed to delete field: {field_name}")

    def rename_field(self, layer_id, old_name, new_name, **kwargs):
        """Rename a field in a vector layer."""
        layer = self._get_vector_layer(layer_id)
        idx = layer.fields().indexOf(old_name)
        if idx < 0:
            raise Exception(f"Field not found: {old_name}")

        if layer.dataProvider().renameAttributes({idx: new_name}):
            layer.updateFields()
            return {"ok": True, "old_name": old_name, "new_name": new_name}
        else:
            raise Exception(f"Failed to rename field: {old_name}")

    def apply_style_qml(self, layer_id, path, **kwargs):
        """Apply a QML style to a layer."""
        project = QgsProject.instance()
        layer = project.mapLayer(layer_id)
        if not layer:
            raise Exception(f"Layer not found: {layer_id}")

        message, success = layer.loadNamedStyle(path)
        if success:
            layer.triggerRepaint()
            self.iface.layerTreeView().refreshLayerSymbology(layer.id())
            return {"ok": True, "message": message}
        else:
            raise Exception(f"Failed to apply style: {message}")

    def save_style_qml(self, layer_id, path, **kwargs):
        """Save a layer's style to a QML file."""
        project = QgsProject.instance()
        layer = project.mapLayer(layer_id)
        if not layer:
            raise Exception(f"Layer not found: {layer_id}")

        message, success = layer.saveNamedStyle(path)
        if success:
            return {"ok": True, "path": path}
        else:
            raise Exception(f"Failed to save style: {message}")

    def create_layout(self, name, **kwargs):
        """Create a new print layout."""
        project = QgsProject.instance()
        layout = QgsPrintLayout(project)
        layout.initializeDefaults()
        layout.setName(name)
        project.layoutManager().addLayout(layout)
        return {"ok": True, "name": name}

    def add_layout_map(self, layout_name, x, y, width, height, **kwargs):
        """Add a map item to a print layout."""
        manager = QgsProject.instance().layoutManager()
        layout = manager.layoutByName(layout_name)
        if not layout:
            raise Exception(f"Layout not found: {layout_name}")

        map_item = QgsLayoutItemMap(layout)
        map_item.attemptMove(QgsLayoutPoint(x, y))
        map_item.attemptResize(QgsLayoutSize(width, height))
        map_item.zoomToExtent(self.iface.mapCanvas().extent())
        layout.addLayoutItem(map_item)
        return {"ok": True}

    # ------------------------------------------------------------------
    # Layout & atlas authoring (extended)
    # ------------------------------------------------------------------

    def _get_layout(self, layout_name):
        """Get a print layout by name or raise."""
        layout = QgsProject.instance().layoutManager().layoutByName(layout_name)
        if not layout:
            raise Exception(f"Layout not found: {layout_name}")
        return layout

    def _find_layout_map(self, layout, map_item_id=None):
        """Find a map item in a layout by id/uuid, else the first map item."""
        maps = [it for it in layout.items() if isinstance(it, QgsLayoutItemMap)]
        if not maps:
            return None
        if map_item_id:
            for m in maps:
                if m.id() == map_item_id or m.uuid() == map_item_id:
                    return m
        return maps[0]

    def get_layout_info(self, layout_name, **kwargs):
        """List items in a print layout (type, id, position, size)."""
        layout = self._get_layout(layout_name)
        items = []
        for item in layout.items():
            if not hasattr(item, "uuid"):
                continue
            try:
                pos = item.positionWithUnits()
                size = item.sizeWithUnits()
                x, y = pos.x(), pos.y()
                w, h = size.width(), size.height()
            except Exception:
                x = y = w = h = None
            items.append(
                {
                    "id": item.id(),
                    "uuid": item.uuid(),
                    "type": type(item).__name__,
                    "x": x,
                    "y": y,
                    "width": w,
                    "height": h,
                }
            )
        return {
            "layout": layout_name,
            "items": items,
            "count": len(items),
            "page_count": layout.pageCollection().pageCount(),
        }

    def add_layout_label(
        self,
        layout_name,
        text,
        x=10,
        y=10,
        width=100,
        height=20,
        font_size=12,
        color="#000000",
        **kwargs,
    ):
        """Add a text label to a print layout. Supports [% expression %] in text."""
        from qgis.core import QgsLayoutItemLabel

        layout = self._get_layout(layout_name)
        label = QgsLayoutItemLabel(layout)
        label.setText(text)
        label.setFontColor(QColor(color))
        font = label.font()
        font.setPointSize(int(font_size))
        label.setFont(font)
        layout.addLayoutItem(label)
        label.attemptMove(QgsLayoutPoint(x, y))
        label.attemptResize(QgsLayoutSize(width, height))
        return {"ok": True, "uuid": label.uuid()}

    def add_layout_legend(
        self,
        layout_name,
        map_item_id=None,
        x=10,
        y=10,
        width=80,
        height=100,
        title="Legend",
        **kwargs,
    ):
        """Add a legend to a print layout, linked to a map item."""
        from qgis.core import QgsLayoutItemLegend

        layout = self._get_layout(layout_name)
        legend = QgsLayoutItemLegend(layout)
        legend.setTitle(title)
        map_item = self._find_layout_map(layout, map_item_id)
        if map_item:
            legend.setLinkedMap(map_item)
        layout.addLayoutItem(legend)
        legend.attemptMove(QgsLayoutPoint(x, y))
        legend.attemptResize(QgsLayoutSize(width, height))
        return {"ok": True, "uuid": legend.uuid()}

    def add_layout_scalebar(
        self,
        layout_name,
        map_item_id=None,
        x=10,
        y=180,
        width=80,
        height=20,
        style="Single Box",
        **kwargs,
    ):
        """Add a scale bar to a print layout, linked to a map item."""
        from qgis.core import QgsLayoutItemScaleBar

        layout = self._get_layout(layout_name)
        bar = QgsLayoutItemScaleBar(layout)
        bar.setStyle(style)
        map_item = self._find_layout_map(layout, map_item_id)
        if map_item:
            bar.setLinkedMap(map_item)
        bar.applyDefaultSize()
        layout.addLayoutItem(bar)
        bar.attemptMove(QgsLayoutPoint(x, y))
        return {"ok": True, "uuid": bar.uuid()}

    def add_layout_picture(
        self, layout_name, path, x=10, y=10, width=30, height=30, **kwargs
    ):
        """Add a picture/SVG (logo, north arrow) to a print layout."""
        from qgis.core import QgsLayoutItemPicture

        layout = self._get_layout(layout_name)
        pic = QgsLayoutItemPicture(layout)
        pic.setPicturePath(path)
        layout.addLayoutItem(pic)
        pic.attemptMove(QgsLayoutPoint(x, y))
        pic.attemptResize(QgsLayoutSize(width, height))
        return {"ok": True, "uuid": pic.uuid()}

    def add_layout_table(
        self,
        layout_name,
        layer_id,
        x=10,
        y=10,
        width=180,
        height=80,
        max_rows=20,
        **kwargs,
    ):
        """Add an attribute table for a vector layer to a print layout."""
        from qgis.core import QgsLayoutFrame, QgsLayoutItemAttributeTable

        layer = self._get_vector_layer(layer_id)
        layout = self._get_layout(layout_name)
        table = QgsLayoutItemAttributeTable.create(layout)
        table.setVectorLayer(layer)
        table.setMaximumNumberOfFeatures(int(max_rows))
        layout.addMultiFrame(table)
        frame = QgsLayoutFrame(layout, table)
        frame.attemptMove(QgsLayoutPoint(x, y))
        frame.attemptResize(QgsLayoutSize(width, height))
        table.addFrame(frame)
        return {"ok": True, "uuid": frame.uuid()}

    def configure_atlas(
        self,
        layout_name,
        coverage_layer,
        enabled=True,
        page_name_expression=None,
        filter_expression=None,
        sort_expression=None,
        **kwargs,
    ):
        """Configure the atlas of a print layout (coverage layer, filter, sort)."""
        layer = self._get_vector_layer(coverage_layer)
        layout = self._get_layout(layout_name)
        atlas = layout.atlas()
        atlas.setEnabled(bool(enabled))
        atlas.setCoverageLayer(layer)
        if page_name_expression:
            atlas.setPageNameExpression(page_name_expression)
        if filter_expression:
            atlas.setFilterFeatures(True)
            atlas.setFilterExpression(filter_expression)
        if sort_expression:
            atlas.setSortFeatures(True)
            atlas.setSortExpression(sort_expression)
        atlas.updateFeatures()
        return {
            "ok": True,
            "coverage_layer": layer.name(),
            "enabled": bool(enabled),
            "count": atlas.count(),
        }

    def export_atlas(self, layout_name, output_path, format="pdf", dpi=300, **kwargs):
        """Export an atlas: single multi-page PDF, or one image file per feature."""
        import os

        layout = self._get_layout(layout_name)
        atlas = layout.atlas()
        if not atlas.enabled():
            raise Exception("Atlas not enabled; call configure_atlas first")
        atlas.updateFeatures()
        fmt = format.lower()
        if fmt == "pdf":
            settings = QgsLayoutExporter.PdfExportSettings()
            settings.dpi = dpi
            result, error = QgsLayoutExporter.exportToPdf(atlas, output_path, settings)
        elif fmt in ("png", "jpg", "jpeg", "tif", "tiff"):
            os.makedirs(output_path, exist_ok=True)
            settings = QgsLayoutExporter.ImageExportSettings()
            settings.dpi = dpi
            base = os.path.join(output_path, layout_name)
            result, error = QgsLayoutExporter.exportToImage(atlas, base, fmt, settings)
        else:
            raise Exception(f"Unsupported atlas format: {format}")
        if result != LAYOUT_SUCCESS:
            raise Exception(f"Atlas export failed: {error}")
        return {"ok": True, "output": output_path, "count": atlas.count()}

    def remove_layout(self, layout_name, **kwargs):
        """Remove a print layout from the project."""
        manager = QgsProject.instance().layoutManager()
        layout = manager.layoutByName(layout_name)
        if not layout:
            raise Exception(f"Layout not found: {layout_name}")
        manager.removeLayout(layout)
        return {"ok": True, "removed": layout_name}

    # ------------------------------------------------------------------
    # Query, expression & layer management (extended)
    # ------------------------------------------------------------------

    def execute_sql(
        self,
        query,
        layers=None,
        as_layer=False,
        layer_name="sql_result",
        geometry_field=None,
        uid_field=None,
        **kwargs,
    ):
        """Run SQL across loaded layers via a virtual layer. Reference layers by name."""
        from qgis.core import QgsVirtualLayerDefinition

        project = QgsProject.instance()
        definition = QgsVirtualLayerDefinition()
        src_ids = layers or list(project.mapLayers().keys())
        for lid in src_ids:
            lyr = project.mapLayer(lid)
            if lyr is None:
                raise Exception(f"Layer not found: {lid}")
            definition.addSource(lyr.name(), lid)
        definition.setQuery(query)
        if geometry_field:
            definition.setGeometryField(geometry_field)
        else:
            definition.setGeometryWkbType(WKB_NO_GEOMETRY)
        if uid_field:
            definition.setUid(uid_field)
        vlayer = QgsVectorLayer(definition.toString(), layer_name, "virtual")
        if not vlayer.isValid():
            raise Exception(f"Invalid SQL/virtual layer for query: {query}")
        if as_layer:
            project.addMapLayer(vlayer)
            return {
                "output_layer_id": vlayer.id(),
                "name": vlayer.name(),
                "feature_count": vlayer.featureCount(),
            }
        fields = [f.name() for f in vlayer.fields()]
        rows = []
        for i, feat in enumerate(vlayer.getFeatures()):
            if i >= 1000:
                break
            rows.append({fn: feat[fn] for fn in fields})
        return {"fields": fields, "rows": rows, "count": len(rows)}

    def evaluate_expression(self, expression, layer_id=None, **kwargs):
        """Evaluate a standalone QGIS expression to a scalar value."""
        exp = QgsExpression(expression)
        context = QgsExpressionContext()
        context.appendScope(QgsExpressionContextUtils.globalScope())
        context.appendScope(
            QgsExpressionContextUtils.projectScope(QgsProject.instance())
        )
        if layer_id:
            layer = self._get_vector_layer(layer_id)
            context.appendScope(QgsExpressionContextUtils.layerScope(layer))
        value = exp.evaluate(context)
        if exp.hasParserError():
            raise Exception(f"Parser error: {exp.parserErrorString()}")
        if exp.hasEvalError():
            raise Exception(f"Eval error: {exp.evalErrorString()}")
        return {"expression": expression, "result": value}

    def identify_features(
        self, point, tolerance=0.0, layer_ids=None, limit=10, **kwargs
    ):
        """Identify features at a point [x, y] (project CRS) across layers."""
        project = QgsProject.instance()
        x, y = float(point[0]), float(point[1])
        pt_geom = QgsGeometry.fromPointXY(QgsPointXY(x, y))
        if layer_ids:
            targets = [project.mapLayer(lid) for lid in layer_ids]
        else:
            targets = [
                n.layer() for n in project.layerTreeRoot().findLayers() if n.isVisible()
            ]
        prefilter = QgsRectangle(
            x - tolerance, y - tolerance, x + tolerance, y + tolerance
        )
        results = []
        for layer in targets:
            if layer is None or layer.type() != LAYER_VECTOR:
                continue
            req = QgsFeatureRequest().setFilterRect(prefilter)
            feats = []
            for feat in layer.getFeatures(req):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                if tolerance > 0:
                    if geom.distance(pt_geom) > tolerance:
                        continue
                elif not geom.intersects(pt_geom):
                    continue
                attrs = {f.name(): feat[f.name()] for f in layer.fields()}
                attrs["_fid"] = feat.id()
                feats.append(attrs)
                if len(feats) >= limit:
                    break
            if feats:
                results.append(
                    {
                        "layer_id": layer.id(),
                        "name": layer.name(),
                        "features": feats,
                        "count": len(feats),
                    }
                )
        return {"point": [x, y], "results": results}

    def duplicate_layer(self, layer_id, new_name=None, **kwargs):
        """Duplicate a layer (with its style) under a new name."""
        project = QgsProject.instance()
        if layer_id not in project.mapLayers():
            raise Exception(f"Layer not found: {layer_id}")
        layer = project.mapLayer(layer_id)
        clone = layer.clone()
        clone.setName(new_name or f"{layer.name()} copy")
        project.addMapLayer(clone)
        return {"ok": True, "output_layer_id": clone.id(), "name": clone.name()}

    def set_layer_order(self, layer_ids, **kwargs):
        """Reorder layer tree nodes (top to bottom); tree order is draw order.

        Listed layers are rearranged into the given order within their common
        parent; unlisted siblings keep their slots. Deliberately does NOT use
        QGIS's custom draw order — that freezes a snapshot list, so any layer
        added afterwards would silently draw behind everything; any existing
        custom order is cleared for the same reason.
        """
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        nodes = []
        for lid in layer_ids:
            node = root.findLayer(lid)
            if node is None:
                raise Exception(f"Layer not found in layer tree: {lid}")
            nodes.append(node)
        parent = nodes[0].parent()
        for node in nodes[1:]:
            if node.parent() is not parent:
                raise Exception("All layers must be in the same group to reorder")

        siblings = list(parent.children())
        slots = sorted(siblings.index(n) for n in nodes)
        new_order = list(siblings)
        for slot, node in zip(slots, nodes, strict=True):
            new_order[slot] = node
        clones = [n.clone() for n in new_order]
        parent.insertChildNodes(0, clones)
        for node in siblings:
            parent.removeChildNode(node)

        root.setHasCustomLayerOrder(False)
        return {"ok": True, "order": layer_ids}

    # ------------------------------------------------------------------
    # Processing framework (extended)
    # ------------------------------------------------------------------

    def list_processing_models(self, **kwargs):
        """List registered Processing models (provider 'model')."""
        registry = QgsApplication.processingRegistry()
        models = []
        for alg in registry.algorithms():
            if alg.provider().id() == "model":
                models.append(
                    {"id": alg.id(), "name": alg.displayName(), "group": alg.group()}
                )
        return {"models": models, "count": len(models)}

    def run_model(self, model, parameters=None, **kwargs):
        """Run a Processing model by registered id or by .model3 file path."""
        import processing

        parameters = parameters or {}
        if isinstance(model, str) and model.lower().endswith(".model3"):
            alg = QgsProcessingModelAlgorithm()
            if not alg.fromFile(model):
                raise Exception(f"Failed to load model file: {model}")
            alg.initAlgorithm()
            target = alg
        else:
            target = model
        result = processing.run(target, parameters)
        return {"model": model, "result": {k: str(v) for k, v in result.items()}}

    def get_processing_providers(self, **kwargs):
        """List Processing providers with algorithm counts and active status."""
        registry = QgsApplication.processingRegistry()
        providers = []
        for p in registry.providers():
            info = {
                "id": p.id(),
                "name": p.name(),
                "algorithm_count": len(p.algorithms()),
            }
            with contextlib.suppress(Exception):
                info["active"] = bool(p.isActive())
            providers.append(info)
        return {"providers": providers, "count": len(providers)}

    def execute_processing_batch(self, algorithm, parameters_list, **kwargs):
        """Run the same algorithm once per parameter dict; collect per-run results."""
        import processing

        results = []
        for i, params in enumerate(parameters_list):
            try:
                r = processing.run(algorithm, params)
                results.append(
                    {
                        "index": i,
                        "status": "success",
                        "result": {k: str(v) for k, v in r.items()},
                    }
                )
            except Exception as e:
                results.append({"index": i, "status": "error", "message": str(e)})
        return {"algorithm": algorithm, "results": results, "count": len(results)}

    # ------------------------------------------------------------------
    # Raster compute
    # ------------------------------------------------------------------

    def raster_calculator(self, expression, output_path, reference_layer=None, **kwargs):
        """Band math via QgsRasterCalculator. Reference loaded rasters as 'name@band'."""
        from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry

        project = QgsProject.instance()
        entries = []
        ref = None
        rasters = []
        for lid, layer in project.mapLayers().items():
            if layer.type() != LAYER_RASTER:
                continue
            rasters.append(layer)
            for band in range(1, layer.bandCount() + 1):
                e = QgsRasterCalculatorEntry()
                e.ref = f"{layer.name()}@{band}"
                e.raster = layer
                e.bandNumber = band
                entries.append(e)
            if reference_layer and reference_layer in (lid, layer.name()):
                ref = layer
        if ref is None:
            if not rasters:
                raise Exception("No raster layers loaded to compute from")
            ref = rasters[0]

        extent = ref.extent()
        cols = ref.width()
        rows = ref.height()
        try:
            calc = QgsRasterCalculator(
                expression, output_path, "GTiff", extent, cols, rows, entries,
                project.transformContext(),
            )
        except TypeError:
            calc = QgsRasterCalculator(
                expression, output_path, "GTiff", extent, cols, rows, entries
            )
        res = calc.processCalculation()
        if int(res) != 0:
            raise Exception(f"Raster calculation failed (code {int(res)})")
        return {"ok": True, "output": output_path, "reference_layer": ref.name()}

    def zonal_statistics(
        self, polygon_layer, raster_layer, band=1, prefix="_", stats=None,
        output_path=None, **kwargs,
    ):
        """Per-polygon raster statistics (native:zonalstatisticsfb).

        stats: list of int codes (0=count,1=sum,2=mean,3=median,4=stdev,5=min,
        6=max,7=range,8=minority,9=majority,10=variety,11=variance).
        """
        import processing

        poly = self._get_vector_layer(polygon_layer)
        rast = self._resolve_raster_layer(raster_layer)
        params = {
            "INPUT": poly,
            "INPUT_RASTER": rast,
            "RASTER_BAND": band,
            "COLUMN_PREFIX": prefix,
            "STATISTICS": stats or [0, 1, 2],
            "OUTPUT": output_path or "memory:zonal_stats",
        }
        r = processing.run("native:zonalstatisticsfb", params)
        return self._register_output(r["OUTPUT"], "zonal_stats")

    def sample_raster_values(self, raster_layer, points, band=None, **kwargs):
        """Sample raster values at points [[x, y], ...] in the raster's CRS."""
        layer = self._resolve_raster_layer(raster_layer)
        dp = layer.dataProvider()
        results = []
        for pt in points:
            p = QgsPointXY(pt[0], pt[1])
            if band:
                val, ok = dp.sample(p, band)
                results.append(
                    {"x": pt[0], "y": pt[1], "band": band, "value": val if ok else None}
                )
            else:
                vals = {}
                for b in range(1, layer.bandCount() + 1):
                    v, ok = dp.sample(p, b)
                    vals[b] = v if ok else None
                results.append({"x": pt[0], "y": pt[1], "values": vals})
        return {"samples": results, "count": len(results)}

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_layer(
        self, layer_id, output_path, target_crs=None, filter_expression=None, **kwargs
    ):
        """Export a vector/raster layer to disk. target_crs reprojects; format by extension."""
        import processing

        project = QgsProject.instance()
        if layer_id not in project.mapLayers():
            raise Exception(f"Layer not found: {layer_id}")
        layer = project.mapLayer(layer_id)

        if layer.type() == LAYER_VECTOR:
            src = layer
            if filter_expression:
                r = processing.run(
                    "native:extractbyexpression",
                    {"INPUT": layer, "EXPRESSION": filter_expression, "OUTPUT": "memory:"},
                )
                src = r["OUTPUT"]
            if target_crs:
                processing.run(
                    "native:reprojectlayer",
                    {"INPUT": src, "TARGET_CRS": target_crs, "OUTPUT": output_path},
                )
            else:
                processing.run("native:savefeatures", {"INPUT": src, "OUTPUT": output_path})
            return {"ok": True, "output": output_path}

        if layer.type() == LAYER_RASTER:
            if target_crs:
                processing.run(
                    "gdal:warpreproject",
                    {"INPUT": layer, "TARGET_CRS": target_crs, "OUTPUT": output_path},
                )
            else:
                processing.run("gdal:translate", {"INPUT": layer, "OUTPUT": output_path})
            return {"ok": True, "output": output_path}

        raise Exception(f"Unsupported layer type for export: {layer_id}")

    # ------------------------------------------------------------------
    # Vector helpers
    # ------------------------------------------------------------------

    def field_calculator(
        self, layer_id, field_name, expression, field_type="double",
        length=0, precision=0, **kwargs,
    ):
        """Add (if missing) and populate a field from a QGIS expression, in-place."""
        layer = self._get_vector_layer(layer_id)
        type_map = {
            "string": QVAR_STRING,
            "int": QVAR_INT,
            "double": QVAR_DOUBLE,
            "bool": QVAR_BOOL,
            "date": QVAR_DATE,
            "datetime": QVAR_DATETIME,
        }
        idx = layer.fields().indexOf(field_name)
        created = False
        if idx < 0:
            v_type = type_map.get(field_type.lower(), QVAR_DOUBLE)
            layer.dataProvider().addAttributes(
                [QgsField(field_name, v_type, field_type, length, precision)]
            )
            layer.updateFields()
            idx = layer.fields().indexOf(field_name)
            created = True

        expr = QgsExpression(expression)
        if expr.hasParserError():
            raise Exception(f"Expression parse error: {expr.parserErrorString()}")
        ctx = QgsExpressionContext()
        ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        expr.prepare(ctx)

        if not layer.startEditing():
            raise Exception("Could not start editing layer")
        updated = 0
        for feat in layer.getFeatures():
            ctx.setFeature(feat)
            val = expr.evaluate(ctx)
            if expr.hasEvalError():
                continue
            layer.changeAttributeValue(feat.id(), idx, val)
            updated += 1
        if not layer.commitChanges():
            errs = "; ".join(layer.commitErrors())
            raise Exception(f"Commit failed: {errs}")
        return {"ok": True, "field_name": field_name, "created": created, "updated": updated}

    def get_unique_values(self, layer_id, field, limit=1000, **kwargs):
        """Return distinct values of a field (limit -1 for all)."""
        layer = self._get_vector_layer(layer_id)
        idx = layer.fields().indexOf(field)
        if idx < 0:
            raise Exception(f"Field not found: {field}")
        raw = layer.uniqueValues(idx, limit)
        values = [v for v in raw if v is not None and str(v) != "NULL"]
        with contextlib.suppress(TypeError):
            values = sorted(values, key=lambda x: (str(type(x)), x))
        return {"field": field, "values": values, "count": len(values)}

    def spatial_join(
        self, target_layer, join_layer, predicates=None, join_fields=None,
        method=1, prefix="", output_path=None, **kwargs,
    ):
        """Join attributes by location (native:joinattributesbylocation).

        predicates: list of int (0=intersects,1=contains,2=equals,3=touches,
        4=overlaps,5=within,6=crosses). method: 0=one-to-many, 1=first match,
        2=largest overlap.
        """
        import processing

        target = self._get_vector_layer(target_layer)
        join = self._get_vector_layer(join_layer)
        params = {
            "INPUT": target,
            "JOIN": join,
            "PREDICATE": predicates or [0],
            "JOIN_FIELDS": join_fields or [],
            "METHOD": method,
            "PREFIX": prefix,
            "OUTPUT": output_path or "memory:joined",
        }
        r = processing.run("native:joinattributesbylocation", params)
        return self._register_output(r["OUTPUT"], "joined")

    # ------------------------------------------------------------------
    # Shared helpers for the tools above
    # ------------------------------------------------------------------

    def _resolve_raster_layer(self, layer_id):
        """Get a raster layer by id or raise."""
        project = QgsProject.instance()
        if layer_id not in project.mapLayers():
            raise Exception(f"Layer not found: {layer_id}")
        layer = project.mapLayer(layer_id)
        if layer.type() != LAYER_RASTER:
            raise Exception(f"Not a raster layer: {layer_id}")
        return layer

    def _register_output(self, out, default_name):
        """Add a processing output layer to the project, or report a file path."""
        if isinstance(out, str):
            return {"output": out}
        out.setName(default_name)
        QgsProject.instance().addMapLayer(out)
        return {"output_layer_id": out.id(), "name": out.name()}


def _client_config_registry(repo_dir):
    """Map client name -> {path, key} (or {print_only}) for MCP config files.

    Shared by the configurator dialog and the stale-config migration check.
    """
    home = Path.home()
    appdata = (
        Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        if sys.platform == "win32"
        else None
    )

    if sys.platform == "darwin":
        claude_cfg = (
            home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        )
    elif sys.platform == "win32":
        claude_cfg = appdata / "Claude" / "claude_desktop_config.json"
    else:
        claude_cfg = home / ".config" / "Claude" / "claude_desktop_config.json"

    cursor_cfg = home / ".cursor" / "mcp.json"
    windsurf_cfg = home / ".windsurf" / "mcp.json"
    vscode_cfg = repo_dir / ".vscode" / "mcp.json"

    if sys.platform == "win32":
        zed_cfg = appdata / "Zed" / "settings.json"
    else:
        zed_cfg = home / ".config" / "zed" / "settings.json"

    opencode_cfg = home / ".config" / "opencode" / "opencode.json"

    # Hermes desktop app (Windows) - YAML-based config, handled as print_only
    hermes_cfg = (
        appdata / "Hermes" / "config.yaml"
        if sys.platform == "win32" and appdata
        else None
    )

    return {
        "claude-desktop": {"path": claude_cfg, "key": "mcpServers"},
        "cursor": {"path": cursor_cfg, "key": "mcpServers"},
        "vscode": {"path": vscode_cfg, "key": "mcpServers", "project_local": True},
        "windsurf": {"path": windsurf_cfg, "key": "mcpServers"},
        "zed": {"path": zed_cfg, "key": "context_servers"},
        "opencode": {"path": opencode_cfg, "key": "mcp"},
        "claude-code": {"print_only": True},
        "hermes": {"print_only": True, "entry_format": "hermes", "hermes_cfg": hermes_cfg},
    }


def _qgis_entry_command_args(entry):
    """Return (command, args) for a 'qgis' server entry.
    """
    if not isinstance(entry, dict):
        return None, []
    cmd = entry.get("command")
    return cmd, entry.get("args", [])


def _qgis_entry_has_refresh(entry):
    """True when a remote uvx 'qgis' entry has --refresh-package (fails offline)."""
    command, args = _qgis_entry_command_args(entry)
    if command != "uvx" or "qgis-mcp-server" not in args:
        return False  # local mode / unknown — leave alone
    return "--refresh-package" in args


def _remove_refresh_from_entry(entry):
    """Remove '--refresh-package qgis-mcp' from a uvx 'qgis' entry."""
    cmd = entry.get("command")
    args = cmd.get("args", []) if isinstance(cmd, dict) else entry.get("args", [])
    try:
        idx = args.index("--refresh-package")
        del args[idx:idx + 2]
    except ValueError:
        pass
    if isinstance(cmd, dict):
        cmd["args"] = args
    else:
        entry["args"] = args
    return entry


class MCPConfiguratorDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("QGIS MCP — Setup & Configurator")
        self.setMinimumSize(600, 500)

        self.repo_dir = Path(__file__).resolve().parent.parent
        # Zip archive instead of git+ URL: uvx then needs no git executable,
        # which is not visible to GUI-spawned MCP servers (e.g. Claude Desktop
        # on Windows).
        self.github_url = "https://github.com/nkarasiak/qgis-mcp/archive/refs/heads/main.zip"

        self.init_ui()
        self.refresh_status()

    def init_ui(self):
        self.setStyleSheet(
            "QGroupBox {"
            "  font-weight: bold;"
            "  border: 1px solid palette(mid);"
            "  border-radius: 6px;"
            "  margin-top: 10px;"
            "  padding: 10px 10px 8px 10px;"
            "}"
            "QGroupBox::title {"
            "  subcontrol-origin: margin;"
            "  subcontrol-position: top left;"
            "  left: 8px;"
            "  padding: 0 4px;"
            "}"
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # ── Header (logo + title) ────────────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(10)
        logo = QLabel()
        icon_path = os.path.join(os.path.dirname(__file__), "icons", "icon.png")
        logo.setPixmap(QIcon(icon_path).pixmap(QSize(44, 44)))
        header.addWidget(logo)
        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        heading = QLabel("QGIS MCP")
        heading.setStyleSheet("font-size: 17px; font-weight: bold;")
        subtitle = QLabel("Connect your AI client to QGIS")
        subtitle.setStyleSheet("color: palette(mid);")
        title_col.addWidget(heading)
        title_col.addWidget(subtitle)
        header.addLayout(title_col)
        header.addStretch()
        layout.addLayout(header)

        # ── Step 1: client + options ─────────────────────────────────
        client_group = QGroupBox("1  ·  AI client")
        client_form = QVBoxLayout(client_group)
        client_form.setSpacing(8)

        client_row = QHBoxLayout()
        client_row.addWidget(QLabel("Client:"))
        self.client_combo = QComboBox()
        self.client_combo.addItems(
            ["claude-code", "claude-desktop", "cursor", "hermes", "opencode", "vscode", "windsurf", "zed"]
        )
        self.client_combo.setMinimumWidth(180)
        self.client_combo.currentTextChanged.connect(self._on_client_changed)
        client_row.addWidget(self.client_combo)

        # Mode selector — only relevant for dev installs with a local clone
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Remote (uvx — recommended)", "Local (uv run)"])
        self.mode_combo.setToolTip(
            "Remote: install MCP server on-the-fly via uvx (no clone needed).\n"
            "Local: run MCP server from your git clone via uv."
        )
        self.mode_combo.currentTextChanged.connect(self._on_client_changed)
        self.mode_combo.setVisible(self._is_dev_install())
        client_row.addWidget(self.mode_combo)
        client_row.addStretch()
        client_form.addLayout(client_row)

        # Refresh toggle — adds `--refresh-package qgis-mcp` so uvx re-pulls the
        # latest server from GitHub on every launch (remote mode only).
        self.refresh_check = QCheckBox("Always pull latest server from GitHub")
        self.refresh_check.setToolTip(
            "Add --refresh-package qgis-mcp so uvx re-pulls the latest server from\n"
            "GitHub on every client launch (stays in sync with the plugin).\n"
            "Warning: requires network at launch — the server fails to start offline.\n"
            "Leave unchecked to use the cached version (works offline, manual updates)."
        )
        self.refresh_check.setChecked(False)
        self.refresh_check.toggled.connect(self._on_client_changed)
        client_form.addWidget(self.refresh_check)

        self.autostart_check = QCheckBox("Start MCP server automatically when QGIS opens")
        self.autostart_check.setToolTip(
            "Launch the MCP server on QGIS startup so an AI agent can reconnect\n"
            "without manually starting it (e.g. after a crash and restart)."
        )
        self.autostart_check.setChecked(
            QgsSettings().value(f"{QgisMCPPlugin.SETTINGS_PREFIX}/autostart", False, type=bool)
        )
        self.autostart_check.toggled.connect(self._save_autostart)
        client_form.addWidget(self.autostart_check)
        layout.addWidget(client_group)

        # ── Step 2: configuration preview ────────────────────────────
        preview_group = QGroupBox("2  ·  Configuration")
        preview_box = QVBoxLayout(preview_group)
        preview_box.setSpacing(6)

        self.preview_label = QLabel("Add to your client config file:")
        preview_box.addWidget(self.preview_label)

        preview_row = QHBoxLayout()
        self.preview_edit = QPlainTextEdit()
        self.preview_edit.setReadOnly(True)
        self.preview_edit.setMaximumHeight(160)
        self.preview_edit.setStyleSheet("font-family: monospace;")
        preview_row.addWidget(self.preview_edit)

        copy_col = QVBoxLayout()
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setFixedWidth(64)
        self.copy_btn.clicked.connect(self._copy_preview)
        copy_col.addWidget(self.copy_btn)
        copy_col.addStretch()
        preview_row.addLayout(copy_col)
        preview_box.addLayout(preview_row)

        self.status_label = QLabel()
        preview_box.addWidget(self.status_label)
        layout.addWidget(preview_group)

        layout.addStretch()

        # ── Actions ──────────────────────────────────────────────────
        action_row = QHBoxLayout()
        self.apply_btn = QPushButton("Apply Config")
        self.apply_btn.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: white; font-weight: bold; padding: 6px 16px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #43A047; }"
            "QPushButton:disabled { background-color: #aaa; }"
        )
        self.apply_btn.clicked.connect(self.run_config)
        action_row.addWidget(self.apply_btn)
        action_row.addStretch()
        github_btn = QPushButton("Open GitHub")
        github_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://github.com/nkarasiak/qgis-mcp"))
        )
        action_row.addWidget(github_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        action_row.addWidget(close_btn)
        layout.addLayout(action_row)

    def _is_dev_install(self):
        """True when the plugin is running from a git-cloned repository."""
        return (self.repo_dir / ".git").exists()

    def _save_autostart(self, checked):
        """Persist the auto-start-on-QGIS-startup preference."""
        QgsSettings().setValue(f"{QgisMCPPlugin.SETTINGS_PREFIX}/autostart", checked)

    def _find_uv(self):
        """Return uv executable path, checking common Windows install locations."""
        uv = shutil.which("uv")
        if uv:
            return uv
        if sys.platform == "win32":
            candidates = [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "uv.exe",
                Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "uv.exe",
                Path(os.environ.get("USERPROFILE", "")) / ".cargo" / "bin" / "uv.exe",
            ]
            for p in candidates:
                if p.exists():
                    return str(p)
        return None

    def _on_client_changed(self):
        self.refresh_status()

    def _copy_preview(self):
        QgsApplication.clipboard().setText(self.preview_edit.toPlainText())
        self.copy_btn.setText("Copied!")
        QTimer.singleShot(1500, lambda: self.copy_btn.setText("Copy"))

    def _get_client_info(self, client_name):
        return _client_config_registry(self.repo_dir).get(client_name)

    def _get_server_entry(self, client, remote, refresh=False):
        if remote:
            args = ["--from", self.github_url, "qgis-mcp-server"]
            if refresh:
                args = ["--refresh-package", "qgis-mcp", *args]
            entry = {
                "command": "uvx",
                "args": args,
            }
        else:
            uv = self._find_uv()
            if uv:
                entry = {
                    "command": uv,
                    "args": [
                        "--directory", str(self.repo_dir),
                        "run", "--no-sync", "src/qgis_mcp/server.py",
                    ],
                }
            else:
                if sys.platform == "win32":
                    python = self.repo_dir / ".venv" / "Scripts" / "python.exe"
                else:
                    python = self.repo_dir / ".venv" / "bin" / "python"
                entry = {
                    "command": str(python),
                    "args": [str(self.repo_dir / "src" / "qgis_mcp" / "server.py")],
                }

        if client == "opencode":
            return {
                "type": "local",
                "command": [entry["command"], *entry["args"]],
            }
        return entry

    def _hermes_preview_text(self, remote: bool) -> str:
        """Return the full setup instructions for Hermes desktop app (Windows).

        Note: the bat-file content here mirrors install.py's _hermes_bat_content().
        The plugin cannot import install.py (it runs inside QGIS), so the logic is
        intentionally duplicated to keep the plugin self-contained.
        """
        home = Path.home()
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        hermes_dir = appdata / "Hermes"
        bat_path = hermes_dir / "qgis-mcp-launch.bat"
        cfg_path = hermes_dir / "config.yaml"

        if remote:
            launch_cmd = f'uvx --from "{self.github_url}" qgis-mcp-server'
        else:
            uv = self._find_uv() or "uv"
            launch_cmd = f'"{uv}" --directory "{self.repo_dir}" run --no-sync src/qgis_mcp/server.py'

        bat_lines = [
            "@echo off",
            "REM Launch qgis-mcp-server isolated from Hermes's own Python venv.",
            "REM Clearing venv vars prevents Hermes's pydantic/mcp from being imported.",
            "set VIRTUAL_ENV=",
            "set PYTHONPATH=",
            "set PYTHONHOME=",
            launch_cmd,
        ]
        bat_content = "\n".join(bat_lines)

        bat_escaped = str(bat_path).replace("\\", "\\\\")
        yaml_block = (
            "mcpServers:\n"
            "  qgis:\n"
            f'    command: "{bat_escaped}"\n'
            "    args: []"
        )

        return (
            f"Step 1 — Create this file:\n"
            f"  {bat_path}\n\n"
            f"Contents:\n"
            f"{bat_content}\n\n"
            f"Step 2 — Add to:\n"
            f"  {cfg_path}\n\n"
            f"{yaml_block}\n\n"
            f"See docs/agent-integration.md for full details."
        )

    def update_preview(self):
        client = self.client_combo.currentText()
        remote = self.mode_combo.currentText().startswith("Remote")
        refresh = remote and self.refresh_check.isChecked()
        # Refresh only applies to remote (uvx) mode.
        self.refresh_check.setEnabled(remote)
        info = self._get_client_info(client)

        if info.get("entry_format") == "hermes":
            self.preview_label.setText(
                "Manual setup required — copy the .bat content and YAML config below:"
            )
            self.preview_edit.setPlainText(self._hermes_preview_text(remote))
            return

        if info.get("print_only"):
            if remote:
                refresh_flag = "--refresh-package qgis-mcp " if refresh else ""
                cmd = f'claude mcp add qgis -- uvx {refresh_flag}--from "{self.github_url}" qgis-mcp-server'
            else:
                uv = self._find_uv() or "uv"
                cmd = (
                    f'claude mcp add -s user qgis -- '
                    f'"{uv}" --directory "{self.repo_dir}" run --no-sync src/qgis_mcp/server.py'
                )
            self.preview_label.setText("Run this command in your terminal:")
            self.preview_edit.setPlainText(cmd)
            return

        self.preview_label.setText("Add to your client config file:")
        entry = self._get_server_entry(client, remote, refresh)
        self.preview_edit.setPlainText(json.dumps({"qgis": entry}, indent=2))

    def refresh_status(self):
        client = self.client_combo.currentText()
        info = self._get_client_info(client)

        if info.get("entry_format") == "hermes":
            hermes_cfg = info.get("hermes_cfg")
            if hermes_cfg and hermes_cfg.exists():
                self.status_label.setText(
                    f"Status: config.yaml found — verify 'qgis' is in mcpServers ({hermes_cfg})"
                )
                self.status_label.setStyleSheet("color: orange;")
            else:
                cfg_hint = str(hermes_cfg) if hermes_cfg else "%APPDATA%\\Hermes\\config.yaml"
                self.status_label.setText(
                    f"Status: Follow the steps below, then edit {cfg_hint}"
                )
                self.status_label.setStyleSheet("color: gray;")
            self.apply_btn.setEnabled(False)
            self.update_preview()
            return

        if info.get("print_only"):
            self.status_label.setText("Run the command above in your terminal.")
            self.status_label.setStyleSheet("color: gray;")
            self.apply_btn.setEnabled(False)
            self.update_preview()
            return

        self.apply_btn.setEnabled(True)

        path = info["path"]
        key = info["key"]

        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if key in data and "qgis" in data[key]:
                    self.status_label.setText(f"Status: Configured in {path.name}")
                    self.status_label.setStyleSheet("color: green;")
                else:
                    self.status_label.setText(f"Status: Not configured in {path.name}")
                    self.status_label.setStyleSheet("color: orange;")
            except Exception as e:
                self.status_label.setText(f"Status: Error reading config: {e}")
                self.status_label.setStyleSheet("color: red;")
        else:
            self.status_label.setText(f"Status: Config file not found ({path.name})")
            self.status_label.setStyleSheet("color: gray;")

        self.update_preview()

    def run_config(self):
        client = self.client_combo.currentText()
        remote = self.mode_combo.currentText().startswith("Remote")
        refresh = remote and self.refresh_check.isChecked()
        info = self._get_client_info(client)

        if info.get("print_only"):
            return

        path = info["path"]
        key = info["key"]
        entry = self._get_server_entry(client, remote, refresh)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = {}

            data.setdefault(key, {})
            data[key]["qgis"] = entry

            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")

            self.refresh_status()
            QgsMessageLog.logMessage(f"Configured {client} at {path}", "MCP", MSG_INFO)
        except Exception as e:
            QgsMessageLog.logMessage(f"Failed to configure {client}: {e}", "MCP", MSG_CRITICAL)
            self.status_label.setText(f"Status: Failed to write: {e}")
            self.status_label.setStyleSheet("color: red;")


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

        configure_action = QAction("Configure…", self.iface.mainWindow())
        configure_action.triggered.connect(self._show_help)

        menu = QMenu()
        menu.addAction(port_wa)
        menu.addAction(autostart_wa)
        menu.addSeparator()
        menu.addAction(configure_action)

        # Tool button with dropdown (like Plugin Reloader)
        self.tool_button = QToolButton()
        self.tool_button.setDefaultAction(self.action)
        self.tool_button.setMenu(menu)
        self.tool_button.setPopupMode(TOOLBUTTON_MENU_POPUP)
        self.tool_button.setToolButtonStyle(TOOLBUTTON_ICON_ONLY)
        self._toolbar_action = toolbar.addWidget(self.tool_button)

        self.help_action = QAction(self._logo_icon(), "MCP Setup Configurator", self.iface.mainWindow())
        self.help_action.triggered.connect(self._show_help)

        self.iface.addPluginToMenu("QGIS MCP", self.action)
        self.iface.addPluginToMenu("QGIS MCP", self.help_action)

        # Set the icon on the "QGIS MCP" submenu itself (top-level entry)
        for sub in self.iface.pluginMenu().actions():
            if sub.text() == "QGIS MCP" and sub.menu():
                sub.setIcon(self._logo_icon())
                break

        # Restore saved port
        saved_port = settings.value(f"{self.SETTINGS_PREFIX}/port", _DEFAULT_PORT, type=int)
        self.port_spin.setValue(saved_port)

        # Auto-start if enabled
        if self.autostart_cb.isChecked():
            self.action.setChecked(True)
            self.toggle_server(True)

        # Proactive Welcome / Setup check
        QTimer.singleShot(1000, self._proactive_setup_check)
        QTimer.singleShot(1500, self._check_stale_mcp_configs)

    def _proactive_setup_check(self):
        """Show a welcome dialog on first install."""
        settings = QgsSettings()
        first_run = settings.value(f"{self.SETTINGS_PREFIX}/first_run", True, type=bool)
        if not first_run:
            return
        settings.setValue(f"{self.SETTINGS_PREFIX}/first_run", False)

        dlg = QDialog(self.iface.mainWindow())
        dlg.setWindowTitle("Welcome to QGIS MCP")
        dlg.setMinimumWidth(420)
        layout = QVBoxLayout(dlg)

        title = QLabel("<h2>QGIS MCP installed!</h2>")
        layout.addWidget(title)

        body = QLabel(
            "<p>This plugin lets Claude (and other LLMs) control QGIS directly "
            "via the Model Context Protocol.</p>"
            "<p><b>Quick start:</b></p>"
            "<ol>"
            "<li>Click the MCP toolbar icon → <b>Start Server</b></li>"
            "<li>Open <b>Configure…</b> in the same menu to connect your AI client</li>"
            "<li>Ask Claude to work with your QGIS project</li>"
            "</ol>"
        )
        body.setWordWrap(True)
        body.setOpenExternalLinks(True)
        layout.addWidget(body)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        github_btn = QPushButton("Open GitHub")
        github_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://github.com/nkarasiak/qgis-mcp"))
        )
        configure_btn = QPushButton("Open Configurator")
        configure_btn.clicked.connect(dlg.accept)
        configure_btn.clicked.connect(self._show_help)
        configure_btn.setDefault(True)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.reject)

        btn_layout.addWidget(github_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        btn_layout.addWidget(configure_btn)
        layout.addLayout(btn_layout)

        dlg.exec()

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

    def _badge_icon(self, count):
        """Green logo with a notification-style badge showing the client count."""
        if count <= 0:
            return self._green_logo_icon()
        pixmap = self._green_logo_icon().pixmap(QSize(64, 64))
        size = pixmap.width()
        d = int(size * 0.45)  # badge diameter — large enough to survive toolbar downscale
        x = 0
        y = size - d  # bottom-left corner
        painter = QPainter(pixmap)
        painter.setRenderHint(PAINTER_ANTIALIAS)
        painter.setBrush(QColor("#D32F2F"))
        pen = QPen(QColor("white"))  # white ring for contrast against the logo
        pen.setWidth(max(2, size // 20))
        painter.setPen(pen)
        painter.drawEllipse(x + 1, y + 1, d - 2, d - 2)
        painter.setPen(QColor("white"))
        font = painter.font()
        font.setPixelSize(int(d * 0.72))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(x, y, d, d, ALIGN_CENTER, str(min(count, 9)))
        painter.end()
        return QIcon(pixmap)

    def _on_clients_changed(self, count):
        """Update the toolbar icon badge when MCP clients connect/disconnect."""
        if not (self.action and self.server):
            return
        port = self.server.port
        self.action.setIcon(self._badge_icon(count))
        plural = "client" if count == 1 else "clients"
        self.action.setToolTip(
            f"MCP server running on :{port} — {count} {plural} connected — click to stop"
        )

    def _show_help(self):
        """Show the MCP Setup & Configurator dialog."""
        dlg = MCPConfiguratorDialog(self.iface, self.iface.mainWindow())
        dlg.exec()
        # Reflect an auto-start change made in the dialog onto the toolbar checkbox.
        if hasattr(self, "autostart_cb"):
            saved = QgsSettings().value(f"{self.SETTINGS_PREFIX}/autostart", False, type=bool)
            if self.autostart_cb.isChecked() != saved:
                self.autostart_cb.blockSignals(True)
                self.autostart_cb.setChecked(saved)
                self.autostart_cb.blockSignals(False)

    def _check_stale_mcp_configs(self):
        """Offer (once) to remove --refresh-package from existing uvx configs.

        --refresh-package forces uvx to re-resolve the package from GitHub on
        every launch, so the MCP server fails to start without network. Detect
        those entries and offer a one-click rewrite to the cached version.
        """
        settings = QgsSettings()
        if settings.value(f"{self.SETTINGS_PREFIX}/refresh_removal_prompted", False, type=bool):
            return

        repo_dir = Path(__file__).resolve().parent.parent
        affected = []  # (client, path, key, data)
        for client, info in _client_config_registry(repo_dir).items():
            if info.get("print_only"):
                continue
            path = info["path"]
            if not path.exists():
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            entry = data.get(info["key"], {}).get("qgis")
            if _qgis_entry_has_refresh(entry):
                affected.append((client, path, info["key"], data))

        if not affected:
            return  # nothing to migrate — stay silent

        # Prompt once, regardless of choice.
        settings.setValue(f"{self.SETTINGS_PREFIX}/refresh_removal_prompted", True)

        clients = ", ".join(sorted({c for c, *_ in affected}))
        box = QMessageBox(self.iface.mainWindow())
        box.setWindowTitle("QGIS MCP — fix offline startup?")
        box.setIcon(MSGBOX_QUESTION)
        box.setText(
            f"Your MCP config for {clients} uses '--refresh-package', which "
            "makes the server fail to start without internet access."
        )
        box.setInformativeText(
            "Remove '--refresh-package qgis-mcp' so the cached version is used "
            "(works offline, faster start; update manually when needed). "
            "Restart your AI client afterwards to take effect."
        )
        update_btn = box.addButton("Update configs", MSGBOX_ACCEPT_ROLE)
        box.addButton("Not now", MSGBOX_REJECT_ROLE)
        box.exec()

        if box.clickedButton() is not update_btn:
            return

        updated = []
        for client, path, key, data in affected:
            _remove_refresh_from_entry(data[key]["qgis"])
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                updated.append(client)
            except OSError as e:
                QgsMessageLog.logMessage(
                    f"Failed to update {client} config: {e}", "MCP", MSG_CRITICAL
                )
        if updated:
            QgsMessageLog.logMessage(
                f"Removed --refresh-package from configs: {', '.join(updated)}", "MCP", MSG_INFO
            )

    def toggle_server(self, checked):
        if checked:
            port = self.port_spin.value()
            self.server = QgisMCPServer(
                port=port, iface=self.iface, on_clients_changed=self._on_clients_changed
            )
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
