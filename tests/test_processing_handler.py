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
    calls = {"runner": None, "checked": None, "context": None}

    def run(algorithm, parameters, feedback=None, context=None):
        calls["runner"] = "run"
        calls["context"] = context
        return {"OUTPUT": parameters["OUTPUT"]}

    def run_and_load_results(algorithm, parameters, feedback=None, context=None):
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

    def run(algorithm, parameters, feedback=None, context=None):
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


@pytest.fixture
def dataobjects(monkeypatch):
    """Stub processing.tools.dataobjects; createContext returns one recording context."""
    from unittest.mock import MagicMock

    module = MagicMock()
    monkeypatch.setitem(sys.modules, "processing.tools", MagicMock(dataobjects=module))
    monkeypatch.setitem(sys.modules, "processing.tools.dataobjects", module)
    return module


def test_default_run_leaves_the_context_to_processing(processing):
    server, calls = processing

    server.execute_processing("native:buffer", {"INPUT": "c", "OUTPUT": "/tmp/o.gpkg"})

    assert calls["context"] is None


def test_ellipsoid_is_set_on_the_processing_context(processing, dataobjects):
    """Argleton c008/c023: WGS84 areas were measured on the project's ellipsoid."""
    server, calls = processing

    server.execute_processing(
        "native:exportaddgeometrycolumns",
        {"INPUT": "c", "OUTPUT": "TEMPORARY_OUTPUT"},
        ellipsoid="EPSG:7030",
    )

    context = dataobjects.createContext.return_value
    assert calls["context"] is context
    context.setEllipsoid.assert_called_once_with("EPSG:7030")


def test_unknown_ellipsoid_is_refused_before_running(
    processing, plugin_handlers, dataobjects, monkeypatch
):
    server, calls = processing
    params = plugin_handlers.processing.QgsEllipsoidUtils.ellipsoidParameters.return_value
    monkeypatch.setattr(params, "valid", False)

    with pytest.raises(plugin_handlers.processing.CommandError, match="Unknown ellipsoid"):
        server.execute_processing("native:buffer", {"INPUT": "c"}, ellipsoid="bogus")

    assert calls["runner"] is None


# --- every _run_alg caller surfaces the algorithm's reason -------------------


def _failing_run(*reasons):
    def run(algorithm, parameters, feedback=None, context=None):
        feedback.errors.extend(reasons)
        raise Exception("There were errors executing the algorithm.")

    return run


def test_run_alg_callers_get_the_reason_as_a_command_error(
    processing, plugin_handlers, monkeypatch
):
    """zonal_statistics & co. used to surface the generic text as an internal error."""
    server, _ = processing
    monkeypatch.setattr(
        sys.modules["processing"], "run", _failing_run("Invalid band number for BAND (5)")
    )

    with pytest.raises(plugin_handlers.processing.CommandError) as excinfo:
        server._run_alg("native:zonalstatisticsfb", {"INPUT": "c"})

    assert "Invalid band number for BAND (5)" in str(excinfo.value)


def test_batch_run_error_carries_the_reason(processing, monkeypatch):
    server, _ = processing
    monkeypatch.setattr(sys.modules["processing"], "run", _failing_run("bad input"))

    response = server.execute_processing_batch("native:buffer", [{"INPUT": "c"}])

    assert response["results"][0]["status"] == "error"
    assert "bad input" in response["results"][0]["message"]


def test_missing_output_message_keeps_every_reported_error(processing, monkeypatch):
    """GDAL's first stderr line is often a warning; the exit code comes last."""
    server, _ = processing
    monkeypatch.setattr(server, "_missing_outputs", lambda alg, params: ["/tmp/o.tif"])

    def run(algorithm, parameters, feedback=None, context=None):
        feedback.errors.extend(["Warning 1: harmless", "Process returned error code 1"])
        return {"OUTPUT": parameters["OUTPUT"]}

    monkeypatch.setattr(sys.modules["processing"], "run", run)

    with pytest.raises(Exception, match="Process returned error code 1"):
        server._run_alg("gdal:translate", {"OUTPUT": "/tmp/o.tif"})


def test_cancelled_run_that_raises_reports_the_timeout(processing, plugin_handlers, monkeypatch):
    server, _ = processing

    def run(algorithm, parameters, feedback=None, context=None):
        feedback.timed_out = True
        raise Exception("Processing cancelled")

    monkeypatch.setattr(sys.modules["processing"], "run", run)

    with pytest.raises(plugin_handlers.processing.CommandError, match="cancelled after"):
        server._run_alg("native:buffer", {"INPUT": "c"})


# --- raster_calculator -------------------------------------------------------


class _Raster:
    def __init__(self, handlers, lid, name, provider="gdal", crs="EPSG:32631"):
        self._type = handlers.processing.LAYER_RASTER
        self._id, self._name, self._provider, self._crs = lid, name, provider, crs

    def type(self):
        return self._type

    def id(self):
        return self._id

    def name(self):
        return self._name

    def providerType(self):
        return self._provider

    def bandCount(self):
        return 1

    def crs(self):
        crs = self._crs

        class _Crs:
            def authid(self):
                return crs

        return _Crs()

    def extent(self):
        return f"extent-of-{self._id}"

    def width(self):
        return 4

    def height(self):
        return 4


