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


# --- rasters ---------------------------------------------------------------------


@pytest.fixture
def raster(plugin_handlers, monkeypatch):
    """A 1-band raster covering x, y in [0, 4]; pixel (0.5, 3.5) is nodata."""
    project = MagicMock()
    qgs_project = MagicMock(**{"instance.return_value": project})
    monkeypatch.setattr(plugin_handlers.base, "QgsProject", qgs_project)
    monkeypatch.setattr(plugin_handlers.processing, "QgsPointXY", lambda x, y: (x, y))
    layer = MagicMock()
    layer.type.return_value = plugin_handlers.base.LAYER_RASTER
    layer.bandCount.return_value = 1
    layer.crs.return_value.authid.return_value = "EPSG:32631"
    layer.extent.return_value.contains.side_effect = lambda p: 0 <= p[0] <= 4 and 0 <= p[1] <= 4
    dp = layer.dataProvider.return_value
    dp.sample.side_effect = lambda p, b: (
        (float("nan"), False)
        if p == (0.5, 3.5) or not (0 <= p[0] <= 4 and 0 <= p[1] <= 4)
        else (6.0, True)
    )
    project.mapLayer.return_value = layer

    class Server(plugin_handlers.processing.ProcessingHandlers, plugin_handlers.base.HandlerBase):
        LOG_TAG = "test"

    return Server(), layer


def test_sample_tells_nodata_from_a_point_off_the_raster(raster):
    """Both came back as value: null, so a CRS mix-up looked like missing data."""
    server, _ = raster

    result = server.sample_raster_values("r", [[1.5, 3.5], [0.5, 3.5], [2.35, 48.85]], band=1)

    got = [(s["value"], s["outside_extent"]) for s in result["samples"]]
    assert got == [(6.0, False), (None, False), (None, True)]
    assert result["crs"] == "EPSG:32631"


@pytest.mark.parametrize("band", [0, 5])
def test_sample_refuses_a_band_the_raster_lacks(raster, plugin_handlers, band):
    """band=0 used to mean "all bands" and band=5 returned null for every point."""
    server, _ = raster

    with pytest.raises(plugin_handlers.base.CommandError, match="out of range"):
        server.sample_raster_values("r", [[1.5, 3.5]], band=band)


@pytest.fixture
def raster_info(plugin_handlers, raster, monkeypatch):
    _, layer = raster

    class Server(plugin_handlers.layers.LayerHandlers, plugin_handlers.base.HandlerBase):
        LOG_TAG = "test"

    dp = layer.dataProvider.return_value
    dp.sourceHasNoDataValue.return_value = True
    dp.sourceNoDataValue.return_value = -9999.0
    dp.userNoDataValues.return_value = []
    dp.bandScale.return_value = 0.1
    dp.bandOffset.return_value = 5.0
    return Server(), dp


def test_raster_info_says_whether_nodata_is_applied(raster_info):
    """With "use source nodata" off, the stats count nodata pixels as data."""
    server, dp = raster_info
    dp.useSourceNoDataValue.return_value = False

    band = server.get_raster_info("r")["bands"][0]

    assert (band["nodata"], band["nodata_used"]) == (-9999.0, False)


def test_raster_info_reports_scale_and_offset(raster_info):
    """Stats are scaled and nodata is raw; without scale/offset they never match."""
    server, dp = raster_info
    dp.useSourceNoDataValue.return_value = True

    band = server.get_raster_info("r")["bands"][0]

    assert (band["scale"], band["offset"]) == (0.1, 5.0)
    assert "raw" in band["note"]


def test_raster_info_omits_nodata_when_the_band_has_none(raster_info):
    server, dp = raster_info
    dp.sourceHasNoDataValue.return_value = False
    dp.bandScale.return_value, dp.bandOffset.return_value = 1.0, 0.0

    band = server.get_raster_info("r")["bands"][0]

    assert "nodata" not in band and "scale" not in band


def test_sample_points_in_an_explicit_crs_are_reprojected(raster, plugin_handlers, monkeypatch):
    server, layer = raster
    monkeypatch.setattr(plugin_handlers.base, "QgsCoordinateReferenceSystem", lambda s: Crs(s))
    layer.crs.return_value = Crs("EPSG:32631")

    class ToRaster:
        def __init__(self, src, dst, project):
            assert (src.authid(), dst.authid()) == ("EPSG:4326", "EPSG:32631")

        def transform(self, p):
            return (1.5, 3.5)  # a data pixel

    monkeypatch.setattr(plugin_handlers.processing, "QgsCoordinateTransform", ToRaster)

    result = server.sample_raster_values("r", [[2.35, 48.85]], band=1, crs="EPSG:4326")

    assert result["samples"][0]["value"] == 6.0
    assert result["samples"][0]["outside_extent"] is False
    assert result["crs"] == "EPSG:4326"


def test_sample_a_point_the_raster_crs_cannot_express_is_skipped(
    raster, plugin_handlers, monkeypatch
):
    """A QgsCsException on one point used to abort the whole call."""
    server, layer = raster
    monkeypatch.setattr(plugin_handlers.base, "QgsCoordinateReferenceSystem", lambda s: Crs(s))
    layer.crs.return_value = Crs("EPSG:32631")

    class CsError(Exception):
        pass

    class ToRaster:
        def __init__(self, src, dst, project):
            pass

        def transform(self, p):
            if p[0] > 100:
                raise CsError("forward transform")
            return (1.5, 3.5)

    monkeypatch.setattr(plugin_handlers.processing, "QgsCsException", CsError)
    monkeypatch.setattr(plugin_handlers.processing, "QgsCoordinateTransform", ToRaster)

    result = server.sample_raster_values(
        "r", [[500.0, 0.0], [2.35, 48.85]], band=1, crs="EPSG:4326"
    )

    first, second = result["samples"]
    assert first == {
        "x": 500.0,
        "y": 0.0,
        "outside_extent": True,
        "band": 1,
        "value": None,
        "transform_failed": True,
    }
    assert second["value"] == 6.0
