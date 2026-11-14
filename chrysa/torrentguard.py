"""Decide which files inside a torrent may be downloaded: video, subtitles and artwork only.

A torrent's file list is fixed by its metadata, so it is checked before a single payload byte is fetched:

* a torrent that ships a program or script (``setup.exe``, ``crack.exe``, ``run.sh``, ``movie.mp4.exe``...) next
  to its video is refused outright; media releases never need one, and its presence marks the torrent as bait;
* anything else that is not a video or a harmless companion file (archives, disk images...) is never fetched;
* a torrent with no video at all is refused.

What the downloaded files really are is checked afterwards by :mod:`chrysa.validate`; extensions prove nothing.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from .releases import HARMLESS_EXT, VIDEO_EXT


class UnsafeTorrent(Exception):
    pass


def bdecode(data: bytes, pos: int = 0):
    """Minimal bencode reader; returns ``(value, next_position)``."""
    head = data[pos:pos + 1]
    if head == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    if head in (b"l", b"d"):
        pos += 1
        items = []
        while data[pos:pos + 1] != b"e":
            value, pos = bdecode(data, pos)
            items.append(value)
        if head == b"l":
            return items, pos + 1
        return dict(zip(items[::2], items[1::2])), pos + 1
    colon = data.index(b":", pos)
    length = int(data[pos:colon])
    return data[colon + 1:colon + 1 + length], colon + 1 + length


def file_paths(torrent: bytes) -> list[str]:
    """Relative paths of every file in a ``.torrent`` file, in torrent order."""
    info = bdecode(torrent)[0][b"info"]
    name = info[b"name"].decode("utf-8", "replace")
    if b"files" not in info:
        return [name]
    return [
        "/".join([name] + [part.decode("utf-8", "replace") for part in entry[b"path"]])
        for entry in info[b"files"]
    ]


# Executables and scripts on Windows, Linux, macOS and Android.
EXECUTABLE_EXT = {
    ".exe", ".dll", ".scr", ".bat", ".cmd", ".com", ".pif", ".msi", ".msp", ".lnk", ".hta", ".cpl", ".ps1", ".psm1",
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".jar", ".py", ".pyw", ".pl", ".rb", ".sh", ".bash", ".run", ".bin",
    ".appimage", ".deb", ".rpm", ".desktop", ".apk", ".app", ".dmg", ".pkg", ".command", ".reg", ".inf",
}


def executables(paths: list[str]) -> list[str]:
    """The paths that are programs or scripts by their final extension."""
    return [path for path in paths if PurePosixPath(path).suffix.lower() in EXECUTABLE_EXT]


def allowed(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if ".." in parts or path.startswith("/"):
        return False
    return PurePosixPath(path).suffix.lower() in VIDEO_EXT | HARMLESS_EXT


def wanted_indices(paths: list[str]) -> list[int]:
    """Zero-based indices of the files to download. Raises :class:`UnsafeTorrent` if there is no video in it."""
    bait = executables(paths)
    if bait:
        names = ", ".join(PurePosixPath(p).name for p in bait[:3])
        raise UnsafeTorrent(f"This torrent contains programs ({names}), so it was rejected. "
                            "Real video releases never include them.")
    keep = [i for i, path in enumerate(paths) if allowed(path)]
    if not any(PurePosixPath(paths[i]).suffix.lower() in VIDEO_EXT for i in keep):
        raise UnsafeTorrent("This torrent contains no video files, so it was not downloaded.")
    return keep
