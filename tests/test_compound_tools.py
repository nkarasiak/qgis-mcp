"""Compound tool mode (QGIS_MCP_TOOL_MODE=compound): schema, dispatch, parity, instance refusal."""

import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import COMPOUND_TOOL_COUNT, make_ctx
from mcp_compat import connect, schema

from qgis_mcp.compound_tools import FastMCP, register_compound_tools
from qgis_mcp.server import ToolError


@pytest.mark.asyncio
async def test_compound_tools_expose_params_object():
    """Regression for #24: compound schemas must carry an object-typed `params`.

    A `**kwargs` signature degenerates into a required string named `kwargs`,
    which makes every parameterised action uncallable.
    """
    mcp = FastMCP("compound-schema-test")
    register_compound_tools(
        mcp,
        _send=AsyncMock(return_value={}),
        _confirm_destructive=AsyncMock(return_value=True),
    )

    tools = await mcp.list_tools()
    assert len(tools) == COMPOUND_TOOL_COUNT
    for tool in tools:
        properties = schema(tool)["properties"]
        assert "kwargs" not in properties, f"{tool.name} still exposes **kwargs"
        assert set(properties) == {"action", "params"}, tool.name
        assert schema(tool).get("required") == ["action"], tool.name
        # params must accept an arbitrary object (nullable, defaulted)
        variants = properties["params"].get("anyOf", [properties["params"]])
        assert any(v.get("type") == "object" for v in variants), tool.name


@pytest.mark.asyncio
async def test_compound_tool_forwards_params_to_send():
    """Parameters passed inside `params` must reach the underlying command."""
    send = AsyncMock(return_value={"expression": "2+3", "result": 5})
    mcp = FastMCP("compound-call-test")
    register_compound_tools(mcp, _send=send, _confirm_destructive=AsyncMock(return_value=True))

    await mcp.call_tool("expression", {"action": "evaluate", "params": {"expression": "2+3"}})
    cmd, params = send.call_args[0][:2]
    assert cmd == "evaluate_expression"
    assert params["expression"] == "2+3"


def test_compound_mode_covers_every_granular_command():
    """Compound mode must reach every plugin command the granular tools expose.

    Compound mode is meant to be a re-packaging of the same surface, not a
    subset - an action missing here means the feature is unreachable for any
    client running with QGIS_MCP_TOOL_MODE=compound.
    """
    src_dir = Path(__file__).resolve().parent.parent / "src" / "qgis_mcp"
    granular_src = (src_dir / "server.py").read_text()
    compound_src = (src_dir / "compound_tools.py").read_text()

    def sent_commands(text):
        return set(re.findall(r'_send\(\s*"([a-z0-9_]+)"', text))

    granular = sent_commands(granular_src)
    compound = sent_commands(compound_src)
    # Commands reached through indirection rather than a literal _send() call.
    compound |= set(re.findall(r'"\w+":\s*"([a-z0-9_]+)"', compound_src.split("def register")[0]))
    compound |= {"validate_expression", "evaluate_expression"}  # chosen via a ternary

    missing = sorted(granular - compound)
    assert not missing, f"commands unreachable in compound mode: {missing}"


@pytest.mark.asyncio
async def test_compound_new_groups_registered():
    """The field/analysis groups and the extended layer/processing actions exist."""
    mcp = FastMCP("compound-groups-test")
    register_compound_tools(
        mcp, _send=AsyncMock(return_value={}), _confirm_destructive=AsyncMock(return_value=True)
    )
    tools = {t.name: t.description for t in await mcp.list_tools()}
    assert {"field", "analysis"} <= set(tools)
    for action in ("export", "add_web", "save_style", "apply_style", "add_join"):
        assert action in tools["layer"], f"layer.{action} undocumented"
    for action in ("execute_batch", "get_providers", "list_models", "run_model"):
        assert action in tools["processing"], f"processing.{action} undocumented"


@pytest.mark.asyncio
async def test_compound_field_and_analysis_dispatch():
    """New actions must forward their params to the right plugin command."""
    send = AsyncMock(return_value={"ok": True})
    mcp = FastMCP("compound-dispatch-test")
    register_compound_tools(mcp, _send=send, _confirm_destructive=AsyncMock(return_value=True))

    # ctx.info() on some actions needs a live request context.
    async with connect(mcp) as client:
        await client.call_tool(
            "field",
            {
                "action": "calculate",
                "params": {"layer_id": "L1", "field_name": "v2", "expression": '"v" * 2'},
            },
        )
        cmd, params = send.call_args[0][:2]
        assert cmd == "field_calculator"
        assert params["expression"] == '"v" * 2'
        assert params["field_type"] == "double"

        await client.call_tool(
            "analysis",
            {
                "action": "zonal_statistics",
                "params": {"polygon_layer": "P", "raster_layer": "R", "stats": [0, 2]},
            },
        )
        cmd, params = send.call_args[0][:2]
        assert cmd == "zonal_statistics"
        assert params["stats"] == [0, 2]
        assert params["band"] == 1

        await client.call_tool(
            "processing", {"action": "run_model", "params": {"model": "model:x"}}
        )
        cmd, params = send.call_args[0][:2]
        assert cmd == "run_model"
        assert params == {"model": "model:x", "parameters": {}}


