"""ThemeManager and the palette table (pure Python, no Qt)."""

import unittest

from chrysa.ui.theme_manager import ThemeManager
from chrysa.ui.themes import DEFAULT_THEME, THEMES

ORDER = ["neon_grid", "deep_cosmos", "solarized_amber", "boreal_forest"]
KEYS = {"key", "display_name", "bg_base", "bg_surface", "border", "accent", "accent_alt", "text_primary", "text_muted"}


class Palettes(unittest.TestCase):
    def test_four_complete_themes_in_cycle_order(self):
        self.assertEqual(list(THEMES), ORDER)
        for key, palette in THEMES.items():
            self.assertEqual(set(palette), KEYS)
            self.assertEqual(palette["key"], key)

    def test_default_exists(self):
        self.assertIn(DEFAULT_THEME, THEMES)


class Manager(unittest.TestCase):
    def test_cycle_order_wraps_and_notifies(self):
        manager = ThemeManager()
        manager.set_theme("neon_grid")
        got = []
        manager.subscribe(got.append)
        for expected in ORDER[1:] + ORDER[:1]:
            palette = manager.cycle_theme()
            self.assertEqual(palette["key"], expected)
        self.assertEqual([p["key"] for p in got], ORDER[1:] + ORDER[:1])

    def test_persistence_round_trip(self):
        store: dict[str, str] = {}
        manager = ThemeManager(load=lambda: store.get("ui.theme"), save=lambda k: store.update({"ui.theme": k}))
        self.assertEqual(manager.current_theme_key, DEFAULT_THEME)
        manager.cycle_theme()
        self.assertEqual(store["ui.theme"], manager.current_theme_key)
        again = ThemeManager(load=lambda: store.get("ui.theme"))
        self.assertEqual(again.current_theme_key, manager.current_theme_key)

    def test_invalid_theme_and_invalid_saved_value_are_ignored(self):
        manager = ThemeManager(load=lambda: "nope")
        self.assertEqual(manager.current_theme_key, DEFAULT_THEME)
        with self.assertLogs("chrysa.ui.theme_manager", level="WARNING"):
            manager.set_theme("also-nope")
        self.assertEqual(manager.current_theme_key, DEFAULT_THEME)

    def test_unsubscribe_and_failing_listener(self):
        manager = ThemeManager()
        calls = []

        def boom(_palette):
            raise RuntimeError("bad listener")

        manager.subscribe(boom)
        manager.subscribe(calls.append)
        with self.assertLogs("chrysa.ui.theme_manager", level="ERROR"):
            manager.cycle_theme()
        self.assertEqual(len(calls), 1)  # one bad listener does not starve the others
        manager.unsubscribe(calls.append)
        manager.unsubscribe(boom)
        manager.cycle_theme()
        self.assertEqual(len(calls), 1)

    def test_failing_save_keeps_the_theme(self):
        def fail(_key):
            raise OSError("disk full")

        manager = ThemeManager(save=fail)
        with self.assertLogs("chrysa.ui.theme_manager", level="ERROR"):
            manager.cycle_theme()
        self.assertNotEqual(manager.current_theme_key, DEFAULT_THEME)


class Readability(unittest.TestCase):
    @unittest.skipUnless(__import__("importlib").util.find_spec("PyQt6"), "PyQt6 not installed")
    def test_contrast(self):
        from chrysa.ui import theme

        for palette in THEMES.values():
            theme._install(palette)
            for fg, bg, minimum in (
                (theme.TEXT, theme.BG, 7.0),
                (theme.TEXT, theme.SURFACE, 7.0),
                (theme.TEXT_MUTED, theme.BG, 3.0),
                (theme.TEXT_MUTED, theme.SURFACE, 2.5),
                (theme.ON_ACCENT, theme.ACCENT, 4.0),
                (theme.ACCENT, theme.BG, 3.0),
            ):
                with self.subTest(theme=palette["key"], fg=fg, bg=bg):
                    self.assertGreaterEqual(theme.contrast_ratio(fg, bg), minimum)
        theme._install(THEMES[theme.manager.current_theme_key])


if __name__ == "__main__":
    unittest.main()
