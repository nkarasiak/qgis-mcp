import os
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
    # Invalid expression should return an error
    assert resp["status"] == "error"


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
