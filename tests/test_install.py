"""Tests for install.py — symlinks, client configs, CLI parsing.

Uses monkeypatched home/appdata directories so we never touch the real QGIS profile
or Claude Desktop config. Symlink creation may fall back to a junction on Windows
without admin; tests assert the target exists either way.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def install_mod(tmp_path, monkeypatch):
    """Load install.py as a module, redirected to tmp_path for home + appdata."""
    spec = importlib.util.spec_from_file_location("install_mod", REPO_ROOT / "install.py")
    install = importlib.util.module_from_spec(spec)
    sys.modules["install_mod"] = install
    spec.loader.exec_module(install)

    fake_home = tmp_path / "home"
    fake_appdata = tmp_path / "appdata"
    fake_home.mkdir()
    fake_appdata.mkdir()

    monkeypatch.setattr(install, "_home", lambda: fake_home)
    monkeypatch.setattr(install, "_appdata", lambda: fake_appdata)

    yield install


# ── qgis_plugins_dir ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "platform,expected_segments",
    [
        ("linux", [".local", "share", "QGIS", "QGIS3", "profiles", "default", "python", "plugins"]),
        ("darwin", ["Library", "Application Support", "QGIS", "QGIS3", "profiles", "default", "python", "plugins"]),
        ("win32", ["QGIS", "QGIS3", "profiles", "default", "python", "plugins"]),
    ],
)
def test_qgis_plugins_dir_per_platform(install_mod, monkeypatch, platform, expected_segments):
    monkeypatch.setattr(sys, "platform", platform)
    p = install_mod.qgis_plugins_dir("default")
    path_str = str(p)
    for seg in expected_segments:
        assert seg in path_str, f"{seg!r} not in {path_str!r} (platform={platform})"


# ── install_plugin ──────────────────────────────────────────────────────────


def test_install_plugin_creates_symlink(install_mod):
    target = install_mod.install_plugin("default")
    assert target.exists()
    # Symlink or junction on Windows — both are reported as "exists" + dir-like.
    assert target.is_dir() or target.is_symlink()


def test_install_plugin_idempotent(install_mod, capsys):
    install_mod.install_plugin("default")
    capsys.readouterr()  # clear
    install_mod.install_plugin("default")  # second call
    out = capsys.readouterr().out
    assert "already linked" in out or "Linked:" in out


def test_install_plugin_replaces_stale_symlink(install_mod, tmp_path):
    """If a non-matching link/dir exists at the target, it gets replaced."""
    target_dir = install_mod.qgis_plugins_dir("default")
    target_dir.mkdir(parents=True, exist_ok=True)
    stale_target = target_dir / "qgis_mcp_north_plugin"
    other_src = tmp_path / "stale_source"
    other_src.mkdir()
    # Create a non-matching symlink (or plain dir if symlink fails on Windows)
    try:
        stale_target.symlink_to(other_src, target_is_directory=True)
    except OSError:
        stale_target.mkdir()

    install_mod.install_plugin("default")
    # After install, the target should exist and point at the real PLUGIN_SRC.
    final = target_dir / "qgis_mcp_north_plugin"
    assert final.exists()
    if final.is_symlink():
        assert final.resolve() == install_mod.PLUGIN_SRC.resolve()


def test_uninstall_plugin_removes_symlink(install_mod):
    install_mod.install_plugin("default")
    target = install_mod.qgis_plugins_dir("default") / "qgis_mcp_north_plugin"
    assert target.exists()
    install_mod.uninstall_plugin("default")
    assert not target.exists()


def test_uninstall_plugin_when_not_installed(install_mod, capsys):
    install_mod.uninstall_plugin("default")
    out = capsys.readouterr().out
    assert "Not installed" in out


# ── configure_client ────────────────────────────────────────────────────────


def test_configure_client_writes_mcpservers_block(install_mod):
    install_mod.configure_client("claude-desktop", remote=False)
    cfg_path = install_mod._client_registry()["claude-desktop"]["path"]
    data = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    assert "mcpServers" in data
    assert "qgis-north" in data["mcpServers"]


def test_configure_client_preserves_existing_keys(install_mod):
    cfg_path = Path(install_mod._client_registry()["claude-desktop"]["path"])
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps({"mcpServers": {"existing-server": {"command": "x"}}, "other_key": 42}),
        encoding="utf-8",
    )

    install_mod.configure_client("claude-desktop", remote=False)
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "existing-server" in data["mcpServers"]
    assert "qgis-north" in data["mcpServers"]
    assert data["other_key"] == 42


def test_configure_client_creates_backup_when_replacing(install_mod):
    cfg_path = Path(install_mod._client_registry()["claude-desktop"]["path"])
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    install_mod.configure_client("claude-desktop", remote=False)
    backup = cfg_path.with_suffix(cfg_path.suffix + ".bak")
    assert backup.exists(), "backup .bak file should be created when config exists"


def test_configure_client_remote_uses_uvx(install_mod):
    install_mod.configure_client("claude-desktop", remote=True)
    cfg_path = install_mod._client_registry()["claude-desktop"]["path"]
    data = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    entry = data["mcpServers"]["qgis-north"]
    assert entry["command"] == "uvx"


# ── CLI parsing ─────────────────────────────────────────────────────────────


def test_unknown_client_exits(install_mod, monkeypatch):
    """install.main() exits with an error when --clients lists an unknown client."""
    monkeypatch.setattr(sys, "argv", ["install.py", "--non-interactive", "--clients", "not-a-real-client"])
    with pytest.raises(SystemExit) as exc:
        install_mod.main()
    assert "Unknown clients" in str(exc.value) or exc.value.code is not None
