"""Static guards on the plugin package's structure.

The handlers live in ``qgis_mcp_plugin/handlers/`` as mixins combined onto
``QgisMCPServer``. None of that can be imported here (it needs ``qgis``), so
these checks are AST-based, like ``test_py39_compat.py``.
"""

import ast
import os
import re

HERE = os.path.dirname(__file__)
PLUGIN_DIR = os.path.join(HERE, "..", "qgis_mcp_plugin")
HANDLERS_DIR = os.path.join(PLUGIN_DIR, "handlers")
MCP_SRC = os.path.join(HERE, "..", "src", "qgis_mcp")

# Modules that sit in the plugin package root. A module inside handlers/ must
# reach them with two dots; one dot resolves inside handlers/ and fails at
# import time - which no compile check or unit test catches, because the plugin
# is only ever imported by QGIS.
PACKAGE_ROOT_MODULES = ("compat", "constants", "errors", "registry", "wire")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _handler_modules():
    for name in sorted(os.listdir(HANDLERS_DIR)):
        if name.endswith(".py") and name != "__init__.py":
            yield name, _read(os.path.join(HANDLERS_DIR, name))


def _command_sources():
    """Every module that may register wire commands: the mixins plus server.py."""
    yield from _handler_modules()
    yield "server.py", _read(os.path.join(PLUGIN_DIR, "server.py"))


def _is_command_decorator(node):
    """True for `@command` and for an attribute access ending in `.command`."""
    return (getattr(node, "id", None) or getattr(node, "attr", None)) == "command"


def _registered_commands():
    commands = {}
    for name, src in _command_sources():
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.FunctionDef):
                continue
            # Bare `@command` today; `@registry.command` would be an Attribute,
            # and matching only the Name form would silently pass on every module.
            if any(_is_command_decorator(d) for d in node.decorator_list):
                commands.setdefault(node.name, []).append(name)
    return commands


def _commands_the_server_sends():
    """Command names the MCP server puts on the wire, from its own source."""
    sent = set()
    for name in ("server.py", "compound_tools.py"):
        for node in ast.walk(ast.parse(_read(os.path.join(MCP_SRC, name)))):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            called = getattr(func, "id", None) or getattr(func, "attr", None)
            if called in ("_send", "_send_sync", "send_command"):
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    sent.add(first.value)
    return sent


def _one_dot_imports(src):
    """``from .x import ...`` nodes - one dot resolves inside handlers/."""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            yield node


def test_handler_modules_reach_the_package_root_with_two_dots():
    """`from .compat import ...` inside handlers/ is an ImportError at load time."""
    offenders = []
    for name, src in _handler_modules():
        for node in _one_dot_imports(src):
            if node.module in PACKAGE_ROOT_MODULES:
                offenders.append(f"handlers/{name}:{node.lineno} from .{node.module}")
    assert not offenders, (
        "handlers/ is a subpackage: these must import from '..' instead of '.', "
        f"or QGIS fails to load the plugin: {offenders}"
    )


def test_handler_mixins_do_not_import_each_other():
    """The mixins are siblings on one class; they must not depend on each other.

    Shared helpers belong in ``handlers/base.py`` (``HandlerBase``), which is
    last in the MRO, so any mixin can call them through ``self``.
    """
    offenders = []
    for name, src in _handler_modules():
        for node in _one_dot_imports(src):
            if node.module not in PACKAGE_ROOT_MODULES:
                offenders.append(f"handlers/{name}:{node.lineno} imports .{node.module}")
    assert not offenders, f"handler mixins must not import one another: {offenders}"


def test_command_names_are_unique():
    """Two mixins registering the same name would silently shadow by MRO order."""
    dupes = {n: mods for n, mods in _registered_commands().items() if len(mods) > 1}
    assert not dupes, f"the same command is registered in more than one module: {dupes}"


def test_every_command_the_mcp_server_sends_is_registered():
    """A command the server sends but the plugin does not register is dead on arrival."""
    missing = sorted(_commands_the_server_sends() - set(_registered_commands()))
    assert not missing, (
        f"src/qgis_mcp sends these commands but no @command handler exists for them: {missing}"
    )


def test_every_registered_command_is_reachable_from_the_mcp_server():
    """The reverse: a handler nothing can call is either dead code or a missing tool."""
    unreachable = sorted(set(_registered_commands()) - _commands_the_server_sends())
    assert not unreachable, (
        "these handlers are registered but src/qgis_mcp never sends them - add the "
        f"tool or drop the handler: {unreachable}"
    )


