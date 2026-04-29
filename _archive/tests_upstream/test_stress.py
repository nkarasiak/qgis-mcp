"""Stress tests for QGIS MCP — run before cutting a release.

Requires a running QGIS instance with the MCP plugin enabled on localhost:9876.
Exercises all tool categories end-to-end: project, layers, features, styling,
rendering, processing, selection, bookmarks, layer tree, variables, transforms,
batch, plugins, and edge cases (serialization, large payloads, error recovery).

Usage:
    uv run --no-sync pytest tests/test_stress.py -v
"""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from conftest import make_client
from qgis_mcp.helpers import TIMEOUT_LONG


# ---------------------------------------------------------------------------
# 1. System & connectivity
# ---------------------------------------------------------------------------


class TestSystem:
    def test_ping(self, client):
        resp = client.send_command("ping")
        assert resp["status"] == "success"
        assert resp["result"]["pong"] is True

    def test_diagnose(self, client):
        resp = client.send_command("diagnose")
        assert resp["status"] == "success"
        result = resp["result"]
        assert result["status"] in ("healthy", "degraded")
        check_names = [c["name"] for c in result["checks"]]
        assert "qgis" in check_names
        assert "plugin_version" in check_names

    def test_get_qgis_info(self, client):
        resp = client.send_command("get_qgis_info")
        assert resp["status"] == "success"
        assert "qgis_version" in resp["result"]
        assert "profile_folder" in resp["result"]


# ---------------------------------------------------------------------------
# 2. Project operations
# ---------------------------------------------------------------------------


class TestProject:
    def test_get_project_info(self, client, test_project):
        resp = client.send_command("get_project_info")
        assert resp["status"] == "success"
        assert "crs" in resp["result"]
        assert "layer_count" in resp["result"]

    def test_save_project(self, client, test_project):
        resp = client.send_command("save_project")
        assert resp["status"] == "success"
        assert "saved" in resp["result"]

    def test_set_and_get_project_variable(self, client, test_project):
        resp = client.send_command("set_project_variable", {"key": "stress_run", "value": "yes"})
        assert resp["status"] == "success"

        resp = client.send_command("get_project_variables")
        assert resp["status"] == "success"
        assert resp["result"]["variables"]["stress_run"] == "yes"

    def test_project_variables_serializable(self, client, test_project):
        """Regression: QVariant/QDateTime must be JSON-serializable."""
        resp = client.send_command("get_project_variables")
        assert resp["status"] == "success"
        variables = resp["result"]["variables"]
        # project_creation_date is a QDateTime internally
        assert "project_creation_date" in variables
        assert isinstance(variables["project_creation_date"], str)


# ---------------------------------------------------------------------------
# 3. Layer management
# ---------------------------------------------------------------------------


class TestLayers:
    def test_get_layers(self, client, cities_layer):
        resp = client.send_command("get_layers")
        assert resp["status"] == "success"
        ids = [lyr["id"] for lyr in resp["result"]["layers"]]
        assert cities_layer in ids

    def test_get_layers_pagination(self, client, cities_layer):
        resp = client.send_command("get_layers", {"limit": 1, "offset": 0})
        assert resp["status"] == "success"
        assert len(resp["result"]["layers"]) <= 1
        assert resp["result"]["total_count"] >= 1

    def test_find_layer(self, client, cities_layer):
        resp = client.send_command("find_layer", {"name_pattern": "test_cities*"})
        assert resp["status"] == "success"
        assert resp["result"]["count"] >= 1

    def test_zoom_to_layer(self, client, cities_layer):
        resp = client.send_command("zoom_to_layer", {"layer_id": cities_layer})
        assert resp["status"] == "success"

    def test_get_layer_extent(self, client, cities_layer):
        resp = client.send_command("get_layer_extent", {"layer_id": cities_layer})
        assert resp["status"] == "success"
        result = resp["result"]
        assert result["xmin"] < result["xmax"]
        assert result["ymin"] < result["ymax"]

    def test_set_layer_visibility(self, client, cities_layer):
        resp = client.send_command("set_layer_visibility", {"layer_id": cities_layer, "visible": False})
        assert resp["status"] == "success"
        assert resp["result"]["visible"] is False

        resp = client.send_command("set_layer_visibility", {"layer_id": cities_layer, "visible": True})
        assert resp["status"] == "success"
        assert resp["result"]["visible"] is True

    def test_set_layer_property_opacity(self, client, cities_layer):
        resp = client.send_command(
            "set_layer_property", {"layer_id": cities_layer, "property": "opacity", "value": 0.5}
        )
        assert resp["status"] == "success"
        # Restore
        client.send_command(
            "set_layer_property", {"layer_id": cities_layer, "property": "opacity", "value": 1.0}
        )


