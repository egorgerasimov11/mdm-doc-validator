"""Consolidation: append extracted vendors into a Business Partner template.

The mapping engine is the sap-vendor-autoload converter (installed as the
sap_vendor_autoload package via an editable install, like address-validator).
Everything here degrades gracefully when the package is absent: available()
gates the UI tab and the routes.
"""
from __future__ import annotations

import importlib.util


def available() -> bool:
    return importlib.util.find_spec("sap_vendor_autoload") is not None
