"""Feature handlers against a stubbed qgis (no QGIS needed).

Covers the counts and limits the handlers report, which used to be assumed
rather than measured: the layer total standing in for an expression's match
count, a deleted count taken from the request, a set sliced without an order,
and a row cap nothing could see or change.
"""

from unittest.mock import MagicMock

import pytest


class Row:
    """A feature whose every column reads back as one value."""

    def __init__(self, value):
        self.value = value

    def __getitem__(self, key):
        return self.value


class FakeExpression:
    """QgsExpression that parses and prepares unless a test says otherwise."""

    parse_error = ""
    prepare_error = ""

    def __init__(self, text):
        self.text = text

    def hasParserError(self):
        return bool(self.parse_error)

    def parserErrorString(self):
        return self.parse_error

    def prepare(self, context):
        return not self.prepare_error

    def hasEvalError(self):
        return bool(self.prepare_error)

    def evalErrorString(self):
        return self.prepare_error


@pytest.fixture
def features(plugin_handlers, monkeypatch):
    """The feature mixin with the qgis names it touches freshly mocked per test."""
    base, features = plugin_handlers.base, plugin_handlers.features
    monkeypatch.setattr(base, "QgsProject", MagicMock())
    monkeypatch.setattr(base, "QgsExpression", FakeExpression)
    monkeypatch.setattr(FakeExpression, "parse_error", "")
    monkeypatch.setattr(FakeExpression, "prepare_error", "")
    monkeypatch.setattr(features, "QgsFeatureRequest", MagicMock())
    monkeypatch.setattr(features, "QgsVectorLayer", MagicMock())
    return features


@pytest.fixture
def server(plugin_handlers, features):
    class Server(features.FeatureHandlers, plugin_handlers.base.HandlerBase):
        pass

    return Server()


@pytest.fixture
def layer(plugin_handlers, features):
    """A vector layer returned for every layer id."""
    layer = MagicMock()
    layer.type.return_value = plugin_handlers.base.LAYER_VECTOR
    plugin_handlers.base.QgsProject.instance.return_value.mapLayer.return_value = layer
    return layer


def test_get_layer_features_counts_what_the_expression_matches(server, layer):
    layer.fields.return_value = []
    layer.featureCount.return_value = 100
    matches = [MagicMock(), MagicMock(), MagicMock()]
    layer.getFeatures.side_effect = [matches, matches]

    result = server.get_layer_features("lid", expression="population > 1000")

    assert result["feature_count"] == 100  # the layer
    assert result["matched"] == 3  # the expression
    assert len(result["features"]) == 3


def test_get_layer_features_without_an_expression_matches_the_layer(server, layer):
    layer.fields.return_value = []
    layer.featureCount.return_value = 42
    layer.getFeatures.return_value = []

    result = server.get_layer_features("lid")

    assert result["matched"] == result["feature_count"] == 42
    assert layer.getFeatures.call_count == 1  # no second pass to count


def test_distinct_values_are_sorted_before_the_slice(server, layer):
    fields = layer.fields.return_value
    fields.indexOf.return_value = 0
    fields.at.return_value.isNumeric.return_value = False
    values = [f"v{i:03d}" for i in range(60)]
    layer.aggregate.side_effect = [(60, True), (list(reversed(values)), True)]

    stats = server.get_field_statistics("lid", "name")

    assert stats["distinct_count"] == 60
    assert stats["distinct_values"] == values[:50]


def test_delete_features_reports_what_actually_went_away(server, layer):
    layer.isEditable.return_value = False
    layer.dataProvider.return_value.deleteFeatures.return_value = True
    layer.featureCount.side_effect = [10, 8]  # around the delete

    result = server.delete_features("lid", fids=[1, 2, 3])

    assert result == {"requested": 3, "deleted": 2, "buffered": False}


def test_execute_sql_caps_rows_and_says_so(server, features, layer):
    vlayer = features.QgsVectorLayer.return_value
    vlayer.fields.return_value = [MagicMock(**{"name.return_value": "n"})]
    vlayer.getFeatures.return_value = [Row("x") for _ in range(5)]

    capped = server.execute_sql("SELECT 1", layers=["lid"], limit=3)
    assert capped["count"] == 3
    assert capped["truncated"] is True

    uncapped = server.execute_sql("SELECT 1", layers=["lid"], limit=-1)
    assert uncapped["count"] == 5
    assert uncapped["truncated"] is False


