"""Tests for qgis_render_od_flows — mocked plugin executor.

Verifies MCP-side responsibilities:
- CSV header validation (origin/dest/value/zone_id columns)
- top_n truncation to strongest flows (sort descending)
- Unmatched zone counts surface from plugin response
- Path resolution to absolute
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qgis_mcp_workflows.errors import FieldNotFoundError
from qgis_mcp_workflows.server import qgis_render_od_flows


def _ok_response(**overrides) -> dict:
    base = {
        "output_path": "/tmp/od.png",
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [139.68, 35.69, 139.74, 35.73],
        "crs": "EPSG:4326", "n_layers": 2,
        "n_flows": 6,
        "n_flows_rendered": 6,
        "n_zones": 4,
        "max_flow": 200.0,
        "min_flow_rendered": 25.0,
        "n_unmatched_origins": 0,
        "n_unmatched_destinations": 0,
    }
    base.update(overrides)
    return base


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    lines = [",".join(header)]
    for r in rows:
        lines.append(",".join(str(x) for x in r))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


FIXTURE_DIR = Path(__file__).parent / "fixtures"
TINY_OD = FIXTURE_DIR / "tiny_od.csv"
TINY_ZONES = FIXTURE_DIR / "tiny_zones.geojson"


def test_od_csv_dispatched_with_flows(fake_executor):
    """Happy path: tiny fixture parsed, dispatched to plugin."""
    fake_executor.responses["render_od_flows"] = _ok_response()
    result = qgis_render_od_flows(
        od_csv=str(TINY_OD),
        zones_layer_path=str(TINY_ZONES),
        output_png="/tmp/od.png",
    )
    cmd, params = fake_executor.calls[0]
    assert cmd == "render_od_flows"
    assert len(params["flows"]) == 6
    assert params["flows"][0]["origin"] in {"Z01", "Z02", "Z03", "Z04"}
    assert "value" in params["flows"][0]
    assert os.path.isabs(params["zones_path"])
    assert result.n_flows == 6


def test_top_n_truncates_to_strongest_flows(fake_executor):
    """top_n=2 sends only the 2 highest-trip_count flows."""
    fake_executor.responses["render_od_flows"] = _ok_response(
        n_flows=2, n_flows_rendered=2
    )
    qgis_render_od_flows(
        od_csv=str(TINY_OD),
        zones_layer_path=str(TINY_ZONES),
        output_png="/tmp/od.png",
        top_n=2,
    )
    params = fake_executor.calls[0][1]
    flows = params["flows"]
    assert len(flows) == 2
    # Tiny fixture: max trip_count=200 (Z02→Z03), next=150 (Z03→Z04)
    assert flows[0]["value"] == 200.0
    assert flows[1]["value"] == 150.0


def test_flows_sorted_descending_by_value(fake_executor):
    """Even without top_n, flows are sorted descending so plugin can compute max easily."""
    fake_executor.responses["render_od_flows"] = _ok_response()
    qgis_render_od_flows(
        od_csv=str(TINY_OD),
        zones_layer_path=str(TINY_ZONES),
        output_png="/tmp/od.png",
    )
    flows = fake_executor.calls[0][1]["flows"]
    values = [f["value"] for f in flows]
    assert values == sorted(values, reverse=True)


def test_unmatched_zones_surface_in_response(fake_executor):
    fake_executor.responses["render_od_flows"] = _ok_response(
        n_flows=6, n_flows_rendered=4,
        n_unmatched_origins=1, n_unmatched_destinations=1,
    )
    result = qgis_render_od_flows(
        od_csv=str(TINY_OD),
        zones_layer_path=str(TINY_ZONES),
        output_png="/tmp/od.png",
    )
    assert result.n_unmatched_origins == 1
    assert result.n_unmatched_destinations == 1
    assert result.n_flows_rendered == 4


def test_missing_origin_col_raises_field_not_found(fake_executor, tmp_path: Path):
    bad = tmp_path / "bad.csv"
    _write_csv(bad, ["from_zone", "destination", "trip_count"], [["Z01", "Z02", "10"]])
    with pytest.raises(FieldNotFoundError, match="origin"):
        qgis_render_od_flows(
            od_csv=str(bad),
            zones_layer_path=str(TINY_ZONES),
            output_png="/tmp/od.png",
        )
    assert fake_executor.calls == []


def test_missing_value_col_raises_field_not_found(fake_executor, tmp_path: Path):
    bad = tmp_path / "bad.csv"
    _write_csv(bad, ["origin", "destination", "count"], [["Z01", "Z02", "10"]])
    with pytest.raises(FieldNotFoundError, match="trip_count"):
        qgis_render_od_flows(
            od_csv=str(bad),
            zones_layer_path=str(TINY_ZONES),
            output_png="/tmp/od.png",
        )


def test_paths_resolved_to_absolute(fake_executor):
    fake_executor.responses["render_od_flows"] = _ok_response()
    qgis_render_od_flows(
        od_csv=str(TINY_OD),
        zones_layer_path=str(TINY_ZONES),
        output_png="rel_out.png",
        basemap_paths=["rel_basemap.shp"],
    )
    params = fake_executor.calls[0][1]
    assert os.path.isabs(params["output_png"])
    assert os.path.isabs(params["zones_path"])
    assert all(os.path.isabs(p) for p in params["basemap_paths"])


def test_zone_id_field_passed_through(fake_executor):
    """Custom zone_id_field (e.g., PRF_CODE) must reach the plugin."""
    fake_executor.responses["render_od_flows"] = _ok_response()
    qgis_render_od_flows(
        od_csv=str(TINY_OD),
        zones_layer_path=str(TINY_ZONES),
        output_png="/tmp/od.png",
        zone_id_field="PRF_CODE",
    )
    params = fake_executor.calls[0][1]
    assert params["zone_id_field"] == "PRF_CODE"


def test_basemap_preset_threads_spec(fake_executor):
    fake_executor.responses["render_od_flows"] = _ok_response()
    qgis_render_od_flows(
        od_csv=str(TINY_OD), zones_layer_path=str(TINY_ZONES),
        output_png="/tmp/od.png", basemap="positron", basemap_opacity=0.7,
    )
    spec = fake_executor.calls[0][1]["basemap_spec"]
    assert spec is not None and spec["kind"] == "xyz"
    assert spec["opacity"] == 0.7


def test_default_sends_no_basemap_spec(fake_executor):
    fake_executor.responses["render_od_flows"] = _ok_response()
    qgis_render_od_flows(
        od_csv=str(TINY_OD), zones_layer_path=str(TINY_ZONES), output_png="/tmp/od.png",
    )
    assert fake_executor.calls[0][1]["basemap_spec"] is None


def test_arc_style_threads_to_plugin(fake_executor):
    fake_executor.responses["render_od_flows"] = _ok_response()
    qgis_render_od_flows(
        od_csv=str(TINY_OD), zones_layer_path=str(TINY_ZONES),
        output_png="/tmp/od.png", arc_style="curved",
    )
    assert fake_executor.calls[0][1]["arc_style"] == "curved"


def test_default_arc_style_is_line(fake_executor):
    fake_executor.responses["render_od_flows"] = _ok_response()
    qgis_render_od_flows(
        od_csv=str(TINY_OD), zones_layer_path=str(TINY_ZONES), output_png="/tmp/od.png",
    )
    assert fake_executor.calls[0][1]["arc_style"] == "line"
