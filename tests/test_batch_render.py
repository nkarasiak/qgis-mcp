"""Tests for qgis_batch_render — mocked plugin executor."""

from __future__ import annotations

import os

import pytest

from qgis_mcp_workflows.errors import ExecutorError, FieldNotFoundError
from qgis_mcp_workflows.server import qgis_batch_render


def _ok_response(**overrides) -> dict:
    base = {
        "output_dir": "/tmp/batch/",
        "n_rendered": 2,
        "manifest": [
            {"value": "Z01", "output_path": "/tmp/batch/Z01.png", "extent": [0.0, 0.0, 1.0, 1.0]},
            {"value": "Z02", "output_path": "/tmp/batch/Z02.png", "extent": [0.0, 0.0, 1.0, 1.0]},
        ],
        "errors": [],
    }
    base.update(overrides)
    return base


def test_batch_render_emits_manifest(fake_executor):
    fake_executor.responses["batch_render"] = _ok_response()
    result = qgis_batch_render(
        template_qgz="/tmp/tpl.qgz",
        attribute="zone_id",
        values=["Z01", "Z02"],
        output_dir="/tmp/batch/",
    )
    cmd, params = fake_executor.calls[0]
    assert cmd == "batch_render"
    assert params["attribute"] == "zone_id"
    assert params["values"] == ["Z01", "Z02"]
    assert len(result.manifest) == 2
    assert result.manifest[0].value == "Z01"
    assert result.n_rendered == 2


def test_batch_render_with_layout(fake_executor):
    fake_executor.responses["batch_render"] = _ok_response()
    qgis_batch_render(
        template_qgz="/tmp/tpl.qgz",
        attribute="zone_id",
        values=["Z01"],
        output_dir="/tmp/batch/",
        layout_name="test_layout",
    )
    params = fake_executor.calls[0][1]
    assert params["layout_name"] == "test_layout"


def test_batch_render_without_layout_renders_canvas(fake_executor):
    fake_executor.responses["batch_render"] = _ok_response()
    qgis_batch_render(
        template_qgz="/tmp/tpl.qgz",
        attribute="zone_id",
        values=["Z01"],
        output_dir="/tmp/batch/",
    )
    params = fake_executor.calls[0][1]
    assert params.get("layout_name") is None


def test_batch_render_filename_template_passed(fake_executor):
    fake_executor.responses["batch_render"] = _ok_response()
    qgis_batch_render(
        template_qgz="/tmp/tpl.qgz",
        attribute="zone_id",
        values=["Z01"],
        output_dir="/tmp/batch/",
        filename_template="choropleth_{value}.png",
    )
    params = fake_executor.calls[0][1]
    assert params["filename_template"] == "choropleth_{value}.png"


def test_batch_render_errors_surface(fake_executor):
    fake_executor.responses["batch_render"] = _ok_response(
        n_rendered=2,
        manifest=[
            {"value": "Z01", "output_path": "/tmp/batch/Z01.png", "extent": [0.0, 0.0, 1.0, 1.0]},
            {"value": "Z02", "output_path": "/tmp/batch/Z02.png", "extent": [0.0, 0.0, 1.0, 1.0]},
        ],
        errors=[{"value": "Z99", "error": "No features match filter"}],
    )
    result = qgis_batch_render(
        template_qgz="/tmp/tpl.qgz",
        attribute="zone_id",
        values=["Z01", "Z02", "Z99"],
        output_dir="/tmp/batch/",
    )
    assert len(result.errors) == 1
    assert result.errors[0].value == "Z99"


def test_missing_attribute_raises_field_not_found(fake_executor):
    def fail(_params):
        raise ExecutorError(
            "batch_render",
            "FIELD_NOT_FOUND: 'nope'. Available: ['zone_id', 'name']",
        )
    fake_executor.responses["batch_render"] = fail

    with pytest.raises(FieldNotFoundError, match="nope"):
        qgis_batch_render(
            template_qgz="/tmp/tpl.qgz",
            attribute="nope",
            values=["Z01"],
            output_dir="/tmp/batch/",
        )


def test_empty_values_returns_empty_manifest_no_dispatch(fake_executor):
    """values=[] short-circuits without dispatching."""
    result = qgis_batch_render(
        template_qgz="/tmp/tpl.qgz",
        attribute="zone_id",
        values=[],
        output_dir="/tmp/batch/",
    )
    assert fake_executor.calls == []
    assert result.n_rendered == 0
    assert result.manifest == []
    assert result.errors == []


def test_paths_resolved_to_absolute(fake_executor):
    fake_executor.responses["batch_render"] = _ok_response()
    qgis_batch_render(
        template_qgz="rel.qgz",
        attribute="zone_id",
        values=["Z01"],
        output_dir="rel_out/",
    )
    params = fake_executor.calls[0][1]
    assert os.path.isabs(params["template_qgz"])
    assert os.path.isabs(params["output_dir"])
