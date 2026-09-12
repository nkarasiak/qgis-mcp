"""The "Configure MCP clients" dialog and the client-config registry it writes.

Knows where each supported MCP client keeps its config file and what a qgis-mcp
server entry has to look like in it.
"""

import json
import os
import shutil
import sys
from pathlib import Path

from qgis.core import QgsApplication, QgsMessageLog, QgsSettings
from qgis.PyQt.QtCore import QSize, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices, QIcon
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .compat import MSG_CRITICAL, MSG_INFO, TEXT_SELECTABLE_BY_MOUSE
from .constants import SETTINGS_PREFIX, plugin_version

# Fallback for a drifted MCP server too old to announce its own update command.
# Assumes the recommended uvx install, where clearing the cache is what makes
# the next launch re-resolve the archive URL instead of reusing the old build.
DEFAULT_VERSION_FIX = "uv cache clean qgis-mcp"


def write_json_atomic(path, data):
    """Write JSON to ``path`` through a temporary file, then rename it into place.

    These are the user's own MCP client configs. A write that dies partway (full
    disk, QGIS killed) would otherwise leave one truncated and unparseable, and
    the client would start with no servers at all.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


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
    # Windsurf reads ~/.codeium/windsurf/mcp_config.json on every platform.
    windsurf_cfg = home / ".codeium" / "windsurf" / "mcp_config.json"
    vscode_cfg = repo_dir / ".vscode" / "mcp.json"

    if sys.platform == "win32":
        zed_cfg = appdata / "Zed" / "settings.json"
    else:
        zed_cfg = home / ".config" / "zed" / "settings.json"

    opencode_cfg = home / ".config" / "opencode" / "opencode.json"

    # Hermes desktop app (Windows) - YAML-based config, handled as print_only
    hermes_cfg = appdata / "Hermes" / "config.yaml" if sys.platform == "win32" and appdata else None

    # Clients sharing Claude Desktop's mcpServers + command/args schema.
    kimi_cfg = Path(os.environ.get("KIMI_CODE_HOME", home / ".kimi-code")) / "mcp.json"
    gemini_cfg = home / ".gemini" / "settings.json"
    qwen_cfg = home / ".qwen" / "settings.json"
    copilot_cfg = Path(os.environ.get("COPILOT_HOME", home / ".copilot")) / "mcp-config.json"
    lmstudio_cfg = home / ".lmstudio" / "mcp.json"

    return {
        "claude-desktop": {"path": claude_cfg, "key": "mcpServers"},
        "cursor": {"path": cursor_cfg, "key": "mcpServers"},
        "vscode": {"path": vscode_cfg, "key": "mcpServers", "project_local": True},
        "windsurf": {"path": windsurf_cfg, "key": "mcpServers"},
        "zed": {"path": zed_cfg, "key": "context_servers"},
        "opencode": {"path": opencode_cfg, "key": "mcp"},
        "claude-code": {"print_only": True},
        "hermes": {"print_only": True, "entry_format": "hermes", "hermes_cfg": hermes_cfg},
        "kimi": {"path": kimi_cfg, "key": "mcpServers"},
        "gemini": {"path": gemini_cfg, "key": "mcpServers"},
        "qwen": {"path": qwen_cfg, "key": "mcpServers"},
        "copilot-cli": {"path": copilot_cfg, "key": "mcpServers"},
        "lmstudio": {"path": lmstudio_cfg, "key": "mcpServers"},
    }


def _qgis_entry_command_args(entry):
    """Return (command, args) for a 'qgis' server entry."""
    if not isinstance(entry, dict):
        return None, []
    cmd = entry.get("command")
    return cmd, entry.get("args", [])


def _qgis_entry_has_refresh(entry):
    """True when a remote uvx 'qgis' entry has --refresh-package (fails offline)."""
    command, args = _qgis_entry_command_args(entry)
    if command != "uvx" or "qgis-mcp-server" not in args:
        return False  # local mode / unknown - leave alone
    return "--refresh-package" in args


def _remove_refresh_from_entry(entry):
    """Remove '--refresh-package qgis-mcp' from a uvx 'qgis' entry."""
    cmd = entry.get("command")
    args = cmd.get("args", []) if isinstance(cmd, dict) else entry.get("args", [])
    try:
        idx = args.index("--refresh-package")
        end = idx + 2  # the flag and its value
        del args[idx:end]
    except ValueError:
        pass
    if isinstance(cmd, dict):
        cmd["args"] = args
    else:
        entry["args"] = args
    return entry


class MCPConfiguratorDialog(QDialog):
    def __init__(self, iface, parent=None, server=None, start_server=None):
        super().__init__(parent)
        self.iface = iface
        # The running QgisMCPServer, when there is one: it knows which MCP server
        # versions have actually connected, which is the only way this dialog can
        # report drift between the two halves.
        self.server = server
        # Callable that starts the socket server and returns it (or None on
        # failure). Supplied by the plugin, which owns the toolbar state.
        self.start_server = start_server
        self.setWindowTitle("QGIS MCP - Setup & Configurator")
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
            [
                "claude-code",
                "claude-desktop",
                "copilot-cli",
                "cursor",
                "gemini",
                "hermes",
                "kimi",
                "lmstudio",
                "opencode",
                "qwen",
                "vscode",
                "windsurf",
                "zed",
            ]
        )
        self.client_combo.setMinimumWidth(180)
        self.client_combo.currentTextChanged.connect(self._on_client_changed)
        client_row.addWidget(self.client_combo)

        # Mode selector - only relevant for dev installs with a local clone
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Remote (uvx - recommended)", "Local (uv run)"])
        self.mode_combo.setToolTip(
            "Remote: install MCP server on-the-fly via uvx (no clone needed).\n"
            "Local: run MCP server from your git clone via uv."
        )
        self.mode_combo.currentTextChanged.connect(self._on_client_changed)
        self.mode_combo.setVisible(self._is_dev_install())
        client_row.addWidget(self.mode_combo)
        client_row.addStretch()
        client_form.addLayout(client_row)

        # Refresh toggle - adds `--refresh-package qgis-mcp` so uvx re-pulls the
        # latest server from GitHub on every launch (remote mode only).
        self.refresh_check = QCheckBox("Always pull latest server from GitHub")
        self.refresh_check.setToolTip(
            "Add --refresh-package qgis-mcp so uvx re-pulls the latest server from\n"
            "GitHub on every client launch (stays in sync with the plugin).\n"
            "Warning: requires network at launch - the server fails to start offline.\n"
            "Leave unchecked to use the cached version (works offline, manual updates)."
        )
        self.refresh_check.setChecked(False)
        self.refresh_check.toggled.connect(self._on_client_changed)
        client_form.addWidget(self.refresh_check)

        self.autostart_check = QCheckBox("Start MCP server automatically when QGIS opens")
        self.autostart_check.setToolTip(
            "Launch the MCP server on QGIS startup so an AI agent can reconnect\n"
            "without manually starting it (e.g. after a crash and restart).\n"
            "Ticking this also starts the server now if it is not running."
        )
        self.autostart_check.setChecked(
            QgsSettings().value(f"{SETTINGS_PREFIX}/autostart", False, type=bool)
        )
        self.autostart_check.toggled.connect(self._save_autostart)
        client_form.addWidget(self.autostart_check)

        # Whether the socket server is up. Without it, ticking auto-start looks
        # like nothing happened - the toolbar icon is hidden behind this dialog.
        self.server_label = QLabel()
        client_form.addWidget(self.server_label)
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

        # Plugin vs MCP server version. The two are installed and updated by
        # different mechanisms (Plugin Manager vs the uvx cache), so drift is
        # normal and invisible unless it is stated somewhere the user looks.
        self.version_label = QLabel()
        self.version_label.setWordWrap(True)
        # The drift message names a command to run; a plain QLabel cannot even be
        # selected, so give it both selection and a one-click copy.
        self.version_label.setTextInteractionFlags(TEXT_SELECTABLE_BY_MOUSE)
        version_row = QHBoxLayout()
        version_row.addWidget(self.version_label, 1)
        self.version_fix_btn = QPushButton("Copy fix")
        self.version_fix_btn.setFixedWidth(80)
        self.version_fix_btn.clicked.connect(self._copy_version_fix)
        self.version_fix_btn.setVisible(False)
        version_row.addWidget(self.version_fix_btn)
        preview_box.addLayout(version_row)
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
        """Persist the auto-start preference, and start the server if it is off.

        On a fresh install the server has never been started, so ticking this
        and getting nothing until the next QGIS launch reads as a broken
        checkbox. Unticking only affects the next launch - it does not stop a
        running server, which would be a surprising way to lose a live session.
        """
        QgsSettings().setValue(f"{SETTINGS_PREFIX}/autostart", checked)
        if checked and self.server is None and self.start_server:
            self.server = self.start_server()
            self.refresh_status()

    def _find_uv(self):
        """Return uv executable path, checking common Windows install locations."""
        uv = shutil.which("uv")
        if uv:
            return uv
        if sys.platform == "win32":
            localappdata = Path(os.environ.get("LOCALAPPDATA", ""))
            userprofile = Path(os.environ.get("USERPROFILE", ""))
            candidates = [
                localappdata / "Microsoft" / "WinGet" / "Links" / "uv.exe",
                userprofile / ".local" / "bin" / "uv.exe",
                userprofile / ".cargo" / "bin" / "uv.exe",
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

    def _version_fix_commands(self, drifted):
        """The update commands for the drifted MCP servers, one per install kind.

        Only the MCP server side knows whether it was launched by uvx or from a
        source checkout, and the two commands are not interchangeable, so the
        plugin builds one per kind it was told about. Kept as a list rather than
        joined into a single line: `;` chains commands in bash and PowerShell
        but not in cmd.exe, and a copy button that silently produces a broken
        command line is worse than two lines to paste.
        """
        fixes = getattr(self.server, "client_fixes", None) or {}
        return sorted({fixes[v] for v in drifted if v in fixes}) or [DEFAULT_VERSION_FIX]

    def _copy_version_fix(self):
        QgsApplication.clipboard().setText(self.version_fix_btn.property("command"))
        self.version_fix_btn.setText("Copied!")
        QTimer.singleShot(1500, lambda: self.version_fix_btn.setText("Copy fix"))

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
                        "--directory",
                        str(self.repo_dir),
                        "run",
                        "--no-sync",
                        "src/qgis_mcp/server.py",
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
            launch_cmd = (
                f'"{uv}" --directory "{self.repo_dir}" run --no-sync src/qgis_mcp/server.py'
            )

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
        yaml_block = f'mcpServers:\n  qgis:\n    command: "{bat_escaped}"\n    args: []'

        return (
            f"Step 1 - Create this file:\n"
            f"  {bat_path}\n\n"
            f"Contents:\n"
            f"{bat_content}\n\n"
            f"Step 2 - Add to:\n"
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
                "Manual setup required - copy the .bat content and YAML config below:"
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
                    f"claude mcp add -s user qgis -- "
                    f'"{uv}" --directory "{self.repo_dir}" run --no-sync src/qgis_mcp/server.py'
                )
            self.preview_label.setText("Run this command in your terminal:")
            self.preview_edit.setPlainText(cmd)
            return

        self.preview_label.setText("Add to your client config file:")
        entry = self._get_server_entry(client, remote, refresh)
        self.preview_edit.setPlainText(json.dumps({"qgis": entry}, indent=2))

    def _refresh_versions(self):
        """Show this plugin's version against the MCP servers that connected.

        Set before every early return in refresh_status(): it does not depend on
        which client is selected, and it is the one place a user can see drift
        without asking an agent to run 'diagnose'.
        """
        mine = plugin_version()
        seen = sorted(getattr(self.server, "client_versions", ()) or ())
        if not seen:
            self.version_fix_btn.setVisible(False)
            self.version_label.setText(
                f"Plugin {mine}, MCP server: none connected yet "
                "(start the server and run a tool once)."
            )
            self.version_label.setStyleSheet("color: gray;")
            return
        drifted = [v for v in seen if v != mine]
        self.version_fix_btn.setVisible(bool(drifted))
        if drifted:
            fixes = self._version_fix_commands(drifted)
            self.version_fix_btn.setProperty("command", "\n".join(fixes))
            self.version_fix_btn.setToolTip("Copy to the clipboard:\n" + "\n".join(fixes))
            spelled = " and ".join(f"`{f}`" for f in fixes)
            self.version_label.setText(
                f"Plugin {mine}, MCP server {', '.join(seen)}. The versions differ. "
                "Everything still works, but tools added since the older half was built "
                f"will be missing. To match them, run {spelled}, then restart your MCP client."
            )
            self.version_label.setStyleSheet("color: orange;")
        else:
            self.version_label.setText(
                f"Plugin {mine}, MCP server {', '.join(seen)}. The versions match."
            )
            self.version_label.setStyleSheet("color: green;")

    def _refresh_server_state(self):
        """Show whether the socket server is listening."""
        if self.server is not None:
            self.server_label.setText(f"Server: running on port {self.server.port}")
            self.server_label.setStyleSheet("color: green;")
        else:
            self.server_label.setText("Server: not running")
            self.server_label.setStyleSheet("color: gray;")

    def refresh_status(self):
        self._refresh_server_state()
        self._refresh_versions()
        client = self.client_combo.currentText()
        info = self._get_client_info(client)

        if info.get("entry_format") == "hermes":
            hermes_cfg = info.get("hermes_cfg")
            if hermes_cfg and hermes_cfg.exists():
                self.status_label.setText(
                    f"Status: config.yaml found - verify 'qgis' is in mcpServers ({hermes_cfg})"
                )
                self.status_label.setStyleSheet("color: orange;")
            else:
                cfg_hint = str(hermes_cfg) if hermes_cfg else "%APPDATA%\\Hermes\\config.yaml"
                self.status_label.setText(f"Status: Follow the steps below, then edit {cfg_hint}")
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

            write_json_atomic(path, data)

            self.refresh_status()
            QgsMessageLog.logMessage(f"Configured {client} at {path}", "MCP", MSG_INFO)
        except Exception as e:
            QgsMessageLog.logMessage(f"Failed to configure {client}: {e}", "MCP", MSG_CRITICAL)
            self.status_label.setText(f"Status: Failed to write: {e}")
            self.status_label.setStyleSheet("color: red;")
