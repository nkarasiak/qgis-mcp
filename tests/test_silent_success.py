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


def test_unknown_field_type_is_refused(server, vector, CommandError):
    """An unknown type became a string field (field_calculator: test_feature_handlers)."""
    vector.fields.return_value.indexOf.return_value = -1
    with pytest.raises(CommandError, match="Unknown field_type"):
        server.add_field("lid", "f", "integer64")

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


# --- failures carry the reason QGIS gave ---------------------------------------


def _layer_with_errors(layer_error, provider_error):
    layer = MagicMock()
    layer.error.return_value.summary.return_value = layer_error
    layer.dataProvider.return_value.error.return_value.summary.return_value = provider_error
    return layer


@pytest.mark.parametrize(
    ("layer_error", "provider_error", "expected"),
    [
        (
            "Cannot open GDAL dataset x",
            "Cannot open GDAL dataset x",
            ": Cannot open GDAL dataset x",
        ),
        ("", "Referenced table t in query not found!", ": Referenced table t in query not found!"),
        ("", "", ""),
    ],
)
def test_load_error_reports_what_qgis_recorded(server, layer_error, provider_error, expected):
    assert server._load_error(_layer_with_errors(layer_error, provider_error)) == expected


def test_execute_sql_failure_names_the_missing_table(server, plugin_handlers, vector, monkeypatch):
    vlayer = _layer_with_errors("", "Referenced table missingtable in query not found!")
    vlayer.isValid.return_value = False
    monkeypatch.setattr(plugin_handlers.features, "QgsVectorLayer", lambda *a: vlayer)

    with pytest.raises(plugin_handlers.base.CommandError, match="missingtable in query not found"):
        server.execute_sql("SELECT * FROM missingtable", layers=["lid"])


def test_save_project_failure_carries_project_error(server, project, CommandError):
    project.write.return_value = False
    project.error.return_value = "Unable to open file for writing"

    with pytest.raises(CommandError, match="Unable to open file for writing"):
        server.save_project("/ro/p.qgz")


def test_layout_export_failure_carries_exporter_message(
    server, project, plugin_handlers, monkeypatch, CommandError
):
    exporter = MagicMock()
    exporter.exportToPdf.return_value = "FileError"
    exporter.errorMessage.return_value = "Cannot write to /ro/out.pdf"
    exporter_cls = MagicMock(return_value=exporter)
    monkeypatch.setattr(plugin_handlers.layout, "QgsLayoutExporter", exporter_cls)

    with pytest.raises(CommandError, match="Cannot write to /ro/out.pdf"):
        server.export_layout("L", "/ro/out.pdf")


def test_provider_write_failure_carries_the_providers_own_error(server, vector, CommandError):
    dp = vector.dataProvider.return_value
    dp.addAttributes.return_value = False
    dp.hasErrors.return_value = True
    dp.errors.return_value = ["OGR error creating field f: read-only"]

    with pytest.raises(CommandError, match="OGR error creating field f: read-only"):
        server.add_field("lid", "f", "string")

    dp.clearErrors.assert_called_once()  # a stale error must not be blamed on this write


# --- silent fallbacks left from the review ----------------------------------------


def test_delete_features_refuses_fids_and_expression_together(server, vector, CommandError):
    """fids used to win silently, deleting a set the caller had not filtered."""
    with pytest.raises(CommandError, match="not both"):
        server.delete_features("lid", fids=[1, 2], expression='"a" = 1')

    vector.dataProvider.return_value.deleteFeatures.assert_not_called()


def test_identify_refuses_an_explicit_raster(server, vector, plugin_handlers):
    """An explicit raster id was skipped, answering "nothing here"."""
    vector.type.return_value = plugin_handlers.base.LAYER_RASTER

    with pytest.raises(plugin_handlers.base.WrongLayerType):
        server.identify_features([0.0, 0.0], layer_ids=["dem"])


