"""End-to-end W17 deck demo — v1.0 acceptance gate.

Three execution modes:
- **fake**: dispatch through FakeExecutor with scripted responses. Verifies the
  tool-chain wiring without QGIS. Always runs.
- **plugin**: requires a running QGIS Desktop with the plugin enabled. Skipped
  in CI / when port 9877 is closed.
- **headless**: requires QGIS_MCP_WORKFLOWS_QGIS_LAUNCHER (or PyQGIS on PATH). Skipped
  when launcher isn't available.

The v1.0 release ships only when at least one of the plugin/headless modes
produces a valid PPTX. The fake mode catches dispatch-shape regressions.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.conftest import requires_headless, requires_plugin

REPO_ROOT = Path(__file__).resolve().parents[2]


def _import_demo():
    """Import scripts/demo_w17.py without running its main()."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "demo_w17", REPO_ROOT / "scripts" / "demo_w17.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_w17"] = module
    spec.loader.exec_module(module)
    return module


def _scripted_responses() -> dict:
    """Wire FakeExecutor responses to exercise the full 6-step pipeline."""
    return {
        # Step 1: qgis_layer_inspect on tiny_zones.geojson
        "add_vector_layer": {"id": "L1", "name": "tiny_zones"},
        "get_layer_info": {
            "type": "vector_2",
            "crs": "EPSG:4326",
            "feature_count": 4,
            "extent": {"xmin": 139.68, "ymin": 35.69, "xmax": 139.74, "ymax": 35.73},
            "fields": [{"name": "zone_id", "type": "String"}, {"name": "name", "type": "String"}],
        },
        "remove_layer": {"removed": True},
        # Step 3: choropleth
        "render_choropleth": {
            "output_path": "<placeholder>",
            "width": 1600, "height": 1200, "dpi": 150,
            "extent": [139.68, 35.69, 139.74, 35.73],
            "crs": "EPSG:4326", "n_layers": 1,
            "field": "total_trips", "n_classes": 5,
            "breaks": [430.0, 850.0, 1100.0, 1500.0, 2100.0, 2500.0], "mode": "quantile",
            "min_value": 430.0, "max_value": 2100.0,
            "n_features": 4, "n_matched": 4, "n_unmatched": 0,
        },
        # Step 4: trajectory heatmap
        "render_trajectory": {
            "output_path": "<placeholder>",
            "width": 1600, "height": 1200, "dpi": 150,
            "extent": [139.68, 35.69, 139.74, 35.73],
            "crs": "EPSG:4326", "n_layers": 1,
            "n_trajectories": 3, "n_points_total": 30, "n_points_rendered": 30,
            "downsampled": False, "time_range": None, "modes": None,
            "used_movingpandas": False,
        },
        # Step 5: OD flows
        "render_od_flows": {
            "output_path": "<placeholder>",
            "width": 1600, "height": 1200, "dpi": 150,
            "extent": [139.68, 35.69, 139.74, 35.73],
            "crs": "EPSG:4326", "n_layers": 2,
            "n_flows": 6, "n_flows_rendered": 6, "n_zones": 4,
            "max_flow": 200.0, "min_flow_rendered": 25.0,
            "n_unmatched_origins": 0, "n_unmatched_destinations": 0,
        },
    }


def test_w17_demo_fake_mode(tmp_path, monkeypatch):
    """Smoke test: dispatch chain works with all responses scripted; verifies tool wiring."""
    from qgis_mcp_workflows import executors
    from tests.conftest import FakeExecutor

    fake = FakeExecutor()
    fake.responses = _scripted_responses()

    # Adjust render output_paths to match what demo_w17 writes
    def render_choropleth_response(params):
        r = _scripted_responses()["render_choropleth"]
        r["output_path"] = params["output_png"]
        return r

    def render_trajectory_response(params):
        r = _scripted_responses()["render_trajectory"]
        r["output_path"] = params["output_png"]
        return r

    def render_od_flows_response(params):
        r = _scripted_responses()["render_od_flows"]
        r["output_path"] = params["output_png"]
        return r

    fake.responses["render_choropleth"] = render_choropleth_response
    fake.responses["render_trajectory"] = render_trajectory_response
    fake.responses["render_od_flows"] = render_od_flows_response

    executors.set_executor(fake)
    try:
        demo = _import_demo()
        # FakeExecutor doesn't actually write files; pre-create valid (but minimal)
        # PNGs so the demo's existence + size assertions pass and python-pptx can
        # embed them. We're testing the dispatch chain, not the rendering.
        from PIL import Image

        out_dir = tmp_path / "demo_w17_fake"
        out_dir.mkdir(parents=True)
        for name in ("01_choropleth.png", "02_trajectory.png", "03_od_flows.png"):
            img = Image.new("RGB", (200, 150), color=(220, 230, 240))
            img.save(out_dir / name, "PNG")
            # Pad to ~10KB so the >5KB assertion passes.
            with (out_dir / name).open("ab") as f:
                f.write(b"\0" * (10_000 - (out_dir / name).stat().st_size))

        # Monkeypatch the demo's internal _set_executor_from_env to be a no-op
        # (we've already set fake).
        monkeypatch.setattr(demo, "_set_executor_from_env", lambda: "fake")

        pptx_path = demo.run_demo(out_dir)
        assert pptx_path.exists()
        assert pptx_path.stat().st_size > 10 * 1024

        # Verify the expected dispatch sequence (every step hit the right command).
        commands = [c[0] for c in fake.calls]
        assert "add_vector_layer" in commands  # from qgis_layer_inspect
        assert "render_choropleth" in commands
        assert "render_trajectory" in commands
        assert "render_od_flows" in commands
    finally:
        executors.set_executor(None)


