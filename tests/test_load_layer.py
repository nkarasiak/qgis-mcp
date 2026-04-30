"""Tests for qgis_load_layer — pragmatic, mocked-executor unit tests."""

from __future__ import annotations

import os

import pytest

from qgis_mcp_north.server import qgis_load_layer


def _vector_responses(layer_id: str = "L_x"):
    return {
        "add_vector_layer": {
            "id": layer_id, "name": "x", "type": "vector_2", "feature_count": 5,
        },
        "get_layer_info": {
            "id": layer_id, "name": "x", "type": "vector_2", "crs": "EPSG:4326",
            "extent": {"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1},
            "feature_count": 5, "geometry_type": 2,
            "fields": [{"name": "nam", "type": "String", "length": 80}],
            "is_valid": True, "source": "x.shp", "provider": "ogr",
        },
    }


def test_returns_loaded_layer_with_id_and_does_not_remove(fake_executor):
    """Critical contract: the layer stays loaded for downstream tools."""
    fake_executor.responses.update(_vector_responses("L_polbnda"))

    result = qgis_load_layer("/data/x.shp")

    assert result.layer_id == "L_polbnda"
    assert result.geometry_type == "polygon"
    assert result.crs == "EPSG:4326"
    assert result.path == os.path.abspath("/data/x.shp")

    commands = [c[0] for c in fake_executor.calls]
    assert commands == ["add_vector_layer", "get_layer_info"], (
        "load_layer must NOT remove — that's qgis_layer_inspect's job"
    )


def test_custom_name_passed_to_plugin(fake_executor):
    fake_executor.responses.update(_vector_responses())
    qgis_load_layer("/data/x.shp", name="My Custom Name")
    add_call_params = fake_executor.calls[0][1]
    assert add_call_params["name"] == "My Custom Name"


def test_no_name_means_no_name_param(fake_executor):
    fake_executor.responses.update(_vector_responses())
    qgis_load_layer("/data/x.shp")
    add_call_params = fake_executor.calls[0][1]
    assert "name" not in add_call_params, "plugin defaults to filename when name omitted"


def test_raster_dispatches_raster_loader(fake_executor):
    fake_executor.responses["add_raster_layer"] = {
        "id": "L_jaxa", "name": "jaxa", "type": "raster", "width": 100, "height": 100,
    }
    fake_executor.responses["get_layer_info"] = {
        "id": "L_jaxa", "name": "jaxa", "type": "raster", "crs": "EPSG:4326",
        "extent": {"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1},
        "is_valid": True, "source": "x.tif", "provider": "gdal",
        "width": 100, "height": 100, "band_count": 1,
    }

    result = qgis_load_layer("/data/x.tif")

    assert result.geometry_type == "raster"
    commands = [c[0] for c in fake_executor.calls]
    assert commands == ["add_raster_layer", "get_layer_info"]


def test_crs_override_dispatches_set_layer_crs(fake_executor):
    """v0.4: a crs override post-loads via set_layer_crs and the response reflects it."""
    fake_executor.responses.update(_vector_responses("L_x"))
    fake_executor.responses["set_layer_crs"] = {
        "ok": True, "layer_id": "L_x", "crs": "EPSG:3857",
    }

    result = qgis_load_layer("/data/x.shp", crs="EPSG:3857")

    assert result.crs == "EPSG:3857"
    commands = [c[0] for c in fake_executor.calls]
    assert commands == ["add_vector_layer", "get_layer_info", "set_layer_crs"]
    set_crs_params = fake_executor.calls[2][1]
    assert set_crs_params == {"layer_id": "L_x", "crs": "EPSG:3857"}


def test_crs_override_failure_cleans_up_partial_layer(fake_executor):
    """v0.4: if set_layer_crs fails, the orphaned layer is removed and CrsMismatchError raised."""
    from qgis_mcp_north.errors import CrsMismatchError, ExecutorError

    fake_executor.responses.update(_vector_responses("L_x"))

    def _raise_invalid_crs(_params):
        raise ExecutorError("set_layer_crs", "Invalid CRS: bogus")

    fake_executor.responses["set_layer_crs"] = _raise_invalid_crs
    fake_executor.responses["remove_layer"] = {"ok": True}

    with pytest.raises(CrsMismatchError, match="bogus"):
        qgis_load_layer("/data/x.shp", crs="EPSG:bogus")

    commands = [c[0] for c in fake_executor.calls]
    assert commands == [
        "add_vector_layer", "get_layer_info", "set_layer_crs", "remove_layer",
    ], "failed crs override must roll back the partially-loaded layer"
