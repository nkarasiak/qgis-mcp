"""QGIS plugin entry point: toolbar action, menu, and server start/stop.

The socket server lives in :mod:`qgis_mcp_plugin.server` and the MCP client
configurator in :mod:`qgis_mcp_plugin.configurator`.
"""

import contextlib
import json
import os
from pathlib import Path

from qgis.core import QgsMessageLog, QgsSettings
from qgis.PyQt.QtCore import QSize, QTimer, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPen
from qgis.PyQt.QtWidgets import (
    QAction,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from .compat import (
    ALIGN_CENTER,
    MSG_CRITICAL,
    MSG_INFO,
    MSGBOX_ACCEPT_ROLE,
    MSGBOX_QUESTION,
    MSGBOX_REJECT_ROLE,
    PAINTER_ANTIALIAS,
    TOOLBUTTON_ICON_ONLY,
    TOOLBUTTON_MENU_POPUP,
)
from .configurator import (
    MCPConfiguratorDialog,
    _client_config_registry,
    _qgis_entry_has_refresh,
    _remove_refresh_from_entry,
    write_json_atomic,
)
from .constants import DEFAULT_PORT, PLUGIN_DIR, SETTINGS_PREFIX
from .server import QgisMCPServer


class QgisMCPPlugin:
    """Main plugin class for QGIS MCP"""

    REPO_URL = "https://github.com/nkarasiak/qgis-mcp"

    def __init__(self, iface):
        self.iface = iface
        self.server = None
        self.action = None
        self.help_action = None
        self.tool_button = None
        self._toolbar_action = None  # the action wrapping the tool button

    def _logo_icon(self):
        """Load the MCP logo from the plugin directory."""
        icon_path = os.path.join(PLUGIN_DIR, "icons", "icon.png")
        return QIcon(icon_path)

    def initGui(self):
        toolbar = self.iface.pluginToolBar()

        # Main action (used for menu entry + click handler)
        self.action = QAction(self._logo_icon(), "Run MCP", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.setToolTip(f"Start MCP server on port {DEFAULT_PORT}")
        self.action.triggered.connect(self.toggle_server)

        # Port config in dropdown menu
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(DEFAULT_PORT)
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
            settings.value(f"{SETTINGS_PREFIX}/autostart", False, type=bool)
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

        self.help_action = QAction(
            self._logo_icon(), "MCP Setup Configurator", self.iface.mainWindow()
        )
        self.help_action.triggered.connect(self._show_help)

        self.iface.addPluginToMenu("QGIS MCP", self.action)
        self.iface.addPluginToMenu("QGIS MCP", self.help_action)

        # Set the icon on the "QGIS MCP" submenu itself (top-level entry)
        for sub in self.iface.pluginMenu().actions():
            if sub.text() == "QGIS MCP" and sub.menu():
                sub.setIcon(self._logo_icon())
                break

        # Restore saved port
        saved_port = settings.value(f"{SETTINGS_PREFIX}/port", DEFAULT_PORT, type=int)
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
        first_run = settings.value(f"{SETTINGS_PREFIX}/first_run", True, type=bool)
        if not first_run:
            return
        settings.setValue(f"{SETTINGS_PREFIX}/first_run", False)

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
        QgsSettings().setValue(f"{SETTINGS_PREFIX}/autostart", checked)

    def _save_port(self, port):
        """Persist port preference."""
        QgsSettings().setValue(f"{SETTINGS_PREFIX}/port", port)

    def _green_logo_icon(self):
        """Load the green MCP logo for active state."""
        icon_path = os.path.join(PLUGIN_DIR, "icons", "icon_active.png")
        return QIcon(icon_path)

    def _badge_icon(self, count):
        """Green logo with a notification-style badge showing the client count."""
        if count <= 0:
            return self._green_logo_icon()
        pixmap = self._green_logo_icon().pixmap(QSize(64, 64))
        size = pixmap.width()
        d = int(size * 0.45)  # badge diameter - large enough to survive toolbar downscale
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
            f"MCP server running on :{port} - {count} {plural} connected - click to stop"
        )

    def _start_server_from_dialog(self):
        """Start the socket server on behalf of the configurator dialog.

        Returns the running server, or None if it failed to bind. Goes through
        the toolbar action so the icon and checked state stay in sync.
        """
        if self.server is None:
            self.action.setChecked(True)
            self.toggle_server(True)
        return self.server

    def _show_help(self):
        """Show the MCP Setup & Configurator dialog."""
        dlg = MCPConfiguratorDialog(
            self.iface,
            self.iface.mainWindow(),
            server=self.server,
            start_server=self._start_server_from_dialog,
        )
        dlg.exec()
        # Reflect an auto-start change made in the dialog onto the toolbar checkbox.
        if hasattr(self, "autostart_cb"):
            saved = QgsSettings().value(f"{SETTINGS_PREFIX}/autostart", False, type=bool)
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
        if settings.value(f"{SETTINGS_PREFIX}/refresh_removal_prompted", False, type=bool):
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
            return  # nothing to migrate - stay silent

        # Prompt once, regardless of choice.
        settings.setValue(f"{SETTINGS_PREFIX}/refresh_removal_prompted", True)

        clients = ", ".join(sorted({c for c, *_ in affected}))
        box = QMessageBox(self.iface.mainWindow())
        box.setWindowTitle("QGIS MCP - fix offline startup?")
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
                write_json_atomic(path, data)
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
                port=port,
                iface=self.iface,
                on_clients_changed=self._on_clients_changed,
            )
            if self.server.start():
                self.action.setIcon(self._green_logo_icon())
                self.action.setText(f"MCP :{port}")
                self.action.setToolTip(f"MCP server running on :{port} - click to stop")
                self.port_spin.setEnabled(False)
            else:
                # Without this the button just pops back out and the only trace is a
                # line in the message log, so a port clash looks like nothing happened.
                reason = self.server.start_error or "unknown error"
                with contextlib.suppress(Exception):
                    self.iface.messageBar().pushWarning("QGIS MCP", reason)
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


def classFactory(iface):
    return QgisMCPPlugin(iface)
