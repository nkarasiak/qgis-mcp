"""Tests for qgis_style_categorized — mocked plugin executor."""

from __future__ import annotations

import pytest

from qgis_mcp_workflows.errors import ExecutorError, FieldNotFoundError, LayerNotFoundError
from qgis_mcp_workflows.server import qgis_style_categorized


def _ok_response(**overrides) -> dict:
    base = {
        "ok": True,
        "n_classes": 3,
        "classes": [
            {"value": "taxi", "color": "#1f78b4", "n_features": 15},
            {"value": "truck", "color": "#33a02c", "n_features": 10},
            {"value": "bus", "color": "#e31a1c", "n_features": 5},
        ],
    }
    base.update(overrides)
    return base


def test_returns_style_result_with_class_entries(fake_executor):
    fake_executor.responses["set_layer_style"] = _ok_response()
    result = qgis_style_categorized(layer_id="L1", field="transport_mode")
    cmd, params = fake_executor.calls[0]
    assert cmd == "set_layer_style"
    assert params["layer_id"] == "L1"
    assert params["style_type"] == "categorized"
    assert params["field"] == "transport_mode"
    assert result.n_classes == 3
    assert result.classes[0].value == "taxi"
    assert result.classes[0].color == "#1f78b4"
    assert result.classes[0].n_features == 15


def test_palette_mapped_to_color_ramp(fake_executor):
    """MCP `palette` (DESIGN.md) maps to plugin's `color_ramp` (legacy)."""
    fake_executor.responses["set_layer_style"] = _ok_response()
    qgis_style_categorized(layer_id="L1", field="mode", palette="Set2")
    params = fake_executor.calls[0][1]
    assert params["color_ramp"] == "Set2"


def test_missing_field_raises_field_not_found(fake_executor):
    def fail(_params):
        raise ExecutorError("set_layer_style", "Field not found: nope")
    fake_executor.responses["set_layer_style"] = fail

    with pytest.raises(FieldNotFoundError, match="nope"):
        qgis_style_categorized(layer_id="L1", field="nope")


def test_missing_layer_raises_layer_not_found(fake_executor):
    def fail(_params):
        raise ExecutorError("set_layer_style", "Layer not found: L99")
    fake_executor.responses["set_layer_style"] = fail

    with pytest.raises(LayerNotFoundError, match="L99"):
        qgis_style_categorized(layer_id="L99", field="mode")
