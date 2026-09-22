"""Handlers for feature and attribute content of vector layers.

Includes the edit-session commands. While a session is open the feature writes
go through the layer's edit buffer (undoable, discarded by rollback); with no
session they go straight to the data provider. Each write reports which path it
took via ``buffered``.
"""

import contextlib

from qgis.core import (
    QgsCoordinateTransform,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
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
                    simplified_geom = geom.simplify(0.001)
                    points_count = len(simplified_geom.asWkt().split(","))
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
                        "wkt": geom.asWkt(precision=3),
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
    def add_features(self, layer_id, features, **kwargs):
        layer = self._get_vector_layer(layer_id)
        dp = layer.dataProvider()
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
        return {"added": count, "buffered": layer.isEditable()}

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
            layer.dataProvider().addAttributes(
                [QgsField(field_name, v_type, field_type, length, precision)]
            )
            layer.updateFields()
            idx = layer.fields().indexOf(field_name)
            created = True

        expr = QgsExpression(expression)
        if expr.hasParserError():
            raise CommandError(f"Expression parse error: {expr.parserErrorString()}")
        ctx = QgsExpressionContext()
        ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        expr.prepare(ctx)

        if not layer.startEditing():
            raise CommandError("Could not start editing layer")
        updated = 0
        for feat in layer.getFeatures():
            ctx.setFeature(feat)
            val = expr.evaluate(ctx)
            if expr.hasEvalError():
                continue
            layer.changeAttributeValue(feat.id(), idx, val)
            updated += 1
        if not layer.commitChanges():
            errs = "; ".join(layer.commitErrors())
            raise CommandError(f"Commit failed: {errs}")
        return {"ok": True, "field_name": field_name, "created": created, "updated": updated}

    @command
    def get_unique_values(self, layer_id, field, limit=1000, **kwargs):
        """Return distinct values of a field (limit -1 for all)."""
        layer = self._get_vector_layer(layer_id)
        idx = layer.fields().indexOf(field)
        if idx < 0:
            raise CommandError(f"Field not found: {field}")
        raw = layer.uniqueValues(idx, limit)
        values = [v for v in raw if v is not None and str(v) != "NULL"]
        with contextlib.suppress(TypeError):
            values = sorted(values, key=lambda x: (str(type(x)), x))
        return {"field": field, "values": values, "count": len(values)}

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

    @command
    def identify_features(self, point, tolerance=0.0, layer_ids=None, limit=10, **kwargs):
        """Identify features at a point [x, y] (project CRS) across layers."""
        project = QgsProject.instance()
        x, y = float(point[0]), float(point[1])
        pt_geom = QgsGeometry.fromPointXY(QgsPointXY(x, y))
        if layer_ids:
            targets = [self._layer(lid) for lid in layer_ids]
        else:
            targets = [n.layer() for n in project.layerTreeRoot().findLayers() if n.isVisible()]
        prefilter = QgsRectangle(x - tolerance, y - tolerance, x + tolerance, y + tolerance)
        results = []
        for layer in targets:
            if layer is None or layer.type() != LAYER_VECTOR:
                continue
            # The point and tolerance are in project CRS; the features are in the
            # layer's. Comparing them raw answered "nothing here" whenever the
            # two differ, so search in layer CRS and compare in project CRS.
            to_project = QgsCoordinateTransform(layer.crs(), project.crs(), project)
            reproject = layer.crs() != project.crs() and to_project.isValid()
            layer_rect = prefilter
            if reproject:
                to_layer = QgsCoordinateTransform(project.crs(), layer.crs(), project)
                layer_rect = to_layer.transformBoundingBox(prefilter)
            req = QgsFeatureRequest().setFilterRect(layer_rect)
            feats = []
            for feat in layer.getFeatures(req):
                geom = feat.geometry()
                if geom.isEmpty():
                    continue
                if reproject:
                    geom = QgsGeometry(geom)
                    geom.transform(to_project)
                if tolerance > 0:
                    if geom.distance(pt_geom) > tolerance:
                        continue
                elif not geom.intersects(pt_geom):
                    continue
                attrs = {f.name(): self._convert_attribute(feat[f.name()]) for f in layer.fields()}
                attrs["_fid"] = feat.id()
                feats.append(attrs)
                if len(feats) >= limit:
                    break
            if feats:
                results.append(
                    {
                        "layer_id": layer.id(),
                        "name": layer.name(),
                        "features": feats,
                        "count": len(feats),
                    }
                )
        return {"point": [x, y], "crs": project.crs().authid(), "results": results}
