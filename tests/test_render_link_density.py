"""End-to-end tool tests for qgis_render_link_density — FakeExecutor, no QGIS.

Verifies the MCP-side responsibilities:
- DRMNetworkNotFoundError raised when drm_network_path doesn't exist
- Aggregation dispatched to plugin with the right param keys
- Response shape (LinkDensityResult fields) translated correctly
- aggregation='sum' without value_col → ValueError before dispatch
- Empty density dict → EmptyAfterFilterError before dispatch
"""

from __future__ import annotations

import csv as _csv
from pathlib import Path

import pytest


def _ok_response(**overrides) -> dict:
    base = {
        "output_path": "/tmp/links.png",
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [139.6, 35.6, 139.9, 35.9],
        "crs": "EPSG:4326", "n_layers": 2,
        "n_links_with_traffic": 2,
        "n_links_rendered": 2,
        "n_unmatched_link_ids": 0,
        "density_field": "n_points",
        "breaks": [1.0, 2.0, 3.0, 4.0, 5.0],
        "mode": "quantile",
        "min_density": 1.0,
        "max_density": 5.0,
    }
    base.update(overrides)
    return base


def _write_traj_csv(path: Path, rows: list[dict]) -> None:
    fields = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_drm_path_missing_raises(fake_executor, tmp_path: Path):
    from qgis_mcp_workflows.errors import DRMNetworkNotFoundError
    from qgis_mcp_workflows.server import qgis_render_link_density

    traj = tmp_path / "traj.csv"
    _write_traj_csv(traj, [{"link_id": "100001"}])

    with pytest.raises(DRMNetworkNotFoundError, match="drm_network"):
        qgis_render_link_density(
            trajectory_csvs=[str(traj)],
            drm_network_path=str(tmp_path / "does_not_exist.gpkg"),
            output_png="/tmp/x.png",
        )
    # Should fail before dispatching to executor
    assert fake_executor.calls == []


def test_happy_path_dispatches_density_dict(fake_executor, tmp_path: Path):
    from qgis_mcp_workflows.server import qgis_render_link_density

    traj = tmp_path / "traj.csv"
    _write_traj_csv(traj, [
        {"link_id": "100001"},
        {"link_id": "100001"},
        {"link_id": "100002"},
    ])
    drm = tmp_path / "drm.gpkg"
    drm.write_bytes(b"")  # presence check only

    fake_executor.responses["render_link_density"] = _ok_response()
    result = qgis_render_link_density(
        trajectory_csvs=[str(traj)],
        drm_network_path=str(drm),
        output_png="/tmp/links.png",
    )

    cmd, params = fake_executor.calls[0]
    assert cmd == "render_link_density"
    assert params["density"] == {"100001": 2.0, "100002": 1.0}
    assert params["aggregation"] == "count"
    assert params["drm_network_path"].endswith("drm.gpkg")
    assert result.n_trajectory_rows_total == 3
    assert result.n_points_total == 3
    assert result.density_field == "n_points"


def test_sum_aggregation_passes_value_col(fake_executor, tmp_path: Path):
    from qgis_mcp_workflows.server import qgis_render_link_density

    traj = tmp_path / "traj.csv"
    _write_traj_csv(traj, [
        {"link_id": "100001", "weight": "3.5"},
        {"link_id": "100001", "weight": "1.5"},
    ])
    drm = tmp_path / "drm.gpkg"
    drm.write_bytes(b"")

    fake_executor.responses["render_link_density"] = _ok_response(
        density_field="sum_weight", min_density=5.0, max_density=5.0
    )
    result = qgis_render_link_density(
        trajectory_csvs=[str(traj)], drm_network_path=str(drm),
        output_png="/tmp/x.png",
        aggregation="sum", value_col="weight",
    )
    params = fake_executor.calls[0][1]
    assert params["aggregation"] == "sum"
    assert params["value_col"] == "weight"
    assert params["density"]["100001"] == pytest.approx(5.0)
    assert result.aggregation == "sum"


