"""QGIS 3.x / 4.x enum compatibility shim.

QGIS 4.x (Qt6/PyQt6) moves most enums into the ``Qgis`` namespace and
fully-qualified enum forms.  This module resolves the correct value at
import time so the rest of the plugin stays clean.

Strategy: try the **new** form first, fall back to the old one.
"""

from qgis.core import (
    Qgis,
    QgsAggregateCalculator,
    QgsLayoutExporter,
    QgsMapLayer,
    QgsProcessingParameterDefinition,
    QgsRasterBandStats,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QIODevice, Qt
from qgis.PyQt.QtWidgets import QToolButton

# ── Layer types ──────────────────────────────────────────────────────
try:
    LAYER_VECTOR = Qgis.LayerType.Vector
except AttributeError:
    LAYER_VECTOR = QgsMapLayer.VectorLayer

try:
    LAYER_RASTER = Qgis.LayerType.Raster
except AttributeError:
    LAYER_RASTER = QgsMapLayer.RasterLayer

# ── Message levels ───────────────────────────────────────────────────
try:
    MSG_INFO = Qgis.MessageLevel.Info
except AttributeError:
    MSG_INFO = Qgis.Info

try:
    MSG_WARNING = Qgis.MessageLevel.Warning
except AttributeError:
    MSG_WARNING = Qgis.Warning

try:
    MSG_CRITICAL = Qgis.MessageLevel.Critical
except AttributeError:
    MSG_CRITICAL = Qgis.Critical

# ── Geometry types ───────────────────────────────────────────────────
try:
    GEOM_POLYGON = Qgis.GeometryType.Polygon
except AttributeError:
    GEOM_POLYGON = QgsWkbTypes.PolygonGeometry

try:
    GEOM_LINE = Qgis.GeometryType.Line
except AttributeError:
    GEOM_LINE = QgsWkbTypes.LineGeometry

# ── Raster stats ─────────────────────────────────────────────────────
try:
    RASTER_STATS_ALL = Qgis.RasterBandStatistic.All
except AttributeError:
    RASTER_STATS_ALL = QgsRasterBandStats.All

# ── Layout export result ─────────────────────────────────────────────
try:
    LAYOUT_SUCCESS = Qgis.LayoutResult.Success
except AttributeError:
    LAYOUT_SUCCESS = QgsLayoutExporter.Success

# ── Processing parameter flags ───────────────────────────────────────
try:
    PROCESSING_OPTIONAL = Qgis.ProcessingParameterFlag.Optional
except AttributeError:
    PROCESSING_OPTIONAL = QgsProcessingParameterDefinition.FlagOptional

# ── Aggregate functions ──────────────────────────────────────────────
try:
    AGG_COUNT = Qgis.Aggregate.Count
    AGG_SUM = Qgis.Aggregate.Sum
    AGG_MEAN = Qgis.Aggregate.Mean
    AGG_MIN = Qgis.Aggregate.Min
    AGG_MAX = Qgis.Aggregate.Max
    AGG_STDEV = Qgis.Aggregate.StDev
    AGG_ARRAY = Qgis.Aggregate.ArrayAggregate
except AttributeError:
    AGG_COUNT = QgsAggregateCalculator.Count
    AGG_SUM = QgsAggregateCalculator.Sum
    AGG_MEAN = QgsAggregateCalculator.Mean
    AGG_MIN = QgsAggregateCalculator.Min
    AGG_MAX = QgsAggregateCalculator.Max
    AGG_STDEV = QgsAggregateCalculator.StDev
    AGG_ARRAY = QgsAggregateCalculator.ArrayAggregate

# ── Qt IO / widget enums ─────────────────────────────────────────────
try:
    IODEVICE_WRITEONLY = QIODevice.OpenModeFlag.WriteOnly
except AttributeError:
    IODEVICE_WRITEONLY = QIODevice.WriteOnly

try:
    TOOLBUTTON_MENU_POPUP = QToolButton.ToolButtonPopupMode.MenuButtonPopup
except AttributeError:
    TOOLBUTTON_MENU_POPUP = QToolButton.MenuButtonPopup

try:
    TOOLBUTTON_ICON_ONLY = Qt.ToolButtonStyle.ToolButtonIconOnly
except AttributeError:
    TOOLBUTTON_ICON_ONLY = Qt.ToolButtonIconOnly
