"""Tests for qgis_export_layout — mocked plugin executor."""

from __future__ import annotations

import os

import pytest

from qgis_mcp_north.errors import ExecutorError, LayoutNotFoundError
from qgis_mcp_north.server import qgis_export_layout


def _ok_response(**overrides) -> dict:
    base = {
        "output_path": "/tmp/out.png",
        "format": "png",
        "n_pages": 1,
        "layout_name": "test_layout",
    }
    base.update(overrides)
    return base


def test_export_layout_png(fake_executor):
    fake_executor.responses["export_layout"] = _ok_response()
    result = qgis_export_layout(
        qgz_path="/tmp/proj.qgz",
        layout_name="test_layout",
        output_path="/tmp/out.png",
    )
    cmd, params = fake_executor.calls[0]
    assert cmd == "export_layout"
    assert params["layout_name"] == "test_layout"
    assert params["format"] == "png"
    assert params["qgz_path"] == os.path.abspath("/tmp/proj.qgz")
    assert result.format == "png"
    assert result.n_pages == 1


def test_export_layout_pdf(fake_executor):
    fake_executor.responses["export_layout"] = _ok_response(format="pdf")
    qgis_export_layout(
        qgz_path="/tmp/proj.qgz",
        layout_name="test_layout",
        output_path="/tmp/out.pdf",
        format="pdf",
    )
    params = fake_executor.calls[0][1]
    assert params["format"] == "pdf"


def test_export_layout_svg(fake_executor):
    fake_executor.responses["export_layout"] = _ok_response(format="svg")
    qgis_export_layout(
        qgz_path="/tmp/proj.qgz",
        layout_name="test_layout",
        output_path="/tmp/out.svg",
        format="svg",
    )
    params = fake_executor.calls[0][1]
    assert params["format"] == "svg"


def test_missing_layout_raises_layout_not_found(fake_executor):
    def fail(_params):
        raise ExecutorError(
            "export_layout",
            "LAYOUT_NOT_FOUND: 'nope'. Available: ['test_layout']",
        )
    fake_executor.responses["export_layout"] = fail

    with pytest.raises(LayoutNotFoundError, match="nope"):
        qgis_export_layout(
            qgz_path="/tmp/proj.qgz",
            layout_name="nope",
            output_path="/tmp/x.png",
        )


def test_paths_resolved_to_absolute(fake_executor):
    fake_executor.responses["export_layout"] = _ok_response()
    qgis_export_layout(
        qgz_path="rel.qgz",
        layout_name="test_layout",
        output_path="rel_out.png",
    )
    params = fake_executor.calls[0][1]
    assert os.path.isabs(params["qgz_path"])
    assert os.path.isabs(params["output_path"])


def test_dpi_passed_through(fake_executor):
    fake_executor.responses["export_layout"] = _ok_response()
    qgis_export_layout(
        qgz_path="/tmp/proj.qgz",
        layout_name="test_layout",
        output_path="/tmp/out.png",
        dpi=200,
    )
    params = fake_executor.calls[0][1]
    assert params["dpi"] == 200
