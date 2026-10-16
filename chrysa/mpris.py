"""MPRIS (org.mpris.MediaPlayer2) service: hardware media keys and Bluetooth headphone buttons.

The D-Bus loop runs in a daemon thread. Qt widgets must only be touched from the GUI thread,
so every command is delivered through a Qt signal, which queues it across threads.
Linux only; without pydbus/PyGObject the service quietly stays off.
"""
from __future__ import annotations

import sys
import threading

from PyQt6.QtCore import QObject, pyqtSignal

try:
    from gi.repository import GLib
    from pydbus import SessionBus
except Exception:  # not Linux, or the optional packages are missing
    GLib = SessionBus = None

BUS_NAME = "org.mpris.MediaPlayer2.chrysa"
OBJECT_PATH = "/org/mpris/MediaPlayer2"


class ChrysaMPRIS:
    """
    <node>
      <interface name="org.mpris.MediaPlayer2">
        <method name="Raise"/>
        <method name="Quit"/>
        <property name="CanQuit" type="b" access="read"/>
        <property name="CanRaise" type="b" access="read"/>
        <property name="HasTrackList" type="b" access="read"/>
        <property name="Identity" type="s" access="read"/>
        <property name="SupportedUriSchemes" type="as" access="read"/>
        <property name="SupportedMimeTypes" type="as" access="read"/>
      </interface>
      <interface name="org.mpris.MediaPlayer2.Player">
        <method name="Next"/>
        <method name="Previous"/>
        <method name="Pause"/>
        <method name="PlayPause"/>
        <method name="Stop"/>
        <method name="Play"/>
        <property name="PlaybackStatus" type="s" access="read"/>
        <property name="CanGoNext" type="b" access="read"/>
        <property name="CanGoPrevious" type="b" access="read"/>
        <property name="CanPlay" type="b" access="read"/>
        <property name="CanPause" type="b" access="read"/>
        <property name="CanControl" type="b" access="read"/>
      </interface>
    </node>
    """

    CanQuit = False
    CanRaise = False
    HasTrackList = False
    Identity = "Chrysa"
    SupportedUriSchemes: list[str] = []
    SupportedMimeTypes: list[str] = []
    CanGoNext = True
    CanGoPrevious = True
    CanPlay = True
    CanPause = True
    CanControl = True

    def __init__(self, player) -> None:
        self._player = player
        self._bridge = _Bridge(player)

    def Raise(self) -> None:
        pass

    def Quit(self) -> None:
        pass

    def Next(self) -> None:
        self._bridge.command.emit("next")

    def Previous(self) -> None:
        self._bridge.command.emit("previous")

    def Pause(self) -> None:
        self._bridge.command.emit("pause")

    def PlayPause(self) -> None:
        self._bridge.command.emit("playpause")

    def Stop(self) -> None:
        self._bridge.command.emit("stop")

    def Play(self) -> None:
        self._bridge.command.emit("play")

    @property
    def PlaybackStatus(self) -> str:
        return self._player.playback_status


class _Bridge(QObject):
    """Lives in the GUI thread; the D-Bus thread emits, the player's slot runs in the GUI thread."""

    command = pyqtSignal(str)

    def __init__(self, player) -> None:
        super().__init__(player)
        self.command.connect(player.handle_media_command)


def start(player) -> None:
    """Publish the player on the session bus from a daemon thread."""
    if SessionBus is None or not sys.platform.startswith("linux"):
        return
    service = ChrysaMPRIS(player)  # built here so the bridge QObject belongs to the GUI thread

    def serve() -> None:
        try:
            bus = SessionBus()
            bus.publish(BUS_NAME, (OBJECT_PATH, service))
            GLib.MainLoop().run()
        except Exception as error:  # no session bus, name taken, ...
            print(f"[chrysa] MPRIS unavailable: {error}", file=sys.stderr)

    threading.Thread(target=serve, name="chrysa-mpris", daemon=True).start()
