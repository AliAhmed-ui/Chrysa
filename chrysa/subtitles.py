"""Find subtitle files that belong to a video and give them readable names.

Releases ship subtitles in two ways: next to the video with the same file name
(``Heat.1995.srt``), or in a ``Subs`` folder with names like ``SDH.eng.HI.srt``
or ``Latin American.spa.srt``. libVLC only auto-loads the former and calls every
external track "Track 1", so Chrysa discovers them itself and labels them.
"""

from __future__ import annotations

import os
import re
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from babelfish import Language

from .config import SUBTITLE_EXTENSIONS, VIDEO_EXTENSIONS
from .models import Media
from .probe import has_embedded_subtitles

_SUBTITLE_DIRS = frozenset({"subs", "sub", "subtitles", "subtitle"})
_FLAGS = {"sdh": "SDH", "hi": "HI", "cc": "CC", "forced": "Forced"}
_NOISE = frozenset({"regular", "full", "default", "normal", "subs", "sub", "subtitles"})


@dataclass(frozen=True, slots=True)
class ExternalSubtitle:
    path: str
    label: str
    sidecar: bool  # named after the video itself, i.e. the release's default subtitle


def find_external_subtitles(video_path: str) -> list[ExternalSubtitle]:
    """Subtitle files for *video_path*, the release's default one first."""
    directory, filename = os.path.split(video_path)
    stem = os.path.splitext(filename)[0]
    base = stem.casefold()
    found: dict[str, ExternalSubtitle] = {}

    def consider(path: str, name: str, exclusive: bool) -> None:
        sub_stem, ext = os.path.splitext(name)
        if ext.lower() not in SUBTITLE_EXTENSIONS or path in found:
            return
        rest = sub_stem[len(stem):]
        # "Show S01E1" must not claim the subtitle of "Show S01E10": a longer name only
        # belongs to this video when the extra part starts after a separator.
        if sub_stem.casefold().startswith(base) and (not rest or not rest[0].isalnum()):
            found[path] = ExternalSubtitle(path, describe(rest) or "Subtitles", sidecar=not rest.strip(" ._-"))
        elif exclusive:
            found[path] = ExternalSubtitle(path, describe(sub_stem) or sub_stem, sidecar=False)

    names = _listdir(directory)
    # Subtitles in a shared Subs/ folder belong to this video only if it is alone here.
    alone = sum(1 for n in names if os.path.splitext(n)[1].lower() in VIDEO_EXTENSIONS) <= 1
    for name in names:
        path = os.path.join(directory, name)
        if name.casefold() in _SUBTITLE_DIRS and os.path.isdir(path):
            for sub in _listdir(path):
                sub_path = os.path.join(path, sub)
                if os.path.isdir(sub_path):
                    if sub.casefold() == base:  # Subs/<episode name>/2_English.srt
                        for nested in _listdir(sub_path):
                            consider(os.path.join(sub_path, nested), nested, exclusive=True)
                else:
                    consider(sub_path, sub, exclusive=alone)
        else:
            consider(path, name, exclusive=False)

    return sorted(found.values(), key=lambda s: (not s.sidecar, s.label.casefold(), s.path))


def describe(text: str) -> str:
    """Turn the distinguishing part of a subtitle file name into a label.

    ``SDH.eng.HI`` → ``English (SDH)``; ``Latin American.spa`` → ``Spanish (Latin American)``;
    ``2_English`` → ``English``; ``Forced.eng`` → ``English (Forced)``.
    """
    tokens = [t for t in re.split(r"[\s._\-\[\]()]+", text) if t and not t.isdigit()]
    language: str | None = None
    flags: list[str] = []
    words: list[str] = []
    for token in tokens:
        low = token.casefold()
        if low in _FLAGS and (low != "hi" or len(tokens) > 1):
            flags.append(_FLAGS[low])
        elif low in _NOISE:
            continue
        elif language is None and 2 <= len(low) <= 3 and (name := _language_from_code(low)):
            language = name
        else:
            words.append(token)
    if language is None and words:
        name = _language_from_name(" ".join(words))
        if name:
            language, words = name, []
    if "SDH" in flags and "HI" in flags:
        flags.remove("HI")
    extras = ([" ".join(words)] if language and words else []) + flags
    label = language or " ".join(words)
    if not label:
        return ", ".join(flags)
    return f"{label} ({', '.join(extras)})" if extras else label


