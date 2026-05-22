"""Unit tests for the link-density aggregation helper.

Pure-Python aggregation — no QGIS, no executor. Just CSV → dict[link_id → count|sum].
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_csv(path: Path, rows: list[dict]) -> None:
    """Write a trivial CSV with header derived from the first row's keys."""
    import csv as _csv

    fields = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_count_aggregation_single_file(tmp_path: Path):
    from qgis_mcp_workflows.server import _aggregate_link_density

    csv_path = tmp_path / "traj.csv"
    _write_csv(csv_path, [
        {"link_id": "100001", "lon": "139.7", "lat": "35.7"},
        {"link_id": "100001", "lon": "139.7", "lat": "35.7"},
        {"link_id": "100002", "lon": "139.8", "lat": "35.8"},
    ])

    density, n_rows = _aggregate_link_density(
        csv_paths=[csv_path], link_id_col="link_id", aggregation="count", value_col=None
    )
    assert density == {"100001": 2.0, "100002": 1.0}
    assert n_rows == 3


def test_count_aggregation_concatenates_files(tmp_path: Path):
    from qgis_mcp_workflows.server import _aggregate_link_density

    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    _write_csv(a, [{"link_id": "100001", "lon": "139.7", "lat": "35.7"}])
    _write_csv(b, [
        {"link_id": "100001", "lon": "139.7", "lat": "35.7"},
        {"link_id": "100002", "lon": "139.8", "lat": "35.8"},
    ])

    density, n_rows = _aggregate_link_density(
        csv_paths=[a, b], link_id_col="link_id", aggregation="count", value_col=None
    )
    assert density == {"100001": 2.0, "100002": 1.0}
    assert n_rows == 3


def test_sum_aggregation_with_numeric_column(tmp_path: Path):
    from qgis_mcp_workflows.server import _aggregate_link_density

    csv_path = tmp_path / "traj.csv"
    _write_csv(csv_path, [
        {"link_id": "100001", "weight": "3.5"},
        {"link_id": "100001", "weight": "1.5"},
        {"link_id": "100002", "weight": "10"},
    ])
    density, n_rows = _aggregate_link_density(
        csv_paths=[csv_path], link_id_col="link_id",
        aggregation="sum", value_col="weight",
    )
    assert density["100001"] == pytest.approx(5.0)
    assert density["100002"] == pytest.approx(10.0)
    assert n_rows == 3


def test_sum_aggregation_skips_non_numeric_values(tmp_path: Path):
    from qgis_mcp_workflows.server import _aggregate_link_density

    csv_path = tmp_path / "traj.csv"
    _write_csv(csv_path, [
        {"link_id": "100001", "weight": "3.5"},
        {"link_id": "100001", "weight": "NaN"},
        {"link_id": "100001", "weight": ""},
        {"link_id": "100002", "weight": "garbage"},
    ])
    density, n_rows = _aggregate_link_density(
        csv_paths=[csv_path], link_id_col="link_id",
        aggregation="sum", value_col="weight",
    )
    # Only the 3.5 row counts.
    assert density == {"100001": pytest.approx(3.5)}
    # n_rows counts CSV rows read, not aggregated.
    assert n_rows == 4


def test_missing_link_id_column_raises_field_not_found(tmp_path: Path):
    from qgis_mcp_workflows.errors import FieldNotFoundError
    from qgis_mcp_workflows.server import _aggregate_link_density

    csv_path = tmp_path / "traj.csv"
    _write_csv(csv_path, [{"lon": "139.7", "lat": "35.7"}])

    with pytest.raises(FieldNotFoundError, match="link_id"):
        _aggregate_link_density(
            csv_paths=[csv_path], link_id_col="link_id",
            aggregation="count", value_col=None,
        )


def test_sum_without_value_col_raises_valueerror(tmp_path: Path):
    from qgis_mcp_workflows.server import _aggregate_link_density

    csv_path = tmp_path / "traj.csv"
    _write_csv(csv_path, [{"link_id": "100001", "weight": "1.0"}])

    with pytest.raises(ValueError, match="value_col"):
        _aggregate_link_density(
            csv_paths=[csv_path], link_id_col="link_id",
            aggregation="sum", value_col=None,
        )
