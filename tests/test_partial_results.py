"""Results that are partial, capped or defaulted say so.

A capped list with no flag reads as the whole answer; these tests pin the
indicator on each handler that caps.
"""

from unittest.mock import MagicMock

import pytest
from test_silent_success import CommandError, project, server, vector  # noqa: F401


def test_unique_values_flags_a_capped_list_and_nulls(server, vector):  # noqa: F811
    vector.fields.return_value.indexOf.return_value = 0
    vector.uniqueValues.return_value = {"a", "b", "c", None}

    result = server.get_unique_values("lid", "f", limit=2)

    vector.uniqueValues.assert_called_once_with(0, 3)  # one past the limit
    assert result["values"] == ["a", "b"]
    assert result["truncated"] is True
    assert result["has_null"] is True


def test_unique_values_complete_list_is_not_truncated(server, vector):  # noqa: F811
    vector.fields.return_value.indexOf.return_value = 0
    vector.uniqueValues.return_value = {"a", "b"}

    result = server.get_unique_values("lid", "f", limit=2)

    assert (result["truncated"], result["has_null"]) == (False, False)


def test_unique_values_unlimited_passes_minus_one(server, vector):  # noqa: F811
    vector.fields.return_value.indexOf.return_value = 0
    vector.uniqueValues.return_value = {"a"}

    assert server.get_unique_values("lid", "f", limit=-1)["truncated"] is False
    vector.uniqueValues.assert_called_once_with(0, -1)


@pytest.fixture
def identify_hits(plugin_handlers, server, project, vector, monkeypatch):  # noqa: F811
    """identify_features where every feature in the layer is a hit, same CRS."""
    monkeypatch.setattr(plugin_handlers.features, "QgsGeometry", MagicMock())
    crs = project.crs.return_value
    vector.crs.return_value = crs
    vector.fields.return_value = []

    def load(n):
        feats = []
        for i in range(n):
            f = MagicMock()
            f.geometry.return_value.isEmpty.return_value = False
            f.geometry.return_value.intersects.return_value = True
            f.id.return_value = i
            feats.append(f)
        vector.getFeatures.return_value = feats

    return load


@pytest.mark.parametrize(("hits", "truncated"), [(3, True), (2, False)])
def test_identify_flags_a_layer_capped_by_limit(server, identify_hits, hits, truncated):  # noqa: F811
    identify_hits(hits)

    layer = server.identify_features([0.0, 0.0], layer_ids=["lid"], limit=2)["results"][0]

    assert layer["count"] == 2
    assert layer["truncated"] is truncated


@pytest.fixture
def log_server(server):  # noqa: F811
    server._message_log = [{"level": "info", "tag": "MCP", "message": str(i)} for i in range(5)] + [
        {"level": "warning", "tag": "MCP", "message": "w"}
    ]
    return server


def test_message_log_reports_total_and_truncation(log_server):
    result = log_server.get_message_log(limit=2)

    assert (result["count"], result["total"], result["truncated"]) == (2, 6, True)


def test_message_log_level_is_case_insensitive(log_server):
    assert log_server.get_message_log(level="WARNING")["count"] == 1


def test_message_log_refuses_an_unknown_level(log_server, CommandError):  # noqa: F811
    """An unknown level ("error") used to filter everything out silently."""
    with pytest.raises(CommandError, match="Unknown level: 'error'"):
        log_server.get_message_log(level="error")


@pytest.mark.parametrize(("n", "truncated"), [(12, True), (10, False)])
def test_project_info_flags_the_ten_layer_cap(server, project, n, truncated):  # noqa: F811
    project.mapLayers.return_value = {f"l{i}": MagicMock() for i in range(n)}

    info = server.get_project_info()

    assert info["layer_count"] == n
    assert len(info["layers"]) == min(n, 10)
    assert info["layers_truncated"] is truncated
