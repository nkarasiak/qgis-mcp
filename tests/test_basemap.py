"""Tests for basemap tile support on render tools (live XYZ presets).

Server-side unit tests with the mocked executor. The plugin-side PyQGIS
(QgsRasterLayer type=xyz, EPSG:3857 reprojection) is verified live against a
running QGIS instance, not here — there is no QGIS in the unit-test process.

What these lock down:
- preset → basemap_spec resolution (the wire shape sent to the plugin)
- "none" preserves the legacy no-basemap behavior (back-compat)
- the spec is threaded into the dispatch params untouched
- provider attribution round-trips back into the response model
"""

from __future__ import annotations

from qgis_mcp_workflows.server import _resolve_basemap, qgis_render_choropleth


def _ok_response() -> dict:
    return {
        "output_path": "/tmp/x.png",
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [15555000.0, 4256000.0, 15566000.0, 4263000.0],
        "crs": "EPSG:3857", "n_layers": 2,
        "field": "total_trips", "n_classes": 5,
        "breaks": [0.0, 100.0, 500.0, 1000.0, 5000.0, 10000.0],
        "mode": "quantile",
        "min_value": 0.0, "max_value": 9876.0,
        "n_features": 4, "n_matched": 4, "n_unmatched": 0,
        "basemap_attribution": "© OpenStreetMap contributors © CARTO",
        "basemap_source": "positron (live xyz)",
    }


def test_resolve_basemap_none_returns_none():
    assert _resolve_basemap("none", 1.0) is None


def test_resolve_basemap_positron_returns_xyz_spec():
    spec = _resolve_basemap("positron", 0.85)
    assert spec["kind"] == "xyz"
    assert "cartocdn" in spec["url"]
    assert spec["attribution"]  # non-empty provider credit
    assert spec["opacity"] == 0.85
    assert spec["zmin"] == 0
    assert spec["zmax"] >= 1


def test_resolve_basemap_esri_imagery_present():
    spec = _resolve_basemap("esri_imagery", 1.0)
    assert "World_Imagery" in spec["url"]
    assert spec["attribution"]


def test_choropleth_default_sends_no_basemap_spec(fake_executor):
    fake_executor.responses["render_choropleth"] = _ok_response()
    qgis_render_choropleth(
        zones_path="/z.shp", value_field="population", output_png="/o.png"
    )
    params = fake_executor.calls[0][1]
    assert params["basemap_spec"] is None


def test_choropleth_basemap_threads_spec_into_params(fake_executor):
    fake_executor.responses["render_choropleth"] = _ok_response()
    qgis_render_choropleth(
        zones_path="/z.shp", value_field="population", output_png="/o.png",
        basemap="positron", basemap_opacity=0.85,
    )
    spec = fake_executor.calls[0][1]["basemap_spec"]
    assert spec is not None and spec["kind"] == "xyz"
    assert spec["opacity"] == 0.85
    assert "cartocdn" in spec["url"]


def test_choropleth_response_carries_attribution(fake_executor):
    fake_executor.responses["render_choropleth"] = _ok_response()
    result = qgis_render_choropleth(
        zones_path="/z.shp", value_field="population", output_png="/o.png",
        basemap="positron",
    )
    assert result.basemap_attribution == "© OpenStreetMap contributors © CARTO"
    assert result.basemap_source == "positron (live xyz)"
