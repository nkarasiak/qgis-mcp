"""Handlers for feature and attribute content of vector layers.

Includes the edit-session commands. While a session is open the feature writes
go through the layer's edit buffer (undoable, discarded by rollback); with no
session they go straight to the data provider. Each write reports which path it
took via ``buffered``.
"""

import contextlib
import re

from qgis.core import (
    QgsCoordinateTransform,
    QgsCsException,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsExpressionNodeFunction,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..compat import (
    AGG_ARRAY,
    AGG_COUNT,
    AGG_MAX,
    AGG_MEAN,
    AGG_MIN,
    AGG_STDEV,
    AGG_SUM,
    GEOM_LINE,
    GEOM_POLYGON,
    GEOM_UNKNOWN,
    LAYER_VECTOR,
    QVAR_BOOL,
    QVAR_DATE,
    QVAR_DATETIME,
    QVAR_DOUBLE,
    QVAR_INT,
    QVAR_STRING,
    WKB_NO_GEOMETRY,
)
from ..errors import CommandError
from ..registry import command

# Child accessors of each QgsExpressionNode kind; PyQGIS has no findNodes().
_SINGLE_CHILDREN = (
    "opLeft",
    "opRight",
    "operand",
    "node",
    "lowerBound",
    "higherBound",
    "container",
    "index",
    "elseExp",
)


def _function_calls(node):
    """Every function node in the expression tree under *node*."""
    if isinstance(node, QgsExpressionNodeFunction):
        yield node
    kids = [getattr(node, name)() for name in _SINGLE_CHILDREN if hasattr(node, name)]
    for name in ("args", "list"):
        node_list = getattr(node, name)() if hasattr(node, name) else None
        if node_list is not None:
            kids.extend(node_list.list())
    for when_then in node.conditions() if hasattr(node, "conditions") else ():
        kids.extend((when_then.whenExp(), when_then.thenExp()))
    for kid in kids:
        if kid is not None:
            yield from _function_calls(kid)


def _measures_geometry(expression):
    """Whether *expression* calls area(), perimeter() or length() on a geometry.

    length() is also the string length: length("name") counts characters, so
    it only counts when its argument involves a geometry.
    """
    # The expression owns its node tree: keep it alive for the walk, or the
    # nodes are freed under it (a native crash on QGIS 4).
    parsed = QgsExpression(expression)
    root = parsed.rootNode()
    if root is None:
        return False
    functions = QgsExpression.Functions()
    for call in _function_calls(root):
        name = functions[call.fnIndex()].name()
        if name in ("area", "perimeter"):
            return True
        if name == "length":
            args = call.args().list() if call.args() is not None else []
            if args and args[0].needsGeometry():
                return True
    return False


class FeatureHandlers:
    """Feature and attribute content of vector layers, including edit sessions."""

    @command
    def get_layer_features(
        self, layer_id, limit=10, offset=0, expression=None, include_geometry=False, **kwargs
    ):
        layer = self._get_vector_layer(layer_id)

        field_names = [field.name() for field in layer.fields()]
        feature_count = layer.featureCount()

        request = QgsFeatureRequest()
        matched = feature_count
        if expression:
            self._check_filter_expression(layer, expression)
            request.setFilterExpression(expression)
            # featureCount() is the whole layer. Report what the expression
            # selects too, since that is what limit and offset page through.
            counter = QgsFeatureRequest().setFilterExpression(expression)
            counter.setNoAttributes()
            matched = sum(1 for _ in layer.getFeatures(counter))

        # Decimals for point WKT: 3 is a millimetre in metres but ~55 m in
        # degrees, so geographic coordinates keep 7 (~1 cm).
        precision = 7 if layer.crs().isGeographic() else 3
        features = []
        skipped = 0
        for feature in layer.getFeatures(request):
            if skipped < offset:
                skipped += 1
                continue
            if len(features) >= limit:
                break

            # Phase 1C: Flatten to {"_fid": id, ...attrs} instead of nested "attributes"
            feature_obj = {"_fid": feature.id()}
            for field in layer.fields():
                feature_obj[field.name()] = self._convert_attribute(feature.attribute(field.name()))

            if include_geometry and feature.hasGeometry():
                geom = feature.geometry()
                geom_type = geom.type()

                wkb_type_name = QgsWkbTypes.displayString(geom.wkbType())

                if geom_type in [GEOM_POLYGON, GEOM_LINE]:
                    # The real vertex count: simplify(0.001) was in layer units,
                    # ~100 m in degrees and 1 mm in metres, so not comparable.
                    points_count = geom.constGet().nCoordinates()
                    geom_obj = {
                        "type": geom_type,
                        "wkb_type": wkb_type_name,
                        "wkt_summary": f"{wkb_type_name} with {points_count} points",
                        "bbox": [
                            geom.boundingBox().xMinimum(),
                            geom.boundingBox().yMinimum(),
                            geom.boundingBox().xMaximum(),
                            geom.boundingBox().yMaximum(),
                        ],
                    }
                else:
                    geom_obj = {
                        "type": geom_type,
                        "wkb_type": wkb_type_name,
                        "wkt": geom.asWkt(precision=precision),
                    }

                feature_obj["_geometry"] = geom_obj

            features.append(feature_obj)

        # Phase 1B: Stripped layer_id, layer_name, geometry_included
        return {
            # Features in the layer, whether or not the expression selects them.
            "feature_count": feature_count,
            # Features the expression selects (the layer total when there is none).
            "matched": matched,
            "fields": field_names,
            "features": features,
            # What coordinates in _geometry are in.
            "crs": layer.crs().authid(),
        }

    @command
    def get_field_statistics(self, layer_id, field_name, **kwargs):
        layer = self._get_vector_layer(layer_id)

        field_idx = layer.fields().indexOf(field_name)
        if field_idx < 0:
            raise CommandError(f"Field not found: {field_name}")

        field = layer.fields().at(field_idx)
        is_numeric = field.isNumeric()

        # Phase 1B: Stripped layer_id, field_name
        stats = {"is_numeric": is_numeric}

        if is_numeric:
            for stat_name, stat_enum in [
                ("count", AGG_COUNT),
                ("sum", AGG_SUM),
                ("mean", AGG_MEAN),
                ("min", AGG_MIN),
                ("max", AGG_MAX),
                ("stdev", AGG_STDEV),
            ]:
                val, ok = layer.aggregate(stat_enum, field_name)
                if ok:
                    stats[stat_name] = val
        else:
            count_val, ok = layer.aggregate(AGG_COUNT, field_name)
            if ok:
                stats["count"] = count_val
            distinct_val, ok = layer.aggregate(AGG_ARRAY, field_name)
            if ok and isinstance(distinct_val, list):
                # Sorted before the slice: a set has no order, so the same call
                # returned a different 50 values each time.
                unique = sorted(set(str(v) for v in distinct_val if v is not None))
                stats["distinct_count"] = len(unique)
                stats["distinct_values"] = unique[:50]

        return stats

    @command
    def add_features(self, layer_id, features, crs=None, **kwargs):
        """Add features; geometry_wkt is in *crs* when given, else the layer CRS."""
        layer = self._get_vector_layer(layer_id)
        dp = layer.dataProvider()
        to_layer = None
        if crs:
            src = self._parse_crs(crs)
            if src != layer.crs():
                to_layer = QgsCoordinateTransform(src, layer.crs(), QgsProject.instance())
        warnings = []
        qgs_features = []
        for i, feat_data in enumerate(features):
            unknown = sorted(set(feat_data) - {"attributes", "geometry_wkt"})
            if unknown:
                raise CommandError(
                    f"Feature {i}: unknown key(s) {unknown} - expected "
                    "'attributes' and/or 'geometry_wkt'"
                )
            f = QgsFeature(layer.fields())
            attrs = feat_data.get("attributes", {})
            for field_name, value in attrs.items():
                idx = layer.fields().indexOf(field_name)
                if idx < 0:
                    names = [fld.name() for fld in layer.fields()]
                    raise CommandError(
                        f"Feature {i}: no field '{field_name}' in layer (fields: {names})"
                    )
                f.setAttribute(idx, value)
            wkt = feat_data.get("geometry_wkt")
            if wkt:
                geom = QgsGeometry.fromWkt(wkt)
                if geom.isNull():
                    raise CommandError(f"Feature {i}: invalid geometry_wkt: {wkt!r}")
                warnings.extend(self._geometry_warnings(i, geom, layer))
                if to_layer is not None:
                    geom.transform(to_layer)
                f.setGeometry(geom)
            qgs_features.append(f)

        # An open edit session owns the layer: writing straight to the provider
        # would land underneath the buffer and be lost on rollback.
        if layer.isEditable():
            if not layer.addFeatures(qgs_features):
                raise CommandError("Failed to add features to the edit buffer")
            count = len(qgs_features)
        else:
            dp.clearErrors()
            ok, added = dp.addFeatures(qgs_features)
            if not ok:
                raise CommandError(f"Failed to add features{self._provider_error(dp)}")
            count = len(added)
        layer.updateExtents()
        response = {"added": count, "buffered": layer.isEditable()}
        if warnings:
            response["warnings"] = warnings
        return response

    @staticmethod
    def _geometry_warnings(i, geom, layer):
        """Refuse a geometry type the layer cannot hold; list what else is off.

        The WKT used to be stored as given: a bowtie polygon (whose area is then
        0) or 2D coordinates on a Z layer went in without a word.
        """
        # A generic GEOMETRY column (or GeometryCollection layer) holds any type.
        if layer.geometryType() not in (GEOM_UNKNOWN, geom.type()):
            raise CommandError(
                f"Feature {i}: {QgsWkbTypes.geometryDisplayString(geom.type())} geometry "
                f"on a {QgsWkbTypes.geometryDisplayString(layer.geometryType())} layer"
            )
        warnings = []
        if not geom.isGeosValid():
            warnings.append(
                f"Feature {i}: invalid geometry (e.g. self-intersecting); areas and "
                "overlays computed on it are unreliable"
            )
        if QgsWkbTypes.hasZ(layer.wkbType()) and not QgsWkbTypes.hasZ(geom.wkbType()):
            warnings.append(f"Feature {i}: 2D geometry on a layer with Z; Z is not set")
        return warnings

    @command
    def update_features(self, layer_id, updates, **kwargs):
        layer = self._get_vector_layer(layer_id)
        dp = layer.dataProvider()
        attr_map = {}
        for i, upd in enumerate(updates):
            unknown = sorted(set(upd) - {"fid", "attributes"})
            if unknown:
                raise CommandError(
                    f"Update {i}: unknown key(s) {unknown} - expected 'fid' and 'attributes'"
                )
            if "fid" not in upd:
                raise CommandError(f"Update {i}: missing 'fid'")
            fid = upd["fid"]
            if not layer.getFeature(fid).isValid():
                raise CommandError(f"Update {i}: no feature with fid {fid} in layer")
            attrs = upd.get("attributes", {})
            field_map = {}
            for field_name, value in attrs.items():
                idx = layer.fields().indexOf(field_name)
                if idx < 0:
                    names = [fld.name() for fld in layer.fields()]
                    raise CommandError(
                        f"Update {i}: no field '{field_name}' in layer (fields: {names})"
                    )
                field_map[idx] = value
            if field_map:
                attr_map[fid] = field_map

        if attr_map:
            if layer.isEditable():
                for applied, (fid, field_map) in enumerate(attr_map.items()):
                    for idx, value in field_map.items():
                        if not layer.changeAttributeValue(fid, idx, value):
                            raise CommandError(
                                f"Failed to update fid {fid} in the edit buffer; "
                                f"{applied} of {len(attr_map)} features applied, "
                                "rollback_edits to discard"
                            )
            else:
                dp.clearErrors()
                if not dp.changeAttributeValues(attr_map):
                    raise CommandError(f"Failed to update features{self._provider_error(dp)}")
        return {"updated": len(attr_map), "buffered": layer.isEditable()}

    @command
    def delete_features(self, layer_id, fids=None, expression=None, **kwargs):
        layer = self._get_vector_layer(layer_id)
        dp = layer.dataProvider()

        if fids is not None and expression:
            # fids used to win silently, deleting a set the caller did not filter.
            raise CommandError("Pass fids or expression, not both")
        if fids is not None:
            target_fids = fids
        elif expression:
            self._check_filter_expression(layer, expression)
            request = QgsFeatureRequest().setFilterExpression(expression)
            request.setNoAttributes()
            target_fids = [f.id() for f in layer.getFeatures(request)]
        else:
            raise CommandError("Either fids or expression must be provided")

        before = layer.featureCount()
        if layer.isEditable():
            ok = layer.deleteFeatures(target_fids)
        else:
            dp.clearErrors()
            ok = dp.deleteFeatures(target_fids)
        if not ok:
            raise CommandError(f"Failed to delete features{self._provider_error(dp)}")
        layer.updateExtents()
        return {
            "requested": len(target_fids),
            # Measured, not assumed: a fid that matched nothing deletes nothing.
            "deleted": before - layer.featureCount(),
            "buffered": layer.isEditable(),
        }

    # --- Edit sessions -----------------------------------------------------

    @command
    def start_editing(self, layer_id, **kwargs):
        layer = self._get_vector_layer(layer_id)
        if layer.isEditable():
            return {"ok": True, "editing": True, "already_editing": True}
        if not layer.startEditing():
            raise CommandError(f"Failed to start editing '{layer.name()}' (read-only provider?)")
        return {"ok": True, "editing": True, "already_editing": False}

    @command
    def commit_edits(self, layer_id, **kwargs):
        layer = self._get_vector_layer(layer_id)
        if not layer.isEditable():
            raise CommandError(f"Layer '{layer.name()}' is not in edit mode")
        if not layer.commitChanges():
            errors = "; ".join(layer.commitErrors())
            raise CommandError(f"Commit failed: {errors}")
        layer.triggerRepaint()
        return {"ok": True, "editing": layer.isEditable()}

    @command
    def rollback_edits(self, layer_id, **kwargs):
        layer = self._get_vector_layer(layer_id)
        if not layer.isEditable():
            raise CommandError(f"Layer '{layer.name()}' is not in edit mode")
        if not layer.rollBack():
            raise CommandError(f"Rollback failed for '{layer.name()}'")
        layer.triggerRepaint()
        return {"ok": True, "editing": layer.isEditable()}

    @command
    def get_edit_status(self, layer_id, **kwargs):
        layer = self._get_vector_layer(layer_id)
        stack = layer.undoStack()
        status = {
            "layer_id": layer.id(),
            "name": layer.name(),
            "editable": layer.isEditable(),
            "modified": layer.isModified(),
            "can_undo": stack.canUndo(),
            "can_redo": stack.canRedo(),
            "undo_steps": stack.index(),
        }
        buf = layer.editBuffer()
        if buf is not None:
            status["pending"] = {
                "added": len(buf.addedFeatures()),
                "deleted": len(buf.deletedFeatureIds()),
                "changed_attributes": len(buf.changedAttributeValues()),
                "changed_geometries": len(buf.changedGeometries()),
            }
        return status

    def _step_undo_stack(self, layer_id, steps, redo):
        layer = self._get_vector_layer(layer_id)
        stack = layer.undoStack()
        steps = max(1, int(steps))
        done = 0
        for _ in range(steps):
            if redo:
                if not stack.canRedo():
                    break
                stack.redo()
            else:
                if not stack.canUndo():
                    break
                stack.undo()
            done += 1
        layer.triggerRepaint()
        return {
            "redone" if redo else "undone": done,
            "requested": steps,
            "can_undo": stack.canUndo(),
            "can_redo": stack.canRedo(),
        }

    @command
    def undo_edits(self, layer_id, steps=1, **kwargs):
        return self._step_undo_stack(layer_id, steps, redo=False)

    @command
    def redo_edits(self, layer_id, steps=1, **kwargs):
        return self._step_undo_stack(layer_id, steps, redo=True)

    @command
    def update_feature_geometry(self, layer_id, updates, **kwargs):
        layer = self._get_vector_layer(layer_id)
        geom_map = {}
        for i, upd in enumerate(updates):
            unknown = sorted(set(upd) - {"fid", "geometry_wkt"})
            if unknown:
                raise CommandError(
                    f"Update {i}: unknown key(s) {unknown} - expected 'fid' and 'geometry_wkt'"
                )
            if "fid" not in upd:
                raise CommandError(f"Update {i}: missing 'fid'")
            if "geometry_wkt" not in upd:
                raise CommandError(f"Update {i}: missing 'geometry_wkt'")
            fid = upd["fid"]
            if not layer.getFeature(fid).isValid():
                raise CommandError(f"Update {i}: no feature with fid {fid} in layer")
            geom = QgsGeometry.fromWkt(upd["geometry_wkt"])
            if geom.isNull():
                raise CommandError(f"Update {i}: invalid geometry_wkt: {upd['geometry_wkt']!r}")
            geom_map[fid] = geom

        if geom_map:
            if layer.isEditable():
                for applied, (fid, geom) in enumerate(geom_map.items()):
                    if not layer.changeGeometry(fid, geom):
                        raise CommandError(
                            f"Failed to update geometry for fid {fid}; "
                            f"{applied} of {len(geom_map)} applied, rollback_edits to discard"
                        )
            else:
                dp = layer.dataProvider()
                dp.clearErrors()
                if not dp.changeGeometryValues(geom_map):
                    raise CommandError(f"Failed to update geometries{self._provider_error(dp)}")
            layer.updateExtents()
            layer.triggerRepaint()
        return {"updated": len(geom_map), "buffered": layer.isEditable()}

    @command
    def select_features(self, layer_id, expression=None, fids=None, **kwargs):
        layer = self._get_vector_layer(layer_id)

        if fids is not None:
            layer.selectByIds(fids)
        elif expression:
            self._check_filter_expression(layer, expression)
            layer.selectByExpression(expression)
        else:
            raise CommandError("Either fids or expression must be provided")

        return {"selected": layer.selectedFeatureCount()}

    @command
    def get_selection(self, layer_id, **kwargs):
        layer = self._get_vector_layer(layer_id)
        return {
            "fids": list(layer.selectedFeatureIds()),
            "count": layer.selectedFeatureCount(),
        }

    @command
    def clear_selection(self, layer_id, **kwargs):
        layer = self._get_vector_layer(layer_id)
        layer.removeSelection()
        return {"ok": True}

    @command
    def add_field(self, layer_id, field_name, field_type, length=None, precision=None, **kwargs):
        """Add a field to a vector layer."""
        layer = self._get_vector_layer(layer_id)

        type_map = {
            "string": QVAR_STRING,
            "int": QVAR_INT,
            "double": QVAR_DOUBLE,
            "bool": QVAR_BOOL,
            "date": QVAR_DATE,
            "datetime": QVAR_DATETIME,
        }
        # An unknown type used to become a string field without a word.
        v_type = self._pick(type_map, field_type.lower(), "field_type")
        field = QgsField(field_name, v_type, field_type, length or 0, precision or 0)

        dp = layer.dataProvider()
        dp.clearErrors()
        if dp.addAttributes([field]):
            layer.updateFields()
            return {"ok": True, "field_name": field_name}
        else:
            raise CommandError(f"Failed to add field: {field_name}{self._provider_error(dp)}")

    @command
    def delete_field(self, layer_id, field_name, **kwargs):
        """Delete a field from a vector layer."""
        layer = self._get_vector_layer(layer_id)
        idx = layer.fields().indexOf(field_name)
        if idx < 0:
            raise CommandError(f"Field not found: {field_name}")

        dp = layer.dataProvider()
        dp.clearErrors()
        if dp.deleteAttributes([idx]):
            layer.updateFields()
            return {"ok": True, "field_name": field_name}
        else:
            raise CommandError(f"Failed to delete field: {field_name}{self._provider_error(dp)}")

    @command
    def rename_field(self, layer_id, old_name, new_name, **kwargs):
        """Rename a field in a vector layer."""
        layer = self._get_vector_layer(layer_id)
        idx = layer.fields().indexOf(old_name)
        if idx < 0:
            raise CommandError(f"Field not found: {old_name}")

        dp = layer.dataProvider()
        dp.clearErrors()
        if dp.renameAttributes({idx: new_name}):
            layer.updateFields()
            return {"ok": True, "old_name": old_name, "new_name": new_name}
        else:
            raise CommandError(f"Failed to rename field: {old_name}{self._provider_error(dp)}")

    @command
    def field_calculator(
        self,
        layer_id,
        field_name,
        expression,
        field_type="double",
        length=0,
        precision=0,
        **kwargs,
    ):
        """Add (if missing) and populate a field from a QGIS expression, in-place."""
        layer = self._get_vector_layer(layer_id)
        # Everything that can refuse the call runs before the schema changes:
        # a bad expression or an open session used to fail after the new
        # field had been added, leaving it behind empty.
        if layer.isEditable():
            raise CommandError(
                f"'{layer.name()}' has an open edit session; commit_edits or rollback_edits first"
            )
        self._check_filter_expression(layer, expression)
        type_map = {
            "string": QVAR_STRING,
            "int": QVAR_INT,
            "double": QVAR_DOUBLE,
            "bool": QVAR_BOOL,
            "date": QVAR_DATE,
            "datetime": QVAR_DATETIME,
        }
        idx = layer.fields().indexOf(field_name)
        created = False
        if idx < 0:
            v_type = self._pick(type_map, field_type.lower(), "field_type")
            dp = layer.dataProvider()
            dp.clearErrors()
            if not dp.addAttributes([QgsField(field_name, v_type, field_type, length, precision)]):
                raise CommandError(f"Failed to add field: {field_name}{self._provider_error(dp)}")
            layer.updateFields()
            idx = layer.fields().indexOf(field_name)
            created = True

        expr = QgsExpression(expression)
        ctx = QgsExpressionContext()
        ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        expr.prepare(ctx)

        if not layer.startEditing():
            raise CommandError("Could not start editing layer")
        updated = 0
        failed = 0
        first_error = None
        for feat in layer.getFeatures():
            ctx.setFeature(feat)
            val = expr.evaluate(ctx)
            # A feature the expression fails on keeps its old value; count it
            # and keep the first reason instead of skipping it unseen.
            if expr.hasEvalError():
                error = expr.evalErrorString()
            elif not layer.changeAttributeValue(feat.id(), idx, val):
                error = f"could not write {val!r} to {field_name}"
            else:
                updated += 1
                continue
            failed += 1
            if first_error is None:
                first_error = f"fid {feat.id()}: {error}"
        if not layer.commitChanges():
            errs = "; ".join(layer.commitErrors())
            raise CommandError(f"Commit failed: {errs}")
        response = {
            "ok": True,
            "field_name": field_name,
            "created": created,
            "updated": updated,
            "failed": failed,
        }
        if first_error:
            response["first_error"] = first_error
        measurement = {}
        if re.search(r"\$(area|length|perimeter)\b", expression):
            # $area/$length follow the project's units and ellipsoid, not the
            # layer's - say which, since the field name often claims otherwise.
            project = QgsProject.instance()
            measurement.update(
                ellipsoid=project.ellipsoid(),
                area_units=QgsUnitTypes.encodeUnit(project.areaUnits()),
                distance_units=QgsUnitTypes.encodeUnit(project.distanceUnits()),
            )
        if _measures_geometry(expression):
            # The function forms are planimetric in the geometry's CRS (the
            # layer's for $geometry): square degrees on a geographic layer.
            measurement["planimetric_units"] = QgsUnitTypes.encodeUnit(layer.crs().mapUnits())
        if measurement:
            response["measurement"] = measurement
        return response

    @command
    def get_unique_values(self, layer_id, field, limit=1000, **kwargs):
        """Return distinct values of a field (limit -1 for all)."""
        layer = self._get_vector_layer(layer_id)
        idx = layer.fields().indexOf(field)
        if idx < 0:
            raise CommandError(f"Field not found: {field}")
        # Two past the limit (room for NULL plus one), so a capped list says so
        # instead of reading as the whole set.
        raw = layer.uniqueValues(idx, limit + 2 if limit >= 0 else -1)
        values = [v for v in raw if v is not None and str(v) != "NULL"]
        truncated = limit >= 0 and len(values) > limit
        with contextlib.suppress(TypeError):
            values = sorted(values, key=lambda x: (str(type(x)), x))
        if limit >= 0:
            values = values[:limit]
        # NULL is dropped from values; say whether the field has any.
        has_null = any(v is None or str(v) == "NULL" for v in raw)
        if truncated and not has_null:
            # A capped sample can miss NULL, so ask for one directly.
            request = QgsFeatureRequest().setFilterExpression(
                f"{QgsExpression.quotedColumnRef(field)} IS NULL"
            )
            request.setLimit(1)
            has_null = any(True for _ in layer.getFeatures(request))
        return {
            "field": field,
            "values": values,
            "count": len(values),
            "truncated": truncated,
            "has_null": has_null,
        }

    @command
    def validate_expression(self, expression, layer_id=None, **kwargs):
        expr = QgsExpression(expression)
        result = {
            "valid": not expr.hasParserError(),
            "referenced_columns": list(expr.referencedColumns()),
        }
        if expr.hasParserError():
            result["error"] = expr.parserErrorString()

        if layer_id:
            # Skipping an unknown layer dropped the column check and said valid.
            layer = self._get_vector_layer(layer_id)
            context = QgsExpressionContext()
            context.appendScope(QgsExpressionContextUtils.layerScope(layer))
            expr.prepare(context)
            if expr.hasEvalError():
                result["valid"] = False
                result["eval_error"] = expr.evalErrorString()

        return result

    @command
    def evaluate_expression(self, expression, layer_id=None, **kwargs):
        """Evaluate a standalone QGIS expression to a scalar value."""
        exp = QgsExpression(expression)
        context = QgsExpressionContext()
        context.appendScope(QgsExpressionContextUtils.globalScope())
        context.appendScope(QgsExpressionContextUtils.projectScope(QgsProject.instance()))
        if layer_id:
            layer = self._get_vector_layer(layer_id)
            context.appendScope(QgsExpressionContextUtils.layerScope(layer))
        value = exp.evaluate(context)
        if exp.hasParserError():
            raise CommandError(f"Parser error: {exp.parserErrorString()}")
        if exp.hasEvalError():
            raise CommandError(f"Eval error: {exp.evalErrorString()}")
        return {"expression": expression, "result": value}

    @command
    def execute_sql(
        self,
        query,
        layers=None,
        as_layer=False,
        layer_name="sql_result",
        geometry_field=None,
        uid_field=None,
        limit=1000,
        **kwargs,
    ):
        """Run SQL across loaded layers via a virtual layer. Reference layers by name."""
        from qgis.core import QgsVirtualLayerDefinition

        project = QgsProject.instance()
        definition = QgsVirtualLayerDefinition()
        explicit = bool(layers)
        src_ids = layers or list(project.mapLayers().keys())
        sources = []
        for lid in src_ids:
            lyr = self._layer(lid)
            # A virtual layer can only join vector sources; a raster (or any
            # other layer type) makes the whole definition invalid.
            if lyr.type() != LAYER_VECTOR:
                if explicit:
                    raise CommandError(
                        f"Layer '{lyr.name()}' is not a vector layer - cannot be queried"
                    )
                continue
            if lyr.name() in sources:
                # Two sources under one table name: the query reads one of
                # them without saying which.
                raise CommandError(
                    f"Two queried layers are named '{lyr.name()}'; rename one or pass "
                    "'layers' to pick which to query"
                )
            definition.addSource(lyr.name(), lid)
            sources.append(lyr.name())
        if not sources:
            raise CommandError("No vector layers available to query")
        definition.setQuery(query)
        if geometry_field:
            definition.setGeometryField(geometry_field)
        else:
            definition.setGeometryWkbType(WKB_NO_GEOMETRY)
        if uid_field:
            definition.setUid(uid_field)
        vlayer = QgsVectorLayer(definition.toString(), layer_name, "virtual")
        if not vlayer.isValid():
            raise CommandError(
                f"Invalid SQL/virtual layer for query: {query} "
                f"(available table names: {sorted(sources)}){self._load_error(vlayer)}"
            )
        if as_layer:
            project.addMapLayer(vlayer)
            return {
                "output_layer_id": vlayer.id(),
                "name": vlayer.name(),
                "feature_count": vlayer.featureCount(),
            }
        fields = [f.name() for f in vlayer.fields()]
        limit = int(limit)
        rows = []
        truncated = False
        for feat in vlayer.getFeatures():
            if limit >= 0 and len(rows) >= limit:
                truncated = True
                break
            rows.append({fn: self._convert_attribute(feat[fn]) for fn in fields})
        return {"fields": fields, "rows": rows, "count": len(rows), "truncated": truncated}

    def _identify_in_layer(self, layer, rect, to_ref, pt_geom, tolerance, limit):
        """(hits, truncated) for *layer* in *rect*, compared in the point's CRS."""
        feats = []
        for feat in layer.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
            geom = feat.geometry()
            if geom.isEmpty():
                continue
            if to_ref is not None:
                geom = QgsGeometry(geom)
                try:
                    geom.transform(to_ref)
                except QgsCsException:
                    # This feature reaches past ref_crs; the rest still count.
                    continue
            if tolerance > 0:
                if geom.distance(pt_geom) > tolerance:
                    continue
            elif not geom.intersects(pt_geom):
                continue
            if len(feats) >= limit:
                # A hit past the limit: stop, and say the list is partial.
                return feats, True
            attrs = {f.name(): self._convert_attribute(feat[f.name()]) for f in layer.fields()}
            attrs["_fid"] = feat.id()
            feats.append(attrs)
        return feats, False

    @command
    def identify_features(self, point, tolerance=0.0, layer_ids=None, limit=10, crs=None, **kwargs):
        """Identify features at a point [x, y] across layers.

        The point and tolerance are in *crs* when given, else the project CRS.
        """
        project = QgsProject.instance()
        ref_crs = self._parse_crs(crs) if crs else project.crs()
        x, y = float(point[0]), float(point[1])
        pt_geom = QgsGeometry.fromPointXY(QgsPointXY(x, y))
        if layer_ids:
            # An explicit raster used to be skipped, answering "nothing here".
            targets = [self._get_vector_layer(lid) for lid in layer_ids]
        else:
            targets = [n.layer() for n in project.layerTreeRoot().findLayers() if n.isVisible()]
        prefilter = QgsRectangle(x - tolerance, y - tolerance, x + tolerance, y + tolerance)
        results = []
        skipped = []
        for layer in targets:
            if layer is None or layer.type() != LAYER_VECTOR:
                continue
            # The point and tolerance are in ref_crs; the features are in the
            # layer's. Comparing them raw answered "nothing here" whenever the
            # two differ, so search in layer CRS and compare in ref_crs.
            to_project = QgsCoordinateTransform(layer.crs(), ref_crs, project)
            reproject = layer.crs() != ref_crs and to_project.isValid()
            try:
                layer_rect = prefilter
                if reproject:
                    to_layer = QgsCoordinateTransform(ref_crs, layer.crs(), project)
                    layer_rect = to_layer.transformBoundingBox(prefilter)
                feats, truncated = self._identify_in_layer(
                    layer, layer_rect, to_project if reproject else None, pt_geom, tolerance, limit
                )
            except QgsCsException:
                # The point lies outside what the layer's CRS can express; one
                # such layer used to abort the whole call.
                skipped.append({"layer_id": layer.id(), "reason": "point not transformable"})
                continue
            if feats:
                results.append(
                    {
                        "layer_id": layer.id(),
                        "name": layer.name(),
                        "features": feats,
                        "count": len(feats),
                        "truncated": truncated,
                    }
                )
        response = {"point": [x, y], "crs": ref_crs.authid(), "results": results}
        if skipped:
            response["skipped_layers"] = skipped
        return response
