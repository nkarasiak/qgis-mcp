"""Tests for qgis_render_choropleth — pragmatic, mocked plugin executor.

Approach B (memory-layer rebuild) per design choice. Tests verify:
- CSV parse happens MCP-side via stdlib (no pandas)
- Plugin receives parsed value_dict, not raw CSV path
- Hard-fail when plugin reports JOIN_NO_MATCH (zero matches)
- Bad CSV column → FieldNotFoundError before plugin call
- Direct mode (no value_csv) passes value_dict=None
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qgis_mcp_workflows.errors import ExecutorError, FieldNotFoundError, JoinError
from qgis_mcp_workflows.server import qgis_render_choropleth


def _ok_response() -> dict:
    return {
        "output_path": "/tmp/x.png",
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [122.0, 24.0, 154.0, 46.0],
        "crs": "EPSG:4326", "n_layers": 1,
        "field": "total_trips", "n_classes": 5,
        "breaks": [0.0, 100.0, 500.0, 1000.0, 5000.0, 10000.0],
        "mode": "quantile",
        "min_value": 0.0, "max_value": 9876.0,
        "n_features": 47, "n_matched": 45, "n_unmatched": 2,
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("zone_id,total_trips\n", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    lines = [",".join(fields)]
    for r in rows:
        lines.append(",".join(str(r[f]) for f in fields))
    path.write_text("\n".join(lines), encoding="utf-8")


def test_csv_parsed_to_dict_then_dispatched(fake_executor, tmp_path: Path):
    csv = tmp_path / "values.csv"
    _write_csv(csv, [
        {"zone_id": "MFS01", "total_trips": 12345},
        {"zone_id": "MFS02", "total_trips": 67890},
    ])
    fake_executor.responses["render_choropleth"] = _ok_response()

    result = qgis_render_choropleth(
        zones_path="/zones.shp",
        value_field="total_trips",
        output_png="/out.png",
        value_csv=str(csv),
        join_field="zone_id",
    )

    cmd, params = fake_executor.calls[0]
    assert cmd == "render_choropleth"
    assert params["value_dict"] == {"MFS01": 12345.0, "MFS02": 67890.0}
    assert params["join_field"] == "zone_id"
    assert params["value_field"] == "total_trips"

    assert result.join is not None
    assert result.join.n_matched == 45
    assert result.join.n_unmatched == 2
    assert result.join.csv == os.path.abspath(str(csv))
    assert result.join.field == "zone_id"


def test_no_csv_passes_value_dict_none(fake_executor, tmp_path: Path):
    """Direct mode: value_field is already a column on zones — no join."""
    fake_executor.responses["render_choropleth"] = _ok_response()
    qgis_render_choropleth(
        zones_path="/zones.shp",
        value_field="population",
        output_png="/out.png",
    )
    params = fake_executor.calls[0][1]
    assert params["value_dict"] is None


def test_csv_missing_value_field_raises_field_not_found(fake_executor, tmp_path: Path):
    csv = tmp_path / "values.csv"
    _write_csv(csv, [{"zone_id": "MFS01", "TRIPS": 100}])
    with pytest.raises(FieldNotFoundError, match="total_trips"):
        qgis_render_choropleth(
            zones_path="/zones.shp",
            value_field="total_trips",
            output_png="/out.png",
            value_csv=str(csv),
        )
    # Plugin must NOT have been called — validation happens client-side.
    assert fake_executor.calls == []


def test_csv_missing_join_field_raises_field_not_found(fake_executor, tmp_path: Path):
    csv = tmp_path / "values.csv"
    _write_csv(csv, [{"REGION": "MFS01", "total_trips": 100}])
    with pytest.raises(FieldNotFoundError, match="zone_id"):
        qgis_render_choropleth(
            zones_path="/zones.shp",
            value_field="total_trips",
            output_png="/out.png",
            value_csv=str(csv),
            join_field="zone_id",
        )


def test_join_no_match_raises_join_error(fake_executor, tmp_path: Path):
    """Plugin's JOIN_NO_MATCH marker → typed JoinError on the MCP side."""
    csv = tmp_path / "values.csv"
    _write_csv(csv, [{"zone_id": "MFS01", "total_trips": 1}])

    def join_fail(_params):
        raise ExecutorError(
            "render_choropleth",
            "JOIN_NO_MATCH on zone_id: 0 matches. "
            "Sample CSV keys: ['MFS01']; sample layer keys: ['Hokkaido', 'Tokyo']",
        )
    fake_executor.responses["render_choropleth"] = join_fail

    with pytest.raises(JoinError) as exc_info:
        qgis_render_choropleth(
            zones_path="/zones.shp",
            value_field="total_trips",
            output_png="/out.png",
            value_csv=str(csv),
            join_field="zone_id",
        )
    assert "MFS01" in str(exc_info.value)
    assert "qgis_layer_inspect" in str(exc_info.value)