def test_identify_features_rejects_an_unknown_layer_id(plugin_handlers, server):
    plugin_handlers.base.QgsProject.instance.return_value.mapLayer.return_value = None

    with pytest.raises(plugin_handlers.base.LayerNotFound):
        server.identify_features([1.0, 2.0], layer_ids=["nope"])


# A filter that fails to parse or names a missing field matches nothing without
# raising in QGIS, so "0 matches" used to come back for a filter that never ran.


@pytest.mark.parametrize(
    ("attr", "message"),
    [
        ("parse_error", "syntax error, unexpected EQ"),
        ("prepare_error", "Field 'nmae' not found"),
    ],
)
@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.get_layer_features("lid", expression="bad"),
        lambda s: s.delete_features("lid", expression="bad"),
        lambda s: s.select_features("lid", expression="bad"),
    ],
    ids=["get_layer_features", "delete_features", "select_features"],
)
def test_a_broken_filter_is_an_error_not_zero_matches(
    plugin_handlers, server, layer, monkeypatch, attr, message, call
):
    monkeypatch.setattr(FakeExpression, attr, message)

    with pytest.raises(plugin_handlers.base.CommandError, match=message):
        call(server)

    layer.getFeatures.assert_not_called()
    layer.selectByExpression.assert_not_called()
    layer.deleteFeatures.assert_not_called()


@pytest.fixture
def exporter(plugin_handlers, features, layer):
    runs = []

    class Server(plugin_handlers.layers.LayerHandlers, plugin_handlers.base.HandlerBase):
        def _run_alg(self, algorithm, parameters, *args, **kwargs):
            runs.append(algorithm)
            self.last_params = parameters
            return {"OUTPUT": "out"}

    return Server(), runs


def test_export_layer_refuses_a_broken_filter_before_writing(
    plugin_handlers, exporter, monkeypatch
):
    server, runs = exporter
    monkeypatch.setattr(FakeExpression, "prepare_error", "Field 'nmae' not found")

    with pytest.raises(plugin_handlers.base.CommandError, match="nmae"):
        server.export_layer("lid", "/tmp/o.gpkg", filter_expression='"nmae" = 1')

    assert runs == []


def test_export_layer_refuses_a_filter_on_a_raster(plugin_handlers, exporter, layer):
    """It used to be ignored, handing back the whole raster as if filtered."""
    server, runs = exporter
    layer.type.return_value = plugin_handlers.base.LAYER_RASTER

    with pytest.raises(plugin_handlers.base.CommandError, match="vector layers only"):
        server.export_layer("lid", "/tmp/o.tif", filter_expression="1 = 1")

    assert runs == []


@pytest.mark.parametrize("has_nodata", [(True, True), (True, False)], ids=["all", "one_missing"])
def test_export_layer_warp_marks_fill_cells_only_without_source_nodata(
    plugin_handlers, exporter, layer, has_nodata
):
    """gdalwarp fills outside the footprint with 0 when the source has no nodata."""
    server, runs = exporter
    layer.type.return_value = plugin_handlers.base.LAYER_RASTER
    layer.bandCount.return_value = 2
    layer.dataProvider.return_value.sourceHasNoDataValue.side_effect = lambda b: has_nodata[b - 1]

    result = server.export_layer("lid", "/tmp/o.tif", target_crs="EPSG:3857")

    assert runs == ["gdal:warpreproject"]
    if all(has_nodata):
        assert "EXTRA" not in server.last_params
        assert "alpha_band_added" not in result
    else:
        assert server.last_params["EXTRA"] == "-dstalpha"
        assert result["alpha_band_added"] is True


