"""Tests for scripts/build_drm_network.py — parsing logic only.

GeoPackage I/O is exercised by a separate slow integration test (not in this file)
because it requires pyogrio installed. These tests cover the pure-stdlib parsing:
TSV → list of dicts with link_id, road_class, from/to coords, coords list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_drm_01.tsv"


def test_parse_tsv_returns_five_links():
    from scripts.build_drm_network import parse_drm_tsv

    rows = list(parse_drm_tsv(FIXTURE))
    assert len(rows) == 5
    assert rows[0]["link_id"] == "100001"
    assert rows[0]["road_class"] == "1"


def test_parse_tsv_extracts_linestring_coords():
    from scripts.build_drm_network import parse_drm_tsv

    rows = list(parse_drm_tsv(FIXTURE))
    coords_first = rows[0]["coords"]
    assert coords_first == [
        (139.700, 35.700),
        (139.7005, 35.7005),
        (139.701, 35.701),
    ]


def test_parse_tsv_three_point_linestring_with_kink():
    from scripts.build_drm_network import parse_drm_tsv

    rows = list(parse_drm_tsv(FIXTURE))
    link_3 = next(r for r in rows if r["link_id"] == "100003")
    assert link_3["coords"] == [
        (139.702, 35.702),
        (139.7025, 35.7035),
        (139.703, 35.704),
    ]


def test_parse_tsv_skips_blank_lines(tmp_path: Path):
    """Real DRM TSVs may contain trailing blank lines — those must be skipped silently."""
    from scripts.build_drm_network import parse_drm_tsv

    blob = FIXTURE.read_text(encoding="utf-8")
    padded = blob + "\n\n\n"
    pad_path = tmp_path / "padded.tsv"
    pad_path.write_text(padded, encoding="utf-8")

    rows = list(parse_drm_tsv(pad_path))
    assert len(rows) == 5  # blanks ignored


def test_parse_tsv_raises_on_malformed_wkt(tmp_path: Path):
    """A non-LINESTRING WKT in column 14 → ValueError, not silent skip."""
    from scripts.build_drm_network import parse_drm_tsv

    bad_path = tmp_path / "bad.tsv"
    bad_path.write_text(
        "100001\t1001\t1002\t1\tc5\tc6\tc7\tc8\t139.0\t35.0\t139.0\t35.0\tPOINT(139.0 35.0)\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="LINESTRING"):
        list(parse_drm_tsv(bad_path))
