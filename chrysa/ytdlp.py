"""Finds and loads the local yt-dlp checkout that powers Paste Link.

The checkout is the source of truth: it is looked up (``CHRYSA_YTDLP_DIR``, then a ``yt-dlp`` folder next to the
Chrysa folder), put first on ``sys.path`` and imported, and the import is refused if ``yt_dlp`` resolves to anywhere
else (a pip install, a conda copy). Nothing is fetched from the network and no ``yt-dlp`` executable is run.

yt-dlp's core needs only the standard library. What it finds in Chrysa's environment is used when present:
``certifi``/``requests`` (already installed for subliminal) and ``curl_cffi`` (browser impersonation).
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
from pathlib import Path

ENV_VAR = "CHRYSA_YTDLP_DIR"
_lock = threading.Lock()
_module = None


class EngineUnavailable(RuntimeError):
    """The local yt-dlp checkout is missing or could not be imported."""


def checkout_dir() -> Path | None:
    """The yt-dlp checkout to use: ``$CHRYSA_YTDLP_DIR``, else ``<Chrysa folder>/../yt-dlp``."""
    candidates = []
    if os.environ.get(ENV_VAR):
        candidates.append(Path(os.environ[ENV_VAR]).expanduser())
    candidates.append(Path(__file__).resolve().parent.parent.parent / "yt-dlp")
    return next((p.resolve() for p in candidates if (p / "yt_dlp" / "__init__.py").is_file()), None)


def load():
    """Import and return the local ``yt_dlp`` package (thread-safe, once)."""
    global _module
    with _lock:
        if _module is not None:
            return _module
        root = checkout_dir()
        if root is None:
            raise EngineUnavailable(
                f"The download engine wasn't found. Put the yt-dlp folder next to Chrysa, or set {ENV_VAR}.")
        loaded = sys.modules.get("yt_dlp")
        if loaded is None:
            sys.path.insert(0, str(root))
        try:
            import yt_dlp  # noqa: PLC0415
        except Exception as error:
            raise EngineUnavailable(f"The download engine could not be loaded ({type(error).__name__}).") from error
        where = Path(yt_dlp.__file__).resolve().parent.parent
        if where != root:
            raise EngineUnavailable(f"yt_dlp was already loaded from {where}, not the local checkout {root}.")
        _module = yt_dlp
        return yt_dlp


def version() -> str:
    return load().version.__version__


def impersonation_available() -> bool:
    """Whether a request handler that can impersonate a browser (``curl_cffi``) is installed."""
    try:
        yt_dlp = load()
        with yt_dlp.YoutubeDL({"quiet": True, "impersonate": yt_dlp.networking.impersonate.ImpersonateTarget()}):
            return True
    except Exception:  # yt-dlp raises YoutubeDLError when no handler supports the target
        return False


def js_runtime() -> str | None:
    """A JavaScript runtime other than yt-dlp's default (deno) that is installed, as yt-dlp names it."""
    if shutil.which("deno"):
        return None  # enabled by default already
    return next((name for name in ("node", "quickjs", "bun") if shutil.which(name if name != "quickjs" else "qjs")), None)