def _language_from_code(code: str) -> str | None:
    """Language name for an ISO 639 code: ``en``, ``eng`` or the bibliographic ``ger``."""
    converters = ("alpha2",) if len(code) == 2 else (None, "alpha3b")
    for converter in converters:
        try:
            language = Language(code) if converter is None else Language.fromcode(code, converter)
            return language.name
        except Exception:  # babelfish raises several unrelated error types for unknown codes
            continue
    return None


def _language_from_name(name: str) -> str | None:
    try:
        return Language.fromcode(name.title(), "name").name
    except Exception:
        return None


def _listdir(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


# ---------------------------------------------------------------- online download

# Providers that work without an account. subliminal is imported lazily: it is only
# needed when the user asks for subtitles, and it must never break offline use.
_PROVIDERS = ("podnapisi", "gestdown", "tvsubtitles")
_PROBE_HOSTS = (("www.podnapisi.net", 443), ("1.1.1.1", 443), ("8.8.8.8", 53))

LogFn = Callable[[str, str], None]  # (level, message)


@dataclass(slots=True)
class FetchSummary:
    downloaded: int = 0
    skipped: int = 0
    not_found: int = 0
    failed: int = 0
    cancelled: bool = False
    offline: bool = False


def display_name(media: Media) -> str:
    """``Friends S02E18`` for an episode, the title for anything else."""
    if media.is_episode and media.season is not None and media.episode is not None:
        return f"{media.show_title or media.title} S{media.season:02d}E{media.episode:02d}"
    return media.title


def has_subtitles(path: str) -> bool:
    """A subtitle file next to the video (or in its Subs folder), or one inside the container."""
    return bool(find_external_subtitles(path)) or has_embedded_subtitles(path)


def subtitle_target(video_path: str) -> str:
    """Where the English subtitle goes: ``Movie.mkv`` → ``Movie.en.srt`` beside it."""
    return os.path.splitext(video_path)[0] + ".en.srt"


def is_online(timeout: float = 5.0) -> bool:
    for host in _PROBE_HOSTS:  # any one answering proves a connection; a single host may be blocked
        try:
            with socket.create_connection(host, timeout=timeout):
                return True
        except OSError:
            continue
    return False


def search_online(video_path: str) -> bytes | None:
    """Best English subtitle for the file as SRT bytes, or None when nothing matched."""
    from subliminal import Video, download_best_subtitles

    video = Video.fromname(os.path.basename(video_path))
    found = download_best_subtitles({video}, {Language("eng")}, providers=list(_PROVIDERS), only_one=True)
    for subtitle in found.get(video, []):
        if subtitle.content:
            return subtitle.content
    return None


def fetch_missing_subtitles(
    videos: Iterable[Media],
    log: LogFn,
    should_stop: Callable[[], bool] = lambda: False,
    search: Callable[[str], bytes | None] = search_online,
    online: Callable[[], bool] = is_online,
) -> FetchSummary:
    """Download an English ``.srt`` beside every video that has no subtitles yet.

    Never raises: network and provider errors are logged as warnings and skipped.
    """
    summary = FetchSummary()
    log("INFO", "Starting subtitle search across library...")
    checked_online = False
    for media in videos:
        if should_stop():
            summary.cancelled = True
            break
        name = display_name(media)
        if not os.path.isfile(media.path):
            continue
        if has_subtitles(media.path):
            summary.skipped += 1
            log("SKIP", f"{name} already has subtitles.")
            continue
        if not checked_online:
            if not online():
                summary.offline = True
                log("WARN", "No internet connection - subtitle search skipped.")
                break
            checked_online = True
        log("FOUND", f"{name}: Downloading subtitles...")
        target = subtitle_target(media.path)
        try:
            content = search(media.path)
            if content is None:
                summary.not_found += 1
                log("WARN", f"No English subtitles found for {name}.")
                continue
            with open(target, "wb") as handle:
                handle.write(content)
        except Exception as error:  # provider, network and disk errors must not stop the run
            summary.failed += 1
            log("WARN", f"{name}: {type(error).__name__}: {error}")
            continue
        summary.downloaded += 1
        log("OK", f"Saved {os.path.basename(target)}")
    if summary.cancelled:
        log("INFO", "Subtitle search cancelled.")
    elif not summary.offline:
        log("DONE", f"Subtitle check complete. {summary.downloaded} new subtitles downloaded.")
    return summary
