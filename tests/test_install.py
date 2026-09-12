"""Unit tests for install.py: JSONC stripping, atomic config writes, config merge."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import install


def test_jsonc_keeps_comment_markers_inside_strings():
    text = '{"glob": "/*.log", "b": "x*/y", "url": "https://x/y", "ok": 1}'
    assert json.loads(install._jsonc_to_json(text)) == {
        "glob": "/*.log",
        "b": "x*/y",
        "url": "https://x/y",
        "ok": 1,
    }


def test_jsonc_strips_comments_and_trailing_commas():
    text = """{
        // line comment
        "a": 1, /* block
        spanning lines */
        "b": [1, 2,],
    }"""
    assert json.loads(install._jsonc_to_json(text)) == {"a": 1, "b": [1, 2]}


def test_write_json_is_atomic(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text('{"old": true}\n', encoding="utf-8")

    install._write_json(path, {"new": True})

    assert json.loads(path.read_text(encoding="utf-8")) == {"new": True}
    assert list(tmp_path.iterdir()) == [path], "temp file left behind"


def test_write_json_leaves_original_when_serialization_fails(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text('{"old": true}\n', encoding="utf-8")

    try:
        install._write_json(path, {"bad": object()})
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError on unserializable value")

    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}


def test_configure_client_merges_into_existing_config(tmp_path, monkeypatch):
    path = tmp_path / "cursor.json"
    path.write_text(
        json.dumps({"other": 1, "mcpServers": {"keepme": {"command": "x"}}}), encoding="utf-8"
    )
    monkeypatch.setattr(
        install, "_client_registry", lambda: {"cursor": {"path": path, "key": "mcpServers"}}
    )

    install.configure_client("cursor", remote=True)

    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["other"] == 1
    assert config["mcpServers"]["keepme"] == {"command": "x"}
    assert config["mcpServers"]["qgis"] == install._remote_entry()
    assert list(tmp_path.glob("cursor.json.bak-*")), "no backup written"


def test_unconfigure_client_drops_empty_key(tmp_path, monkeypatch):
    path = tmp_path / "cursor.json"
    path.write_text(json.dumps({"mcpServers": {"qgis": {"command": "uvx"}}}), encoding="utf-8")
    monkeypatch.setattr(
        install, "_client_registry", lambda: {"cursor": {"path": path, "key": "mcpServers"}}
    )

    install.unconfigure_client("cursor")

    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_remove_target_asks_before_deleting_a_real_directory(tmp_path, monkeypatch):
    target = tmp_path / "qgis_mcp_plugin"
    target.mkdir()
    (target / "metadata.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda *a: "n")

    try:
        install._remove_target(target)
    except SystemExit:
        pass
    else:
        raise AssertionError("expected SystemExit when the user declines")
    assert target.exists()

    monkeypatch.setattr("builtins.input", lambda *a: "y")
    install._remove_target(target)
    assert not target.exists()


def test_remove_target_never_asks_for_a_symlink(tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    install._remove_target(link)  # input() would raise OSError under pytest
    assert not link.is_symlink()


def test_configure_client_replaces_a_non_dict_key(tmp_path, monkeypatch):
    path = tmp_path / "zed.json"
    path.write_text(json.dumps({"context_servers": ["oops"]}), encoding="utf-8")
    monkeypatch.setattr(
        install, "_client_registry", lambda: {"zed": {"path": path, "key": "context_servers"}}
    )

    install.configure_client("zed", remote=True)

    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["context_servers"] == {"qgis": install._remote_entry()}


def test_backup_does_not_overwrite_an_earlier_backup(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("first", encoding="utf-8")
    install._backup(path)
    path.write_text("second", encoding="utf-8")
    install._backup(path)

    saved = sorted(b.read_text(encoding="utf-8") for b in tmp_path.glob("mcp.json.bak*"))
    assert saved == ["first", "second"]


def test_hermes_is_offered_on_windows_only():
    expected = sys.platform == "win32"
    assert ("hermes" in install.ALL_CLIENTS) is expected
    assert ("hermes" in install._client_registry()) is expected


def test_one_malformed_config_does_not_stop_the_next_client(tmp_path, monkeypatch, capsys):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json at all", encoding="utf-8")
    good = tmp_path / "good.json"
    monkeypatch.setattr(
        install,
        "_client_registry",
        lambda: {
            "broken": {"path": broken, "key": "mcpServers"},
            "good": {"path": good, "key": "mcpServers"},
        },
    )
    args = argparse.Namespace(uninstall=False, non_interactive=True, remote=True)

    install._do_clients(args, ["broken", "good"])

    config = json.loads(good.read_text(encoding="utf-8"))
    assert config["mcpServers"]["qgis"] == install._remote_entry()
    assert "Skipped broken" in capsys.readouterr().out
