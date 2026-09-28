"""Component-owned engine profiles.

A later component agent adds a module in this package and calls
``register_engine_profile`` at import time. ``load_registered_components``
discovers those modules with ``pkgutil`` and never names them. Do not
edit the core contract to add a component.
"""
