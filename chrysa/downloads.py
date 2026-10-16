"""A small BitTorrent download client that runs off the UI thread.

Backends, in order of preference: ``libtorrent`` (python-libtorrent bindings) and
the ``aria2c`` command. :func:`start_download` hands a magnet link or ``.torrent``
URL/path to a background thread and returns immediately.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_DIR = Path.home() / "Downloads"
_PERCENT = re.compile(r"\((\d+)%\)")


class DownloadError(Exception):
    pass


def is_valid_target(target: str) -> bool:
    return target.startswith(("magnet:?", "http://", "https://")) or Path(target).suffix == ".torrent"


@dataclass
class Download:
    target: str
    name: str = ""
    state: str = "queued"  # queued | downloading | seeding-done | finished | failed | cancelled
    progress: float = 0.0  # 0..1
    message: str = ""
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)

    def cancel(self) -> None:
        self._stop.set()


def available_backend() -> str | None:
    try:
        import libtorrent  # noqa: F401
        return "libtorrent"
    except ImportError:
        pass
    return "aria2c" if shutil.which("aria2c") else None


class DownloadClient:
    """Owns the download threads. ``on_update`` is called from worker threads."""

    def __init__(self, directory: Path = DEFAULT_DIR, on_update: Callable[[Download], None] | None = None) -> None:
        self.directory = Path(directory)
        self.on_update = on_update or (lambda _download: None)
        self.downloads: list[Download] = []
        self._lock = threading.Lock()

    def start(self, target: str) -> Download:
        target = target.strip()
        if not is_valid_target(target):
            raise DownloadError("Not a magnet link or .torrent file.")
        backend = available_backend()
        if backend is None:
            raise DownloadError(
                "No BitTorrent engine found. Install aria2 (e.g. `sudo apt install aria2`) "
                "or the libtorrent Python bindings, then try again."
            )
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise DownloadError(f"Cannot use download folder {self.directory}: {error.strerror or error}") from error
        download = Download(target)
        with self._lock:
            self.downloads.append(download)
        run = self._run_libtorrent if backend == "libtorrent" else self._run_aria2
        threading.Thread(target=self._guard, args=(run, download), name="chrysa-download", daemon=True).start()
        return download

    def cancel_all(self) -> None:
        with self._lock:
            for download in self.downloads:
                download.cancel()

    # ------------------------------------------------------------ internals

    def _emit(self, download: Download, **changes) -> None:
        for key, value in changes.items():
            setattr(download, key, value)
        self.on_update(download)

    def _guard(self, run: Callable[[Download], None], download: Download) -> None:
        try:
            run(download)
        except Exception as error:  # a failed download must never take the app down
            log.exception("download failed")
            self._emit(download, state="failed", message=str(error))

    def _run_aria2(self, download: Download) -> None:
        self._emit(download, state="downloading", message="Starting aria2c…")
        command = [
            "aria2c", "--dir", str(self.directory), "--seed-time=0", "--bt-save-metadata=true",
            "--follow-torrent=true", "--summary-interval=1", "--console-log-level=warn",
            "--allow-overwrite=false", "--auto-file-renaming=true", "--", download.target,
        ]
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
        )
        assert process.stdout is not None
        # Cancel by terminating the process; the reader loop below then ends.
        threading.Thread(
            target=lambda: (download._stop.wait(), process.poll() is None and process.terminate()), daemon=True
        ).start()
        tail: list[str] = []
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            tail = (tail + [line])[-5:]
            match = _PERCENT.search(line)
            if match:
                self._emit(download, progress=int(match.group(1)) / 100, message=line[:120])
        code = process.wait()
        if download._stop.is_set():
            self._emit(download, state="cancelled", message="Cancelled")
            download._stop.set()
        elif code == 0:
            self._emit(download, state="finished", progress=1.0, message=f"Saved to {self.directory}")
            download._stop.set()  # releases the watcher thread
        else:
            raise DownloadError(tail[-1] if tail else f"aria2c exited with status {code}")

    def _run_libtorrent(self, download: Download) -> None:
        import libtorrent as lt

        session = lt.session({"listen_interfaces": "0.0.0.0:6881"})
        params = self._torrent_params(lt, download.target)
        params.save_path = str(self.directory)
        handle = session.add_torrent(params)
        self._emit(download, state="downloading", message="Fetching metadata…")
        while not download._stop.is_set():
            status = handle.status()
            name = status.name or download.name
            self._emit(
                download, name=name, progress=status.progress,
                message=f"{status.progress * 100:.0f}% · {status.download_rate / 1e6:.2f} MB/s · "
                        f"{status.num_peers} peers",
            )
            if status.is_seeding or status.state == lt.torrent_status.finished:
                self._emit(download, state="finished", progress=1.0, message=f"Saved to {self.directory}")
                break
            time.sleep(1)
        else:
            self._emit(download, state="cancelled", message="Cancelled")
        session.remove_torrent(handle)

    @staticmethod
    def _torrent_params(lt, target: str):
        if target.startswith("magnet:?"):
            return lt.parse_magnet_uri(target)
        if target.startswith(("http://", "https://")):
            request = urllib.request.Request(target, headers={"User-Agent": "Chrysa"})
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()
        else:
            data = Path(target).expanduser().read_bytes()
        params = lt.add_torrent_params()
        params.ti = lt.torrent_info(lt.bdecode(data))
        return params