def test_export_layer_warp_keeps_a_source_alpha_band(plugin_handlers, exporter, layer, monkeypatch):
    """gdalwarp carries it over: reporting an alpha band as added was false."""
    server, _ = exporter
    monkeypatch.setattr(plugin_handlers.layers, "RASTER_ALPHA_BAND", "alpha")
    layer.type.return_value = plugin_handlers.base.LAYER_RASTER
    layer.bandCount.return_value = 2
    dp = layer.dataProvider.return_value
    dp.sourceHasNoDataValue.return_value = False
    dp.colorInterpretation.side_effect = lambda b: "alpha" if b == 2 else "gray"

    result = server.export_layer("lid", "/tmp/o.tif", target_crs="EPSG:3857")

    assert "EXTRA" not in server.last_params
    assert "alpha_band_added" not in result


class Crs(str):
    """A CRS that compares by authid, like QgsCoordinateReferenceSystem."""

    def authid(self):
        return str(self)

    def isValid(self):
        return True


class FakeTransform:
    def __init__(self, src, dst, project):
        self.src, self.dst = src, dst

    def isValid(self):
        return True

    def transformBoundingBox(self, rect):
        return ("rect", self.src, self.dst)


@pytest.fixture
def identify(plugin_handlers, server, layer, features, monkeypatch):
    """identify_features with a project in EPSG:3857 and one hit feature."""
    project = plugin_handlers.base.QgsProject.instance.return_value
    monkeypatch.setattr(features, "QgsProject", plugin_handlers.base.QgsProject)
    monkeypatch.setattr(features, "QgsCoordinateTransform", FakeTransform)
    monkeypatch.setattr(features, "QgsGeometry", MagicMock())
    project.crs.return_value = Crs("EPSG:3857")
    layer.fields.return_value = []
    feat = MagicMock()
    feat.geometry.return_value.isEmpty.return_value = False
    layer.getFeatures.return_value = [feat]
    return server, layer, features


def test_identify_searches_a_layer_in_its_own_crs(identify):
    """The point is project CRS; comparing it raw to layer coords found nothing."""
    server, layer, features = identify
    layer.crs.return_value = Crs("EPSG:4326")

    result = server.identify_features([250000.0, 6200000.0], layer_ids=["lid"])

    rect = features.QgsFeatureRequest.return_value.setFilterRect.call_args[0][0]
    assert rect == ("rect", "EPSG:3857", "EPSG:4326"), "prefilter must be in layer CRS"
    copy = features.QgsGeometry.return_value
    (to_project,) = copy.transform.call_args[0]
    assert (to_project.src, to_project.dst) == ("EPSG:4326", "EPSG:3857")
    assert result["crs"] == "EPSG:3857"
    assert result["results"][0]["count"] == 1


def test_identify_same_crs_uses_the_point_as_is(identify):
    server, layer, features = identify
    layer.crs.return_value = Crs("EPSG:3857")

    server.identify_features([1.0, 2.0], layer_ids=["lid"])

    features.QgsGeometry.return_value.transform.assert_not_called()


class CsError(Exception):
    pass


def test_identify_skips_a_layer_the_point_cannot_reach(identify, monkeypatch):
    """A QgsCsException on one layer used to abort the whole call."""
    server, layer, features = identify
    monkeypatch.setattr(features, "QgsCsException", CsError)
    layer.crs.return_value = Crs("EPSG:4326")
    layer.id.return_value = "lid"

    def unreachable(self, rect):
        raise CsError("forward transform")

    monkeypatch.setattr(FakeTransform, "transformBoundingBox", unreachable)

    result = server.identify_features([1.0, 2.0], layer_ids=["lid"])

    assert result["results"] == []
    assert result["skipped_layers"] == [{"layer_id": "lid", "reason": "point not transformable"}]


# --- set_layer_property -------------------------------------------------------


@pytest.fixture
def layer_server(plugin_handlers, features, layer):
    class Server(plugin_handlers.layers.LayerHandlers, plugin_handlers.base.HandlerBase):
        iface = MagicMock()

    return Server()


@pytest.mark.parametrize(("sent", "applied"), [("false", False), ("True", True), ("0", False)])
def test_scale_visibility_reads_the_boolean_it_is_sent(layer_server, layer, sent, applied):
    """bool("false") is True, so "false" used to switch scale visibility on."""
    result = layer_server.set_layer_property("lid", "scale_visibility", sent)

    layer.setScaleBasedVisibility.assert_called_once_with(applied)
    assert result["value"] is applied


