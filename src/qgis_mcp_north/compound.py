"""Compound-mode tools — 4 grouped wrappers that collapse 12 fine-grained tools.

Activated by ``QGIS_MCP_NORTH_TOOL_MODE=compound``. In compound mode FastMCP exposes
4 grouped tools (qgis_inspect, qgis_style, qgis_render, qgis_export) instead of the
12 standalone tools. ``qgis_eval`` registers in both modes (universal escape hatch).

Token savings: a 5-tool surface (4 compound + eval) has ~60% fewer tool-schema bytes
than the 13-tool surface, useful for token-constrained LLMs (Haiku, small open-weights).

The compound wrappers do NOT reimplement logic — they import the standalone tool
functions from ``server`` and dispatch via match statements on a discriminator arg.
Same code path, same behavior, same tests pass for the underlying functions.
"""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from qgis_mcp_north.server import (
    BatchRenderResult,
    ChoroplethResult,
    ExportResult,
    GraduatedStyleResult,
    LayerInfo,
    LoadedLayer,
    ODFlowResult,
    PptxResult,
    ProjectInfo,
    RenderResult,
    StyleResult,
    TrajectoryResult,
    _maybe_compound_tool,
    qgis_batch_render,
    qgis_export_layout,
    qgis_figures_to_pptx,
    qgis_layer_inspect,
    qgis_load_layer,
    qgis_project_load,
    qgis_render_choropleth,
    qgis_render_map,
    qgis_render_od_flows,
    qgis_render_trajectory,
    qgis_style_categorized,
    qgis_style_graduated,
)

# ---------------------------------------------------------------------------
# qgis_inspect — replaces qgis_layer_inspect / qgis_load_layer / qgis_project_load
# ---------------------------------------------------------------------------


@_maybe_compound_tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=False, destructiveHint=False, openWorldHint=True
    )
)
def qgis_inspect(
    kind: Annotated[Literal["layer", "project"], Field(description='"layer" for a vector/raster file, "project" for a .qgz/.qgs.')],
    path: Annotated[str, Field(description="Absolute path to the file.")],
    register: Annotated[bool, Field(description="kind='layer' only: True keeps the layer loaded (mutates project) and returns layer_id; False loads transiently for metadata only.")] = False,
    name: Annotated[str | None, Field(description="kind='layer' + register=True only: optional display name.")] = None,
    crs: Annotated[str | None, Field(description='kind=\'layer\' + register=True only: override CRS, e.g. "EPSG:4326".')] = None,
) -> LayerInfo | LoadedLayer | ProjectInfo:
    """Inspect or load a layer/project — compound replacement for the 3 inspection tools.

    Dispatch:
    - kind="layer" + register=False → metadata-only inspect (no project mutation)
    - kind="layer" + register=True  → register layer + return layer_id
    - kind="project"                → load .qgz/.qgs + return layers + layouts

    When to use: every read-only or layer/project-loading step in a pipeline.
    """
    if kind == "layer":
        if register:
            return qgis_load_layer(path=path, name=name, crs=crs)
        return qgis_layer_inspect(path=path)
    if kind == "project":
        return qgis_project_load(qgz_path=path)
    raise ValueError(f"Unknown kind: {kind!r}")


# ---------------------------------------------------------------------------
# qgis_style — replaces qgis_style_categorized / qgis_style_graduated
# ---------------------------------------------------------------------------


@_maybe_compound_tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=True, destructiveHint=False, openWorldHint=False
    )
)
def qgis_style(
    type: Annotated[Literal["categorized", "graduated"], Field(description='"categorized" for one-color-per-value, "graduated" for value-binned ramp.')],
    layer_id: Annotated[str, Field(description="layer_id from qgis_inspect(register=True) or qgis_inspect(kind='project').")],
    field: Annotated[str, Field(description="Field name to style on.")],
    palette: Annotated[str, Field(description='ColorBrewer palette name, e.g. "Set2" for categorized, "YlOrRd" for graduated.')] = "Spectral",
    n_classes: Annotated[int, Field(description="graduated only: number of bins.", ge=2, le=15)] = 5,
    mode: Annotated[Literal["quantile", "equal_interval", "natural_breaks", "pretty"], Field(description="graduated only: binning strategy.")] = "quantile",
    classes: Annotated[list[str] | None, Field(description="categorized only: subset/order of category values.")] = None,
) -> StyleResult | GraduatedStyleResult:
    """Apply categorical or graduated symbology — compound replacement for the 2 styling tools.

    Dispatch:
    - type="categorized" → qgis_style_categorized (palette, classes)
    - type="graduated"   → qgis_style_graduated (palette, n_classes, mode)
    """
    if type == "categorized":
        return qgis_style_categorized(
            layer_id=layer_id, field=field, palette=palette, classes=classes
        )
    if type == "graduated":
        return qgis_style_graduated(
            layer_id=layer_id, field=field, n_classes=n_classes, mode=mode, palette=palette
        )
    raise ValueError(f"Unknown type: {type!r}")


