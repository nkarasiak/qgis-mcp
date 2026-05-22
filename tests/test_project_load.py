"""Tests for qgis_project_load — mocked plugin executor.

Phase B prerequisite: loads a .qgz and returns ProjectInfo with layers + layouts
so that export_layout / batch_render know what's available.
"""

from __future__ import annotations

import os

import pytest

from qgis_mcp_workflows.errors import ExecutorError, ProjectLoadError
from qgis_mcp_workflows.server import qgis_project_load


def _ok_response(**overrides) -> dict:
    base = {
        "project_path": "/tmp/proj.qgz",
        "crs": "EPSG:4326",
        "extent": [139.68, 35.69, 139.74, 35.73],
        "layers": [
            {"layer_id": "L1", "name": "zones", "geometry_type": "polygon", "visible": True},
        ],
        "layouts": [{"name": "test_layout"}],
    }
    base.update(overrides)
    return base


def test_project_load_returns_layers_and_layouts(fake_executor):
    fake_executor.responses["project_load"] = _ok_response()
    result = qgis_project_load(qgz_path="/tmp/proj.qgz")
    cmd, params = fake_executor.calls[0]
    assert cmd == "project_load"
    assert params["qgz_path"] == os.path.abspath("/tmp/proj.qgz")
    assert len(result.layers) == 1
    assert result.layers[0].layer_id == "L1"
    assert result.layouts[0].name == "test_layout"


def test_project_load_path_resolved_to_absolute(fake_executor):
    fake_executor.responses["project_load"] = _ok_response()
    qgis_project_load(qgz_path="rel.qgz")
    params = fake_executor.calls[0][1]
    assert os.path.isabs(params["qgz_path"])


def test_invalid_qgz_raises_project_load_error(fake_executor):
    def fail(_params):
        raise ExecutorError("project_load", "Failed to read project")
    fake_executor.responses["project_load"] = fail

    with pytest.raises(ProjectLoadError, match=r"proj\.qgz"):
        qgis_project_load(qgz_path="/tmp/proj.qgz")
