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
    result["checks"].append({"name": "version_match", "status": version_match, "detail": detail})
    if version_match == "mismatch" and result["status"] == "healthy":
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
    if path:
        content.append(
            TextContent(
                type="text",
                text=json.dumps({"saved": path, "width": width, "height": height}),
                annotations=Annotations(audience=["assistant"], priority=0.5),
            )
        )
    return content