def test_sum_without_value_col_raises_before_dispatch(fake_executor, tmp_path: Path):
    from qgis_mcp_workflows.server import qgis_render_link_density

    traj = tmp_path / "traj.csv"
    _write_traj_csv(traj, [{"link_id": "100001"}])
    drm = tmp_path / "drm.gpkg"
    drm.write_bytes(b"")

    with pytest.raises(ValueError, match="value_col"):
        qgis_render_link_density(
            trajectory_csvs=[str(traj)], drm_network_path=str(drm),
            output_png="/tmp/x.png",
            aggregation="sum",
        )
    assert fake_executor.calls == []


def test_min_density_filter_applied_mcp_side(fake_executor, tmp_path: Path):
    from qgis_mcp_workflows.server import qgis_render_link_density

    traj = tmp_path / "traj.csv"
    _write_traj_csv(traj, [
        {"link_id": "100001"}, {"link_id": "100001"}, {"link_id": "100001"},
        {"link_id": "100002"},  # density = 1, below threshold
    ])
    drm = tmp_path / "drm.gpkg"
    drm.write_bytes(b"")

    fake_executor.responses["render_link_density"] = _ok_response()
    qgis_render_link_density(
        trajectory_csvs=[str(traj)], drm_network_path=str(drm),
        output_png="/tmp/x.png", min_density=2,
    )
    params = fake_executor.calls[0][1]
    # 100002 should be filtered out before dispatch
    assert params["density"] == {"100001": 3.0}


def test_top_n_clips_to_largest(fake_executor, tmp_path: Path):
    from qgis_mcp_workflows.server import qgis_render_link_density

    traj = tmp_path / "traj.csv"
    rows = []
    for link_id, count in [("A", 5), ("B", 3), ("C", 1)]:
        rows.extend({"link_id": link_id} for _ in range(count))
    _write_traj_csv(traj, rows)
    drm = tmp_path / "drm.gpkg"
    drm.write_bytes(b"")

    fake_executor.responses["render_link_density"] = _ok_response()
    qgis_render_link_density(
        trajectory_csvs=[str(traj)], drm_network_path=str(drm),
        output_png="/tmp/x.png", top_n=2,
    )
    params = fake_executor.calls[0][1]
    assert set(params["density"].keys()) == {"A", "B"}


def test_empty_after_filter_raises(fake_executor, tmp_path: Path):
    from qgis_mcp_workflows.errors import EmptyAfterFilterError
    from qgis_mcp_workflows.server import qgis_render_link_density

    traj = tmp_path / "traj.csv"
    _write_traj_csv(traj, [{"link_id": "100001"}])  # density = 1
    drm = tmp_path / "drm.gpkg"
    drm.write_bytes(b"")

    with pytest.raises(EmptyAfterFilterError, match="min_density"):
        qgis_render_link_density(
            trajectory_csvs=[str(traj)], drm_network_path=str(drm),
            output_png="/tmp/x.png", min_density=99,
        )
    assert fake_executor.calls == []


def test_basemap_preset_threads_spec(fake_executor, tmp_path: Path):
    from qgis_mcp_workflows.server import qgis_render_link_density

    traj = tmp_path / "traj.csv"
    _write_traj_csv(traj, [{"link_id": "100001"}, {"link_id": "100001"}])
    drm = tmp_path / "drm.gpkg"
    drm.write_bytes(b"")

    fake_executor.responses["render_link_density"] = _ok_response()
    qgis_render_link_density(
        trajectory_csvs=[str(traj)], drm_network_path=str(drm),
        output_png="/tmp/x.png", basemap="dark_matter", basemap_opacity=0.6,
    )
    spec = fake_executor.calls[0][1]["basemap_spec"]
    assert spec is not None and spec["kind"] == "xyz"
    assert spec["opacity"] == 0.6
