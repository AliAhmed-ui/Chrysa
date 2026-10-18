"""A small BitTorrent download client that runs off the UI thread.

Backends, in order of preference: ``libtorrent`` (python-libtorrent bindings) and
the ``aria2c`` command. :func:`start_download` hands a magnet link or ``.torrent``
URL/path to a background thread and returns immediately.
"""

from __future__ import annotations

import json
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
# aria2's periodic summary: [#78dfa0 1.5MiB/1.9GiB(0%) CN:3 SD:1 DL:2.1MiB ETA:12m3s]
_SUMMARY = re.compile(
    r"\[#\w+ ([\d.]+[KMGT]?i?B)/([\d.]+[KMGT]?i?B)\(\d+%\) CN:(\d+)(?: SD:(\d+))? DL:([\d.]+[KMGT]?i?B)(?: ETA:(\S+?))?\]"
)
_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}


def parse_bytes(text: str) -> int:
    """``'1.5MiB'`` -> bytes."""
    match = re.fullmatch(r"([\d.]+)([KMGT]?)i?B", text)
    return int(float(match.group(1)) * _UNITS[match.group(2)]) if match else 0


_ARIA2_EXIT = {
    1: "Unknown error", 2: "Timed out", 3: "Not found", 4: "Too many 'not found' errors",
    5: "Download speed too slow", 6: "Network problem", 8: "Server does not support resuming",
    9: "Not enough disk space in the download folder", 10: "Piece length differs from the control file",
    11: "Already downloading this torrent", 12: "Already downloading this torrent",
    13: "A file with that name already exists", 15: "Could not open the file", 16: "Could not open the file",
    17: "Could not write the file", 18: "Could not create the folder", 19: "Name lookup failed",
    22: "Bad server response", 24: "Authorization failed", 28: "Bad command line options",
}
_NOISE = ("***", "Download Results", "gid", "===", "---", "Status Legend", "(OK)", "(ERR)", "(INPR)", "(RM)",
          "If there are any errors", "[#")


def _is_noise(line: str) -> bool:
    return line.startswith(_NOISE)


def remove_partial_files(directory: Path, names: list[str], target: str) -> None:
    """Delete the files a download created: each top-level *name*, its ``.aria2`` control file and the
    ``<infohash>.torrent`` aria2 saved for a magnet link. Never touches anything outside *directory*."""
    root = directory.resolve()
    victims = []
    for name in names:
        victims += [root / name, root / f"{name}.aria2"]
    btih = re.search(r"btih:([0-9a-fA-F]{40})", target)
    if btih:
        victims.append(root / f"{btih.group(1).lower()}.torrent")
    for victim in victims:
        if victim.resolve().parent != root:  # "../x", "..", absolute paths and the folder itself are refused
            continue
        try:
            if victim.is_dir() and not victim.is_symlink():
                shutil.rmtree(victim)
            elif victim.exists() or victim.is_symlink():
                victim.unlink()
        except OSError:
            log.warning("could not delete %s", victim)


