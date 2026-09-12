#!/usr/bin/env python3
"""Multi-client installer for QGIS MCP.

Symlinks the QGIS plugin and configures MCP clients (Claude Desktop,
Cursor, VS Code Copilot, Windsurf, Zed, Claude Code, Codex CLI, opencode,
Kimi Code CLI, Gemini CLI, Qwen Code, GitHub Copilot CLI, LM Studio).

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
from datetime import datetime
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
        "claude-code": {"print_only": True, "cli": "claude"},
        "codex": {"print_only": True, "cli": "codex"},
        "opencode": {"path": opencode_cfg, "key": "mcp", "entry_format": "opencode"},
        # Windows only: the steps it prints name %APPDATA% paths that do not
        # exist elsewhere, so offering it on Linux/macOS only misleads.
        **(
            {"hermes": {"print_only": True, "entry_format": "hermes", "hermes_cfg": hermes_cfg}}
            if sys.platform == "win32"
            else {}
        ),
        "kimi": {"path": kimi_cfg, "key": "mcpServers"},
        "gemini": {"path": gemini_cfg, "key": "mcpServers"},
        "qwen": {"path": qwen_cfg, "key": "mcpServers"},
        "copilot-cli": {"path": copilot_cfg, "key": "mcpServers"},
        "lmstudio": {"path": lmstudio_cfg, "key": "mcpServers"},
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
        launch_cmd = f'uv --directory "{REPO_DIR}" run --no-sync src/qgis_mcp/server.py'
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
    print("  Hermes desktop app (Windows) - manual setup required:")
    print()
    print(f"  Step 1 - Create the launcher: {bat_path}")
    print()
    for line in bat_content.splitlines():
        print(f"    {line}")
    print()
    print(f"  Step 2 - Add to {cfg_path}:")
    print()
    bat_escaped = str(bat_path).replace("\\", "\\\\")
    print("    mcpServers:")
    print("      qgis:")
    print(f'        command: "{bat_escaped}"')
    print("        args: []")
    print()
    print("  See docs/agent-integration.md for full details.")


# ── Plugin installation ────────────────────────────────────────────────────


def _remove_target(target: Path, force: bool = False) -> None:
    """Remove a plugin target - handles files, symlinks, Windows junctions, and dirs.

    Path.is_symlink() returns False for Windows directory junctions (created via
    `mklink /J`), so we also check os.path.islink() and fall back to rmdir() for
    junctions before resorting to shutil.rmtree() on real directories.

    A real directory is a Plugin Manager install, not something this script put
    there, so deleting it asks first unless `force` is set.
    """
    if target.is_symlink() or os.path.islink(target) or target.is_file():
        target.unlink()
        return
    if sys.platform == "win32":
        try:
            target.rmdir()  # cleanly removes a junction without touching the target
            return
        except OSError:
            pass
    if not force:
        answer = input(f"  {target} is a real directory, not a link. Delete it? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            sys.exit("Aborted: existing plugin directory left in place.")
    shutil.rmtree(target)


def install_plugin(profile: str, version: str = "auto", force: bool = False) -> Path:
    plugins_dir = qgis_plugins_dir(profile, version)
    target = plugins_dir / "qgis_mcp_plugin"

    if target.is_symlink() or target.exists() or os.path.islink(target):
        if target.is_symlink() and target.resolve() == PLUGIN_SRC.resolve():
            print(f"  Plugin already linked: {target}")
            return target
        print(f"  Removing existing: {target}")
        _remove_target(target, force)

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


def uninstall_plugin(profile: str, version: str = "auto", force: bool = False) -> None:
    target = qgis_plugins_dir(profile, version) / "qgis_mcp_plugin"
    if target.is_symlink() or target.exists() or os.path.islink(target):
        _remove_target(target, force)
        print(f"  Removed: {target}")
    else:
        print(f"  Not installed: {target}")


# ── Client configuration ───────────────────────────────────────────────────


def _jsonc_to_json(text: str) -> str:
    """Convert potential JSONC json file to valid JSON.

    One pass, with the string-literal branch first so anything quoted is kept
    verbatim: URLs (`https://`), a `//` or a `/*` inside a value. Block and line
    comments outside strings are dropped, then trailing commas in objects and
    arrays. `[^\\n]` in the line-comment branch because DOTALL is on for the
    block comment branch and `.` would otherwise swallow the rest of the file.
    """
    stripped = re.sub(
        r'("(?:\\.|[^"\\])*")|/\*.*?\*/|//[^\n]*',
        lambda m: m.group(1) or "",
        text,
        flags=re.DOTALL,
    )
    return re.sub(r",\s*([}\]])", r"\1", stripped)


def _read_json(path: Path) -> dict:
    if not path.exists() or not (text := path.read_text(encoding="utf-8").strip()):
        return {}

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    try:
        cleaned = _jsonc_to_json(text)
        config = json.loads(cleaned)
        print(f"  Note: {path} has comments; they are not preserved when it is rewritten.")
        return config
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse {path}: not valid JSON or JSONC. Error: {e}") from e


def _backup(path: Path) -> None:
    """Copy a config aside under a timestamped `.bak-` suffix.

    Timestamped rather than a fixed `.bak`: a second run would otherwise
    overwrite the only untouched copy with the one the first run wrote.
    """
    if not path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    bak = path.with_suffix(f"{path.suffix}.bak-{stamp}")
    n = 0
    while bak.exists():  # two runs inside the same second
        n += 1
        bak = path.with_suffix(f"{path.suffix}.bak-{stamp}-{n}")
    shutil.copy2(path, bak)
    print(f"  Backup: {bak}")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Serialize into a sibling temp file first: an open(path, "w") truncates the
    # existing config before json.dumps runs, so a failure there loses it.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def configure_client(client_name: str, remote: bool) -> None:
    registry = _client_registry()
    info = registry[client_name]

    # Hermes desktop app: YAML config + bat launcher - print instructions only
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
                "uv",
                "run",
                "--no-sync",
                "--directory",
                str(REPO_DIR),
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
        entry = _remote_entry() if remote else _local_entry()

    config = _read_json(path)
    if path.exists():
        _backup(path)

    # Not setdefault: an existing key holding a list or a string would make the
    # assignment below raise instead of being replaced.
    if not isinstance(config.get(key), dict):
        config[key] = {}
    config[key]["qgis"] = entry
    _write_json(path, config)
    print(f"  Wrote: {path}")


def unconfigure_client(client_name: str) -> None:
    registry = _client_registry()
    info = registry[client_name]

    # Hermes: manual YAML edit required - just advise the user
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

ALL_CLIENTS = [
    "claude-desktop",
    "cursor",
    "vscode",
    "windsurf",
    "zed",
    "claude-code",
    "codex",
    "opencode",
    *(["hermes"] if sys.platform == "win32" else []),
    "kimi",
    "gemini",
    "qwen",
    "copilot-cli",
    "lmstudio",
]


def interactive_menu() -> list[str]:
    print("\nAvailable MCP clients:")
    tags = {"vscode": " (project-local)", "hermes": " (manual steps)"}
    for i, name in enumerate(ALL_CLIENTS, 1):
        print(f"  {i}. {name}{tags.get(name, '')}")
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


def _parse_args() -> argparse.Namespace:
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
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete an existing real plugin directory without asking",
    )
    return parser.parse_args()


def _do_plugin(args: argparse.Namespace, qgis_ver: str, force: bool) -> None:
    if args.uninstall:
        print("[1/3] Removing QGIS plugin...")
        uninstall_plugin(args.profile, qgis_ver, force)
    else:
        print("[1/3] Installing QGIS plugin...")
        install_plugin(args.profile, qgis_ver, force)


def _do_deps(args: argparse.Namespace) -> None:
    """Set up the venv, which an uninstall and remote (uvx) mode do not need."""
    if args.uninstall or args.remote:
        return
    print("\n[2/3] Setting up dependencies...")
    setup_venv()


def _do_clients(args: argparse.Namespace, clients: list[str]) -> None:
    # --clients is honoured with or without --non-interactive; only ask when it is absent.
    if not clients and not args.non_interactive:
        clients = interactive_menu()
    if not clients:
        return

    # --remote wins outright: prompting would let Enter flip it back to local after
    # the venv step was already skipped, leaving the config pointing at nothing.
    remote = args.remote or (
        interactive_mode_choice() if not args.uninstall and not args.non_interactive else False
    )

    print(f"\n[3/3] {'Removing' if args.uninstall else 'Configuring'} MCP clients...")
    for client in clients:
        print(f"\n  -- {client} --")
        try:
            if args.uninstall:
                unconfigure_client(client)
            else:
                configure_client(client, remote)
        except (ValueError, OSError) as exc:
            # One unreadable or malformed config must not cost the user every
            # other client they asked for.
            print(f"  Skipped {client}: {exc}")


def main() -> None:
    args = _parse_args()

    # Validate before anything is touched, so a typo does not cost a symlink or a venv.
    clients = [c.strip() for c in args.clients.split(",")] if args.clients else []
    valid = set(_client_registry())
    invalid = [c for c in clients if c not in valid]
    if invalid:
        sys.exit(f"Unknown clients: {', '.join(invalid)}.  Valid: {', '.join(sorted(valid))}")

    force = args.force or args.non_interactive
    qgis_ver = args.qgis_version
    if qgis_ver == "auto":
        qgis_ver = _detect_qgis_version()

    print(f"QGIS MCP Installer ({'uninstall' if args.uninstall else 'install'})")
    print(f"Platform:     {sys.platform}")
    print(f"Profile:      {args.profile}")
    print(f"QGIS version: {qgis_ver}")
    print()

    _do_plugin(args, qgis_ver, force)
    _do_deps(args)
    _do_clients(args, clients)

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
