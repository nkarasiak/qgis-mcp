"""Tests for qgis_style_graduated — mocked plugin executor."""

from __future__ import annotations

import pytest

from qgis_mcp_workflows.errors import ExecutorError, FieldNotFoundError
from qgis_mcp_workflows.server import qgis_style_graduated


def _ok_response(**overrides) -> dict:
    base = {
        "ok": True,
        "n_classes": 5,
        "classes": [
            {"value": "0.0-100.0", "color": "#fff5eb", "n_features": 10},
            {"value": "100.0-500.0", "color": "#fdd0a2", "n_features": 12},
            {"value": "500.0-1000.0", "color": "#fdae6b", "n_features": 8},
            {"value": "1000.0-5000.0", "color": "#fd8d3c", "n_features": 15},
            {"value": "5000.0-10000.0", "color": "#d94801", "n_features": 2},
        ],
        "breaks": [0.0, 100.0, 500.0, 1000.0, 5000.0, 10000.0],
        "mode": "quantile",
    }
    base.update(overrides)
    return base


def test_returns_graduated_result_with_breaks_and_mode(fake_executor):
    fake_executor.responses["set_layer_style"] = _ok_response()
    result = qgis_style_graduated(
        layer_id="L1", field="total_trips", n_classes=5, mode="quantile"
    )
    cmd, params = fake_executor.calls[0]
    assert cmd == "set_layer_style"
    assert params["style_type"] == "graduated"
    assert params["field"] == "total_trips"
    assert params["classes"] == 5
    assert params["mode"] == "quantile"
    assert result.n_classes == 5
    assert result.breaks == [0.0, 100.0, 500.0, 1000.0, 5000.0, 10000.0]
    assert result.mode == "quantile"


def test_default_mode_is_quantile(fake_executor):
    fake_executor.responses["set_layer_style"] = _ok_response()
    qgis_style_graduated(layer_id="L1", field="total_trips")
    params = fake_executor.calls[0][1]
    assert params["mode"] == "quantile"


def test_palette_mapped_to_color_ramp(fake_executor):
    fake_executor.responses["set_layer_style"] = _ok_response()
    qgis_style_graduated(layer_id="L1", field="total_trips", palette="YlOrRd")
    params = fake_executor.calls[0][1]
    assert params["color_ramp"] == "YlOrRd"


def test_missing_field_raises_field_not_found(fake_executor):
    def fail(_params):
        raise ExecutorError("set_layer_style", "Field not found: nope")
    fake_executor.responses["set_layer_style"] = fail

    with pytest.raises(FieldNotFoundError, match="nope"):
        qgis_style_graduated(layer_id="L1", field="nope")