# ---------------------------------------------------------------------------
# 4. Features
# ---------------------------------------------------------------------------


class TestFeatures:
    def test_get_features_all(self, client, cities_layer):
        resp = client.send_command(
            "get_layer_features", {"layer_id": cities_layer, "limit": 50}
        )
        assert resp["status"] == "success"
        assert len(resp["result"]["features"]) == 20

    def test_get_features_with_geometry(self, client, cities_layer):
        resp = client.send_command(
            "get_layer_features", {"layer_id": cities_layer, "limit": 1, "include_geometry": True}
        )
        assert resp["status"] == "success"
        feature = resp["result"]["features"][0]
        assert "_geometry" in feature
        assert "wkt" in feature["_geometry"]

    def test_expression_filter(self, client, cities_layer):
        resp = client.send_command(
            "get_layer_features",
            {"layer_id": cities_layer, "expression": "population > 10000000", "limit": 50},
        )
        assert resp["status"] == "success"
        features = resp["result"]["features"]
        assert len(features) == 8
        for f in features:
            assert f["population"] > 10000000

    def test_pagination(self, client, cities_layer):
        resp1 = client.send_command(
            "get_layer_features", {"layer_id": cities_layer, "limit": 5, "offset": 0}
        )
        resp2 = client.send_command(
            "get_layer_features", {"layer_id": cities_layer, "limit": 5, "offset": 5}
        )
        assert resp1["status"] == "success"
        assert resp2["status"] == "success"
        fids1 = {f["_fid"] for f in resp1["result"]["features"]}
        fids2 = {f["_fid"] for f in resp2["result"]["features"]}
        assert fids1.isdisjoint(fids2), "Pages should not overlap"

    def test_field_statistics_numeric(self, client, cities_layer):
        resp = client.send_command(
            "get_field_statistics", {"layer_id": cities_layer, "field_name": "population"}
        )
        assert resp["status"] == "success"
        result = resp["result"]
        assert result["is_numeric"] is True
        assert result["count"] == 20
        assert result["min"] == 2161000
        assert result["max"] == 21540000

    def test_field_statistics_string(self, client, cities_layer):
        resp = client.send_command(
            "get_field_statistics", {"layer_id": cities_layer, "field_name": "country"}
        )
        assert resp["status"] == "success"
        assert resp["result"]["is_numeric"] is False
        assert resp["result"]["count"] == 20

    def test_update_features(self, client, cities_layer):
        # Get Paris fid
        resp = client.send_command(
            "get_layer_features",
            {"layer_id": cities_layer, "expression": "\"name\" = 'Paris'", "limit": 1},
        )
        fid = resp["result"]["features"][0]["_fid"]

        resp = client.send_command(
            "update_features",
            {"layer_id": cities_layer, "updates": [{"fid": fid, "attributes": {"population": 2200000}}]},
        )
        assert resp["status"] == "success"
        assert resp["result"]["updated"] == 1

        # Verify & restore
        resp = client.send_command(
            "get_layer_features",
            {"layer_id": cities_layer, "expression": f"$id = {fid}", "limit": 1},
        )
        assert resp["result"]["features"][0]["population"] == 2200000
        client.send_command(
            "update_features",
            {"layer_id": cities_layer, "updates": [{"fid": fid, "attributes": {"population": 2161000}}]},
        )

    def test_add_and_delete_features(self, client, cities_layer):
        resp = client.send_command(
            "add_features",
            {
                "layer_id": cities_layer,
                "features": [
                    {"attributes": {"name": "TempCity", "population": 1, "country": "Test"}, "geometry_wkt": "POINT(0 0)"},
                ],
            },
        )
        assert resp["status"] == "success"
        assert resp["result"]["added"] == 1

        resp = client.send_command(
            "delete_features", {"layer_id": cities_layer, "expression": "\"name\" = 'TempCity'"}
        )
        assert resp["status"] == "success"
        assert resp["result"]["deleted"] == 1


# ---------------------------------------------------------------------------
# 5. Selection
# ---------------------------------------------------------------------------


