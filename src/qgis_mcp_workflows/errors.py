"""Typed exceptions for qgis-mcp-workflows tools.

Every exception message ends with one suggested next tool call, per
``docs/DESIGN.md`` §5 ("actionable error messages"). Tools raise these;
FastMCP turns them into MCP error responses.
"""

from __future__ import annotations


class QgisMcpWorkflowsError(Exception):
    """Base class for all qgis-mcp-workflows tool errors."""


class PluginUnavailableError(QgisMcpWorkflowsError):
    """The QGIS plugin socket isn't reachable."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__(
            f"Cannot reach QGIS plugin at {host}:{port}. "
            f"Open QGIS, enable the 'QGIS MCP Workflows' plugin, click Start. "
            f"Next: retry the same tool call."
        )
        self.host = host
        self.port = port


class ExecutorError(QgisMcpWorkflowsError):
    """Generic plugin-side error (unmapped)."""

    def __init__(self, command: str, message: str) -> None:
        super().__init__(
            f"Plugin command '{command}' failed: {message}. "
            f"Next: inspect inputs, then retry. If persistent, escape via qgis_eval."
        )
        self.command = command
        self.message = message


class LayerNotFoundError(QgisMcpWorkflowsError):
    """A layer file or layer_id couldn't be resolved."""

    def __init__(self, path_or_id: str) -> None:
        super().__init__(
            f"Layer not found: {path_or_id!r}. "
            f"Next: call qgis_layer_inspect with an absolute path to verify the file exists."
        )
        self.path_or_id = path_or_id


class FieldNotFoundError(QgisMcpWorkflowsError):
    """A field name isn't present on the layer / CSV."""

    def __init__(self, field: str, available: list[str]) -> None:
        avail = ", ".join(available) if available else "(none)"
        super().__init__(
            f"Field {field!r} not found. Available: {avail}. "
            f"Next: call qgis_layer_inspect to see actual field names, then retry with the correct value_field."
        )
        self.field = field
        self.available = available


class JoinError(QgisMcpWorkflowsError):
    """A CSV → polygon join produced zero matches (clearly wrong join key).

    Constructor takes a free-form description; the plugin already embeds the
    join field name and sample CSV / layer values in its error message.
    """

    def __init__(self, description: str) -> None:
        super().__init__(
            f"{description} "
            f"Likely cause: zone-id systems differ (PFLOW uses MFS##, PRF##, Z## in different files). "
            f"Next: call qgis_layer_inspect on the polygon layer to see the actual join field's values."
        )
        self.description = description


class HeadlessUnavailableError(QgisMcpWorkflowsError):
    """The headless transport's QGIS Python launcher could not be found or started."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"Headless transport unavailable: {detail}. "
            f"Install OSGeo4W (Windows) or QGIS standalone Python with PyQGIS, then set "
            f"QGIS_MCP_WORKFLOWS_QGIS_LAUNCHER to its python-qgis(-ltr).bat / qgis_python wrapper. "
            f"Next: re-run with --transport=plugin if QGIS Desktop is open instead."
        )
        self.detail = detail


class CrsMismatchError(QgisMcpWorkflowsError):
    """A requested CRS override could not be applied to the loaded layer."""

    def __init__(self, requested_crs: str, detail: str) -> None:
        super().__init__(
            f"Could not apply CRS {requested_crs!r}: {detail}. "
            f"Next: call qgis_layer_inspect to confirm the file's native CRS, "
            f"then retry qgis_load_layer with a known-valid EPSG code (e.g. EPSG:4326)."
        )
        self.requested_crs = requested_crs
        self.detail = detail


class EmptyAfterFilterError(QgisMcpWorkflowsError):
    """An extent / sampling / filter step left zero features to render."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"No rows survive the filter: {detail}. "
            f"Next: widen extent, raise sample_rate, or call qgis_layer_inspect on the source to confirm coverage."
        )
        self.detail = detail


class ProjectLoadError(QgisMcpWorkflowsError):
    """A QGIS project (.qgz / .qgs) failed to load."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(
            f"Failed to load project {path!r}: {reason}. "
            f"Next: open the file in QGIS Desktop to confirm it loads cleanly, then retry."
        )
        self.path = path
        self.reason = reason


class LayoutNotFoundError(QgisMcpWorkflowsError):
    """A print-composer layout name is missing from the loaded project."""

    def __init__(self, name: str, available: list[str]) -> None:
        avail = ", ".join(available) if available else "(none)"
        super().__init__(
            f"Layout {name!r} not found in project. Available: {avail}. "
            f"Next: call qgis_project_load to list available layouts, then retry with one of those names."
        )
        self.name = name
        self.available = available
