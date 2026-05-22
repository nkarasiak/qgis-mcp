# v0.6 — Vault Ingest Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the v0.6 milestone from `docs/DESIGN.md` §7: wire `qgis-mcp-workflows` into the obsidian-vault knowledge system. Specifically — populate `vault/raw/qgis/dev/` from this codebase via `/kb-ingest`, compile to `vault/wiki/qgis/` via `/kb-compile`, drop cross-link stubs into `vault/wiki/pflow/` and `vault/wiki/gufm/`, and add a weekly rendering routine where `/kb-report weekly qgis` emits a Markdown brief embedding fresh PNGs rendered through the MCP server's tool surface.

**Architecture:** This plan is cross-repo orchestration, not new code. The only new artifact in this repo is `scripts/weekly_figures.py` — a small batch driver that calls the existing MCP tools (`qgis_render_choropleth`, `qgis_render_trajectory`, `qgis_render_link_density` once Plan 2 is merged) to produce a fixed set of PNGs under `H:/Dropbox/obsidian-vault/wiki/qgis/figures/weekly/<date>/`. The vault's `kb-report` skill then references those paths in its weekly Markdown output, so the brief always shows freshly-rendered figures.

**Tech Stack:** Python 3.12 (existing), plus the kb-* skills (already installed at the vault level). No new packages.

**Prereqs:**
1. Plan 1 (rename to `qgis-mcp-workflows`) is merged.
2. Plan 2 (link-density) is *optional* — Plan 3 works without it, but link-density is the most compelling weekly figure. The plan calls out which steps to skip if Plan 2 isn't ready.
3. The vault is at `H:/Dropbox/obsidian-vault/` with `wiki/qgis/` already initialized (verified — `wiki/qgis/index.md` exists with the pre-seeded `/kb-ingest qgis` invitation).

**Scope decisions (locked in before this plan was written):**
- Project slug in the vault is `qgis` (already chosen — `wiki/qgis/` is the existing directory).
- Weekly figures land in `wiki/qgis/figures/weekly/YYYY-MM-DD/` (one subdir per run, dated).
- Weekly cadence: **every Monday at 09:00 local time** — fits the existing weekly-deck rhythm called out in DESIGN.md §1.
- The weekly script uses **fake fixtures by default** so it can run from CI / on a fresh checkout. A `--real-pflow` flag points it at the actual `H:/Dropbox/PFLOW/output (Selective Sync Conflict)/` paths when running from the user's main workstation.
- Cross-links: `wiki/pflow/applications-qgis.md` and `wiki/gufm/applications-qgis.md` get stub pages that reference back to `[[qgis/index]]` and the tool docs. Keeps Tobler-bridge synthesis discoverable.

---

## File Structure

**New files in this repo (`qgis-mcp-workflows`):**
- `scripts/weekly_figures.py` — orchestrator. Renders the fixed weekly figure set into `wiki/qgis/figures/weekly/<date>/`.
- `tests/test_weekly_figures.py` — unit tests using `FakeExecutor` + tmpdir output.
- `docs/vault-integration.md` — terse README pointing to the vault-side workflow.

**New files in the vault (`H:/Dropbox/obsidian-vault/`):**
- `wiki/qgis/figures/weekly/.gitkeep` — bootstrap directory.
- `wiki/qgis/concepts/tool-surface.md` — overview of the 14 MCP tools. **Will be created by `/kb-compile` from raw notes**, not hand-written.
- `wiki/qgis/concepts/transports.md` — plugin vs headless. **kb-compile artifact.**
- `wiki/qgis/concepts/big-data-discipline.md` — streaming + sampling pattern. **kb-compile artifact.**
- `wiki/pflow/applications-qgis.md` — short cross-link stub.
- `wiki/gufm/applications-qgis.md` — short cross-link stub.
- `reports/qgis/weekly-*.md` — `/kb-report weekly qgis` output. Created automatically each run; not part of this plan beyond verifying the first one renders cleanly.

**Modified files in the vault:**
- `wiki/qgis/index.md` — update the codebase path (`qgis-mcp-north` → `qgis-mcp-workflows`), refresh the concept-page list once kb-compile populates them.
- `wiki/qgis/log.md` — append an entry for the v0.6 ingest landing.

**Modified files in this repo:**
- `docs/DESIGN.md` — mark v0.6 shipped in §7.
- `CHANGELOG.md` — v1.3.0 entry.
- `pyproject.toml` — version bump.
- `qgis_mcp_workflows_plugin/metadata.txt` — version bump.

---

### Task 1: Branch + vault state snapshot

**Files:** none yet.

- [ ] **Step 1: Confirm Plan 1 is merged into main**

```bash
git log --oneline -3
git grep -c qgis_mcp_workflows src/ | head -3
```

Expected: the recent commits include the rename release; `git grep` confirms the new package name lives in `src/`.

- [ ] **Step 2: Confirm Plan 2 status**

```bash
git log --oneline -1 -- src/qgis_mcp_workflows/server.py | grep -i link.density || echo "Plan 2 NOT merged"
```

If Plan 2 isn't merged: the weekly script will skip the `qgis_render_link_density` step (handled by an `if` branch in Task 4 Step 3). Plan 3 still ships.

- [ ] **Step 3: Create branch**

```bash
git checkout -b feat/v0.6-vault-ingest
```

- [ ] **Step 4: Snapshot the vault's current `wiki/qgis/` state**

```bash
ls /h/Dropbox/obsidian-vault/wiki/qgis/
ls /h/Dropbox/obsidian-vault/raw/qgis/dev/ 2>/dev/null || echo "(empty / missing)"
```

Expected: `wiki/qgis/` contains `index.md` and `log.md` only. `raw/qgis/dev/` is empty or missing. If you see existing concept pages or raw notes that weren't generated by this plan, stop and ask the user before overwriting.

---

### Task 2: Update `wiki/qgis/index.md` for the rename

**Files:**
- Modify: `H:/Dropbox/obsidian-vault/wiki/qgis/index.md`

