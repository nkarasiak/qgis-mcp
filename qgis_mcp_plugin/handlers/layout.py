"""Handlers for print layouts, layout items and atlas export."""

import os
from typing import ClassVar

from qgis.core import (
    QgsLayoutExporter,
    QgsLayoutItemMap,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsPrintLayout,
    QgsProject,
)
from qgis.PyQt.QtGui import QColor

from ..compat import LAYOUT_SUCCESS, RENDER_UNIT_POINTS
from ..errors import CommandError
from ..registry import command


class LayoutHandlers:
    """Print layouts, layout items and atlas export."""

    @command
    def list_layouts(self, **kwargs):
        manager = QgsProject.instance().layoutManager()
        layouts = []
        for layout in manager.layouts():
            layouts.append(
                {
                    "name": layout.name(),
                    "page_count": layout.pageCollection().pageCount(),
                }
            )
        return {"layouts": layouts, "count": len(layouts)}

    # format -> (QgsLayoutExporter settings class, exporter method). Named
    # rather than referenced so the attribute lookup happens on the running
    # QGIS, as elsewhere in the plugin; the accepted list is the table's keys.
    _LAYOUT_EXPORTS: ClassVar[dict] = dict(
        {"pdf": ("PdfExportSettings", "exportToPdf"), "svg": ("SvgExportSettings", "exportToSvg")},
        **dict.fromkeys(
            ("png", "jpg", "jpeg", "tif", "tiff", "bmp"),
            ("ImageExportSettings", "exportToImage"),
        ),
    )

    @command
    def export_layout(self, layout_name, path, format="pdf", dpi=300, **kwargs):
        layout = self._get_layout(layout_name)
        exporter = QgsLayoutExporter(layout)
        settings_name, method = self._pick(self._LAYOUT_EXPORTS, format.lower(), "format")
        settings = getattr(QgsLayoutExporter, settings_name)()
        settings.dpi = dpi
        result = getattr(exporter, method)(path, settings)

        if result != LAYOUT_SUCCESS:
            raise CommandError(f"Export failed with code: {result}")

        return {"ok": True, "path": path}

    @command
    def create_layout(self, name, **kwargs):
        """Create a new print layout."""
        project = QgsProject.instance()
        layout = QgsPrintLayout(project)
        layout.initializeDefaults()
        layout.setName(name)
        project.layoutManager().addLayout(layout)
        return {"ok": True, "name": name}

    @command
    def add_layout_map(self, layout_name, x, y, width, height, **kwargs):
        """Add a map item to a print layout."""
        layout = self._get_layout(layout_name)
        map_item = QgsLayoutItemMap(layout)
        map_item.attemptMove(QgsLayoutPoint(x, y))
        map_item.attemptResize(QgsLayoutSize(width, height))
        map_item.zoomToExtent(self.iface.mapCanvas().extent())
        layout.addLayoutItem(map_item)
        return {"ok": True}

    def _get_layout(self, layout_name):
        """Get a print layout by name or raise."""
        layout = QgsProject.instance().layoutManager().layoutByName(layout_name)
        if not layout:
            raise CommandError(f"Layout not found: {layout_name}")
        return layout

    def _find_layout_map(self, layout, map_item_id=None):
        """Find a map item in a layout by id/uuid, else the first map item."""
        maps = [it for it in layout.items() if isinstance(it, QgsLayoutItemMap)]
        if not maps:
            return None
        if map_item_id:
            for m in maps:
                if m.id() == map_item_id or m.uuid() == map_item_id:
                    return m
        return maps[0]

    @command
    def get_layout_info(self, layout_name, **kwargs):
        """List items in a print layout (type, id, position, size)."""
        layout = self._get_layout(layout_name)
        items = []
        for item in layout.items():
            if not hasattr(item, "uuid"):
                continue
            try:
                pos = item.positionWithUnits()
                size = item.sizeWithUnits()
                x, y = pos.x(), pos.y()
                w, h = size.width(), size.height()
            except Exception:
                x = y = w = h = None
            items.append(
                {
                    "id": item.id(),
                    "uuid": item.uuid(),
                    "type": type(item).__name__,
                    "x": x,
                    "y": y,
                    "width": w,
                    "height": h,
                }
            )
        return {
            "layout": layout_name,
            "items": items,
            "count": len(items),
            "page_count": layout.pageCollection().pageCount(),
        }

    @staticmethod
    def _style_label(label, color, font_size):
        """Set a layout label's colour and size through the current API.

        QGIS 4 deprecates setFontColor()/font()/setFont() on QgsLayoutItemLabel
        in favour of a QgsTextFormat, and each call logs a DeprecationWarning
        with a traceback into the user's QGIS log. setTextFormat() arrived in
        3.24, below the plugin's 3.28 minimum, but the old path is kept as a
        fallback rather than assuming: a missing method here would take out
        every layout label.
        """
        text_format = getattr(label, "textFormat", None)
        if text_format is not None and hasattr(label, "setTextFormat"):
            fmt = text_format()
            fmt.setColor(QColor(color))
            fmt.setSize(float(font_size))
            fmt.setSizeUnit(RENDER_UNIT_POINTS)
            label.setTextFormat(fmt)
            return
        label.setFontColor(QColor(color))
        font = label.font()
        font.setPointSize(int(font_size))
        label.setFont(font)

    @command
    def add_layout_label(
        self,
        layout_name,
        text,
        x=10,
        y=10,
        width=100,
        height=20,
        font_size=12,
        color="#000000",
        **kwargs,
    ):
        """Add a text label to a print layout. Supports [% expression %] in text."""
        from qgis.core import QgsLayoutItemLabel

        layout = self._get_layout(layout_name)
        label = QgsLayoutItemLabel(layout)
        label.setText(text)
        self._style_label(label, color, font_size)
        layout.addLayoutItem(label)
        label.attemptMove(QgsLayoutPoint(x, y))
        label.attemptResize(QgsLayoutSize(width, height))
        return {"ok": True, "uuid": label.uuid()}

    @command
    def add_layout_legend(
        self,
        layout_name,
        map_item_id=None,
        x=10,
        y=10,
        width=80,
        height=100,
        title="Legend",
        **kwargs,
    ):
        """Add a legend to a print layout, linked to a map item."""
        from qgis.core import QgsLayoutItemLegend

        layout = self._get_layout(layout_name)
        legend = QgsLayoutItemLegend(layout)
        legend.setTitle(title)
        map_item = self._find_layout_map(layout, map_item_id)
        if map_item:
            legend.setLinkedMap(map_item)
        layout.addLayoutItem(legend)
        legend.attemptMove(QgsLayoutPoint(x, y))
        legend.attemptResize(QgsLayoutSize(width, height))
        return {"ok": True, "uuid": legend.uuid()}

    @command
    def add_layout_scalebar(
        self,
        layout_name,
        map_item_id=None,
        x=10,
        y=180,
        width=80,
        height=20,
        style="Single Box",
        **kwargs,
    ):
        """Add a scale bar to a print layout, linked to a map item."""
        from qgis.core import QgsLayoutItemScaleBar

        layout = self._get_layout(layout_name)
        bar = QgsLayoutItemScaleBar(layout)
        bar.setStyle(style)
        map_item = self._find_layout_map(layout, map_item_id)
        if map_item:
            bar.setLinkedMap(map_item)
        bar.applyDefaultSize()
        layout.addLayoutItem(bar)
        bar.attemptMove(QgsLayoutPoint(x, y))
        return {"ok": True, "uuid": bar.uuid()}

    @command
    def add_layout_picture(self, layout_name, path, x=10, y=10, width=30, height=30, **kwargs):
        """Add a picture/SVG (logo, north arrow) to a print layout."""
        from qgis.core import QgsLayoutItemPicture

        layout = self._get_layout(layout_name)
        pic = QgsLayoutItemPicture(layout)
        pic.setPicturePath(path)
        layout.addLayoutItem(pic)
        pic.attemptMove(QgsLayoutPoint(x, y))
        pic.attemptResize(QgsLayoutSize(width, height))
        return {"ok": True, "uuid": pic.uuid()}

    @command
    def add_layout_table(
        self,
        layout_name,
        layer_id,
        x=10,
        y=10,
        width=180,
        height=80,
        max_rows=20,
        **kwargs,
    ):
        """Add an attribute table for a vector layer to a print layout."""
        from qgis.core import QgsLayoutFrame, QgsLayoutItemAttributeTable

        layer = self._get_vector_layer(layer_id)
        layout = self._get_layout(layout_name)
        table = QgsLayoutItemAttributeTable.create(layout)
        table.setVectorLayer(layer)
        table.setMaximumNumberOfFeatures(int(max_rows))
        layout.addMultiFrame(table)
        frame = QgsLayoutFrame(layout, table)
        frame.attemptMove(QgsLayoutPoint(x, y))
        frame.attemptResize(QgsLayoutSize(width, height))
        table.addFrame(frame)
        return {"ok": True, "uuid": frame.uuid()}

    @command
    def configure_atlas(
        self,
        layout_name,
        coverage_layer,
        enabled=True,
        page_name_expression=None,
        filter_expression=None,
        sort_expression=None,
        **kwargs,
    ):
        """Configure the atlas of a print layout (coverage layer, filter, sort)."""
        layer = self._get_vector_layer(coverage_layer)
        layout = self._get_layout(layout_name)
        atlas = layout.atlas()
        atlas.setEnabled(bool(enabled))
        atlas.setCoverageLayer(layer)
        if page_name_expression:
            atlas.setPageNameExpression(page_name_expression)
        if filter_expression:
            atlas.setFilterFeatures(True)
            atlas.setFilterExpression(filter_expression)
        if sort_expression:
            atlas.setSortFeatures(True)
            atlas.setSortExpression(sort_expression)
        atlas.updateFeatures()
        return {
            "ok": True,
            "coverage_layer": layer.name(),
            "enabled": bool(enabled),
            "count": atlas.count(),
        }

    @command
    def export_atlas(self, layout_name, output_path, format="pdf", dpi=300, **kwargs):
        """Export an atlas: single multi-page PDF, or one image file per feature."""

        layout = self._get_layout(layout_name)
        atlas = layout.atlas()
        if not atlas.enabled():
            raise CommandError("Atlas not enabled; call configure_atlas first")
        atlas.updateFeatures()
        fmt = format.lower()
        if fmt == "pdf":
            settings = QgsLayoutExporter.PdfExportSettings()
            settings.dpi = dpi
            result, error = QgsLayoutExporter.exportToPdf(atlas, output_path, settings)
        elif fmt in ("png", "jpg", "jpeg", "tif", "tiff"):
            os.makedirs(output_path, exist_ok=True)
            settings = QgsLayoutExporter.ImageExportSettings()
            settings.dpi = dpi
            base = os.path.join(output_path, layout_name)
            result, error = QgsLayoutExporter.exportToImage(atlas, base, fmt, settings)
        else:
            raise CommandError(f"Unsupported atlas format: {format}")
        if result != LAYOUT_SUCCESS:
            raise CommandError(f"Atlas export failed: {error}")
        return {"ok": True, "output": output_path, "count": atlas.count()}

    @command
    def remove_layout(self, layout_name, **kwargs):
        """Remove a print layout from the project."""
        layout = self._get_layout(layout_name)
        QgsProject.instance().layoutManager().removeLayout(layout)
        return {"ok": True, "removed": layout_name}