def test_csv_non_numeric_value_skipped_with_dict_omission(fake_executor, tmp_path: Path):
    """Non-numeric value rows are skipped (logged) but processing continues."""
    csv = tmp_path / "values.csv"
    csv.write_text("zone_id,total_trips\nMFS01,1234\nMFS02,oops\nMFS03,5678\n", encoding="utf-8")
    fake_executor.responses["render_choropleth"] = _ok_response()
    qgis_render_choropleth(
        zones_path="/zones.shp", value_field="total_trips",
        output_png="/out.png", value_csv=str(csv),
    )
    params = fake_executor.calls[0][1]
    assert "MFS02" not in params["value_dict"], "non-numeric row should be dropped"
    assert params["value_dict"]["MFS01"] == 1234.0
    assert params["value_dict"]["MFS03"] == 5678.0


def test_paths_resolved_to_absolute(fake_executor, tmp_path: Path):
    fake_executor.responses["render_choropleth"] = _ok_response()
    qgis_render_choropleth(
        zones_path="rel_zones.shp", value_field="x", output_png="rel_out.png",
        basemap_paths=["rel_basemap.shp"],
    )
    params = fake_executor.calls[0][1]
    assert os.path.isabs(params["zones_path"])
    assert os.path.isabs(params["output_png"])
    assert all(os.path.isabs(p) for p in params["basemap_paths"])


def test_diverging_and_center_thread_into_params(fake_executor):
    fake_executor.responses["render_choropleth"] = _ok_response()
    qgis_render_choropleth(
        zones_path="/z.shp", value_field="am_net", output_png="/o.png",
        diverging=True, center=0.0, palette="vik",
    )
    params = fake_executor.calls[0][1]
    assert params["diverging"] is True
    assert params["center"] == 0.0


def test_default_choropleth_is_not_diverging(fake_executor):
    fake_executor.responses["render_choropleth"] = _ok_response()
    qgis_render_choropleth(zones_path="/z.shp", value_field="x", output_png="/o.png")
    params = fake_executor.calls[0][1]
    assert params["diverging"] is False


def test_diverging_response_fields_echoed(fake_executor):
    resp = _ok_response()
    resp.update({"diverging": True, "center": 0.0, "diverging_one_sided": False})
    fake_executor.responses["render_choropleth"] = resp
    result = qgis_render_choropleth(
        zones_path="/z.shp", value_field="am_net", output_png="/o.png",
        diverging=True, center=0.0,
    )
    assert result.diverging is True
    assert result.center == 0.0
    assert result.diverging_one_sided is False


def test_label_field_threads_to_plugin(fake_executor):
    fake_executor.responses["render_choropleth"] = _ok_response()
    qgis_render_choropleth(
        zones_path="/z.shp", value_field="am_net", output_png="/o.png",
        label_field="city",
    )
    assert fake_executor.calls[0][1]["label_field"] == "city"
