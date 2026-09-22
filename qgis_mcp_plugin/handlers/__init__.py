"""Command handler mixins that make up ``QgisMCPServer``.

One module per domain. ``base`` comes last in the MRO: it holds the
lookups and conversions the other mixins call.
"""

from .base import HandlerBase
from .canvas import CanvasHandlers
from .connections import ConnectionHandlers
from .features import FeatureHandlers
from .layers import LayerHandlers
from .layout import LayoutHandlers
from .processing import ProcessingHandlers
from .project import ProjectHandlers
from .session import SessionHandlers
from .style import StyleHandlers
from .system import SystemHandlers

__all__ = [
    "CanvasHandlers",
    "ConnectionHandlers",
    "FeatureHandlers",
    "HandlerBase",
    "LayerHandlers",
    "LayoutHandlers",
    "ProcessingHandlers",
    "ProjectHandlers",
    "SessionHandlers",
    "StyleHandlers",
    "SystemHandlers",
]