class TestSelection:
    def test_select_by_expression(self, client, cities_layer):
        resp = client.send_command(
            "select_features", {"layer_id": cities_layer, "expression": "country = 'France' OR country = 'Germany'"}
        )
        assert resp["status"] == "success"
        assert resp["result"]["selected"] == 2

    def test_get_selection(self, client, cities_layer):
        resp = client.send_command("get_selection", {"layer_id": cities_layer})
        assert resp["status"] == "success"
        assert resp["result"]["count"] == 2

    def test_clear_selection(self, client, cities_layer):
        resp = client.send_command("clear_selection", {"layer_id": cities_layer})
        assert resp["status"] == "success"

        resp = client.send_command("get_selection", {"layer_id": cities_layer})
        assert resp["result"]["count"] == 0


# ---------------------------------------------------------------------------
# 6. Styling & labeling
# ---------------------------------------------------------------------------


class TestStyling:
    def test_graduated_style(self, client, cities_layer):
        resp = client.send_command(
            "set_layer_style",
            {"layer_id": cities_layer, "style_type": "graduated", "field": "population", "classes": 5, "color_ramp": "Viridis"},
        )
        assert resp["status"] == "success"

    def test_categorized_style(self, client, cities_layer):
        resp = client.send_command(
            "set_layer_style",
            {"layer_id": cities_layer, "style_type": "categorized", "field": "country"},
        )
        assert resp["status"] == "success"

    def test_single_style(self, client, cities_layer):
        resp = client.send_command(
            "set_layer_style", {"layer_id": cities_layer, "style_type": "single"}
        )
        assert resp["status"] == "success"

    def test_labeling_on_off(self, client, cities_layer):
        resp = client.send_command(
            "set_layer_labeling",
            {"layer_id": cities_layer, "field_name": "name", "font_size": 10, "color": "#222222"},
        )
        assert resp["status"] == "success"
        assert resp["result"]["enabled"] is True

        resp = client.send_command(
            "set_layer_labeling", {"layer_id": cities_layer, "enabled": False}
        )
        assert resp["status"] == "success"
        assert resp["result"]["enabled"] is False

    def test_get_layer_labeling(self, client, cities_layer):
        resp = client.send_command("get_layer_labeling", {"layer_id": cities_layer})
        assert resp["status"] == "success"


# ---------------------------------------------------------------------------
# 7. Canvas & rendering
# ---------------------------------------------------------------------------


class TestCanvas:
    def test_get_canvas_extent(self, client):
        resp = client.send_command("get_canvas_extent")
        assert resp["status"] == "success"
        for key in ("xmin", "ymin", "xmax", "ymax", "crs"):
            assert key in resp["result"]

    def test_set_canvas_extent(self, client):
        resp = client.send_command("set_canvas_extent", {"xmin": -10, "ymin": 35, "xmax": 40, "ymax": 60})
        assert resp["status"] == "success"

    def test_get_canvas_scale(self, client):
        resp = client.send_command("get_canvas_scale")
        assert resp["status"] == "success"

    def test_render_map(self, client, cities_layer):
        resp = client.send_command("render_map_base64", {"width": 400, "height": 300}, timeout=60)
        assert resp["status"] == "success"
        assert len(resp["result"]["base64_data"]) > 100

    def test_get_canvas_screenshot(self, client):
        resp = client.send_command("get_canvas_screenshot")
        assert resp["status"] == "success"
        assert len(resp["result"]["base64_data"]) > 100


# ---------------------------------------------------------------------------
# 8. Processing
# ---------------------------------------------------------------------------


class TestProcessing:
    def test_list_algorithms(self, client):
        resp = client.send_command("list_processing_algorithms", {"search": "buffer"})
        assert resp["status"] == "success"
        assert resp["result"]["count"] >= 1

    def test_get_algorithm_help(self, client):
        resp = client.send_command("get_algorithm_help", {"algorithm_id": "native:buffer"})
        assert resp["status"] == "success"
        assert "parameters" in resp["result"]

    def test_execute_buffer(self, client, cities_layer):
        resp = client.send_command(
            "execute_processing",
            {
                "algorithm": "native:buffer",
                "parameters": {"INPUT": cities_layer, "DISTANCE": 1, "SEGMENTS": 5, "OUTPUT": "memory:"},
            },
            timeout=60,
        )
        assert resp["status"] == "success"
        assert "OUTPUT" in resp["result"]["result"]

        # Clean up the buffer output layer (it's added to the project)
        layers_resp = client.send_command("find_layer", {"name_pattern": "output*"})
        if layers_resp["status"] == "success":
            for lyr in layers_resp["result"].get("layers", []):
                client.send_command("remove_layer", {"layer_id": lyr["id"]})


