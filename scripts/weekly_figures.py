"""Render the weekly figure set into the vault for /kb-report consumption.

Two modes:
- Default: real PFLOW. Reads from H:/Dropbox/PFLOW/output/... and writes to
  H:/Dropbox/obsidian-vault/wiki/qgis/figures/weekly/<date>/.
- Demo mode (`--demo-mode`): uses tests/fixtures/*.csv|*.geojson. Used by CI and
  for the unit tests. Writes anywhere (caller-controlled --output-root).

Manifest written as <output_dir>/manifest.json so /kb-report can discover what
to embed without scraping the directory.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VAULT_QGIS_FIGURES = Path("H:/Dropbox/obsidian-vault/wiki/qgis/figures/weekly")

# Default real-data paths — keep in sync with DESIGN.md §10 test data inventory.
DEFAULT_ZONES_PATH = (
    "H:/Dropbox/PFLOW/Pseudo-PFLOW/src/shared/gm-jp/polbnda_jpn_new.shp"
)
DEFAULT_ZONE_TRIPS_CSV = (
    "H:/Dropbox/PFLOW/output (Selective Sync Conflict)/"
    "trips/truck/run_20260422_215727/zone_trips.csv"
)
DEFAULT_TRAJECTORY_CSV = (
    "H:/Dropbox/PFLOW/output (Selective Sync Conflict)/"
    "trajectory/taxi/osaka/trajectory_0000.csv"
)
DEFAULT_DRM_GPKG = "assets/drm_network.gpkg"

# Demo-mode paths — bundled fixtures, suitable for CI.
DEMO_ZONES_PATH = REPO_ROOT / "tests" / "benchmarks" / "fixtures" / "scaled_zones_134.geojson"
DEMO_TRAJ_CSV = REPO_ROOT / "tests" / "fixtures" / "tiny_trajectory.csv"


def run_weekly(
    output_root: Path,
    demo_mode: bool,
    with_link_density: bool,
    date_str: str | None = None,
    zones_path: str | None = None,
    zone_trips_csv: str | None = None,
    trajectory_csv: str | None = None,
    drm_gpkg: str | None = None,
) -> dict:
    """Render the weekly figure set and return a manifest dict.

    Manifest schema::

        {"date": "YYYY-MM-DD",
         "figures": {"choropleth": {"path": "...", "n_features": ..., ...}, ...},
         "demo": bool}
    """
    from qgis_mcp_workflows.server import (
        qgis_render_choropleth,
        qgis_render_trajectory,
    )

    if date_str is None:
        date_str = _dt.date.today().isoformat()

    out_dir = output_root / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    if demo_mode:
        zones_path = zones_path or str(DEMO_ZONES_PATH)
        zone_trips_csv = zone_trips_csv or None  # demo zones have total_trips inline
        trajectory_csv = trajectory_csv or str(DEMO_TRAJ_CSV)
    else:
        zones_path = zones_path or DEFAULT_ZONES_PATH
        zone_trips_csv = zone_trips_csv or DEFAULT_ZONE_TRIPS_CSV
        trajectory_csv = trajectory_csv or DEFAULT_TRAJECTORY_CSV

    figures: dict[str, dict] = {}

    # 1) Choropleth
    choro_out = out_dir / "choropleth.png"
    choro_result = qgis_render_choropleth(
        zones_path=zones_path,
        value_field="total_trips",
        output_png=str(choro_out),
        value_csv=zone_trips_csv,
        title="Weekly trips by zone" if not demo_mode else "Demo: synthetic zones",
    )
    figures["choropleth"] = {
        "path": choro_result.output_path,
        "n_features": choro_result.n_features,
        "min": choro_result.min_value,
        "max": choro_result.max_value,
    }

    # 2) Trajectory
    traj_out = out_dir / "trajectory.png"
    traj_result = qgis_render_trajectory(
        input_path=trajectory_csv,
        output_png=str(traj_out),
        render_mode="lines" if demo_mode else "heatmap",
        sample_rate=1.0 if demo_mode else 0.01,
    )
    figures["trajectory"] = {
        "path": traj_result.output_path,
        "n_trajectories": traj_result.n_trajectories,
        "n_points_total": traj_result.n_points_total,
        "downsampled": traj_result.downsampled,
    }

    # 3) Link density (optional — requires Plan 2 + a built DRM GeoPackage).
    # In demo mode and tests, dispatch directly to the executor to bypass the
    # DRM file-existence check in qgis_render_link_density (no fixture gpkg).
    if with_link_density:
        _render_link_density(
            trajectory_csv=trajectory_csv,
            drm_gpkg=drm_gpkg,
            out_dir=out_dir,
            demo_mode=demo_mode,
            figures=figures,
        )

    manifest = {
        "date": date_str,
        "demo": demo_mode,
        "figures": figures,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    return manifest


def _render_link_density(
    trajectory_csv: str,
    drm_gpkg: str | None,
    out_dir: Path,
    demo_mode: bool,
    figures: dict,
) -> None:
    """Dispatch link-density rendering, handling demo vs real mode safely.

    In real mode: delegates to ``qgis_render_link_density`` (which raises
    ``DRMNetworkNotFoundError`` when the gpkg is absent — correct behaviour).

    In demo / test mode: dispatches directly to the executor so the test's
    FakeExecutor response is used without requiring a real DRM GeoPackage on disk.
    """
    from qgis_mcp_workflows.executors import get_executor

    ld_out = out_dir / "link_density.png"

    if not demo_mode:
        # Real mode: use the full server function with its validation.
        try:
            from qgis_mcp_workflows.server import qgis_render_link_density
        except ImportError:
            print(
                "  qgis_render_link_density not available — skipping (Plan 2 not merged)",
                flush=True,
            )
            return

        drm_path = drm_gpkg or str(REPO_ROOT / DEFAULT_DRM_GPKG)
        if not Path(drm_path).exists():
            print(
                f"  DRM network not found at {drm_path} — skipping link density",
                flush=True,
            )
            return

        if not Path(trajectory_csv).exists():
            print(
                f"  Trajectory CSV not found at {trajectory_csv} — skipping link density",
                flush=True,
            )
            return

        ld_result = qgis_render_link_density(
            trajectory_csvs=[trajectory_csv],
            drm_network_path=drm_path,
            output_png=str(ld_out),
        )
        figures["link_density"] = {
            "path": ld_result.output_path,
            "n_links_rendered": ld_result.n_links_rendered,
            "min_density": ld_result.min_density,
            "max_density": ld_result.max_density,
        }

    else:
        # Demo / test mode: dispatch directly to the executor.
        # The FakeExecutor (or real headless/plugin) handles "render_link_density".
        result = get_executor().dispatch(
            "render_link_density",
            {
                "trajectory_csvs": [trajectory_csv],
                "drm_network_path": drm_gpkg or DEFAULT_DRM_GPKG,
                "output_png": str(ld_out),
                "link_id_col": "link_id",
                "aggregation": "count",
                "value_col": None,
                "n_classes": 7,
                "mode": "quantile",
                "palette": "YlOrRd",
                "min_density": 1.0,
                "top_n": None,
                "extent": None,
                "basemap_paths": [],
                "width": 1600,
                "height": 1200,
                "dpi": 150,
            },
            timeout=120,
        )
        figures["link_density"] = {
            "path": result["output_path"],
            "n_links_rendered": result["n_links_rendered"],
            "min_density": result["min_density"],
            "max_density": result["max_density"],
        }


def main() -> None:
    parser = argparse.ArgumentParser(prog="weekly_figures")
    parser.add_argument(
        "--output-root",
        default=str(VAULT_QGIS_FIGURES),
        help=f"Root directory for dated figure sets (default: {VAULT_QGIS_FIGURES}).",
    )
    parser.add_argument(
        "--demo-mode",
        action="store_true",
        help="Use bundled test fixtures instead of real PFLOW paths.",
    )
    parser.add_argument(
        "--with-link-density",
        action="store_true",
        help="Also render the DRM link-density figure (requires assets/drm_network.gpkg).",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Override date stamp (default: today, YYYY-MM-DD).",
    )
    args = parser.parse_args()

    # Initialize the executor (the script is meant to run with a live MCP backend).
    if os.environ.get("QGIS_MCP_WORKFLOWS_TRANSPORT", "auto") != "fake":
        from qgis_mcp_workflows.executors import set_executor
        from qgis_mcp_workflows.server import _build_executor
        executor, chosen = _build_executor(os.environ.get("QGIS_MCP_WORKFLOWS_TRANSPORT", "auto"))
        set_executor(executor)
        print(f"Transport: {chosen}", flush=True)

    manifest = run_weekly(
        output_root=Path(args.output_root),
        demo_mode=args.demo_mode,
        with_link_density=args.with_link_density,
        date_str=args.date,
    )
    print(
        f"Wrote {len(manifest['figures'])} figures to "
        f"{Path(args.output_root) / manifest['date']}",
        flush=True,
    )
    print(
        f"Manifest: {Path(args.output_root) / manifest['date'] / 'manifest.json'}",
        flush=True,
    )


if __name__ == "__main__":
    main()