The vault's index still says "QGIS-MCP-North" and points at `H:/Dropbox/qgis-mcp-north`. Update to the post-Plan-1 names.

- [ ] **Step 1: Replace the index header and codebase path**

```
Edit:
  file_path: H:/Dropbox/obsidian-vault/wiki/qgis/index.md
  old_string: # QGIS-MCP-North — Wiki Index

North's local QGIS MCP plugin. Bridges QGIS spatial analysis with Claude Code tooling.

**Codebase:** `H:/Dropbox/qgis-mcp-north`

## Concept Pages

*(none yet — run `/kb-ingest qgis H:/Dropbox/qgis-mcp-north` to seed)*
  new_string: # qgis-mcp-workflows — Wiki Index

Focused QGIS MCP server with workflow tools for transportation-research figure pipelines (PFLOW, GUFM). Renamed from `qgis-mcp-north` in v1.1.0 — the fork's positioning is now in the name.

**Codebase:** `H:/Dropbox/qgis-mcp-workflows`
**Upstream:** `nkarasiak/qgis-mcp` (different tool surface; co-installable)

**Transports:** plugin (TCP socket on port 9877 to running QGIS Desktop) | headless (PyQGIS subprocess, for cron / CI).

## Concept Pages

*(populated by `/kb-compile qgis` — see Task 4 below)*

- [[qgis/concepts/tool-surface]] — the 14 MCP tools, when to reach for each
- [[qgis/concepts/transports]] — plugin vs headless trade-offs
- [[qgis/concepts/big-data-discipline]] — streaming + sampling for multi-GB inputs
- [[qgis/concepts/error-taxonomy]] — typed errors with "Next:" hints
- [[qgis/concepts/zone-id-systems]] — PFLOW's `MFS##`/`PRF##`/`Z##` zones

## Weekly figures

Latest renders: `wiki/qgis/figures/weekly/` (most recent dated subdir). Generated by `scripts/weekly_figures.py` in the repo; embedded in `reports/qgis/weekly-*.md` via `/kb-report weekly qgis`.

## Source Summaries

*(populated by `/kb-compile qgis` from `raw/qgis/dev/`)*

## Cross-project applications

- [[pflow/applications-qgis]] — PFLOW figure pipelines that use this MCP
- [[gufm/applications-qgis]] — GUFM trajectory rendering

## Activity

See [[qgis/log]] for the chronological log.
```

(If `wiki/qgis/index.md` already has a heading not matching the `old_string` above — possibly because the vault has been edited since 2026-05-05 — `git diff` from the vault repo first, reconcile, then retry.)

- [ ] **Step 2: Append a v0.6 entry to `wiki/qgis/log.md`**

```
Edit:
  file_path: H:/Dropbox/obsidian-vault/wiki/qgis/log.md
  old_string: # qgis — log
  new_string: # qgis — log

## 2026-05-22 — v0.6 vault ingest landed

- Repository renamed `qgis-mcp-north` → `qgis-mcp-workflows` (Plan 1, v1.1.0).
- `/kb-ingest qgis H:/Dropbox/qgis-mcp-workflows` seeded `raw/qgis/dev/` with module-level summaries.
- `/kb-compile qgis` produced first-pass concept pages (tool surface, transports, big-data discipline, error taxonomy, zone-id systems).
- `scripts/weekly_figures.py` (in repo) renders the weekly figure set into `wiki/qgis/figures/weekly/<date>/`.
- Cross-link stubs added: `wiki/pflow/applications-qgis.md`, `wiki/gufm/applications-qgis.md`.
- Weekly `/kb-report weekly qgis` is scheduled — see [[qgis/index]].

