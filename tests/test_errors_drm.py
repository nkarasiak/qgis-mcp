"""Smoke test for DRMNetworkNotFoundError message format."""

from qgis_mcp_workflows.errors import DRMNetworkNotFoundError


def test_drm_not_found_message_quotes_path_and_suggests_script():
    err = DRMNetworkNotFoundError("/abs/path/to/drm_network.gpkg")
    msg = str(err)
    assert "/abs/path/to/drm_network.gpkg" in msg
    assert "scripts/build_drm_network.py" in msg
    assert "Next:" in msg