def test_scale_visibility_refuses_a_non_boolean(plugin_handlers, layer_server, layer):
    with pytest.raises(plugin_handlers.base.CommandError, match="Not a boolean"):
        layer_server.set_layer_property("lid", "scale_visibility", "maybe")

    layer.setScaleBasedVisibility.assert_not_called()


# --- create_new_project -------------------------------------------------------


@pytest.fixture
def project_server(plugin_handlers, monkeypatch):
    project = MagicMock()
    project.write.return_value = True
    project.crs.return_value.authid.return_value = "EPSG:4326"
    project.ellipsoid.return_value = "EPSG:7030"
    qgs_project = MagicMock(**{"instance.return_value": project})
    monkeypatch.setattr(plugin_handlers.project, "QgsProject", qgs_project)

    class Server(plugin_handlers.project.ProjectHandlers):
        LOG_TAG = "test"
        iface = MagicMock()

    return Server(), project


def test_create_new_project_runs_file_new(project_server):
    """clear() left no CRS and ellipsoid NONE, and kept an unsaved project's layers."""
    server, project = project_server
    server.iface.newProject.return_value = True

    result = server.create_new_project("/tmp/p.qgz")

    server.iface.newProject.assert_called_once_with(False)
    project.clear.assert_not_called()
    project.setFileName.assert_called_once_with("/tmp/p.qgz")
    assert (result["crs"], result["ellipsoid"]) == ("EPSG:4326", "EPSG:7030")


def test_create_new_project_reports_when_qgis_refuses(plugin_handlers, project_server):
    server, project = project_server
    server.iface.newProject.return_value = False

    with pytest.raises(plugin_handlers.base.CommandError):
        server.create_new_project("/tmp/p.qgz")

    project.write.assert_not_called()


# --- geometry output -----------------------------------------------------------


def _feature_with(geom):
    feat = MagicMock()
    feat.hasGeometry.return_value = True
    feat.geometry.return_value = geom
    return feat


@pytest.mark.parametrize(("geographic", "decimals"), [(True, 7), (False, 3)])
def test_point_wkt_precision_follows_the_crs(server, layer, features, geographic, decimals):
    """3 decimals is a millimetre in metres but ~55 m in degrees."""
    layer.fields.return_value = []
    layer.crs.return_value.isGeographic.return_value = geographic
    layer.crs.return_value.authid.return_value = "EPSG:4326" if geographic else "EPSG:2154"
    geom = MagicMock()
    geom.type.return_value = "point"
    layer.getFeatures.return_value = [_feature_with(geom)]

    result = server.get_layer_features("lid", include_geometry=True)

    geom.asWkt.assert_called_once_with(precision=decimals)
    assert result["crs"] == ("EPSG:4326" if geographic else "EPSG:2154")


def test_polygon_summary_counts_the_real_vertices(server, layer, features):
    """simplify(0.001) in layer units made the count differ between CRSs."""
    layer.fields.return_value = []
    geom = MagicMock()
    geom.type.return_value = features.GEOM_POLYGON
    geom.constGet.return_value.nCoordinates.return_value = 9
    layer.getFeatures.return_value = [_feature_with(geom)]

    result = server.get_layer_features("lid", include_geometry=True)

    assert "with 9 points" in result["features"][0]["_geometry"]["wkt_summary"]
    geom.simplify.assert_not_called()


# --- field_calculator ----------------------------------------------------------


class EvalExpression:
    """Evaluates per feature: feature ids in `fail` raise an eval error."""

    fail = ()

    def __init__(self, text):
        self._error = ""

    def prepare(self, context):
        return True

    def evaluate(self, context):
        fid = context.setFeature.call_args[0][0].id()
        self._error = f"cannot convert row {fid}" if fid in self.fail else ""
        return None if self._error else fid * 10

    def hasEvalError(self):
        return bool(self._error)

    def evalErrorString(self):
        return self._error


