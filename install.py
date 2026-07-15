#!/usr/bin/env python3
"""Multi-client installer for QGIS MCP.

Symlinks the QGIS plugin and configures MCP clients (Claude Desktop,
Cursor, VS Code Copilot, Windsurf, Zed, Claude Code, Codex CLI, opencode).

Usage:
    python install.py                          # Interactive menu
    python install.py --non-interactive --clients opencode
    python install.py --non-interactive --clients claude-desktop,cursor
    python install.py --remote                 # Use uvx (no local clone needed)
    python install.py --uninstall --clients cursor
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
PLUGIN_SRC = REPO_DIR / "qgis_mcp_plugin"
# Zip archive instead of git+ URL: uvx then needs no git executable, which is
# not visible to GUI-spawned MCP servers (e.g. Claude Desktop on Windows).
GITHUB_URL = "https://github.com/nkarasiak/qgis-mcp/archive/refs/heads/main.zip"

# ── Platform helpers ────────────────────────────────────────────────────────


def _home() -> Path:
    return Path.home()


def _appdata() -> Path:
    """Windows %APPDATA% or fallback."""
    return Path(os.environ.get("APPDATA", _home() / "AppData" / "Roaming"))


def _qgis_base_dir(version: str) -> Path:
    """Return the QGIS data root for a given major version ('3' or '4')."""
    folder = f"QGIS{version}"
    bases = {
        "linux": _home() / ".local" / "share" / "QGIS" / folder,
        "darwin": _home() / "Library" / "Application Support" / "QGIS" / folder,
        "win32": _appdata() / "QGIS" / folder,
    }
    base = bases.get(sys.platform)
    if base is None:
        sys.exit(f"Unsupported platform: {sys.platform}")
    return base


def _detect_qgis_version() -> str:
    """Return '4' if QGIS4 profile dir exists, else '3'."""
    if _qgis_base_dir("4").exists():
        return "4"
    return "3"


def qgis_plugins_dir(profile: str, version: str = "auto") -> Path:
    if version == "auto":
        version = _detect_qgis_version()
    return _qgis_base_dir(version) / "profiles" / profile / "python" / "plugins"


# ── Client config paths ────────────────────────────────────────────────────

ClientInfo = dict[str, str | Path | bool]


def _client_registry() -> dict[str, ClientInfo]:
    """Return per-client metadata.  Paths resolved at call time."""
    home = _home()
    appdata = _appdata()

    if sys.platform == "darwin":
        claude_cfg = (
            home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        )
    elif sys.platform == "win32":
        claude_cfg = appdata / "Claude" / "claude_desktop_config.json"
    else:
        claude_cfg = home / ".config" / "Claude" / "claude_desktop_config.json"

    cursor_cfg = home / ".cursor" / "mcp.json"
    windsurf_cfg = home / ".codeium" / "windsurf" / "mcp_config.json"
    vscode_cfg = REPO_DIR / ".vscode" / "mcp.json"

    if sys.platform == "darwin":
        zed_cfg = home / ".config" / "zed" / "settings.json"
    elif sys.platform == "win32":
        zed_cfg = appdata / "Zed" / "settings.json"
    else:
        zed_cfg = home / ".config" / "zed" / "settings.json"

    # opencode (https://opencode.ai) - uses "mcp" key with type/command-array format
    if sys.platform == "win32":
        opencode_cfg = appdata / "opencode" / "config.json"
    else:
        opencode_cfg = home / ".config" / "opencode" / "config.json"

    # Hermes desktop app (Windows) - uses config.yaml with mcpServers block.
    # Requires a .bat launcher to avoid Hermes's venv polluting the MCP server.
    hermes_cfg = appdata / "Hermes" / "config.yaml" if sys.platform == "win32" else None

    return {
        "claude-desktop": {"path": claude_cfg, "key": "mcpServers"},
        "cursor": {"path": cursor_cfg, "key": "mcpServers"},
        "vscode": {"path": vscode_cfg, "key": "mcpServers", "project_local": True},
        "windsurf": {"path": windsurf_cfg, "key": "mcpServers"},
        "zed": {"path": zed_cfg, "key": "context_servers"},
        "claude-code": {"print_only": True, "cli": "claude"},
        "codex": {"print_only": True, "cli": "codex"},
        "opencode": {"path": opencode_cfg, "key": "mcp", "entry_format": "opencode"},
        "hermes": {"print_only": True, "entry_format": "hermes", "hermes_cfg": hermes_cfg},
    }


# ── MCP server entry builders ──────────────────────────────────────────────


def _venv_python() -> Path:
    """Return the Python executable inside the project venv."""
    if sys.platform == "win32":
        return REPO_DIR / ".venv" / "Scripts" / "python.exe"
    return REPO_DIR / ".venv" / "bin" / "python"


def _is_venv_ready() -> bool:
    """Check if the venv exists and qgis_mcp is importable."""
    python = _venv_python()
    if not python.exists():
        return False
    result = subprocess.run(
        [str(python), "-c", "import qgis_mcp"],
        capture_output=True,
    )
    return result.returncode == 0


def setup_venv() -> None:
    """Create venv and install dependencies, using uv if available, else pip."""
    if _is_venv_ready():
        print("  Dependencies already installed.")
        return

    uv = shutil.which("uv")
    if uv:
        print("  Setting up dependencies with uv...")
        subprocess.run([uv, "sync"], cwd=str(REPO_DIR), check=True)
    else:
        print("  uv not found, falling back to pip...")
        venv_dir = REPO_DIR / ".venv"
        if not venv_dir.exists():
            print("  Creating virtual environment...")
            subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        python = str(_venv_python())
        subprocess.run([python, "-m", "pip", "install", "-e", str(REPO_DIR)], check=True)

    print("  Dependencies installed.")


def _local_entry() -> dict:
    if shutil.which("uv"):
        # `--directory` is preferred over `cwd` because some MCP clients (notably
        # MSIX-packaged Claude Desktop on Windows) run servers in a sandbox that
        # silently ignores `cwd`. `--directory` bakes the project path into the
        # command itself so it works regardless of the spawn environment.
        return {
            "command": "uv",
            "args": [
                "--directory",
                str(REPO_DIR),
                "run",
                "--no-sync",
                "src/qgis_mcp/server.py",
            ],
        }
    # Fallback: run directly from the venv Python
    return {
        "command": str(_venv_python()),
        "args": [str(REPO_DIR / "src" / "qgis_mcp" / "server.py")],
    }


def _remote_entry() -> dict:
    return {
        "command": "uvx",
        "args": ["--from", GITHUB_URL, "qgis-mcp-server"],
    }


def _server_entry(client: str, remote: bool) -> dict:
    return _remote_entry() if remote else _local_entry()


def _opencode_server_entry(remote: bool) -> dict:
    """Build an MCP server entry in opencode's native format.

    opencode uses ``{"type": "local", "command": [...]}`` (command as an array)
    under the top-level ``"mcp"`` key instead of the ``mcpServers`` / split
    command+args shape used by most other clients.
    """
    if remote:
        cmd: list[str] = ["uvx", "--from", GITHUB_URL, "qgis-mcp-server"]
    elif shutil.which("uv"):
        cmd = [
            "uv",
            "--directory",
            str(REPO_DIR),
            "run",
            "--no-sync",
            "src/qgis_mcp/server.py",
        ]
    else:
        cmd = [str(_venv_python()), str(REPO_DIR / "src" / "qgis_mcp" / "server.py")]
    return {"type": "local", "command": cmd}


def _hermes_bat_content(remote: bool) -> str:
    """Return the content of qgis-mcp-launch.bat for Hermes desktop app (Windows).

    The .bat clears the Python venv environment set by Hermes before launching
    uvx, preventing Hermes's broken pydantic/mcp packages from being imported
    by the qgis-mcp-server process.
    """
    if remote:
        launch_cmd = f'uvx --from "{GITHUB_URL}" qgis-mcp-server'
    elif shutil.which("uv"):
        launch_cmd = (
            f'uv --directory "{REPO_DIR}" run --no-sync src/qgis_mcp/server.py'
        )
    else:
        python = _venv_python()
        launch_cmd = f'"{python}" "{REPO_DIR / "src" / "qgis_mcp" / "server.py"}"'
    return (
        # CRLF line endings are intentional: .bat files must use Windows line endings
        # to work correctly regardless of the text editor used to create them.
        "@echo off\r\n"
        "REM Launcher for qgis-mcp-server, isolated from Hermes's own Python venv.\r\n"
        "REM Clears venv vars so uvx/uv uses the system Python, not Hermes's packages.\r\n"
        "set VIRTUAL_ENV=\r\n"
        "set PYTHONPATH=\r\n"
        "set PYTHONHOME=\r\n"
        f"{launch_cmd}\r\n"
    )


def _hermes_instructions(remote: bool) -> None:
    """Print step-by-step Hermes desktop app setup instructions (Windows only)."""
    home = _home()
    hermes_dir = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Hermes"
    bat_path = hermes_dir / "qgis-mcp-launch.bat"
    cfg_path = hermes_dir / "config.yaml"

    bat_content = _hermes_bat_content(remote)

    print()
    print("  Hermes desktop app (Windows) — manual setup required:")
    print()
    print(f"  Step 1 — Create the launcher: {bat_path}")
    print()
    for line in bat_content.splitlines():
        print(f"    {line}")
    print()
    print(f"  Step 2 — Add to {cfg_path}:")
    print()
    bat_escaped = str(bat_path).replace("\\", "\\\\")
    print("    mcpServers:")
    print("      qgis:")
    print(f'        command: "{bat_escaped}"')
    print("        args: []")
    print()
    print("  See docs/agent-integration.md for full details.")


# ── Plugin installation ────────────────────────────────────────────────────


def _remove_target(target: Path) -> None:
    """Remove a plugin target — handles files, symlinks, Windows junctions, and dirs.

    Path.is_symlink() returns False for Windows directory junctions (created via
    `mklink /J`), so we also check os.path.islink() and fall back to rmdir() for
    junctions before resorting to shutil.rmtree() on real directories.
    """
    if target.is_symlink() or os.path.islink(target) or target.is_file():
        target.unlink()
    elif sys.platform == "win32":
        try:
            target.rmdir()  # cleanly removes a junction without touching the target
        except OSError:
            shutil.rmtree(target)
    else:
        shutil.rmtree(target)


def install_plugin(profile: str, version: str = "auto") -> Path:
    plugins_dir = qgis_plugins_dir(profile, version)
    target = plugins_dir / "qgis_mcp_plugin"

    if target.is_symlink() or target.exists() or os.path.islink(target):
        if target.is_symlink() and target.resolve() == PLUGIN_SRC.resolve():
            print(f"  Plugin already linked: {target}")
            return target
        print(f"  Removing existing: {target}")
        _remove_target(target)

    plugins_dir.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        # Symlinks may require admin on Windows; fall back to dir junction
        try:
            target.symlink_to(PLUGIN_SRC, target_is_directory=True)
        except OSError:
            # Junction via direct API call (no shell, unlike `mklink /J`)
            import _winapi

            _winapi.CreateJunction(str(PLUGIN_SRC), str(target))
    else:
        target.symlink_to(PLUGIN_SRC)

    print(f"  Linked: {target} -> {PLUGIN_SRC}")
    return target


def uninstall_plugin(profile: str, version: str = "auto") -> None:
    target = qgis_plugins_dir(profile, version) / "qgis_mcp_plugin"
    if target.is_symlink() or target.exists() or os.path.islink(target):
        _remove_target(target)
        print(f"  Removed: {target}")
    else:
        print(f"  Not installed: {target}")


# ── Client configuration ───────────────────────────────────────────────────

def _jsonc_to_json(text) -> str:
    """Convert potential JSONC json file to valid JSON

    Cases:
        - A: JSONC with multi-line comments
        - B: JSONC with single-line comment
        - C: URLs with `http://`, `https://` preserved
        - D: // inside string values preserved
        - E: Trailing commas in objects and arrays
    """
    caseA = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    casesBCD = re.sub(
        r'("(?:\\.|[^"\\])*")|//.*',
        lambda m: m.group(1) or '',
        caseA,
        flags=re.MULTILINE
    )
    caseE = re.sub(r',\s*([}\]])', r'\1', casesBCD)
    return caseE

def _read_json(path: Path) -> dict:
    if not path.exists() or not (text := path.read_text(encoding="utf-8").strip()):
        return {}

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    try:
        cleaned = _jsonc_to_json(text)
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Failed to parse {path}: not valid JSON or JSONC. "
            f"Error: {e}"
        ) from e


def _backup(path: Path) -> None:
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, bak)
        print(f"  Backup: {bak}")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def configure_client(client_name: str, remote: bool) -> None:
    registry = _client_registry()
    info = registry[client_name]

    # Hermes desktop app: YAML config + bat launcher — print instructions only
    if info.get("entry_format") == "hermes":
        _hermes_instructions(remote)
        return

    # CLI-based clients (Claude Code, Codex): use their `mcp add` subcommand
    if info.get("print_only"):
        cli_name = info.get("cli", "claude")
        cli_bin = shutil.which(cli_name)
        if not cli_bin:
            print(f"  '{cli_name}' CLI not found in PATH – skipping.")
            return

        if remote:
            add_args = ["uvx", "--from", GITHUB_URL, "qgis-mcp-server"]
        elif shutil.which("uv"):
            add_args = [
                "uv", "run", "--no-sync",
                "--directory", str(REPO_DIR),
                "src/qgis_mcp/server.py",
            ]
        else:
            add_args = [str(_venv_python()), str(REPO_DIR / "src" / "qgis_mcp" / "server.py")]

        if cli_name == "claude":
            # Claude Code supports scoped installs; use user scope for QGIS (global tool)
            subprocess.run(
                [cli_bin, "mcp", "remove", "-s", "user", "qgis"],
                capture_output=True,
            )
            result = subprocess.run(
                [cli_bin, "mcp", "add", "-s", "user", "qgis", "--", *add_args],
                capture_output=True,
                text=True,
            )
            label = "Claude Code (user scope)"
        else:
            # Codex CLI: `codex mcp add <name> -- <cmd> [args...]`
            subprocess.run(
                [cli_bin, "mcp", "remove", "qgis"],
                capture_output=True,
            )
            result = subprocess.run(
                [cli_bin, "mcp", "add", "qgis", "--", *add_args],
                capture_output=True,
                text=True,
            )
            label = "Codex CLI"

        if result.returncode == 0:
            print(f"  Configured {label}.")
        else:
            print(f"  Failed to configure {label}: {result.stderr.strip()}")
        return

    path = Path(info["path"])
    key = info["key"]
    if info.get("entry_format") == "opencode":
        entry = _opencode_server_entry(remote)
    else:
        entry = _server_entry(client_name, remote)

    config = _read_json(path)
    if path.exists():
        _backup(path)

    config.setdefault(key, {})
    config[key]["qgis"] = entry
    _write_json(path, config)
    print(f"  Wrote: {path}")


def unconfigure_client(client_name: str) -> None:
    registry = _client_registry()
    info = registry[client_name]

    # Hermes: manual YAML edit required — just advise the user
    if info.get("entry_format") == "hermes":
        hermes_cfg = info.get("hermes_cfg")
        cfg_hint = str(hermes_cfg) if hermes_cfg else "%APPDATA%\\Hermes\\config.yaml"
        print(f"  Hermes: remove the 'qgis' key from mcpServers in {cfg_hint} manually.")
        return

    if info.get("print_only"):
        cli_name = info.get("cli", "claude")
        cli_bin = shutil.which(cli_name)
        if not cli_bin:
            print(f"  '{cli_name}' CLI not found in PATH – skipping.")
            return

        if cli_name == "claude":
            remove_cmd = [cli_bin, "mcp", "remove", "-s", "user", "qgis"]
            label = "Claude Code"
        else:
            remove_cmd = [cli_bin, "mcp", "remove", "qgis"]
            label = "Codex CLI"

        result = subprocess.run(remove_cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  Removed qgis from {label}.")
        else:
            print(f"  Not configured in {label}: {result.stderr.strip()}")
        return

    path = Path(info["path"])
    key = info["key"]

    config = _read_json(path)
    if key in config and "qgis" in config[key]:
        _backup(path)
        del config[key]["qgis"]
        if not config[key]:
            del config[key]
        _write_json(path, config)
        print(f"  Removed qgis from: {path}")
    else:
        print(f"  Not configured: {path}")


# ── Interactive menu ────────────────────────────────────────────────────────

ALL_CLIENTS = ["claude-desktop", "cursor", "vscode", "windsurf", "zed", "claude-code", "codex", "opencode", "hermes"]


def interactive_menu() -> list[str]:
    print("\nAvailable MCP clients:")
    for i, name in enumerate(ALL_CLIENTS, 1):
        tag = " (project-local)" if name == "vscode" else ""
        tag = " (prints command)" if name == "claude-code" else tag
        print(f"  {i}. {name}{tag}")
    print("  a. All")
    print("  q. Skip client configuration")

    choice = input("\nSelect clients (comma-separated numbers, 'a', or 'q'): ").strip().lower()
    if choice == "q":
        return []
    if choice == "a":
        return list(ALL_CLIENTS)

    selected = []
    for part in choice.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(ALL_CLIENTS):
            selected.append(ALL_CLIENTS[int(part) - 1])
    return selected


def interactive_mode_choice() -> bool:
    choice = input(
        "\nInstall mode:\n  1. Local dev (uv run from repo)\n  2. Remote (uvx from GitHub)\nChoice [1]: "
    ).strip()
    return choice == "2"


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install QGIS MCP plugin and configure MCP clients.",
    )
    parser.add_argument("--profile", default="default", help="QGIS profile name (default: default)")
    parser.add_argument(
        "--qgis-version",
        default="auto",
        choices=["auto", "3", "4"],
        help="QGIS major version to target (default: auto-detect, prefers 4)",
    )
    parser.add_argument(
        "--clients", help="Comma-separated client names (e.g. claude-desktop,cursor)"
    )
    parser.add_argument("--non-interactive", action="store_true", help="Skip interactive prompts")
    parser.add_argument(
        "--remote", action="store_true", help="Use uvx from GitHub instead of local uv run"
    )
    parser.add_argument("--uninstall", action="store_true", help="Remove plugin and client configs")
    args = parser.parse_args()

    qgis_ver = args.qgis_version
    if qgis_ver == "auto":
        qgis_ver = _detect_qgis_version()

    print(f"QGIS MCP Installer ({'uninstall' if args.uninstall else 'install'})")
    print(f"Platform:     {sys.platform}")
    print(f"Profile:      {args.profile}")
    print(f"QGIS version: {qgis_ver}")
    print()

    # ── Plugin ──
    if args.uninstall:
        print("[1/3] Removing QGIS plugin...")
        uninstall_plugin(args.profile, qgis_ver)
    else:
        print("[1/3] Installing QGIS plugin...")
        install_plugin(args.profile, qgis_ver)

    # ── Dependencies (skip for uninstall and remote mode) ──
    if not args.uninstall and not args.remote:
        print("\n[2/3] Setting up dependencies...")
        setup_venv()

    # ── Clients ──
    if args.non_interactive:
        clients = [c.strip() for c in args.clients.split(",")] if args.clients else []
        remote = args.remote
    else:
        clients = interactive_menu()
        remote = interactive_mode_choice() if clients and not args.uninstall else args.remote

    valid = set(_client_registry())
    invalid = [c for c in clients if c not in valid]
    if invalid:
        sys.exit(f"Unknown clients: {', '.join(invalid)}.  Valid: {', '.join(sorted(valid))}")

    if clients:
        print(f"\n[3/3] {'Removing' if args.uninstall else 'Configuring'} MCP clients...")
        for client in clients:
            print(f"\n  -- {client} --")
            if args.uninstall:
                unconfigure_client(client)
            else:
                configure_client(client, remote)

    # ── Summary ──
    print("\n" + "=" * 50)
    if args.uninstall:
        print("Uninstall complete.")
    else:
        print("Installation complete.")
        print("\nNext steps:")
        print("  1. Restart QGIS and enable the 'QGIS MCP' plugin")
        print("  2. Click 'Start Server' in the MCP dock widget")
        print("  3. Restart your MCP client to pick up the new config")


if __name__ == "__main__":
    main()
