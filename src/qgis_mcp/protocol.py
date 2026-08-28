"""Wire-protocol constants and auth shared by client and server.

Stdlib-only - the client must stay importable in environments without the
``mcp`` package (e.g. gis_utils' qgis_bridge connecting from a plain
conda env).
"""

import importlib.metadata
import os
import pathlib
import struct

# ---------------------------------------------------------------------------
# Protocol constants - single source of truth for defaults across all modules
# ---------------------------------------------------------------------------

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876
TIMEOUT_DEFAULT = 30  # seconds - most tool commands
TIMEOUT_LONG = 60  # seconds - execute_processing, render_map, execute_code, batch
RECV_CHUNK_SIZE = 65536  # bytes per recv/recv_into call
MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10 MB - plugin-side buffer/message limit
HEADER_STRUCT = struct.Struct(">I")  # 4-byte big-endian uint32 length prefix

BATCH_BLOCKED_COMMANDS = frozenset(
    {
        "execute_code",
        "remove_layer",
        "delete_features",
        "set_setting",
        "reload_plugin",
        "rollback_edits",
        "execute_connection_sql",
        "import_layer_to_connection",
    }
)


class CommandTimeout(ConnectionError):
    """The command reached QGIS but the client gave up waiting for its response.

    A ``ConnectionError`` so every existing reconnect path still fires, but its
    own type because the two halves mean different things to a caller: failing
    to reach QGIS leaves nothing behind, while abandoning a response leaves the
    command running in QGIS. Retrying the second kind runs it twice.
    """


def get_auth_token():
    """Return the shared-secret socket token, or ``None`` when auth is disabled.

    Read from the ``QGIS_MCP_TOKEN`` environment variable. When unset or empty,
    authentication is off and behaviour is unchanged - the plugin accepts any
    command (the historical default). When set, the client attaches it to every
    command and the plugin rejects commands that don't present a matching token.
    """
    token = os.environ.get("QGIS_MCP_TOKEN", "").strip()
    return token or None


# Cap what goes on the wire and in front of a QGIS user: the plugin treats this
# as untrusted input, and no real version string is anywhere near this long.
MAX_VERSION_LENGTH = 32

_client_version = None


def get_client_version():
    """The qgis-mcp version this MCP server reports to the plugin.

    Read from the installed distribution metadata, which is what
    ``enrich_diagnose`` compares against the plugin's ``metadata.txt``. Running
    from a source checkout therefore reports the version recorded when the
    checkout was last installed, not what the working tree currently says - a
    stale record is a real mismatch, and the ``fix`` in the diagnose output says
    how to re-record it.

    Resolved once: it cannot change while the process lives, and this is on the
    path of every single command.
    """
    global _client_version
    if _client_version is None:
        try:
            _client_version = importlib.metadata.version("qgis-mcp")
        except importlib.metadata.PackageNotFoundError:
            _client_version = "unknown"
    return _client_version[:MAX_VERSION_LENGTH]


# How this MCP server was launched. Announced to the plugin so it can name the
# command that updates *this* install: `uv cache clean` and `uv sync` are not
# interchangeable, and only this side can tell the two apart.
INSTALL_UVX = "uvx"
INSTALL_SOURCE = "source"

# The checkout path travels with INSTALL_SOURCE and ends up in the QGIS log and
# the configurator, so it is capped like the version string.
MAX_PATH_LENGTH = 160

_install_info = None


def get_install_info():
    """``(kind, root)``: how this MCP server was installed, and from where.

    ``root`` is the checkout path for ``INSTALL_SOURCE`` and ``None`` for
    ``INSTALL_UVX``. A source checkout has a pyproject.toml two levels above
    this module; a uvx install lives in an ephemeral environment that does not.

    Only the *kind* crosses the socket, never a ready-made command string: the
    plugin shows the command to the user as something to run, so it builds it
    from its own literals rather than trusting text from a peer.

    Lives here rather than in ``helpers`` because the client sends this on every
    command and must stay importable without ``mcp``. Resolved once: it cannot
    change while the process lives.
    """
    global _install_info
    if _install_info is None:
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        if (repo_root / "pyproject.toml").is_file():
            _install_info = (INSTALL_SOURCE, str(repo_root)[:MAX_PATH_LENGTH])
        else:
            _install_info = (INSTALL_UVX, None)
    return _install_info


def get_update_command():
    """The command that updates *this* MCP server install, for local reporting.

    Used by ``enrich_diagnose``, which runs on this side of the socket, so the
    string is safe to build here. The plugin has its own copy of these templates
    for the same reason in reverse.
    """
    kind, root = get_install_info()
    if kind == INSTALL_SOURCE:
        # Deliberately no `git pull`: the mismatch is a stale *recorded* version
        # in the venv, not out-of-date source, and touching someone's working
        # tree is not this tool's business. `uv sync` re-records it.
        return f'uv --directory "{root}" sync'
    # uvx caches the built package per source URL, and the configured URL points
    # at a branch archive, so nothing re-downloads until the cache is dropped.
    return "uv cache clean qgis-mcp"
