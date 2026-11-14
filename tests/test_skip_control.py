"""The Skip Intro control and its place in the player (both designs): when it shows, how it behaves, where it sits."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QEvent, QEventLoop, QPoint, Qt, QTimer
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QWidget

    from chrysa.skip_control import SkipIntroControl

    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

from tests.helpers import LibraryCase


def spin(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class ControlCase:
    design = ""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["chrysa-tests"])
        cls.theme = __import__(f"chrysa.{cls.design}.theme", fromlist=["apply"])
        cls.theme.apply(cls.app)

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.control = SkipIntroControl(self.host, self.theme)
        self.presses = []
        self.control.activated.connect(lambda: self.presses.append(1))
        self.host.show()
        self.addCleanup(self.host.close)
        self.addCleanup(self.theme.set_reduced_motion, None) if hasattr(self.theme, "set_reduced_motion") else None

    def test_hidden_until_asked_and_gone_after_dismissal(self):
        self.assertFalse(self.control.isVisibleTo(self.host))
        self.control.present()
        self.assertTrue(self.control.isVisibleTo(self.host))
        self.assertTrue(self.control.is_presented)
        spin(350)
        self.assertEqual(self.control._progress, 1.0)
        self.control.dismiss()
        self.assertFalse(self.control.is_presented)
        spin(350)
        self.assertFalse(self.control.isVisibleTo(self.host))  # it does not linger

    def test_it_arrives_by_growing_not_by_popping(self):
        self.control.present()
        spin(60)
        partway = self.control._progress
        self.assertTrue(0.0 < partway < 1.0)
        mid = self.control._pill().width()
        spin(350)
        self.assertGreater(self.control._pill().width(), mid)
        self.assertEqual(self.control._pill().width(), self.control.width())  # fully open

    def test_reduced_motion_is_instant(self):
        if not hasattr(self.theme, "set_reduced_motion"):
            self.skipTest("this design has no reduced-motion setting")
        self.theme.set_reduced_motion(True)
        self.control.present()
        self.assertEqual(self.control._progress, 1.0)
        self.control.dismiss()
        self.assertFalse(self.control.isVisibleTo(self.host))

    def test_one_activation_per_showing(self):
        self.control.present()
        spin(300)
        for _ in range(3):  # a double click and a key press
            self.control.activate()
        self.assertEqual(self.presses, [1])
        spin(300)
        self.control.present()  # shown again (the viewer went back into the intro)
        spin(300)
        self.control.activate()
        self.assertEqual(self.presses, [1, 1])

    def test_mouse_click_activates_only_inside_the_pill(self):
        self.control.present()
        spin(300)
        QTest.mouseClick(self.control, Qt.MouseButton.LeftButton, pos=self.control.rect().center())
        self.assertEqual(self.presses, [1])

    def test_keyboard_when_focused_and_never_otherwise(self):
        self.control.present()
        spin(300)
        QTest.keyClick(self.control, Qt.Key.Key_Left)  # player keys are not swallowed
        QTest.keyClick(self.control, Qt.Key.Key_F)
        self.assertEqual(self.presses, [])
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier)
        self.control.keyPressEvent(event)
        self.assertFalse(event.isAccepted())
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.control.reset()
            self.control.present()
            QTest.keyClick(self.control, key)
        self.assertEqual(len(self.presses), 3)

    def test_it_is_reachable_with_tab_but_does_not_steal_focus(self):
        self.assertEqual(self.control.focusPolicy(), Qt.FocusPolicy.TabFocus)
        self.assertEqual(self.control.accessibleName(), "Skip Intro")

    def test_it_paints_and_is_clipped_to_the_pill(self):
        self.control.present()
        spin(350)
        image = self.control.grab().toImage()
        self.assertFalse(image.isNull())
        self.assertGreater(self.control.mask().boundingRect().width(), self.control.width() * 0.9)
        self.control.reset()
        self.assertFalse(self.control.isVisibleTo(self.host))

    def test_it_scales_with_the_window(self):
        small = (self.control.width(), self.control.pill_height())
        self.control.set_scale(1.4)
        self.assertGreater(self.control.width(), small[0])
        self.assertGreater(self.control.pill_height(), small[1])
        self.assertLess(self.control.pill_height(), 60)  # still compact in fullscreen


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class ClassicControl(ControlCase, unittest.TestCase):
    design = "ui"


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class AppleControl(ControlCase, unittest.TestCase):
    design = "ui_apple"


class PlayerCase:
    """The control as the player drives it: only inside this episode's window, and where it should sit."""

    design = ""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["chrysa-tests"])
        cls.theme = __import__(f"chrysa.{cls.design}.theme", fromlist=["apply"])
        cls.theme.apply(cls.app)
        cls.player_module = __import__(f"chrysa.{cls.design}.player", fromlist=["PlayerPage"])

    def make_player(self):
        player = self.player_module.PlayerPage(self.library)
        player.resize(1280, 720)
        player.show()
        self.addCleanup(player.close)
        return player

    def test_only_inside_the_episodes_own_window(self):
        player = self.make_player()
        player._position_valid = True
        player._intro = (125.0, 198.0)  # this episode's own timestamps, not a season's
        for position, expected in ((10.0, False), (120.0, False), (125.0, True), (160.0, True), (197.5, False), (400.0, False)):
            player._update_skip_intro(position)
            spin(300)
            self.assertEqual(player._skip_intro.isVisibleTo(player), expected, f"at {position}")

    def test_no_intro_for_this_episode_means_no_control(self):
        player = self.make_player()
        player._position_valid = True
        player._intro = None
        for position in (0.0, 60.0, 125.0):
            player._update_skip_intro(position)
        spin(300)
        self.assertFalse(player._skip_intro.isVisibleTo(player))

    def test_skip_seeks_to_the_end_and_enter_does_the_same(self):
        player = self.make_player()
        player._media = mock.Mock(id=1)
        player._intro = (125.0, 198.0)
        player._position_valid = True
        with mock.patch.object(player, "seek") as seek, mock.patch.object(player, "_show_hud"):
            player._update_skip_intro(130.0)
            spin(300)
            player._skip_intro.activate()
            seek.assert_called_once_with(198.0)
            spin(300)
            self.assertFalse(player._skip_intro.isVisibleTo(player))
            player._skip_intro.reset()
            player._update_skip_intro(131.0)
            spin(300)
            QTest.keyClick(player, Qt.Key.Key_Return)
            self.assertEqual(seek.call_count, 2)

    def test_enter_does_nothing_without_the_control_and_space_still_pauses(self):
        player = self.make_player()
        with mock.patch.object(player, "toggle_pause") as pause, mock.patch.object(player, "skip_intro") as skip:
            QTest.keyClick(player, Qt.Key.Key_Return)
            skip.assert_not_called()
            QTest.keyClick(player, Qt.Key.Key_Space)
            pause.assert_called_once()

    def test_placement_clears_the_control_bar_and_follows_the_window(self):
        player = self.make_player()
        bar_top = player.controls.geometry().top()
        for width, height in ((1280, 720), (800, 500), (1920, 1080)):
            player.resize(width, height)
            control = player._skip_intro
            pill_bottom = control.y() + control.pill_height()
            self.assertLess(pill_bottom, player.controls.geometry().top(), (width, height))
            self.assertLessEqual(control.x() + control.width(), width)
            self.assertGreater(control.x(), width // 2)  # the right side, clear of centred subtitles
        self.assertLess(player._skip_intro.pill_height(), 60)
        self.assertGreater(bar_top, 0)

    def test_a_new_video_starts_clean(self):
        player = self.make_player()
        player._position_valid = True
        player._intro = (10.0, 90.0)
        player._update_skip_intro(20.0)
        spin(300)
        self.assertTrue(player._skip_intro.isVisibleTo(player))
        player._skip_intro.reset()
        self.assertFalse(player._skip_intro.is_presented)


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class ClassicPlayer(PlayerCase, LibraryCase):
    design = "ui"


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class ApplePlayer(PlayerCase, LibraryCase):
    design = "ui_apple"


if __name__ == "__main__":
    unittest.main()