# ---------------------------------------------------------------------------
# 9. Layer tree & groups
# ---------------------------------------------------------------------------


class TestLayerTree:
    def test_get_layer_tree(self, client, cities_layer):
        resp = client.send_command("get_layer_tree")
        assert resp["status"] == "success"
        assert "children" in resp["result"]

    def test_create_group_and_move(self, client, cities_layer):
        group_name = f"stress_group_{uuid.uuid4().hex[:6]}"
        resp = client.send_command("create_layer_group", {"name": group_name})
        assert resp["status"] == "success"

        resp = client.send_command(
            "move_layer_to_group", {"layer_id": cities_layer, "group_name": group_name}
        )
        assert resp["status"] == "success"

        # Verify in tree
        resp = client.send_command("get_layer_tree")
        tree = resp["result"]
        group = next((c for c in tree["children"] if c.get("name") == group_name), None)
        assert group is not None
        layer_ids = [c["layer_id"] for c in group.get("children", []) if c["type"] == "layer"]
        assert cities_layer in layer_ids


# ---------------------------------------------------------------------------
# 10. Bookmarks
# ---------------------------------------------------------------------------


class TestBookmarks:
    def test_add_get_remove_bookmark(self, client, test_project):
        name = f"stress_bm_{uuid.uuid4().hex[:6]}"
        resp = client.send_command(
            "add_bookmark", {"name": name, "xmin": -10, "ymin": 35, "xmax": 40, "ymax": 60}
        )
        assert resp["status"] == "success"
        bm_id = resp["result"]["id"]

        resp = client.send_command("get_bookmarks")
        assert resp["status"] == "success"
        names = [b["name"] for b in resp["result"]["bookmarks"]]
        assert name in names

        resp = client.send_command("remove_bookmark", {"bookmark_id": bm_id})
        assert resp["status"] == "success"


# ---------------------------------------------------------------------------
# 11. CRS & transforms
# ---------------------------------------------------------------------------


class TestTransforms:
    def test_transform_single_point(self, client):
        resp = client.send_command(
            "transform_coordinates",
            {"source_crs": "EPSG:4326", "target_crs": "EPSG:3857", "point": {"x": 2.35, "y": 48.86}},
        )
        assert resp["status"] == "success"
        pt = resp["result"]["point"]
        assert abs(pt["x"] - 261600.8) < 1
        assert abs(pt["y"] - 6251139.6) < 1

    def test_transform_multiple_points(self, client):
        resp = client.send_command(
            "transform_coordinates",
            {
                "source_crs": "EPSG:4326",
                "target_crs": "EPSG:3857",
                "points": [{"x": 0, "y": 0}, {"x": 10, "y": 10}],
            },
        )
        assert resp["status"] == "success"
        assert len(resp["result"]["points"]) == 2

    def test_transform_bbox(self, client):
        resp = client.send_command(
            "transform_coordinates",
            {
                "source_crs": "EPSG:4326",
                "target_crs": "EPSG:3857",
                "bbox": {"xmin": -10, "ymin": 35, "xmax": 40, "ymax": 60},
            },
        )
        assert resp["status"] == "success"
        bbox = resp["result"]["bbox"]
        assert bbox["xmin"] < bbox["xmax"]

    def test_get_layer_crs(self, client, cities_layer):
        resp = client.send_command("get_layer_crs", {"layer_id": cities_layer})
        assert resp["status"] == "success"
        assert resp["result"].get("authid") == "EPSG:4326"

    def test_validate_expression(self, client, cities_layer):
        resp = client.send_command(
            "validate_expression", {"expression": "population > 1000000", "layer_id": cities_layer}
        )
        assert resp["status"] == "success"
        assert resp["result"]["valid"] is True
        assert "population" in resp["result"]["referenced_columns"]

    def test_validate_expression_invalid(self, client):
        resp = client.send_command("validate_expression", {"expression": "))) bad ((("})
        assert resp["status"] == "success"
        assert resp["result"]["valid"] is False


# ---------------------------------------------------------------------------
# 12. Plugins & message log
# ---------------------------------------------------------------------------


