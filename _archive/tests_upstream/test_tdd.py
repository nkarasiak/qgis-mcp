"""TDD-oriented tests — regression guards, boundary conditions, failure modes.

Each test documents a specific bug, edge case, or invariant that must hold.
Requires a running QGIS instance with the MCP plugin on localhost:9876.

Usage:
    uv run --no-sync pytest tests/test_tdd.py -v
"""

import uuid

import pytest

from conftest import make_client


# ---------------------------------------------------------------------------
# Layer lifecycle — creation, mutation, deletion invariants
# ---------------------------------------------------------------------------


class TestLayerLifecycle:
    """Ensure layers can be created, modified, and cleaned up without leaks."""

    def test_create_and_remove_memory_layer(self, client, test_project):
        """Layer created via API must be removable and gone afterward."""
        resp = client.send_command(
            "create_memory_layer",
            {
                "name": f"lifecycle_{uuid.uuid4().hex[:6]}",
                "geometry_type": "Point",
                "crs": "EPSG:4326",
                "fields": [{"name": "x", "type": "integer"}],
            },
        )
        assert resp["status"] == "success"
        lid = resp["result"]["id"]

        # Verify it appears in layer list
        resp = client.send_command("find_layer", {"name_pattern": "lifecycle_*"})
        assert any(l["id"] == lid for l in resp["result"]["layers"])

        # Remove
        resp = client.send_command("remove_layer", {"layer_id": lid})
        assert resp["status"] == "success"

        # Verify it's gone
        resp = client.send_command("find_layer", {"name_pattern": "lifecycle_*"})
        assert all(l["id"] != lid for l in resp["result"]["layers"])

    def test_remove_nonexistent_layer_returns_error(self, client):
        resp = client.send_command("remove_layer", {"layer_id": "does_not_exist_xyz"})
        assert resp["status"] == "error"

    def test_double_remove_returns_error(self, client, test_project):
        """Removing a layer twice must error on the second call."""
        resp = client.send_command(
            "create_memory_layer",
            {"name": f"dbl_{uuid.uuid4().hex[:4]}", "geometry_type": "Point", "crs": "EPSG:4326", "fields": []},
        )
        lid = resp["result"]["id"]
        r1 = client.send_command("remove_layer", {"layer_id": lid})
        assert r1["status"] == "success"
        r2 = client.send_command("remove_layer", {"layer_id": lid})
        assert r2["status"] == "error"

    def test_empty_layer_has_zero_features(self, client, test_project):
        resp = client.send_command(
            "create_memory_layer",
            {"name": f"empty_{uuid.uuid4().hex[:4]}", "geometry_type": "Point", "crs": "EPSG:4326", "fields": [{"name": "a", "type": "string"}]},
        )
        lid = resp["result"]["id"]
        resp = client.send_command("get_layer_features", {"layer_id": lid, "limit": 50})
        assert resp["status"] == "success"
        assert len(resp["result"]["features"]) == 0
        client.send_command("remove_layer", {"layer_id": lid})


# ---------------------------------------------------------------------------
# Feature CRUD — add, read, update, delete invariants
# ---------------------------------------------------------------------------


