"""Find subtitle files that belong to a video and give them readable names.

Releases ship subtitles in two ways: next to the video with the same file name
(``Heat.1995.srt``), or in a ``Subs`` folder with names like ``SDH.eng.HI.srt``
or ``Latin American.spa.srt``. libVLC only auto-loads the former and calls every
external track "Track 1", so Theia discovers them itself and labels them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from babelfish import Language

from .config import SUBTITLE_EXTENSIONS, VIDEO_EXTENSIONS

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
        if sub_stem.casefold().startswith(base):
            rest = sub_stem[len(stem):]
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
