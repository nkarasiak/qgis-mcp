"""The session journal, its export as a script, checkpoints and background jobs.

Against a stubbed qgis: what is checked here is the plugin's bookkeeping - what gets
journaled, what a restore rewinds, what a finished job reports - not QGIS itself.
"""

import ast
import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def server(plugin_handlers):
    import qgis_mcp_plugin.server as mod

    return mod.QgisMCPServer()


def _commands(server):
    return [entry["command"] for entry in server._journal]


def test_mutating_commands_are_journaled_and_read_only_ones_are_not(server):
    assert server._dispatch({"type": "ping"})["status"] == "success"
    response = server._dispatch(
        {"type": "set_project_variable", "params": {"key": "k", "value": "v"}}
    )

    assert response["status"] == "success"
    assert _commands(server) == ["set_project_variable"]
    assert server._journal[0]["params"] == {"key": "k", "value": "v"}


def test_failed_commands_and_scripts_that_raised_are_not_journaled(server, plugin_handlers):
    def refuse(key, value, **kwargs):
        raise plugin_handlers.processing.CommandError("no")

    server.set_project_variable = refuse
    server._dispatch({"type": "set_project_variable", "params": {"key": "k", "value": "v"}})
    server._dispatch({"type": "execute_code", "params": {"code": "raise ValueError('x')"}})
    server._dispatch({"type": "execute_code", "params": {"code": "x = 1"}})

    assert _commands(server) == ["execute_code"]
    assert server._journal[0]["params"] == {"code": "x = 1"}


def test_journal_keeps_the_parameters_as_sent_not_as_the_handler_left_them(server):
    def rewrite(key, value, **kwargs):
        value["path"] = object()  # what runAndLoadResults does to its outputs
        return {}

    server.set_project_variable = rewrite
    server._dispatch(
        {"type": "set_project_variable", "params": {"key": "k", "value": {"path": "/o"}}}
    )

    assert server._journal[0]["params"]["value"] == {"path": "/o"}


def test_batch_journals_each_command_not_the_batch(server):
    command = {"type": "set_project_variable", "params": {"key": "k", "value": "v"}}
    server._dispatch({"type": "batch", "params": {"commands": [command, command]}})

    assert _commands(server) == ["set_project_variable", "set_project_variable"]


def test_exported_script_is_valid_python_replaying_each_command(server):
    code = "for layer in QgsProject.instance().mapLayers().values():\n    print('\\d', layer)"
    server._record("execute_code", {"code": code})
    server._record("add_vector_layer", {"path": "/data/roads.gpkg"}, [("roads_1", "roads")])
    server._record("set_layer_style", {"layer_id": "roads_1", "style": "single"})

    script = server.export_session()["script"]

    compile(script, "replay.py", "exec")
    assert f"code=r'''{code}'''" in script  # as written, not as an escaped repr
    assert "_creates=[('roads_1', 'roads')]" in script
    assert "mcp('set_layer_style', layer_id='roads_1', style='single')" in script


def test_exported_script_replays_through_dispatch_and_remaps_layer_ids(server, monkeypatch):
    server._record("add_vector_layer", {"path": "/r.gpkg"}, [("old_id", "roads")])
    server._record("zoom_to_layer", {"layer_id": "old_id"})
    script = server.export_session()["script"]

    class Layer:
        def __init__(self, lid, name):
            self._id, self._name = lid, name

        def id(self):
            return self._id

        def name(self):
            return self._name

    layers = {}
    project = MagicMock()
    project.mapLayers.side_effect = lambda: dict(layers)
    project.mapLayer.side_effect = layers.get
    sent = []

    def dispatch(command):
        sent.append(command)
        if command["type"] == "add_vector_layer":
            layers["new_id"] = Layer("new_id", "roads")
        return {"status": "success", "result": {}}

    replayer = MagicMock(_dispatch=dispatch)
    monkeypatch.setattr(
        sys.modules["qgis.utils"], "plugins", {"qgis_mcp_plugin": MagicMock(server=replayer)}
    )
    monkeypatch.setattr(sys.modules["qgis.core"].QgsProject, "instance", lambda: project)

    exec(compile(script, "replay.py", "exec"), {})

    assert sent[1] == {"type": "zoom_to_layer", "params": {"layer_id": "new_id"}}


def test_export_writes_to_a_path_and_clear_starts_over(server, tmp_path):
    server._record("set_project_variable", {"key": "k", "value": "v"})
    path = tmp_path / "session.py"

    response = server.export_session(path=str(path), clear=True)

    assert response == {"command_count": 1, "path": str(path)}
    assert "set_project_variable" in path.read_text(encoding="utf-8")
    assert list(server._journal) == []


def test_restoring_a_checkpoint_rewinds_the_journal(server):
    server.iface = MagicMock()
    server._record("set_project_variable", {"key": "a", "value": 1})
    checkpoint = server.create_checkpoint(name="before")
    server._record("set_project_variable", {"key": "b", "value": 2})

    server.restore_checkpoint(checkpoint["id"])

    assert [e["params"]["key"] for e in server._journal] == ["a"]
    assert server.list_checkpoints()["checkpoints"][0]["name"] == "before"
    server.stop()


def test_checkpoint_leaves_the_project_file_name_alone(server, plugin_handlers, monkeypatch):
    project = MagicMock()
    project.fileName.return_value = "/work/city.qgz"
    project.isDirty.return_value = False
    project.mapLayers.return_value = {}
    monkeypatch.setattr(plugin_handlers.session.QgsProject, "instance", lambda: project)

    server.create_checkpoint()

    written_to = project.write.call_args.args[0]
    assert written_to.endswith("cp1.qgz") and written_to != "/work/city.qgz"
    project.setFileName.assert_called_with("/work/city.qgz")
    project.setDirty.assert_called_with(False)
    server.stop()


