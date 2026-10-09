"""Application entry point: argument handling, Qt bootstrap, single-instance guard."""

from __future__ import annotations

import os
import signal
import sys

from . import cli
from .config import APP_ID, APP_NAME, get_paths


def main(argv: list[str] | None = None) -> int:
    args = cli.build_parser().parse_args(argv)
    if cli.is_headless(args):
        return cli.run(args)
    return run_gui(args)


def run_gui(args) -> int:
    # libVLC draws into an X11 window, so the app must run on X11/XWayland even in
    # a Wayland session; a native Wayland window would leave the video detached.
    if sys.platform.startswith("linux"):
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    # Qt 6.8+ logs a harmless portal-registration warning for non-sandboxed apps.
    rules = os.environ.get("QT_LOGGING_RULES", "")
    os.environ["QT_LOGGING_RULES"] = ";".join(filter(None, [rules, "qt.qpa.services=false"]))

    from PyQt6.QtCore import QLockFile, QTimer
    from PyQt6.QtNetwork import QLocalServer, QLocalSocket
    from PyQt6.QtWidgets import QApplication

    from .library import Library
    from .ui import theme
    from .ui.main_window import MainWindow

    # argv[0] becomes the X11 instance name; with the application name it forms
    # WM_CLASS ("theia", "Theia"), which is how the dock matches theia.desktop.
    app = QApplication([APP_ID])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName(APP_ID)

    paths = get_paths().ensure()

    # One window per user: a second launch just raises the first.
    server_name = f"theia-{os.getuid()}"
    lock = QLockFile(str(paths.data_dir / "theia.lock"))
    lock.setStaleLockTime(0)  # a lock left by a crashed process is detected by its dead PID
    if not lock.tryLock(100):
        socket = QLocalSocket()
        socket.connectToServer(server_name)
        if socket.waitForConnected(1500):
            socket.write(b"raise\n")
            socket.waitForBytesWritten(1000)
            socket.disconnectFromServer()
            return 0
        # The lock holder is not answering; carry on rather than refuse to start.

    library = Library.open(paths)
    library.ensure_default_folder()
    for folder in args.add_folder:
        library.add_folder(folder)

    # The saved theme (settings key "ui.theme") must be adopted before anything is painted.
    theme.manager.bind_storage(
        load=lambda: library.setting(theme.SETTING_KEY),
        save=lambda key: library.set_setting(theme.SETTING_KEY, key),
    )
    theme.apply(app)
    app.setWindowIcon(theme.app_icon())
    window = MainWindow(library)

    QLocalServer.removeServer(server_name)
    server = QLocalServer(app)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)

    def on_connection() -> None:
        connection = server.nextPendingConnection()
        if connection is not None:
            connection.disconnectFromServer()
        window.bring_to_front()

    server.newConnection.connect(on_connection)
    server.listen(server_name)

    # Ctrl+C / SIGTERM close the window properly, so the playback position is saved.
    # Python only runs signal handlers between bytecodes, hence the idle timer.
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: QTimer.singleShot(0, window.close))
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(300)

    window.show()
    code = app.exec()
    server.close()
    lock.unlock()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
