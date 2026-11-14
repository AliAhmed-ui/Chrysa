"""A small BitTorrent download client that runs off the UI thread.

Backends, in order of preference: ``libtorrent`` (python-libtorrent bindings) and
the ``aria2c`` command. :func:`start_download` hands a magnet link or ``.torrent``
URL/path to a background thread and returns immediately.

While a download runs, a :class:`Monitor` measures what really arrives (time to first data, average and peak
speed, peers) and tells healthy, slow and stalled apart. A download that genuinely stalls with no peers may switch,
once or twice and never while another download holds that torrent, to an equivalent release it was given as an
alternative. When it finishes, an optional *pipeline* (see :mod:`chrysa.acquire`) validates the files and moves them
from the incoming folder into the library folder.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import torrentguard
from .sources import info_hash as info_hash_of

log = logging.getLogger(__name__)

DEFAULT_DIR = Path.home() / "Downloads"
INCOMING = ".chrysa-incoming"  # hidden, so the library scanner never sees unverified files
# aria2's periodic summary: [#78dfa0 1.5MiB/1.9GiB(0%) CN:3 SD:1 DL:2.1MiB ETA:12m3s]
_SUMMARY = re.compile(
    r"\[#\w+ ([\d.]+[KMGT]?i?B)/([\d.]+[KMGT]?i?B)\(\d+%\) CN:(\d+)(?: SD:(\d+))? DL:([\d.]+[KMGT]?i?B)(?: ETA:(\S+?))?\]"
)
_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}


def parse_bytes(text: str) -> int:
    """``'1.5MiB'`` -> bytes."""
    match = re.fullmatch(r"([\d.]+)([KMGT]?)i?B", text)
    return int(float(match.group(1)) * _UNITS[match.group(2)]) if match else 0


def incoming_dir(final_dir: Path) -> Path:
    """Where downloads for *final_dir* wait until they are validated."""
    return Path(final_dir) / INCOMING


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


def top_level_names(paths: list[str]) -> list[str]:
    """First path component of each torrent file path, in order, without duplicates."""
    names = []
    for path in paths:
        parts = PurePosixPath(path).parts
        if parts and parts[0] not in names and parts[0] not in ("..", "/"):
            names.append(parts[0])
    return names


class DownloadError(Exception):
    pass


class _Cancelled(Exception):
    """The user stopped a download while its file list was still being checked."""


class _Stalled(Exception):
    """The torrent stopped delivering and an alternative release is available."""


def is_valid_target(target: str) -> bool:
    return target.startswith(("magnet:?", "http://", "https://")) or Path(target).suffix == ".torrent"


class Monitor:
    """Measures one download's real performance and classifies it.

    ``connecting`` (no data yet) → ``healthy`` (data arriving) / ``slow`` (arriving, but under :attr:`SLOW_BPS` over
    the last minute) → ``stalled`` (nothing new for :attr:`STALL_AFTER` seconds, or no first byte within
    :attr:`FIRST_DATA_TIMEOUT`). Brief dips never count as a stall.
    """

    STALL_AFTER = 240.0
    FIRST_DATA_TIMEOUT = 420.0
    SLOW_BPS = 64 * 1024
    WINDOW = 60.0

    def __init__(self, now: float | None = None) -> None:
        self.started = time.monotonic() if now is None else now
        self.base: int | None = None
        self.last_done = 0
        self.first_data: float | None = None  # when the first new byte was seen
        self._since = self.started  # the sample before it: the transfer began after this moment
        self._last_sample = self.started
        self.last_progress = self.started
        self.peak = 0
        self._peers = [0, 0]  # sum, samples
        self._window: deque[tuple[float, int]] = deque()

    def sample(self, done: int, speed: int, peers: int, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        if self.base is None:
            self.base = self.last_done = done
        if done > self.last_done:
            if self.first_data is None:
                self.first_data, self._since = now, self._last_sample
            self.last_done, self.last_progress = done, now
        self._last_sample = now
        self.peak = max(self.peak, speed)
        self._peers[0] += peers
        self._peers[1] += 1
        self._window.append((now, done))
        while self._window and now - self._window[0][0] > self.WINDOW:
            self._window.popleft()
        return self.health(now)

    def health(self, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        if self.first_data is None:
            return "stalled" if now - self.started > self.FIRST_DATA_TIMEOUT else "connecting"
        if now - self.last_progress > self.STALL_AFTER:
            return "stalled"
        if len(self._window) > 1 and self._window[-1][0] - self._window[0][0] >= 20 and self.recent_rate < self.SLOW_BPS:
            return "slow"
        return "healthy"

    def stalled_for(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return now - (self.last_progress if self.first_data is not None else self.started)

    @property
    def recent_rate(self) -> float:
        if len(self._window) < 2:
            return 0.0
        (t0, d0), (t1, d1) = self._window[0], self._window[-1]
        return (d1 - d0) / (t1 - t0) if t1 > t0 else 0.0

    def average(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        if self.first_data is None or self.base is None:
            return 0.0
        return (self.last_done - self.base) / max(1.0, self.last_progress - self._since)

    @property
    def ttfd(self) -> float | None:
        return None if self.first_data is None else self.first_data - self.started

    @property
    def average_peers(self) -> float:
        return self._peers[0] / self._peers[1] if self._peers[1] else 0.0


@dataclass
class Download:
    target: str
    name: str = ""
    # queued | downloading | finished | validating | verified | rejected | failed | cancelled
    state: str = "queued"
    progress: float = 0.0  # 0..1
    message: str = ""
    directory: Path | None = None  # where the payload is written (the incoming folder when quarantined)
    final_dir: Path | None = None  # where verified files go; None = they stay in ``directory``
    speed: int = 0  # bytes per second
    eta: str = ""
    size_done: int = 0
    size_total: int = 0
    peers: int = 0
    seeds: int | None = None
    avg_speed: float = 0.0
    peak_speed: int = 0
    health: str = ""  # connecting | healthy | slow | stalled
    discard: bool = False  # cancelled by the user: forget it instead of resuming next launch
    files: list[str] = field(default_factory=list)  # top-level names it created in ``directory``
    alternatives: list[str] = field(default_factory=list)  # equivalent releases to fall back to
    switches: int = 0
    meta: dict = field(default_factory=dict)  # what the search knew (source, size, seeders...), for the history
    checks: list = field(default_factory=list)  # validate.Check rows while validating
    admitted: list[str] = field(default_factory=list)  # final paths of verified files
    monitor: Monitor | None = field(default=None, repr=False)
    _process: subprocess.Popen | None = field(default=None, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _switch: bool = field(default=False, repr=False)

    @property
    def info_hash(self) -> str:
        return info_hash_of(self.target)

    @property
    def active(self) -> bool:
        return self.state in ("queued", "downloading", "finished", "validating") and not self._stop.is_set()

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
    """Owns the download threads. ``on_update`` is called from worker threads.

    *pipeline* (optional) gets ``started(download)``, ``ended(download, status)`` and
    ``finalize(download, emit)`` calls; see :class:`chrysa.acquire.Pipeline`.
    """

    MAX_SWITCHES = 2

    def __init__(
        self, directory: Path = DEFAULT_DIR, on_update: Callable[[Download], None] | None = None,
        state_file: Path | None = None, pipeline=None,
    ) -> None:
        self.directory = Path(directory)
        self.state_file = Path(state_file) if state_file else None
        self.on_update = on_update or (lambda _download: None)
        self.pipeline = pipeline
        self.downloads: list[Download] = []
        self._lock = threading.Lock()
        self.pending: list[dict] = self._load()  # left over from an earlier run, not yet restarted

    def find_active(self, target: str) -> Download | None:
        """The running download of the same torrent (or the same link), if any."""
        key = info_hash_of(target) or target
        with self._lock:
            return next((d for d in self.downloads if d.active and (d.info_hash or d.target) == key), None)

    def start(
        self, target: str, name: str = "", directory: Path | None = None, files: list[str] | None = None, *,
        final_dir: Path | None = None, alternatives: list[str] | None = None, meta: dict | None = None,
    ) -> Download:
        target = target.strip()
        if not is_valid_target(target):
            raise DownloadError("Not a magnet link or .torrent file.")
        running = self.find_active(target)
        if running is not None:
            raise DownloadError(f"Already downloading this torrent (“{running.name or 'download'}”).")
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
        download = Download(target, name=name, directory=folder, files=list(files or []),
                            final_dir=Path(final_dir) if final_dir else None,
                            alternatives=[a for a in alternatives or [] if a != target], meta=dict(meta or {}))
        with self._lock:
            self.downloads.append(download)
            self.pending = [e for e in self.pending if e["target"] != target]
        self._save()
        if self.pipeline is not None:
            self.pipeline.started(download)
        run = self._run_libtorrent if backend == "libtorrent" else self._run_aria2
        download._thread = threading.Thread(target=self._guard, args=(run, download), name="chrysa-download", daemon=True)
        download._thread.start()
        return download

    def saved(self) -> list[dict]:
        """Downloads left unfinished by an earlier run: ``{"target", "name", "directory", "files", ...}``."""
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
        if self.pipeline is not None:
            self.pipeline.ended(download, "cancelled")
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
                 "files": d.files, "final_dir": str(d.final_dir) if d.final_dir else None,
                 "alternatives": d.alternatives, "meta": {k: v for k, v in d.meta.items() if k != "history_id"}}
                for d in self.downloads
                if d.state not in ("finished", "failed", "verified", "rejected") and not d.discard
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
            while True:
                try:
                    download.monitor = Monitor()
                    run(download)
                    break
                except _Stalled:
                    self._switch_release(download)
            if download.state == "finished" and not download._stop.is_set() and self.pipeline is not None:
                self.pipeline.finalize(download, self._emit)
            elif download.state == "finished" and self.pipeline is not None:
                self.pipeline.ended(download, "completed")
        except Exception as error:  # a failed download must never take the app down
            log.exception("download failed")
            if self.pipeline is not None:
                self.pipeline.ended(download, "failed", detail=str(error))
            self._emit(download, state="failed", message=str(error))
        finally:
            if download.state not in ("downloading", "queued"):
                download._stop.set()  # releases the aria2 watcher thread

    def _can_switch(self, download: Download) -> bool:
        """Only a genuine stall, early on, with somewhere better to go, and not too often."""
        monitor = download.monitor
        if monitor is None or monitor.health() != "stalled" or download.switches >= self.MAX_SWITCHES:
            return False
        if download.progress >= 0.5:
            return False  # half done: waiting for peers beats starting over
        if download.peers > 0 and monitor.stalled_for() < 2 * Monitor.STALL_AFTER:
            return False  # peers are connected; give them longer
        return any(self.find_active(alt) is None for alt in download.alternatives)

    def _switch_release(self, download: Download) -> None:
        """Abandon the stalled torrent (deleting its partial data) for the next alternative."""
        if self.pipeline is not None:
            self.pipeline.ended(download, "stalled", detail="no data; switched to an alternative")
        remove_partial_files(download.directory or self.directory, download.files, download.target)
        while download.alternatives:
            nxt = download.alternatives.pop(0)
            if self.find_active(nxt) is None:
                break
        else:
            raise DownloadError("The torrent stalled and no alternative release is available.")
        log.info("[DOWNLOAD] %s stalled; switching to an equivalent release", download.name)
        download.target, download.files, download.switches = nxt, [], download.switches + 1
        download._switch = False
        download.meta = {**download.meta, "info_hash": info_hash_of(nxt)}
        if self.pipeline is not None:
            self.pipeline.started(download)
        self._emit(download, state="downloading", progress=0.0, size_done=0, speed=0, eta="", health="connecting",
                   message="Stalled with no usable peers — switched to an equivalent release")

    def _sample(self, download: Download, done: int, total: int, speed: int, peers: int, seeds: int | None,
                eta: str) -> None:
        """Record one progress sample and decide whether the torrent is worth abandoning."""
        monitor = download.monitor or Monitor()
        health = monitor.sample(done, speed, peers)
        message = ""
        if health == "stalled":
            message = "Stalled — waiting for peers" if peers else "Stalled — no peers are sending data"
        elif health == "connecting":
            message = "Connecting to peers…"
        self._emit(download, size_done=done, size_total=total, progress=done / total if total else 0.0, peers=peers,
                   seeds=seeds, speed=speed, eta=eta, health=health, avg_speed=monitor.average(),
                   peak_speed=monitor.peak, message=message)
        if health == "stalled" and download.alternatives and self._can_switch(download):
            download._switch = True

    def _inspect_aria2(self, download: Download, workdir: Path) -> tuple[str, list[str]]:
        """Fetch the torrent's file list first; return the target to run and the ``--select-file`` option for it."""
        target = download.target
        if target.startswith("magnet:?"):
            self._emit(download, state="downloading", message="Fetching the torrent's file list…")
            process = subprocess.Popen(
                ["aria2c", "--dir", str(workdir), "--bt-metadata-only=true", "--bt-save-metadata=true",
                 "--enable-color=false", "--console-log-level=error", "--", target],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 240
            while process.poll() is None:
                if download._stop.is_set() or time.monotonic() > deadline:
                    process.terminate()
                    process.wait()
                    if download._stop.is_set():
                        raise _Cancelled
                    if download.alternatives and download.switches < self.MAX_SWITCHES:
                        download.monitor = Monitor(time.monotonic() - Monitor.FIRST_DATA_TIMEOUT - 1)
                        raise _Stalled
                    raise DownloadError("Could not fetch the torrent's file list; no peers answered.")
                time.sleep(0.5)
            saved = next(workdir.glob("*.torrent"), None)
            if saved is None:
                raise DownloadError("Could not fetch the torrent's file list; no peers answered.")
            data, target = saved.read_bytes(), str(saved)
        elif target.startswith(("http://", "https://")):
            request = urllib.request.Request(target, headers={"User-Agent": "Chrysa"})
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()
            saved = workdir / "download.torrent"
            saved.write_bytes(data)
            target = str(saved)
        else:
            data = Path(target).expanduser().read_bytes()
        try:
            paths = torrentguard.file_paths(data)
            keep = torrentguard.wanted_indices(paths)
        except torrentguard.UnsafeTorrent as error:
            raise DownloadError(str(error)) from error
        except (ValueError, KeyError, IndexError) as error:
            raise DownloadError("This torrent's file list could not be read, so it was not downloaded.") from error
        self._note_files(download, [paths[i] for i in keep])
        self._emit(download, message=f"File list checked: {len(keep)} file(s) to fetch, nothing suspicious")
        if len(keep) == len(paths):
            return target, []
        return target, ["--select-file=" + ",".join(str(i + 1) for i in keep)]  # aria2 numbers files from 1

    def _note_files(self, download: Download, paths: list[str]) -> None:
        for name in top_level_names(paths):
            if name not in download.files:
                download.files.append(name)
        self._save()

    def _run_aria2(self, download: Download) -> None:
        workdir = Path(tempfile.mkdtemp(prefix="chrysa-torrent-"))
        try:
            target, select = self._inspect_aria2(download, workdir)
            self._run_aria2_checked(download, target, select)
        except _Cancelled:
            if not download.discard:
                self._emit(download, state="cancelled", speed=0, message="Paused — resumes next time")
            download._stop.set()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _run_aria2_checked(self, download: Download, target: str, select: list[str]) -> None:
        self._emit(download, state="downloading", message="Starting…", health="connecting")
        directory = (download.directory or self.directory).resolve()
        command = [
            "aria2c", "--dir", str(directory), "--seed-time=0",
            "--bt-save-metadata=true", "--bt-load-saved-metadata=true", "--follow-torrent=true",
            "--continue=true", "--file-allocation=none", "--enable-color=false",
            "--summary-interval=1", "--console-log-level=warn", "--allow-overwrite=false",
            "--auto-file-renaming=true", *select, "--", target,
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
                self._sample(download, parse_bytes(match.group(1)), parse_bytes(match.group(2)),
                             parse_bytes(match.group(5)), int(match.group(3)),
                             int(match.group(4)) if match.group(4) else None, match.group(6) or "")
                if download._switch and process.poll() is None:
                    process.terminate()
            elif line.startswith("FILE:"):
                path = Path(re.sub(r"\s*\(\d+more\)$", "", line[5:].strip()))
                if not str(path).startswith("[MEMORY]"):
                    try:  # aria2 prints the full path
                        relative = path.resolve().relative_to(directory) if path.is_absolute() else path
                    except ValueError:
                        continue
                    self._note_files(download, [relative.as_posix()])
            elif _is_noise(line):
                continue
            else:
                tail = (tail + [line])[-8:]
        code = process.wait()
        if download._switch and not download._stop.is_set():
            raise _Stalled
        if download._stop.is_set():
            if not download.discard:  # a user cancel is reported by _cancel_and_delete once the files are gone
                self._emit(download, state="cancelled", speed=0, message="Paused — resumes next time")
            download._stop.set()
        elif code == 0:
            self._emit(download, state="finished", progress=1.0, speed=0, eta="", message=f"Saved to {download.directory}")
            if self.pipeline is None:
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
        self._emit(download, state="downloading", message="Fetching metadata…", health="connecting")
        checked = False
        stalled = False
        while not download._stop.is_set():
            status = handle.status()
            if not checked and handle.has_metadata():
                checked = True
                info = handle.torrent_file()
                files = info.files()
                paths = [files.file_path(i) for i in range(files.num_files())]
                try:
                    keep = torrentguard.wanted_indices(paths)
                except torrentguard.UnsafeTorrent as error:
                    session.remove_torrent(handle)
                    raise DownloadError(str(error)) from error
                handle.prioritize_files([4 if i in set(keep) else 0 for i in range(len(paths))])
                self._note_files(download, [paths[i] for i in keep])
            name = status.name or download.name
            if name != download.name:
                download.name = name
            remaining = status.total_wanted - status.total_wanted_done
            self._sample(download, status.total_wanted_done, status.total_wanted, int(status.download_rate),
                         status.num_peers, status.num_seeds,
                         _format_eta(remaining / status.download_rate) if status.download_rate > 0 else "")
            if status.is_seeding or status.state == lt.torrent_status.finished:
                self._emit(download, state="finished", progress=1.0, speed=0, eta="",
                           message=f"Saved to {download.directory or self.directory}")
                break
            if download._switch:
                stalled = True
                break
            time.sleep(1)
        else:
            if not download.discard:
                self._emit(download, state="cancelled", speed=0, message="Paused — resumes next time")
        session.remove_torrent(handle)
        if stalled:
            raise _Stalled

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