def test_unknown_checkpoint_names_the_known_ones(server, plugin_handlers):
    with pytest.raises(plugin_handlers.session.CommandError, match="Known checkpoints: none"):
        server.restore_checkpoint("cp9")


# --- background jobs ---


class _Param:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name

    def flags(self):
        return 0


@pytest.fixture
def algorithm(plugin_handlers, monkeypatch):
    processing = plugin_handlers.processing
    alg = MagicMock()
    alg.flags.return_value = 0
    alg.destinationParameterDefinitions.return_value = [_Param("OUTPUT")]
    alg.checkParameterValues.return_value = (True, "")
    registry = processing.QgsApplication.processingRegistry.return_value
    monkeypatch.setattr(registry, "algorithmById", lambda alg_id: alg)
    monkeypatch.setattr(processing, "PROC_ALG_NO_THREADING", 4)
    # OUTPUT is a feature sink: a temporary one is a memory layer the job would drop.
    monkeypatch.setattr(sys.modules["qgis.core"], "QgsProcessingParameterFeatureSink", _Param)
    monkeypatch.setitem(sys.modules, "processing.tools", MagicMock())
    # The stub feedback base answers every call with None; QGIS's reports a float.
    monkeypatch.setattr(processing._CollectingFeedback, "progress", lambda self: 0.0, raising=False)
    return alg


def test_a_job_refuses_a_temporary_output_it_would_discard(server, algorithm, plugin_handlers):
    with pytest.raises(plugin_handlers.processing.CommandError, match="load_results=True"):
        server.start_processing_job("native:buffer", {"INPUT": "a", "OUTPUT": "TEMPORARY_OUTPUT"})
    assert server._jobs == {}


def test_a_main_thread_algorithm_is_refused(server, algorithm, plugin_handlers):
    algorithm.flags.return_value = 4
    with pytest.raises(plugin_handlers.processing.CommandError, match="execute_processing"):
        server.start_processing_job("qgis:something", {"OUTPUT": "/tmp/o.gpkg"})


def test_a_finished_job_reports_its_result_and_is_journaled_as_blocking(
    server, algorithm, monkeypatch
):
    monkeypatch.setattr(server, "_missing_outputs", lambda alg, params: [])
    job = server.start_processing_job("native:buffer", {"INPUT": "a", "OUTPUT": "/tmp/o.gpkg"})
    assert job["state"] == "running" and _commands(server) == []

    server._finish_job(server._jobs[job["id"]], True, {"OUTPUT": "/tmp/o.gpkg"})

    done = server.get_processing_job(job["id"])
    assert done["state"] == "succeeded" and done["result"] == {"OUTPUT": "/tmp/o.gpkg"}
    assert _commands(server) == ["execute_processing"]
    params = server._journal[0]["params"]
    assert params["parameters"] == {"INPUT": "a", "OUTPUT": "/tmp/o.gpkg"}
    assert params["timeout"] >= 55


def test_a_job_that_wrote_nothing_fails_and_is_not_journaled(server, algorithm, monkeypatch):
    monkeypatch.setattr(server, "_missing_outputs", lambda alg, params: ["/tmp/o.gpkg"])
    job = server.start_processing_job("native:buffer", {"OUTPUT": "/tmp/o.gpkg"})

    server._finish_job(server._jobs[job["id"]], True, {"OUTPUT": "/tmp/o.gpkg"})

    done = server.get_processing_job(job["id"])
    assert done["state"] == "failed" and "wrote no /tmp/o.gpkg" in done["error"]
    assert list(server._journal) == []


def test_a_cancelled_job_says_so(server, algorithm, monkeypatch):
    job_id = server.start_processing_job("native:buffer", {"OUTPUT": "/tmp/o.gpkg"})["id"]
    job = server._jobs[job_id]
    monkeypatch.setattr(job["feedback"], "isCanceled", lambda: True, raising=False)

    server.cancel_processing_job(job_id)
    server._finish_job(job, False, {})

    assert server.get_processing_job(job_id)["state"] == "cancelled"
    assert server.get_processing_job()["count"] == 1


def test_exported_script_keeps_code_that_ends_in_a_quote(server):
    code = "x = 1\nname = 'roads'"
    server._record("execute_code", {"code": code})
    script = server.export_session()["script"]

    tree = ast.parse(script)
    call = tree.body[-1].value
    assert ast.literal_eval(call.keywords[0].value) == code


def test_layers_a_job_journaled_mid_command_are_not_the_commands(server, monkeypatch):
    import qgis_mcp_plugin.server as mod

    layers = {}
    project = MagicMock()
    project.mapLayers.side_effect = lambda: dict(layers)
    monkeypatch.setattr(mod.QgsProject, "instance", lambda: project)

    def handler(key, value, **kwargs):
        # A background job finishes while this handler pumps the event loop.
        layers["job_layer"] = MagicMock(**{"name.return_value": "job"})
        server._record("execute_processing", {}, [("job_layer", "job")])
        layers["own_layer"] = MagicMock(**{"name.return_value": "own"})
        return {}

    monkeypatch.setattr(server, "set_project_variable", handler)
    response = server._dispatch(
        {"type": "set_project_variable", "params": {"key": "k", "value": "v"}}
    )

    assert response["status"] == "success", response
    assert server._journal[-1]["creates"] == [("own_layer", "own")]
