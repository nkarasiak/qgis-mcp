"""Handlers for the project: load/save, CRS, variables, bookmarks, map themes."""

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsExpressionContextUtils,
    QgsMessageLog,
    QgsProject,
    QgsRectangle,
)

from ..compat import MSG_INFO
from ..errors import CommandError
from ..registry import command


class ProjectHandlers:
    """The current project: files, CRS, variables, bookmarks, map themes."""

    @command
    def get_project_info(self, **kwargs):
        project = QgsProject.instance()

        info = {
            "filename": project.fileName(),
            "title": project.title(),
            "layer_count": len(project.mapLayers()),
            "crs": project.crs().authid(),
            "layers": [],
        }

        layers = list(project.mapLayers().values())
        for layer in layers[:10]:
            layer_info = {
                "id": layer.id(),
                "name": layer.name(),
                "type": self._get_layer_type(layer),
                "visible": layer.isValid() and self._is_visible(project, layer.id()),
            }
            info["layers"].append(layer_info)

        return info

    @command
    def save_project(self, path=None, **kwargs):
        project = QgsProject.instance()

        if not path and not project.fileName():
            raise CommandError("No project path specified and no current project path")

        save_path = path if path else project.fileName()
        if project.write(save_path):
            QgsMessageLog.logMessage(f"Project saved: {save_path}", self.LOG_TAG, MSG_INFO)
            return {"saved": save_path}
        else:
            raise CommandError(f"Failed to save project to {save_path}")

    @command
    def load_project(self, path, **kwargs):
        project = QgsProject.instance()
        if project.read(path):
            self.iface.mapCanvas().refresh()
            QgsMessageLog.logMessage(f"Project loaded: {path}", self.LOG_TAG, MSG_INFO)
            return {"loaded": path, "layer_count": len(project.mapLayers())}
        else:
            raise CommandError(f"Failed to load project from {path}")

    @command
    def create_new_project(self, path, **kwargs):
        project = QgsProject.instance()
        if project.fileName():
            project.clear()
        project.setFileName(path)
        self.iface.mapCanvas().refresh()
        if project.write():
            QgsMessageLog.logMessage(f"Project created: {path}", self.LOG_TAG, MSG_INFO)
            return {
                "created": f"Project created and saved successfully at: {path}",
                "layer_count": len(project.mapLayers()),
            }
        else:
            raise CommandError(f"Failed to save project to {path}")

    @command
    def get_project_variables(self, **kwargs):
        scope = QgsExpressionContextUtils.projectScope(QgsProject.instance())
        variables = {}
        for name in scope.variableNames():
            variables[name] = self._convert_attribute(scope.variable(name))
        return {"variables": variables}

    @command
    def set_project_variable(self, key, value, **kwargs):
        QgsExpressionContextUtils.setProjectVariable(QgsProject.instance(), key, value)
        return {"ok": True, "key": key, "value": value}

    @command
    def set_project_crs(self, crs, **kwargs):
        """Set the project CRS."""
        new_crs = QgsCoordinateReferenceSystem(crs)
        if not new_crs.isValid():
            raise CommandError(f"Invalid CRS: {crs}")
        QgsProject.instance().setCrs(new_crs)
        return {"ok": True, "crs": new_crs.authid(), "description": new_crs.description()}

    @command
    def get_bookmarks(self, **kwargs):
        """Get spatial bookmarks from the project."""
        bm = QgsProject.instance().bookmarkManager()
        bookmarks = []
        for b in bm.bookmarks():
            extent = b.extent()
            bookmarks.append(
                {
                    "id": b.id(),
                    "name": b.name(),
                    "group": b.group(),
                    "extent": {
                        "xmin": extent.xMinimum(),
                        "ymin": extent.yMinimum(),
                        "xmax": extent.xMaximum(),
                        "ymax": extent.yMaximum(),
                    },
                    "crs": extent.crs().authid() if extent.crs().isValid() else None,
                }
            )
        return {"bookmarks": bookmarks, "count": len(bookmarks)}

    @command
    def add_bookmark(self, name, xmin, ymin, xmax, ymax, crs="EPSG:4326", group="", **kwargs):
        """Add a spatial bookmark to the project."""
        from qgis.core import QgsBookmark, QgsReferencedRectangle

        crs_obj = QgsCoordinateReferenceSystem(crs)
        if not crs_obj.isValid():
            raise CommandError(f"Invalid CRS: {crs}")
        extent = QgsReferencedRectangle(QgsRectangle(xmin, ymin, xmax, ymax), crs_obj)
        bookmark = QgsBookmark()
        bookmark.setName(name)
        bookmark.setGroup(group)
        bookmark.setExtent(extent)
        result = QgsProject.instance().bookmarkManager().addBookmark(bookmark)
        # addBookmark returns (id, success) tuple in QGIS 3.x+
        bookmark_id = result[0] if isinstance(result, (list, tuple)) else result
        return {"ok": True, "id": bookmark_id, "name": name}

    @command
    def remove_bookmark(self, bookmark_id, **kwargs):
        """Remove a spatial bookmark by ID."""
        bm = QgsProject.instance().bookmarkManager()
        bm.removeBookmark(bookmark_id)
        return {"ok": True, "id": bookmark_id}

    @command
    def get_map_themes(self, **kwargs):
        """Get map themes (visibility presets)."""
        collection = QgsProject.instance().mapThemeCollection()
        themes = collection.mapThemes()
        result = []
        for name in themes:
            layer_ids = collection.mapThemeVisibleLayerIds(name)
            result.append(
                {
                    "name": name,
                    "visible_layer_count": len(layer_ids),
                    "visible_layer_ids": layer_ids,
                }
            )
        return {"themes": result, "count": len(result)}

    @command
    def add_map_theme(self, name, **kwargs):
        """Create a map theme from the current layer visibility state."""
        from qgis.core import QgsMapThemeCollection

        collection = QgsProject.instance().mapThemeCollection()
        root = QgsProject.instance().layerTreeRoot()
        model = self.iface.layerTreeView().layerTreeModel()
        record = QgsMapThemeCollection.createThemeFromCurrentState(root, model)
        if collection.hasMapTheme(name):
            collection.update(name, record)
            return {"ok": True, "name": name, "action": "updated"}
        else:
            collection.insert(name, record)
            return {"ok": True, "name": name, "action": "created"}

    @command
    def remove_map_theme(self, name, **kwargs):
        """Remove a map theme."""
        collection = QgsProject.instance().mapThemeCollection()
        if not collection.hasMapTheme(name):
            raise CommandError(f"Map theme not found: {name}")
        collection.removeMapTheme(name)
        return {"ok": True, "name": name}

    @command
    def apply_map_theme(self, name, **kwargs):
        """Apply a map theme (restore its layer visibility state)."""
        collection = QgsProject.instance().mapThemeCollection()
        if not collection.hasMapTheme(name):
            raise CommandError(f"Map theme not found: {name}")
        root = QgsProject.instance().layerTreeRoot()
        model = self.iface.layerTreeView().layerTreeModel()
        collection.applyTheme(name, root, model)
        self.iface.mapCanvas().refresh()
        return {"ok": True, "name": name}
