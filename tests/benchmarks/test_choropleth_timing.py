"""Choropleth render timing — small (4 zones) vs scaled (134 zones)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.benchmarks.conftest import requires_headless

TINY_ZONES = Path(__file__).resolve().parents[1] / "fixtures" / "tiny_zones.geojson"


@pytest.mark.bench
@requires_headless
def test_choropleth_4_zones(benchmark, tmp_path):
    """4-polygon synthetic zones with inline value_field."""
    from qgis_mcp_north.server import qgis_render_choropleth

    benchmark(
        qgis_render_choropleth,
        zones_path=str(TINY_ZONES),
        value_field="zone_id",  # not numeric in tiny_zones; this benchmark exists to
        # exercise the join+render path; expect a join_no_match or naive numeric fallback.
        output_png=str(tmp_path / "small.png"),
    )


@pytest.mark.bench
@requires_headless
def test_choropleth_134_zones(benchmark, scaled_zones, scaled_choropleth_csv, tmp_path):
    """134-polygon synthetic grid with a CSV-joined total_trips column."""
    from qgis_mcp_north.server import qgis_render_choropleth

    benchmark(
        qgis_render_choropleth,
        zones_path=str(scaled_zones),
        value_csv=str(scaled_choropleth_csv),
        value_field="total_trips",
        join_field="zone_id",
        output_png=str(tmp_path / "scaled.png"),
    )
