# Choropleth Diverging Color Schemes + Scientific Colormaps — Design Spec

- **Date:** 2026-06-22
- **Status:** Design approved; implementation plan pending.
- **Scope:** Approach 1 — a shared color helper plus diverging support, wired into
  `qgis_render_choropleth` and `qgis_style_graduated` only.
- **Origin:** First slice of the broader "improve MCP cartography" effort, prioritized
  against the `N:\TransInfor` transit-figure pipeline (the `ward_tide` net-flux map) and
  the 2026-06-14 map-beauty roadmap (item #2, scientific colormaps).

## 1. Motivation

Net-flux transport data (arrivals − departures) is *signed*: it diverges around zero.
Today both color-bearing style paths resolve a ramp via
`QgsStyle.defaultStyle().colorRamp(name)` with a `Spectral` fallback
(`render_choropleth` at plugin.py:1241; `set_layer_style` graduated at plugin.py:2129),
and classify with `setClassificationMethod().updateClasses()`. That has two limits:

1. **No perceptually-uniform / scientific ramps guaranteed.** Availability of `viridis`
   etc. depends on the QGIS install's default style; Crameri/cmocean ramps are absent.
2. **No way to pin a diverging midpoint.** Quantile/Jenks float the neutral color to the
   data median, so "0" net-flux can render mid-hue instead of neutral.

Of the 8 roadmap items, only basemaps shipped (choropleth only). This slice adds the
color foundation the rest reuse.

## 2. Goals / Non-goals

**Goals**
- Vendor scientific colormaps as code (no new dependency).
- Add a `diverging` mode with an explicit `center` pin to `qgis_render_choropleth` and
  `qgis_style_graduated`.
- Introduce one shared `_build_graduated_renderer` helper, removing the duplicated
  ramp-resolve + renderer-build logic at the two call sites.
- 100% backward compatibility: existing calls render byte-identically.

**Non-goals (explicitly deferred)**
- Wiring the color helper into `render_od_flows` / `render_link_density` (trivial
  follow-up; they will call the same helper).
- Blend modes, glow, programmatic layouts (later roadmap slices).
- The **gufm Theory Companion palette** — add as a named ramp once its hex stops are
  provided.
- Per-side quantile classification for diverging (symmetric equal-interval only, for now).

## 3. Components

### 3.1 New module `qgis_mcp_workflows_plugin/colormaps.py`
Pure data + builder; no QGIS UI, safe in headless transport.

- `COLORMAPS: dict[str, list[tuple[float, tuple[int, int, int]]]]` — name → ordered list
  of `(position 0..1, (r, g, b))` stops (~11 stops each):
  - **Sequential:** `viridis`, `cividis`, `magma` (matplotlib), `batlow` (Crameri).
  - **Diverging:** `vik`, `roma` (Crameri), `balance` (cmocean), `RdBu`, `BrBG` (ColorBrewer).
  - Stops vendored from canonical sources (matplotlib listed colormaps; Crameri Scientific
    Colour Maps; cmocean; ColorBrewer) so values are accurate, not eyeballed.
- `DIVERGING: set[str] = {"vik", "roma", "balance", "RdBu", "BrBG"}`.
- `build_ramp(name: str) -> QgsGradientColorRamp | None` — assemble a gradient ramp from
  the stop table; `None` if the name is unknown.

### 3.2 `_resolve_color_ramp(name, diverging=False)` (plugin.py)
Resolution order:
1. `colormaps.build_ramp(name)` if vendored.
2. else `QgsStyle.defaultStyle().colorRamp(name)` (keeps `YlOrRd`, `Blues`, `Spectral`, …).
3. else default: `build_ramp("vik")` when `diverging` else `colorRamp("Spectral")`.

Replaces the two `colorRamp(...) or Spectral` blocks (plugin.py:1241–1243, 2129–2131).

### 3.3 `_build_graduated_renderer(layer, field, *, n_classes, mode, palette, diverging, center)` (plugin.py)
Returns `(QgsGraduatedSymbolRenderer, breaks: list[float])`.
- **Sequential path** (`diverging=False`): today's behavior exactly —
  `setSourceColorRamp(_resolve_color_ramp(palette))`, `setClassificationMethod(method_cls())`,
  `updateClasses(layer, n_classes)`.
- **Diverging path** (`diverging=True`): Section 4.

Both `render_choropleth` (replacing plugin.py:1244–1256) and the `graduated` branch of
`set_layer_style` (replacing plugin.py:2133–2145) call it. Per-class feature counts in
`set_layer_style` stay where they are (they iterate features, out of scope for the helper).

## 4. Diverging algorithm

When `diverging=True`, bypass the QGIS classifier and build symmetric ranges by hand:

1. From non-null numeric values, `R = max(|min − center|, |max − center|)` — the larger
   tail, so both sides fit within the symmetric range.
2. `breaks` = `k + 1` (where `k = n_classes`) equal-width edges from `center − R` to
   `center + R`. Even `k` puts `center` exactly on a class edge (clean); odd `k` straddles it.
3. For class *j* with midpoint `m_j`, color = `ramp.color((m_j − (center − R)) / (2R))`,
   so `center` maps to the ramp's neutral midpoint (0.5) and the tails get the two hues.
4. Build a manual `QgsRendererRange` list and a `QgsGraduatedSymbolRenderer(field)` from it.

`mode` is ignored when `diverging=True` (symmetric equal-interval is the intent). Empty
classes (all data on one side of `center`) render harmlessly in the legend.

**Rationale:** quantile/Jenks optimize boundaries to the distribution, floating the neutral
color off the meaningful anchor. Diverging trades that for a fixed, reproducible midpoint.

## 5. API & response changes

`qgis_render_choropleth` and `qgis_style_graduated` (server.py) each gain:
- `diverging: bool = False`
- `center: float = 0.0`

Plumbed server.py → dispatched command params → plugin handler kwargs.

**Backward compatibility:** with `diverging=False` and a previously-valid `palette`/`mode`,
output is byte-identical. New colormap names simply become resolvable.

**Response additions** (both `ChoroplethResult` and `GraduatedStyleResult`):
- echo `diverging` and `center`
- `diverging_one_sided: bool` — `true` when the data did not straddle `center` (a quiet
  signal that diverging may be the wrong choice for this data).

Existing fields (`breaks`, `min_value`, `max_value`, `n_classes`, per-class `classes`) are
unchanged in shape.

## 6. Error handling

- Unknown `palette` no longer raises — it falls through `_resolve_color_ramp` to a sane
  default, matching today's `or "Spectral"` behavior.
- `n_classes` bounds remain enforced by the server-side Pydantic field (2–15).
- No new typed exceptions in `errors.py` (no new unrecoverable failure mode).

## 7. Testing

`tests/` use the `FakeExecutor` (no QGIS required):
- New params (`diverging`, `center`) reach the dispatched command for both tools.
- Back-compat: a default call dispatches the same command shape as before.

Pure-Python unit tests (no QGIS) for the break math, factored so it's importable without
PyQGIS:
- symmetric breaks span `center ± R`;
- even `k` places `center` on a class edge;
- color-position mapping puts `center` at ramp position 0.5;
- `diverging_one_sided` detection (all-positive vs straddling data).
- `colormaps` smoke test: every ramp's stop positions are monotonic in `[0, 1]`.

## 8. Verified integration points

| Location | Current | Change |
|---|---|---|
| plugin.py:1241–1243 | `colorRamp(palette) or Spectral` | → `_resolve_color_ramp` (via helper) |
| plugin.py:1244–1256 | inline graduated build + breaks | → `_build_graduated_renderer(...)` |
| plugin.py:2129–2131 | `colorRamp(color_ramp) or Spectral` | → `_resolve_color_ramp` (via helper) |
| plugin.py:2133–2145 | inline graduated build + breaks | → `_build_graduated_renderer(...)` |
| server.py (`qgis_render_choropleth`, `qgis_style_graduated`) | params + result models | + `diverging`, `center`, response echoes |
| `qgis_mcp_workflows_plugin/colormaps.py` | — | new module |

## 9. Open items

- gufm Theory Companion palette: add as a named ramp when hex stops are provided.
- Follow-up slice: route `render_od_flows` / `render_link_density` color through the same
  helper.