# ---------------------------------------------------------------------------
# qgis_render — replaces the 4 render tools
# ---------------------------------------------------------------------------


@_maybe_compound_tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=True, destructiveHint=False, openWorldHint=True
    )
)
def qgis_render(
    mode: Annotated[Literal["map", "choropleth", "trajectory", "od_flows"], Field(description='Render mode.')],
    output_png: Annotated[str, Field(description="Absolute path for the output PNG.")],
    # map-mode args
    layer_ids: Annotated[list[str] | None, Field(description='mode="map" only: layer_ids to render bottom-to-top.')] = None,
    extent: Annotated[list[float] | None, Field(description="map/trajectory: [xmin, ymin, xmax, ymax].")] = None,
    background: Annotated[str, Field(description="map: background color.")] = "white",
    # choropleth-mode args
    zones_path: Annotated[str | None, Field(description="choropleth/od_flows: polygon zones file.")] = None,
    value_field: Annotated[str | None, Field(description='choropleth: numeric column to render.')] = None,
    value_csv: Annotated[str | None, Field(description="choropleth: optional CSV joined to zones_path.")] = None,
    join_field: Annotated[str, Field(description="choropleth: join column on both sides.")] = "zone_id",
    n_classes: Annotated[int, Field(description="choropleth bins.", ge=2, le=15)] = 5,
    classification_mode: Annotated[Literal["quantile", "equal_interval", "natural_breaks", "pretty"], Field(description="choropleth binning strategy.")] = "quantile",
    palette: Annotated[str, Field(description="choropleth palette.")] = "YlOrRd",
    title: Annotated[str | None, Field(description="choropleth title.")] = None,
    legend: Annotated[bool, Field(description="choropleth legend.")] = True,
    # trajectory-mode args
    input_path: Annotated[str | None, Field(description='trajectory: CSV or GPX path.')] = None,
    lon_col: Annotated[str, Field(description="trajectory lon col.")] = "lon",
    lat_col: Annotated[str, Field(description="trajectory lat col.")] = "lat",
    time_col: Annotated[str, Field(description="trajectory time col.")] = "datetime",
    id_col: Annotated[str, Field(description="trajectory grouping col.")] = "trip_id",
    mode_col: Annotated[str | None, Field(description='trajectory categorical color column.')] = None,
    render_mode: Annotated[Literal["lines", "points", "heatmap"], Field(description="trajectory visualization style.")] = "lines",
    sample_rate: Annotated[float, Field(description="trajectory sample_rate.", gt=0.0, le=1.0)] = 1.0,
    max_points: Annotated[int, Field(description="trajectory hard cap.", ge=1000)] = 500_000,
    # od_flows-mode args
    od_csv: Annotated[str | None, Field(description='od_flows: OD CSV.')] = None,
    origin_col: Annotated[str, Field(description="od_flows origin col.")] = "origin",
    dest_col: Annotated[str, Field(description="od_flows destination col.")] = "destination",
    value_col: Annotated[str, Field(description="od_flows magnitude col.")] = "trip_count",
    zone_id_field: Annotated[str, Field(description="od_flows zone-id field on zones layer.")] = "zone_id",
    top_n: Annotated[int | None, Field(description="od_flows top-N filter.")] = None,
    # shared
    basemap_paths: Annotated[list[str] | None, Field(description="Optional basemap layers.")] = None,
    width: Annotated[int, Field(description="Image width.", ge=200, le=8000)] = 1600,
    height: Annotated[int, Field(description="Image height.", ge=200, le=8000)] = 1200,
    dpi: Annotated[int, Field(description="Image DPI.", ge=72, le=600)] = 150,
) -> RenderResult | ChoroplethResult | TrajectoryResult | ODFlowResult:
    """Render any figure type — compound replacement for the 4 render tools.

    Dispatch:
    - mode="map"        → qgis_render_map (layer_ids must be set)
    - mode="choropleth" → qgis_render_choropleth (zones_path + value_field)
    - mode="trajectory" → qgis_render_trajectory (input_path)
    - mode="od_flows"   → qgis_render_od_flows (od_csv + zones_path)
    """
    if mode == "map":
        if not layer_ids:
            raise ValueError('qgis_render(mode="map") requires layer_ids.')
        return qgis_render_map(
            layer_ids=layer_ids, output_png=output_png, width=width, height=height,
            dpi=dpi, extent=extent, background=background,
        )
    if mode == "choropleth":
        if not zones_path or not value_field:
            raise ValueError('qgis_render(mode="choropleth") requires zones_path and value_field.')
        return qgis_render_choropleth(
            zones_path=zones_path, value_field=value_field, output_png=output_png,
            value_csv=value_csv, join_field=join_field, n_classes=n_classes,
            mode=classification_mode, palette=palette, title=title, legend=legend,
            basemap_paths=basemap_paths, width=width, height=height, dpi=dpi,
        )
    if mode == "trajectory":
        if not input_path:
            raise ValueError('qgis_render(mode="trajectory") requires input_path.')
        return qgis_render_trajectory(
            input_path=input_path, output_png=output_png, lon_col=lon_col,
            lat_col=lat_col, time_col=time_col, id_col=id_col, mode_col=mode_col,
            render_mode=render_mode, sample_rate=sample_rate, max_points=max_points,
            basemap_paths=basemap_paths, extent=extent, width=width, height=height, dpi=dpi,
        )
    if mode == "od_flows":
        if not od_csv or not zones_path:
            raise ValueError('qgis_render(mode="od_flows") requires od_csv and zones_path.')
        return qgis_render_od_flows(
            od_csv=od_csv, zones_layer_path=zones_path, output_png=output_png,
            origin_col=origin_col, dest_col=dest_col, value_col=value_col,
            zone_id_field=zone_id_field, top_n=top_n, basemap_paths=basemap_paths,
            width=width, height=height, dpi=dpi,
        )
    raise ValueError(f"Unknown mode: {mode!r}")


