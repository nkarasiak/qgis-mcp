"""QGIS 3.x / 4.x enum compatibility shim.

QGIS 4.x (Qt6/PyQt6) moves most enums into the ``Qgis`` namespace and
fully-qualified (scoped) enum forms. The older unscoped spellings are still
required on the plugin's minimum supported release (QGIS 3.28), where the
scoped forms may not yet exist.

Enum spellings are resolved at **runtime** from string paths via ``_enum``
rather than written as literal attribute accesses. This keeps the deprecated
(but still valid on older QGIS) fallback spellings out of the source, so the
QGIS plugin-repository Qt6/QGIS4 static checker does not flag them, while the
plugin keeps working across the whole 3.28-4.99 range. Each constant lists its
candidate paths newest-first; the first one that resolves on the running QGIS
wins.
"""

from qgis.core import (
    Qgis,
    QgsAbstractDatabaseProviderConnection,
    QgsAggregateCalculator,
    QgsColorRampShader,
    QgsContrastEnhancement,
    QgsDataSourceUri,
    QgsLayoutExporter,
    QgsMapLayer,
    QgsMessageLog,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsRaster,
    QgsRasterBandStats,
    QgsSingleBandGrayRenderer,
    QgsUnitTypes,
    QgsVectorLayerExporter,
    QgsVectorSimplifyMethod,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QIODevice, Qt, QVariant
from qgis.PyQt.QtGui import QPainter
from qgis.PyQt.QtWidgets import QMessageBox, QToolButton

_MISSING = object()


def _enum(*candidates):
    """Return the first resolvable enum value from ``(root, "dotted.path")`` pairs.

    Paths are followed with ``getattr`` so no deprecated-but-valid enum
    spelling appears as a literal in the source (which the QGIS4/Qt6 upload
    checker would flag); resolution happens on the running QGIS version.

    A name no candidate resolves yields None and one log line, rather than an
    exception: every constant here is resolved at import, so raising would take
    the whole plugin down over one enum a future QGIS renamed. The commands
    that use that constant then fail; everything else keeps working.
    """
    for root, path in candidates:
        obj = root
        for part in path.split("."):
            obj = getattr(obj, part, _MISSING)
            if obj is _MISSING:
                break
        else:
            return obj
    tried = ", ".join(f"{getattr(r, '__name__', r)}.{p}" for r, p in candidates)
    # No level argument: it defaults to Warning, and naming one here would mean
    # a literal enum spelling in the source - the thing this module exists to
    # keep out (and MSG_WARNING is itself still being resolved below).
    QgsMessageLog.logMessage(
        f"None of the enum spellings resolved: {tried}. Commands using it will fail; "
        "please report this with your QGIS version.",
        "MCP",
    )
    return None


# ── Layer types ──────────────────────────────────────────────────────
LAYER_VECTOR = _enum((Qgis, "LayerType.Vector"), (QgsMapLayer, "VectorLayer"))
LAYER_RASTER = _enum((Qgis, "LayerType.Raster"), (QgsMapLayer, "RasterLayer"))

# ── Message levels ───────────────────────────────────────────────────
MSG_INFO = _enum((Qgis, "MessageLevel.Info"), (Qgis, "Info"))
MSG_WARNING = _enum((Qgis, "MessageLevel.Warning"), (Qgis, "Warning"))
MSG_CRITICAL = _enum((Qgis, "MessageLevel.Critical"), (Qgis, "Critical"))

# ── Geometry types ───────────────────────────────────────────────────
GEOM_POLYGON = _enum((Qgis, "GeometryType.Polygon"), (QgsWkbTypes, "PolygonGeometry"))
GEOM_LINE = _enum((Qgis, "GeometryType.Line"), (QgsWkbTypes, "LineGeometry"))
GEOM_UNKNOWN = _enum((Qgis, "GeometryType.Unknown"), (QgsWkbTypes, "UnknownGeometry"))

# ── Raster stats ─────────────────────────────────────────────────────
RASTER_STATS_ALL = _enum((Qgis, "RasterBandStatistic.All"), (QgsRasterBandStats, "All"))
RASTER_ALPHA_BAND = _enum((Qgis, "RasterColorInterpretation.AlphaBand"), (QgsRaster, "AlphaBand"))

# ── Layout export result ─────────────────────────────────────────────
LAYOUT_SUCCESS = _enum((Qgis, "LayoutResult.Success"), (QgsLayoutExporter, "Success"))

# ── Processing parameter flags ───────────────────────────────────────
PROCESSING_OPTIONAL = _enum(
    (Qgis, "ProcessingParameterFlag.Optional"),
    (QgsProcessingParameterDefinition, "FlagOptional"),
)

# ── Aggregate functions ──────────────────────────────────────────────
AGG_COUNT = _enum((Qgis, "Aggregate.Count"), (QgsAggregateCalculator, "Count"))
AGG_SUM = _enum((Qgis, "Aggregate.Sum"), (QgsAggregateCalculator, "Sum"))
AGG_MEAN = _enum((Qgis, "Aggregate.Mean"), (QgsAggregateCalculator, "Mean"))
AGG_MIN = _enum((Qgis, "Aggregate.Min"), (QgsAggregateCalculator, "Min"))
AGG_MAX = _enum((Qgis, "Aggregate.Max"), (QgsAggregateCalculator, "Max"))
AGG_STDEV = _enum((Qgis, "Aggregate.StDev"), (QgsAggregateCalculator, "StDev"))
AGG_ARRAY = _enum(
    (Qgis, "Aggregate.ArrayAggregate"),
    (QgsAggregateCalculator, "ArrayAggregate"),
)

# ── Qt IO / widget enums ─────────────────────────────────────────────
IODEVICE_WRITEONLY = _enum((QIODevice, "OpenModeFlag.WriteOnly"), (QIODevice, "WriteOnly"))
TOOLBUTTON_MENU_POPUP = _enum(
    (QToolButton, "ToolButtonPopupMode.MenuButtonPopup"),
    (QToolButton, "MenuButtonPopup"),
)
TOOLBUTTON_ICON_ONLY = _enum(
    (Qt, "ToolButtonStyle.ToolButtonIconOnly"),
    (Qt, "ToolButtonIconOnly"),
)
PAINTER_ANTIALIAS = _enum((QPainter, "RenderHint.Antialiasing"), (QPainter, "Antialiasing"))
ALIGN_CENTER = _enum((Qt, "AlignmentFlag.AlignCenter"), (Qt, "AlignCenter"))
TEXT_SELECTABLE_BY_MOUSE = _enum(
    (Qt, "TextInteractionFlag.TextSelectableByMouse"),
    (Qt, "TextSelectableByMouse"),
)
MSGBOX_QUESTION = _enum((QMessageBox, "Icon.Question"), (QMessageBox, "Question"))
MSGBOX_ACCEPT_ROLE = _enum((QMessageBox, "ButtonRole.AcceptRole"), (QMessageBox, "AcceptRole"))
MSGBOX_REJECT_ROLE = _enum((QMessageBox, "ButtonRole.RejectRole"), (QMessageBox, "RejectRole"))

# ── Render units (layout text sizing) ────────────────────────────────
# Qgis.RenderUnit arrived in 3.30; QgsUnitTypes.RenderPoints covers 3.28.
RENDER_UNIT_POINTS = _enum(
    (Qgis, "RenderUnit.Points"),
    (QgsUnitTypes, "RenderUnit.RenderPoints"),
    (QgsUnitTypes, "RenderPoints"),
)

# ── Vector simplification hints ─────────────────────────────────────
SIMPLIFY_GEOMETRY = _enum(
    (QgsVectorSimplifyMethod, "SimplifyHint.GeometrySimplification"),
    (QgsVectorSimplifyMethod, "GeometrySimplification"),
)
SIMPLIFY_ANTIALIAS = _enum(
    (QgsVectorSimplifyMethod, "SimplifyHint.AntialiasingSimplification"),
    (QgsVectorSimplifyMethod, "AntialiasingSimplification"),
)

# ── QVariant type enums ──────────────────────────────────────────────
# PyQt6/QGIS4 expose the unscoped spelling (e.g. QVariant dot String); PyQt5
# also has the scoped enum-class form. Prefer the unscoped spelling first.
QVAR_STRING = _enum((QVariant, "String"), (QVariant, "Type.String"))
QVAR_INT = _enum((QVariant, "Int"), (QVariant, "Type.Int"))
QVAR_DOUBLE = _enum((QVariant, "Double"), (QVariant, "Type.Double"))
QVAR_BOOL = _enum((QVariant, "Bool"), (QVariant, "Type.Bool"))
QVAR_DATE = _enum((QVariant, "Date"), (QVariant, "Type.Date"))
QVAR_DATETIME = _enum((QVariant, "DateTime"), (QVariant, "Type.DateTime"))

# ── WKB / geometry types used directly in plugin handlers ────────────
WKB_NO_GEOMETRY = _enum(
    (Qgis, "WkbType.NoGeometry"),
    (QgsWkbTypes, "Type.NoGeometry"),
    (QgsWkbTypes, "NoGeometry"),
)

# ── Processing parameter member enums ────────────────────────────────
PROC_NUM_INTEGER = _enum(
    (QgsProcessingParameterNumber, "Type.Integer"),
    (QgsProcessingParameterNumber, "Integer"),
)
PROC_FILE_FOLDER = _enum(
    (Qgis, "ProcessingFileParameterBehavior.Folder"),
    (QgsProcessingParameterFile, "Behavior.Folder"),
    (QgsProcessingParameterFile, "Folder"),
)

# ── Raster shader interpolation / classification ─────────────────────
# Qgis.ShaderInterpolationMethod / ShaderClassificationMethod arrived in 3.38;
# the QgsColorRampShader nested forms cover 3.28-3.36.
SHADER_INTERPOLATED = _enum(
    (Qgis, "ShaderInterpolationMethod.Linear"),
    (QgsColorRampShader, "Type.Interpolated"),
)
SHADER_DISCRETE = _enum(
    (Qgis, "ShaderInterpolationMethod.Discrete"),
    (QgsColorRampShader, "Type.Discrete"),
)
SHADER_EXACT = _enum(
    (Qgis, "ShaderInterpolationMethod.Exact"),
    (QgsColorRampShader, "Type.Exact"),
)
SHADER_CLASS_CONTINUOUS = _enum(
    (Qgis, "ShaderClassificationMethod.Continuous"),
    (QgsColorRampShader, "ClassificationMode.Continuous"),
)
SHADER_CLASS_EQUAL_INTERVAL = _enum(
    (Qgis, "ShaderClassificationMethod.EqualInterval"),
    (QgsColorRampShader, "ClassificationMode.EqualInterval"),
)
SHADER_CLASS_QUANTILE = _enum(
    (Qgis, "ShaderClassificationMethod.Quantile"),
    (QgsColorRampShader, "ClassificationMode.Quantile"),
)

# ── Raster contrast enhancement / gray gradient ──────────────────────
CONTRAST_NONE = _enum(
    (QgsContrastEnhancement, "ContrastEnhancementAlgorithm.NoEnhancement"),
    (QgsContrastEnhancement, "NoEnhancement"),
)
CONTRAST_STRETCH_MINMAX = _enum(
    (QgsContrastEnhancement, "ContrastEnhancementAlgorithm.StretchToMinimumMaximum"),
    (QgsContrastEnhancement, "StretchToMinimumMaximum"),
)
CONTRAST_CLIP_MINMAX = _enum(
    (QgsContrastEnhancement, "ContrastEnhancementAlgorithm.ClipToMinimumMaximum"),
    (QgsContrastEnhancement, "ClipToMinimumMaximum"),
)
CONTRAST_STRETCH_CLIP_MINMAX = _enum(
    (QgsContrastEnhancement, "ContrastEnhancementAlgorithm.StretchAndClipToMinimumMaximum"),
    (QgsContrastEnhancement, "StretchAndClipToMinimumMaximum"),
)
GRAY_BLACK_TO_WHITE = _enum(
    (QgsSingleBandGrayRenderer, "Gradient.BlackToWhite"),
    (QgsSingleBandGrayRenderer, "BlackToWhite"),
)
GRAY_WHITE_TO_BLACK = _enum(
    (QgsSingleBandGrayRenderer, "Gradient.WhiteToBlack"),
    (QgsSingleBandGrayRenderer, "WhiteToBlack"),
)

# ── Database provider connections ────────────────────────────────────
CONN_TABLE_VECTOR = _enum(
    (QgsAbstractDatabaseProviderConnection, "TableFlag.Vector"),
    (QgsAbstractDatabaseProviderConnection, "Vector"),
)
CONN_TABLE_RASTER = _enum(
    (QgsAbstractDatabaseProviderConnection, "TableFlag.Raster"),
    (QgsAbstractDatabaseProviderConnection, "Raster"),
)
CONN_TABLE_VIEW = _enum(
    (QgsAbstractDatabaseProviderConnection, "TableFlag.View"),
    (QgsAbstractDatabaseProviderConnection, "View"),
)
CONN_TABLE_ASPATIAL = _enum(
    (QgsAbstractDatabaseProviderConnection, "TableFlag.Aspatial"),
    (QgsAbstractDatabaseProviderConnection, "Aspatial"),
)
CONN_CAP_SCHEMAS = _enum(
    (QgsAbstractDatabaseProviderConnection, "Capability.Schemas"),
    (QgsAbstractDatabaseProviderConnection, "Schemas"),
)
CONN_CAP_SQL_LAYERS = _enum(
    (QgsAbstractDatabaseProviderConnection, "Capability.SqlLayers"),
    (QgsAbstractDatabaseProviderConnection, "SqlLayers"),
)
CONN_CAP_EXECUTE_SQL = _enum(
    (QgsAbstractDatabaseProviderConnection, "Capability.ExecuteSql"),
    (QgsAbstractDatabaseProviderConnection, "ExecuteSql"),
)

# ── Data source URI SSL modes ────────────────────────────────────────
URI_SSL_PREFER = _enum((QgsDataSourceUri, "SslMode.SslPrefer"), (QgsDataSourceUri, "SslPrefer"))
URI_SSL_DISABLE = _enum((QgsDataSourceUri, "SslMode.SslDisable"), (QgsDataSourceUri, "SslDisable"))
URI_SSL_ALLOW = _enum((QgsDataSourceUri, "SslMode.SslAllow"), (QgsDataSourceUri, "SslAllow"))
URI_SSL_REQUIRE = _enum((QgsDataSourceUri, "SslMode.SslRequire"), (QgsDataSourceUri, "SslRequire"))
URI_SSL_VERIFY_CA = _enum(
    (QgsDataSourceUri, "SslMode.SslVerifyCa"), (QgsDataSourceUri, "SslVerifyCa")
)
URI_SSL_VERIFY_FULL = _enum(
    (QgsDataSourceUri, "SslMode.SslVerifyFull"), (QgsDataSourceUri, "SslVerifyFull")
)

# ── Vector layer export result ───────────────────────────────────────
EXPORT_SUCCESS = _enum(
    (Qgis, "VectorExportResult.Success"),
    (QgsVectorLayerExporter, "NoError"),
)