def test_compound_mode_refuses_multiple_instances():
    """Compound tools cannot select an instance, so the combination must not start.

    Silently routing every compound call to one instance while the config
    advertises several is a wrong-instance write with no error - worse than
    refusing to boot.
    """
    env = {
        **os.environ,
        "QGIS_MCP_TOOL_MODE": "compound",
        "QGIS_MCP_INSTANCES": "a=9876,b=9877",
        "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src"),
    }
    proc = subprocess.run(
        [sys.executable, "-c", "import qgis_mcp.server"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "does not support multiple instances" in proc.stderr


def test_compound_mode_allows_single_instance():
    """Single-instance compound mode is unaffected by the guard."""
    env = {
        **os.environ,
        "QGIS_MCP_TOOL_MODE": "compound",
        "QGIS_MCP_INSTANCES": "solo=9876",
        "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src"),
    }
    proc = subprocess.run(
        [sys.executable, "-c", "import qgis_mcp.server"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.asyncio
async def test_compound_unknown_action_is_refused():
    """A mistyped action must come back naming itself, not as a masked tool error."""
    mcp = FastMCP("compound-unknown-action-test")
    register_compound_tools(
        mcp, _send=AsyncMock(return_value={}), _confirm_destructive=AsyncMock(return_value=True)
    )
    with pytest.raises(ToolError, match="Unknown system action: nope"):
        await mcp._tool_manager.get_tool("system").fn(ctx=None, action="nope")


@pytest.mark.asyncio
async def test_compound_missing_required_param_names_itself():
    """A bare KeyError reached the client as "'expression'", or masked on mcp >= 2.1."""
    mcp = FastMCP("compound-missing-param-test")
    register_compound_tools(
        mcp, _send=AsyncMock(return_value={}), _confirm_destructive=AsyncMock(return_value=True)
    )
    with pytest.raises(ToolError, match="missing required parameter 'expression'"):
        await mcp._tool_manager.get_tool("expression").fn(ctx=None, action="evaluate", params={})


@pytest.mark.asyncio
async def test_compound_code_execute_script_that_raises_is_a_tool_error():
    mcp = FastMCP("compound-code-error-test")
    failed = {"executed": False, "error": "boom", "traceback": "Traceback ...", "elapsed": 0.1}
    register_compound_tools(
        mcp, _send=AsyncMock(return_value=failed), _confirm_destructive=AsyncMock(return_value=True)
    )
    with pytest.raises(ToolError, match=r"Code failed after 0\.1s: boom"):
        await mcp._tool_manager.get_tool("code").fn(
            ctx=make_ctx(), action="execute", params={"code": "raise"}
        )


@pytest.mark.asyncio
async def test_compound_refuses_a_misspelled_param_before_sending():
    """A typo ("expresion") was dropped, returning unfiltered features as the answer."""
    send = AsyncMock(return_value={"features": []})
    mcp = FastMCP("compound-typo-test")
    register_compound_tools(mcp, _send=send, _confirm_destructive=AsyncMock(return_value=True))

    with pytest.raises(ToolError, match=r"unknown parameter\(s\) \['expresion'\]"):
        await mcp._tool_manager.get_tool("features").fn(
            ctx=None, action="get", params={"layer_id": "l", "expresion": "pop > 1e6"}
        )
    send.assert_not_called()


# Placeholder values by parameter name, good enough for every handler to build its
# payload and reach _send - where the unknown-parameter check runs.
_LIST_PARAM = re.compile(
    r"(points|fields|fids|predicates|stats|steps|commands|layer_ids|inputs|outputs|_list"
    r"|layers|features|updates|columns|values|order)$"
)
_NUMBER_PARAM = re.compile(
    r"(timeout|limit|offset|band|dpi|width|height|^x$|^y$|size|classes|method|scale|opacity"
    r"|index|precision|length|rotation|distance|heading|pitch|min_value|max_value|zoom)"
)
# Words the description parser picks up from prose, not parameter names.
_NOT_PARAMS = {"str", "int", "list", "dict", "float", "bool", "extension", "models"}


def _placeholder(name):
    if _LIST_PARAM.search(name):
        return []
    if name in ("parameters", "attributes", "variables"):
        return {}
    if name == "point":
        return [0.0, 0.0]
    if name == "bbox":
        return {"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1}
    if _NUMBER_PARAM.search(name):
        return 1
    return "x"


@pytest.mark.asyncio
async def test_compound_accepts_every_documented_param():
    """The unknown-parameter check must never refuse a parameter the description lists."""
    send = AsyncMock(return_value={"results": [], "base64_data": "AA", "layers": [], "ok": True})
    mcp = FastMCP("compound-documented-params-test")
    register_compound_tools(mcp, _send=send, _confirm_destructive=AsyncMock(return_value=True))
    ctx = MagicMock(info=AsyncMock(), report_progress=AsyncMock())
    refused, checked = [], 0
    for tool in await mcp.list_tools():
        for line in (tool.description or "").splitlines():
            match = re.match(r"- (\w+):\s*(.*)", line.strip())
            if not match:
                continue
            action, rest = match.groups()
            names = [n for n in re.findall(r"(\w+) \(", rest) if n not in _NOT_PARAMS]
            send.reset_mock()
            try:
                await mcp._tool_manager.get_tool(tool.name).fn(
                    ctx=ctx, action=action, params={n: _placeholder(n) for n in names}
                )
            except ToolError as e:
                if "unknown parameter" in str(e):
                    refused.append(f"{tool.name}.{action}: {e}")
            checked += send.called
    assert refused == []
    assert checked > 100, "most actions must reach _send, or this test checks nothing"
