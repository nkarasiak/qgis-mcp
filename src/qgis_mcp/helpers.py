"""Shared helpers for server.py and compound_tools.py.

Imports only from ``mcp``, stdlib, and the stdlib-only ``protocol``
module - no circular-import risk. Protocol constants live in
``protocol.py`` (and are re-exported here) so the client stays
importable without the ``mcp`` package.
"""

import json

from mcp.types import Annotations, ImageContent, ResourceLink, TextContent

from qgis_mcp.protocol import (  # noqa: F401 - re-exported for server-side importers
    BATCH_BLOCKED_COMMANDS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    HEADER_STRUCT,
    MAX_MESSAGE_SIZE,
    RECV_CHUNK_SIZE,
    TIMEOUT_DEFAULT,
    TIMEOUT_LONG,
    CommandTimeout,
    get_auth_token,
    get_client_version,
    get_update_command,
)

MAX_FEATURE_LIMIT = 50


def feature_limit_error(limit: int) -> str | None:
    """Refusal text for a get_layer_features limit above the cap, else None.

    It used to be clamped to 50 unseen, so a caller asking for 200 read 50
    features as if they were all it asked for.
    """
    if limit > MAX_FEATURE_LIMIT:
        return (
            f"limit {limit} exceeds the maximum of {MAX_FEATURE_LIMIT}; "
            "page through the rest with offset"
        )
    return None


def code_failure_message(result: dict) -> str | None:
    """Error text for an execute_code result whose script raised or timed out, else None.

    The plugin reports a failed script as a successful command with
    ``executed: False``; the MCP tools raise this text as a ToolError so the
    client sees ``isError`` and still gets the traceback and partial output.
    """
    if result.get("executed", True):
        return None
    parts = [
        f"Code failed after {result.get('elapsed', '?')}s: {result.get('error', 'unknown error')}"
    ]
    for key in ("traceback", "stdout", "stderr"):
        if result.get(key):
            parts.append(f"{key}:\n{result[key].rstrip()}")
    if not result.get("timed_out"):
        parts.append("Changes the script made before it failed are kept.")
    return "\n\n".join(parts)


def enrich_diagnose(result: dict) -> dict:
    """Append server/plugin version-match check to a diagnose result."""
    server_version = get_client_version()
    if server_version == "unknown":
        server_version = "unknown (editable install?)"

    plugin_version = None
    for check in result.get("checks", []):
        if check["name"] == "plugin_version":
            plugin_version = check.get("detail")
            break

    version_match = "ok" if plugin_version == server_version else "mismatch"
    detail = {"server": server_version, "plugin": plugin_version}
    if version_match == "mismatch":
        # The whole point of reporting a mismatch is that someone can act on it.
        # Say what it actually costs, too: mismatched halves keep working, and a
        # report that reads like an outage gets ignored the next time it matters.
        detail["fix"] = get_update_command()
        detail["restart_after_fix"] = True
        detail["note"] = (
            "Not fatal. Mismatched halves keep working; tools added since the "
            "older half was built will be missing or refused, so matching them "
            "is recommended rather than required. Restart your MCP client after "
            "running the fix."
        )
    # An older plugin's diagnose may not carry either key, and this check is the
    # one that reports exactly that kind of drift - it must not raise on it.
    result.setdefault("checks", []).append(
        {"name": "version_match", "status": version_match, "detail": detail}
    )
    if version_match == "mismatch" and result.get("status") == "healthy":
        result["status"] = "degraded"

    return result


def make_layer_response(result: dict, fallback_name: str = "Layer") -> list:
    """Build [TextContent, ResourceLink] for a layer-mutating tool response."""
    layer_id = result.get("layer_id", result.get("id", ""))
    return [
        TextContent(type="text", text=json.dumps(result)),
        ResourceLink(
            type="resource_link",
            uri=f"qgis://layers/{layer_id}/info",
            name=result.get("name", fallback_name),
        ),
    ]


def make_project_response(result: dict) -> list:
    """Build [TextContent, ResourceLink] for a project-mutating tool response."""
    return [
        TextContent(type="text", text=json.dumps(result)),
        ResourceLink(type="resource_link", uri="qgis://project", name="Project Info"),
    ]


def make_render_response(result: dict, width: int, height: int, path: str | None) -> list:
    """Build [ImageContent, optional TextContent] for a render_map response."""
    content: list = [
        ImageContent(
            type="image",
            data=result["base64_data"],
            mimeType="image/png",
            annotations=Annotations(audience=["user", "assistant"], priority=1.0),
        )
    ]
    info: dict = {}
    if path:
        info.update({"saved": path, "width": width, "height": height})
    if result.get("warnings"):
        # Layers that failed to draw; the image alone looks like a clean render.
        info["warnings"] = result["warnings"]
    if info:
        content.append(
            TextContent(
                type="text",
                text=json.dumps(info),
                annotations=Annotations(audience=["assistant"], priority=0.5),
            )
        )
    return content
