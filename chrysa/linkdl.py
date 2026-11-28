"""Paste Link: download a web link with the local yt-dlp, and recover from the failures that can be recovered from.

    link -> :func:`normalize_url` -> attempt 1 (clean yt-dlp defaults) -> failure? -> :func:`classify` ->
    :func:`next_step` -> attempt 2 ... (at most :data:`MAX_ATTEMPTS`) -> files, or a :class:`Outcome` that says why not

The engine is yt-dlp's Python API (see :mod:`chrysa.ytdlp`), so the pasted text is never part of a command line.
Nothing here enables an option "just in case": every retry follows from what the previous attempt reported, a
strategy is never used twice, and a failure that no yt-dlp option can fix ends the run with an explanation.

* a network error: once patiently (more retries, back-off, longer timeout); at once with IPv4 when the error is an
  IPv6 symptom (address family / unreachable); IPv6 if the IPv4-only attempt could not connect and IPv6 resolves;
  then the proxy the user configured, if any;
* a proxy error: direct, when the proxy came from the environment; otherwise report the proxy as the problem;
* geo restriction: the user's proxy as yt-dlp's geo-verification proxy (only the check goes through it), then as the
  proxy for everything; with no proxy, say that one is required (yt-dlp has already tried its X-Forwarded-For trick);
* authentication / forbidden: the browser cookies the user enabled, read by yt-dlp itself; browser impersonation when
  the site refused the connection and ``curl_cffi`` is installed;
* a missing format: relax the selection once; a YouTube sign-in/JS-challenge failure: enable the installed
  JavaScript runtime; HLS fragment failures: patient retry, then ffmpeg for HLS.
"""

from __future__ import annotations

import errno
import logging
import re
import socket
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import geoproxy, ytdlp

log = logging.getLogger("chrysa.link")

MAX_ATTEMPTS = 4  # the first try plus at most three recoveries
OUTPUT_TEMPLATE = "%(title).150B.%(ext)s"
PROXY_SCHEMES = ("http", "https", "socks4", "socks4a", "socks5", "socks5h")  # what yt-dlp supports
BROWSERS = ("firefox", "chrome", "chromium", "brave", "edge", "opera", "vivaldi", "safari", "whale")
AUDIO_EXTENSIONS = frozenset({".m4a", ".mp3", ".opus", ".ogg", ".oga", ".flac", ".wav", ".aac", ".weba"})

# failure kinds
NETWORK, PROXY, GEO, AUTH, BLOCKED, IMPERSONATION = "network", "proxy", "geo", "auth", "blocked", "impersonation"
EXTRACTOR, FORMAT, FRAGMENT, PERMISSION = "extractor", "format", "fragment", "permission"
UNSUPPORTED, UNAVAILABLE, DRM, COOKIES, NO_FFMPEG, CANCELLED, UNKNOWN = (
    "unsupported", "unavailable", "drm", "cookies", "no_ffmpeg", "cancelled", "unknown")


class LinkError(ValueError):
    """The pasted text cannot be a link (shown to the user as is)."""


def normalize_url(text: str) -> str:
    """The URL to download for what the user pasted, or raise :class:`LinkError`.

    Only ``http``/``https`` links are accepted: yt-dlp also understands ``file:`` paths and search prefixes such as
    ``ytsearch:``, which a pasted line must never trigger.
    """
    url = (text or "").strip()
    if not url:
        raise LinkError("Paste a link first.")
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise LinkError("That doesn't look like a single link.")
    if "://" not in url:
        if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I) and not re.match(r"^[^/:]+\.[^/:]+(:\d+)?(/|$)", url):
            raise LinkError("Only web links (http or https) can be downloaded.")
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        raise LinkError("Only web links (http or https) can be downloaded.")
    host = parts.hostname or ""
    if not host or ("." not in host and host != "localhost" and ":" not in host):
        raise LinkError("That doesn't look like a web link.")
    return url


