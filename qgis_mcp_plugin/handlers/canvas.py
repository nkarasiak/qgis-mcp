"""Handlers for the map canvas: extent, scale, rendering and screenshots."""

import base64
import contextlib
import os
import secrets
import tempfile

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsLayoutExporter,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsPointXY,
    QgsPrintLayout,
    QgsProject,
    QgsRectangle,
    QgsVectorSimplifyMethod,
)
from qgis.PyQt.QtCore import QBuffer, QByteArray, QEventLoop, QSize, QTimer
from qgis.PyQt.QtGui import QColor, QImage

from ..compat import IODEVICE_WRITEONLY, LAYOUT_SUCCESS, SIMPLIFY_ANTIALIAS, SIMPLIFY_GEOMETRY
from ..errors import CommandError
from ..registry import command


class CanvasHandlers:
    """Map canvas extent, scale, rendering and screenshots."""

    @command
    def get_canvas_extent(self, **kwargs):
        canvas = self.iface.mapCanvas()
        extent = canvas.extent()
        crs = canvas.mapSettings().destinationCrs()
        return {
            "xmin": extent.xMinimum(),
            "ymin": extent.yMinimum(),
            "xmax": extent.xMaximum(),
            "ymax": extent.yMaximum(),
            "crs": crs.authid(),
            "width": canvas.width(),
            "height": canvas.height(),
        }

    @command
    def set_canvas_extent(self, xmin, ymin, xmax, ymax, crs=None, **kwargs):
        canvas = self.iface.mapCanvas()
        rect = QgsRectangle(xmin, ymin, xmax, ymax)

        if crs:
            src_crs = QgsCoordinateReferenceSystem(crs)
            dst_crs = canvas.mapSettings().destinationCrs()
            if src_crs != dst_crs:
                transform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
                rect = transform.transformBoundingBox(rect)

        canvas.setExtent(rect)
        canvas.refresh()
        return {"extent": [rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()]}

    _RENDER_TIMEOUT = 55  # seconds (below MCP's 60s TIMEOUT_LONG)

    @command
    def render_map_base64(self, width=800, height=600, path=None, **kwargs):
        """Render the map and return base64-encoded PNG data."""
        try:
            canvas = self.iface.mapCanvas()
            # Clone the canvas settings so the render reproduces what the user
            # sees: visible layers only, canvas/custom draw order, destination
            # CRS, transform context, labeling, style overrides and temporal
            # state. Rebuilding from the project registry
            # (QgsProject.mapLayers()) renders hidden layers in registry order
            # and can place an opaque hidden layer over the overlays (issue #21).
            src = canvas.mapSettings()
            try:
                ms = QgsMapSettings(src)  # copy constructor (preferred)
            except Exception:
                # Fallback if the copy constructor is unavailable on this QGIS:
                # at minimum honour visibility, draw order, CRS and transform.
                ms = QgsMapSettings()
                ms.setLayers(src.layers())
                ms.setDestinationCrs(src.destinationCrs())
                ms.setTransformContext(QgsProject.instance().transformContext())
            ms.setExtent(canvas.extent())
            ms.setOutputSize(QSize(width, height))
            ms.setBackgroundColor(QColor(255, 255, 255))
            ms.setOutputDpi(96)

            # Enable geometry simplification (matches QGIS canvas defaults).
            # Skips sub-pixel vertices - critical for large datasets at small scales.
            simplify = QgsVectorSimplifyMethod()
            simplify.setSimplifyHints(SIMPLIFY_GEOMETRY | SIMPLIFY_ANTIALIAS)
            simplify.setThreshold(1.0)  # 1 pixel
            simplify.setForceLocalOptimization(True)
            ms.setSimplifyMethod(simplify)

            render = QgsMapRendererParallelJob(ms)

            # Use QEventLoop + QTimer for non-blocking wait with timeout.
            # Keeps Qt event loop alive and allows cancellation.
            loop = QEventLoop()
            timed_out = []
            render.finished.connect(loop.quit)

            timeout_timer = QTimer()
            timeout_timer.setSingleShot(True)
            timeout_timer.timeout.connect(lambda: (timed_out.append(True), loop.quit()))
            timeout_timer.start(self._RENDER_TIMEOUT * 1000)

            render.start()
            loop.exec()

            timeout_timer.stop()
            if timed_out:
                render.cancelWithoutBlocking()
                render.waitForFinished()
                raise CommandError(f"Render timed out after {self._RENDER_TIMEOUT}s")

            img = render.renderedImage()

            if path and not img.save(path):
                raise CommandError(f"Failed to write image to {path}")

            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(IODEVICE_WRITEONLY)
            if not img.save(buf, "PNG"):
                raise CommandError("Failed to encode the rendered map as PNG")
            buf.close()
            b64 = base64.b64encode(bytes(ba)).decode("utf-8")

            response = {
                "base64_data": b64,
                "mime_type": "image/png",
                "width": width,
                "height": height,
            }
            # A layer that failed to draw (broken WMS, moved file) leaves a blank
            # area in an image that otherwise looks like a successful render.
            errors = [f"{e.layerID}: {e.message}" for e in render.errors()]
            if errors:
                response["warnings"] = errors
            return response

        except CommandError:
            raise  # the render timeout above - don't re-wrap a deliberate message
        except Exception as e:
            raise CommandError(f"Render error: {e!s}") from e

    @command
    def get_canvas_screenshot(self, **kwargs):
        """Grab the current map canvas as a fast screenshot (no re-render)."""
        canvas = self.iface.mapCanvas()
        pixmap = canvas.grab()
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(IODEVICE_WRITEONLY)
        if not pixmap.save(buf, "PNG"):
            raise CommandError("Failed to encode the canvas grab as PNG")
        buf.close()
        b64 = base64.b64encode(ba.data()).decode("ascii")
        return {
            "base64_data": b64,
            "mime_type": "image/png",
            "width": pixmap.width(),
            "height": pixmap.height(),
        }

    @command
    def get_3d_screenshot(
        self, view_index=0, dpi=96, pitch=None, distance=None, heading=None, **kwargs
    ):
        """Capture an open 3D Map View as a PNG image.

        The 3D map view is an OpenGL ``QWindow`` that ``QWidget.grab()`` cannot
        read, so this reuses the open view's scene settings + camera pose and
        renders them through a print layout's 3D map item (whose offscreen 3D
        engine runs in C++). Requires a 3D map view to be open
        (View > 3D Map Views > New 3D Map View).

        Optional ``pitch`` (0 = straight down / top-down, 90 = horizontal /
        edge-on; ~45 is a balanced oblique), ``heading`` (compass degrees), and
        ``distance`` (metres) override the captured camera angle without changing
        the live view.
        """
        view_index = int(view_index)
        dpi = max(10, min(int(dpi), 600))  # clamp: bound render cost / memory use
        try:
            from qgis._3d import Qgs3DMapSettings, QgsLayoutItem3DMap
        except ImportError as e:
            raise CommandError(
                f"3D support is unavailable in this QGIS build (qgis._3d import failed: {e})"
            ) from e

        views = self.iface.mapCanvases3D()
        if not views:
            raise CommandError(
                "No 3D map view is open. Open one via "
                "View > 3D Map Views > New 3D Map View, frame your scene, then retry."
            )
        if view_index < 0 or view_index >= len(views):
            raise CommandError(
                f"view_index {view_index} out of range (open 3D views: {len(views)})"
            )
        canvas3d = views[view_index]

        # Clone the scene settings (copy constructor) so the live view is never
        # mutated or shared, and snapshot its current camera pose.
        map_settings = Qgs3DMapSettings(canvas3d.mapSettings())
        pose = canvas3d.cameraController().cameraPose()

        # Optional camera overrides - applied only to this captured copy, so the
        # live 3D view is left untouched.
        if pitch is not None:
            pose.setPitchAngle(max(0.0, min(float(pitch), 90.0)))
        if heading is not None:
            pose.setHeadingAngle(float(heading) % 360.0)
        if distance is not None and float(distance) > 0:
            pose.setDistanceFromCenterPoint(float(distance))

        # Match the output image to the 3D view's aspect ratio.
        size = canvas3d.size()
        view_w = max(1, size.width())
        view_h = max(1, size.height())
        page_w = 200.0
        page_h = page_w * view_h / view_w

        layout = QgsPrintLayout(QgsProject.instance())
        layout.initializeDefaults()
        layout.pageCollection().page(0).setPageSize(QgsLayoutSize(page_w, page_h))

        item = QgsLayoutItem3DMap(layout)
        item.attemptMove(QgsLayoutPoint(0, 0))
        item.attemptResize(QgsLayoutSize(page_w, page_h))
        item.setMapSettings(map_settings)
        item.setCameraPose(pose)
        layout.addLayoutItem(item)

        tmp = os.path.join(tempfile.gettempdir(), f"mcp_3d_{secrets.token_hex(6)}.png")
        try:
            exporter = QgsLayoutExporter(layout)
            export_settings = QgsLayoutExporter.ImageExportSettings()
            export_settings.dpi = dpi
            result = exporter.exportToImage(tmp, export_settings)
            if int(result) != int(LAYOUT_SUCCESS) or not os.path.exists(tmp):
                reason = exporter.errorMessage()
                detail = f": {reason}" if reason else ""
                raise CommandError(f"3D layout export failed (export code {int(result)}){detail}")
            with open(tmp, "rb") as fh:
                data = fh.read()
        finally:
            with contextlib.suppress(Exception):
                os.remove(tmp)

        img = QImage()
        img.loadFromData(data, "PNG")
        return {
            "base64_data": base64.b64encode(data).decode("ascii"),
            "mime_type": "image/png",
            "width": img.width(),
            "height": img.height(),
            "view_index": view_index,
            "open_3d_views": len(views),
        }

    @command
    def transform_coordinates(
        self, source_crs, target_crs, point=None, points=None, bbox=None, **kwargs
    ):
        """Transform coordinates between coordinate reference systems."""
        src = QgsCoordinateReferenceSystem(source_crs)
        dst = QgsCoordinateReferenceSystem(target_crs)
        if not src.isValid():
            raise CommandError(f"Invalid source CRS: {source_crs}")
        if not dst.isValid():
            raise CommandError(f"Invalid target CRS: {target_crs}")

        xform = QgsCoordinateTransform(src, dst, QgsProject.instance())
        result = {"source_crs": source_crs, "target_crs": target_crs}

        if point:
            pt = xform.transform(QgsPointXY(point["x"], point["y"]))
            result["point"] = {"x": pt.x(), "y": pt.y()}

        if points:
            transformed = []
            for p in points:
                pt = xform.transform(QgsPointXY(p["x"], p["y"]))
                transformed.append({"x": pt.x(), "y": pt.y()})
            result["points"] = transformed

        if bbox:
            rect = QgsRectangle(bbox["xmin"], bbox["ymin"], bbox["xmax"], bbox["ymax"])
            transformed_rect = xform.transformBoundingBox(rect)
            result["bbox"] = {
                "xmin": transformed_rect.xMinimum(),
                "ymin": transformed_rect.yMinimum(),
                "xmax": transformed_rect.xMaximum(),
                "ymax": transformed_rect.yMaximum(),
            }

        return result

    @command
    def get_canvas_scale(self, **kwargs):
        """Get map canvas scale, rotation, and magnification."""
        canvas = self.iface.mapCanvas()
        return {
            "scale": canvas.scale(),
            "rotation": canvas.rotation(),
            "magnification": canvas.magnificationFactor(),
        }

    @command
    def set_canvas_scale(self, scale=None, rotation=None, **kwargs):
        """Set map canvas scale and/or rotation."""
        canvas = self.iface.mapCanvas()
        if scale is not None:
            canvas.zoomScale(scale)
        if rotation is not None:
            canvas.setRotation(rotation)
        canvas.refresh()
        return {
            "ok": True,
            "scale": canvas.scale(),
            "rotation": canvas.rotation(),
        }
