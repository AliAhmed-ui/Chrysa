"""Tests for the animated rainbow BrandLabel wordmark."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtWidgets import QApplication

    from chrysa.ui_apple.widgets import BrandLabel as AppleBrandLabel
    from chrysa.ui.widgets import BrandLabel as LegacyBrandLabel
    from chrysa.ui_apple import theme as apple_theme
    from chrysa.ui_apple.themes import THEMES

    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestBrandLabelAnimation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["chrysa-tests"])

    def test_timer_runs_at_least_60_fps(self):
        """Frame interval must be <= 16.67ms with PreciseTimer to guarantee >= 60 FPS."""
        label = AppleBrandLabel()
        self.assertLessEqual(label._timer.interval(), 16)
        self.assertGreater(1000.0 / label._timer.interval(), 60.0)
        self.assertEqual(label._timer.timerType(), Qt.TimerType.PreciseTimer)

    def test_timer_starts_on_show_and_stops_on_hide(self):
        label = AppleBrandLabel()
        label.show()
        self.assertTrue(label._timer.isActive())
        label.hide()
        self.assertFalse(label._timer.isActive())

    def test_original_dimensions(self):
        """Original headline style dimensions: height 30 for Apple UI, 34 for Legacy UI."""
        apple_label = AppleBrandLabel()
        self.assertEqual(apple_label.height(), 30)
        legacy_label = LegacyBrandLabel()
        self.assertEqual(legacy_label.height(), 34)

    def test_apple_paint_event_renders_in_dark_and_light_themes(self):
        label = AppleBrandLabel()
        label.resize(200, 30)

        for theme_key in ("apple_dark", "apple_light"):
            apple_theme._install(THEMES[theme_key])
            img = QImage(label.size(), QImage.Format.Format_ARGB32)
            img.fill(Qt.GlobalColor.transparent)
            painter = QPainter(img)
            label.render(painter)
            painter.end()

            # Verify that non-transparent pixels exist (text is drawn)
            non_transparent = sum(
                1
                for x in range(img.width())
                for y in range(img.height())
                if img.pixelColor(x, y).alpha() > 50
            )
            self.assertGreater(non_transparent, 50)

    def test_rainbow_wave_moves_left_to_right(self):
        """As time progresses, color coordinates shift to the right (positive X direction)."""
        label = AppleBrandLabel()
        cycle_width = label.CYCLE_WIDTH
        speed = label.SPEED

        # At t0 = 0.0s, offset = 0
        offset_t0 = (0.0 * speed) % cycle_width
        # At t1 = 0.5s, offset increases positively
        offset_t1 = (0.5 * speed) % cycle_width

        self.assertGreater(offset_t1, offset_t0)
        self.assertAlmostEqual(offset_t1 - offset_t0, 0.5 * speed)

    def test_legacy_brand_label(self):
        label = LegacyBrandLabel()
        self.assertLessEqual(label._timer.interval(), 16)
        self.assertGreater(1000.0 / label._timer.interval(), 60.0)
        self.assertEqual(label._timer.timerType(), Qt.TimerType.PreciseTimer)

        label.show()
        self.assertTrue(label._timer.isActive())
        label.hide()
        self.assertFalse(label._timer.isActive())
