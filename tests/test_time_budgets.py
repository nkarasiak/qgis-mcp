"""execute_code and execute_processing_batch time budgets against a stubbed qgis (#43).

Both run synchronously inside QGIS's event loop under a 60s client socket timeout. Past
that the client gives up while QGIS keeps working and the result is lost for good, so the
plugin has to give up first and say why, the way execute_processing already does.
"""

import sys
import time

import pytest


@pytest.fixture
def system(plugin_handlers):
    class Server(plugin_handlers.system.SystemHandlers):
        LOG_TAG = "test"
        iface = None

    return Server()


def test_execute_code_reports_elapsed(system):
    result = system.execute_code("print('hi')")
    assert result["executed"] is True
    assert result["stdout"] == "hi\n"
    assert 0 <= result["elapsed"] < 5


def test_execute_code_stops_a_runaway_loop_at_the_deadline(system):
    started = time.monotonic()
    result = system.execute_code("print('before')\nwhile True:\n    pass\n", timeout=0.3)
    assert time.monotonic() - started < 5
    assert result["executed"] is False
    assert result["timed_out"] is True
    assert "0.3s" in result["error"] and "timeout" in result["error"]
    assert result["stdout"] == "before\n", "output printed before the deadline must survive"
    assert result["elapsed"] >= 0.3


def test_execute_code_deadline_reaches_loops_inside_script_functions(system):
    code = "def spin():\n    while True:\n        pass\n\nspin()\n"
    assert system.execute_code(code, timeout=0.3)["timed_out"] is True


def test_execute_code_cuts_the_line_after_a_blocking_call(system):
    """A blocking call cannot be interrupted; the script stops at the next line instead."""
    result = system.execute_code("import time\ntime.sleep(0.3)\nprint('late')\n", timeout=0.1)
    assert result["timed_out"] is True
    assert result["stdout"] == ""
    assert result["elapsed"] >= 0.3


def test_execute_code_that_finishes_late_is_not_reported_cancelled(system):
    result = system.execute_code("import time\ntime.sleep(0.3)\n", timeout=0.1)
    assert result["executed"] is True
    assert "timed_out" not in result
    assert result["elapsed"] >= 0.3


def test_execute_code_restores_the_trace_function(system):
    before = sys.gettrace()
    system.execute_code("x = 1")
    system.execute_code("while True: pass", timeout=0.1)
    assert sys.gettrace() is before


def test_execute_code_errors_still_carry_elapsed(system):
    result = system.execute_code("1/0")
    assert result["executed"] is False
    assert "ZeroDivisionError" in result["traceback"]
    assert "elapsed" in result


@pytest.fixture
def processing(plugin_handlers, monkeypatch):
    class Server(plugin_handlers.processing.ProcessingHandlers):
        LOG_TAG = "test"

    server = Server()
    budgets = []

    def slow_run(algorithm, params, feedback=None):
        budgets.append(feedback.budget)
        time.sleep(params["sleep"])
        return {"OUTPUT": params["sleep"]}

    monkeypatch.setattr(server, "_run_alg", slow_run)
    return server, budgets


def test_batch_skips_the_runs_that_would_start_after_the_budget(processing):
    server, budgets = processing
    result = server.execute_processing_batch("native:buffer", [{"sleep": 0.2}] * 4, timeout=0.3)
    assert [r["status"] for r in result["results"]] == ["success", "success", "skipped", "skipped"]
    assert result["count"] == 4
    assert result["timed_out"] is True
    assert "0.3s" in result["results"][2]["message"]
    assert 0 < budgets[1] < 0.15, "a run only gets what is left of the batch budget"


def test_batch_default_budget_is_the_processing_timeout(processing):
    server, budgets = processing
    result = server.execute_processing_batch("native:buffer", [{"sleep": 0}] * 2)
    assert [r["status"] for r in result["results"]] == ["success", "success"]
    assert "timed_out" not in result
    assert budgets[0] == pytest.approx(server._PROCESSING_TIMEOUT, abs=0.1)