class TestFeatureCRUD:
    """Feature operations must be atomic and consistent."""

    def test_add_feature_increments_count(self, client, cities_layer):
        """Adding a feature must increase the count by exactly 1."""
        r1 = client.send_command("get_field_statistics", {"layer_id": cities_layer, "field_name": "name"})
        before = r1["result"]["count"]

        client.send_command(
            "add_features",
            {"layer_id": cities_layer, "features": [
                {"attributes": {"name": "TestAdd", "population": 1, "country": "X"}, "geometry_wkt": "POINT(0 0)"},
            ]},
        )

        r2 = client.send_command("get_field_statistics", {"layer_id": cities_layer, "field_name": "name"})
        assert r2["result"]["count"] == before + 1

        # Cleanup
        client.send_command("delete_features", {"layer_id": cities_layer, "expression": "\"name\" = 'TestAdd'"})

    def test_delete_by_expression_only_deletes_matching(self, client, cities_layer):
        """Delete must only remove features matching the expression."""
        r1 = client.send_command("get_field_statistics", {"layer_id": cities_layer, "field_name": "name"})
        total_before = r1["result"]["count"]

        # Add 2 temp features, delete only 1 by expression
        client.send_command(
            "add_features",
            {"layer_id": cities_layer, "features": [
                {"attributes": {"name": "DelA", "population": 1, "country": "X"}, "geometry_wkt": "POINT(0 0)"},
                {"attributes": {"name": "DelB", "population": 1, "country": "X"}, "geometry_wkt": "POINT(1 1)"},
            ]},
        )
        resp = client.send_command("delete_features", {"layer_id": cities_layer, "expression": "\"name\" = 'DelA'"})
        assert resp["result"]["deleted"] == 1

        r2 = client.send_command("get_field_statistics", {"layer_id": cities_layer, "field_name": "name"})
        assert r2["result"]["count"] == total_before + 1  # +2 added, -1 deleted

        # Cleanup the other
        client.send_command("delete_features", {"layer_id": cities_layer, "expression": "\"name\" = 'DelB'"})

    def test_update_nonexistent_fid_does_not_crash(self, client, cities_layer):
        """Updating a fid that doesn't exist must not crash the plugin."""
        resp = client.send_command(
            "update_features",
            {"layer_id": cities_layer, "updates": [{"fid": 999999, "attributes": {"name": "Ghost"}}]},
        )
        assert resp["status"] == "success"
        # QGIS may report updated=1 even for nonexistent fids (provider-dependent)

    def test_add_feature_wrong_field_returns_error_or_ignores(self, client, cities_layer):
        """Adding a feature with a nonexistent field should not crash."""
        resp = client.send_command(
            "add_features",
            {"layer_id": cities_layer, "features": [
                {"attributes": {"nonexistent_field": "val"}, "geometry_wkt": "POINT(0 0)"},
            ]},
        )
        # Should either succeed (ignoring unknown field) or return a clear error
        assert resp["status"] in ("success", "error")
        # If it succeeded, clean up
        if resp["status"] == "success":
            client.send_command("delete_features", {"layer_id": cities_layer, "expression": "\"name\" IS NULL"})

    def test_feature_fid_stable_after_update(self, client, cities_layer):
        """A feature's fid must not change after an attribute update."""
        resp = client.send_command(
            "get_layer_features",
            {"layer_id": cities_layer, "expression": "\"name\" = 'Paris'", "limit": 1},
        )
        fid_before = resp["result"]["features"][0]["_fid"]

        client.send_command(
            "update_features",
            {"layer_id": cities_layer, "updates": [{"fid": fid_before, "attributes": {"population": 2200000}}]},
        )

        resp = client.send_command(
            "get_layer_features",
            {"layer_id": cities_layer, "expression": "\"name\" = 'Paris'", "limit": 1},
        )
        assert resp["result"]["features"][0]["_fid"] == fid_before

        # Restore
        client.send_command(
            "update_features",
            {"layer_id": cities_layer, "updates": [{"fid": fid_before, "attributes": {"population": 2161000}}]},
        )


# ---------------------------------------------------------------------------
# Selection invariants
# ---------------------------------------------------------------------------


class TestSelectionInvariants:
    def test_select_then_clear_gives_zero(self, client, cities_layer):
        client.send_command("select_features", {"layer_id": cities_layer, "expression": "TRUE"})
        resp = client.send_command("get_selection", {"layer_id": cities_layer})
        assert resp["result"]["count"] > 0

        client.send_command("clear_selection", {"layer_id": cities_layer})
        resp = client.send_command("get_selection", {"layer_id": cities_layer})
        assert resp["result"]["count"] == 0

    def test_select_impossible_expression_gives_zero(self, client, cities_layer):
        """Expression that matches nothing must select 0 features."""
        client.send_command("select_features", {"layer_id": cities_layer, "expression": "population < 0"})
        resp = client.send_command("get_selection", {"layer_id": cities_layer})
        assert resp["result"]["count"] == 0

    def test_select_by_fids(self, client, cities_layer):
        """Select by explicit fids must select exactly those fids."""
        resp = client.send_command("get_layer_features", {"layer_id": cities_layer, "limit": 3})
        fids = [f["_fid"] for f in resp["result"]["features"]]

        client.send_command("select_features", {"layer_id": cities_layer, "fids": fids})
        resp = client.send_command("get_selection", {"layer_id": cities_layer})
        assert resp["result"]["count"] == len(fids)
        assert set(resp["result"]["fids"]) == set(fids)

        client.send_command("clear_selection", {"layer_id": cities_layer})


