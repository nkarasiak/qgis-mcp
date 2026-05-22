"""Slow integration test for the prep script — writes a GeoPackage to tmp_path.

Marked `slow` (skipped by default per pyproject.toml pytest config). Requires
pyogrio installed (uv sync --extra drm).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pyogrio = pytest.importorskip("pyogrio")

FIXTURE_TSV = Path(__file__).parent / "fixtures" / "tiny_drm_01.tsv"


@pytest.mark.slow
def test_build_writes_geopackage_with_five_features(tmp_path: Path):
    from scripts.build_drm_network import build_geopackage

    out = tmp_path / "drm_network.gpkg"
    build_geopackage(tsv_paths=[FIXTURE_TSV], output_path=out)

    assert out.exists() and out.stat().st_size > 0

    df = pyogrio.read_dataframe(out)
    assert len(df) == 5
    assert sorted(df["link_id"].tolist()) == ["100001", "100002", "100003", "100004", "100005"]
    # geometry should be LineString
    assert df.geometry.iloc[0].geom_type == "LineString"


@pytest.mark.slow
def test_build_combines_multiple_tsvs(tmp_path: Path):
    """A second TSV gets appended, link_ids stay distinct."""
    from scripts.build_drm_network import build_geopackage

    # Synthesize a second TSV with different link_ids
    second = tmp_path / "drm_02.tsv"
    second.write_text(
        "200001\t3001\t3002\t1\tc5\tc6\tc7\tc8\t140.0\t36.0\t140.001\t36.001\tLINESTRING(140.0 36.0, 140.001 36.001)\n",
        encoding="utf-8",
    )

    out = tmp_path / "drm_network.gpkg"
    build_geopackage(tsv_paths=[FIXTURE_TSV, second], output_path=out)

    df = pyogrio.read_dataframe(out)
    assert len(df) == 6  # 5 + 1
    assert "200001" in df["link_id"].tolist()
