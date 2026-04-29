#!/usr/bin/env python3
"""Multi-client installer for QGIS MCP.

Symlinks the QGIS plugin and configures MCP clients (Claude Desktop,
Cursor, VS Code Copilot, Windsurf, Zed, Claude Code).

Usage:
    python install.py                          # Interactive menu
    python install.py --non-interactive --clients claude-desktop,cursor
    python install.py --remote                 # Use uvx (no local clone needed)
    python install.py --uninstall --clients cursor
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
PLUGIN_SRC = REPO_DIR / "qgis_mcp_plugin"
GITHUB_URL = "git+https://github.com/nkarasiak/qgis-mcp.git"

# ── Platform helpers ────────────────────────────────────────────────────────


def _home() -> Path:
    return Path.home()


def _appdata() -> Path:
    """Windows %APPDATA% or fallback."""
    return Path(os.environ.get("APPDATA", _home() / "AppData" / "Roaming"))


def qgis_plugins_dir(profile: str) -> Path:
    base = {
        "linux": _home() / ".local" / "share" / "QGIS" / "QGIS3",
        "darwin": _home() / "Library" / "Application Support" / "QGIS" / "QGIS3",
        "win32": _appdata() / "QGIS" / "QGIS3",
    }.get(sys.platform)
    if base is None:
        sys.exit(f"Unsupported platform: {sys.platform}")
    return base / "profiles" / profile / "python" / "plugins"


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
    windsurf_cfg = home / ".windsurf" / "mcp.json"
    vscode_cfg = REPO_DIR / ".vscode" / "mcp.json"

    if sys.platform == "darwin":
        zed_cfg = home / ".config" / "zed" / "settings.json"
    elif sys.platform == "win32":
        zed_cfg = appdata / "Zed" / "settings.json"
    else:
        zed_cfg = home / ".config" / "zed" / "settings.json"

    return {
        "claude-desktop": {"path": claude_cfg, "key": "mcpServers"},
        "cursor": {"path": cursor_cfg, "key": "mcpServers"},
        "vscode": {"path": vscode_cfg, "key": "mcpServers", "project_local": True},
        "windsurf": {"path": windsurf_cfg, "key": "mcpServers"},
        "zed": {"path": zed_cfg, "key": "context_servers"},
        "claude-code": {"print_only": True},
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


def _zed_local_entry() -> dict:
    if shutil.which("uv"):
        return {
            "command": {
                "path": "uv",
                "args": [
                    "--directory",
                    str(REPO_DIR),
                    "run",
                    "--no-sync",
                    "src/qgis_mcp/server.py",
                ],
                "env": {"QGIS_MCP_TRANSPORT": "stdio"},
            },
            "settings": {},
        }
    return {
        "command": {
            "path": str(_venv_python()),
            "args": [str(REPO_DIR / "src" / "qgis_mcp" / "server.py")],
            "env": {"QGIS_MCP_TRANSPORT": "stdio"},
        },
        "settings": {},
    }


def _zed_remote_entry() -> dict:
    return {
        "command": {
            "path": "uvx",
            "args": ["--from", GITHUB_URL, "qgis-mcp-server"],
            "env": {"QGIS_MCP_TRANSPORT": "stdio"},
        },
        "settings": {},
    }


def _server_entry(client: str, remote: bool) -> dict:
    if client == "zed":
        return _zed_remote_entry() if remote else _zed_local_entry()
    return _remote_entry() if remote else _local_entry()


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


def install_plugin(profile: str) -> Path:
    plugins_dir = qgis_plugins_dir(profile)
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
            os.system(f'mklink /J "{target}" "{PLUGIN_SRC}"')
    else:
        target.symlink_to(PLUGIN_SRC)

    print(f"  Linked: {target} -> {PLUGIN_SRC}")
    return target


def uninstall_plugin(profile: str) -> None:
    target = qgis_plugins_dir(profile) / "qgis_mcp_plugin"
    if target.is_symlink() or target.exists() or os.path.islink(target):
        _remove_target(target)
        print(f"  Removed: {target}")
    else:
        print(f"  Not installed: {target}")


# ── Client configuration ───────────────────────────────────────────────────


def _read_json(path: Path) -> dict:
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        return json.loads(text) if text else {}
    return {}


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

    # Claude Code: print command only
    if info.get("print_only"):
        if remote:
            cmd = f'claude mcp add qgis -- uvx --from "{GITHUB_URL}" qgis-mcp-server'
        elif shutil.which("uv"):
            cmd = (
                f'claude mcp add qgis -- uv --directory "{REPO_DIR}" '
                "run --no-sync src/qgis_mcp/server.py"
            )
        else:
            python = str(_venv_python())
            server = str(REPO_DIR / "src" / "qgis_mcp" / "server.py")
            cmd = f'claude mcp add qgis -- "{python}" "{server}"'
        print(f"  {cmd}")
        return

    path = Path(info["path"])
    key = info["key"]
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

    if info.get("print_only"):
        print("  Run: claude mcp remove qgis")
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

ALL_CLIENTS = ["claude-desktop", "cursor", "vscode", "windsurf", "zed", "claude-code"]


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
        "--clients", help="Comma-separated client names (e.g. claude-desktop,cursor)"
    )
    parser.add_argument("--non-interactive", action="store_true", help="Skip interactive prompts")
    parser.add_argument(
        "--remote", action="store_true", help="Use uvx from GitHub instead of local uv run"
    )
    parser.add_argument("--uninstall", action="store_true", help="Remove plugin and client configs")
    args = parser.parse_args()

    print(f"QGIS MCP Installer ({'uninstall' if args.uninstall else 'install'})")
    print(f"Platform: {sys.platform}")
    print(f"Profile:  {args.profile}")
    print()

    # ── Plugin ──
    if args.uninstall:
        print("[1/3] Removing QGIS plugin...")
        uninstall_plugin(args.profile)
    else:
        print("[1/3] Installing QGIS plugin...")
        install_plugin(args.profile)

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
