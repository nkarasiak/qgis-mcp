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


class Crs(str):
    """A CRS that compares by authid, like QgsCoordinateReferenceSystem."""

    def authid(self):
        return str(self)


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