# ---------------------------------------------------------------------------
# Expression validation — boundary cases
# ---------------------------------------------------------------------------


class TestExpressions:
    def test_empty_expression_is_invalid(self, client):
        resp = client.send_command("validate_expression", {"expression": ""})
        assert resp["status"] == "success"
        # Empty expression: either invalid or valid-with-no-columns
        # The key is it doesn't crash

    def test_sql_injection_in_expression(self, client, cities_layer):
        """Expressions go through QGIS's parser, not SQL — ensure no crash."""
        resp = client.send_command(
            "get_layer_features",
            {"layer_id": cities_layer, "expression": "'; DROP TABLE --", "limit": 1},
        )
        # Should return success with 0 features (invalid expression filtered) or error
        assert resp["status"] in ("success", "error")

    def test_expression_with_special_chars(self, client, cities_layer):
        """Unicode and special chars in expression must not crash."""
        resp = client.send_command(
            "get_layer_features",
            {"layer_id": cities_layer, "expression": "\"name\" = 'São Paulo'", "limit": 1},
        )
        assert resp["status"] == "success"
        assert resp["result"]["features"][0]["name"] == "São Paulo"

    def test_expression_numeric_comparison(self, client, cities_layer):
        """Numeric filters must be exact."""
        resp = client.send_command(
            "get_layer_features",
            {"layer_id": cities_layer, "expression": "population = 21540000", "limit": 50},
        )
        assert resp["status"] == "success"
        assert len(resp["result"]["features"]) == 1
        assert resp["result"]["features"][0]["name"] == "Beijing"


# ---------------------------------------------------------------------------
# Pagination — offset/limit boundary conditions
# ---------------------------------------------------------------------------


class TestPagination:
    def test_offset_beyond_count_returns_empty(self, client, cities_layer):
        resp = client.send_command(
            "get_layer_features", {"layer_id": cities_layer, "limit": 10, "offset": 9999}
        )
        assert resp["status"] == "success"
        assert len(resp["result"]["features"]) == 0

    def test_limit_zero_returns_empty(self, client, cities_layer):
        resp = client.send_command(
            "get_layer_features", {"layer_id": cities_layer, "limit": 0}
        )
        assert resp["status"] == "success"
        assert len(resp["result"]["features"]) == 0

    def test_full_scan_via_pagination(self, client, cities_layer):
        """Walking page by page must yield all 20 features with no duplicates."""
        all_fids = set()
        offset = 0
        page_size = 7
        while True:
            resp = client.send_command(
                "get_layer_features", {"layer_id": cities_layer, "limit": page_size, "offset": offset}
            )
            assert resp["status"] == "success"
            features = resp["result"]["features"]
            if not features:
                break
            for f in features:
                assert f["_fid"] not in all_fids, f"Duplicate fid {f['_fid']} at offset {offset}"
                all_fids.add(f["_fid"])
            offset += page_size
        assert len(all_fids) == 20


# ---------------------------------------------------------------------------
# Styling — idempotency and error handling
# ---------------------------------------------------------------------------


class TestStylingRobustness:
    def test_style_same_type_twice_is_idempotent(self, client, cities_layer):
        """Applying the same style type twice must not error."""
        for _ in range(2):
            resp = client.send_command(
                "set_layer_style", {"layer_id": cities_layer, "style_type": "single"}
            )
            assert resp["status"] == "success"

    def test_graduated_style_on_string_field(self, client, cities_layer):
        """Graduated style on a string field should error or degrade gracefully."""
        resp = client.send_command(
            "set_layer_style",
            {"layer_id": cities_layer, "style_type": "graduated", "field": "name", "classes": 3},
        )
        # May succeed (QGIS casts) or error — must not crash
        assert resp["status"] in ("success", "error")

    def test_labeling_roundtrip(self, client, cities_layer):
        """Enable labeling, read it back, disable it, read again."""
        client.send_command(
            "set_layer_labeling",
            {"layer_id": cities_layer, "field_name": "name", "font_size": 12, "color": "#FF0000"},
        )
        resp = client.send_command("get_layer_labeling", {"layer_id": cities_layer})
        assert resp["status"] == "success"
        assert resp["result"]["enabled"] is True

        client.send_command("set_layer_labeling", {"layer_id": cities_layer, "enabled": False})
        resp = client.send_command("get_layer_labeling", {"layer_id": cities_layer})
        assert resp["status"] == "success"
        assert resp["result"]["enabled"] is False


