"""Which GUI package the app runs.

Each design is a self-contained package next to this file, with the same module
layout (``theme``, ``main_window``, ...). To go back to the previous look, change
``ACTIVE`` to ``"ui"``; nothing else needs to move.
"""

from __future__ import annotations

import importlib
from types import ModuleType

ACTIVE = "ui_apple"  # "ui" = the original neon design, "ui_apple" = the Apple-inspired one


def load(module: str) -> ModuleType:
    """Import *module* (e.g. ``"theme"``) from the active design package."""
    return importlib.import_module(f"{__package__}.{ACTIVE}.{module}")