def normalize_proxy(text: str) -> str:
    """A proxy URL yt-dlp accepts (``http://host:port``, ``socks5://host:port`` ...), or raise :class:`LinkError`.

    A bare ``host:port`` is read as an HTTP proxy. An empty string means "no proxy".
    """
    value = (text or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    parts = urlsplit(value)
    if parts.scheme.lower() not in PROXY_SCHEMES or not parts.hostname:
        raise LinkError("Use a proxy like http://host:port or socks5://host:port.")
    try:
        parts.port  # noqa: B018 - raises on a bad port
    except ValueError as error:
        raise LinkError("That proxy's port isn't valid.") from error
    return value


# --------------------------------------------------------------------------- classification


@dataclass(frozen=True)
class Failure:
    kind: str
    text: str  # the error text, one line (diagnostics only)
    signals: frozenset = frozenset()  # facts the planner uses: ipv6, timeout, dns, hls, youtube, js, status:403 ...
    countries: tuple = ()  # where yt-dlp says a geo-restricted video is available

    def has(self, signal: str) -> bool:
        return signal in self.signals


_FLAT = re.compile(r"\s+")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _flat(text: str) -> str:
    return _FLAT.sub(" ", _ANSI.sub("", text or "")).strip()


def _names(error: BaseException | None) -> set[str]:
    return {cls.__name__ for cls in type(error).__mro__} if error is not None else set()


def _root(error: BaseException) -> BaseException:
    """The exception yt-dlp wrapped (``DownloadError.exc_info``), else *error* itself."""
    info = getattr(error, "exc_info", None)
    return info[1] if info and info[1] is not None else error


_IPV6_SIGNS = ("address family", "network is unreachable", "[errno 101]", "no route to host", "[errno 113]",
               "[errno -9]", "cannot assign requested address", "[errno 99]")
_DNS_SIGNS = ("name or service not known", "temporary failure in name resolution", "nodename nor servname",
              "no address associated", "[errno -2]", "[errno -3]", "[errno -5]")  # not "[errno -9]": that is IPv6
_TIMEOUT_SIGNS = ("timed out", "timeout")
_CONNECT_SIGNS = ("failed to establish a new connection", "connection refused", "unable to connect", "unreachable",
                  "no route to host", "timed out", "address family")
_GEO_SIGNS = ("geo restrict", "geo-restrict", "geo-block", "geoblock", "from your location", "in your country",
              "not available in your", "available in your country", "your region", "from your country")
_AUTH_SIGNS = ("sign in", "log in", "login required", "logged in", "private video", "members-only", "members only",
               "requires authentication", "--cookies", "not a bot", "age-restricted", "confirm your age",
               "subscribers only", "http error 401", "must be logged", "this video is private")
_UNAVAILABLE_SIGNS = ("video unavailable", "has been removed", "no longer available", "does not exist", "http error 404",
                      "was deleted", "terminated", "this live event will begin", "premieres in", "is not live",
                      "not currently live", "has been blocked", "copyright")
_FORMAT_SIGNS = ("requested format is not available", "no video formats", "no formats", "only images are available",
                 "no suitable format", "format is not available")
_COOKIE_SIGNS = ("could not find", "failed to decrypt", "unable to read", "keyring", "database is locked",
                 "could not copy", "cookies database", "cookie database", "unsupported browser", "failed to load cookies")


def classify(error: BaseException, warnings: tuple[str, ...] = ()) -> Failure:
    """What kind of failure *error* (an exception raised by yt-dlp) is, and the facts about it a retry may need."""
    root = _root(error)
    names = _names(root) | _names(error)
    text = _flat(str(error))
    low = text.lower()
    signals: set[str] = set()
    if "[youtube" in low or "youtube.com" in low or "youtu.be" in low:
        signals.add("youtube")
    if any("javascript runtime" in w.lower() or "js runtime" in w.lower() for w in warnings) or "javascript" in low \
            or "challenge" in low or "n function" in low:
        signals.add("js")
    if "m3u8" in low or "hls" in low:
        signals.add("hls")
    status = getattr(root, "status", None)
    if isinstance(status, int):
        signals.add(f"status:{status}")
    if any(s in low for s in _IPV6_SIGNS):
        signals.add("ipv6")
    if any(s in low for s in _DNS_SIGNS):
        signals.add("dns")
    if any(s in low for s in _TIMEOUT_SIGNS) or "TimeoutError" in names:
        signals.add("timeout")
    if any(s in low for s in _CONNECT_SIGNS):
        signals.add("connect")

    found = getattr(root, "countries", None) or getattr(error, "countries", None) or ()

    def done(kind: str) -> Failure:
        return Failure(kind, text[:400], frozenset(signals), tuple(str(c) for c in found))

    if "DownloadCancelled" in names:
        return done(CANCELLED)
    if isinstance(root, OSError) and getattr(root, "errno", None) in (errno.EACCES, errno.EPERM, errno.ENOSPC,
                                                                      errno.EROFS, errno.EDQUOT):
        return done(PERMISSION)
    if "UnsupportedError" in names:
        return done(UNSUPPORTED)
    if "GeoRestrictedError" in names:
        return done(GEO)
    if "ProxyError" in names:
        return done(PROXY)
    if "drm" in low and ("protected" in low or "drm" in low.split()):
        return done(DRM)
    if "ffmpeg" in low and any(s in low for s in ("not found", "not installed", "is not available", "you have requested merging")):
        return done(NO_FFMPEG)
    if "cookie" in low and any(s in low for s in _COOKIE_SIGNS) and "sign in" not in low:
        return done(COOKIES)
    if "impersonat" in low or "cloudflare anti-bot" in low:
        return done(IMPERSONATION)
    if any(s in low for s in _GEO_SIGNS):
        return done(GEO)
    if "PermissionError" in names or "permission denied" in low or "no space left" in low:
        return done(PERMISSION)
    if any(s in low for s in _FORMAT_SIGNS):
        return done(FORMAT)
    if any(s in low for s in _AUTH_SIGNS) and status not in (403, 404, 429) and "dns" not in signals:
        return done(AUTH)
    if isinstance(status, int) and status >= 500:
        return done(NETWORK)  # the server, not the link: worth a patient retry
    if status == 404 or any(s in low for s in _UNAVAILABLE_SIGNS):
        return done(UNAVAILABLE)
    if status in (401, 407):
        return done(AUTH)
    if status == 403:
        return done(BLOCKED)
    if "fragment" in low or "did not get any data blocks" in low:
        return done(FRAGMENT)
    if status == 429 or "too many requests" in low:
        signals.add("rate_limit")
        return done(NETWORK)
    if names & {"TransportError", "IncompleteRead", "SSLError", "CertificateVerifyError", "ContentTooShortError",
                "TimeoutError", "ConnectionError", "socket.timeout", "HTTPError"} \
            or "ipv6" in signals or "dns" in signals or "timeout" in signals \
            or any(s in low for s in ("connection reset", "connection refused", "connection aborted", "remote end closed",
                                      "unable to connect", "http error 5", "ssl", "incomplete")):
        return done(NETWORK)
    if "ExtractorError" in names or "unable to extract" in low or "unable to download" in low:
        return done(EXTRACTOR)
    return done(UNKNOWN)


# --------------------------------------------------------------------------- environment and planning


@dataclass
class Environment:
    """What the user and this machine offer a retry: configured proxy/cookies and installed capabilities."""

    proxy: str = ""  # a proxy the user configured (never invented, never a third party's)
    cookies_browser: str = ""  # a browser the user allowed yt-dlp to read cookies from
    system_proxy: bool = False  # the environment already routes through a proxy (yt-dlp honours it by itself)
    impersonation: bool = False  # curl_cffi is installed
    js_runtime: str | None = None  # an installed JavaScript runtime that yt-dlp does not enable by default
    tor: bool = False  # Tor is installed, so Chrysa can start a private route for geo-restricted links
    tor_route: str = ""  # the local SOCKS URL of that route once it is running (set by run())
    start_tor: Callable = field(default=geoproxy.proxy_for, repr=False)
    resolves: Callable[[str, int], bool] = field(default=lambda host, family: True, repr=False)

    @classmethod
    def detect(cls, proxy: str = "", cookies_browser: str = "") -> Environment:
        env = urllib.request.getproxies()
        return cls(proxy=proxy, cookies_browser=cookies_browser,
                   system_proxy=any(k in env for k in ("http", "https", "socks", "all")),
                   impersonation=ytdlp.impersonation_available(), js_runtime=ytdlp.js_runtime(), resolves=_resolves,
                   tor=geoproxy.installed())


def _resolves(host: str, family: int) -> bool:
    try:
        return bool(socket.getaddrinfo(host, 443, family, socket.SOCK_STREAM))
    except OSError:
        return False


def next_step(failure: Failure, applied: list[str], env: Environment, host: str = "") -> str | None:
    """The one strategy worth trying after *failure*, or None when nothing yt-dlp offers can help.

    *applied* are the strategies already used in this run; none is ever returned twice.
    """
    def fresh(key: str) -> str | None:
        return key if key not in applied else None

    kind = failure.kind
    if kind == NETWORK:
        if failure.has("dns"):
            return None  # the name does not resolve: neither patience nor IPv4 changes that
        if failure.has("ipv6") and "ipv6" not in applied and "ipv4" not in applied:
            return "ipv4"  # an IPv6 symptom: skip the patience, switch the address family
        if "ipv4" in applied and "ipv6" not in applied and failure.has("connect") and env.resolves(host, socket.AF_INET6):
            return "ipv6"  # IPv4-only could not connect either, and the host does have an IPv6 address
        step = fresh("patient")
        if step:
            return step
        if "ipv4" not in applied and "ipv6" not in applied and failure.has("connect") and env.resolves(host, socket.AF_INET):
            return "ipv4"  # the connection never completed: a dead IPv6 route is the usual cause
        if env.proxy and "proxy" not in applied:
            return "proxy"
        return None
    if kind == PROXY:
        if "proxy" in applied or not env.system_proxy:
            return None  # the proxy Chrysa applied is itself the problem
        return fresh("direct")
    if kind == GEO:
        if env.proxy:
            step = fresh("geo_proxy") or fresh("proxy")
            if step:
                return step
        return fresh("tor") if env.tor else None  # no proxy of the user's: a private Tor route, if installed
    if kind == AUTH:
        return fresh("cookies") if env.cookies_browser else None
    if kind in (BLOCKED, IMPERSONATION):
        if env.impersonation and "impersonate" not in applied:
            return "impersonate"
        if kind == BLOCKED and env.cookies_browser and "cookies" not in applied:
            return "cookies"
        if kind == BLOCKED and env.proxy and "proxy" not in applied:
            return "proxy"
        return None
    if kind == FORMAT:
        if failure.has("youtube") and failure.has("js") and env.js_runtime and "js_runtime" not in applied:
            return "js_runtime"
        return fresh("relax_format")
    if kind == FRAGMENT:
        if "patient" not in applied:
            return "patient"
        return fresh("hls_ffmpeg") if failure.has("hls") else None
    if kind == EXTRACTOR:
        if failure.has("youtube") and failure.has("js") and env.js_runtime:
            return fresh("js_runtime")
        if failure.has("status:403") and env.impersonation:
            return fresh("impersonate")
        return None  # an unexpected extractor error is a yt-dlp bug or a site change: retrying cannot fix it
    return None  # unsupported, unavailable, DRM, permission, cookies, missing ffmpeg, cancelled, unknown


# what the user sees while a strategy runs, and what the attempt log calls it
STEP_TEXT = {
    "patient": ("Connection problem. Trying again more patiently…", "longer timeouts and retries with back-off"),
    "ipv4": ("Trying another connection method…", "IPv4 only"),
    "ipv6": ("Trying another connection method…", "IPv6 only"),
    "direct": ("The proxy didn't respond. Trying a direct connection…", "direct connection, system proxy off"),
    "proxy": ("Trying through your proxy…", "configured proxy"),
    "geo_proxy": ("This looks region-locked. Checking through your proxy…", "configured proxy for geo-verification"),
    "tor": ("This looks region-locked. Connecting through a private route — this can take a minute…",
            "private Tor route (exit in the video's region)"),
    "cookies": ("This link needs a signed-in session. Using your browser's…", "browser cookies"),
    "impersonate": ("The site rejected the connection. Trying browser-compatible access…", "browser impersonation"),
    "relax_format": ("Trying another quality…", "any available format"),
    "js_runtime": ("Preparing this site's player…", "JavaScript runtime enabled"),
    "hls_ffmpeg": ("Trying another way to fetch the stream…", "ffmpeg for HLS"),
}


def _backoff(attempt: int) -> float:
    return min(2.0 ** attempt, 20.0)


def step_params(key: str, env: Environment) -> dict:
    """The yt-dlp parameters (``YoutubeDL(params)``) a strategy adds. Merged over earlier strategies' by the runner."""
    if key == "patient":
        return {"retries": 6, "fragment_retries": 10, "extractor_retries": 5, "socket_timeout": 30,
                "retry_sleep_functions": {"http": _backoff, "fragment": _backoff, "extractor": _backoff}}
    if key == "ipv4":
        return {"source_address": "0.0.0.0"}
    if key == "ipv6":
        return {"source_address": "::"}
    if key == "direct":
        return {"proxy": ""}  # an explicit empty proxy switches the environment's proxy off
    if key == "proxy":
        return {"proxy": env.proxy}
    if key == "geo_proxy":
        return {"geo_verification_proxy": env.proxy}
    if key == "tor":
        return {"proxy": env.tor_route}
    if key == "cookies":
        return {"cookiesfrombrowser": (env.cookies_browser,)}
    if key == "impersonate":
        yt_dlp = ytdlp.load()
        return {"impersonate": yt_dlp.networking.impersonate.ImpersonateTarget(),
                "extractor_args": {"generic": {"impersonate": [""]}}}
    if key == "relax_format":
        return {"format": "b*"}
    if key == "js_runtime":
        return {"js_runtimes": {env.js_runtime: {}}}
    if key == "hls_ffmpeg":
        return {"hls_prefer_native": False}
    raise KeyError(key)


def base_params(directory: Path, hooks: list, post_hooks: list, logger) -> dict:
    """The clean first attempt: yt-dlp's own defaults, one video, into *directory*."""
    return {
        "paths": {"home": str(directory)},
        "outtmpl": OUTPUT_TEMPLATE,
        "noplaylist": True,
        "playlist_items": "1",  # a pasted playlist/channel link fetches its first entry only
        "quiet": True,
        "noprogress": True,
        "no_color": True,
        "logger": logger,
        "progress_hooks": hooks,
        "postprocessor_hooks": post_hooks,
        "consoletitle": False,
    }


# --------------------------------------------------------------------------- explaining a final failure


@dataclass(frozen=True)
class Outcome:
    """A failure Chrysa could not recover from, in the user's words."""

    title: str
    detail: str = ""
    action: str = ""  # "" | "retry" | "proxy" | "cookies"
    kind: str = UNKNOWN

    def message(self) -> str:
        return f"{self.title} {self.detail}".strip()


def explain(failure: Failure, env: Environment, applied: list[str], tor_failed: bool = False) -> Outcome:
    """Plain-language explanation and the one action that could still help."""
    kind = failure.kind
    tried_proxy = "proxy" in applied or "geo_proxy" in applied
    if kind == CANCELLED:
        return Outcome("Cancelled.", kind=kind)
    if kind == UNSUPPORTED:
        return Outcome("Couldn't download this link.", "Chrysa can't find a video there.", kind=kind)
    if kind == UNAVAILABLE:
        return Outcome("This video isn't available.", "It may have been removed, made private, or not started yet.", kind=kind)
    if kind == DRM:
        return Outcome("This video is protected.", "It can't be downloaded.", kind=kind)
    if kind == PERMISSION:
        return Outcome("Couldn't save the file.", "Check the download folder's permissions and free space.", "retry", kind)
    if kind == NO_FFMPEG:
        return Outcome("Couldn't finish the download.", "ffmpeg is needed to combine this video. Install ffmpeg and try again.", "retry", kind)
    if kind == COOKIES:
        return Outcome("Couldn't read your browser's sign-in.",
                       "Close the browser and try again, or choose another browser.", "cookies", kind)
    if kind == AUTH:
        if "cookies" in applied:
            return Outcome("Still no access.", "The browser you chose isn't signed in to this site, or its session expired.",
                           "cookies", kind)
        return Outcome("This link requires access through a logged-in browser session.",
                       "Choose a browser that's signed in to this site.", "cookies", kind)
    if kind == GEO:
        if "tor" in applied or tor_failed:
            return Outcome("This video is still blocked in your region.",
                           "The private route didn't get through. A VPN or proxy in a permitted country would.",
                           "proxy", kind)
        if tried_proxy:
            return Outcome("This video is still blocked in your region.",
                           "Your proxy may be in a country where it isn't available.", "proxy", kind)
        return Outcome("This video isn't available in your region.",
                       "Install Tor (sudo apt install tor) and Chrysa will route around this automatically, "
                       "or set a proxy.", "proxy", kind)
    if kind == PROXY:
        return Outcome("Couldn't reach your proxy.", "Check the proxy address.", "proxy", kind)
    if kind in (BLOCKED, IMPERSONATION):
        if kind == IMPERSONATION and not env.impersonation:
            return Outcome("This site rejected the connection.",
                           "It needs browser-compatible access, which requires the curl_cffi package.", "retry", kind)
        return Outcome("This site rejected the connection.",
                       "It isn't available to this device right now." if not (env.proxy or env.cookies_browser)
                       else "It still refuses access after the alternatives.", "retry", kind)
    if kind == NETWORK:
        if failure.has("dns"):
            return Outcome("Couldn't reach this address.", "Check the link and your internet connection.", "retry", kind)
        if tried_proxy:
            return Outcome("Couldn't connect, even through your proxy.", "", "proxy", kind)
        if not env.proxy:
            return Outcome("Couldn't connect to this site.",
                           "If your network or region blocks it, a proxy is required.", "proxy", kind)
        return Outcome("Couldn't connect to this site.", "Check your internet connection and try again.", "retry", kind)
    if kind == FORMAT:
        return Outcome("Couldn't find a version to download.",
                       "The site isn't offering a format the download engine can fetch." if not failure.has("youtube") else
                       "YouTube isn't offering this video to the download engine right now.", "retry", kind)
    if kind == FRAGMENT:
        return Outcome("The download was interrupted.", "The site kept dropping parts of the video.", "retry", kind)
    if kind == EXTRACTOR:
        return Outcome("Couldn't download this link.",
                       "The site may have changed. Updating the download engine usually fixes this.", "retry", kind)
    return Outcome("Couldn't download this link.", "", "retry", kind)


# --------------------------------------------------------------------------- running


@dataclass
class Attempt:
    number: int
    steps: list  # strategies in effect, in the order they were added
    ok: bool = False
    kind: str = ""  # failure kind when not ok
    detail: str = ""  # the (single-line, truncated) error
    seconds: float = 0.0

    def line(self) -> str:
        how = "default settings" if not self.steps else ", ".join(STEP_TEXT[s][1] for s in self.steps)
        result = "succeeded" if self.ok else f"failed ({self.kind})"
        return f"Attempt {self.number}: {how} — {result}" + (f": {self.detail}" if self.detail and not self.ok else "")


@dataclass
class LinkResult:
    ok: bool = False
    title: str = ""
    names: list[str] = field(default_factory=list)  # top-level file names created in the directory
    paths: list[str] = field(default_factory=list)  # every file that was written (for cleanup)
    attempts: list[Attempt] = field(default_factory=list)
    outcome: Outcome | None = None


class _Logger:
    """Receives yt-dlp's console output. It is kept (a few lines) for diagnostics and never shown as is."""

    def __init__(self, on_stage: Callable[[str], None]) -> None:
        self.on_stage = on_stage
        self.warnings: list[str] = []

    def debug(self, message: str) -> None:
        if message.startswith("[info]") and "Downloading" in message and "format" in message:
            self.on_stage("connecting")

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        self.warnings.append(_flat(message)[:300])
        del self.warnings[:-8]

    def error(self, message: str) -> None:
        pass


Emit = Callable[..., None]


def run(url: str, directory: Path, env: Environment, emit: Emit, stop: threading.Event,
        base: Mapping | None = None) -> LinkResult:
    """Download *url* into *directory*, recovering from failures. *emit(**changes)* reports progress as
    ``Download`` fields (``name``, ``progress``, ``speed``, ``eta``, ``size_done``, ``size_total``, ``message``,
    ``health``). Blocks; call it from a worker thread. *stop* cancels it."""
    yt_dlp = ytdlp.load()
    host = urlsplit(url).hostname or ""
    result = LinkResult()
    applied: list[str] = []
    params_by_step: dict[str, dict] = {}
    seen_paths: set[str] = set()
    geo_countries: tuple = ()
    state = {"title": "", "files_done": 0, "progress": 0.0, "current": None, "done_bytes": 0}

    def hook(data: dict) -> None:
        for key in ("filename", "tmpfilename"):  # remembered first: a cancel must still know what to delete
            if data.get(key):
                seen_paths.add(str(data[key]))
                seen_paths.update((str(data[key]) + ".part", str(data[key]) + ".ytdl"))
        if stop.is_set():
            raise yt_dlp.utils.DownloadCancelled("cancelled")
        info = data.get("info_dict") or {}
        title = info.get("title") or ""
        changes: dict = {}
        if title and title != state["title"]:
            state["title"] = title
            changes["name"] = title
        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            done = data.get("downloaded_bytes") or 0
            streams = max(1, len(info.get("requested_formats") or []))
            if state["current"] != data.get("filename"):
                if state["current"] is not None:
                    state["files_done"] += 1
                    state["done_bytes"] += state.get("last_total", 0)
                state["current"] = data.get("filename")
            state["last_total"] = done if not total else total
            fraction = (done / total) if total else 0.0
            if data.get("fragment_count"):
                fraction = max(fraction, (data.get("fragment_index") or 0) / data["fragment_count"])
            overall = min(0.99, (state["files_done"] + min(fraction, 1.0)) / streams)
            state["progress"] = max(state["progress"], overall)
            changes.update(progress=state["progress"], speed=int(data.get("speed") or 0),
                           eta=_clock(data.get("eta")), size_done=state["done_bytes"] + done,
                           size_total=state["done_bytes"] + total if total else 0, message="", health="healthy")
        emit(**changes)

    def post_hook(data: dict) -> None:
        if data.get("status") == "started" and data.get("postprocessor") in ("Merger", "FFmpegMerger", "VideoConvertor",
                                                                              "FFmpegVideoRemuxer"):
            emit(message="Finishing…", health="connecting", speed=0, eta="")

    def stage(name: str) -> None:
        if name == "connecting" and not stop.is_set():
            emit(message="Connecting…", health="connecting")

    logger = _Logger(stage)
    started = time.monotonic()
    while True:
        number = len(result.attempts) + 1
        params = base_params(directory, [hook], [post_hook], logger)
        params.update(base or {})
        if "tor" in applied and not env.tor_route:
            try:
                env.tor_route = env.start_tor(geo_countries, stop)
            except geoproxy.TorUnavailable:
                result.outcome = explain(Failure(GEO, "tor unavailable", countries=geo_countries), env, applied, tor_failed=True)
                if stop.is_set():
                    result.outcome = explain(Failure(CANCELLED, "cancelled"), env, applied)
                return result
        for key in applied:
            params_by_step[key] = params_by_step.get(key) or step_params(key, env)
            _merge(params, params_by_step[key])
        attempt = Attempt(number, list(applied))
        result.attempts.append(attempt)
        logger.warnings.clear()
        if number == 1:
            emit(message="Preparing…", health="connecting", speed=0, eta="")
        else:
            emit(message=STEP_TEXT[applied[-1]][0], health="recovering", speed=0, eta="", progress=0.0)
            state.update(files_done=0, progress=0.0, current=None, done_bytes=0)
        t0 = time.monotonic()
        try:
            with yt_dlp.YoutubeDL(params) as ydl:
                info = ydl.extract_info(url, download=True)
            if stop.is_set():
                raise yt_dlp.utils.DownloadCancelled("cancelled")
            found = _files(info, directory)
            if not found:
                raise yt_dlp.utils.DownloadError("yt-dlp finished without writing a file")
            attempt.ok, attempt.seconds = True, time.monotonic() - t0
            result.ok, result.title = True, (info or {}).get("title") or state["title"]
            result.names = sorted({Path(p).relative_to(directory).parts[0] for p in found})
            result.paths = sorted(set(found) | seen_paths)
            log.info("[LINK] %s", attempt.line())
            return result
        except BaseException as error:  # noqa: BLE001 - yt-dlp raises many types; classify decides
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            attempt.seconds = time.monotonic() - t0
            failure = classify(error, tuple(logger.warnings))
            if stop.is_set():
                failure = Failure(CANCELLED, "cancelled")
            attempt.kind, attempt.detail = failure.kind, failure.text[:200]
            if failure.kind == GEO and failure.countries:
                geo_countries = failure.countries
            log.info("[LINK] %s", attempt.line())
            result.paths = sorted(seen_paths)
            step = None
            if failure.kind != CANCELLED and len(result.attempts) < MAX_ATTEMPTS:
                step = next_step(failure, applied, env, host)
            if step is None:
                result.outcome = explain(failure, env, applied)
                return result
            applied.append(step)
            if step == "patient" and _pause(5.0 if failure.has("rate_limit") else 2.0, stop):
                result.outcome = explain(Failure(CANCELLED, "cancelled"), env, applied)
                return result
            log.info("[LINK] recovering from %s with %s (%.0fs so far)", failure.kind, step, time.monotonic() - started)


def _pause(seconds: float, stop: threading.Event) -> bool:
    """Wait *seconds* (a server that just dropped us needs a moment); True if cancelled meanwhile."""
    return stop.wait(seconds)


def _merge(params: dict, extra: dict) -> None:
    for key, value in extra.items():
        if key == "extractor_args":
            merged = {k: dict(v) for k, v in params.get(key, {}).items()}
            for ie, args in value.items():
                merged.setdefault(ie, {}).update(args)
            params[key] = merged
        else:
            params[key] = value


def _files(info: dict | None, directory: Path) -> list[str]:
    """Paths of the files yt-dlp wrote for *info* (a video or a one-entry playlist), inside *directory*."""
    found: list[str] = []
    stack = [info or {}]
    while stack:
        item = stack.pop()
        for entry in item.get("entries") or ():
            if entry:
                stack.append(entry)
        for done in item.get("requested_downloads") or ():
            path = done.get("filepath") or done.get("_filename")
            if path and Path(path).is_file():
                found.append(path)
    root = directory.resolve()
    return [p for p in found if Path(p).resolve().is_relative_to(root)]


def _clock(seconds) -> str:
    if not seconds or seconds < 0:
        return ""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
