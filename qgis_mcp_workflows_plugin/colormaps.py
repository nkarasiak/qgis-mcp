"""Vendored scientific colormaps + diverging classification math.

Why vendored: QGIS's default style only reliably ships ColorBrewer ramps;
perceptually-uniform (viridis/cividis/magma), Crameri (batlow/vik/roma), and
cmocean (balance) ramps are not guaranteed present. Copying compact RGB
stop-tables keeps the renderer dependency-free and reproducible across installs.

Layout discipline: this module must import **without** a QGIS runtime so the
data tables and ``diverging_breaks`` math are unit-testable headlessly. The only
QGIS-touching function, ``build_ramp``, imports ``qgis`` lazily at call time.

Stop tables are anchor samples (5 stops) of each map — enough for a smooth
gradient ramp, not a 256-entry LUT.
"""

from __future__ import annotations

from typing import NamedTuple

# name -> [(position 0..1, (r, g, b)), ...], monotonic in position, 0.0 .. 1.0.
COLORMAPS: dict[str, list[tuple[float, tuple[int, int, int]]]] = {
    # --- perceptually-uniform sequential (matplotlib) ---
    "viridis": [
        (0.0, (68, 1, 84)), (0.25, (59, 82, 139)), (0.5, (33, 145, 140)),
        (0.75, (94, 201, 98)), (1.0, (253, 231, 37)),
    ],
    "cividis": [
        (0.0, (0, 32, 76)), (0.25, (46, 80, 112)), (0.5, (124, 123, 120)),
        (0.75, (188, 175, 110)), (1.0, (255, 234, 70)),
    ],
    "magma": [
        (0.0, (0, 0, 4)), (0.25, (80, 18, 123)), (0.5, (182, 54, 121)),
        (0.75, (252, 136, 97)), (1.0, (252, 253, 191)),
    ],
    # --- Crameri sequential ---
    "batlow": [
        (0.0, (1, 25, 89)), (0.25, (43, 87, 99)), (0.5, (123, 131, 75)),
        (0.75, (202, 167, 98)), (1.0, (250, 204, 250)),
    ],
    # --- Crameri diverging ---
    "vik": [
        (0.0, (0, 18, 97)), (0.25, (86, 135, 191)), (0.5, (240, 240, 240)),
        (0.75, (197, 101, 76)), (1.0, (89, 0, 1)),
    ],
    "roma": [
        (0.0, (127, 30, 4)), (0.25, (192, 150, 80)), (0.5, (155, 196, 140)),
        (0.75, (80, 168, 160)), (1.0, (25, 51, 102)),
    ],
    # --- cmocean diverging ---
    "balance": [
        (0.0, (24, 28, 67)), (0.25, (60, 116, 180)), (0.5, (240, 236, 230)),
        (0.75, (190, 90, 70)), (1.0, (60, 8, 3)),
    ],
    # --- ColorBrewer diverging ---
    "RdBu": [
        (0.0, (103, 0, 31)), (0.25, (214, 96, 77)), (0.5, (247, 247, 247)),
        (0.75, (67, 147, 195)), (1.0, (5, 48, 97)),
    ],
    "BrBG": [
        (0.0, (84, 48, 5)), (0.25, (191, 129, 45)), (0.5, (245, 245, 245)),
        (0.75, (53, 151, 143)), (1.0, (0, 60, 48)),
    ],
}

# Maps suited to signed/diverging data (neutral midpoint, two diverging hues).
DIVERGING: frozenset[str] = frozenset({"vik", "roma", "balance", "RdBu", "BrBG"})


class DivergingClassification(NamedTuple):
    """Result of ``diverging_breaks``.

    - ``breaks``: ``n_classes + 1`` class edges, symmetric about ``center``.
    - ``positions``: per-class ramp positions in ``[0, 1]`` (class midpoint),
      mapped so ``center`` lands on the ramp's neutral midpoint (0.5).
    - ``one_sided``: ``True`` when the data did not straddle ``center``.
    """

    breaks: list[float]
    positions: list[float]
    one_sided: bool


def diverging_breaks(
    vmin: float, vmax: float, center: float = 0.0, n_classes: int = 5
) -> DivergingClassification:
    """Symmetric class breaks + ramp positions for a diverging scheme.

    Radius is the larger tail ``max(|vmin-center|, |vmax-center|)`` so both sides
    of ``center`` fit. Classes are equal-width across ``[center-R, center+R]``;
    with even ``n_classes`` the center lands exactly on a class edge.
    """
    radius = max(abs(vmin - center), abs(vmax - center))
    if radius == 0:
        radius = 1.0  # degenerate (all values == center): keep a well-formed range
    lo = center - radius
    hi = center + radius
    span = hi - lo  # == 2 * radius
    step = span / n_classes
    breaks = [lo + i * step for i in range(n_classes + 1)]
    positions = [
        ((breaks[i] + breaks[i + 1]) / 2.0 - lo) / span for i in range(n_classes)
    ]
    one_sided = not (vmin < center < vmax)
    return DivergingClassification(breaks, positions, one_sided)


def build_ramp(name: str):
    """Build a ``QgsGradientColorRamp`` from a vendored colormap, or ``None``.

    QGIS-only: imports ``qgis`` lazily so this module stays headless-importable.
    Returns ``None`` for unknown names so callers can fall through to QGIS's
    default style ramps (back-compat).
    """
    stops = COLORMAPS.get(name)
    if stops is None:
        return None
    from qgis.core import QgsGradientColorRamp, QgsGradientStop
    from qgis.PyQt.QtGui import QColor

    color1 = QColor(*stops[0][1])
    color2 = QColor(*stops[-1][1])
    interior = [QgsGradientStop(pos, QColor(*rgb)) for pos, rgb in stops[1:-1]]
    ramp = QgsGradientColorRamp(color1, color2)
    if interior:
        ramp.setStops(interior)
    return ramp
