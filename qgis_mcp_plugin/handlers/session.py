"""Handlers for the session: project checkpoints, and the journal as a script."""

import contextlib
import os
import tempfile
import time
from typing import ClassVar

from qgis.core import QgsFeatureRequest, QgsMessageLog, QgsProject

from ..compat import LAYER_VECTOR, MSG_INFO
from ..errors import CommandError
from ..registry import command

# Runs in the QGIS Python console. Each call goes through the plugin's own
# dispatch, so a replay gets the same validation and output checks as the
# session it came from.
_SCRIPT_HEADER = '''\
"""Replay of a QGIS MCP session, exported {created}.

Run it from the QGIS Python console (Plugins > Python Console > Show Editor,
open, Run) with the QGIS MCP plugin loaded and its server started. It stops at
the first command that fails.
"""

from qgis.core import QgsProject
from qgis.utils import plugins

_server = plugins["qgis_mcp_plugin"].server
if _server is None:
    raise RuntimeError("Start the QGIS MCP server first (Plugins > QGIS MCP)")

# Layer ids differ on every run: the id each layer had when the session was
# recorded, mapped onto the one it has in this run.
_ids = {{}}


def _swap(value):
    if isinstance(value, str):
        return _ids.get(value, value)
    if isinstance(value, list):
        return [_swap(v) for v in value]
    if isinstance(value, dict):
        return {{k: _swap(v) for k, v in value.items()}}
    return value


def mcp(command, _creates=(), **params):
    project = QgsProject.instance()
    before = set(project.mapLayers())
    response = _server._dispatch({{"type": command, "params": _swap(params)}})
    if response["status"] != "success":
        raise RuntimeError(f"{{command}}: {{response['message']}}")
    result = response["result"]
    if isinstance(result, dict) and result.get("executed") is False:
        raise RuntimeError(f"{{command}}: {{result.get('traceback') or result.get('error')}}")
    added = [project.mapLayer(i) for i in project.mapLayers() if i not in before]
    for old_id, name in _creates:
        match = next((lyr for lyr in added if lyr.name() == name), None)
        match = match or (added[0] if added else None)
        if match is not None:
            _ids[old_id] = match.id()
            added.remove(match)
    return result

'''


def _literal(value):
    """*value* as Python source; multi-line text as a raw block, as it was written."""
    if isinstance(value, str) and "\n" in value and "'''" not in value and value[-1] not in "\\'":
        return f"r'''{value}'''"
    return repr(value)


def _replay_line(entry):
    args = [repr(entry["command"])]
    args += [f"{name}={_literal(value)}" for name, value in entry["params"].items()]
    if entry["creates"]:
        args.append(f"_creates={entry['creates']!r}")
    return f"mcp({', '.join(args)})"