# ---------------------------------------------------------------------------
# qgis_export — replaces qgis_export_layout / qgis_batch_render / qgis_figures_to_pptx
# ---------------------------------------------------------------------------


@_maybe_compound_tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, idempotentHint=True, destructiveHint=False, openWorldHint=True
    )
)
def qgis_export(
    kind: Annotated[Literal["layout", "batch", "pptx"], Field(description='"layout" exports a single print composer, "batch" fans out per attribute value, "pptx" assembles PNGs into slides.')],
    output_path: Annotated[str | None, Field(description='Output file path for kind in {"layout","pptx"}.')] = None,
    # layout-kind args
    qgz_path: Annotated[str | None, Field(description='kind="layout": project file.')] = None,
    layout_name: Annotated[str | None, Field(description='kind in {"layout","batch"}: print-composer layout name.')] = None,
    format: Annotated[Literal["png", "pdf", "svg"], Field(description='kind="layout" only: output format.')] = "png",
    dpi: Annotated[int, Field(description='kind="layout" DPI.', ge=72, le=600)] = 300,
    # batch-kind args
    template_qgz: Annotated[str | None, Field(description='kind="batch": template project.')] = None,
    attribute: Annotated[str | None, Field(description='kind="batch": field on the active layer to filter by.')] = None,
    values: Annotated[list[str] | None, Field(description='kind="batch": filter values to iterate.')] = None,
    output_dir: Annotated[str | None, Field(description='kind="batch": output directory.')] = None,
    filename_template: Annotated[str, Field(description='kind="batch" filename template.')] = "{value}.png",
    # pptx-kind args
    figure_paths: Annotated[list[str] | None, Field(description='kind="pptx": PNG/JPG paths to add as slides.')] = None,
    layout: Annotated[Literal["title_and_image", "image_only", "two_column", "title_image_caption"], Field(description='kind="pptx": per-slide layout.')] = "title_and_image",
    captions: Annotated[list[str] | None, Field(description='kind="pptx": per-slide captions.')] = None,
    template_pptx: Annotated[str | None, Field(description='kind="pptx": template deck to append to.')] = None,
) -> ExportResult | BatchRenderResult | PptxResult:
    """Export / batch-render / deliver-as-pptx — compound replacement for the 3 export tools.

    Dispatch:
    - kind="layout" → qgis_export_layout (qgz_path + layout_name + output_path)
    - kind="batch"  → qgis_batch_render (template_qgz + attribute + values + output_dir)
    - kind="pptx"   → qgis_figures_to_pptx (figure_paths + output_path)
    """
    if kind == "layout":
        if not qgz_path or not layout_name or not output_path:
            raise ValueError('qgis_export(kind="layout") requires qgz_path, layout_name, output_path.')
        return qgis_export_layout(
            qgz_path=qgz_path, layout_name=layout_name, output_path=output_path,
            format=format, dpi=dpi,
        )
    if kind == "batch":
        if not template_qgz or not attribute or values is None or not output_dir:
            raise ValueError('qgis_export(kind="batch") requires template_qgz, attribute, values, output_dir.')
        return qgis_batch_render(
            template_qgz=template_qgz, attribute=attribute, values=values,
            output_dir=output_dir, layout_name=layout_name,
            filename_template=filename_template,
        )
    if kind == "pptx":
        if not figure_paths or not output_path:
            raise ValueError('qgis_export(kind="pptx") requires figure_paths and output_path.')
        return qgis_figures_to_pptx(
            figure_paths=figure_paths, pptx_path=output_path, layout=layout,
            captions=captions, template_pptx=template_pptx,
        )
    raise ValueError(f"Unknown kind: {kind!r}")
