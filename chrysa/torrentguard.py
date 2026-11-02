"""Decide which files inside a torrent may be downloaded: video, subtitles and artwork only.

A torrent's file list is fixed by its metadata. Anything that is not a video or a harmless companion file
(an ``.exe``, a script, an archive, a disk image...) is never fetched, and a torrent with no video at all is refused.
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


def allowed(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if ".." in parts or path.startswith("/"):
        return False
    return PurePosixPath(path).suffix.lower() in VIDEO_EXT | HARMLESS_EXT


def wanted_indices(paths: list[str]) -> list[int]:
    """Zero-based indices of the files to download. Raises :class:`UnsafeTorrent` if there is no video in it."""
    keep = [i for i, path in enumerate(paths) if allowed(path)]
    if not any(PurePosixPath(paths[i]).suffix.lower() in VIDEO_EXT for i in keep):
        raise UnsafeTorrent("This torrent contains no video files, so it was not downloaded.")
    return keep