class SessionHandlers:
    """Checkpoints of the project, and the session journal exported as a script."""

    # Checkpoints kept; creating one more deletes the oldest.
    _MAX_CHECKPOINTS: ClassVar[int] = 20

    @command
    def create_checkpoint(self, name=None, **kwargs):
        """Snapshot the project so restore_checkpoint can return to it.

        The project is written to a temporary .qgz, which holds everything a
        project file does: layers, styles, labels, layouts, themes, variables.
        Memory layers keep no features in a project file, so a copy of each is
        taken alongside. Data in files and databases is not copied, and edits
        not yet committed are in neither.
        """
        project = QgsProject.instance()
        checkpoint_id = f"cp{next(self._checkpoint_ids)}"
        if self._checkpoint_dir is None:
            self._checkpoint_dir = tempfile.mkdtemp(prefix="qgis-mcp-checkpoints-")
        path = os.path.join(self._checkpoint_dir, f"{checkpoint_id}.qgz")

        vectors = [lyr for lyr in project.mapLayers().values() if lyr.type() == LAYER_VECTOR]
        memory = {
            lyr.id(): lyr.materialize(QgsFeatureRequest())
            for lyr in vectors
            if lyr.providerType() == "memory"
        }
        uncommitted = [
            lyr.name()
            for lyr in vectors
            if lyr.providerType() != "memory" and lyr.isEditable() and lyr.isModified()
        ]
        file_name, dirty = project.fileName(), project.isDirty()
        try:
            written = project.write(path)
        finally:
            # write(path) makes the temp file the project's own; the user's
            # project must stay where it was, saved or not.
            project.setFileName(file_name)
            project.setDirty(dirty)
        if not written:
            error = project.error()
            raise CommandError(f"Could not write the checkpoint{f': {error}' if error else ''}")

        checkpoint = {
            "id": checkpoint_id,
            "name": name or checkpoint_id,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "layer_count": len(project.mapLayers()),
            "path": path,
            "memory": memory,
            # Journal position: commands after it are undone by a restore.
            "seq": self._journal[-1]["seq"] if self._journal else 0,
        }
        self._checkpoints[checkpoint_id] = checkpoint
        while len(self._checkpoints) > self._MAX_CHECKPOINTS:
            oldest = self._checkpoints.pop(next(iter(self._checkpoints)))
            with contextlib.suppress(OSError):
                os.remove(oldest["path"])
        QgsMessageLog.logMessage(f"Checkpoint {checkpoint_id} created", self.LOG_TAG, MSG_INFO)

        response = self._checkpoint_summary(checkpoint)
        if uncommitted:
            response["uncommitted_edits"] = uncommitted
            response["note"] = (
                "These layers have edits not yet committed, which the checkpoint does "
                "not hold. Commit them first to include them."
            )
        return response

    @staticmethod
    def _checkpoint_summary(checkpoint):
        return {key: checkpoint[key] for key in ("id", "name", "created", "layer_count")}

    @command
    def list_checkpoints(self, **kwargs):
        checkpoints = [self._checkpoint_summary(cp) for cp in self._checkpoints.values()]
        return {"checkpoints": checkpoints, "count": len(checkpoints)}

    @command
    def restore_checkpoint(self, checkpoint_id, **kwargs):
        """Put the project back as it was at *checkpoint_id*.

        Everything the project holds is replaced, uncommitted edits included.
        The project keeps its own file name, and stays unsaved until the user
        saves it. The journal is rewound too, so export_session replays the
        restored state rather than the steps that were undone.
        """
        checkpoint = self._checkpoints.get(checkpoint_id)
        if checkpoint is None:
            known = ", ".join(self._checkpoints) or "none"
            raise CommandError(f"No checkpoint {checkpoint_id!r}. Known checkpoints: {known}")
        project = QgsProject.instance()
        file_name = project.fileName()
        ok, unavailable = self._read_project(checkpoint["path"])
        if not ok:
            error = project.error()
            raise CommandError(f"Could not read the checkpoint{f': {error}' if error else ''}")
        project.setFileName(file_name)
        for layer_id, snapshot in checkpoint["memory"].items():
            layer = project.mapLayer(layer_id)
            if layer is None:
                continue
            provider = layer.dataProvider()
            provider.truncate()
            provider.addFeatures(list(snapshot.getFeatures()))
            layer.updateExtents()
        project.setDirty(True)
        self.iface.mapCanvas().refresh()
        while self._journal and self._journal[-1]["seq"] > checkpoint["seq"]:
            self._journal.pop()
        QgsMessageLog.logMessage(f"Checkpoint {checkpoint_id} restored", self.LOG_TAG, MSG_INFO)
        response = {"restored": checkpoint_id, **self._checkpoint_summary(checkpoint)}
        if unavailable:
            # Their sources went missing after the checkpoint (or before it).
            response["unavailable_layers"] = unavailable
        return response

    @command
    def export_session(self, path=None, clear=False, **kwargs):
        """The commands that changed something this session, as a PyQGIS script.

        Returns the script, or writes it to *path* and returns only the path.
        Every client connected to this QGIS shares one journal.
        """
        entries = list(self._journal)
        script = _SCRIPT_HEADER.format(created=time.strftime("%Y-%m-%d %H:%M"))
        script += "".join(f"{_replay_line(entry)}\n" for entry in entries)
        response = {"command_count": len(entries)}
        if self._journal_truncated:
            response["truncated"] = True
            response["note"] = (
                f"The oldest commands were dropped: only {self.MAX_JOURNAL} are kept."
            )
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(script)
            response["path"] = path
        else:
            response["script"] = script
        if clear:
            self._journal.clear()
            self._journal_truncated = False
        return response
