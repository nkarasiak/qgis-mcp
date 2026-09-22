"""Lookups and conversions shared by every handler group.

Mixed into ``QgisMCPServer`` last, so the domain mixins can rely on these
without importing each other.
"""

from qgis.core import (
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsProject,
)
from qgis.PyQt.QtCore import QVariant

from ..compat import LAYER_RASTER, LAYER_VECTOR
from ..errors import CommandError, LayerNotFound, WrongLayerType


class HandlerBase:
    """Layer lookup and value conversion used by every other mixin."""

    @staticmethod
    def _layer(layer_id):
        """The project layer with *layer_id*, or raise :class:`LayerNotFound`.

        Every handler that takes a ``layer_id`` needs exactly this lookup, and
        it used to be open-coded in a dozen of them: a ``mapLayers()``
        membership test, a raise, and a ``mapLayer()`` call, with the wording of
        the error drifting between sites.
        """
        layer = QgsProject.instance().mapLayer(layer_id)
        if layer is None:
            raise LayerNotFound(layer_id)
        return layer

    @classmethod
    def _get_vector_layer(cls, layer_id):
        """The layer with *layer_id*, or raise unless it is a vector layer."""
        layer = cls._layer(layer_id)
        if layer.type() != LAYER_VECTOR:
            raise WrongLayerType(f"Not a vector layer: {layer_id}")
        return layer

    @classmethod
    def _get_raster_layer(cls, layer_id):
        """The layer with *layer_id*, or raise unless it is a raster layer."""
        layer = cls._layer(layer_id)
        if layer.type() != LAYER_RASTER:
            raise WrongLayerType(f"Not a raster layer: {layer_id}")
        return layer

    @staticmethod
    def _check_filter_expression(layer, expression):
        """Raise unless *expression* parses and prepares against *layer*.

        A filter that fails to parse, or names a field the layer lacks, matches
        nothing without raising - so "0 features" reads as a real answer when
        the filter never ran.
        """
        expr = QgsExpression(expression)
        if expr.hasParserError():
            raise CommandError(f"Expression parse error: {expr.parserErrorString()}")
        context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        if not expr.prepare(context) or expr.hasEvalError():
            raise CommandError(f"Expression error: {expr.evalErrorString()}")

    @staticmethod
    def _pick(mapping, key, label):
        try:
            return mapping[key]
        except KeyError:
            raise CommandError(f"Unknown {label}: {key!r}. Use one of {sorted(mapping)}") from None

    def _is_visible(self, project, layer_id):
        """Visibility of a layer in the layer tree.

        Non-spatial tables (attribute-only tables, e.g. GeoPackage tables used by QGIS relations)
        live in the project but have no node in the layer tree, so findLayer() returns None.
        Treat them as not visible instead of raising AttributeError.
        """
        node = project.layerTreeRoot().findLayer(layer_id)
        return node.isVisible() if node is not None else False

    def _get_layer_type(self, layer):
        if layer.type() == LAYER_VECTOR:
            return f"vector_{layer.geometryType()}"
        elif layer.type() == LAYER_RASTER:
            return "raster"
        else:
            return str(layer.type())

    def _convert_attribute(self, value):
        """Convert a QVariant / Qt / Python attribute value to a JSON-serializable type.

        PyQGIS hands back Qt date types unwrapped, so the date branch has to run on
        the bare value, not only inside the QVariant case. A value that reaches
        str() unconverted comes out as a PyQt repr, not a date.
        """
        if isinstance(value, QVariant):
            if value.isNull():
                return None
            value = value.value()
        # Tuple form, not `int | float | ...`: PEP 604 unions in isinstance need
        # Python 3.10, and QGIS ships 3.9 well past 3.28 (3.42 still does). The
        # union form raises TypeError there, which broke every feature read.
        if isinstance(value, (int, float, str, bool, type(None))):
            return value
        for to_py in ("toPyDateTime", "toPyDate", "toPyTime"):
            if hasattr(value, to_py):
                value = getattr(value, to_py)()
                break
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)
