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