def test_dispatch_only_reaches_decorated_handlers():
    """Dispatch must gate getattr on COMMANDS, or any attribute becomes callable.

    Without the membership test a client could send ``{"type": "stop"}`` and
    reach the server's own methods.
    """
    src = _read(os.path.join(PLUGIN_DIR, "server.py"))
    start = src.index("def _dispatch")
    dispatch = src[start:]
    marker = "\n    @command"
    if marker in dispatch:
        end = dispatch.index(marker)
        dispatch = dispatch[:end]
    assert "cmd_type not in COMMANDS" in dispatch, (
        "_dispatch must reject command names that are not in the COMMANDS registry"
    )
    assert re.search(r"getattr\(self, cmd_type\)", dispatch), (
        "_dispatch is expected to resolve the handler with getattr after the COMMANDS check"
    )


def _raises_command_error(node):
    """True for `raise CommandError(...)` / `raise LayerNotFound(...)` and friends."""
    exc = node.exc
    if isinstance(exc, ast.Call):
        exc = exc.func
    name = getattr(exc, "id", None) or getattr(exc, "attr", None)
    return name in ("CommandError", "LayerNotFound", "WrongLayerType")


def _has_command_error_raise(*roots):
    """True when any node under *roots* is `raise CommandError(...)`-like."""
    for root in roots:
        for node in ast.walk(root):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            if _raises_command_error(node):
                return True
    return False


def _catches(handler, *names):
    caught = handler.type
    if caught is None:
        return "Exception" in names  # bare `except:` behaves like except Exception
    parts = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    return any((getattr(p, "id", None) or getattr(p, "attr", None)) in names for p in parts)


def test_deliberate_command_errors_are_not_rewrapped():
    """A `try` that raises CommandError itself must re-raise it before the catch-all.

    ``except Exception as e: raise CommandError(f"Processing error: {e}")`` also
    catches the handler's *own* deliberate message, so a clean "Processing
    cancelled after 55s, pass a larger timeout" came back to the user as
    "Processing error: Processing cancelled after 55s" - reading as if the
    timeout were an internal failure. Re-raise CommandError untouched first.
    """
    offenders = []
    for name, src in _command_sources():
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Try):
                continue
            raised_here = _has_command_error_raise(*node.body)
            rewraps = any(
                _catches(h, "Exception") and _has_command_error_raise(h) for h in node.handlers
            )
            passes_through = any(_catches(h, "CommandError") for h in node.handlers)
            if raised_here and rewraps and not passes_through:
                offenders.append(f"{name}:{node.lineno}")
    assert not offenders, (
        "these try blocks raise a CommandError and then re-wrap it in their own "
        f"catch-all - add `except CommandError: raise` first: {offenders}"
    )


def _commands_of_read_only_tools():
    """Commands sent by granular tools annotated readOnlyHint=True."""
    commands = set()
    for node in ast.walk(ast.parse(_read(os.path.join(MCP_SRC, "server.py")))):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        tools = [
            d
            for d in node.decorator_list
            if isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "tool"
        ]
        read_only = tools and any(
            isinstance(k, ast.keyword)
            and k.arg == "readOnlyHint"
            and getattr(k.value, "value", None) is True
            for k in ast.walk(tools[0])
        )
        if not read_only:
            continue
        for call in ast.walk(node):
            func = getattr(call, "func", None)
            if getattr(func, "id", None) in ("_send", "_send_sync") and call.args:
                commands.add(call.args[0].value)
    return commands


def test_the_journal_leaves_out_exactly_the_read_only_commands():
    """export_session replays what changed something: the read-only tools' commands.

    Anything else in UNRECORDED_COMMANDS has to be listed here on purpose.
    """
    bookkeeping = {
        "batch",
        "cancel_processing_job",
        "create_checkpoint",
        "export_session",
        "restore_checkpoint",
        "start_processing_job",
    }
    tree = ast.parse(_read(os.path.join(PLUGIN_DIR, "wire.py")))
    unrecorded = next(
        ast.literal_eval(node.value.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and node.targets[0].id == "UNRECORDED_COMMANDS"
    )
    assert unrecorded - bookkeeping == _commands_of_read_only_tools()
