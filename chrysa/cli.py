"""Headless commands: scan the library, list it, pre-build thumbnails.

These run without a display, which makes them handy for checking what Chrysa
makes of a folder (``./run.sh --scan --list``) and for tests.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

from . import __version__
from .config import get_paths
from .library import Library
from .models import format_duration
from .thumbnails import PRIORITY_BACKGROUND, PRIORITY_HOME, ThumbnailWorkers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chrysa", description="Chrysa — an offline media center for local movies and TV shows."
    )
    parser.add_argument("--version", action="version", version=f"Chrysa {__version__}")
    parser.add_argument(
        "--add-folder", metavar="DIR", action="append", default=[],
        help="add DIR to the library (may be repeated)",
    )
    headless = parser.add_argument_group("headless commands (no window is opened)")
    headless.add_argument("--scan", action="store_true", help="rescan the library folders and exit")
    headless.add_argument("--list", action="store_true", help="print the library and exit")
    headless.add_argument("--thumbnails", action="store_true", help="generate all missing thumbnails and exit")
    return parser


def is_headless(args: argparse.Namespace) -> bool:
    return args.scan or args.list or args.thumbnails


def run(args: argparse.Namespace) -> int:
    library = Library.open(get_paths())
    library.ensure_default_folder()
    for folder in args.add_folder:
        _added, message = library.add_folder(folder)
        print(f"{folder}: {message}")

    if args.scan or args.add_folder:
        scan(library)
    if args.list:
        print_library(library)
    if args.thumbnails:
        build_thumbnails(library)
    return 0


def scan(library: Library) -> None:
    folders = library.folders()
    if not folders:
        print("No library folders yet. Add one with --add-folder DIR.")
        return
    print("Library folders:")
    for folder in folders:
        print(f"  {folder.path}{'' if folder.available else '   (not available, skipped)'}")

    interactive = sys.stderr.isatty()

    def progress(message: str) -> None:
        if interactive:
            print(f"\r\033[K{message[:100]}", end="", file=sys.stderr, flush=True)

    summary = library.rescan(progress=progress, retry_failed=True)
    if interactive:
        print("\r\033[K", end="", file=sys.stderr, flush=True)
    print(f"Scan finished in {summary.seconds:.1f}s: {summary.describe()}")
    print(
        f"  {summary.added} added, {summary.removed} removed, {summary.moved} moved, "
        f"{summary.probed} probed with ffprobe"
    )
    for path, reason in summary.ignored or []:
        print(f"  ignored ({reason}): {path}")
    for root in summary.missing_roots or []:
        print(f"  folder not available, kept as is: {root}")


def print_library(library: Library) -> None:
    shows, movies, others = library.shows(), library.movies(), library.others()
    watching = library.continue_watching()

    if watching:
        print(f"\nCONTINUE WATCHING ({len(watching)})")
        for media in watching:
            print(f"  {media.full_title}  —  {media.fraction:.0%} watched")

    print(f"\nTV SHOWS ({len(shows)})")
    for show in shows:
        year = f" ({show.year})" if show.year else ""
        print(f"  {show.title}{year}  —  {show.summary}")
        for season in library.seasons(show.id):
            episodes = library.episodes(show.id, season)
            runtime = sum(e.duration for e in episodes)
            name = f"Season {season}" if season > 0 else ("Specials" if season == 0 else "Other")
            first, last = episodes[0], episodes[-1]
            print(
                f"      {name:<10} {len(episodes):>3} episodes  {format_duration(runtime):>8}   "
                f"{first.episode_code} “{first.title}” … {last.episode_code} “{last.title}”"
            )

    print(f"\nMOVIES ({len(movies)})")
    for movie in movies:
        year = f" ({movie.year})" if movie.year else ""
        print(f"  {movie.title + year:<40} {format_duration(movie.duration):>7}")

    print(f"\nOTHER VIDEOS ({len(others)})")
    for other in others:
        print(f"  {other.title:<40} {format_duration(other.duration):>7}")
    print()


def build_thumbnails(library: Library) -> None:
    jobs = library.thumbnail_jobs()
    if not jobs:
        print("Thumbnails are up to date.")
        return
    total = len(jobs)
    done = threading.Semaphore(0)
    failed: list[int] = []

    def store(kind: str, target_id: int, path: str | None) -> None:
        library.set_thumbnail(target_id, path)
        if path is None:
            failed.append(target_id)
        done.release()

    workers = ThumbnailWorkers(library.paths.thumbnails, store, workers=3)
    for index, media in enumerate(jobs):
        priority = PRIORITY_HOME if index < 64 else PRIORITY_BACKGROUND
        workers.enqueue("thumb", media.id, media.path, media.duration, media.size, media.mtime, priority)
    started = time.time()
    interactive = sys.stderr.isatty()
    for count in range(1, total + 1):
        done.acquire()
        if interactive:
            print(f"\r\033[KThumbnails {count}/{total}", end="", file=sys.stderr, flush=True)
    workers.stop()
    if interactive:
        print("\r\033[K", end="", file=sys.stderr, flush=True)
    print(f"Generated {total - len(failed)} thumbnails in {time.time() - started:.0f}s ({len(failed)} failed).")
