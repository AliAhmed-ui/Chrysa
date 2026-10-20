"""Apple-design specifics: tokens, reduced motion, keyboard operation of the new controls."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtWidgets import QApplication, QPushButton

    from chrysa import design
    from chrysa.ui_apple import theme
    from chrysa.ui_apple.themes import THEMES
    from chrysa.ui_apple.toggle import SegmentedControl

    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class Tokens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["chrysa-tests"])

    def tearDown(self):
        theme.set_reduced_motion(None)

    def test_design_pointer_names_an_importable_package(self):
        self.assertEqual(design.load("theme").__name__, f"chrysa.{design.ACTIVE}.theme")

    def test_reduced_motion_shortens_every_duration(self):
        theme.set_reduced_motion(False)
        self.assertEqual(theme.ms("slow"), theme.MOTION["slow"])
        theme.set_reduced_motion(True)
        self.assertLessEqual(theme.ms("slow"), 60)
        self.assertLessEqual(theme.ms(500), 60)

    def test_type_ramp_tightens_large_text_and_opens_small_text(self):
        self.assertLess(theme.TYPE["largeTitle"][2], 0)
        self.assertGreater(theme.TYPE["caption"][2], 0)

    def test_every_theme_style_sheet_has_focus_rings(self):
        for palette in THEMES.values():
            theme._install(palette)
            sheet = theme.style_sheet()
            for selector in ("QPushButton:focus", "QToolButton:focus", "QComboBox:focus", "#searchBox:focus"):
                self.assertIn(selector, sheet)
        theme._install(THEMES[theme.manager.current_theme_key])

    def test_buttons_take_keyboard_focus_but_not_mouse_focus(self):
        theme.apply(self.app)
        button = QPushButton("x")
        button.ensurePolished()
        self.app.sendEvent(button, QEvent(QEvent.Type.Polish))
        self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.TabFocus)


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class Segmented(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["chrysa-tests"])

    def test_arrow_keys_pick_a_side_and_signal(self):
        control = SegmentedControl("Browse", "Library")
        seen = []
        control.toggled.connect(seen.append)
        control.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))
        control.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(seen, [True, False])

    def test_silent_set_does_not_signal(self):
        control = SegmentedControl("Browse", "Library")
        seen = []
        control.toggled.connect(seen.append)
        control.set_checked_silently(True)
        self.assertTrue(control.isChecked())
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
