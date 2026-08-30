"""Runs *inside* the QGIS Python subprocess (OSGeo4W python-qgis-ltr.bat).

Reads length-prefixed JSON commands from stdin, dispatches them via the QGIS
plugin's ``QgisMCPServer.execute_command``, writes length-prefixed JSON
responses to stdout. The protocol matches what ``HeadlessExecutor`` (parent
process) speaks; same envelope as the plugin's TCP socket: ``{type, params}``
in, ``{status, result|message}`` out.

Why re-use the plugin's command class instead of a parallel impl: every v0.3+
handler we care about (``add_vector_layer``, ``render_choropleth`` …) is pure
PyQGIS — no ``self.iface`` dependency. A stub iface no-ops the rare UI calls
(canvas refresh, active-layer setter) so plugin and headless share one
codebase. See ``docs/DESIGN.md`` §2 (architecture) for the seam rationale.

Protocol:
    1. Runner writes ``{"status": "ready"}`` once initQgis completes.
    2. Caller writes ``{"type": cmd, "params": {...}}`` (length-prefixed).
    3. Runner writes ``{"status": "success"|"error", ...}`` (length-prefixed).
    4. Caller writes ``{"type": "shutdown"}`` to exit cleanly; runner replies
       and exits.
    5. EOF on stdin also triggers clean shutdown.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import traceback

# Force offscreen Qt before any QGIS / Qt import — critical for the
# QgsMapRendererParallelJob path, otherwise it tries to open an X11 display
# (Linux) or fails to find a screen (Windows headless).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HEADER = struct.Struct(">I")


def _put_repo_root_on_path() -> None:
    """Make ``qgis_mcp_workflows_plugin`` importable from this subprocess.

    The runner lives at ``src/qgis_mcp_workflows/executors/headless_runner.py``;
    the repo root (which contains ``qgis_mcp_workflows_plugin/``) is four levels
    above. Override via ``QGIS_MCP_WORKFLOWS_REPO_ROOT`` for non-default installs.
    """
    override = os.environ.get("QGIS_MCP_WORKFLOWS_REPO_ROOT")
    if override:
        sys.path.insert(0, override)
        return
    here = os.path.abspath(__file__)
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))
    sys.path.insert(0, repo_root)


# -----------------------------------------------------------------------
# Stub iface — covers every ``self.iface.<x>`` call the plugin makes
# in the headless-supportable handlers. Anything not stubbed will raise
# AttributeError, which surfaces as a clear "this command needs Desktop"
# error in the response — exactly what we want.
# -----------------------------------------------------------------------


class _StubMapCanvas:
    def refresh(self) -> None:
        pass

    def extent(self):
        # No real canvas exists in headless. Callers should pass extent= explicitly.
        raise RuntimeError(
            "Canvas extent is unavailable in headless mode. "
            "Pass an explicit extent or switch to plugin transport."
        )

    def setExtent(self, _rect) -> None:
        pass


class _StubLayerTreeView:
    def refreshLayerSymbology(self, _layer_id) -> None:
        pass

    def layerTreeModel(self):
        # If a handler reaches here, it's truly Desktop-only. Surface it loudly.
        raise RuntimeError(
            "layerTreeView().layerTreeModel() is Desktop-only — use plugin transport."
        )


class _StubIface:
    def mapCanvas(self):
        return _StubMapCanvas()

    def setActiveLayer(self, _layer) -> None:
        pass

    def zoomToActiveLayer(self) -> None:
        pass

    def activeLayer(self):
        return None

    def layerTreeView(self):
        return _StubLayerTreeView()

    def messageBar(self):
        return None  # plugin only logs through it; ignoring is safe.


# -----------------------------------------------------------------------
# Wire protocol — length-prefixed JSON over stdin/stdout
# -----------------------------------------------------------------------


def _read_message(stream) -> dict | None:
    header = stream.read(4)
    if len(header) < 4:
        return None
    (length,) = _HEADER.unpack(header)
    body = stream.read(length)
    if len(body) < length:
        return None
    return json.loads(body.decode("utf-8"))


def _write_message(stream, msg: dict) -> None:
    body = json.dumps(msg).encode("utf-8")
    stream.write(_HEADER.pack(len(body)))
    stream.write(body)
    stream.flush()


def main() -> int:
    _put_repo_root_on_path()

    # Lazy imports — must happen after sys.path is set up and after
    # QT_QPA_PLATFORM is forced to offscreen.
    from qgis.core import QgsApplication

    qgs = QgsApplication([], False)
    # Use the prefix path the launcher exported; fall back to QGIS_PREFIX_PATH
    # if running outside the launcher.
    prefix = os.environ.get("QGIS_PREFIX_PATH")
    if prefix:
        QgsApplication.setPrefixPath(prefix, True)
    qgs.initQgis()

    try:
        from qgis_mcp_workflows_plugin.plugin import QgisMCPServer
    except Exception as exc:
        # The launcher set us up, but the plugin can't import. Tell the parent
        # so it surfaces a useful error.
        _write_message(
            sys.stdout.buffer,
            {
                "status": "error",
                "message": f"Headless runner could not import plugin: {exc!r}",
                "traceback": traceback.format_exc(),
            },
        )
        qgs.exitQgis()
        return 1

    server = QgisMCPServer(host="", port=0, iface=_StubIface())

    _write_message(sys.stdout.buffer, {"status": "ready"})

    try:
        while True:
            msg = _read_message(sys.stdin.buffer)
            if msg is None:
                break  # EOF
            if msg.get("type") == "shutdown":
                _write_message(sys.stdout.buffer, {"status": "success", "result": {"shutdown": True}})
                break
            try:
                response = server.execute_command(msg)
            except Exception as exc:
                response = {
                    "status": "error",
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                }
            _write_message(sys.stdout.buffer, response)
    finally:
        qgs.exitQgis()

    return 0


if __name__ == "__main__":
    sys.exit(main())