def test_demo_with_link_density_smoke(tmp_path, monkeypatch):
    """Smoke test: --with-link-density branch wires up correctly via FakeExecutor.

    When QGIS_MCP_WORKFLOWS_DRM_GPKG env var is set to a valid GeoPackage path
    the DRM file presence check passes. Otherwise the test is skipped.

    This test verifies the dispatch chain only (FakeExecutor, no real QGIS).
    It reuses the same pattern as test_w17_demo_fake_mode, extending it with
    a scripted render_link_density response.
    """
    import os

    drm = os.environ.get("QGIS_MCP_WORKFLOWS_DRM_GPKG")
    if not drm or not os.path.exists(drm):
        pytest.skip("Set QGIS_MCP_WORKFLOWS_DRM_GPKG to a valid GeoPackage to enable this test")

    from qgis_mcp_workflows import executors
    from tests.conftest import FakeExecutor

    fake = FakeExecutor()
    responses = _scripted_responses()

    # Adjust output paths to match what demo_w17 writes (choropleth, trajectory, od_flows).
    def render_choropleth_response(params):
        r = _scripted_responses()["render_choropleth"]
        r["output_path"] = params["output_png"]
        return r

    def render_trajectory_response(params):
        r = _scripted_responses()["render_trajectory"]
        r["output_path"] = params["output_png"]
        return r

    def render_od_flows_response(params):
        r = _scripted_responses()["render_od_flows"]
        r["output_path"] = params["output_png"]
        return r

    def render_link_density_response(params):
        return {
            "output_path": params["output_png"],
            "width": 1600, "height": 1200, "dpi": 150,
            "extent": [139.68, 35.69, 139.74, 35.73],
            "crs": "EPSG:4326", "n_layers": 2,
            "n_links_with_traffic": 3,
            "n_links_rendered": 3,
            "n_unmatched_link_ids": 0,
            "density_field": "n_points",
            "breaks": [1.0, 2.0, 3.0, 4.0, 5.0],
            "mode": "quantile",
            "min_density": 1.0,
            "max_density": 4.0,
        }

    responses["render_choropleth"] = render_choropleth_response
    responses["render_trajectory"] = render_trajectory_response
    responses["render_od_flows"] = render_od_flows_response
    responses["render_link_density"] = render_link_density_response
    fake.responses = responses

    executors.set_executor(fake)
    try:
        demo = _import_demo()

        from pathlib import Path

        from PIL import Image

        out_dir = tmp_path / "demo_w17_link_density"
        out_dir.mkdir(parents=True)
        for name in ("01_choropleth.png", "02_trajectory.png", "03_od_flows.png", "04_link_density.png"):
            img = Image.new("RGB", (200, 150), color=(200, 220, 240))
            img.save(out_dir / name, "PNG")
            # Pad to ~10KB so the >5KB assertion passes.
            with (out_dir / name).open("ab") as f:
                f.write(b"\0" * (10_000 - (out_dir / name).stat().st_size))

        monkeypatch.setattr(demo, "_set_executor_from_env", lambda: "fake")

        pptx_path = demo.run_demo(
            out_dir,
            with_link_density=True,
            drm_network_path=Path(drm),
        )
        assert pptx_path.exists()
        assert pptx_path.stat().st_size > 10 * 1024

        # Verify render_link_density was dispatched.
        commands = [c[0] for c in fake.calls]
        assert "render_link_density" in commands, (
            f"Expected render_link_density in dispatched commands; got {commands}"
        )

        # Verify the PPTX has 4 slides (choropleth + trajectory + od + density).
        import zipfile

        with zipfile.ZipFile(pptx_path) as z:
            slide_count = sum(1 for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
        assert slide_count == 4, f"Expected 4 slides, got {slide_count}"

    finally:
        executors.set_executor(None)


@requires_plugin
def test_w17_demo_plugin_mode(tmp_path):
    """Live demo via QGIS plugin transport. Skipped unless port 9877 is open."""
    from qgis_mcp_workflows import executors
    from qgis_mcp_workflows.executors.plugin import PluginExecutor
    from qgis_mcp_workflows.helpers import DEFAULT_HOST, DEFAULT_PORT

    executors.set_executor(PluginExecutor(host=DEFAULT_HOST, port=DEFAULT_PORT))
    try:
        demo = _import_demo()
        out_dir = tmp_path / "demo_w17_plugin"
        pptx_path = demo.run_demo(out_dir)
        assert pptx_path.exists()
        assert pptx_path.stat().st_size > 10 * 1024
    finally:
        executors.set_executor(None)


@requires_headless
@pytest.mark.slow
def test_w17_demo_headless_mode(tmp_path):
    """Live demo via PyQGIS subprocess. Skipped unless launcher env var set."""
    from qgis_mcp_workflows import executors
    from qgis_mcp_workflows.executors.headless import HeadlessExecutor

    executors.set_executor(HeadlessExecutor())
    try:
        demo = _import_demo()
        out_dir = tmp_path / "demo_w17_headless"
        pptx_path = demo.run_demo(out_dir)
        assert pptx_path.exists()
        assert pptx_path.stat().st_size > 10 * 1024
    finally:
        executors.set_executor(None)
