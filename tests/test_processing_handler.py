"""execute_processing's load_results path against a stubbed qgis (#46).

`processing.runAndLoadResults` rewrites the destination entries of the parameters dict in
place (into `QgsProcessingOutputLayerDefinition`), so the output verification added for
issue #40 has to run against a snapshot taken before the run or it silently stops checking
anything.
"""

import sys

import pytest


class _OutputLayerDefinition:
    """Stand-in for what runAndLoadResults writes back into the parameters dict."""

    def __init__(self, sink):
        self.sink = sink


@pytest.fixture
def processing(plugin_handlers, monkeypatch):
    class Server(plugin_handlers.processing.ProcessingHandlers):
        LOG_TAG = "test"

    server = Server()
    calls = {"runner": None, "checked": None}

    def run(algorithm, parameters, feedback=None):
        calls["runner"] = "run"
        return {"OUTPUT": parameters["OUTPUT"]}

    def run_and_load_results(algorithm, parameters, feedback=None):
        calls["runner"] = "runAndLoadResults"
        parameters["OUTPUT"] = _OutputLayerDefinition(parameters["OUTPUT"])
        return {"OUTPUT": "output layer"}

    monkeypatch.setattr(sys.modules["processing"], "run", run)
    monkeypatch.setattr(sys.modules["processing"], "runAndLoadResults", run_and_load_results)

    def missing_outputs(algorithm, parameters):
        calls["checked"] = dict(parameters)
        return []

    monkeypatch.setattr(server, "_missing_outputs", missing_outputs)
    return server, calls


def _project(plugin_handlers, monkeypatch, *snapshots):
    """Make QgsProject.instance().mapLayers() return each snapshot in turn."""
    project = plugin_handlers.processing.QgsProject.instance.return_value
    monkeypatch.setattr(project.mapLayers, "side_effect", list(snapshots))
    return project


class _Layer:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


def test_load_results_runs_the_loading_variant_and_reports_the_new_layers(
    processing, plugin_handlers, monkeypatch
):
    server, calls = processing
    _project(
        plugin_handlers,
        monkeypatch,
        {"old_1": _Layer("cities")},
        {"old_1": _Layer("cities"), "new_1": _Layer("Buffered")},
    )

    response = server.execute_processing(
        "native:buffer", {"INPUT": "cities", "OUTPUT": "TEMPORARY_OUTPUT"}, load_results=True
    )

    assert calls["runner"] == "runAndLoadResults"
    assert response["loaded_layers"] == [{"id": "new_1", "name": "Buffered"}]


def test_default_stays_on_processing_run_and_loads_nothing(processing):
    server, calls = processing

    response = server.execute_processing("native:buffer", {"INPUT": "c", "OUTPUT": "/tmp/o.gpkg"})

    assert calls["runner"] == "run"
    assert "loaded_layers" not in response


def test_output_verification_survives_the_parameter_rewrite(
    processing, plugin_handlers, monkeypatch
):
    """#40's check must see the path the caller asked for, not the rewritten value."""
    server, calls = processing
    _project(plugin_handlers, monkeypatch, {}, {})

    server.execute_processing(
        "native:buffer", {"INPUT": "c", "OUTPUT": "/tmp/out.gpkg"}, load_results=True
    )

    assert calls["checked"]["OUTPUT"] == "/tmp/out.gpkg"


def test_failed_run_reports_the_engine_reason_not_just_the_generic_message(
    processing, plugin_handlers, monkeypatch
):
    """processing.run raises a generic exception; the real reason went to the feedback."""
    server, _ = processing

    def run(algorithm, parameters, feedback=None):
        # What _ResponsiveFeedback.reportError collects (the stub base has no reportError).
        feedback.errors.append("Input layer has fewer than 3 points")
        raise Exception("There were errors executing the algorithm.")

    monkeypatch.setattr(sys.modules["processing"], "run", run)
    CommandError = plugin_handlers.processing.CommandError

    with pytest.raises(CommandError) as excinfo:
        server.execute_processing("native:voronoipolygons", {"INPUT": "c"})

    message = str(excinfo.value)
    assert "There were errors executing the algorithm." in message
    assert "Input layer has fewer than 3 points" in message
