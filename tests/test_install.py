"""Unit tests for install.py: JSONC stripping, atomic config writes, config merge."""

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
    assert (tmp_path / "cursor.json.bak").exists()


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
