# Rename to qgis-mcp-workflows — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the package, plugin, console script, and surface strings from `qgis-mcp-north` / `qgis_mcp_north` to `qgis-mcp-workflows` / `qgis_mcp_workflows`, releasing as v1.1.0. The fork's positioning ("workflow tools, not 51 PyQGIS primitives" — see `docs/DESIGN.md` §1) becomes the literal name; "North" disappears from user-facing surfaces.

**Architecture:** This is a pure refactor — no behavior changes, no new tools. We rename two directories with `git mv` (preserves blame), then do find/replace across imports, env vars, class names, and documentation. Co-existence with upstream `nkarasiak/qgis-mcp` continues to hold via the different package name, plugin folder, port (9877 stays), and pyproject `name`. Historical completion-report docs (`docs/v0.3-*.md`, `docs/v0.4-*.md`, `docs/v0.5-*.md`, `docs/v1.0-*.md`) stay untouched — they're frozen snapshots.

**Tech Stack:** Python 3.12, `uv`, `hatchling` build backend, FastMCP, pytest, ruff. No new dependencies introduced.

**Scope decisions (locked in before this plan was written):**
- Port stays **9877** (still distinct from upstream's 9876).
- MCP client config key in `install.py` changes `qgis-north` → `qgis-workflows`.
- Error base class `QgisMcpNorthError` → `QgisMcpWorkflowsError`.
- Logger name `QgisMcpNorthServer` → `QgisMcpWorkflowsServer`.
- Plugin class `QgisMCPServer` stays (it was inherited from upstream, internal to the plugin; not user-facing).
- Latent bug `importlib.metadata.version("qgis-mcp")` at `src/qgis_mcp_north/helpers.py:38` → fix to the new package name while we're in there.
- Historical docs (`docs/v0.*-completion-report.md`, `docs/v1.0-completion-report.md`, `docs/benchmarks-v0.5.md`, `docs/v0.3-cloud-prompt.md`) are **not** renamed — they document the codebase at a past point in time.

---

## File Structure

This rename touches three categories of files:

**Renamed (directories — `git mv` preserves blame):**
- `src/qgis_mcp_north/` → `src/qgis_mcp_workflows/`
- `qgis_mcp_north_plugin/` → `qgis_mcp_workflows_plugin/`

**Modified (config — content edits):**
- `pyproject.toml` — package name, wheel target, console script
- `qgis_mcp_workflows_plugin/metadata.txt` — QGIS plugin name + URLs + changelog entry
- `src/qgis_mcp_workflows/helpers.py` — fix `importlib.metadata.version` lookup
- `src/qgis_mcp_workflows/__init__.py` — currently empty, stays empty
- `install.py` — `PLUGIN_SRC`, `GITHUB_URL`, every literal path / config key
- `CLAUDE.md`, `docs/DESIGN.md`, `README.md`, `CHANGELOG.md`, `docs/pflow-usage.md`, `CONTRIBUTING.md` — text references

**Modified (code — programmatic refactor):**
- All `.py` files importing or referencing `qgis_mcp_north` / `qgis_mcp_north_plugin` / `QGIS_MCP_NORTH_*` / `QgisMcpNorthError` / `QgisMcpNorthServer`. There are ~30 such files (per Grep survey, excluding `.venv` and `_archive`).

**Not modified:**
- `.venv/**`, `uv.lock` (uv regenerates), `_archive/**`, `docs/v0.*-completion-report.md`, `docs/v1.0-completion-report.md`, `docs/benchmarks-v0.5.md`, `docs/v0.3-cloud-prompt.md`, `__pycache__/**`.
- The `qgis_mcp_north_plugin/__pycache__/` directory and `scripts/__pycache__/` will be regenerated; safe to delete with the rename.

---

### Task 1: Branch + worktree setup

**Files:** none yet — pure git.

- [ ] **Step 1: Confirm clean working tree**

```bash
git status
```

Expected: `nothing to commit, working tree clean`. If dirty, stop and commit / stash first.

- [ ] **Step 2: Create branch and switch to it**

```bash
git checkout -b rename/qgis-mcp-workflows
```

Expected: `Switched to a new branch 'rename/qgis-mcp-workflows'`.

- [ ] **Step 3: Snapshot the current test count for later parity check**

```bash
uv run --no-sync pytest tests/ --collect-only -q | tail -3
```

Expected: a line like `99 tests collected`. Record this number — we expect the same count after the rename.

---

### Task 2: Rename the `src/qgis_mcp_north/` package directory

**Files:**
- Rename: `src/qgis_mcp_north/` → `src/qgis_mcp_workflows/`

- [ ] **Step 1: `git mv` the package directory**

```bash
git mv src/qgis_mcp_north src/qgis_mcp_workflows
```

Expected: no output on success. Run `git status` to see all subfiles listed as renamed.

- [ ] **Step 2: Delete stale bytecode**

```bash
rm -rf src/qgis_mcp_workflows/__pycache__ src/qgis_mcp_workflows/**/__pycache__ 2>/dev/null
```

Expected: no output (find may report nothing to delete on a fresh checkout; that's fine).

- [ ] **Step 3: Commit the rename, no content changes**

```bash
git commit -m "refactor: rename src/qgis_mcp_north → src/qgis_mcp_workflows (directory only)"
```

Expected: `99 files changed, 0 insertions(+), 0 deletions(-)` style message (file count is the directory's contents). Tests will not run yet — imports still reference the old name.

---

### Task 3: Rename the `qgis_mcp_north_plugin/` directory

**Files:**
- Rename: `qgis_mcp_north_plugin/` → `qgis_mcp_workflows_plugin/`

- [ ] **Step 1: `git mv` the plugin directory**

```bash
git mv qgis_mcp_north_plugin qgis_mcp_workflows_plugin
```

Expected: no output.

- [ ] **Step 2: Delete stale bytecode**

```bash
rm -rf qgis_mcp_workflows_plugin/__pycache__
```

- [ ] **Step 3: Commit**

```bash
git commit -m "refactor: rename qgis_mcp_north_plugin → qgis_mcp_workflows_plugin (directory only)"
```

---

### Task 4: Update `pyproject.toml` to the new package + console script

**Files:**
- Modify: `pyproject.toml` (full content below)

- [ ] **Step 1: Rewrite pyproject.toml**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/qgis_mcp_workflows"]

[project]
name = "qgis-mcp-workflows"
version = "1.1.0"
description = "Focused QGIS MCP server with workflow tools for transportation research figure pipelines (PFLOW, GUFM). Forked from nkarasiak/qgis-mcp."
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "mcp[cli]>=1.20.0",
    "pydantic>=2.7",
]

[project.optional-dependencies]
dev = ["pytest>=7.0", "pytest-asyncio>=0.23"]
pptx = ["python-pptx>=1.0"]
trajectory = ["movingpandas>=0.20"]
bench = ["pytest-benchmark>=4.0"]

[project.scripts]
qgis-mcp-workflows-server = "qgis_mcp_workflows.server:main"

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B", "SIM", "RUF"]
ignore = [
    "E501",    # line too long — handled by formatter
]

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["S101"]
"_archive/*" = ["ALL"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
    "bench: long-running benchmarks (skipped by default; run with `pytest -m bench`)",
    "slow: integration tests that take >10s",
]
addopts = "-m 'not bench'"
```

Three substantive changes vs the current file (everything else is verbatim): `packages` array entry, `name`, `version` (bumped 1.0.0 → 1.1.0), `description` (added "workflow tools"), and the console script (name + module path).

- [ ] **Step 2: Re-sync the lockfile**

```bash
uv sync
```

Expected: uv re-resolves and updates `uv.lock`. The package is now installed as `qgis-mcp-workflows` editable.

- [ ] **Step 3: Verify the new console script is registered**

```bash
uv run --no-sync qgis-mcp-workflows-server --help
```

Expected: at this stage this WILL fail with `ModuleNotFoundError: No module named 'qgis_mcp_workflows.server'` — because we have not yet updated imports inside the renamed package. That's expected; we'll fix imports in Task 7. Skip the verification for now and proceed.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: rename package to qgis-mcp-workflows, bump 1.0.0 → 1.1.0"
```

---

### Task 5: Rewrite internal references inside renamed `.py` files

**Files:** every `.py` file under `src/qgis_mcp_workflows/`, `qgis_mcp_workflows_plugin/`, `tests/`, plus `install.py`, `scripts/demo_w17.py`.

The replacements are mechanical. Run them as **token-aware find/replace** — Edit with `replace_all=True`, not blind sed — because some files contain both the package name and unrelated text using "north" or "qgis-mcp" that we don't want to touch (e.g., the upstream attribution `nkarasiak/qgis-mcp` must stay).

The replacements (in priority order — apply the longest first to avoid partial overlaps):

| Old token | New token | Notes |
|---|---|---|
| `qgis_mcp_north_plugin` | `qgis_mcp_workflows_plugin` | Plugin directory + imports |
| `QGIS_MCP_NORTH_LOG_FILE` | `QGIS_MCP_WORKFLOWS_LOG_FILE` | Env var |
| `QGIS_MCP_NORTH_LOG_LEVEL` | `QGIS_MCP_WORKFLOWS_LOG_LEVEL` | Env var |
| `QGIS_MCP_NORTH_TOOL_MODE` | `QGIS_MCP_WORKFLOWS_TOOL_MODE` | Env var |
| `QGIS_MCP_NORTH_TRANSPORT` | `QGIS_MCP_WORKFLOWS_TRANSPORT` | Env var |
| `QGIS_MCP_NORTH_QGIS_LAUNCHER` | `QGIS_MCP_WORKFLOWS_QGIS_LAUNCHER` | Env var |
| `QGIS_MCP_NORTH_REPO_ROOT` | `QGIS_MCP_WORKFLOWS_REPO_ROOT` | Env var |
| `QGIS_MCP_NORTH_HOST` | `QGIS_MCP_WORKFLOWS_HOST` | Env var |
| `QGIS_MCP_NORTH_PORT` | `QGIS_MCP_WORKFLOWS_PORT` | Env var |
| `qgis-mcp-north-server` | `qgis-mcp-workflows-server` | Console script + cli prog name |
| `qgis-mcp-north` | `qgis-mcp-workflows` | Project name everywhere except upstream attribution |
| `qgis_mcp_north` | `qgis_mcp_workflows` | Python package import |
| `QgisMcpNorthError` | `QgisMcpWorkflowsError` | Error base class |
| `QgisMcpNorthServer` | `QgisMcpWorkflowsServer` | Logger name |
| `QgisMcpNorth` | `QgisMcpWorkflows` | Any other CamelCase remnants |

**Do NOT replace:**
- `nkarasiak/qgis-mcp` (upstream attribution — appears in docstrings, metadata.txt, README)
- `qgis-mcp` in import-error messages where it refers to upstream
- `north` inside `setup_logging` or any non-package use of the word

- [ ] **Step 1: Run the package import path replacement on the source tree**

For each of the four `.py` files in `src/qgis_mcp_workflows/executors/` plus `helpers.py`, `errors.py`, `server.py`, `compound.py`, `client.py`, `__init__.py` — replace `qgis_mcp_north` with `qgis_mcp_workflows` everywhere it appears (`from qgis_mcp_north...`, `import qgis_mcp_north...`, docstrings that name the package, log messages).

Run with `replace_all=True` per file. Files to edit (10):

```
src/qgis_mcp_workflows/__init__.py
src/qgis_mcp_workflows/helpers.py
src/qgis_mcp_workflows/errors.py
src/qgis_mcp_workflows/server.py
src/qgis_mcp_workflows/compound.py
src/qgis_mcp_workflows/client.py
src/qgis_mcp_workflows/executors/__init__.py
src/qgis_mcp_workflows/executors/plugin.py
src/qgis_mcp_workflows/executors/headless.py
src/qgis_mcp_workflows/executors/headless_runner.py
```

Per file the Edit looks like:

```
Edit:
  file_path: src/qgis_mcp_workflows/server.py
  old_string: qgis_mcp_north
  new_string: qgis_mcp_workflows
  replace_all: true
```

- [ ] **Step 2: Run the same replacement on the plugin tree**

```
qgis_mcp_workflows_plugin/__init__.py
qgis_mcp_workflows_plugin/plugin.py
qgis_mcp_workflows_plugin/compat.py
```

Same Edit call per file. Note: the plugin's `plugin.py` references `qgis_mcp_north_plugin` (its own package name) in some places — those become `qgis_mcp_workflows_plugin`.

- [ ] **Step 3: Run on tests**

All files in `tests/` and `tests/integration/` and `tests/benchmarks/`. Same `qgis_mcp_north` → `qgis_mcp_workflows` replacement.

- [ ] **Step 4: Run the env-var replacements**

For each env var in the table above, run the same Edit replacement across the whole tree. Glob for files containing the env var first:

```bash
git grep -l QGIS_MCP_NORTH_HOST
git grep -l QGIS_MCP_NORTH_PORT
# ... etc
```

Then Edit each match. Typical hits: `server.py`, `helpers.py`, `executors/headless.py`, tests, `CLAUDE.md`, `README.md`.

- [ ] **Step 5: Run the class-name replacements**

```
QgisMcpNorthError → QgisMcpWorkflowsError    (errors.py + any subclass test)
QgisMcpNorthServer → QgisMcpWorkflowsServer  (server.py logger name)
```

`git grep -l QgisMcpNorth` to enumerate hits.

- [ ] **Step 6: Run the console-script + plugin-folder replacements**

```
qgis-mcp-north-server → qgis-mcp-workflows-server
qgis-mcp-north        → qgis-mcp-workflows
```

Apply the longer one first. `qgis_mcp_north_plugin` should already be gone from Step 2 above — sanity-check with `git grep qgis_mcp_north_plugin` (expect zero matches).

- [ ] **Step 7: Run the tests**

```bash
uv run --no-sync pytest tests/ -x
```

Expected: same test count as Task 1 Step 3, **all pass**. If any test fails with `ModuleNotFoundError` or `AttributeError`, you missed a reference — `git grep -i 'qgis[_-]mcp[_-]north\|qgismcpnorth'` to find it.

- [ ] **Step 8: Run lint**

```bash
uv tool run ruff check src/ tests/
```

Expected: no errors. (No new violations should be introduced by the rename.)

- [ ] **Step 9: Commit**

```bash
git add -A src/ tests/ qgis_mcp_workflows_plugin/
git commit -m "refactor: replace qgis_mcp_north → qgis_mcp_workflows in code and tests"
```

---

### Task 6: Fix the latent `importlib.metadata.version` lookup

**Files:**
- Modify: `src/qgis_mcp_workflows/helpers.py:38`

The current code looks up `"qgis-mcp"` (upstream's package name), not its own — a pre-existing bug that meant `version_match` always reported `mismatch` against the plugin in non-editable installs. Fix as part of the rename.

- [ ] **Step 1: Edit `helpers.py`**

```
Edit:
  file_path: src/qgis_mcp_workflows/helpers.py
  old_string:         server_version = importlib.metadata.version("qgis-mcp")
  new_string:         server_version = importlib.metadata.version("qgis-mcp-workflows")
```

- [ ] **Step 2: Run the relevant test**

```bash
uv run --no-sync pytest tests/ -k "diagnose or version" -v
```

Expected: any pre-existing diagnose/version test passes. If no such test exists yet, this fix is opportunistic — proceed regardless.

- [ ] **Step 3: Commit**

```bash
git add src/qgis_mcp_workflows/helpers.py
git commit -m "fix(diagnose): look up own package version not upstream's"
```

---

### Task 7: Update `install.py` paths, config keys, and prompts

**Files:**
- Modify: `install.py`

`install.py` references the old name in nine places: `PLUGIN_SRC`, `GITHUB_URL`, hardcoded `src/qgis_mcp_north/server.py` paths (×4), the `import qgis_mcp_north` venv-readiness check, the `qgis-north` MCP-client config key (×6), and the title strings shown to users.

- [ ] **Step 1: Update `PLUGIN_SRC` and `GITHUB_URL`**

```
Edit:
  file_path: install.py
  old_string: PLUGIN_SRC = REPO_DIR / "qgis_mcp_north_plugin"
GITHUB_URL = "git+https://github.com/wattwong103/qgis-mcp-north.git"
  new_string: PLUGIN_SRC = REPO_DIR / "qgis_mcp_workflows_plugin"
GITHUB_URL = "git+https://github.com/wattwong103/qgis-mcp-workflows.git"
```

Note: this assumes the GitHub repo will be renamed too. If the user prefers to keep the GitHub repo at `wattwong103/qgis-mcp-north`, leave the URL alone and document the slug-vs-package divergence in the Task 13 README edit instead.

- [ ] **Step 2: Update the venv-readiness import check**

```
Edit:
  file_path: install.py
  old_string:         [str(python), "-c", "import qgis_mcp_north"],
  new_string:         [str(python), "-c", "import qgis_mcp_workflows"],
```

- [ ] **Step 3: Update the four hardcoded server-script paths**

```
Edit:
  file_path: install.py
  old_string: src/qgis_mcp_north/server.py
  new_string: src/qgis_mcp_workflows/server.py
  replace_all: true
```

```
Edit:
  file_path: install.py
  old_string: REPO_DIR / "src" / "qgis_mcp_north" / "server.py"
  new_string: REPO_DIR / "src" / "qgis_mcp_workflows" / "server.py"
  replace_all: true
```

- [ ] **Step 4: Update the console-script name in `_remote_entry` / `_zed_remote_entry`**

```
Edit:
  file_path: install.py
  old_string: qgis-mcp-north-server
  new_string: qgis-mcp-workflows-server
  replace_all: true
```

- [ ] **Step 5: Update the MCP-client config key (`qgis-north` → `qgis-workflows`)**

```
Edit:
  file_path: install.py
  old_string: qgis-north
  new_string: qgis-workflows
  replace_all: true
```

This replaces six occurrences: in `configure_client`, `unconfigure_client`, and the three `claude mcp add` print strings.

- [ ] **Step 6: Update the user-facing print strings**

```
Edit:
  file_path: install.py
  old_string: print(f"QGIS MCP Installer ({'uninstall' if args.uninstall else 'install'})")
  new_string: print(f"QGIS MCP Workflows Installer ({'uninstall' if args.uninstall else 'install'})")
```

```
Edit:
  file_path: install.py
  old_string:     print("  1. Restart QGIS and enable the 'QGIS MCP' plugin")
  new_string:     print("  1. Restart QGIS and enable the 'QGIS MCP Workflows' plugin")
```

- [ ] **Step 7: Run the install-side tests**

```bash
uv run --no-sync pytest tests/test_install.py -v
```

Expected: all install tests pass. If anything fails, `git diff install.py` and find the missed reference.

- [ ] **Step 8: Commit**

```bash
git add install.py
git commit -m "refactor(install): rename plugin/script paths and MCP config key to qgis-workflows"
```

---

### Task 8: Update the plugin `metadata.txt`

**Files:**
- Modify: `qgis_mcp_workflows_plugin/metadata.txt`

This is the QGIS plugin manifest that gets read by QGIS at plugin-load time. It controls the user-visible name in QGIS's Plugins dialog, the description, the changelog, and the repository URLs. Also: the QGIS plugin repository rejects re-uploads at the same version (per CLAUDE.md), so the version bump must land here too.

- [ ] **Step 1: Rewrite metadata.txt**

```
[general]
name=QGIS MCP Workflows
qgisMinimumVersion=3.28
qgisMaximumVersion=4.99
description=Focused QGIS MCP plugin with workflow tools for transportation research figure pipelines (forked from nkarasiak/qgis-mcp).
about=Companion QGIS plugin for the qgis-mcp-workflows MCP server. Exposes a TCP
    socket that the server connects to so that LLMs can drive figure
    rendering, choropleths, trajectory plots, and OD-flow maps without
    leaving Claude.

    This plugin is intentionally narrow — focused on workflow tools for
    transportation research (PFLOW, GUFM, weekly-deck figure pipelines).
    For the general 51-tool QGIS-MCP surface, install nkarasiak/qgis-mcp
    side-by-side (different plugin folder, different socket port).

    Pair this plugin with the qgis-mcp-workflows MCP server. See
    https://github.com/wattwong103/qgis-mcp-workflows for setup instructions.

    Default socket port: 9877 (vs upstream nkarasiak/qgis-mcp on 9876).

version=1.1.0
author=North
email=
tags=mcp,ai,claude,llm,workflows,transportation,research,pflow,gufm
homepage=https://github.com/wattwong103/qgis-mcp-workflows
tracker=https://github.com/wattwong103/qgis-mcp-workflows/issues
repository=https://github.com/wattwong103/qgis-mcp-workflows
license=MIT
category=Plugins
icon=icons/icon.png
experimental=True
deprecated=False
changelog=1.1.0 : Rename release. The package, plugin, and console script move from
    qgis-mcp-north → qgis-mcp-workflows to put the fork's positioning (workflow tools
    not 51 PyQGIS primitives) in the name. No behavior changes. Co-existence with
    upstream nkarasiak/qgis-mcp unchanged: different plugin folder, port 9877 stays.
    Env vars renamed QGIS_MCP_NORTH_* → QGIS_MCP_WORKFLOWS_*. Installed config key
    in MCP clients changes 'qgis-north' → 'qgis-workflows'.
 1.0.0 : v1.0 — tool surface complete. Final 3 stubs shipped: qgis_style_categorized (per-class
    feature counts), qgis_style_graduated (quantile/equal_interval/natural_breaks/pretty modes + explicit
    breaks), qgis_eval (return_vars capture with _json_safe fallback). Compound mode: QGIS_MCP_WORKFLOWS_TOOL_MODE=compound
    exposes 5 grouped tools (qgis_inspect/style/render/export + qgis_eval) for token-constrained LLMs.
    Benchmarks scaffolding (tests/benchmarks/), end-to-end W17 demo (scripts/demo_w17.py + fake/plugin/headless
    integration tests), installer tests (13 cases). README rewritten. Windows-only support claim.
    99 unit tests + 3 skips + 11 deselected benchmarks.
 0.5.0 : v0.5 — five workflow tools shipped end-to-end. qgis_render_trajectory (lines/points/heatmap
    from PFLOW CSV/GPX, stride sampling + max_points ceiling, optional movingpandas speed bins);
    qgis_render_od_flows (centroid arcs over a zones layer, data-defined stroke width, unmatched zone-id reporting);
    qgis_project_load (loads .qgz + returns layers + layouts in one call); qgis_export_layout (PDF/PNG/SVG with
    n_pages); qgis_batch_render (fan-out per attribute value, manifest + per-value errors, active-layer convention).
    New errors: EmptyAfterFilterError, ProjectLoadError, LayoutNotFoundError. 37 new unit tests (66 total).
    Stubs remaining for v0.6: qgis_style_categorized, qgis_style_graduated, qgis_eval.
 0.4.0 : v0.4 — headless transport. PyQGIS subprocess executor (src/qgis_mcp_workflows/executors/headless.py)
    re-uses the plugin's QgisMCPServer.execute_command via a stub iface, so plugin and headless share one
    handler codebase. Server gains --transport={plugin,headless,auto} (default auto: probe port 9877, fall
    back to headless). qgis_load_layer(crs=...) wired through plugin's set_layer_crs with rollback on
    failure; CrsMismatchError + HeadlessUnavailableError added.
 0.3.0 : v0.3 MVP — 5 of 13 MCP tools implemented end-to-end against the plugin transport.
    Plugin: added render_layers_to_path and render_choropleth dispatch handlers.
    MCP server: implemented qgis_layer_inspect, qgis_load_layer, qgis_render_map,
    qgis_render_choropleth (memory-layer rebuild + CSV join), qgis_figures_to_pptx.
    Executor abstraction (src/qgis_mcp_workflows/executors/plugin.py) shipped — headless
    executor lands in v0.4. 25 unit tests (mocked-executor) + typed errors in errors.py.
    8 tool stubs remain.
 0.1.0 : Initial fork — scaffolding only, 13 tool stubs registered, no functional implementations yet.
    Forked from nkarasiak/qgis-mcp v0.2.1; cut to 12 workflow tools + 1 escape hatch.
    See docs/DESIGN.md for tool surface.
```

Three substantive changes vs the existing file: `name`, `description`/`about`, `version`, `tags` (added `workflows`), `homepage/tracker/repository` URLs, and a prepended `1.1.0` changelog entry. Historical changelog entries are updated only where they reference internal env-var names or paths that have changed (e.g., `QGIS_MCP_NORTH_TOOL_MODE` → `QGIS_MCP_WORKFLOWS_TOOL_MODE`, `src/qgis_mcp_north/executors/headless.py` → `src/qgis_mcp_workflows/...`).

- [ ] **Step 2: Update the LOG_TAG string in the plugin**

```
Edit:
  file_path: qgis_mcp_workflows_plugin/plugin.py
  old_string:     LOG_TAG: ClassVar[str] = "MCP-NORTH"
  new_string:     LOG_TAG: ClassVar[str] = "MCP-WORKFLOWS"
```

This controls the tab name in QGIS's Message Log.

- [ ] **Step 3: Update the plugin's `__init__.py` `classFactory` / plugin name strings**

Run:

```bash
git grep -n "QGIS MCP North\|qgis-mcp-north\|MCP-NORTH" qgis_mcp_workflows_plugin/
```

Edit each match. Most likely: `__init__.py` has a `classFactory` returning a plugin object whose `__str__` or display name references "QGIS MCP North". Replace with "QGIS MCP Workflows".

- [ ] **Step 4: Commit**

```bash
git add qgis_mcp_workflows_plugin/metadata.txt qgis_mcp_workflows_plugin/plugin.py qgis_mcp_workflows_plugin/__init__.py
git commit -m "refactor(plugin): rename plugin manifest to QGIS MCP Workflows, bump to 1.1.0"
```

---

### Task 9: Update server-side docstrings, FastMCP name, and log prefix

**Files:**
- Modify: `src/qgis_mcp_workflows/server.py`

After Task 5 the Python identifiers are renamed, but the FastMCP server `name` string, the module docstring, and the `SERVER_INSTRUCTIONS` block (LLM-visible!) still say "qgis-mcp-north".

- [ ] **Step 1: Update the module docstring**

```
Edit:
  file_path: src/qgis_mcp_workflows/server.py
  old_string: """qgis-mcp-north — focused QGIS MCP server for transportation research figures.
  new_string: """qgis-mcp-workflows — focused QGIS MCP server for transportation research figures.
```

- [ ] **Step 2: Update the SERVER_INSTRUCTIONS string (LLM-visible)**

```
Edit:
  file_path: src/qgis_mcp_workflows/server.py
  old_string: SERVER_INSTRUCTIONS = """\
qgis-mcp-north — opinionated QGIS MCP for transportation research figure
  new_string: SERVER_INSTRUCTIONS = """\
qgis-mcp-workflows — opinionated QGIS MCP for transportation research figure
```

- [ ] **Step 3: Update the FastMCP server name**

```
Edit:
  file_path: src/qgis_mcp_workflows/server.py
  old_string: mcp = FastMCP("qgis-mcp-north", instructions=SERVER_INSTRUCTIONS)
  new_string: mcp = FastMCP("qgis-mcp-workflows", instructions=SERVER_INSTRUCTIONS)
```

- [ ] **Step 4: Update the logger name**

```
Edit:
  file_path: src/qgis_mcp_workflows/server.py
  old_string:     log = logging.getLogger("QgisMcpNorthServer")
  new_string:     log = logging.getLogger("QgisMcpWorkflowsServer")
```

- [ ] **Step 5: Update the log-file default path**

```
Edit:
  file_path: src/qgis_mcp_workflows/server.py
  old_string:     default_log_file = os.path.join("~", ".local", "share", "qgis-mcp-north", "server.log")
  new_string:     default_log_file = os.path.join("~", ".local", "share", "qgis-mcp-workflows", "server.log")
```

- [ ] **Step 6: Update the CLI `prog` name and `argparse` description**

```
Edit:
  file_path: src/qgis_mcp_workflows/server.py
  old_string:     parser = argparse.ArgumentParser(prog="qgis-mcp-north-server")
  new_string:     parser = argparse.ArgumentParser(prog="qgis-mcp-workflows-server")
```

- [ ] **Step 7: Update the startup log line**

```
Edit:
  file_path: src/qgis_mcp_workflows/server.py
  old_string:     logger.info("qgis-mcp-north server starting (v1.0.0, transport=%s)", chosen)
  new_string:     logger.info("qgis-mcp-workflows server starting (v1.1.0, transport=%s)", chosen)
```

- [ ] **Step 8: Update the v0.2 scaffold comment + DESIGN.md cross-reference**

```
Edit:
  file_path: src/qgis_mcp_workflows/server.py
  old_string: Default socket port: 9877 (vs upstream nkarasiak/qgis-mcp on 9876). Both servers
  new_string: Default socket port: 9877 (vs upstream nkarasiak/qgis-mcp on 9876). Both servers
```

(No change in the literal; included as a sanity-check anchor — if the diff shows zero lines changed, the file already has the upstream-co-existence line in the form that doesn't need updating.)

- [ ] **Step 9: Update `PluginUnavailableError` message**

```
Edit:
  file_path: src/qgis_mcp_workflows/errors.py
  old_string:             f"Open QGIS, enable the 'QGIS MCP North' plugin, click Start. "
  new_string:             f"Open QGIS, enable the 'QGIS MCP Workflows' plugin, click Start. "
```

- [ ] **Step 10: Run all tests**

```bash
uv run --no-sync pytest tests/ -x
```

Expected: same test count as Task 1 Step 3, all pass. Any string-comparison tests (e.g., `test_install.py` asserting the MCP config key, or any test asserting on log/error message text) should already be updated by Task 5's mechanical sweep, but watch for orphans.

- [ ] **Step 11: Commit**

```bash
git add src/qgis_mcp_workflows/server.py src/qgis_mcp_workflows/errors.py
git commit -m "refactor: rename server/error/log strings to qgis-mcp-workflows"
```

---

### Task 10: Update `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

`CLAUDE.md` is project guidance — agents read it on every session start. It must reflect the new name.

- [ ] **Step 1: Apply mechanical replacements**

Run the same replacements as Task 5 (`qgis-mcp-north` → `qgis-mcp-workflows`, `qgis_mcp_north` → `qgis_mcp_workflows`, `qgis_mcp_north_plugin` → `qgis_mcp_workflows_plugin`, `QGIS_MCP_NORTH_*` env vars → `QGIS_MCP_WORKFLOWS_*`).

```bash
git grep -n "qgis[_-]mcp[_-]north\|QGIS_MCP_NORTH" CLAUDE.md
```

Edit each match. Roughly 20-25 hits.

- [ ] **Step 2: Update the project overview paragraph**

```
Edit:
  file_path: CLAUDE.md
  old_string: `qgis-mcp-north` (v1.0.0) is a focused fork of `nkarasiak/qgis-mcp` for transportation-research figure pipelines (PFLOW, GUFM).
  new_string: `qgis-mcp-workflows` (v1.1.0) is a focused fork of `nkarasiak/qgis-mcp` for transportation-research figure pipelines (PFLOW, GUFM). Renamed from `qgis-mcp-north` in v1.1.0 to put the fork's positioning (workflow tools, not 51 PyQGIS primitives) in the name.
```

- [ ] **Step 3: Update the "Key differences" section's plugin folder + package name lines**

Already covered by Step 1's mechanical sweep — verify with `git grep -n qgis_mcp_north CLAUDE.md` (expect zero matches).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude.md): update project guidance for rename to qgis-mcp-workflows"
```

---

### Task 11: Update `docs/DESIGN.md`

**Files:**
- Modify: `docs/DESIGN.md`

`docs/DESIGN.md` is the spec. The package-name section (§2 "Co-existence with upstream") needs updating, and the spec status header should bump.

- [ ] **Step 1: Bump the status header**

```
Edit:
  file_path: docs/DESIGN.md
  old_string: Status: **draft v0.1**, 2026-04-30
  new_string: Status: **draft v0.2 (post-rename)**, 2026-05-22
```

- [ ] **Step 2: Apply the mechanical name replacements**

```bash
git grep -n "qgis[_-]mcp[_-]north\|QGIS_MCP_NORTH\|QgisMcpNorth" docs/DESIGN.md
```

Edit each match with `replace_all=True` per pattern.

**Do NOT touch:** any line that says `nkarasiak/qgis-mcp` (upstream attribution) or that's inside the §7 milestones history (which describes the past, not the present).

Actually: re-read §7 carefully. The milestone entries from v0.1–v1.0 *do* mention `qgis_mcp_north` directories and `QGIS_MCP_NORTH_*` env vars — those are accurate descriptions of what shipped at that point in time. Decision: **leave historical milestone entries with the old names intact**, and add a new v1.1 milestone entry at the bottom.

- [ ] **Step 3: Add a v1.1 milestone entry**

```
Edit:
  file_path: docs/DESIGN.md
  old_string: **v1.0 — first real W17-style deck rendered end-to-end** from a single LLM prompt, using only `qgis-mcp-north` tools.
  new_string: **v1.0 — first real W17-style deck rendered end-to-end** from a single LLM prompt, using only `qgis-mcp-north` tools.

**v1.1 — rename release.** ✅ Shipped 2026-05-22. Package/plugin/console-script renamed to `qgis-mcp-workflows` (positioning over personal name). Env vars `QGIS_MCP_NORTH_*` → `QGIS_MCP_WORKFLOWS_*`. Co-existence with upstream unchanged: port 9877 stays, plugin folder is now `qgis_mcp_workflows_plugin`. No behavior changes; 99-test suite green before and after. CLAUDE.md, DESIGN.md, README, CHANGELOG updated; historical completion-report docs left untouched as point-in-time snapshots.
```

- [ ] **Step 4: Commit**

```bash
git add docs/DESIGN.md
git commit -m "docs(design): rename references + add v1.1 milestone entry"
```

---

### Task 12: Update `README.md` and `CONTRIBUTING.md`

**Files:**
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`

The README is the front door — most user-facing of all the docs.

- [ ] **Step 1: Apply mechanical replacements on README**

```bash
git grep -n "qgis[_-]mcp[_-]north\|QGIS_MCP_NORTH\|QgisMcpNorth" README.md
```

Edit each match. Hits will be in: title heading, install commands, config snippets (`claude mcp add qgis-north …`), env-var section, plugin-folder paths, the upstream-comparison section.

- [ ] **Step 2: Add a "Renamed from qgis-mcp-north" note near the top**

Edit immediately after the title heading. Add a single-line note:

```markdown
> Renamed from `qgis-mcp-north` in v1.1.0. The fork's positioning (workflow tools, not 51 PyQGIS primitives) is now in the name. Existing users: see the [migration note](#v110-rename-migration) below.
```

And at the bottom of the README, add the migration anchor:

```markdown
## v1.1.0 rename migration

If you previously installed `qgis-mcp-north`:

1. Re-run the installer: `python install.py` — it will install the new plugin folder (`qgis_mcp_workflows_plugin/`) and add a new MCP client config key (`qgis-workflows`).
2. Remove the old plugin folder: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/qgis_mcp_north_plugin` (Windows: `%APPDATA%\QGIS\QGIS3\…`).
3. Remove the old MCP client entry: `python install.py --uninstall --clients claude-desktop` against the `v1.0.0` version of this repo, or hand-edit `claude_desktop_config.json` to remove the `"qgis-north"` key.
4. Restart QGIS, enable the "QGIS MCP Workflows" plugin in the Plugins dialog, click Start Server.
5. Env vars: rename any `QGIS_MCP_NORTH_*` in your shell profile / launch scripts → `QGIS_MCP_WORKFLOWS_*`. Port `9877` is unchanged.
```

- [ ] **Step 3: Apply replacements on CONTRIBUTING.md**

```bash
git grep -n "qgis[_-]mcp[_-]north" CONTRIBUTING.md
```

Edit each match. Likely small (path references in dev-setup instructions).

- [ ] **Step 4: Commit**

```bash
git add README.md CONTRIBUTING.md
git commit -m "docs(readme): rename references + add v1.1.0 migration note"
```

---

### Task 13: Update `CHANGELOG.md` and `docs/pflow-usage.md`

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `docs/pflow-usage.md`

- [ ] **Step 1: Add a v1.1.0 entry at the top of CHANGELOG.md**

Add to the top (before the existing v1.0.0 entry):

```markdown
## v1.1.0 — 2026-05-22 — Rename to qgis-mcp-workflows

Pure rename release. The package, plugin, console script, env vars, and MCP-client config key all change to make the fork's positioning ("workflow tools, not 51 PyQGIS primitives") the literal name. No behavior changes; 99-test suite green before and after.

Renames:
- Package: `qgis-mcp-north` → `qgis-mcp-workflows`
- Plugin folder: `qgis_mcp_north_plugin/` → `qgis_mcp_workflows_plugin/`
- Console script: `qgis-mcp-north-server` → `qgis-mcp-workflows-server`
- Python package: `qgis_mcp_north` → `qgis_mcp_workflows`
- Env vars: `QGIS_MCP_NORTH_*` → `QGIS_MCP_WORKFLOWS_*` (HOST, PORT, TRANSPORT, TOOL_MODE, QGIS_LAUNCHER, REPO_ROOT, LOG_FILE, LOG_LEVEL)
- MCP-client config key in installer: `qgis-north` → `qgis-workflows`
- Error base class: `QgisMcpNorthError` → `QgisMcpWorkflowsError`
- Logger: `QgisMcpNorthServer` → `QgisMcpWorkflowsServer`
- Plugin LOG_TAG: `MCP-NORTH` → `MCP-WORKFLOWS`

Unchanged:
- Socket port `9877` (still distinct from upstream's 9876)
- Co-existence guarantee with upstream `nkarasiak/qgis-mcp`
- Plugin class name `QgisMCPServer` (internal; inherited from upstream)
- Tool names (`qgis_layer_inspect`, `qgis_render_choropleth`, etc.)
- Tool response shapes, error message format, escape-hatch behavior
- Historical completion-report docs (`docs/v0.3-*`, `docs/v0.4-*`, `docs/v0.5-*`, `docs/v1.0-*`) — frozen snapshots

Migration: see [README §v1.1.0 rename migration](README.md#v110-rename-migration).

Latent-bug fix folded in: `src/qgis_mcp_workflows/helpers.py:38`'s `importlib.metadata.version("qgis-mcp")` (looked up upstream's package name) is now `version("qgis-mcp-workflows")` — pre-existing diagnose mismatch.
```

- [ ] **Step 2: Apply mechanical replacements on `docs/pflow-usage.md`**

```bash
git grep -n "qgis[_-]mcp[_-]north\|QGIS_MCP_NORTH" docs/pflow-usage.md
```

Edit each match.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md docs/pflow-usage.md
git commit -m "docs(changelog): record v1.1.0 rename release"
```

---

### Task 14: Final verification — grep, lint, tests, lockfile

**Files:** none — verification only.

- [ ] **Step 1: Confirm zero residual references to the old name in tracked files**

```bash
git grep -i 'qgis[_-]mcp[_-]north\|QGIS_MCP_NORTH\|QgisMcpNorth\|MCP-NORTH\|qgis-north' -- ':!docs/v0.3-cloud-prompt.md' ':!docs/v0.3-completion-report.md' ':!docs/v0.4-completion-report.md' ':!docs/v0.5-completion-report.md' ':!docs/v1.0-completion-report.md' ':!docs/benchmarks-v0.5.md' ':!CHANGELOG.md' ':!docs/DESIGN.md' ':!qgis_mcp_workflows_plugin/metadata.txt' ':!docs/superpowers/plans/'
```

Expected: zero output. The exclusions cover:
- Historical completion reports (frozen snapshots, intentionally untouched).
- `CHANGELOG.md` (contains the rename history with old names quoted).
- `docs/DESIGN.md` (§7 milestones intentionally preserve old names).
- `qgis_mcp_workflows_plugin/metadata.txt` (changelog entries quote old names).
- The plan files themselves.

If any other file shows up, edit it and re-commit before proceeding.

- [ ] **Step 2: Run the full test suite**

```bash
uv run --no-sync pytest tests/ -v
```

Expected: all tests pass. Test count should match Task 1 Step 3.

- [ ] **Step 3: Run lint**

```bash
uv tool run ruff check src/ tests/ install.py scripts/
```

Expected: no violations.

- [ ] **Step 4: Verify the new console script works**

```bash
uv run --no-sync qgis-mcp-workflows-server --help
```

Expected: argparse help text printed, prog name shown as `qgis-mcp-workflows-server`, `--transport` flag documented.

- [ ] **Step 5: Verify the package import works**

```bash
uv run --no-sync python -c "import qgis_mcp_workflows; print(qgis_mcp_workflows.__name__)"
```

Expected: `qgis_mcp_workflows` printed (no import error).

- [ ] **Step 6: Verify the package version reads correctly**

```bash
uv run --no-sync python -c "import importlib.metadata; print(importlib.metadata.version('qgis-mcp-workflows'))"
```

Expected: `1.1.0`. (Confirms the Task 6 fix and the pyproject version bump are both live.)

- [ ] **Step 7: Commit any cleanup if Step 1 found stragglers; otherwise no commit**

- [ ] **Step 8: Tag the release**

```bash
git tag -a v1.1.0 -m "v1.1.0 — rename to qgis-mcp-workflows"
```

Do not push the tag in this plan — push happens at the merge handoff.

---

### Task 15: Optional — rename the GitHub repository (deferred)

**Files:** none — coordinated outside the repo.

**Status: not required for plan completion.** The GitHub repo can be renamed in the GitHub UI later; the local code already references `qgis-mcp-workflows` in `install.py`'s `GITHUB_URL`. If the user keeps the GitHub repo at `wattwong103/qgis-mcp-north`, revert the `GITHUB_URL` edit from Task 7 Step 1 as a follow-up commit and document the divergence in README.

If the user does rename:
1. GitHub Settings → repository name → `qgis-mcp-workflows`. GitHub auto-redirects from the old URL.
2. Re-push the v1.1.0 tag: `git push --tags`.
3. Update `git remote set-url origin git@github.com:wattwong103/qgis-mcp-workflows.git` on every clone.
4. Update QGIS plugin repository submission (if the plugin was ever uploaded — per CLAUDE.md the repo "rejects re-uploads at the same version", so the new submission would be at 1.1.0).

---

## Self-Review

**Spec coverage:**

- Package name → ✅ Task 4 (pyproject), Task 5 (imports)
- Plugin folder → ✅ Task 3 (directory), Task 8 (metadata)
- Console script → ✅ Task 4, Task 9 (CLI)
- Env vars (8 of them) → ✅ Task 5 Step 4
- Class names (error base, logger) → ✅ Task 5 Step 5, Task 9
- LOG_TAG (plugin-side) → ✅ Task 8 Step 2
- Plugin user-visible name → ✅ Task 8 Step 1 + Step 3
- MCP-client config key → ✅ Task 7 Step 5
- pyproject version bump (1.0.0 → 1.1.0) → ✅ Task 4
- Plugin version bump (1.0.0 → 1.1.0) → ✅ Task 8 Step 1
- Latent `importlib.metadata.version("qgis-mcp")` bug → ✅ Task 6
- CLAUDE.md, DESIGN.md, README.md, CONTRIBUTING.md, CHANGELOG.md, docs/pflow-usage.md → ✅ Tasks 10–13
- Historical completion reports left untouched → ✅ explicit in Task 11 + Task 14 Step 1 excludes
- Tag the release → ✅ Task 14 Step 8

**Placeholder scan:** none. Every step shows complete code or exact commands.

**Type consistency:** all renamed identifiers (`qgis_mcp_workflows`, `qgis_mcp_workflows_plugin`, `QgisMcpWorkflowsError`, `QgisMcpWorkflowsServer`, `QGIS_MCP_WORKFLOWS_*`, `qgis-mcp-workflows-server`, `qgis-workflows`) are spelled identically across every task that uses them — verified by re-reading the plan.

**Dependent plans:** Plans 2 (link-density) and 3 (vault-ingest) will be written against the post-rename names. If Plan 1 fails to merge, the other two plans need their import paths revised accordingly — but per the proposed execution order, Plan 1 lands first.
