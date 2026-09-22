"""Handlers that reported ok when QGIS had refused or ignored the request.

Each of these QGIS calls signals failure only through its return value (or a
None), and the handler used to drop it and answer ``ok: True``.
"""

import json
from unittest.mock import MagicMock

import pytest

from qgis_mcp.helpers import make_render_response


@pytest.fixture
def project(plugin_handlers, monkeypatch):
    """One mocked project instance shared by every handler module."""
    project = MagicMock()
    qgs_project = MagicMock(**{"instance.return_value": project})
    for module in ("base", "layers", "layout", "project", "features"):
        monkeypatch.setattr(getattr(plugin_handlers, module), "QgsProject", qgs_project)
    return project


@pytest.fixture
def vector(plugin_handlers, project):
    layer = MagicMock()
    layer.type.return_value = plugin_handlers.base.LAYER_VECTOR
    project.mapLayer.return_value = layer
    return layer


@pytest.fixture
def server(plugin_handlers):
    h = plugin_handlers

    class Server(
        h.layers.LayerHandlers,
        h.layout.LayoutHandlers,
        h.project.ProjectHandlers,
        h.features.FeatureHandlers,
        h.style.StyleHandlers,
        h.system.SystemHandlers,
        h.base.HandlerBase,
    ):
        LOG_TAG = "test"
        iface = MagicMock()

    return Server()


@pytest.fixture
def CommandError(plugin_handlers):
    return plugin_handlers.base.CommandError


def test_create_layout_refuses_a_duplicate_name(server, project, CommandError):
    project.layoutManager.return_value.addLayout.return_value = False

    with pytest.raises(CommandError, match="Could not add layout 'A'"):
        server.create_layout("A")


class FakeMap:
    def __init__(self, item_id):
        self._id = item_id

    def id(self):
        return self._id

    def uuid(self):
        return f"uuid-{self._id}"


@pytest.fixture
def layout_with_maps(plugin_handlers, monkeypatch):
    monkeypatch.setattr(plugin_handlers.layout, "QgsLayoutItemMap", FakeMap)
    layout = MagicMock()
    layout.items.return_value = [FakeMap("main"), FakeMap("inset")]
    return layout


def test_layout_map_lookup_refuses_an_unknown_id(server, layout_with_maps, CommandError):
    """Falling back to the first map linked the legend to a map nobody named."""
    with pytest.raises(CommandError, match="Map item not found"):
        server._find_layout_map(layout_with_maps, "typo")


def test_layout_map_lookup_finds_by_id_and_defaults_to_the_first(server, layout_with_maps):
    assert server._find_layout_map(layout_with_maps, "inset").id() == "inset"
    assert server._find_layout_map(layout_with_maps).id() == "main"


def test_atlas_filter_syntax_error_is_reported(server, project, vector, CommandError):
    layout = project.layoutManager.return_value.layoutByName.return_value
    layout.atlas.return_value.setFilterExpression.return_value = (False, "unexpected EQ")

    with pytest.raises(CommandError, match="unexpected EQ"):
        server.configure_atlas("L", "lid", filter_expression='"a" = = 1')


def test_table_join_refuses_a_missing_field(server, vector, CommandError):
    """addJoin accepts unknown fields, and every joined column then reads NULL."""
    vector.fields.return_value.indexOf.return_value = -1

    with pytest.raises(CommandError, match="Field not found"):
        server.add_table_join("t", "j", "code", "kode")

    vector.addJoin.assert_not_called()


def test_reload_plugin_reports_a_plugin_that_failed_to_start(
    server, plugin_handlers, monkeypatch, CommandError
):
    monkeypatch.setattr(plugin_handlers.system, "active_plugins", ["broken"])
    monkeypatch.setattr(plugin_handlers.system, "reloadPlugin", lambda name: False)

    with pytest.raises(CommandError, match="failed to start"):
        server.reload_plugin("broken")


def test_remove_bookmark_refuses_an_unknown_id(server, project, CommandError):
    project.bookmarkManager.return_value.removeBookmark.return_value = False

    with pytest.raises(CommandError, match="Bookmark not found"):
        server.remove_bookmark("nope")


def test_unknown_color_ramp_is_refused(server, plugin_handlers, monkeypatch, CommandError):
    """It fell back to Spectral/Viridis while the response echoed the requested name."""
    style = MagicMock()
    style.colorRamp.return_value = None
    style.colorRampNames.return_value = ["Spectral", "Viridis"]
    monkeypatch.setattr(
        plugin_handlers.style, "QgsStyle", MagicMock(**{"defaultStyle.return_value": style})
    )

    with pytest.raises(CommandError, match="Unknown color ramp: 'Spectrall'"):
        server._color_ramp("Spectrall")


@pytest.mark.parametrize("call", ["add_field", "field_calculator"])
def test_unknown_field_type_is_refused(server, vector, CommandError, call):
    """An unknown type became a string (add_field) or double (field_calculator) field."""
    vector.fields.return_value.indexOf.return_value = -1
    with pytest.raises(CommandError, match="Unknown field_type"):
        if call == "add_field":
            server.add_field("lid", "f", "integer64")
        else:
            server.field_calculator("lid", "f", "1", field_type="integer64")

    vector.dataProvider.return_value.addAttributes.assert_not_called()


def test_validate_expression_refuses_an_unknown_layer(server, project, plugin_handlers):
    """Skipping it dropped the column check and answered valid: True."""
    project.mapLayer.return_value = None

    with pytest.raises(plugin_handlers.base.LayerNotFound):
        server.validate_expression("1 = 1", layer_id="nope")


def test_validate_expression_is_invalid_when_the_column_check_fails(
    server, vector, plugin_handlers, monkeypatch
):
    expr = MagicMock()
    expr.hasParserError.return_value = False
    expr.hasEvalError.return_value = True
    expr.evalErrorString.return_value = "Field 'nmae' not found"
    monkeypatch.setattr(plugin_handlers.features, "QgsExpression", lambda text: expr)

    result = server.validate_expression('"nmae" = 1', layer_id="lid")

    assert result["valid"] is False
    assert result["eval_error"] == "Field 'nmae' not found"


def test_render_warnings_reach_the_caller():
    """A layer that failed to draw leaves a blank area in a clean-looking image."""
    content = make_render_response(
        {"base64_data": "AAAA", "warnings": ["wms_1: Connection refused"]}, 800, 600, None
    )

    assert json.loads(content[1].text)["warnings"] == ["wms_1: Connection refused"]


def test_render_without_path_or_warnings_is_just_the_image():
    assert len(make_render_response({"base64_data": "AAAA"}, 800, 600, None)) == 1