def test_execute_sql_refuses_two_layers_with_one_name(
    server, project, plugin_handlers, monkeypatch, CommandError
):
    """Both register as one table name and the query reads either, unannounced."""
    layers = {}
    for lid in ("a", "b"):
        lyr = MagicMock()
        lyr.type.return_value = plugin_handlers.base.LAYER_VECTOR
        lyr.name.return_value = "roads"
        layers[lid] = lyr
    project.mapLayer.side_effect = layers.get

    with pytest.raises(CommandError, match="named 'roads'"):
        server.execute_sql("SELECT * FROM roads", layers=["a", "b"])


@pytest.mark.parametrize("valid", [True, False])
def test_added_layer_reports_its_crs_and_warns_without_one(
    server, plugin_handlers, monkeypatch, valid
):
    layer = MagicMock()
    layer.isValid.return_value = True
    layer.isSpatial.return_value = True
    layer.crs.return_value.isValid.return_value = valid
    layer.crs.return_value.authid.return_value = "EPSG:2154" if valid else ""
    monkeypatch.setattr(plugin_handlers.layers, "QgsRasterLayer", lambda *a: layer)

    result = server.add_raster_layer("/data/scan.tif")

    assert result["crs"] == ("EPSG:2154" if valid else "")
    assert ("warning" in result) is (not valid)


class FakeGroup:
    def __init__(self, name, *children):
        self._name, self._children = name, list(children)

    def name(self):
        return self._name

    def children(self):
        return self._children


@pytest.fixture
def tree(plugin_handlers, project, monkeypatch):
    monkeypatch.setattr(plugin_handlers.layers, "QgsLayerTreeGroup", FakeGroup)
    root = FakeGroup("", FakeGroup("roads"), FakeGroup("admin", FakeGroup("roads")))
    project.layerTreeRoot.return_value = root
    return root


def test_group_lookup_refuses_a_name_two_groups_share(server, tree, CommandError):
    """findGroup() took the first, so the layer went to whichever the tree listed first."""
    with pytest.raises(CommandError, match="2 groups are named 'roads'"):
        server._group(tree, "roads", "Group")


def test_group_lookup_finds_a_unique_nested_group(server, tree, CommandError):
    assert server._group(tree, "admin", "Group").name() == "admin"
    with pytest.raises(CommandError, match="Group not found: water"):
        server._group(tree, "water", "Group")


# --- execute_connection_sql -------------------------------------------------------


class FakeResult:
    def __init__(self, columns, rows):
        self._columns, self._rows = columns, list(rows)

    def columns(self):
        return self._columns

    def hasNextRow(self):
        return bool(self._rows)

    def nextRow(self):
        return self._rows.pop(0)


@pytest.fixture
def db(plugin_handlers, monkeypatch):
    class Server(plugin_handlers.connections.ConnectionHandlers, plugin_handlers.base.HandlerBase):
        LOG_TAG = "test"

    server = Server()
    conn = MagicMock()
    conn.capabilities.return_value = plugin_handlers.connections.CONN_CAP_EXECUTE_SQL
    monkeypatch.setattr(server, "_connection", lambda provider, name: conn)
    return server, conn


def test_connection_sql_names_its_columns(db):
    """Bare row lists left SELECT * output to positional guessing."""
    server, conn = db
    conn.execSql.return_value = FakeResult(["name", "pop"], [["a", 1], ["b", 2], ["c", 3]])

    result = server.execute_connection_sql("postgres", "db", "SELECT * FROM t", limit=2)

    assert result["columns"] == ["name", "pop"]
    assert result["rows"] == [["a", 1], ["b", 2]]
    assert result["truncated"] is True


def test_connection_sql_error_is_a_user_error(db, plugin_handlers, monkeypatch):
    server, conn = db

    class ConnError(Exception):
        pass

    monkeypatch.setattr(plugin_handlers.connections, "QgsProviderConnectionException", ConnError)
    conn.execSql.side_effect = ConnError("no such table: t")

    with pytest.raises(plugin_handlers.base.CommandError, match="SQL failed: no such table"):
        server.execute_connection_sql("postgres", "db", "SELECT * FROM t")