class TestPlugins:
    def test_list_plugins(self, client):
        resp = client.send_command("list_plugins", {"enabled_only": True})
        assert resp["status"] == "success"
        names = [p["name"] for p in resp["result"]["plugins"]]
        assert "qgis_mcp_plugin" in names

    def test_get_plugin_info(self, client):
        resp = client.send_command("get_plugin_info", {"plugin_name": "qgis_mcp_plugin"})
        assert resp["status"] == "success"
        assert "version" in resp["result"]

    def test_get_message_log(self, client):
        resp = client.send_command("get_message_log", {"tag": "MCP", "limit": 10})
        assert resp["status"] == "success"
        assert len(resp["result"]["messages"]) > 0


# ---------------------------------------------------------------------------
# 13. Batch commands
# ---------------------------------------------------------------------------


class TestBatch:
    def test_batch_multiple_commands(self, client, cities_layer):
        resp = client.send_command(
            "batch",
            {
                "commands": [
                    {"type": "ping", "params": {}},
                    {"type": "get_project_info", "params": {}},
                    {"type": "get_canvas_extent", "params": {}},
                    {"type": "get_project_variables", "params": {}},
                    {"type": "get_bookmarks", "params": {}},
                ]
            },
            timeout=60,
        )
        assert resp["status"] == "success"
        results = resp["result"]
        assert len(results) == 5
        assert all(r["status"] == "success" for r in results)

    def test_batch_with_many_commands(self, client):
        """Batch with 10 read-only commands should all succeed."""
        commands = [{"type": "ping", "params": {}} for _ in range(10)]
        resp = client.send_command("batch", {"commands": commands}, timeout=TIMEOUT_LONG)
        assert resp["status"] == "success"
        results = resp["result"]
        assert len(results) == 10
        assert all(r["status"] == "success" for r in results)


# ---------------------------------------------------------------------------
# 14. Map themes
# ---------------------------------------------------------------------------


class TestMapThemes:
    def test_add_list_remove_theme(self, client, cities_layer):
        name = f"stress_theme_{uuid.uuid4().hex[:6]}"
        resp = client.send_command("add_map_theme", {"name": name})
        assert resp["status"] == "success"

        resp = client.send_command("get_map_themes")
        assert resp["status"] == "success"
        names = [t["name"] for t in resp["result"]["themes"]]
        assert name in names

        resp = client.send_command("remove_map_theme", {"name": name})
        assert resp["status"] == "success"


# ---------------------------------------------------------------------------
# 15. Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_get_setting(self, client):
        resp = client.send_command("get_setting", {"key": "locale/userLocale"})
        assert resp["status"] == "success"

    def test_set_setting_roundtrip(self, client):
        key = "qgis_mcp/stress_test_setting"
        resp = client.send_command("set_setting", {"key": key, "value": "hello"})
        assert resp["status"] == "success"

        resp = client.send_command("get_setting", {"key": key})
        assert resp["status"] == "success"
        assert resp["result"]["value"] == "hello"


# ---------------------------------------------------------------------------
# 16. Edge cases & error handling
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_invalid_layer_id(self, client):
        resp = client.send_command("get_layer_features", {"layer_id": "nonexistent", "limit": 1})
        assert resp["status"] == "error"

    def test_invalid_expression_returns_empty(self, client, cities_layer):
        """Invalid expressions return success with 0 features (QGIS silently filters)."""
        resp = client.send_command(
            "get_layer_features",
            {"layer_id": cities_layer, "expression": "))) INVALID ((((", "limit": 1},
        )
        assert resp["status"] == "success"
        assert len(resp["result"]["features"]) == 0

    def test_large_payload(self, client):
        """Ensure large responses don't break the socket framing."""
        code = 'data = "X" * 200000\nprint(data)'
        resp = client.send_command("execute_code", {"code": code}, timeout=60)
        assert resp["status"] == "success"
        assert len(resp["result"]["stdout"]) >= 200000

    def test_unicode_roundtrip(self, client, cities_layer):
        """Ensure non-ASCII data survives the JSON-over-socket round-trip."""
        resp = client.send_command(
            "get_layer_features",
            {"layer_id": cities_layer, "expression": "\"name\" = 'São Paulo'", "limit": 1},
        )
        assert resp["status"] == "success"
        assert resp["result"]["features"][0]["name"] == "São Paulo"

    def test_rapid_fire_commands(self, client):
        """Send 50 pings in quick succession."""
        for _ in range(50):
            resp = client.send_command("ping")
            assert resp["status"] == "success"

    def test_connection_recovery(self, client, cities_layer):
        """After an error, subsequent commands should still work."""
        # Trigger an error
        client.send_command("get_layer_features", {"layer_id": "nonexistent"})
        # Next command should work fine
        resp = client.send_command("ping")
        assert resp["status"] == "success"