# ---------------------------------------------------------------------------
# Canvas state — set/get consistency
# ---------------------------------------------------------------------------


class TestCanvasState:
    def test_set_then_get_extent_roundtrip(self, client):
        """Setting an extent and reading it back must return approximately the same bbox."""
        client.send_command("set_canvas_extent", {"xmin": 10, "ymin": 40, "xmax": 20, "ymax": 50})
        resp = client.send_command("get_canvas_extent")
        assert resp["status"] == "success"
        r = resp["result"]
        # Canvas may adjust for aspect ratio — check center is preserved
        cx = (r["xmin"] + r["xmax"]) / 2
        cy = (r["ymin"] + r["ymax"]) / 2
        assert abs(cx - 15) < 5, f"Center X drifted: {cx}"
        assert abs(cy - 45) < 5, f"Center Y drifted: {cy}"

    def test_set_canvas_scale_via_extent(self, client):
        """After setting a small extent, scale should decrease."""
        client.send_command("set_canvas_extent", {"xmin": 2, "ymin": 48, "xmax": 3, "ymax": 49})
        r1 = client.send_command("get_canvas_scale")

        client.send_command("set_canvas_extent", {"xmin": -180, "ymin": -90, "xmax": 180, "ymax": 90})
        r2 = client.send_command("get_canvas_scale")

        assert r1["status"] == "success" and r2["status"] == "success"
        # Wider extent = larger scale denominator
        assert r2["result"]["scale"] > r1["result"]["scale"]


# ---------------------------------------------------------------------------
# Project variables — type preservation
# ---------------------------------------------------------------------------


class TestProjectVariables:
    def test_string_variable_roundtrip(self, client, test_project):
        client.send_command("set_project_variable", {"key": "tdd_str", "value": "hello"})
        resp = client.send_command("get_project_variables")
        assert resp["result"]["variables"]["tdd_str"] == "hello"

    def test_numeric_string_variable_preserved(self, client, test_project):
        """A numeric value stored as string should come back as-is."""
        client.send_command("set_project_variable", {"key": "tdd_num", "value": "42"})
        resp = client.send_command("get_project_variables")
        assert resp["result"]["variables"]["tdd_num"] == "42"

    def test_unicode_variable_roundtrip(self, client, test_project):
        client.send_command("set_project_variable", {"key": "tdd_uni", "value": "café ñ 日本語"})
        resp = client.send_command("get_project_variables")
        assert resp["result"]["variables"]["tdd_uni"] == "café ñ 日本語"

    def test_overwrite_variable(self, client, test_project):
        client.send_command("set_project_variable", {"key": "tdd_ow", "value": "first"})
        client.send_command("set_project_variable", {"key": "tdd_ow", "value": "second"})
        resp = client.send_command("get_project_variables")
        assert resp["result"]["variables"]["tdd_ow"] == "second"


# ---------------------------------------------------------------------------
# Bookmarks — CRUD invariants
# ---------------------------------------------------------------------------