@pytest.fixture
def calculator(plugin_handlers, server, layer, features, monkeypatch):
    monkeypatch.setattr(features, "QgsExpression", EvalExpression)
    monkeypatch.setattr(features, "QgsExpressionContext", MagicMock)
    monkeypatch.setattr(EvalExpression, "fail", ())
    layer.isEditable.return_value = False
    layer.fields.return_value.indexOf.return_value = 2  # field exists
    rows = []
    for fid in range(3):
        f = MagicMock()
        f.id.return_value = fid
        rows.append(f)
    layer.getFeatures.return_value = rows
    layer.startEditing.return_value = True
    layer.commitChanges.return_value = True
    layer.changeAttributeValue.return_value = True
    return server, layer


def test_field_calculator_counts_and_explains_failed_features(calculator, monkeypatch):
    """A feature the expression failed on was skipped unseen and kept its old value."""
    server, _ = calculator
    monkeypatch.setattr(EvalExpression, "fail", (1,))

    result = server.field_calculator("lid", "v", 'to_int("s")')

    assert (result["updated"], result["failed"]) == (2, 1)
    assert result["first_error"] == "fid 1: cannot convert row 1"


def test_field_calculator_counts_a_refused_write_as_failed(calculator):
    server, layer = calculator
    layer.changeAttributeValue.side_effect = [True, False, True]

    result = server.field_calculator("lid", "v", "1")

    assert (result["updated"], result["failed"]) == (2, 1)
    assert "could not write" in result["first_error"]


def test_field_calculator_checks_the_expression_before_adding_the_field(
    calculator, plugin_handlers, monkeypatch
):
    """A bad expression used to fail after the new field was added, leaving it empty."""
    server, layer = calculator
    layer.fields.return_value.indexOf.return_value = -1
    monkeypatch.setattr(FakeExpression, "prepare_error", "Field 'nmae' not found")

    with pytest.raises(plugin_handlers.base.CommandError, match="nmae"):
        server.field_calculator("lid", "new", '"nmae" * 2')

    layer.dataProvider.return_value.addAttributes.assert_not_called()


def test_field_calculator_refuses_an_open_edit_session_before_touching_the_schema(
    calculator, plugin_handlers
):
    server, layer = calculator
    layer.isEditable.return_value = True
    layer.fields.return_value.indexOf.return_value = -1

    with pytest.raises(plugin_handlers.base.CommandError, match="open edit session"):
        server.field_calculator("lid", "new", "1")

    layer.dataProvider.return_value.addAttributes.assert_not_called()


def test_field_calculator_refuses_an_unknown_field_type(calculator, plugin_handlers):
    """It used to become a double field without a word."""
    server, layer = calculator
    layer.fields.return_value.indexOf.return_value = -1

    with pytest.raises(plugin_handlers.base.CommandError, match="Unknown field_type"):
        server.field_calculator("lid", "new", "1", field_type="integer64")


def test_field_calculator_reports_the_units_of_area(
    calculator, plugin_handlers, features, monkeypatch
):
    """$area follows the project's units: area_m2 got hectares with the project in ha."""
    server, _ = calculator
    project = MagicMock()
    project.ellipsoid.return_value = "EPSG:7030"
    monkeypatch.setattr(features, "QgsProject", MagicMock(**{"instance.return_value": project}))
    monkeypatch.setattr(
        features, "QgsUnitTypes", MagicMock(**{"encodeUnit.side_effect": ["ha", "meters"]})
    )

    result = server.field_calculator("lid", "area", "$area")

    assert result["measurement"] == {
        "ellipsoid": "EPSG:7030",
        "area_units": "ha",
        "distance_units": "meters",
    }


def test_field_calculator_reports_the_crs_units_of_the_area_function(
    calculator, features, monkeypatch
):
    """area() is planimetric in the layer CRS, not the project's ellipsoid."""
    server, layer = calculator
    monkeypatch.setattr(
        features, "QgsUnitTypes", MagicMock(**{"encodeUnit.return_value": "degrees"})
    )

    result = server.field_calculator("lid", "area", "area($geometry)")

    assert result["measurement"] == {"planimetric_units": "degrees"}
    features.QgsUnitTypes.encodeUnit.assert_called_once_with(
        layer.crs.return_value.mapUnits.return_value
    )