# ---------------------------------------------------------------------------
# 17. Concurrent clients
# ---------------------------------------------------------------------------


class TestConcurrentClients:
    """Multiple TCP connections hitting the plugin simultaneously."""

    def test_two_clients_interleaved(self, client, cities_layer):
        """Two clients sending commands in alternation."""
        c2 = make_client()
        try:
            r1 = client.send_command("ping")
            r2 = c2.send_command("ping")
            assert r1["status"] == "success"
            assert r2["status"] == "success"

            r1 = client.send_command("get_layer_features", {"layer_id": cities_layer, "limit": 5})
            r2 = c2.send_command("get_layer_features", {"layer_id": cities_layer, "limit": 5})
            assert r1["status"] == "success"
            assert r2["status"] == "success"
            assert len(r1["result"]["features"]) == 5
            assert len(r2["result"]["features"]) == 5
        finally:
            c2.disconnect()

    def test_five_concurrent_pings(self):
        """Five clients each sending a ping in parallel threads."""
        def ping_once():
            c = make_client()
            try:
                return c.send_command("ping")
            finally:
                c.disconnect()

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(ping_once) for _ in range(5)]
            results = [f.result() for f in as_completed(futures)]

        assert len(results) == 5
        assert all(r["status"] == "success" for r in results)

    def test_concurrent_reads(self, cities_layer):
        """Five clients reading features simultaneously."""
        def read_features():
            c = make_client()
            try:
                return c.send_command(
                    "get_layer_features",
                    {"layer_id": cities_layer, "limit": 20},
                )
            finally:
                c.disconnect()

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(read_features) for _ in range(5)]
            results = [f.result() for f in as_completed(futures)]

        assert all(r["status"] == "success" for r in results)
        assert all(len(r["result"]["features"]) == 20 for r in results)

    def test_concurrent_mixed_operations(self, cities_layer):
        """Parallel clients doing different operations (reads + canvas + stats)."""
        def op_features():
            c = make_client()
            try:
                return c.send_command("get_layer_features", {"layer_id": cities_layer, "limit": 10})
            finally:
                c.disconnect()

        def op_extent():
            c = make_client()
            try:
                return c.send_command("get_canvas_extent")
            finally:
                c.disconnect()

        def op_stats():
            c = make_client()
            try:
                return c.send_command("get_field_statistics", {"layer_id": cities_layer, "field_name": "population"})
            finally:
                c.disconnect()

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = []
            for op in [op_features, op_extent, op_stats] * 2:
                futures.append(pool.submit(op))
            results = [f.result() for f in as_completed(futures)]

        assert all(r["status"] == "success" for r in results)

    def test_client_disconnect_no_impact(self, client):
        """A second client connecting and disconnecting shouldn't affect the first."""
        r1 = client.send_command("ping")
        assert r1["status"] == "success"

        c2 = make_client()
        c2.send_command("ping")
        c2.disconnect()

        r2 = client.send_command("ping")
        assert r2["status"] == "success"


# ---------------------------------------------------------------------------
# 18. Heavy load
# ---------------------------------------------------------------------------


