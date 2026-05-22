# Vault integration

This codebase feeds the obsidian-vault knowledge system at `H:/Dropbox/obsidian-vault/` under the `qgis` project slug. The workflow:

## One-time setup (already done, v0.6 / 2026-05-22)

```
/kb-ingest qgis H:/Dropbox/qgis-mcp-north    # → vault/raw/qgis/dev/
/kb-compile qgis                              # → vault/wiki/qgis/concepts/
```

Prerequisite: the vault's `kb-ingest`, `kb-compile`, and `kb-report` skills must include `qgis` in their allowed project slugs. This was added 2026-05-22; if you reinstall the vault skills from upstream, re-add it.

## Weekly cadence

```
uv run --no-sync python scripts/weekly_figures.py --with-link-density
/kb-report weekly qgis
```

The first command renders the figure set into `H:/Dropbox/obsidian-vault/wiki/qgis/figures/weekly/<date>/` and writes a `manifest.json`. The second invokes the vault's `/kb-report` skill, which embeds the figures into `H:/Dropbox/obsidian-vault/reports/qgis/weekly-<date>.md`.

**Manifest schema** (the contract between the renderer and `/kb-report`):
```json
{
  "date": "YYYY-MM-DD",
  "demo": false,
  "figures": {
    "choropleth": {"path": "...", "n_features": 47, "min": 0.0, "max": 12000.0},
    "trajectory": {"path": "...", "n_trajectories": 152, "n_points_total": 480000, "downsampled": true},
    "link_density": {"path": "...", "n_links_rendered": 42031, "min_density": 1.0, "max_density": 583.0}
  }
}
```

## Scheduling the weekly run

Cron expression `0 9 * * 1` = every Monday at 09:00 local. Schedule via Claude Code's `/schedule` skill once and forget:

```
/schedule "0 9 * * 1" "uv run --no-sync python scripts/weekly_figures.py --with-link-density && /kb-report weekly qgis"
```

If you don't use `/schedule`, fall back to OS cron / Task Scheduler with the same command.

## What lives where

| Artifact | Location | Owner |
|---|---|---|
| Source code | `H:/Dropbox/qgis-mcp-north/src/qgis_mcp_workflows/` | This repo |
| Plugin | `H:/Dropbox/qgis-mcp-north/qgis_mcp_workflows_plugin/` | This repo |
| Spec | `H:/Dropbox/qgis-mcp-north/docs/DESIGN.md` | This repo (single source of truth) |
| Concept pages | `H:/Dropbox/obsidian-vault/wiki/qgis/*.md` | Vault; auto-compiled from raw |
| Raw source summaries | `H:/Dropbox/obsidian-vault/raw/qgis/dev/` | Vault; auto-ingested |
| Cross-links to projects | `H:/Dropbox/obsidian-vault/wiki/{pflow,gufm}/applications-qgis.md` | Vault; hand-curated |
| Weekly figures | `H:/Dropbox/obsidian-vault/wiki/qgis/figures/weekly/<date>/` | Vault; written by this repo's `scripts/weekly_figures.py` |
| Weekly reports | `H:/Dropbox/obsidian-vault/reports/qgis/weekly-*.md` | Vault; produced by `/kb-report` |

## Re-syncing when the codebase changes

Whenever `src/qgis_mcp_workflows/` changes substantially — new tool, renamed module, refactored executor — re-ingest:

```
/kb-ingest qgis H:/Dropbox/qgis-mcp-north
/kb-compile qgis
/kb-health qgis
```

The kb-* skills are idempotent and lint-aware; safe to run repeatedly. `kb-ingest`'s incremental check (`.ingest-log.json` mtime comparison) means unchanged files are skipped.

## On-disk directory name

Note: the working directory is still `H:/Dropbox/qgis-mcp-north/` on disk even after the v1.1.0 rename. Only the Python package (`qgis_mcp_workflows`), plugin folder (`qgis_mcp_workflows_plugin`), console script (`qgis-mcp-workflows-server`), and env vars (`QGIS_MCP_WORKFLOWS_*`) were renamed. Renaming the on-disk folder requires moving an active Dropbox-synced directory — out of scope for v1.1.0; the rename buys nothing in exchange for substantial sync disruption.