def _format_eta(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes}m" if hours else f"{minutes}m{secs}s" if minutes else f"{secs}s"


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
    directory: Path | None = None
    speed: int = 0  # bytes per second
    eta: str = ""
    size_done: int = 0
    size_total: int = 0
    peers: int = 0
    discard: bool = False  # cancelled by the user: forget it instead of resuming next launch
    files: list[str] = field(default_factory=list)  # top-level names it created in ``directory``
    _process: subprocess.Popen | None = field(default=None, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)

    def cancel(self) -> None:
        """Stop for good; it will not be resumed on the next launch."""
        self.discard = True
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

    def __init__(
        self, directory: Path = DEFAULT_DIR, on_update: Callable[[Download], None] | None = None,
        state_file: Path | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.state_file = Path(state_file) if state_file else None
        self.on_update = on_update or (lambda _download: None)
        self.downloads: list[Download] = []
        self._lock = threading.Lock()
        self.pending: list[dict] = self._load()  # left over from an earlier run, not yet restarted

    def start(
        self, target: str, name: str = "", directory: Path | None = None, files: list[str] | None = None
    ) -> Download:
        target = target.strip()
        if not is_valid_target(target):
            raise DownloadError("Not a magnet link or .torrent file.")
        backend = available_backend()
        if backend is None:
            raise DownloadError(
                "No BitTorrent engine found. Install aria2 (e.g. `sudo apt install aria2`) "
                "or the libtorrent Python bindings, then try again."
            )
        folder = Path(directory) if directory else self.directory
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise DownloadError(f"Cannot use download folder {folder}: {error.strerror or error}") from error
        download = Download(target, name=name, directory=folder, files=list(files or []))
        with self._lock:
            self.downloads.append(download)
            self.pending = [e for e in self.pending if e["target"] != target]
        self._save()
        run = self._run_libtorrent if backend == "libtorrent" else self._run_aria2
        download._thread = threading.Thread(target=self._guard, args=(run, download), name="chrysa-download", daemon=True)
        download._thread.start()
        return download

    def saved(self) -> list[dict]:
        """Downloads left unfinished by an earlier run: ``{"target", "name", "directory", "files"}``."""
        return list(self.pending)

    def _load(self) -> list[dict]:
        try:
            entries = json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file else []
        except (OSError, ValueError):
            return []
        return [e for e in entries if isinstance(e, dict) and is_valid_target(str(e.get("target", "")))]

    def cancel_and_delete(self, download: Download) -> None:
        """Stop *download* and delete what it wrote. Returns at once; ``on_update`` reports the end."""
        download.discard = True
        threading.Thread(target=self._cancel_and_delete, args=(download,), name="chrysa-cancel", daemon=True).start()

    def _cancel_and_delete(self, download: Download) -> None:
        download._stop.set()
        process = download._process
        if process is not None and process.poll() is None:
            process.terminate()
        if download._thread is not None:
            download._thread.join(timeout=10)
        if process is not None and process.poll() is None:
            process.kill()
        remove_partial_files(download.directory or self.directory, download.files, download.target)
        self._emit(download, state="cancelled", speed=0, eta="", message="Cancelled — partial download deleted")

    def discard_saved(self, entry: dict) -> None:
        """Forget an interrupted download that was never restarted, deleting its partial files."""
        remove_partial_files(Path(entry.get("directory") or self.directory), entry.get("files") or [], entry["target"])
        with self._lock:
            self.pending = [e for e in self.pending if e is not entry]
        self._save()

    def shutdown(self) -> None:
        """Stop every download cleanly (aria2 writes its resume data) but keep them for the next launch."""
        with self._lock:
            active = list(self.downloads)
        for download in active:
            download._stop.set()
            process = download._process
            if process is not None and process.poll() is None:
                process.terminate()
        for download in active:
            if download._thread is not None:
                download._thread.join(timeout=5)
            process = download._process
            if process is not None and process.poll() is None:
                process.kill()

    cancel_all = shutdown

    def _save(self) -> None:
        if not self.state_file:
            return
        with self._lock:
            keep = list(self.pending) + [
                {"target": d.target, "name": d.name, "directory": str(d.directory or self.directory),
                 "files": d.files}
                for d in self.downloads if d.state not in ("finished", "failed") and not d.discard
            ]
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(keep, indent=1), encoding="utf-8")
        except OSError:
            log.warning("could not write %s", self.state_file)

    # ------------------------------------------------------------ internals

    def _emit(self, download: Download, **changes) -> None:
        for key, value in changes.items():
            setattr(download, key, value)
        if "state" in changes:
            self._save()
        self.on_update(download)

    def _guard(self, run: Callable[[Download], None], download: Download) -> None:
        try:
            run(download)
        except Exception as error:  # a failed download must never take the app down
            log.exception("download failed")
            self._emit(download, state="failed", message=str(error))

    def _run_aria2(self, download: Download) -> None:
        self._emit(download, state="downloading", message="Starting…")
        command = [
            "aria2c", "--dir", str(download.directory or self.directory), "--seed-time=0",
            "--bt-save-metadata=true", "--bt-load-saved-metadata=true", "--follow-torrent=true",
            "--continue=true", "--file-allocation=none", "--enable-color=false",
            "--summary-interval=1", "--console-log-level=warn", "--allow-overwrite=false",
            "--auto-file-renaming=true", "--", download.target,
        ]
        process = download._process = subprocess.Popen(
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
            match = _SUMMARY.search(line)
            if match:
                done, total = parse_bytes(match.group(1)), parse_bytes(match.group(2))
                self._emit(
                    download, size_done=done, size_total=total, progress=done / total if total else 0.0,
                    peers=int(match.group(3)), speed=parse_bytes(match.group(5)), eta=match.group(6) or "",
                    message="",
                )
            elif line.startswith("FILE:"):
                path = re.sub(r"\s*\(\d+more\)$", "", line[5:].strip())
                top = [part for part in Path(path).parts if part not in (".", "/")]
                if top and not path.startswith("[MEMORY]") and top[0] not in download.files:
                    download.files.append(top[0])
                    self._save()
            elif _is_noise(line):
                continue
            else:
                tail = (tail + [line])[-8:]
        code = process.wait()
        if download._stop.is_set():
            if not download.discard:  # a user cancel is reported by _cancel_and_delete once the files are gone
                self._emit(download, state="cancelled", speed=0, message="Paused — resumes next time")
            download._stop.set()
        elif code == 0:
            self._emit(download, state="finished", progress=1.0, speed=0, eta="", message=f"Saved to {download.directory}")
            download._stop.set()  # releases the watcher thread
        else:
            log.error("aria2c exited with status %s: %s", code, " | ".join(tail))
            reason = _ARIA2_EXIT.get(code, f"aria2c exited with status {code}")
            detail = next((t for t in reversed(tail) if re.search(r"ERR|error|exception|fail", t, re.I)), "")
            raise DownloadError(f"{reason} ({detail[:100]})" if detail else reason)

    def _run_libtorrent(self, download: Download) -> None:
        import libtorrent as lt

        session = lt.session({"listen_interfaces": "0.0.0.0:6881"})
        params = self._torrent_params(lt, download.target)
        params.save_path = str(download.directory or self.directory)
        handle = session.add_torrent(params)
        self._emit(download, state="downloading", message="Fetching metadata…")
        while not download._stop.is_set():
            status = handle.status()
            name = status.name or download.name
            if status.name and status.name not in download.files:
                download.files.append(status.name)
            remaining = status.total_wanted - status.total_wanted_done
            self._emit(
                download, name=name, progress=status.progress, speed=int(status.download_rate),
                size_done=status.total_wanted_done, size_total=status.total_wanted, peers=status.num_peers,
                eta=_format_eta(remaining / status.download_rate) if status.download_rate > 0 else "", message="",
            )
            if status.is_seeding or status.state == lt.torrent_status.finished:
                self._emit(download, state="finished", progress=1.0, speed=0, eta="",
                           message=f"Saved to {download.directory or self.directory}")
                break
            time.sleep(1)
        else:
            if not download.discard:
                self._emit(download, state="cancelled", speed=0, message="Paused — resumes next time")
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