class TestHeavyLoad:
    """Sustained throughput and large data volume tests."""

    def test_200_sequential_commands(self, client, cities_layer):
        """200 commands on a single connection — throughput and stability."""
        t0 = time.perf_counter()
        for i in range(200):
            if i % 3 == 0:
                resp = client.send_command("ping")
            elif i % 3 == 1:
                resp = client.send_command("get_canvas_extent")
            else:
                resp = client.send_command(
                    "get_layer_features", {"layer_id": cities_layer, "limit": 1}
                )
            assert resp["status"] == "success", f"Failed at iteration {i}: {resp}"
        elapsed = time.perf_counter() - t0
        # Should complete well within 30s on loopback
        assert elapsed < 30, f"200 commands took {elapsed:.1f}s — too slow"

    def test_bulk_feature_insert_and_delete(self, client, test_project):
        """Insert 500 features, verify, then delete them all."""
        layer_name = f"bulk_{uuid.uuid4().hex[:6]}"
        resp = client.send_command(
            "create_memory_layer",
            {
                "name": layer_name,
                "geometry_type": "Point",
                "crs": "EPSG:4326",
                "fields": [{"name": "idx", "type": "integer"}],
            },
        )
        assert resp["status"] == "success"
        layer_id = resp["result"]["id"]

        # Insert in batches of 100
        total = 500
        batch_size = 100
        for start in range(0, total, batch_size):
            batch = [
                {
                    "attributes": {"idx": i},
                    "geometry_wkt": f"POINT({(i % 360) - 180} {(i % 180) - 90})",
                }
                for i in range(start, min(start + batch_size, total))
            ]
            resp = client.send_command("add_features", {"layer_id": layer_id, "features": batch})
            assert resp["status"] == "success"
            assert resp["result"]["added"] == len(batch)

        # Verify count
        resp = client.send_command(
            "get_field_statistics", {"layer_id": layer_id, "field_name": "idx"}
        )
        assert resp["status"] == "success"
        assert resp["result"]["count"] == total

        # Delete all
        resp = client.send_command(
            "delete_features", {"layer_id": layer_id, "expression": "TRUE"}
        )
        assert resp["status"] == "success"
        assert resp["result"]["deleted"] == total

        client.send_command("remove_layer", {"layer_id": layer_id})

    def test_large_response_payload(self, client, cities_layer):
        """Request all features with geometry to produce a larger response."""
        resp = client.send_command(
            "get_layer_features",
            {"layer_id": cities_layer, "limit": 50, "include_geometry": True},
        )
        assert resp["status"] == "success"
        features = resp["result"]["features"]
        assert len(features) == 20
        assert all("_geometry" in f for f in features)

    def test_batch_20_commands(self, client, cities_layer):
        """Batch with 20 mixed commands in a single round-trip."""
        commands = []
        for i in range(20):
            if i % 4 == 0:
                commands.append({"type": "ping", "params": {}})
            elif i % 4 == 1:
                commands.append({"type": "get_canvas_extent", "params": {}})
            elif i % 4 == 2:
                commands.append({"type": "get_project_variables", "params": {}})
            else:
                commands.append(
                    {"type": "get_layer_features", "params": {"layer_id": cities_layer, "limit": 5}}
                )
        resp = client.send_command("batch", {"commands": commands}, timeout=TIMEOUT_LONG)
        assert resp["status"] == "success"
        assert len(resp["result"]) == 20
        assert all(r["status"] == "success" for r in resp["result"])

    def test_concurrent_writes_different_layers(self, client, test_project):
        """Two clients writing to different layers simultaneously."""
        layers = []
        for i in range(2):
            resp = client.send_command(
                "create_memory_layer",
                {
                    "name": f"write_test_{i}_{uuid.uuid4().hex[:4]}",
                    "geometry_type": "Point",
                    "crs": "EPSG:4326",
                    "fields": [{"name": "val", "type": "integer"}],
                },
            )
            assert resp["status"] == "success"
            layers.append(resp["result"]["id"])

        def write_to_layer(layer_id, start):
            c = make_client()
            try:
                features = [
                    {"attributes": {"val": j}, "geometry_wkt": f"POINT({j} {j})"}
                    for j in range(start, start + 50)
                ]
                return c.send_command("add_features", {"layer_id": layer_id, "features": features})
            finally:
                c.disconnect()

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(write_to_layer, layers[0], 0)
            f2 = pool.submit(write_to_layer, layers[1], 100)
            r1, r2 = f1.result(), f2.result()

        assert r1["status"] == "success"
        assert r1["result"]["added"] == 50
        assert r2["status"] == "success"
        assert r2["result"]["added"] == 50

        for lid in layers:
            client.send_command("remove_layer", {"layer_id": lid})

    def test_reconnect_after_close(self):
        """Client can reconnect after disconnect and resume operations."""
        c = make_client()
        r = c.send_command("ping")
        assert r["status"] == "success"
        c.disconnect()

        assert c.connect()
        r = c.send_command("ping")
        assert r["status"] == "success"
        c.disconnect()

    def test_ten_connect_disconnect_cycles(self):
        """Rapid connect/disconnect — plugin should handle without leaking."""
        for _ in range(10):
            c = make_client()
            r = c.send_command("ping")
            assert r["status"] == "success"
            c.disconnect()