@pytest.fixture
def calculator(plugin_handlers, monkeypatch):
    """Record the QgsRasterCalculator constructor arguments; succeed by default."""
    calls = {}

    class Calc:
        def __init__(self, *args):
            calls["args"] = args

        def processCalculation(self):
            return 0

        def lastError(self):
            return ""

    monkeypatch.setattr(sys.modules["qgis.analysis"], "QgsRasterCalculator", Calc)

    class Server(plugin_handlers.processing.ProcessingHandlers):
        LOG_TAG = "test"

    def load(*layers):
        project = plugin_handlers.processing.QgsProject.instance.return_value
        monkeypatch.setattr(
            project.mapLayers, "return_value", {layer.id(): layer for layer in layers}
        )

    return Server(), calls, load


def test_raster_calculator_writes_the_reference_crs_not_the_first_entrys(
    calculator, plugin_handlers
):
    """Argleton-class silent error: z32 loaded first used to label z31's grid EPSG:32632."""
    server, calls, load = calculator
    load(
        _Raster(plugin_handlers, "z32_id", "z32", crs="EPSG:32632"),
        _Raster(plugin_handlers, "z31_id", "z31", crs="EPSG:32631"),
    )

    response = server.raster_calculator('"z31@1"', "/tmp/o.tif", reference_layer="z31_id")

    args = calls["args"]
    assert args[3] == "extent-of-z31_id"
    assert args[4].authid() == "EPSG:32631", "output CRS must be the reference layer's"
    assert response["crs"] == "EPSG:32631"


def test_raster_calculator_refuses_an_unknown_reference_layer(calculator, plugin_handlers):
    server, _, load = calculator
    load(_Raster(plugin_handlers, "a_id", "a"))

    with pytest.raises(plugin_handlers.processing.CommandError, match="not found"):
        server.raster_calculator('"a@1"', "/tmp/o.tif", reference_layer="typo")


def test_raster_calculator_refuses_an_ambiguous_layer_name(calculator, plugin_handlers):
    server, _, load = calculator
    load(_Raster(plugin_handlers, "d1", "dem"), _Raster(plugin_handlers, "d2", "dem"))

    with pytest.raises(plugin_handlers.processing.CommandError, match="Ambiguous raster name"):
        server.raster_calculator('"dem@1" * 2', "/tmp/o.tif", reference_layer="d1")


def test_raster_calculator_ignores_duplicates_the_expression_does_not_use(
    calculator, plugin_handlers
):
    server, _, load = calculator
    load(
        _Raster(plugin_handlers, "d1", "dem"),
        _Raster(plugin_handlers, "d2", "dem"),
        _Raster(plugin_handlers, "s", "slope"),
    )

    response = server.raster_calculator('"slope@1" > 30', "/tmp/o.tif", reference_layer="s")

    assert response["ok"]


def test_raster_calculator_default_reference_skips_web_rasters(calculator, plugin_handlers):
    server, calls, load = calculator
    load(
        _Raster(plugin_handlers, "osm", "OSM", provider="wms"),
        _Raster(plugin_handlers, "dem_id", "dem"),
    )

    response = server.raster_calculator('"dem@1"', "/tmp/o.tif")

    assert calls["args"][3] == "extent-of-dem_id"
    assert response["reference_layer_id"] == "dem_id"


def test_raster_calculator_failure_carries_last_error(calculator, plugin_handlers, monkeypatch):
    server, _, load = calculator
    load(_Raster(plugin_handlers, "a_id", "a"))
    calc = sys.modules["qgis.analysis"].QgsRasterCalculator
    monkeypatch.setattr(calc, "processCalculation", lambda self: 2)
    monkeypatch.setattr(calc, "lastError", lambda self: "Could not open input a@1")

    with pytest.raises(plugin_handlers.processing.CommandError, match="Could not open input"):
        server.raster_calculator('"a@1"', "/tmp/o.tif")


# --- ellipsoid on batch and model runs ----------------------------------------


def test_batch_runs_measure_on_the_requested_ellipsoid(processing, dataobjects):
    server, calls = processing

    response = server.execute_processing_batch(
        "native:exportaddgeometrycolumns",
        [{"INPUT": "c", "OUTPUT": "TEMPORARY_OUTPUT"}],
        ellipsoid="EPSG:7030",
    )

    assert response["results"][0]["status"] == "success"
    assert calls["context"] is dataobjects.createContext.return_value
    dataobjects.createContext.return_value.setEllipsoid.assert_called_with("EPSG:7030")


def test_batch_refuses_an_unknown_ellipsoid_before_any_run(
    processing, plugin_handlers, dataobjects, monkeypatch
):
    server, calls = processing
    params = plugin_handlers.processing.QgsEllipsoidUtils.ellipsoidParameters.return_value
    monkeypatch.setattr(params, "valid", False)

    with pytest.raises(plugin_handlers.processing.CommandError, match="Unknown ellipsoid"):
        server.execute_processing_batch("native:buffer", [{"INPUT": "c"}], ellipsoid="bogus")

    assert calls["runner"] is None


def test_run_model_measures_on_the_requested_ellipsoid(processing, dataobjects):
    server, calls = processing

    server.run_model("model:areas", {"OUTPUT": "TEMPORARY_OUTPUT"}, ellipsoid="EPSG:7030")

    assert calls["context"] is dataobjects.createContext.return_value
    dataobjects.createContext.return_value.setEllipsoid.assert_called_with("EPSG:7030")


def test_run_model_default_leaves_the_context_to_processing(processing):
    server, calls = processing

    server.run_model("model:areas", {"OUTPUT": "TEMPORARY_OUTPUT"})

    assert calls["context"] is None