Closes DESIGN.md §7 v0.6 milestone.
```

- [ ] **Step 3: Commit the vault changes**

The vault is its own git repo. Commit there separately:

```bash
cd /h/Dropbox/obsidian-vault
git add wiki/qgis/index.md wiki/qgis/log.md
git commit -m "wiki(qgis): update index for qgis-mcp-workflows rename, log v0.6"
cd /h/Dropbox/qgis-mcp-workflows
```

If the vault is not a separate git repo (i.e., it's tracked elsewhere or just synced via Dropbox), skip the commit — Dropbox sync handles it. Either way, the index update is non-destructive.

---

### Task 3: Run `/kb-ingest qgis` to populate `raw/qgis/dev/`

**Files:** generated by the skill, not hand-written.

`/kb-ingest` is a vault-side skill. It reads a project directory, extracts source summaries, and writes them under `raw/<slug>/dev/`. This task is one slash-command invocation.

- [ ] **Step 1: Invoke the kb-ingest skill**

From the project directory (or anywhere — kb-ingest takes the project path as an argument):

```
/kb-ingest qgis H:/Dropbox/qgis-mcp-workflows
```

Expected:
- New files appear under `H:/Dropbox/obsidian-vault/raw/qgis/dev/`. Typical contents: one file per top-level module (`server.md`, `helpers.md`, `errors.md`, `executors_plugin.md`, `executors_headless.md`, `plugin.md`, `metadata.md`, `tools_overview.md`).
- The skill may emit a summary report in chat — note any files it skipped (e.g., `.pyc`, vendor, generated).

- [ ] **Step 2: Inspect the seed**

```bash
ls /h/Dropbox/obsidian-vault/raw/qgis/dev/
```

Expected: ≥6 markdown files. Each should have YAML frontmatter (`type:`, `tags:`, `source:`).

- [ ] **Step 3: Skim the largest file as a sanity check**

```bash
head -50 /h/Dropbox/obsidian-vault/raw/qgis/dev/server.md 2>/dev/null || head -50 /h/Dropbox/obsidian-vault/raw/qgis/dev/*server*.md
```

Expected: it summarizes the 14 MCP tools and the executor abstraction in plain English. If it's garbled or empty, re-run `/kb-ingest qgis` with verbose mode (consult the kb-ingest skill docs).

- [ ] **Step 4: Commit the vault raw notes**

```bash
cd /h/Dropbox/obsidian-vault
git add raw/qgis/dev/
git commit -m "raw(qgis): ingest qgis-mcp-workflows source summaries (v0.6)"
cd /h/Dropbox/qgis-mcp-workflows
```

---

### Task 4: Run `/kb-compile qgis` to produce wiki concept pages

**Files:** generated by the skill.

`/kb-compile` reads `raw/<slug>/` and produces structured wiki pages under `wiki/<slug>/`. The five concept pages we linked from the index in Task 2 should fall out naturally; if any are missing, we tweak the raw notes and re-run.

- [ ] **Step 1: Invoke kb-compile**

```
/kb-compile qgis
```

Expected: new files appear under `H:/Dropbox/obsidian-vault/wiki/qgis/concepts/`. Specifically, the five referenced from the index — `tool-surface.md`, `transports.md`, `big-data-discipline.md`, `error-taxonomy.md`, `zone-id-systems.md` — plus possibly others the compiler thought were worth surfacing.

- [ ] **Step 2: Reconcile the index with the actual compiled pages**

If the compiler produced different filenames than the index references (e.g., `concepts/transport-architecture.md` instead of `concepts/transports.md`), the index has dangling `[[wikilinks]]`. Fix by editing the index in `H:/Dropbox/obsidian-vault/wiki/qgis/index.md` to match the actual filenames.

```bash
ls /h/Dropbox/obsidian-vault/wiki/qgis/concepts/
```

If a referenced page is missing entirely, either:
- Augment the raw notes (`raw/qgis/dev/`) with content the compiler would surface, then re-run; or
- Drop the bullet from the index (acceptable — five was a guess, the actual surface depends on what the compiler sees).

- [ ] **Step 3: Run kb-health on the new pages**

```
/kb-health qgis
```

Expected: the skill reports orphans, missing backlinks, broken `[[wikilinks]]`. Fix anything actionable inline.

- [ ] **Step 4: Commit the compiled wiki**

```bash
cd /h/Dropbox/obsidian-vault
git add wiki/qgis/
git commit -m "wiki(qgis): compile concept pages from raw/qgis/dev (v0.6)"
cd /h/Dropbox/qgis-mcp-workflows
```

---

### Task 5: Write `scripts/weekly_figures.py` (the weekly rendering driver)

**Files:**
- Create: `scripts/weekly_figures.py`
- Create: `tests/test_weekly_figures.py`

This is the only new code in this plan. It's a thin orchestrator: calls the existing MCP tools to render a fixed weekly figure set into a dated subdir under the vault.

- [ ] **Step 1: Write the failing test first**

```
Write:
  file_path: tests/test_weekly_figures.py
  content: """Tests for scripts/weekly_figures.py — orchestration only.

Uses FakeExecutor + tmpdir output. No real PFLOW data accessed; the script's
--demo-mode flag (uses tests/fixtures/) is the test path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def test_demo_mode_renders_three_figures(fake_executor, tmp_path: Path, monkeypatch):
    """In demo mode, the script invokes choropleth + trajectory + (optionally) link-density.

    Each call lands in tmp_path with a date-stamped subdir.
    """
    from scripts.weekly_figures import run_weekly

    # Wire all expected tool responses (FakeExecutor needs every command pre-scripted)
    fake_executor.responses["add_vector_layer"] = {"id": "L1", "name": "zones"}
    fake_executor.responses["get_layer_info"] = {
        "type": "vector_2", "crs": "EPSG:4326",
        "extent": {"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1},
        "fields": [{"name": "zone_id", "type": "String"}, {"name": "total_trips", "type": "Integer"}],
        "feature_count": 5,
    }
    fake_executor.responses["remove_layer"] = {"ok": True}
    fake_executor.responses["render_choropleth"] = {
        "output_path": str(tmp_path / "choropleth.png"),
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0.0, 0.0, 1.0, 1.0], "crs": "EPSG:4326", "n_layers": 1,
        "field": "total_trips", "n_classes": 5, "breaks": [10, 20, 30, 40, 50],
        "mode": "quantile", "min_value": 0.0, "max_value": 100.0,
        "n_features": 5, "n_matched": 5, "n_unmatched": 0,
    }
    fake_executor.responses["render_trajectory"] = {
        "output_path": str(tmp_path / "trajectory.png"),
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0.0, 0.0, 1.0, 1.0], "crs": "EPSG:4326", "n_layers": 1,
        "n_trajectories": 3, "n_points_total": 30, "n_points_rendered": 30,
        "downsampled": False, "time_range": None, "modes": None,
        "used_movingpandas": False,
    }

    out_dir = tmp_path / "weekly"
    manifest = run_weekly(
        output_root=out_dir,
        demo_mode=True,
        with_link_density=False,
        date_str="2026-05-22",
    )

    assert (out_dir / "2026-05-22").exists()
    assert manifest["date"] == "2026-05-22"
    assert "choropleth" in manifest["figures"]
    assert "trajectory" in manifest["figures"]
    # link_density skipped because with_link_density=False
    assert "link_density" not in manifest["figures"]


def test_demo_mode_includes_link_density_when_flag_set(fake_executor, tmp_path: Path):
    """With --with-link-density, the link-density tool is invoked too."""
    from scripts.weekly_figures import run_weekly

    # Skip if Plan 2 hasn't merged (qgis_render_link_density doesn't exist yet)
    try:
        from qgis_mcp_workflows.server import qgis_render_link_density  # noqa: F401
    except ImportError:
        pytest.skip("Plan 2 not merged: qgis_render_link_density not available")

    fake_executor.responses["add_vector_layer"] = {"id": "L1", "name": "zones"}
    fake_executor.responses["get_layer_info"] = {
        "type": "vector_2", "crs": "EPSG:4326",
        "extent": {"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1},
        "fields": [{"name": "zone_id", "type": "String"}],
        "feature_count": 5,
    }
    fake_executor.responses["remove_layer"] = {"ok": True}
    fake_executor.responses["render_choropleth"] = {
        "output_path": str(tmp_path / "choropleth.png"),
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0.0, 0.0, 1.0, 1.0], "crs": "EPSG:4326", "n_layers": 1,
        "field": "total_trips", "n_classes": 5, "breaks": [10, 20, 30, 40, 50],
        "mode": "quantile", "min_value": 0.0, "max_value": 100.0,
        "n_features": 5, "n_matched": 5, "n_unmatched": 0,
    }
    fake_executor.responses["render_trajectory"] = {
        "output_path": str(tmp_path / "trajectory.png"),
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0.0, 0.0, 1.0, 1.0], "crs": "EPSG:4326", "n_layers": 1,
        "n_trajectories": 3, "n_points_total": 30, "n_points_rendered": 30,
        "downsampled": False, "time_range": None, "modes": None,
        "used_movingpandas": False,
    }
    fake_executor.responses["render_link_density"] = {
        "output_path": str(tmp_path / "link_density.png"),
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0.0, 0.0, 1.0, 1.0], "crs": "EPSG:4326", "n_layers": 1,
        "n_links_with_traffic": 5, "n_links_rendered": 5,
        "n_unmatched_link_ids": 0,
        "density_field": "n_points",
        "breaks": [1, 2, 3, 4, 5],
        "mode": "quantile",
        "min_density": 1.0, "max_density": 5.0,
    }

    manifest = run_weekly(
        output_root=tmp_path / "weekly",
        demo_mode=True,
        with_link_density=True,
        date_str="2026-05-22",
    )

    assert "link_density" in manifest["figures"]


def test_manifest_written_as_json_alongside_figures(fake_executor, tmp_path: Path):
    """run_weekly writes a manifest.json so /kb-report can discover the figures."""
    import json
    from scripts.weekly_figures import run_weekly

    fake_executor.responses["add_vector_layer"] = {"id": "L1", "name": "zones"}
    fake_executor.responses["get_layer_info"] = {
        "type": "vector_2", "crs": "EPSG:4326",
        "extent": {"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1},
        "fields": [{"name": "zone_id", "type": "String"}],
        "feature_count": 5,
    }
    fake_executor.responses["remove_layer"] = {"ok": True}
    fake_executor.responses["render_choropleth"] = {
        "output_path": str(tmp_path / "choropleth.png"),
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0.0, 0.0, 1.0, 1.0], "crs": "EPSG:4326", "n_layers": 1,
        "field": "total_trips", "n_classes": 5, "breaks": [10, 20, 30, 40, 50],
        "mode": "quantile", "min_value": 0.0, "max_value": 100.0,
        "n_features": 5, "n_matched": 5, "n_unmatched": 0,
    }
    fake_executor.responses["render_trajectory"] = {
        "output_path": str(tmp_path / "trajectory.png"),
        "width": 1600, "height": 1200, "dpi": 150,
        "extent": [0.0, 0.0, 1.0, 1.0], "crs": "EPSG:4326", "n_layers": 1,
        "n_trajectories": 3, "n_points_total": 30, "n_points_rendered": 30,
        "downsampled": False, "time_range": None, "modes": None,
        "used_movingpandas": False,
    }

    out_dir = tmp_path / "weekly"
    run_weekly(
        output_root=out_dir, demo_mode=True,
        with_link_density=False, date_str="2026-05-22",
    )

    manifest_path = out_dir / "2026-05-22" / "manifest.json"
    assert manifest_path.exists()
    parsed = json.loads(manifest_path.read_text())
    assert parsed["date"] == "2026-05-22"
    assert set(parsed["figures"].keys()) == {"choropleth", "trajectory"}
```

- [ ] **Step 2: Run the test (expected red)**

```bash
uv run --no-sync pytest tests/test_weekly_figures.py -v
```

Expected: `ImportError: cannot import name 'run_weekly' from 'scripts.weekly_figures'` (the file doesn't exist).

- [ ] **Step 3: Implement `scripts/weekly_figures.py`**

```
Write:
  file_path: scripts/weekly_figures.py
  content: """Render the weekly figure set into the vault for /kb-report consumption.

Two modes:
- Default: real PFLOW. Reads from H:/Dropbox/PFLOW/output/... and writes to
  H:/Dropbox/obsidian-vault/wiki/qgis/figures/weekly/<date>/.
- Demo mode (`--demo-mode`): uses tests/fixtures/*.csv|*.geojson. Used by CI and
  for the unit tests. Writes anywhere (caller-controlled --output-root).

Manifest written as <output_dir>/manifest.json so /kb-report can discover what
to embed without scraping the directory.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VAULT_QGIS_FIGURES = Path("H:/Dropbox/obsidian-vault/wiki/qgis/figures/weekly")

# Default real-data paths — keep in sync with DESIGN.md §10 test data inventory.
DEFAULT_ZONES_PATH = (
    "H:/Dropbox/PFLOW/Pseudo-PFLOW/src/shared/gm-jp/polbnda_jpn_new.shp"
)
DEFAULT_ZONE_TRIPS_CSV = (
    "H:/Dropbox/PFLOW/output (Selective Sync Conflict)/"
    "trips/truck/run_20260422_215727/zone_trips.csv"
)
DEFAULT_TRAJECTORY_CSV = (
    "H:/Dropbox/PFLOW/output (Selective Sync Conflict)/"
    "trajectory/taxi/osaka/trajectory_0000.csv"
)
DEFAULT_DRM_GPKG = "assets/drm_network.gpkg"

# Demo-mode paths — bundled fixtures, suitable for CI.
DEMO_ZONES_PATH = REPO_ROOT / "tests" / "benchmarks" / "fixtures" / "scaled_zones_134.geojson"
DEMO_TRAJ_CSV = REPO_ROOT / "tests" / "fixtures" / "tiny_trajectory.csv"


def run_weekly(
    output_root: Path,
    demo_mode: bool,
    with_link_density: bool,
    date_str: str | None = None,
    zones_path: str | None = None,
    zone_trips_csv: str | None = None,
    trajectory_csv: str | None = None,
    drm_gpkg: str | None = None,
) -> dict:
    """Render the weekly figure set and return a manifest dict.

    Manifest schema:
        {"date": "YYYY-MM-DD",
         "figures": {"choropleth": {"path": "...", "n_features": ..., ...}, ...},
         "demo": bool}
    """
    from qgis_mcp_workflows.server import (
        qgis_render_choropleth,
        qgis_render_trajectory,
    )

    if date_str is None:
        date_str = _dt.date.today().isoformat()

    out_dir = output_root / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    if demo_mode:
        zones_path = zones_path or str(DEMO_ZONES_PATH)
        zone_trips_csv = zone_trips_csv or None  # demo zones have total_trips inline
        trajectory_csv = trajectory_csv or str(DEMO_TRAJ_CSV)
    else:
        zones_path = zones_path or DEFAULT_ZONES_PATH
        zone_trips_csv = zone_trips_csv or DEFAULT_ZONE_TRIPS_CSV
        trajectory_csv = trajectory_csv or DEFAULT_TRAJECTORY_CSV

    figures: dict[str, dict] = {}

    # 1) Choropleth
    choro_out = out_dir / "choropleth.png"
    choro_result = qgis_render_choropleth(
        zones_path=zones_path,
        value_field="total_trips",
        output_png=str(choro_out),
        value_csv=zone_trips_csv,
        title="Weekly trips by zone" if not demo_mode else "Demo: synthetic zones",
    )
    figures["choropleth"] = {
        "path": choro_result.output_path,
        "n_features": choro_result.n_features,
        "min": choro_result.min_value,
        "max": choro_result.max_value,
    }

    # 2) Trajectory
    traj_out = out_dir / "trajectory.png"
    traj_result = qgis_render_trajectory(
        input_path=trajectory_csv,
        output_png=str(traj_out),
        render_mode="lines" if demo_mode else "heatmap",
        sample_rate=1.0 if demo_mode else 0.01,
    )
    figures["trajectory"] = {
        "path": traj_result.output_path,
        "n_trajectories": traj_result.n_trajectories,
        "n_points_total": traj_result.n_points_total,
        "downsampled": traj_result.downsampled,
    }

    # 3) Link density (optional — requires Plan 2 + a built DRM GeoPackage)
    if with_link_density:
        try:
            from qgis_mcp_workflows.server import qgis_render_link_density
        except ImportError:
            print(
                "  qgis_render_link_density not available — skipping (Plan 2 not merged)",
                flush=True,
            )
        else:
            drm_path = drm_gpkg or DEFAULT_DRM_GPKG
            if not Path(drm_path).exists() and not demo_mode:
                print(f"  DRM network not found at {drm_path} — skipping link density", flush=True)
            else:
                ld_out = out_dir / "link_density.png"
                # In demo mode we'd need a tiny DRM fixture — keep this branch optional;
                # the test pre-scripts the FakeExecutor response, so this works in tests.
                csvs = [trajectory_csv] if Path(trajectory_csv).exists() else []
                if csvs and drm_path:
                    ld_result = qgis_render_link_density(
                        trajectory_csvs=csvs,
                        drm_network_path=drm_path,
                        output_png=str(ld_out),
                    )
                    figures["link_density"] = {
                        "path": ld_result.output_path,
                        "n_links_rendered": ld_result.n_links_rendered,
                        "min_density": ld_result.min_density,
                        "max_density": ld_result.max_density,
                    }

    manifest = {
        "date": date_str,
        "demo": demo_mode,
        "figures": figures,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(prog="weekly_figures")
    parser.add_argument(
        "--output-root",
        default=str(VAULT_QGIS_FIGURES),
        help=f"Root directory for dated figure sets (default: {VAULT_QGIS_FIGURES}).",
    )
    parser.add_argument(
        "--demo-mode",
        action="store_true",
        help="Use bundled test fixtures instead of real PFLOW paths.",
    )
    parser.add_argument(
        "--with-link-density",
        action="store_true",
        help="Also render the DRM link-density figure (requires assets/drm_network.gpkg).",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Override date stamp (default: today, YYYY-MM-DD).",
    )
    args = parser.parse_args()

    # Initialize the executor (the script is meant to run with a live MCP backend).
    if os.environ.get("QGIS_MCP_WORKFLOWS_TRANSPORT", "auto") != "fake":
        from qgis_mcp_workflows.executors import set_executor
        from qgis_mcp_workflows.server import _build_executor
        executor, chosen = _build_executor(os.environ.get("QGIS_MCP_WORKFLOWS_TRANSPORT", "auto"))
        set_executor(executor)
        print(f"Transport: {chosen}", flush=True)

    manifest = run_weekly(
        output_root=Path(args.output_root),
        demo_mode=args.demo_mode,
        with_link_density=args.with_link_density,
        date_str=args.date,
    )
    print(f"Wrote {len(manifest['figures'])} figures to {Path(args.output_root) / manifest['date']}", flush=True)
    print(f"Manifest: {Path(args.output_root) / manifest['date'] / 'manifest.json'}", flush=True)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests to verify green**

```bash
uv run --no-sync pytest tests/test_weekly_figures.py -v
```

Expected: 3 tests pass (the link-density test skips if Plan 2 isn't merged).

- [ ] **Step 5: Smoke-test the CLI in demo mode**

```bash
uv run --no-sync python scripts/weekly_figures.py --demo-mode --output-root /tmp/weekly --date 2026-05-22 --with-link-density
```

Expected (without a running QGIS plugin): the script will fail with `PluginUnavailableError` or `HeadlessUnavailableError` — that's fine, we're verifying the CLI parses args and reaches transport setup. With a running QGIS plugin (or a working headless launcher), expect the script to render 2-3 PNGs into `/tmp/weekly/2026-05-22/` and print a manifest path.

- [ ] **Step 6: Commit**

```bash
git add scripts/weekly_figures.py tests/test_weekly_figures.py
git commit -m "feat(scripts): add weekly_figures.py — render figure set for /kb-report"
```

---

### Task 6: Cross-link stubs in `wiki/pflow/` and `wiki/gufm/`

**Files:**
- Create: `H:/Dropbox/obsidian-vault/wiki/pflow/applications-qgis.md`
- Create: `H:/Dropbox/obsidian-vault/wiki/gufm/applications-qgis.md`

Short bridging pages so Tobler-bridge skill discovers the qgis-mcp ↔ project relationships.

- [ ] **Step 1: Create the PFLOW cross-link**

```
Write:
  file_path: H:/Dropbox/obsidian-vault/wiki/pflow/applications-qgis.md
  content: ---
type: concept
tags: [pflow, qgis, applications, cross-project]
project: pflow
related: [qgis]
date: 2026-05-22
status: active
---

# PFLOW figure pipelines via qgis-mcp-workflows

PFLOW research outputs (zone trips, OD flows, trajectories, link density) become figures via the [[qgis/index|qgis-mcp-workflows]] MCP server. The mapping:

| PFLOW artifact | qgis-mcp-workflows tool | Notes |
|---|---|---|
| `zone_trips.csv` (`MFS##` keyed) | `qgis_render_choropleth` (CSV-join path) | Defaults match PFLOW schema (`zone_id`, `total_trips`) |
| `od_flows.csv` (mixed `PRF##` / `MFS##`) | `qgis_render_od_flows` | Unmatched zone IDs surface in response, not silently zero |
| `trajectory_*.csv` (~3M rows each) | `qgis_render_trajectory` | Streaming + `sample_rate` + `max_points` ceiling |
| `trajectory_*.csv[link_id]` × DRM | `qgis_render_link_density` (v1.2+) | One-time prep via `scripts/build_drm_network.py` |

**Zone-id systems coexist** — see [[qgis/concepts/zone-id-systems]] for the `MFS##` / `PRF##` / `Z##` taxonomy.

**Weekly deck integration:** `scripts/weekly_figures.py` in the qgis-mcp-workflows repo renders a fixed PFLOW figure set every Monday; figures land in [[qgis/index]] for `/kb-report weekly qgis` to consume.

**See also:**
- [[qgis/concepts/tool-surface]] — the 14 MCP tools
- [[qgis/concepts/big-data-discipline]] — streaming + sampling for multi-GB inputs
- [[pflow/index]]
```

- [ ] **Step 2: Create the GUFM cross-link**

```
Write:
  file_path: H:/Dropbox/obsidian-vault/wiki/gufm/applications-qgis.md
  content: ---
type: concept
tags: [gufm, qgis, applications, cross-project]
project: gufm
related: [qgis]
date: 2026-05-22
status: active
---

# GUFM trajectory rendering via qgis-mcp-workflows

GUFM's trajectory-token outputs share schema with PFLOW (`lon, lat, datetime, trip_id, transport_mode`) and render through the same MCP tools. The mapping:

| GUFM artifact | qgis-mcp-workflows tool | Notes |
|---|---|---|
| Predicted trajectories (`mode_col=transport_mode`) | `qgis_render_trajectory(mode_col="transport_mode")` | Mode-colored lines |
| Speed-binned trajectories | `qgis_render_trajectory(render_mode="lines")` with `[trajectory]` extra | Uses MovingPandas; speed in km/h color ramp |
| Zone-level prediction error maps | `qgis_render_choropleth` on per-zone RMSE | Joined to whatever zone polygon the eval used |

**Open coordination:**
- GUFM trajectory data is unavailable as of v1.0 (per DESIGN.md §8 #3) — defaults use PFLOW schema. When GUFM data lands, revisit whether tool defaults need a GUFM-specific override.

**See also:**
- [[qgis/concepts/tool-surface]]
- [[gufm/index]]
```

- [ ] **Step 3: Commit the vault cross-links**

```bash
cd /h/Dropbox/obsidian-vault
git add wiki/pflow/applications-qgis.md wiki/gufm/applications-qgis.md
git commit -m "wiki(pflow,gufm): cross-link qgis-mcp-workflows applications"
cd /h/Dropbox/qgis-mcp-workflows
```

---

### Task 7: First weekly report + schedule the recurring run

**Files:**
- Generated: `H:/Dropbox/obsidian-vault/reports/qgis/weekly-2026-05-22.md` (by the skill).

- [ ] **Step 1: Pre-render a figure set so kb-report has something to embed**

```bash
uv run --no-sync python scripts/weekly_figures.py --demo-mode --date 2026-05-22 --with-link-density
```

(Or omit `--demo-mode` if running on the user's main workstation with a QGIS plugin running and real PFLOW data accessible.)

Expected: `H:/Dropbox/obsidian-vault/wiki/qgis/figures/weekly/2026-05-22/` contains `choropleth.png`, `trajectory.png`, optionally `link_density.png`, and `manifest.json`.

- [ ] **Step 2: Invoke kb-report**

```
/kb-report weekly qgis
```

Expected: skill writes `H:/Dropbox/obsidian-vault/reports/qgis/weekly-2026-05-22.md`. The report should embed the manifest's figure paths via `![[...]]` or markdown image syntax and include a brief written summary referencing the source wiki pages.

- [ ] **Step 3: Inspect the report**

Open the report in an editor or via:

```bash
cat /h/Dropbox/obsidian-vault/reports/qgis/weekly-2026-05-22.md
```

Verify:
- The three (or two, sans link-density) figures are referenced by absolute or vault-relative path.
- The summary text mentions concrete numbers from the manifest (`n_features`, `n_trajectories`, etc.).
- Any "still open" items pulled from DESIGN.md §8 are flagged.

If the report is empty or wrong-shaped, the kb-report skill may need configuration — consult its docs. Don't hand-edit the report; fix the upstream skill or the manifest schema.

- [ ] **Step 4: Schedule the recurring weekly run**

The repo's CLAUDE.md mentions `/schedule` for routines with concrete cadences. Schedule it weekly, Monday 09:00 local time:

```
/schedule "0 9 * * 1" uv run --no-sync python scripts/weekly_figures.py --with-link-density && /kb-report weekly qgis
```

(The exact `/schedule` invocation syntax depends on the user's local schedule skill; the cron expression `0 9 * * 1` is the canonical "every Monday at 09:00" form.)

If the user prefers manual invocation instead, skip this step and leave a TODO in `wiki/qgis/log.md` noting that the weekly is run by hand.

- [ ] **Step 5: Commit the report (if vault is git-tracked)**

```bash
cd /h/Dropbox/obsidian-vault
git add reports/qgis/weekly-2026-05-22.md wiki/qgis/figures/weekly/2026-05-22/manifest.json
git commit -m "report(qgis): first weekly figure set (2026-05-22)"
cd /h/Dropbox/qgis-mcp-workflows
```

Optional: also commit the PNGs themselves, or add them to the vault's .gitignore if they're large enough to bloat history. Typical choropleth PNG at 1600×1200 / 150 DPI is ~200 KB — small. Commit them.

---

### Task 8: Add `docs/vault-integration.md` to this repo

**Files:**
- Create: `docs/vault-integration.md`

A terse pointer from this repo to the vault workflow, for anyone reading the codebase without prior context on the vault.

- [ ] **Step 1: Write the doc**

```
Write:
  file_path: docs/vault-integration.md
  content: # Vault integration

This codebase feeds the obsidian-vault knowledge system at `H:/Dropbox/obsidian-vault/`. The workflow:

## One-time setup (already done, v0.6 / 2026-05-22)

```
/kb-ingest qgis H:/Dropbox/qgis-mcp-workflows    # → vault/raw/qgis/dev/
/kb-compile qgis                                  # → vault/wiki/qgis/concepts/
```

## Weekly cadence

```
uv run --no-sync python scripts/weekly_figures.py --with-link-density
/kb-report weekly qgis
```

The first command renders the figure set into `H:/Dropbox/obsidian-vault/wiki/qgis/figures/weekly/<date>/` and writes a `manifest.json`. The second invokes the vault's `/kb-report` skill, which embeds the figures into `H:/Dropbox/obsidian-vault/reports/qgis/weekly-<date>.md`.

Both are scheduled to run automatically — see `/schedule` configuration. Cron equivalent: `0 9 * * 1`.

## What lives where

| Artifact | Location | Owner |
|---|---|---|
| Source code | `H:/Dropbox/qgis-mcp-workflows/src/qgis_mcp_workflows/` | This repo |
| Plugin | `H:/Dropbox/qgis-mcp-workflows/qgis_mcp_workflows_plugin/` | This repo |
| Spec | `H:/Dropbox/qgis-mcp-workflows/docs/DESIGN.md` | This repo (single source of truth) |
| Concept pages | `H:/Dropbox/obsidian-vault/wiki/qgis/concepts/` | Vault; auto-compiled from raw |
| Raw source summaries | `H:/Dropbox/obsidian-vault/raw/qgis/dev/` | Vault; auto-ingested |
| Cross-links to projects | `H:/Dropbox/obsidian-vault/wiki/{pflow,gufm}/applications-qgis.md` | Vault; hand-curated |
| Weekly figures | `H:/Dropbox/obsidian-vault/wiki/qgis/figures/weekly/<date>/` | Vault; written by this repo's `scripts/weekly_figures.py` |
| Weekly reports | `H:/Dropbox/obsidian-vault/reports/qgis/weekly-*.md` | Vault; produced by `/kb-report` |

## Re-syncing when the codebase changes

Whenever `src/qgis_mcp_workflows/` changes substantially — new tool, renamed module, refactored executor — re-ingest:

```
/kb-ingest qgis H:/Dropbox/qgis-mcp-workflows
/kb-compile qgis
/kb-health qgis
```

The kb-* skills are idempotent and lint-aware; safe to run repeatedly.
```

- [ ] **Step 2: Commit**

```bash
git add docs/vault-integration.md
git commit -m "docs: add vault-integration.md describing /kb-* workflow"
```

---

### Task 9: Final verification + version bump

**Files:**
- Modify: `pyproject.toml`
- Modify: `qgis_mcp_workflows_plugin/metadata.txt`
- Modify: `docs/DESIGN.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump version to 1.3.0**

```
Edit:
  file_path: pyproject.toml
  old_string: version = "1.2.0"
  new_string: version = "1.3.0"
```

If Plan 2 hasn't merged, current pyproject is at 1.1.0 — adapt the bump accordingly.

```
Edit:
  file_path: qgis_mcp_workflows_plugin/metadata.txt
  old_string: version=1.2.0
  new_string: version=1.3.0
```

- [ ] **Step 2: Update the startup-log version string**

```
Edit:
  file_path: src/qgis_mcp_workflows/server.py
  old_string:     logger.info("qgis-mcp-workflows server starting (v1.2.0, transport=%s)", chosen)
  new_string:     logger.info("qgis-mcp-workflows server starting (v1.3.0, transport=%s)", chosen)
```

- [ ] **Step 3: Mark v0.6 shipped in DESIGN.md §7**

```
Edit:
  file_path: docs/DESIGN.md
  old_string: **v0.6 — vault ingest.** `/kb-ingest qgis-mcp-north`. Add `wiki/shared/qgis-mcp-north/` pages, applications notes in `wiki/pflow/` and `wiki/gufm/`. Wire a `/kb-report weekly` rendering pipeline through it.
  new_string: **v0.6 — vault ingest.** ✅ Shipped 2026-05-22 (as v1.3.0). `/kb-ingest qgis H:/Dropbox/qgis-mcp-workflows` seeded `raw/qgis/dev/`; `/kb-compile qgis` produced concept pages under `wiki/qgis/concepts/`. Cross-link stubs added at `wiki/pflow/applications-qgis.md` and `wiki/gufm/applications-qgis.md`. `scripts/weekly_figures.py` renders the figure set into `wiki/qgis/figures/weekly/<date>/`; `/kb-report weekly qgis` embeds them in `reports/qgis/weekly-<date>.md`. Weekly cadence scheduled (Monday 09:00). See `docs/vault-integration.md` for the workflow.
```

- [ ] **Step 4: Add v1.3.0 entry to CHANGELOG.md**

```markdown
## v1.3.0 — 2026-05-22 — Vault integration (v0.6 milestone)

Closes the v0.6 milestone from DESIGN.md §7. This codebase now feeds the obsidian-vault knowledge system end-to-end.

Added:
- `scripts/weekly_figures.py` — renders the weekly figure set (`choropleth`, `trajectory`, optional `link_density`) into `H:/Dropbox/obsidian-vault/wiki/qgis/figures/weekly/<date>/` + writes a `manifest.json`.
- `docs/vault-integration.md` — terse README pointing at the `/kb-*` workflow.
- `tests/test_weekly_figures.py` (3 tests).

Vault-side changes (separate commits in `obsidian-vault`):
- `wiki/qgis/index.md` updated for the rename, refreshed concept-page links.
- `wiki/qgis/concepts/*.md` produced by `/kb-compile qgis` from `raw/qgis/dev/`.
- `wiki/pflow/applications-qgis.md`, `wiki/gufm/applications-qgis.md` — Tobler-bridge cross-links.
- First `reports/qgis/weekly-2026-05-22.md` produced.

Scheduling: weekly run at `0 9 * * 1` (Monday 09:00 local) — configurable via `/schedule`.

Unchanged: tool surface, response shapes, error taxonomy. No runtime dependency changes; the weekly script uses only the existing MCP tools.
```

- [ ] **Step 5: Run the full test suite**

```bash
uv run --no-sync pytest tests/ -v
```

Expected: green. Should now include the 3 new tests from `tests/test_weekly_figures.py` on top of everything from Plans 1 and 2.

- [ ] **Step 6: Run lint**

```bash
uv tool run ruff check src/ tests/ scripts/ install.py
```

Expected: no violations.

- [ ] **Step 7: Verify the vault state**

```bash
ls /h/Dropbox/obsidian-vault/wiki/qgis/concepts/
ls /h/Dropbox/obsidian-vault/wiki/qgis/figures/weekly/
ls /h/Dropbox/obsidian-vault/reports/qgis/
```

Expected:
- `concepts/` has ≥3 markdown files
- `figures/weekly/` has at least the `2026-05-22/` subdirectory with PNGs + `manifest.json`
- `reports/qgis/` has at least `weekly-2026-05-22.md`

- [ ] **Step 8: Commit version bump + tag**

```bash
git add pyproject.toml qgis_mcp_workflows_plugin/metadata.txt src/qgis_mcp_workflows/server.py docs/DESIGN.md CHANGELOG.md
git commit -m "release: bump to v1.3.0 (v0.6 vault ingest)"
git tag -a v1.3.0 -m "v1.3.0 — vault ingest pipeline (v0.6 milestone)"
```

---

## Self-Review

**Spec coverage (against the v0.6 milestone in DESIGN.md §7):**

- `/kb-ingest qgis H:/Dropbox/qgis-mcp-workflows` → ✅ Task 3
- `wiki/qgis/concepts/*` populated via `/kb-compile` → ✅ Task 4
- Applications notes in `wiki/pflow/` and `wiki/gufm/` → ✅ Task 6
- `/kb-report weekly` rendering pipeline wired through this repo → ✅ Task 5 (weekly_figures.py) + Task 7 (kb-report invocation)
- Cross-link `[[wikilinks]]` set up between the wiki/qgis index and concept pages → ✅ Task 2
- Schedule for the recurring weekly → ✅ Task 7 Step 4
- Version bump + CHANGELOG → ✅ Task 9

**Placeholder scan:** Every step has either complete content (markdown, Python, JSON) or a precise slash-command invocation. No "TBD" / "TODO" markers. Two intentional non-deterministic moments:
- Task 4 Step 2 (reconciling kb-compile output with index links) is a verification step, not a code step — the actual filenames depend on what kb-compile chooses to produce.
- Task 7 Step 4's `/schedule` invocation syntax depends on the user's local schedule skill; I quote the canonical cron form (`0 9 * * 1`) and note the substitution.

**Type consistency:** the `manifest.json` schema is the contract between this plan's two scripts (`weekly_figures.py` writes it; `/kb-report` reads it). Schema:
```
{"date": str, "demo": bool, "figures": {<name>: {"path": str, ...}}}
```
Used identically in: Task 5 Step 1 (test asserts on it), Task 5 Step 3 (script writes it), Task 7 Step 3 (verification reads it).

**Cross-repo coordination:**
- This repo's branch: `feat/v0.6-vault-ingest`, merged as v1.3.0.
- Vault changes are committed separately (Task 2 Step 3, Task 3 Step 4, Task 4 Step 4, Task 6 Step 3, Task 7 Step 5) — these are independent commits on the vault's git history (if it's git-tracked at all; Dropbox sync alone is acceptable).

**Dependencies & ordering:**
- Plan 1 (rename) is hard-required — every path uses post-rename names.
- Plan 2 (link-density) is optional — Task 5 Step 3 gracefully degrades when `qgis_render_link_density` isn't importable. Test in Task 5 Step 1 explicitly skips when Plan 2 isn't merged.

**Known gaps:**
- The kb-* skills are vault-side; their exact behavior depends on the installed skill versions. If `/kb-ingest qgis` produces unexpectedly few files, the workaround is to manually augment `raw/qgis/dev/*.md` before `/kb-compile`. Not a blocker — the rest of the plan still ships.
- The schedule entry assumes the user has a working `/schedule` skill or external cron. If neither is available, the user runs `scripts/weekly_figures.py` + `/kb-report weekly qgis` manually each Monday. Documented in Task 7 Step 4 + Task 8's `docs/vault-integration.md`.
- The first kb-report output may need iterative tuning — the kb-report skill's prompt is fixed, but if it produces a report that's too thin, the fix is in the skill (vault-side), not in this plan.

