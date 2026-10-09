"""Which theme is active, who wants to hear about changes, and where the choice is stored."""

from __future__ import annotations

import logging
from collections.abc import Callable

from .themes import DEFAULT_THEME, THEMES, Palette

log = logging.getLogger(__name__)

Listener = Callable[[Palette], None]


class ThemeManager:
    """Holds the active theme, cycles it and tells subscribers (the UI) when it changes.

    Storage is injected as two callables so the manager knows nothing about Qt or
    SQLite: ``load`` returns the saved key (or ``None``), ``save`` stores a key.
    """

    def __init__(
        self,
        load: Callable[[], str | None] | None = None,
        save: Callable[[str], None] | None = None,
    ) -> None:
        self.current_theme_key: str = DEFAULT_THEME
        self._listeners: list[Listener] = []
        self._save = save
        if load is not None:
            self._adopt(load())

    # ------------------------------------------------------------- state

    def get_current_theme(self) -> Palette:
        return THEMES[self.current_theme_key]

    def get_theme_name(self) -> str:
        return self.get_current_theme()["display_name"]

    def keys(self) -> list[str]:
        return list(THEMES)

    # ----------------------------------------------------------- changes

    def cycle_theme(self) -> Palette:
        """Move to the next theme (wrapping), notify listeners and persist."""
        keys = self.keys()
        following = keys[(keys.index(self.current_theme_key) + 1) % len(keys)]
        self._switch(following)
        return self.get_current_theme()

    def set_theme(self, theme_key: str) -> None:
        """Select *theme_key*; an unknown key is ignored (the current theme stays)."""
        if theme_key not in THEMES:
            log.warning("Unknown theme %r ignored", theme_key)
            return
        if theme_key != self.current_theme_key:
            self._switch(theme_key)

    def bind_storage(
        self, load: Callable[[], str | None], save: Callable[[str], None]
    ) -> None:
        """Attach persistence after construction and adopt the saved theme.

        Listeners are told if the saved theme differs; nothing is written back.
        """
        self._save = save
        saved = load()
        if saved in THEMES and saved != self.current_theme_key:
            self.current_theme_key = saved
            self._notify()

    # ------------------------------------------------------ subscriptions

    def subscribe(self, callback: Listener) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def unsubscribe(self, callback: Listener) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    # ----------------------------------------------------------- internals

    def _adopt(self, key: str | None) -> None:
        if key in THEMES:
            self.current_theme_key = key  # type: ignore[assignment]

    def _switch(self, key: str) -> None:
        self.current_theme_key = key
        self._notify()
        if self._save is not None:
            try:
                self._save(key)
            except Exception:  # a failed write must not undo the visible change
                log.exception("Could not save the theme choice")

    def _notify(self) -> None:
        palette = self.get_current_theme()
        for listener in list(self._listeners):  # copy: listeners may unsubscribe themselves
            try:
                listener(palette)
            except Exception:
                log.exception("Theme listener %r failed", listener)
