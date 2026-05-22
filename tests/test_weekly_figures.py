"""Tests for scripts/weekly_figures.py — orchestration only.

Uses FakeExecutor + tmpdir output. No real PFLOW data accessed; the script's
--demo-mode flag (uses tests/fixtures/) is the test path.
"""

from __future__ import annotations

from pathlib import Path


def test_demo_mode_renders_three_figures(fake_executor, tmp_path: Path, monkeypatch):
    """In demo mode, the script invokes choropleth + trajectory + (optionally) link-density.

    Each call lands in tmp_path with a date-stamped subdir.
    """
    from scripts.weekly_figures import run_weekly

    # Wire all expected tool responses (FakeExecutor needs every command pre-scripted)
    fake_executor.responses["add_vector_layer"] = {"id": "L1", "name": "zones"}
    fake_executor.responses["get_layer_info"] = {
        "type": "vector_2", "crs": "EPSG:4326",
        "extent": {"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1},
        "fields": [{"name": "zone_id", "type": "String"}, {"name": "total_trips", "type": "Integer"}],
        "feature_count": 5,
    }
    fake_executor.responses["remove_layer"] = {"ok": True}
    fake_executor.responses["render_choropleth"] = {
        "output_path": str(tmp_path / "choropleth.png"),
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0.0, 0.0, 1.0, 1.0], "crs": "EPSG:4326", "n_layers": 1,
        "field": "total_trips", "n_classes": 5, "breaks": [10, 20, 30, 40, 50],
        "mode": "quantile", "min_value": 0.0, "max_value": 100.0,
        "n_features": 5, "n_matched": 5, "n_unmatched": 0,
    }
    fake_executor.responses["render_trajectory"] = {
        "output_path": str(tmp_path / "trajectory.png"),
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0.0, 0.0, 1.0, 1.0], "crs": "EPSG:4326", "n_layers": 1,
        "n_trajectories": 3, "n_points_total": 30, "n_points_rendered": 30,
        "downsampled": False, "time_range": None, "modes": None,
        "used_movingpandas": False,
    }

    out_dir = tmp_path / "weekly"
    manifest = run_weekly(
        output_root=out_dir,
        demo_mode=True,
        with_link_density=False,
        date_str="2026-05-22",
    )

    assert (out_dir / "2026-05-22").exists()
    assert manifest["date"] == "2026-05-22"
    assert "choropleth" in manifest["figures"]
    assert "trajectory" in manifest["figures"]
    # link_density skipped because with_link_density=False
    assert "link_density" not in manifest["figures"]


def test_demo_mode_includes_link_density_when_flag_set(fake_executor, tmp_path: Path):
    """With --with-link-density, the link-density tool is invoked too."""
    from scripts.weekly_figures import run_weekly

    fake_executor.responses["add_vector_layer"] = {"id": "L1", "name": "zones"}
    fake_executor.responses["get_layer_info"] = {
        "type": "vector_2", "crs": "EPSG:4326",
        "extent": {"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1},
        "fields": [{"name": "zone_id", "type": "String"}],
        "feature_count": 5,
    }
    fake_executor.responses["remove_layer"] = {"ok": True}
    fake_executor.responses["render_choropleth"] = {
        "output_path": str(tmp_path / "choropleth.png"),
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0.0, 0.0, 1.0, 1.0], "crs": "EPSG:4326", "n_layers": 1,
        "field": "total_trips", "n_classes": 5, "breaks": [10, 20, 30, 40, 50],
        "mode": "quantile", "min_value": 0.0, "max_value": 100.0,
        "n_features": 5, "n_matched": 5, "n_unmatched": 0,
    }
    fake_executor.responses["render_trajectory"] = {
        "output_path": str(tmp_path / "trajectory.png"),
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0.0, 0.0, 1.0, 1.0], "crs": "EPSG:4326", "n_layers": 1,
        "n_trajectories": 3, "n_points_total": 30, "n_points_rendered": 30,
        "downsampled": False, "time_range": None, "modes": None,
        "used_movingpandas": False,
    }
    fake_executor.responses["render_link_density"] = {
        "output_path": str(tmp_path / "link_density.png"),
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0.0, 0.0, 1.0, 1.0], "crs": "EPSG:4326", "n_layers": 1,
        "n_links_with_traffic": 5, "n_links_rendered": 5,
        "n_unmatched_link_ids": 0,
        "density_field": "n_points",
        "breaks": [1, 2, 3, 4, 5],
        "mode": "quantile",
        "min_density": 1.0, "max_density": 5.0,
    }

    manifest = run_weekly(
        output_root=tmp_path / "weekly",
        demo_mode=True,
        with_link_density=True,
        date_str="2026-05-22",
    )

    assert "link_density" in manifest["figures"]


def test_manifest_written_as_json_alongside_figures(fake_executor, tmp_path: Path):
    """run_weekly writes a manifest.json so /kb-report can discover the figures."""
    import json

    from scripts.weekly_figures import run_weekly

    fake_executor.responses["add_vector_layer"] = {"id": "L1", "name": "zones"}
    fake_executor.responses["get_layer_info"] = {
        "type": "vector_2", "crs": "EPSG:4326",
        "extent": {"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1},
        "fields": [{"name": "zone_id", "type": "String"}],
        "feature_count": 5,
    }
    fake_executor.responses["remove_layer"] = {"ok": True}
    fake_executor.responses["render_choropleth"] = {
        "output_path": str(tmp_path / "choropleth.png"),
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0.0, 0.0, 1.0, 1.0], "crs": "EPSG:4326", "n_layers": 1,
        "field": "total_trips", "n_classes": 5, "breaks": [10, 20, 30, 40, 50],
        "mode": "quantile", "min_value": 0.0, "max_value": 100.0,
        "n_features": 5, "n_matched": 5, "n_unmatched": 0,
    }
    fake_executor.responses["render_trajectory"] = {
        "output_path": str(tmp_path / "trajectory.png"),
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0.0, 0.0, 1.0, 1.0], "crs": "EPSG:4326", "n_layers": 1,
        "n_trajectories": 3, "n_points_total": 30, "n_points_rendered": 30,
        "downsampled": False, "time_range": None, "modes": None,
        "used_movingpandas": False,
    }

    out_dir = tmp_path / "weekly"
    run_weekly(
        output_root=out_dir, demo_mode=True,
        with_link_density=False, date_str="2026-05-22",
    )

    manifest_path = out_dir / "2026-05-22" / "manifest.json"
    assert manifest_path.exists()
    parsed = json.loads(manifest_path.read_text())
    assert parsed["date"] == "2026-05-22"
    assert set(parsed["figures"].keys()) == {"choropleth", "trajectory"}
