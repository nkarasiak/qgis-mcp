"""CRS edge cases in the canvas/transform handlers: invalid CRSs and the antimeridian."""

from unittest.mock import MagicMock

import pytest


class Rect:
    def __init__(self, xmin, ymin, xmax, ymax):
        self.v = (xmin, ymin, xmax, ymax)

    def xMinimum(self):
        return self.v[0]

    def yMinimum(self):
        return self.v[1]

    def xMaximum(self):
        return self.v[2]

    def yMaximum(self):
        return self.v[3]


class Crs:
    def __init__(self, authid, valid=True):
        self._authid, self._valid = authid, valid

    def isValid(self):
        return self._valid

    def authid(self):
        return self._authid

    def __eq__(self, other):
        return self._authid == other._authid


# EPSG:3832 box from 177E to 174W, as QGIS transforms it (verified live):
CROSSING = Rect(177.0, -20.0, -174.0, -10.0)  # with handle180Crossover
LONG_WAY = Rect(-174.0, -20.0, 177.0, -10.0)  # without


class Transform:
    def __init__(self, src, dst, project):
        pass

    def transformBoundingBox(self, rect, handle180Crossover=False):
        return CROSSING if handle180Crossover else LONG_WAY


@pytest.fixture
def canvas(plugin_handlers, monkeypatch):
    mod = plugin_handlers.canvas
    monkeypatch.setattr(mod, "QgsRectangle", Rect)
    monkeypatch.setattr(mod, "QgsCoordinateTransform", Transform)
    monkeypatch.setattr(
        mod, "QgsCoordinateReferenceSystem", lambda s: Crs(s, valid=s != "EPSG:999999")
    )

    class Server(mod.CanvasHandlers):
        iface = MagicMock()

    server = Server()
    server.iface.mapCanvas.return_value.mapSettings.return_value.destinationCrs.return_value = Crs(
        "EPSG:4326"
    )
    return server


def test_transform_bbox_across_the_antimeridian_is_flagged(canvas):
    """Without crossover handling the box came back ~351 deg wide instead of ~11."""
    result = canvas.transform_coordinates(
        "EPSG:3832", "EPSG:4326", bbox={"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1}
    )

    assert (result["bbox"]["xmin"], result["bbox"]["xmax"]) == (177.0, -174.0)
    assert result["crosses_antimeridian"] is True


def test_set_canvas_extent_refuses_an_invalid_crs(canvas, plugin_handlers):
    """An invalid transform returns the box unchanged: degrees applied as map units."""
    with pytest.raises(plugin_handlers.base.CommandError, match="Invalid CRS"):
        canvas.set_canvas_extent(0, 0, 1, 1, crs="EPSG:999999")

    canvas.iface.mapCanvas.return_value.setExtent.assert_not_called()


def test_set_canvas_extent_warns_when_the_box_crosses_the_antimeridian(canvas):
    result = canvas.set_canvas_extent(0, 0, 1, 1, crs="EPSG:3832")

    assert "antimeridian" in result["warning"]
    assert result["crs"] == "EPSG:4326"
