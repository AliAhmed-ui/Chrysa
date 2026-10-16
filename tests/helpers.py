"""Shared fixtures: throw-away media trees and libraries."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from chrysa import library as library_module
from chrysa import naming
from chrysa.config import Paths
from chrysa.library import Library
from chrysa.probe import MediaInfo

MB = 1024 * 1024


def make_tree(root: Path, files: dict[str, int | bytes]) -> None:
    """Create ``files`` below *root*.

    An int value makes a sparse file of that many bytes (no disk space is used,
    but the scanner sees a realistic size); bytes are written literally.
    """
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            with open(path, "wb") as handle:
                handle.truncate(content)


class TempDirCase(unittest.TestCase):
    """A test with its own scratch directory and a clean name-parsing cache."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="chrysa-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.media = self.tmp / "media"
        self.media.mkdir()
        naming.clear_cache()


class LibraryCase(TempDirCase):
    """A library on a scratch database, with ffprobe replaced by a lookup table."""

    def setUp(self) -> None:
        super().setUp()
        self.durations: dict[str, float] = {}  # file name -> seconds
        self.default_duration = 0.0
        self.probed: list[str] = []
        original = library_module.probe_many

        def fake_probe_many(paths, on_result, should_stop=None, workers=None):
            for path in paths:
                self.probed.append(path)
                seconds = self.durations.get(os.path.basename(path), self.default_duration)
                on_result(path, MediaInfo(duration=seconds, width=1920, height=1080))

        library_module.probe_many = fake_probe_many
        self.addCleanup(setattr, library_module, "probe_many", original)

        self.library = Library.open(Paths(self.tmp / "data"))
        self.addCleanup(self.library.db.close)
        self.library.add_folder(self.media)

    def scan(self, **kwargs):
        self.probed.clear()
        return self.library.rescan(**kwargs)

    def by_title(self, title: str):
        con = self.library.db.connection()
        row = con.execute("SELECT id FROM media WHERE title = ?", (title,)).fetchone()
        self.assertIsNotNone(row, f"no media titled {title!r}")
        return self.library.media(row["id"])
