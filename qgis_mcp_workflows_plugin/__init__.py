def classFactory(iface):  # camelCase name required by the QGIS plugin API
    """QGIS plugin entry point.

    Imported lazily so sibling modules in this package (e.g. ``colormaps``) stay
    importable without a QGIS runtime — the unit-test process has no ``qgis``.
    QGIS still calls ``classFactory(iface)`` exactly as before.
    """
    from .plugin import classFactory as _class_factory

    return _class_factory(iface)
