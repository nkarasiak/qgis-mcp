"""Tests for qgis_eval — mocked plugin executor.

Verifies MCP-side handling of return_vars capture and exception passthrough.
The plugin handler does the actual exec() in QGIS; here we mock its responses.
"""

from __future__ import annotations

from qgis_mcp_workflows.server import qgis_eval


def test_simple_return_vars_captured(fake_executor):
    fake_executor.responses["execute_code"] = {
        "executed": True,
        "stdout": "",
        "stderr": "",
        "return_values": {"x": 42, "y": "hello"},
    }
    result = qgis_eval(code="x = 42\ny = 'hello'", return_vars=["x", "y"])
    cmd, params = fake_executor.calls[0]
    assert cmd == "execute_code"
    assert params["code"] == "x = 42\ny = 'hello'"
    assert params["return_vars"] == ["x", "y"]
    assert result.return_values == {"x": 42, "y": "hello"}
    assert result.exception is None


def test_non_serializable_falls_back_to_repr(fake_executor):
    """Plugin's _json_safe() converts QgsGeometry / QgsVectorLayer to repr() strings."""
    fake_executor.responses["execute_code"] = {
        "executed": True,
        "stdout": "",
        "stderr": "",
        "return_values": {"layer": "<QgsVectorLayer: 'zones'>"},
    }
    result = qgis_eval(code="layer = iface.activeLayer()", return_vars=["layer"])
    assert result.return_values["layer"].startswith("<QgsVectorLayer")


def test_exception_populates_exception_field(fake_executor):
    fake_executor.responses["execute_code"] = {
        "executed": False,
        "error": "division by zero",
        "traceback": "Traceback ...\nZeroDivisionError: division by zero\n",
        "stdout": "",
        "stderr": "",
    }
    result = qgis_eval(code="1/0")
    assert result.exception is not None
    assert "division by zero" in result.exception
    assert result.return_values is None


def test_no_return_vars_omits_return_values(fake_executor):
    """Without return_vars, the return_values field stays None."""
    fake_executor.responses["execute_code"] = {
        "executed": True,
        "stdout": "hello\n",
        "stderr": "",
    }
    result = qgis_eval(code="print('hello')")
    params = fake_executor.calls[0][1]
    assert params.get("return_vars") is None
    assert result.return_values is None
    assert result.stdout == "hello\n"


def test_unset_var_omitted_from_return_values(fake_executor):
    """Plugin omits unbound vars; MCP passes the dict through verbatim."""
    fake_executor.responses["execute_code"] = {
        "executed": True,
        "stdout": "",
        "stderr": "",
        "return_values": {"defined": 1},  # 'undefined' omitted by plugin
    }
    result = qgis_eval(code="defined = 1", return_vars=["defined", "undefined"])
    assert "undefined" not in result.return_values
    assert result.return_values["defined"] == 1
