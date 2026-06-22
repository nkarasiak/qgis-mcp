"""Tests for the vendored scientific colormaps + diverging break math.

Pure-Python, no QGIS: the data tables and the break/positions math are
importable and verifiable without a QGIS runtime. ``build_ramp`` (which
constructs a QgsColorRamp) is QGIS-only and verified live, not here.
"""

from __future__ import annotations

import pytest

from qgis_mcp_workflows_plugin.colormaps import COLORMAPS, DIVERGING, diverging_breaks

EXPECTED_SEQUENTIAL = {"viridis", "cividis", "magma", "batlow"}
EXPECTED_DIVERGING = {"vik", "roma", "balance", "RdBu", "BrBG"}


# --- colormap data integrity ------------------------------------------------

def test_all_expected_colormaps_present():
    for name in EXPECTED_SEQUENTIAL | EXPECTED_DIVERGING:
        assert name in COLORMAPS, f"missing colormap {name!r}"


def test_diverging_set_matches_expected():
    assert DIVERGING == EXPECTED_DIVERGING


def test_diverging_is_subset_of_colormaps():
    assert set(COLORMAPS) >= DIVERGING


def test_stops_monotonic_bounded_and_full_range():
    for name, stops in COLORMAPS.items():
        positions = [p for p, _ in stops]
        assert len(stops) >= 2, f"{name}: need >=2 stops"
        assert positions[0] == pytest.approx(0.0), f"{name}: first stop must be 0.0"
        assert positions[-1] == pytest.approx(1.0), f"{name}: last stop must be 1.0"
        assert all(
            positions[i] <= positions[i + 1] for i in range(len(positions) - 1)
        ), f"{name}: stop positions must be non-decreasing"
        for _, rgb in stops:
            assert len(rgb) == 3, f"{name}: rgb must be a 3-tuple"
            assert all(0 <= c <= 255 for c in rgb), f"{name}: rgb out of range"


# --- diverging break math ---------------------------------------------------

def test_breaks_symmetric_and_equally_spaced():
    bc = diverging_breaks(vmin=-40.0, vmax=60.0, center=0.0, n_classes=4)
    # radius is the larger tail (60), so the range is symmetric ±60 about center
    assert bc.breaks[0] == pytest.approx(-60.0)
    assert bc.breaks[-1] == pytest.approx(60.0)
    assert len(bc.breaks) == 5  # n_classes + 1
    diffs = [bc.breaks[i + 1] - bc.breaks[i] for i in range(4)]
    assert all(d == pytest.approx(30.0) for d in diffs)


def test_even_n_classes_puts_center_on_a_class_edge():
    bc = diverging_breaks(-30.0, 30.0, center=0.0, n_classes=4)
    assert bc.breaks[2] == pytest.approx(0.0)  # the middle edge is exactly center


def test_positions_symmetric_around_half():
    bc = diverging_breaks(-50.0, 50.0, center=0.0, n_classes=6)
    n = len(bc.positions)
    assert n == 6
    for i in range(n):
        assert bc.positions[i] + bc.positions[n - 1 - i] == pytest.approx(1.0)
    assert min(bc.positions) >= 0.0 and max(bc.positions) <= 1.0


def test_radius_uses_the_larger_tail():
    bc = diverging_breaks(-10.0, 80.0, center=0.0, n_classes=4)
    assert bc.breaks[0] == pytest.approx(-80.0)
    assert bc.breaks[-1] == pytest.approx(80.0)


def test_nonzero_center_is_honored():
    bc = diverging_breaks(50.0, 150.0, center=100.0, n_classes=4)
    assert bc.breaks[0] == pytest.approx(50.0)
    assert bc.breaks[-1] == pytest.approx(150.0)
    assert bc.breaks[2] == pytest.approx(100.0)


def test_one_sided_detection():
    assert diverging_breaks(10.0, 50.0, center=0.0, n_classes=4).one_sided is True
    assert diverging_breaks(-30.0, 60.0, center=0.0, n_classes=4).one_sided is False


def test_all_equal_to_center_does_not_divide_by_zero():
    bc = diverging_breaks(0.0, 0.0, center=0.0, n_classes=4)
    assert bc.breaks[0] < bc.breaks[-1]  # degenerate range still well-formed
    assert all(0.0 <= p <= 1.0 for p in bc.positions)
