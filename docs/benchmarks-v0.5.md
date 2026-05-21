# qgis-mcp-north v0.5 — Benchmarks baseline

Run on: _(fill in date / machine specs when you execute)_

These numbers are a v0.5 baseline. v1.0 should re-run on the same machine and compare.

## How to run

```powershell
# Install the bench extra
uv sync --extra bench --extra trajectory

# Build the 134-zone fixture (one-time)
uv run --no-sync python scripts/build_scaled_zones.py

# Plugin benches (start QGIS Desktop + enable the plugin first; listen on :9877)
uv run --no-sync --extra bench pytest tests/benchmarks/ -m bench --benchmark-only -k plugin

# Headless benches (set the launcher path)
$env:QGIS_MCP_NORTH_QGIS_LAUNCHER='M:\QGIS LTR\bin\python-qgis-ltr.bat'
uv run --no-sync --extra bench pytest tests/benchmarks/ -m bench --benchmark-only -k headless
```

`pytest-benchmark` prints a min/mean/median/stddev table per test. Copy the median column into the tables below.

## Cold-start

| Transport | Median wall time | Notes |
|---|---|---|
| headless | _(fill)_ | Spawn PyQGIS subprocess + initQgis + ping |
| plugin | _(fill)_ | Connect TCP socket + ping (QGIS already running) |

**Expected (rough order of magnitude):**
- Headless: 2-5 s (initQgis dominates).
- Plugin: 10-50 ms (just a socket round-trip).

## Trajectory render scaling

Default mode = `lines`, no extent clip, no sample_rate.

| Rows | Headless wall time | Peak memory (tracemalloc) |
|---|---|---|
| 1k | _(fill)_ | n/a |
| 10k | _(fill)_ | n/a |
| 100k | _(fill)_ | _(fill, under 50 MB asserted)_ |
| 500k | _(fill)_ | n/a |

**Expected scaling:** roughly linear in rows for CSV parse + memory-layer build; render time depends on QGIS rasterization.

## Choropleth render

| Zones | Wall time | Notes |
|---|---|---|
| 4 (tiny_zones.geojson) | _(fill)_ | No CSV join; inline value_field |
| 134 (scaled_zones_134.geojson) | _(fill)_ | CSV join on zone_id |

## Transport parity

Same operation (`qgis_render_trajectory` on `tiny_trajectory.csv`, lines mode) under both transports.

| Transport | Wall time | Ratio vs plugin |
|---|---|---|
| plugin | _(fill)_ | 1.0× |
| headless | _(fill)_ | _(fill)_ |

**Flag if:** headless > 3× plugin for the same call. The v0.4 architecture shares one handler codebase, so the only differences are stdin-vs-socket transport and subprocess vs in-process.

## Notes

- `pytest-benchmark` runs 5 rounds × 10 iterations by default. For long renders (500k rows), override with `--benchmark-min-rounds=2`.
- Cold-start variance is high; expect ±30% on shared dev machines.
- These benchmarks are NOT auto-CI'd. Re-run manually for each release.