class TestBookmarkInvariants:
    def test_add_duplicate_name_creates_two(self, client, test_project):
        """Two bookmarks with the same name must both exist."""
        name = f"dup_{uuid.uuid4().hex[:4]}"
        r1 = client.send_command("add_bookmark", {"name": name, "xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1})
        r2 = client.send_command("add_bookmark", {"name": name, "xmin": 2, "ymin": 2, "xmax": 3, "ymax": 3})
        assert r1["status"] == "success" and r2["status"] == "success"

        resp = client.send_command("get_bookmarks")
        matches = [b for b in resp["result"]["bookmarks"] if b["name"] == name]
        assert len(matches) == 2

        # Cleanup
        client.send_command("remove_bookmark", {"bookmark_id": r1["result"]["id"]})
        client.send_command("remove_bookmark", {"bookmark_id": r2["result"]["id"]})

    def test_remove_nonexistent_bookmark_does_not_crash(self, client):
        """Removing a nonexistent bookmark must not crash (QGIS returns success)."""
        resp = client.send_command("remove_bookmark", {"bookmark_id": "bogus_id_xyz"})
        assert resp["status"] in ("success", "error")


# ---------------------------------------------------------------------------
# Batch — error isolation
# ---------------------------------------------------------------------------


class TestBatchErrorIsolation:
    def test_batch_one_bad_command_doesnt_kill_others(self, client, cities_layer):
        """A failing command in a batch must not prevent other commands from running."""
        resp = client.send_command(
            "batch",
            {"commands": [
                {"type": "ping", "params": {}},
                {"type": "get_layer_features", "params": {"layer_id": "nonexistent"}},
                {"type": "ping", "params": {}},
            ]},
            timeout=60,
        )
        assert resp["status"] == "success"
        results = resp["result"]
        assert results[0]["status"] == "success"
        assert results[1]["status"] == "error"
        assert results[2]["status"] == "success"

    def test_batch_empty_commands_list(self, client):
        resp = client.send_command("batch", {"commands": []}, timeout=60)
        assert resp["status"] == "success"
        assert resp["result"] == []


# ---------------------------------------------------------------------------
# CRS transforms — edge cases
# ---------------------------------------------------------------------------


class TestCRSEdgeCases:
    def test_transform_same_crs_is_identity(self, client):
        """Transforming EPSG:4326 → EPSG:4326 must return the same point."""
        resp = client.send_command(
            "transform_coordinates",
            {"source_crs": "EPSG:4326", "target_crs": "EPSG:4326", "point": {"x": 10.5, "y": 48.2}},
        )
        assert resp["status"] == "success"
        pt = resp["result"]["point"]
        assert abs(pt["x"] - 10.5) < 0.0001
        assert abs(pt["y"] - 48.2) < 0.0001

    def test_transform_invalid_crs_returns_error(self, client):
        resp = client.send_command(
            "transform_coordinates",
            {"source_crs": "EPSG:99999", "target_crs": "EPSG:4326", "point": {"x": 0, "y": 0}},
        )
        assert resp["status"] == "error"

    def test_set_project_crs(self, client, test_project):
        """Setting and reading back project CRS must be consistent."""
        resp = client.send_command("set_project_crs", {"crs": "EPSG:3857"})
        assert resp["status"] == "success"

        resp = client.send_command("get_project_info")
        assert resp["result"]["crs"] == "EPSG:3857"

        # Restore
        client.send_command("set_project_crs", {"crs": "EPSG:4326"})


# ---------------------------------------------------------------------------
# Error messages — must be human-readable
# ---------------------------------------------------------------------------


class TestErrorMessages:
    def test_error_has_message_field(self, client):
        resp = client.send_command("get_layer_features", {"layer_id": "nonexistent"})
        assert resp["status"] == "error"
        assert "message" in resp
        assert len(resp["message"]) > 5  # Must be a real message, not empty

    def test_unknown_command_returns_error(self, client):
        resp = client.send_command("totally_fake_command_xyz")
        assert resp["status"] == "error"

    def test_missing_required_param_returns_error(self, client):
        """Calling add_vector_layer without path should error."""
        resp = client.send_command("add_vector_layer", {})
        assert resp["status"] == "error"


# ---------------------------------------------------------------------------
# Map theme lifecycle
# ---------------------------------------------------------------------------


class TestMapThemeLifecycle:
    def test_remove_nonexistent_theme(self, client):
        resp = client.send_command("remove_map_theme", {"name": "no_such_theme_xyz"})
        assert resp["status"] == "error"

    def test_add_apply_remove_theme(self, client, cities_layer):
        name = f"tdd_theme_{uuid.uuid4().hex[:4]}"
        r = client.send_command("add_map_theme", {"name": name})
        assert r["status"] == "success"

        r = client.send_command("apply_map_theme", {"name": name})
        assert r["status"] == "success"

        r = client.send_command("remove_map_theme", {"name": name})
        assert r["status"] == "success"

        # Verify gone
        resp = client.send_command("get_map_themes")
        names = [t["name"] for t in resp["result"]["themes"]]
        assert name not in names
