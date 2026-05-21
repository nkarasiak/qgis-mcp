"""End-to-end W17 deck demo — qgis-mcp-north v1.0 acceptance gate.

Demonstrates the full figure pipeline using ONLY synthetic fixtures from
tests/fixtures/, end-to-end through whichever transport is active. Output is
a 3-slide .pptx containing one choropleth, one trajectory heatmap, and one
OD-flow map.

Run:
    # Plugin transport (with QGIS Desktop open + plugin enabled on :9877)
    uv run --no-sync scripts/demo_w17.py

    # Headless transport
    $env:QGIS_MCP_NORTH_TRANSPORT='headless'
    $env:QGIS_MCP_NORTH_QGIS_LAUNCHER='M:\\QGIS LTR\\bin\\python-qgis-ltr.bat'
    uv run --no-sync scripts/demo_w17.py

    # Compound-mode test (same output, exercised through the 5-tool surface)
    $env:QGIS_MCP_NORTH_TOOL_MODE='compound'
    uv run --no-sync scripts/demo_w17.py

Exit code 0 on success; non-zero if any step fails or output is missing.
"""

from __future__ import annotations

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
    """Plumb in the right executor based on QGIS_MCP_NORTH_TRANSPORT env var."""
    from qgis_mcp_north import executors
    from qgis_mcp_north.helpers import DEFAULT_HOST, DEFAULT_PORT

    transport = os.environ.get("QGIS_MCP_NORTH_TRANSPORT", "auto").lower()
    host = os.environ.get("QGIS_MCP_NORTH_HOST", DEFAULT_HOST)
    port = int(os.environ.get("QGIS_MCP_NORTH_PORT", str(DEFAULT_PORT)))

    if transport in ("plugin", "auto"):
        import socket

        from qgis_mcp_north.executors.plugin import PluginExecutor

        try:
            with socket.create_connection((host, port), timeout=0.5):
                executors.set_executor(PluginExecutor(host=host, port=port))
                return "plugin"
        except OSError:
            if transport == "plugin":
                raise

    from qgis_mcp_north.executors.headless import HeadlessExecutor
    executors.set_executor(HeadlessExecutor())
    return "headless"


def run_demo(output_dir: Path) -> Path:
    """Run the 6-step W17 pipeline. Returns the path to the produced .pptx.

    Steps:
      1. Inspect tiny_zones.geojson (sanity check)
      2. Build synthetic value CSV
      3. Render choropleth
      4. Render trajectory heatmap
      5. Render OD flows
      6. Assemble PPTX
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    from qgis_mcp_north.server import (
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
    print(f"  [1/6] tiny_zones: {info.n_features} features, CRS {info.crs}")
    assert info.n_features == 4, f"expected 4 zones, got {info.n_features}"

    # 2. Build synthetic values CSV.
    values_csv = output_dir / "values.csv"
    _build_synthetic_value_csv(values_csv)
    print(f"  [2/6] values.csv written ({values_csv.stat().st_size} bytes)")

    # 3. Choropleth.
    choro_png = output_dir / "01_choropleth.png"
    choro_result = qgis_render_choropleth(
        zones_path=str(zones),
        value_csv=str(values_csv),
        value_field="total_trips",
        join_field="zone_id",
        output_png=str(choro_png),
    )
    print(f"  [3/6] choropleth → {choro_png.name} ({choro_result.n_classes} classes)")

    # 4. Trajectory heatmap.
    traj_png = output_dir / "02_trajectory.png"
    traj_result = qgis_render_trajectory(
        input_path=str(traj),
        output_png=str(traj_png),
        render_mode="heatmap",
    )
    print(f"  [4/6] trajectory heatmap → {traj_png.name} ({traj_result.n_points_rendered} points)")

    # 5. OD flows.
    od_png = output_dir / "03_od_flows.png"
    od_result = qgis_render_od_flows(
        od_csv=str(od),
        zones_layer_path=str(zones),
        output_png=str(od_png),
    )
    print(f"  [5/6] od_flows → {od_png.name} ({od_result.n_flows_rendered} flows)")

    # 6. PPTX.
    pptx_path = output_dir / "w17.pptx"
    pptx_result = qgis_figures_to_pptx(
        figure_paths=[str(choro_png), str(traj_png), str(od_png)],
        pptx_path=str(pptx_path),
        layout="title_and_image",
        captions=["Trip totals by zone", "Sample trajectories", "OD flows"],
    )
    print(f"  [6/6] deck → {pptx_path.name} ({pptx_result.n_slides_added} slides)")

    # Verify outputs.
    for png in (choro_png, traj_png, od_png):
        assert png.exists(), f"missing render: {png}"
        size = png.stat().st_size
        assert size > 5 * 1024, f"render too small ({size} bytes): {png}"
    assert pptx_path.exists() and pptx_path.stat().st_size > 10 * 1024, "pptx missing or too small"

    return pptx_path


def main() -> int:
    print("qgis-mcp-north v1.0 — W17 deck demo")
    print("=" * 60)

    try:
        transport = _set_executor_from_env()
    except Exception as e:
        print(f"FAIL: transport setup: {e}", file=sys.stderr)
        return 1

    tool_mode = os.environ.get("QGIS_MCP_NORTH_TOOL_MODE", "full")
    print(f"Transport: {transport}  |  Tool mode: {tool_mode}")
    print()

    output_dir = REPO_ROOT / "tmp" / f"demo_w17_{transport}"
    try:
        pptx = run_demo(output_dir)
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
