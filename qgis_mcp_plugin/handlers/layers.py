"""Handlers that add, remove, inspect and configure map layers.

Layer *content* (features, fields, expressions) lives in ``features``; layer
*symbology* lives in ``style``.
"""

import fnmatch
import math
import os
from typing import ClassVar

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsMessageLog,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsVectorLayerJoinInfo,
)
from qgis.PyQt.QtGui import QColor

from ..compat import LAYER_RASTER, LAYER_VECTOR, MSG_INFO, MSG_WARNING, RASTER_STATS_ALL
from ..errors import CommandError
from ..registry import command
from ..wire import zip_strict


class LayerHandlers:
    """Adding, removing, inspecting and configuring map layers."""

    @command
    def add_vector_layer(self, path, name=None, provider="ogr", **kwargs):
        if not name:
            name = os.path.basename(path)

        layer = QgsVectorLayer(path, name, provider)
        if not layer.isValid():
            raise CommandError(f"Layer is not valid: {path}")

        QgsProject.instance().addMapLayer(layer)
        QgsMessageLog.logMessage(f"Vector layer added: {name}", self.LOG_TAG, MSG_INFO)

        return {
            "id": layer.id(),
            "name": layer.name(),
            "type": self._get_layer_type(layer),
            "feature_count": layer.featureCount(),
        }

    @command
    def add_raster_layer(self, path, name=None, provider="gdal", **kwargs):
        if not name:
            name = os.path.basename(path)

        layer = QgsRasterLayer(path, name, provider)
        if not layer.isValid():
            raise CommandError(f"Layer is not valid: {path}")

        QgsProject.instance().addMapLayer(layer)
        QgsMessageLog.logMessage(f"Raster layer added: {name}", self.LOG_TAG, MSG_INFO)

        return {
            "id": layer.id(),
            "name": layer.name(),
            "type": "raster",
            "width": layer.width(),
            "height": layer.height(),
        }

    @command
    def get_layers(self, limit=50, offset=0, **kwargs):
        project = QgsProject.instance()
        all_layers = list(project.mapLayers().items())
        total_count = len(all_layers)
        page_end = offset + limit
        page = all_layers[offset:page_end]

        layers = []
        for layer_id, layer in page:
            layer_info = {
                "id": layer_id,
                "name": layer.name(),
                "type": self._get_layer_type(layer),
                "visible": self._is_visible(project, layer_id),
            }

            if layer.type() == LAYER_VECTOR:
                layer_info.update(
                    {"feature_count": layer.featureCount(), "geometry_type": layer.geometryType()}
                )
            elif layer.type() == LAYER_RASTER:
                layer_info.update({"width": layer.width(), "height": layer.height()})

            layers.append(layer_info)

        return {"layers": layers, "total_count": total_count, "offset": offset, "limit": limit}

    @command
    def remove_layer(self, layer_id, **kwargs):
        layer_name = self._layer(layer_id).name()
        QgsProject.instance().removeMapLayer(layer_id)
        QgsMessageLog.logMessage(f"Layer removed: {layer_name}", self.LOG_TAG, MSG_INFO)
        return {"ok": True}

    @command
    def zoom_to_layer(self, layer_id, **kwargs):
        self.iface.setActiveLayer(self._layer(layer_id))
        self.iface.zoomToActiveLayer()
        return {"ok": True}

    @command
    def set_layer_visibility(self, layer_id, visible, **kwargs):
        self._layer(layer_id)  # existence check, standard "not found" message
        tree_layer = QgsProject.instance().layerTreeRoot().findLayer(layer_id)
        if tree_layer is None:
            raise CommandError(f"Layer not found in layer tree: {layer_id}")

        tree_layer.setItemVisibilityChecked(visible)
        # Phase 1B: Stripped layer_id, return only visible state
        return {"visible": visible}

    @command
    def get_raster_info(self, layer_id, **kwargs):
        layer = self._get_raster_layer(layer_id)

        dp = layer.dataProvider()
        extent = layer.extent()

        # Phase 1B: Stripped layer_id, name
        info = {
            "width": layer.width(),
            "height": layer.height(),
            "band_count": layer.bandCount(),
            "crs": layer.crs().authid(),
            "extent": {
                "xmin": extent.xMinimum(),
                "ymin": extent.yMinimum(),
                "xmax": extent.xMaximum(),
                "ymax": extent.yMaximum(),
            },
            "bands": [],
        }

        for band in range(1, layer.bandCount() + 1):
            band_info = {"band": band}
            try:
                stats = dp.bandStatistics(band, RASTER_STATS_ALL)
                band_info.update(
                    {
                        "min": stats.minimumValue,
                        "max": stats.maximumValue,
                        "mean": stats.mean,
                        "stdev": stats.stdDev,
                    }
                )
            except Exception as e:
                QgsMessageLog.logMessage(
                    f"Could not compute stats for band {band}: {e}", self.LOG_TAG, MSG_WARNING
                )
            nodata = dp.sourceNoDataValue(band)
            if nodata is not None:
                band_info["nodata"] = nodata
            info["bands"].append(band_info)

        return info

    @command
    def get_layer_info(self, layer_id, **kwargs):
        layer = self._layer(layer_id)
        extent = layer.extent()

        info = {
            "id": layer.id(),
            "name": layer.name(),
            "type": self._get_layer_type(layer),
            "crs": layer.crs().authid(),
            "extent": {
                "xmin": extent.xMinimum(),
                "ymin": extent.yMinimum(),
                "xmax": extent.xMaximum(),
                "ymax": extent.yMaximum(),
            },
            "source": layer.source(),
            "provider": layer.providerType(),
            "is_valid": layer.isValid(),
        }

        if layer.type() == LAYER_VECTOR:
            info["feature_count"] = layer.featureCount()
            info["geometry_type"] = layer.geometryType()
            info["fields"] = [
                {"name": f.name(), "type": f.typeName(), "length": f.length()}
                for f in layer.fields()
            ]
        elif layer.type() == LAYER_RASTER:
            info["width"] = layer.width()
            info["height"] = layer.height()
            info["band_count"] = layer.bandCount()

        return info

    @command
    def get_layer_schema(self, layer_id, **kwargs):
        layer = self._get_vector_layer(layer_id)

        # Phase 1B: Stripped layer_id, layer_name
        return {
            "geometry_type": layer.geometryType(),
            "crs": layer.crs().authid(),
            "fields": [
                {
                    "name": f.name(),
                    "type": f.typeName(),
                    "length": f.length(),
                    "precision": f.precision(),
                    "is_numeric": f.isNumeric(),
                }
                for f in layer.fields()
            ],
        }

    @command
    def find_layer(self, name_pattern, **kwargs):
        project = QgsProject.instance()
        matches = []
        pattern_lower = name_pattern.lower()
        for layer_id, layer in project.mapLayers().items():
            name_lower = layer.name().lower()
            if fnmatch.fnmatch(name_lower, pattern_lower) or pattern_lower in name_lower:
                matches.append(
                    {
                        "id": layer_id,
                        "name": layer.name(),
                        "type": self._get_layer_type(layer),
                    }
                )
        return {"layers": matches, "count": len(matches)}

    @command
    def create_memory_layer(self, name, geometry_type, crs="EPSG:4326", fields=None, **kwargs):
        field_parts = []
        if fields:
            for f in fields:
                field_parts.append(f"field={f['name']}:{f['type']}")

        uri = f"{geometry_type}?crs={crs}"
        if field_parts:
            uri += "&" + "&".join(field_parts)

        layer = QgsVectorLayer(uri, name, "memory")
        if not layer.isValid():
            raise CommandError(f"Failed to create memory layer: {uri}")

        QgsProject.instance().addMapLayer(layer)
        return {
            "id": layer.id(),
            "name": layer.name(),
            "type": self._get_layer_type(layer),
            "feature_count": 0,
        }

    @command
    def duplicate_layer(self, layer_id, new_name=None, **kwargs):
        """Duplicate a layer (with its style) under a new name."""
        project = QgsProject.instance()
        layer = self._layer(layer_id)
        clone = layer.clone()
        clone.setName(new_name or f"{layer.name()} copy")
        project.addMapLayer(clone)
        return {"ok": True, "output_layer_id": clone.id(), "name": clone.name()}

    @command
    def set_layer_order(self, layer_ids, **kwargs):
        """Reorder layer tree nodes (top to bottom); tree order is draw order.

        Listed layers are rearranged into the given order within their common
        parent; unlisted siblings keep their slots. Deliberately does NOT use
        QGIS's custom draw order - that freezes a snapshot list, so any layer
        added afterwards would silently draw behind everything; any existing
        custom order is cleared for the same reason.
        """
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        nodes = []
        for lid in layer_ids:
            node = root.findLayer(lid)
            if node is None:
                raise CommandError(f"Layer not found in layer tree: {lid}")
            nodes.append(node)
        parent = nodes[0].parent()
        for node in nodes[1:]:
            if node.parent() is not parent:
                raise CommandError("All layers must be in the same group to reorder")

        siblings = list(parent.children())
        slots = sorted(siblings.index(n) for n in nodes)
        new_order = list(siblings)
        for slot, node in zip_strict(slots, nodes):
            new_order[slot] = node
        clones = [n.clone() for n in new_order]
        parent.insertChildNodes(0, clones)
        for node in siblings:
            parent.removeChildNode(node)

        root.setHasCustomLayerOrder(False)
        return {"ok": True, "order": layer_ids}

    def _layer_tree_node(self, node):
        """Recursively build a dict for a layer tree node."""
        if isinstance(node, QgsLayerTreeGroup):
            children = [self._layer_tree_node(c) for c in node.children()]
            result = {
                "type": "group",
                "name": node.name(),
                "visible": node.isVisible(),
                "children": children,
            }
            return result
        elif isinstance(node, QgsLayerTreeLayer):
            layer = node.layer()
            result = {
                "type": "layer",
                "name": node.name(),
                "visible": node.isVisible(),
            }
            if layer:
                result["layer_id"] = layer.id()
                result["layer_type"] = self._get_layer_type(layer)
            return result
        return {"type": "unknown", "name": str(node)}

    @command
    def get_layer_tree(self, **kwargs):
        root = QgsProject.instance().layerTreeRoot()
        children = [self._layer_tree_node(c) for c in root.children()]
        return {"children": children}

    @command
    def create_layer_group(self, name, parent=None, **kwargs):
        root = QgsProject.instance().layerTreeRoot()
        if parent:
            target = root.findGroup(parent)
            if target is None:
                raise CommandError(f"Parent group not found: {parent}")
        else:
            target = root
        target.addGroup(name)
        return {"name": name, "ok": True}

    @command
    def move_layer_to_group(self, layer_id, group_name, **kwargs):
        project = QgsProject.instance()
        root = project.layerTreeRoot()

        node = root.findLayer(layer_id)
        if node is None:
            raise CommandError(f"Layer not found in tree: {layer_id}")

        target = root.findGroup(group_name)
        if target is None:
            raise CommandError(f"Group not found: {group_name}")

        clone = node.clone()
        target.addChildNode(clone)
        node.parent().removeChildNode(node)
        return {"ok": True}

    # property name -> (coercion, QgsMapLayer setter). The setter is named
    # rather than bound because the layer only exists per call; the "Supported:"
    # list in the error is now generated from the table, so it cannot go stale.
    _LAYER_PROPERTIES: ClassVar[dict] = {
        "opacity": (float, "setOpacity"),
        "name": (str, "setName"),
        "scale_visibility": (bool, "setScaleBasedVisibility"),
        "min_scale": (float, "setMinimumScale"),
        "max_scale": (float, "setMaximumScale"),
    }

    @command
    def set_layer_property(self, layer_id, property, value, **kwargs):
        layer = self._layer(layer_id)
        coerce, setter = self._pick(self._LAYER_PROPERTIES, property, "property")
        getattr(layer, setter)(coerce(value))

        self.iface.mapCanvas().refresh()
        return {"ok": True, "property": property, "value": value}

    @command
    def get_layer_extent(self, layer_id, **kwargs):
        layer = self._layer(layer_id)
        extent = layer.extent()
        # A layer with no features has a null extent whose bounds are NaN;
        # report it explicitly instead of leaking NaN to the client. Test the
        # bounds themselves - QgsRectangle.isEmpty() is also true for the
        # zero-area extent of a single-point layer, which is a real extent.
        bounds = (extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum())
        if extent.isNull() or not all(math.isfinite(b) for b in bounds):
            return {
                "xmin": None,
                "ymin": None,
                "xmax": None,
                "ymax": None,
                "crs": layer.crs().authid(),
                "empty": True,
            }
        return {
            "xmin": extent.xMinimum(),
            "ymin": extent.yMinimum(),
            "xmax": extent.xMaximum(),
            "ymax": extent.yMaximum(),
            "crs": layer.crs().authid(),
        }

    @command
    def get_active_layer(self, **kwargs):
        """Get the currently active (selected) layer in the layer panel."""
        layer = self.iface.activeLayer()
        if not layer:
            return {"active": False, "layer_id": None, "name": None, "type": None}
        return {
            "active": True,
            "layer_id": layer.id(),
            "name": layer.name(),
            "type": self._get_layer_type(layer),
        }

    @command
    def set_active_layer(self, layer_id, **kwargs):
        """Set the active layer by ID."""
        layer = self._layer(layer_id)
        self.iface.setActiveLayer(layer)
        return {"ok": True, "layer_id": layer_id, "name": layer.name()}

    @command
    def get_layer_labeling(self, layer_id, **kwargs):
        """Get labeling configuration for a vector layer."""
        layer = self._get_vector_layer(layer_id)
        result = {
            "layer_id": layer_id,
            "enabled": layer.labelsEnabled(),
        }
        labeling = layer.labeling()
        if labeling:
            settings = labeling.settings()
            result["field_name"] = settings.fieldName
            result["is_expression"] = settings.isExpression
            result["font_size"] = settings.format().size()
            result["color"] = settings.format().color().name()
            result["placement"] = str(settings.placement)
        return result

    @command
    def set_layer_labeling(
        self, layer_id, enabled=True, field_name=None, font_size=None, color=None, **kwargs
    ):
        """Configure labeling for a vector layer."""
        from qgis.core import QgsPalLayerSettings, QgsTextFormat, QgsVectorLayerSimpleLabeling

        layer = self._get_vector_layer(layer_id)

        if not enabled:
            layer.setLabelsEnabled(False)
            layer.triggerRepaint()
            return {"ok": True, "layer_id": layer_id, "enabled": False}

        settings = QgsPalLayerSettings()
        if field_name:
            settings.fieldName = field_name
            settings.isExpression = False

        text_format = QgsTextFormat()
        if font_size:
            text_format.setSize(font_size)
        if color:
            text_format.setColor(QColor(color))
        settings.setFormat(text_format)

        labeling = QgsVectorLayerSimpleLabeling(settings)
        layer.setLabeling(labeling)
        layer.setLabelsEnabled(True)
        layer.triggerRepaint()
        return {"ok": True, "layer_id": layer_id, "enabled": True, "field_name": field_name}

    @command
    def get_layer_crs(self, layer_id, **kwargs):
        """Get the CRS of a layer."""
        layer = self._layer(layer_id)
        crs = layer.crs()
        return {
            "layer_id": layer_id,
            "authid": crs.authid(),
            "description": crs.description(),
            "is_geographic": crs.isGeographic(),
            # toProj4() is deprecated in QGIS 4 (toProj() since 3.10); keep the
            # key name, which is what callers read.
            "proj4": crs.toProj() if hasattr(crs, "toProj") else crs.toProj4(),
        }

    @command
    def set_layer_crs(self, layer_id, crs, **kwargs):
        """Set the CRS of a layer."""
        layer = self._layer(layer_id)
        new_crs = QgsCoordinateReferenceSystem(crs)
        if not new_crs.isValid():
            raise CommandError(f"Invalid CRS: {crs}")
        layer.setCrs(new_crs)
        return {"ok": True, "layer_id": layer_id, "crs": new_crs.authid()}

    # service -> (layer class, provider key, default name, uri prefix). The
    # prefix is concatenated rather than str.format()ed: a URL may legitimately
    # contain braces, which format() would try to interpret.
    _WEB_SERVICES: ClassVar[dict] = {
        "xyz": (QgsRasterLayer, "wms", "XYZ Layer", "type=xyz&url="),
        "wms": (QgsRasterLayer, "wms", "WMS Layer", ""),
        "wfs": (QgsVectorLayer, "WFS", "WFS Layer", ""),
    }

    # How a service takes a CRS in its provider uri. A service missing from this
    # table has no say in it: XYZ is a Web Mercator tile scheme and the wms
    # provider ignores `crs=` for `type=xyz` (verified on QGIS 4.0 - the layer
    # comes back EPSG:3857 whatever you ask for), which is exactly why this
    # parameter used to be accepted and then silently dropped.
    _WEB_CRS_URI: ClassVar[dict] = {
        "wms": "crs={crs}&",
        "wfs": "srsname='{crs}' ",
    }
    _WEB_FIXED_CRS = "EPSG:3857"

    def _web_layer_uri(self, service, url, crs, prefix):
        """Build the provider uri, applying *crs* where the service accepts one."""
        uri = prefix + url
        if not crs:
            return uri
        if not QgsCoordinateReferenceSystem(crs).isValid():
            raise CommandError(f"Invalid CRS: {crs}")
        template = self._WEB_CRS_URI.get(service)
        if template is None:
            # Refuse rather than hand back a layer that is not in the requested
            # CRS: silently ignoring the argument is the bug being fixed here.
            if crs.upper() != self._WEB_FIXED_CRS:
                raise CommandError(
                    f"{service} tile layers are always {self._WEB_FIXED_CRS} (Web Mercator "
                    f"tile scheme), so crs={crs!r} cannot be honoured. Omit crs, or reproject "
                    "the map instead by setting the project CRS."
                )
            return uri
        if f"{template.split('=')[0]}=" in url:
            return uri  # the caller's own uri already specifies it
        return template.format(crs=crs) + uri

    @command
    def add_web_layer(self, url, service, name=None, crs=None, **kwargs):
        """Add a web layer (XYZ, WMS, WFS) to the project.

        *crs* defaults to unset, and only a CRS the caller actually asks for is
        written into the uri. Injecting one by default would override whatever a
        WMS serves natively - which is why the old ``EPSG:3857`` default could
        not have been applied even in principle.
        """
        service = service.lower()
        layer_class, provider, default_name, prefix = self._pick(
            self._WEB_SERVICES, service, "web service"
        )
        uri = self._web_layer_uri(service, url, crs, prefix)
        layer = layer_class(uri, name or default_name, provider)

        if not layer.isValid():
            raise CommandError(f"Layer is not valid: {url}")

        QgsProject.instance().addMapLayer(layer)
        # Report the CRS the layer actually got, so a caller can see what the
        # service agreed to rather than assuming the requested value stuck.
        return {
            "id": layer.id(),
            "name": layer.name(),
            "type": self._get_layer_type(layer),
            "crs": layer.crs().authid(),
        }

    @command
    def add_table_join(
        self, target_layer_id, join_layer_id, target_field, join_field, prefix="", **kwargs
    ):
        """Add a table join to a vector layer."""
        target_layer = self._get_vector_layer(target_layer_id)
        join_layer = self._get_vector_layer(join_layer_id)

        join_info = QgsVectorLayerJoinInfo()
        join_info.setTargetFieldName(target_field)
        join_info.setJoinLayerId(join_layer.id())
        join_info.setJoinFieldName(join_field)
        join_info.setUsingMemoryCache(True)
        if prefix:
            join_info.setPrefix(prefix)

        if target_layer.addJoin(join_info):
            return {"ok": True}
        else:
            raise CommandError("Failed to add table join")

    @command
    def apply_style_qml(self, layer_id, path, **kwargs):
        """Apply a QML style to a layer."""
        layer = self._layer(layer_id)

        message, success = layer.loadNamedStyle(path)
        if success:
            layer.triggerRepaint()
            self.iface.layerTreeView().refreshLayerSymbology(layer.id())
            return {"ok": True, "message": message}
        else:
            raise CommandError(f"Failed to apply style: {message}")

    @command
    def save_style_qml(self, layer_id, path, **kwargs):
        """Save a layer's style to a QML file."""
        layer = self._layer(layer_id)

        message, success = layer.saveNamedStyle(path)
        if success:
            return {"ok": True, "path": path}
        else:
            raise CommandError(f"Failed to save style: {message}")

    @command
    def export_layer(
        self, layer_id, output_path, target_crs=None, filter_expression=None, **kwargs
    ):
        """Export a vector/raster layer to disk. target_crs reprojects; format by extension."""
        layer = self._layer(layer_id)

        if layer.type() == LAYER_VECTOR:
            src = layer
            if filter_expression:
                r = self._run_alg(
                    "native:extractbyexpression",
                    {"INPUT": layer, "EXPRESSION": filter_expression, "OUTPUT": "memory:"},
                )
                src = r["OUTPUT"]
            if target_crs:
                self._run_alg(
                    "native:reprojectlayer",
                    {"INPUT": src, "TARGET_CRS": target_crs, "OUTPUT": output_path},
                )
            else:
                self._run_alg("native:savefeatures", {"INPUT": src, "OUTPUT": output_path})
            return {"ok": True, "output": output_path}

        if layer.type() == LAYER_RASTER:
            if target_crs:
                self._run_alg(
                    "gdal:warpreproject",
                    {"INPUT": layer, "TARGET_CRS": target_crs, "OUTPUT": output_path},
                )
            else:
                self._run_alg("gdal:translate", {"INPUT": layer, "OUTPUT": output_path})
            return {"ok": True, "output": output_path}

        raise CommandError(f"Unsupported layer type for export: {layer_id}")
