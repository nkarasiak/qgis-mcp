"""End-to-end W17 deck demo — qgis-mcp-workflows v1.0 acceptance gate.

Demonstrates the full figure pipeline using ONLY synthetic fixtures from
tests/fixtures/, end-to-end through whichever transport is active. Output is
a 3-slide .pptx containing one choropleth, one trajectory heatmap, and one
OD-flow map.

Run:
    # Plugin transport (with QGIS Desktop open + plugin enabled on :9877)
    uv run --no-sync scripts/demo_w17.py

    # Headless transport
    $env:QGIS_MCP_WORKFLOWS_TRANSPORT='headless'
    $env:QGIS_MCP_WORKFLOWS_QGIS_LAUNCHER='M:\\QGIS LTR\\bin\\python-qgis-ltr.bat'
    uv run --no-sync scripts/demo_w17.py

    # Compound-mode test (same output, exercised through the 5-tool surface)
    $env:QGIS_MCP_WORKFLOWS_TOOL_MODE='compound'
    uv run --no-sync scripts/demo_w17.py

    # Include a DRM link-density figure (requires assets/drm_network.gpkg)
    uv run --no-sync scripts/demo_w17.py --with-link-density
    uv run --no-sync scripts/demo_w17.py --with-link-density --drm-network path/to/drm_network.gpkg

Exit code 0 on success; non-zero if any step fails or output is missing.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
ASSETS_DIR = REPO_ROOT / "assets" / "screenshots"


def _build_synthetic_value_csv(target: Path) -> Path:
    """Write a tiny zone_id,total_trips CSV matching tiny_zones.geojson (Z01..Z04)."""
    with target.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["zone_id", "total_trips"])
        # Deterministic synthetic trips so the demo render is reproducible.
        for i, val in enumerate([1200, 850, 430, 2100], start=1):
            w.writerow([f"Z{i:02d}", val])
    return target


def _set_executor_from_env():
    """Plumb in the right executor based on QGIS_MCP_WORKFLOWS_TRANSPORT env var."""
    from qgis_mcp_workflows import executors
    from qgis_mcp_workflows.helpers import DEFAULT_HOST, DEFAULT_PORT

    transport = os.environ.get("QGIS_MCP_WORKFLOWS_TRANSPORT", "auto").lower()
    host = os.environ.get("QGIS_MCP_WORKFLOWS_HOST", DEFAULT_HOST)
    port = int(os.environ.get("QGIS_MCP_WORKFLOWS_PORT", str(DEFAULT_PORT)))

    if transport in ("plugin", "auto"):
        import socket

        from qgis_mcp_workflows.executors.plugin import PluginExecutor

        try:
            with socket.create_connection((host, port), timeout=0.5):
                executors.set_executor(PluginExecutor(host=host, port=port))
                return "plugin"
        except OSError:
            if transport == "plugin":
                raise

    from qgis_mcp_workflows.executors.headless import HeadlessExecutor
    executors.set_executor(HeadlessExecutor())
    return "headless"


def run_demo(
    output_dir: Path,
    *,
    with_link_density: bool = False,
    drm_network_path: Path | None = None,
    link_density_traj_csv: Path | None = None,
) -> Path:
    """Run the W17 pipeline. Returns the path to the produced .pptx.

    Steps:
      1. Inspect tiny_zones.geojson (sanity check)
      2. Build synthetic value CSV
      3. Render choropleth
      4. Render trajectory heatmap
      5. Render OD flows
      6. [Optional] Render DRM link-density (if with_link_density=True)
      7. Assemble PPTX

    Args:
        output_dir: Directory to write all output files into.
        with_link_density: If True, also render a DRM link-density figure.
            Requires ``drm_network_path`` to point at a valid GeoPackage.
        drm_network_path: Path to the pre-built DRM network GeoPackage.
            Defaults to ``assets/drm_network.gpkg`` under the repo root.
            Ignored when ``with_link_density=False``.
        link_density_traj_csv: Trajectory CSV with a ``link_id`` column for
            the link-density step. Defaults to
            ``tests/fixtures/tiny_trajectory_linkid.csv``. Ignored when
            ``with_link_density=False``.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    from qgis_mcp_workflows.server import (
        qgis_figures_to_pptx,
        qgis_layer_inspect,
        qgis_render_choropleth,
        qgis_render_od_flows,
        qgis_render_trajectory,
    )

    zones = FIXTURE_DIR / "tiny_zones.geojson"
    traj = FIXTURE_DIR / "tiny_trajectory.csv"
    od = FIXTURE_DIR / "tiny_od.csv"

    # 1. Sanity check zones.
    info = qgis_layer_inspect(path=str(zones))
    print(f"  [1] tiny_zones: {info.n_features} features, CRS {info.crs}")
    assert info.n_features == 4, f"expected 4 zones, got {info.n_features}"

    # 2. Build synthetic values CSV.
    values_csv = output_dir / "values.csv"
    _build_synthetic_value_csv(values_csv)
    print(f"  [2] values.csv written ({values_csv.stat().st_size} bytes)")

    # 3. Choropleth.
    choro_png = output_dir / "01_choropleth.png"
    choro_result = qgis_render_choropleth(
        zones_path=str(zones),
        value_csv=str(values_csv),
        value_field="total_trips",
        join_field="zone_id",
        output_png=str(choro_png),
    )
    print(f"  [3] choropleth → {choro_png.name} ({choro_result.n_classes} classes)")

    # 4. Trajectory heatmap.
    traj_png = output_dir / "02_trajectory.png"
    traj_result = qgis_render_trajectory(
        input_path=str(traj),
        output_png=str(traj_png),
        render_mode="heatmap",
    )
    print(f"  [4] trajectory heatmap → {traj_png.name} ({traj_result.n_points_rendered} points)")

    # 5. OD flows.
    od_png = output_dir / "03_od_flows.png"
    od_result = qgis_render_od_flows(
        od_csv=str(od),
        zones_layer_path=str(zones),
        output_png=str(od_png),
    )
    print(f"  [5] od_flows → {od_png.name} ({od_result.n_flows_rendered} flows)")

    # Collect figures + captions for PPTX (may grow if with_link_density).
    figure_paths = [str(choro_png), str(traj_png), str(od_png)]
    captions = ["Trip totals by zone", "Sample trajectories", "OD flows"]

    # 6. Optional: DRM link-density.
    if with_link_density:
        from qgis_mcp_workflows.server import qgis_render_link_density

        drm_path = (drm_network_path or REPO_ROOT / "assets" / "drm_network.gpkg").resolve()
        if not drm_path.exists():
            sys.exit(
                f"DRM network not found at {drm_path}; "
                "run scripts/build_drm_network.py first."
            )
        ld_traj = (
            link_density_traj_csv or FIXTURE_DIR / "tiny_trajectory_linkid.csv"
        ).resolve()
        density_png = output_dir / "04_link_density.png"
        density_result = qgis_render_link_density(
            trajectory_csvs=[str(ld_traj)],
            drm_network_path=str(drm_path),
            output_png=str(density_png),
        )
        print(
            f"  [6] link density → {density_png.name} "
            f"({density_result.n_links_rendered} links rendered)"
        )
        figure_paths.append(str(density_png))
        captions.append("DRM link density")

    # 7. PPTX.
    step_label = "7" if with_link_density else "6"
    pptx_path = output_dir / "w17.pptx"
    pptx_result = qgis_figures_to_pptx(
        figure_paths=figure_paths,
        pptx_path=str(pptx_path),
        layout="title_and_image",
        captions=captions,
    )
    print(f"  [{step_label}] deck → {pptx_path.name} ({pptx_result.n_slides_added} slides)")

    # Verify core outputs (link-density PNG verified separately when present).
    for png in (choro_png, traj_png, od_png):
        assert png.exists(), f"missing render: {png}"
        size = png.stat().st_size
        assert size > 5 * 1024, f"render too small ({size} bytes): {png}"
    if with_link_density:
        assert density_png.exists(), f"missing link-density render: {density_png}"
        assert density_png.stat().st_size > 5 * 1024, f"link-density render too small: {density_png}"
    assert pptx_path.exists() and pptx_path.stat().st_size > 10 * 1024, "pptx missing or too small"

    return pptx_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="qgis-mcp-workflows v1.0 — W17 deck demo",
    )
    parser.add_argument(
        "--with-link-density",
        action="store_true",
        help="Also render a DRM link-density figure (requires assets/drm_network.gpkg).",
    )
    parser.add_argument(
        "--drm-network",
        default=None,
        metavar="PATH",
        help=(
            "Path to the DRM network GeoPackage "
            "(default: assets/drm_network.gpkg under the repo root)."
        ),
    )
    args = parser.parse_args()

    print("qgis-mcp-workflows v1.0 — W17 deck demo")
    print("=" * 60)

    try:
        transport = _set_executor_from_env()
    except Exception as e:
        print(f"FAIL: transport setup: {e}", file=sys.stderr)
        return 1

    tool_mode = os.environ.get("QGIS_MCP_WORKFLOWS_TOOL_MODE", "full")
    print(f"Transport: {transport}  |  Tool mode: {tool_mode}")
    if args.with_link_density:
        drm_label = args.drm_network or "(default: assets/drm_network.gpkg)"
        print(f"Link density: enabled  |  DRM network: {drm_label}")
    print()

    drm_path = Path(args.drm_network).resolve() if args.drm_network else None
    output_dir = REPO_ROOT / "tmp" / f"demo_w17_{transport}"
    try:
        pptx = run_demo(
            output_dir,
            with_link_density=args.with_link_density,
            drm_network_path=drm_path,
        )
    except Exception as e:
        print(f"\nFAIL: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

    # Copy the choropleth to assets/screenshots/ for README embed.
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    screenshot = ASSETS_DIR / "w17_demo.png"
    try:
        import shutil
        shutil.copy(output_dir / "01_choropleth.png", screenshot)
        print(f"\nScreenshot updated: {screenshot.relative_to(REPO_ROOT)}")
    except Exception as e:
        print(f"  (screenshot copy failed: {e})", file=sys.stderr)

    print(f"\nSUCCESS: {pptx.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