def test_identify_point_in_an_explicit_crs(identify, plugin_handlers, monkeypatch):
    """With crs given, the point is compared in that CRS, not the project's."""
    server, layer, features = identify
    monkeypatch.setattr(plugin_handlers.base, "QgsCoordinateReferenceSystem", lambda s: Crs(s))
    layer.crs.return_value = Crs("EPSG:2154")

    result = server.identify_features([2.35, 48.85], layer_ids=["lid"], crs="EPSG:4326")

    rect = features.QgsFeatureRequest.return_value.setFilterRect.call_args[0][0]
    assert rect == ("rect", "EPSG:4326", "EPSG:2154")
    assert result["crs"] == "EPSG:4326"


# --- add_features geometry ---------------------------------------------------------


@pytest.fixture
def adder(server, layer, features, monkeypatch):
    geom = MagicMock()
    geom.isNull.return_value = False
    geom.isGeosValid.return_value = True
    geom.type.return_value = "polygon"
    layer.geometryType.return_value = "polygon"
    layer.isEditable.return_value = False
    layer.fields.return_value = []
    layer.dataProvider.return_value.addFeatures.return_value = (True, [MagicMock()])
    monkeypatch.setattr(features, "QgsGeometry", MagicMock(**{"fromWkt.return_value": geom}))
    monkeypatch.setattr(features, "QgsFeature", MagicMock())
    monkeypatch.setattr(
        features,
        "QgsWkbTypes",
        MagicMock(
            **{"geometryDisplayString.side_effect": str, "hasZ.side_effect": lambda t: t == "z"}
        ),
    )
    return server, layer, geom


def test_add_features_refuses_a_geometry_the_layer_cannot_hold(adder, plugin_handlers):
    server, layer, _ = adder
    layer.geometryType.return_value = "point"

    with pytest.raises(plugin_handlers.base.CommandError, match="polygon geometry on a point"):
        server.add_features("lid", [{"geometry_wkt": "POLYGON((0 0,1 0,1 1,0 0))"}])

    layer.dataProvider.return_value.addFeatures.assert_not_called()


def test_add_features_accepts_any_geometry_on_a_generic_layer(adder, features, monkeypatch):
    """A GPKG GEOMETRY column reports Unknown and holds points, lines and polygons."""
    server, layer, _ = adder
    monkeypatch.setattr(features, "GEOM_UNKNOWN", "unknown")
    layer.geometryType.return_value = "unknown"

    assert server.add_features("lid", [{"geometry_wkt": "POINT(1 2)"}])["added"] == 1


def test_add_features_warns_about_invalid_and_2d_geometry(adder):
    """A bowtie (GEOS area 0) or 2D on a Z layer went in without a word."""
    server, layer, geom = adder
    geom.isGeosValid.return_value = False
    layer.wkbType.return_value = "z"
    geom.wkbType.return_value = "2d"

    result = server.add_features("lid", [{"geometry_wkt": "POLYGON((0 0,10 10,10 0,0 10,0 0))"}])

    assert len(result["warnings"]) == 2
    assert "invalid geometry" in result["warnings"][0]
    assert "2D geometry" in result["warnings"][1]


def test_add_features_reprojects_from_an_explicit_crs(
    adder, plugin_handlers, features, monkeypatch
):
    server, layer, geom = adder
    monkeypatch.setattr(plugin_handlers.base, "QgsCoordinateReferenceSystem", lambda s: Crs(s))
    monkeypatch.setattr(features, "QgsCoordinateTransform", FakeTransform)
    layer.crs.return_value = Crs("EPSG:2154")

    server.add_features(
        "lid", [{"geometry_wkt": "POLYGON((2 48,3 48,3 49,2 48))"}], crs="EPSG:4326"
    )

    (xform,) = geom.transform.call_args[0]
    assert (xform.src, xform.dst) == ("EPSG:4326", "EPSG:2154")


def test_add_features_without_crs_stores_the_wkt_as_given(adder):
    server, _, geom = adder

    result = server.add_features("lid", [{"geometry_wkt": "POLYGON((0 0,1 0,1 1,0 0))"}])

    geom.transform.assert_not_called()
    assert "warnings" not in result
