"""An automatic route out of a blocked region: a private Tor client that Chrysa starts only when it is needed.

Used by :mod:`chrysa.linkdl` after a geo-restriction failure when the user has not set a proxy of their own. Tor is
free and needs no account, so it is the one exit Chrysa can offer without inventing a third-party proxy. It must be
installed (``sudo apt install tor``); Chrysa runs its own instance on a random local port, asks for an exit in a
country the site says it serves when yt-dlp names one, and stops it when Chrysa closes. Tor is slower than a direct
connection and some sites refuse its exits, so it is a fallback, never the first attempt.
"""

from __future__ import annotations

import atexit
import logging
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("chrysa.geoproxy")

BOOT_SECONDS = 75


class TorUnavailable(RuntimeError):
    """Tor is not installed or did not start (shown to the user without technical detail)."""


def installed() -> bool:
    return shutil.which("tor") is not None


class _Route:
    def __init__(self, process: subprocess.Popen, port: int, data: Path) -> None:
        self.process, self.port, self.data = process, port, data

    def alive(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> None:
        if self.alive():
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        shutil.rmtree(self.data, ignore_errors=True)


_lock = threading.Lock()
_routes: dict[tuple[str, ...], _Route] = {}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _countries(codes) -> tuple[str, ...]:
    return tuple(sorted(c.lower() for c in (codes or ()) if re.fullmatch(r"[A-Za-z]{2}", str(c))))


def proxy_for(countries=(), stop: threading.Event | None = None) -> str:
    """A ``socks5h://127.0.0.1:port`` URL for a running private Tor, starting one if needed (blocks up to ~75 s)."""
    tor = shutil.which("tor")
    if tor is None:
        raise TorUnavailable("Tor is not installed.")
    key = _countries(countries)
    with _lock:
        route = _routes.get(key)
        if route is not None and route.alive():
            return f"socks5h://127.0.0.1:{route.port}"
        data = Path(tempfile.mkdtemp(prefix="chrysa-tor-"))
        port = _free_port()
        args = [tor, "--SocksPort", f"127.0.0.1:{port}", "--DataDirectory", str(data), "--ClientOnly", "1",
                "--Log", "notice stdout", "--ControlPort", "0", "--ClientUseIPv6", "0"]
        if key:
            args += ["--ExitNodes", ",".join("{%s}" % c for c in key), "--StrictNodes", "1"]
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)  # no shell
        route = _Route(process, port, data)
        ready = threading.Event()

        def watch() -> None:
            for line in process.stdout:  # type: ignore[union-attr]
                if "Bootstrapped 100%" in line:
                    ready.set()
        threading.Thread(target=watch, name="chrysa-tor", daemon=True).start()
        deadline = time.monotonic() + BOOT_SECONDS
        while not ready.is_set():
            if not route.alive() or time.monotonic() > deadline or (stop is not None and stop.is_set()):
                route.stop()
                raise TorUnavailable("Tor did not start.")
            ready.wait(0.3)
        _routes[key] = route
        log.info("[LINK] private Tor route ready (exit %s)", ",".join(key) or "any")
        return f"socks5h://127.0.0.1:{port}"


def stop_all() -> None:
    with _lock:
        routes, _routes_copy = list(_routes.values()), _routes.clear()
    for route in routes:
        route.stop()


atexit.register(stop_all)
