"""Check a finished download before it may enter the Library.

Nothing here trusts a file name. Each step adds evidence:

1. **Inventory** — files whose type Chrysa never wants (scripts, executables, archives, aria2 leftovers) are deleted,
   and any file whose *bytes* start like an executable rejects the whole download, whatever its extension says.
2. **Container** — every video must start with the signature of a real video container (Matroska, MP4, AVI,
   MPEG-TS...). ``movie.mp4`` that is really a ZIP or a Windows program fails here.
3. **Streams** — ``ffprobe`` must find a video stream, a duration and a frame size; audio is reported.
4. **Integrity** — ``ffmpeg`` decodes a few seconds at the start and at the end (a truncated or corrupt file fails).
5. **Malware scan** — ClamAV, when installed. When it is not, the report says *scan unavailable*, never "safe".

The verdict is ``verified`` (all checks passed), ``unverified`` (signature fine but ffprobe is missing, so streams
could not be inspected) or ``rejected``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .releases import HARMLESS_EXT, VIDEO_EXT

log = logging.getLogger("chrysa.validate")

PASS, FAIL, WARN, SKIP = "pass", "fail", "warn", "skip"
VERIFIED, UNVERIFIED, REJECTED = "verified", "unverified", "rejected"
NO_THREAT, SCAN_UNAVAILABLE, THREAT, SCAN_FAILED = (
    "No threat detected", "Malware scan unavailable", "Threat detected", "Malware scan failed")

MIN_SECONDS = 60  # a "movie" shorter than this is a sample or a fake


@dataclass
class Check:
    state: str  # PASS / FAIL / WARN / SKIP
    label: str
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    verdict: str = REJECTED
    reason: str = ""  # why it was rejected
    scan: str = SCAN_UNAVAILABLE
    videos: list[Path] = field(default_factory=list)
    media: dict = field(default_factory=dict)  # main video: duration, width, height, video_codec, audio_codec, format
    phase: str = "checking"  # checking | scanning | done

    def add(self, state: str, label: str, detail: str = "") -> Check:
        check = Check(state, label, detail)
        self.checks.append(check)
        return check

    def fail(self, label: str, detail: str) -> Report:
        self.add(FAIL, label, detail)
        self.verdict, self.reason = REJECTED, detail
        return self


# --------------------------------------------------------------------------- signatures

VIDEO_KINDS = {"matroska", "mp4", "avi", "mpegts", "mpegps", "flv", "asf", "ogg", "realmedia"}


def sniff(path: Path) -> str:
    """What the first bytes of *path* say it is (``"matroska"``, ``"mp4"``, ``"executable"``, ``"archive"``...)."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(1024)
    except OSError:
        return "unreadable"
    if not head:
        return "empty"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "matroska"  # also WebM
    if head[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide"):
        return "mp4"  # also MOV / M4V / 3GP
    if head[:4] == b"RIFF" and head[8:12] in (b"AVI ", b"AVIX"):
        return "avi"
    if head[0] == 0x47 and len(head) > 376 and head[188] == 0x47 and head[376] == 0x47:
        return "mpegts"
    if len(head) > 196 and head[4] == 0x47 and head[196] == 0x47:
        return "mpegts"  # M2TS: 192-byte packets with a 4-byte timestamp
    if head[:4] == b"\x00\x00\x01\xba":
        return "mpegps"  # MPEG-1/2 program stream (.mpg, .vob)
    if head[:3] == b"FLV":
        return "flv"
    if head[:16] == bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c"):
        return "asf"  # WMV
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b".RMF":
        return "realmedia"
    if head[:2] == b"MZ" or head[:4] == b"\x7fELF" or head[:4] in (b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
        return "executable"
    if head[:2] == b"#!":
        return "script"
    if head[:4] == b"PK\x03\x04" or head[:4] == b"Rar!" or head[:6] == b"7z\xbc\xaf'\x1c" or head[:3] == b"\x1f\x8b\x08":
        return "archive"
    if head[:4] == b"%PDF":
        return "document"
    if head[:5].lower() in (b"<!doc", b"<html") or head[:5] == b"<?xml":
        return "html"
    return _file_command(path)


def _file_command(path: Path) -> str:
    """Second opinion from ``file`` for formats without a fixed magic number."""
    tool = shutil.which("file")
    if not tool:
        return "unknown"
    try:
        mime = subprocess.run([tool, "-b", "--mime-type", "--", str(path)], capture_output=True, text=True,
                              timeout=15, check=False, stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if mime.startswith("video/"):
        return "video"
    if mime in ("application/x-executable", "application/x-dosexec", "application/x-sharedlib",
                "application/x-pie-executable", "application/x-mach-binary", "application/vnd.microsoft.portable-executable"):
        return "executable"
    if mime in ("text/x-shellscript", "text/x-python", "application/javascript", "text/javascript", "text/x-msdos-batch"):
        return "script"
    if mime.startswith("text/"):
        return "text"
    return "unknown"


# --------------------------------------------------------------------------- tools


def _tool(name: str) -> str | None:
    return shutil.which(name)


def probe(path: Path, timeout: float = 60.0) -> dict | None:
    """ffprobe's view of *path*: ``{"format", "duration", "streams": [...]}``; None when ffprobe is missing."""
    ffprobe = _tool("ffprobe")
    if not ffprobe:
        return None
    try:
        done = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL,
        )
        data = json.loads(done.stdout or b"{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"error": "ffprobe could not run"}
    if done.returncode != 0 or not data.get("format"):
        message = done.stderr.decode("utf-8", "replace").replace(f"{path}: ", "").strip().splitlines()
        return {"error": message[-1][:160] if message else f"ffprobe exited with {done.returncode}"}
    return data


def decode_ok(path: Path, start: float, seconds: float = 4.0, timeout: float = 90.0) -> tuple[bool, str]:
    """Decode *seconds* of video from *start*; returns ``(ok, problem)``. True when ffmpeg is missing.

    Zero decoded frames is a failure too: seeking past the real end of a truncated file decodes nothing, silently.
    """
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:
        return True, ""
    try:
        done = subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", *(["-ss", f"{start:.2f}"] if start > 0 else []), "-i", str(path),
             "-t", f"{seconds}",
             "-map", "0:v:0", "-f", "null", "-progress", "pipe:1", "-"],
            capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return False, "decoding timed out"
    except (OSError, subprocess.SubprocessError) as error:
        return True, str(error)
    errors = [line for line in done.stderr.decode("utf-8", "replace").replace(f"{path}: ", "").splitlines() if line.strip()]
    if done.returncode != 0:
        return False, (errors[-1] if errors else f"ffmpeg exited with {done.returncode}")[:160]
    frames = re.findall(rb"^frame=(\d+)", done.stdout, re.M)
    if not frames or int(frames[-1]) == 0:
        return False, f"no picture could be decoded at {_clock(start)}"
    serious = [e for e in errors if re.search(r"(?i)invalid data|corrupt|error while decoding|moov atom not found", e)]
    return len(serious) < 5, (serious[0][:160] if serious else "")


def scan(paths: list[Path], timeout: float = 1800.0) -> tuple[str, str]:
    """ClamAV over *paths*: ``(NO_THREAT | THREAT | SCAN_FAILED | SCAN_UNAVAILABLE, detail)``."""
    daemon, standalone = _tool("clamdscan"), _tool("clamscan")
    if not (daemon or standalone):
        return SCAN_UNAVAILABLE, "ClamAV is not installed"
    commands = []
    if daemon:
        commands.append([daemon, "--no-summary", "--fdpass", *map(str, paths)])
    if standalone:
        commands.append([standalone, "--no-summary", *map(str, paths)])
    last = ""
    for command in commands:  # clamdscan needs a running daemon; fall back to clamscan if it is not
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False,
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            last = "scan timed out"
            continue
        except OSError as error:
            last = str(error)
            continue
        if done.returncode == 0:
            return NO_THREAT, Path(command[0]).name
        if done.returncode == 1:
            found = [line for line in done.stdout.splitlines() if line.endswith("FOUND")]
            return THREAT, (found[0] if found else "ClamAV reported an infection")[:200]
        last = (done.stderr or done.stdout).strip().splitlines()[-1:] or [f"exit {done.returncode}"]
        last = last[0][:160]
    return SCAN_FAILED, last


# --------------------------------------------------------------------------- the whole check


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def payload_files(root: Path, names: list[str]) -> list[Path]:
    """Every regular file under the top-level *names* in *root*, never following links out of it."""
    files = []
    for name in names:
        top = root / name
        if not _inside(root, top) or top.resolve() == root.resolve():
            continue
        if top.is_symlink():
            files.append(top)
        elif top.is_file():
            files.append(top)
        elif top.is_dir():
            for dirpath, _dirs, filenames in os.walk(top, followlinks=False):
                files += [Path(dirpath) / f for f in filenames]
    return files


def validate(root: Path, names: list[str], expect_video: bool = True, malware_scan: bool = True,
             should_stop: Callable[[], bool] | None = None,
             step: Callable[[Report], None] | None = None, *, min_seconds: float = MIN_SECONDS,
             extra_ext: frozenset | set = frozenset()) -> Report:
    """Run every check over the download *names* inside *root*. *step* is told after each check (for the UI).

    A pasted link is something the user chose, not a torrent that may be a fake: it passes ``min_seconds=0`` (a short
    clip is fine) and ``extra_ext`` (audio extensions to keep). Every other check still applies."""
    report = Report()
    stop = should_stop or (lambda: False)
    tell = step or (lambda r: None)

    # 1. inventory
    files = payload_files(root, names)
    removed = []
    for path in files:
        if path.is_symlink():
            path.unlink(missing_ok=True)
            removed.append(path.name)
            continue
        suffix = path.suffix.lower()
        if suffix not in VIDEO_EXT | HARMLESS_EXT | set(extra_ext):
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                return report.fail("Unexpected files", f"could not remove {path.name}")
    files = [p for p in files if p.exists() and not p.is_symlink()]
    for path in files:
        kind = sniff(path)
        if kind in ("executable", "script"):
            return report.fail("File contents", f"{path.name} is really a program ({kind}), not {path.suffix or 'media'}")
    if removed:
        report.add(WARN, "Unwanted files removed", ", ".join(removed[:4]) + ("…" if len(removed) > 4 else ""))
    videos = sorted((p for p in files if p.suffix.lower() in VIDEO_EXT), key=lambda p: p.stat().st_size, reverse=True)
    report.videos = videos
    if expect_video and not videos:
        return report.fail("Video present", "the download contains no video file")
    tell(report)

    # 2. container signature of every video
    for path in videos:
        kind = sniff(path)
        if kind not in VIDEO_KINDS and kind != "video":
            return report.fail("Valid media container", f"{path.name} is not a video container (it looks like: {kind})")
    report.add(PASS, "Valid media container", ", ".join(sorted({sniff(p) for p in videos[:3]})))
    tell(report)
    if stop():
        return report.fail("Cancelled", "validation was stopped")

    # 3. streams of the main video (and a quick look at the others)
    main = videos[0] if videos else None
    info = probe(main) if main else None
    if main is None:
        report.verdict = UNVERIFIED
    elif info is None:
        report.add(SKIP, "Video stream", "ffprobe is not installed, so streams could not be inspected")
        report.verdict = UNVERIFIED
    else:
        if "error" in info:
            return report.fail("Media parses", f"{main.name}: {info['error']}")
        streams = info.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"
                      and not (s.get("disposition") or {}).get("attached_pic")), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if video is None:
            return report.fail("Video stream", f"{main.name} has no video stream")
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        report.add(PASS, "Video stream", f"{video.get('codec_name', '?').upper()} {width}×{height}" if width else
                   video.get("codec_name", "?").upper())
        if audio:
            channels = audio.get("channels")
            report.add(PASS, "Audio stream", f"{audio.get('codec_name', '?').upper()}" + (f" {channels} ch" if channels else ""))
        else:
            report.add(WARN, "Audio stream", "no audio track found")
        try:
            duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
        except ValueError:
            duration = 0.0
        if duration <= 0:
            return report.fail("Duration", f"{main.name} has no readable duration")
        if duration < min_seconds:
            return report.fail("Duration", f"only {duration:.0f} s long: a sample or a fake, not the full video")
        report.add(PASS, "Duration", _clock(duration))
        if width and height:
            report.add(PASS, "Resolution", f"{width}×{height}")
        report.media = {"duration": duration, "width": width, "height": height,
                        "video_codec": video.get("codec_name", ""), "audio_codec": (audio or {}).get("codec_name", ""),
                        "format": info.get("format", {}).get("format_name", "")}
        for other in videos[1:4]:  # extra episodes of a pack: they must parse too
            extra = probe(other)
            if extra and "error" in extra:
                return report.fail("Media parses", f"{other.name}: {extra['error']}")
        tell(report)
        if stop():
            return report.fail("Cancelled", "validation was stopped")

        # 4. integrity: decode the start and the end
        ok_start, why_start = decode_ok(main, 0.0)
        ok_end, why_end = decode_ok(main, duration - 6.0)
        if not (ok_start and ok_end):
            return report.fail("File integrity", f"the video does not decode ({why_start or why_end}); "
                                                 "it is corrupt or truncated")
        report.add(PASS, "File integrity", "start and end decode cleanly")
        report.verdict = VERIFIED
        tell(report)

    # 5. optional malware scan
    if malware_scan and files:
        if stop():
            return report.fail("Cancelled", "validation was stopped")
        if _tool("clamdscan") or _tool("clamscan"):
            report.phase = "scanning"
            tell(report)
        report.scan, detail = scan(files)
        if report.scan == THREAT:
            return report.fail("Malware scan", detail)
        report.add({NO_THREAT: PASS, SCAN_UNAVAILABLE: SKIP}.get(report.scan, WARN), report.scan,
                   "" if report.scan == NO_THREAT else detail)
    report.phase = "done"
    tell(report)
    return report


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
