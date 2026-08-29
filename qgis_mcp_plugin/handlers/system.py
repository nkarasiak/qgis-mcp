"""Handlers for QGIS itself: health, versions, message log, plugins, settings."""

import contextlib
import io
import os
import sys
import time
import traceback

try:
    from datetime import UTC, datetime
except ImportError:
    from datetime import datetime, timezone

    UTC = timezone.utc  # noqa: UP017  (fallback path: datetime.UTC unavailable pre-3.11)

from typing import ClassVar

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsMessageLog,
    QgsProject,
    QgsRasterLayer,
    QgsSettings,
    QgsVectorLayer,
)
from qgis.utils import active_plugins, available_plugins, pluginMetadata, reloadPlugin

from ..compat import MSG_INFO
from ..constants import plugin_version
from ..errors import CommandError
from ..registry import command


class _CodeDeadline(Exception):
    """Raised by the execute_code trace function once the budget is spent."""


class SystemHandlers:
    """QGIS health, versions, message log, plugin list and settings."""

    # Below the client's TIMEOUT_LONG (60s) for the same reason as
    # ProcessingHandlers._PROCESSING_TIMEOUT: the plugin gives up, and says why,
    # before the client abandons the request and the result is lost (#43).
    _CODE_TIMEOUT = 55
    _CODE_FILENAME = "<qgis_mcp execute_code>"

    def _deadline_tracer(self, deadline):
        """Trace function that raises _CodeDeadline once *deadline* has passed.

        Checked on every function call anywhere and on every line of the script's
        own frames (module level and the functions it defines), never on lines
        inside library code, so the cost is proportional to the script rather
        than to what it calls. A single blocking call (time.sleep, a GDAL warp)
        cannot be interrupted this way: it returns when it returns, and the
        check fires right after.
        """

        def trace(frame, event, arg):
            # Only before work starts (a call, a line), never on return: a script
            # that finishes late has finished, and saying it was cancelled would
            # send the caller looking for work that was actually done.
            if event in ("call", "line") and time.monotonic() >= deadline:
                raise _CodeDeadline
            return trace if frame.f_code.co_filename == self._CODE_FILENAME else None

        return trace

    @command
    def ping(self, **kwargs):
        return {"pong": True}

    @command
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
            checks.append({"name": "plugin_version", "status": "ok", "detail": plugin_version()})
        except Exception as e:
            checks.append({"name": "plugin_version", "status": "error", "detail": str(e)})
            overall = "degraded" if overall == "healthy" else overall

        # 3. Connected clients
        client_count = len(self.clients)
        checks.append({"name": "connected_clients", "status": "ok", "detail": client_count})

        # 3b. Which MCP server versions have talked to this plugin. The client
        # adds its own comparison (version_match), but reporting it from the
        # plugin's side covers several clients on one QGIS, where only some are
        # out of date - and it is empty against a server older than 0.10.
        seen = sorted(self.client_versions)
        drifted = [v for v in seen if v != plugin_version()]
        detail = {"seen": seen, "plugin": plugin_version(), "drifted": drifted}
        # The update command each drifted client announced for itself. Absent
        # against a client too old to send one.
        fixes = {v: self.client_fixes[v] for v in drifted if v in self.client_fixes}
        if fixes:
            detail["fixes"] = fixes
        checks.append(
            {
                "name": "client_versions",
                "status": "mismatch" if drifted else "ok",
                "detail": detail,
            }
        )

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

    @command
    def get_qgis_info(self, **kwargs):
        info = {
            "qgis_version": Qgis.version(),
            "profile_folder": QgsApplication.qgisSettingsDirPath(),
            "plugins_count": len(active_plugins),
            # Identity, so a client driving several QGIS windows can tell which one
            # answered rather than inferring it from the port. The pid is unique and
            # stable; the window title is what the user reads in the taskbar and
            # already carries the project name.
            "pid": os.getpid(),
        }
        if self.iface is not None:
            with contextlib.suppress(Exception):
                info["window_title"] = self.iface.mainWindow().windowTitle()
        return info

    @command
    def execute_code(self, code, timeout=None, **kwargs):
        QgsMessageLog.logMessage(f"Executing code ({len(code)} chars)", self.LOG_TAG, MSG_INFO)
        budget = self._CODE_TIMEOUT if timeout is None else float(timeout)
        started = time.monotonic()
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        previous_trace = sys.gettrace()

        def output(**fields):
            fields["stdout"] = stdout_capture.getvalue()
            fields["stderr"] = stderr_capture.getvalue()
            fields["elapsed"] = round(time.monotonic() - started, 2)
            return fields

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

            compiled = compile(code, self._CODE_FILENAME, "exec")
            sys.settrace(self._deadline_tracer(started + budget))
            try:
                exec(compiled, namespace)  # nosec B102 - intentional: MCP execute_code tool
            finally:
                sys.settrace(previous_trace)
            return output(executed=True)
        except _CodeDeadline:
            return output(
                executed=False,
                timed_out=True,
                error=(
                    f"Code cancelled after {budget:g}s (timeout). Pass a larger 'timeout' or "
                    "split the work into smaller scripts; side effects up to this point stand."
                ),
            )
        except Exception as e:
            return output(executed=False, error=str(e), traceback=traceback.format_exc())
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr

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

    @command
    def get_message_log(self, level=None, tag=None, limit=100, **kwargs):
        entries = list(self._message_log)
        entries.reverse()  # newest first
        if level:
            entries = [e for e in entries if e["level"] == level]
        if tag:
            entries = [e for e in entries if e["tag"] == tag]
        entries = entries[:limit]
        return {"messages": entries, "count": len(entries)}

    @command
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

    @command
    def get_plugin_info(self, plugin_name, **kwargs):
        if plugin_name not in available_plugins and plugin_name not in active_plugins:
            raise CommandError(f"Plugin not found: {plugin_name}")
        return {
            "name": plugin_name,
            "enabled": plugin_name in active_plugins,
            "version": pluginMetadata(plugin_name, "version") or "",
            "description": pluginMetadata(plugin_name, "description") or "",
            "author": pluginMetadata(plugin_name, "author") or "",
            "path": pluginMetadata(plugin_name, "path") or "",
        }

    @command
    def reload_plugin(self, plugin_name, **kwargs):
        if plugin_name == "qgis_mcp_plugin":
            raise CommandError("Cannot reload MCP plugin (would break the connection)")
        if plugin_name not in active_plugins:
            raise CommandError(f"Plugin not active: {plugin_name}")
        reloadPlugin(plugin_name)
        return {"reloaded": plugin_name, "ok": True}

    @command
    def get_setting(self, key, **kwargs):
        settings = QgsSettings()
        value = settings.value(key)
        return {
            "key": key,
            "value": value,
            "exists": settings.contains(key),
        }

    @command
    def set_setting(self, key, value, **kwargs):
        settings = QgsSettings()
        settings.setValue(key, value)
        return {"ok": True, "key": key}
