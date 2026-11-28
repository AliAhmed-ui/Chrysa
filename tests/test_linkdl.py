"""Paste Link: URL checks, failure classification, the recovery planner, and real downloads from a local server.

The live tests run the real local yt-dlp against a web server on 127.0.0.1 that misbehaves on purpose (drops
connections, rejects plain clients, wants a cookie...), so every recovery path is exercised by a genuine failure
rather than a mock. They need ffmpeg (to make the clip) and the yt-dlp checkout, and are skipped without them.
"""

import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from chrysa import downloads, geoproxy, linkdl, ytdlp
from chrysa.linkdl import Environment, Failure


class FakeError(Exception):
    """Stands in for what yt-dlp raises: a message, and optionally ``status`` and ``exc_info`` like yt-dlp's errors."""

    def __init__(self, message, status=None, root=None):
        super().__init__(message)
        if status is not None:
            self.status = status
        if root is not None:
            self.exc_info = (type(root), root, None)


def named(name, message, **attrs):
    """An exception whose class carries yt-dlp's name (the classifier goes by class names, not imports)."""
    error = type(name, (Exception,), {})(message)
    for key, value in attrs.items():
        setattr(error, key, value)
    return error


class NormalizeUrl(unittest.TestCase):
    def test_accepts_links(self):
        self.assertEqual(linkdl.normalize_url("  https://example.com/watch?v=1  "), "https://example.com/watch?v=1")
        self.assertEqual(linkdl.normalize_url("example.com/video"), "https://example.com/video")
        self.assertEqual(linkdl.normalize_url("http://localhost:8000/a.mp4"), "http://localhost:8000/a.mp4")

    def test_rejects_empty_malformed_and_dangerous(self):
        for text in ("", "   ", "not a url", "https://", "https://nodots", "file:///etc/passwd", "ytsearch:cats",
                     "javascript:alert(1)", "ftp://example.com/x", "magnet:?xt=urn:btih:abc", "https://exa mple.com",
                     "https://example.com/a\nb"):
            with self.assertRaises(linkdl.LinkError, msg=repr(text)):
                linkdl.normalize_url(text)

    def test_proxy_forms(self):
        self.assertEqual(linkdl.normalize_proxy(""), "")
        self.assertEqual(linkdl.normalize_proxy("10.0.0.1:3128"), "http://10.0.0.1:3128")
        self.assertEqual(linkdl.normalize_proxy("socks5h://localhost:9050"), "socks5h://localhost:9050")
        for bad in ("ftp://x:1", "http://", "http://host:notaport", "javascript:x"):
            with self.assertRaises(linkdl.LinkError, msg=bad):
                linkdl.normalize_proxy(bad)


class Classification(unittest.TestCase):
    def kind(self, error, warnings=()):
        return linkdl.classify(error, warnings)

    def test_by_exception_type(self):
        self.assertEqual(self.kind(named("UnsupportedError", "Unsupported URL: x")).kind, linkdl.UNSUPPORTED)
        self.assertEqual(self.kind(named("GeoRestrictedError", "nope")).kind, linkdl.GEO)
        self.assertEqual(self.kind(named("ProxyError", "Unable to connect to proxy")).kind, linkdl.PROXY)
        self.assertEqual(self.kind(named("DownloadCancelled", "cancelled")).kind, linkdl.CANCELLED)
        self.assertEqual(self.kind(PermissionError(13, "Permission denied")).kind, linkdl.PERMISSION)
        self.assertEqual(self.kind(OSError(28, "No space left on device")).kind, linkdl.PERMISSION)

    def test_by_message(self):
        cases = {
            "[youtube] x: Sign in to confirm you’re not a bot. Use --cookies-from-browser": linkdl.AUTH,
            "This video is private": linkdl.AUTH,
            "[site] x: Video unavailable. This video has been removed by the uploader": linkdl.UNAVAILABLE,
            "[site] This video is not available from your location due to geo restriction": linkdl.GEO,
            "Got HTTP Error 403 caused by Cloudflare anti-bot challenge; try again with --extractor-args generic:impersonate":
                linkdl.IMPERSONATION,
            "Requested format is not available. Use --list-formats for a list of available formats": linkdl.FORMAT,
            "[youtube] x: Only images are available for download": linkdl.FORMAT,
            "fragment 3 not found, unable to continue": linkdl.FRAGMENT,
            "ERROR: ffmpeg not found. Please install or provide the path using --ffmpeg-location": linkdl.NO_FFMPEG,
            "This video is DRM protected": linkdl.DRM,
            "Unable to download webpage: HTTPSConnection: Failed to resolve 'x' ([Errno -2] Name or service not known)":
                linkdl.NETWORK,
            "Unable to extract player response; please report this issue": linkdl.EXTRACTOR,
            "something nobody has seen before": linkdl.UNKNOWN,
            "could not find firefox cookies database in /home/x/.mozilla": linkdl.COOKIES,
        }
        for text, kind in cases.items():
            self.assertEqual(self.kind(FakeError(text)).kind, kind, text)

    def test_http_status(self):
        self.assertEqual(self.kind(named("HTTPError", "HTTP Error 404: Not Found", status=404)).kind, linkdl.UNAVAILABLE)
        self.assertEqual(self.kind(named("HTTPError", "HTTP Error 403: Forbidden", status=403)).kind, linkdl.BLOCKED)
        self.assertEqual(self.kind(named("HTTPError", "HTTP Error 401: Unauthorized", status=401)).kind, linkdl.AUTH)
        limited = self.kind(named("HTTPError", "HTTP Error 429: Too Many Requests", status=429))
        self.assertEqual((limited.kind, limited.has("rate_limit")), (linkdl.NETWORK, True))
        self.assertEqual(self.kind(named("HTTPError", "HTTP Error 503: Service Unavailable", status=503)).kind, linkdl.NETWORK)

    def test_wrapped_errors_are_unwrapped(self):
        """yt-dlp raises DownloadError(exc_info=...): the original exception decides, not the wrapper."""
        wrapped = FakeError("ERROR: unable to download", root=named("ProxyError", "Unable to connect to proxy"))
        self.assertEqual(self.kind(wrapped).kind, linkdl.PROXY)

    def test_network_signals(self):
        ipv6 = self.kind(named("TransportError", "Failed to resolve 'x' ([Errno -9] Address family for hostname not supported)"))
        self.assertTrue(ipv6.has("ipv6") and ipv6.has("dns") is False or ipv6.has("ipv6"))
        self.assertEqual(ipv6.kind, linkdl.NETWORK)
        timeout = self.kind(named("TransportError", "Read timed out"))
        self.assertTrue(timeout.has("timeout") and timeout.has("connect"))
        dns = self.kind(named("TransportError", "Failed to resolve 'x' ([Errno -2] Name or service not known)"))
        self.assertTrue(dns.has("dns"))

    def test_youtube_js_signal_comes_from_warnings_too(self):
        failure = self.kind(FakeError("[youtube] abc: Requested format is not available"),
                            ("[youtube] No supported JavaScript runtime could be found.",))
        self.assertEqual(failure.kind, linkdl.FORMAT)
        self.assertTrue(failure.has("youtube") and failure.has("js"))


class Planner(unittest.TestCase):
    """What to try after a failure: deliberate, finite, never the same thing twice."""

    def env(self, **kw):
        return Environment(resolves=lambda host, family: True, **kw)

    def step(self, failure, applied=(), **kw):
        return linkdl.next_step(failure, list(applied), self.env(**kw), "example.com")

    def test_ipv6_symptom_switches_family_first(self):
        failure = Failure(linkdl.NETWORK, "x", frozenset({"ipv6", "connect"}))
        self.assertEqual(self.step(failure), "ipv4")

    def test_plain_network_error_gets_patience_first_then_ipv4_on_connect_failures(self):
        failure = Failure(linkdl.NETWORK, "x", frozenset({"timeout", "connect"}))
        self.assertEqual(self.step(failure), "patient")
        self.assertEqual(self.step(failure, ["patient"]), "ipv4")

    def test_ipv4_failure_tries_ipv6_only_if_the_host_has_ipv6(self):
        failure = Failure(linkdl.NETWORK, "x", frozenset({"connect"}))
        self.assertEqual(self.step(failure, ["ipv4"]), "ipv6")
        no_v6 = Environment(resolves=lambda host, family: family != socket.AF_INET6)
        self.assertEqual(linkdl.next_step(failure, ["ipv4"], no_v6, "example.com"), "patient")

    def test_dns_failure_is_final(self):
        self.assertIsNone(self.step(Failure(linkdl.NETWORK, "x", frozenset({"dns"}))))

    def test_proxy_is_used_only_when_the_user_configured_one(self):
        failure = Failure(linkdl.NETWORK, "x", frozenset({"connect"}))
        self.assertIsNone(self.step(failure, ["patient", "ipv4", "ipv6"]))
        self.assertEqual(self.step(failure, ["patient", "ipv4", "ipv6"], proxy="http://p:1"), "proxy")

    def test_broken_environment_proxy_falls_back_to_direct_but_our_own_proxy_is_blamed(self):
        failure = Failure(linkdl.PROXY, "x")
        self.assertEqual(self.step(failure, system_proxy=True), "direct")
        self.assertIsNone(self.step(failure, ["proxy"], system_proxy=True, proxy="http://p:1"))
        self.assertIsNone(self.step(failure))

    def test_geo_uses_the_users_proxy_gently_then_fully_and_never_without_one(self):
        failure = Failure(linkdl.GEO, "x")
        self.assertIsNone(self.step(failure))
        self.assertEqual(self.step(failure, proxy="http://p:1"), "geo_proxy")
        self.assertEqual(self.step(failure, ["geo_proxy"], proxy="http://p:1"), "proxy")
        self.assertIsNone(self.step(failure, ["geo_proxy", "proxy"], proxy="http://p:1"))

    def test_geo_without_a_user_proxy_uses_tor_only_when_installed(self):
        failure = Failure(linkdl.GEO, "x", countries=("US",))
        self.assertIsNone(self.step(failure))
        self.assertEqual(self.step(failure, tor=True), "tor")
        self.assertIsNone(self.step(failure, ["tor"], tor=True))
        self.assertEqual(self.step(failure, ["geo_proxy", "proxy"], proxy="http://p:1", tor=True), "tor")

    def test_auth_needs_cookies_the_user_enabled(self):
        failure = Failure(linkdl.AUTH, "x")
        self.assertIsNone(self.step(failure))
        self.assertEqual(self.step(failure, cookies_browser="firefox"), "cookies")
        self.assertIsNone(self.step(failure, ["cookies"], cookies_browser="firefox"))

    def test_impersonation_only_when_it_is_installed(self):
        failure = Failure(linkdl.IMPERSONATION, "x")
        self.assertIsNone(self.step(failure, impersonation=False))
        self.assertEqual(self.step(failure, impersonation=True), "impersonate")
        self.assertIsNone(self.step(failure, ["impersonate"], impersonation=True))

    def test_forbidden_tries_impersonation_then_cookies_then_proxy(self):
        failure = Failure(linkdl.BLOCKED, "x", frozenset({"status:403"}))
        kw = dict(impersonation=True, cookies_browser="firefox", proxy="http://p:1")
        self.assertEqual(self.step(failure, **kw), "impersonate")
        self.assertEqual(self.step(failure, ["impersonate"], **kw), "cookies")
        self.assertEqual(self.step(failure, ["impersonate", "cookies"], **kw), "proxy")
        self.assertIsNone(self.step(failure, ["impersonate", "cookies", "proxy"], **kw))

    def test_format_relaxes_once_and_youtube_js_uses_an_installed_runtime(self):
        self.assertEqual(self.step(Failure(linkdl.FORMAT, "x")), "relax_format")
        self.assertIsNone(self.step(Failure(linkdl.FORMAT, "x"), ["relax_format"]))
        js = Failure(linkdl.FORMAT, "x", frozenset({"youtube", "js"}))
        self.assertEqual(self.step(js, js_runtime="node"), "js_runtime")
        self.assertEqual(self.step(js, js_runtime=None), "relax_format")

    def test_fragments_patience_then_ffmpeg_for_hls_only(self):
        self.assertEqual(self.step(Failure(linkdl.FRAGMENT, "x", frozenset({"hls"}))), "patient")
        self.assertEqual(self.step(Failure(linkdl.FRAGMENT, "x", frozenset({"hls"})), ["patient"]), "hls_ffmpeg")
        self.assertIsNone(self.step(Failure(linkdl.FRAGMENT, "x"), ["patient"]))

    def test_nothing_else_is_retried(self):
        for kind in (linkdl.UNSUPPORTED, linkdl.UNAVAILABLE, linkdl.DRM, linkdl.PERMISSION, linkdl.COOKIES,
                     linkdl.NO_FFMPEG, linkdl.CANCELLED, linkdl.UNKNOWN, linkdl.EXTRACTOR):
            self.assertIsNone(self.step(Failure(kind, "x"), impersonation=True, proxy="http://p:1"), kind)

    def test_every_strategy_has_text_and_parameters(self):
        env = Environment(proxy="http://p:1", cookies_browser="firefox", js_runtime="node")
        for key in linkdl.STEP_TEXT:
            self.assertTrue(linkdl.step_params(key, env), key)


class TorRoute(unittest.TestCase):
    def test_missing_tor_is_reported_cleanly(self):
        with mock.patch.object(geoproxy.shutil, "which", return_value=None):
            self.assertFalse(geoproxy.installed())
            with self.assertRaises(geoproxy.TorUnavailable):
                geoproxy.proxy_for(("us",))

    def test_country_codes_are_sanitised_before_reaching_tor(self):
        self.assertEqual(geoproxy._countries(["US", "gb", "x;rm", "123", None]), ("gb", "us"))

    @unittest.skipUnless(geoproxy.installed(), "Tor is not installed")
    def test_real_tor_starts_and_stops(self):  # pragma: no cover - only where Tor is installed
        try:
            url = geoproxy.proxy_for(())
        except geoproxy.TorUnavailable:
            self.skipTest("Tor is installed but cannot reach the Tor network from here")
        self.assertTrue(url.startswith("socks5h://127.0.0.1:"))
        geoproxy.stop_all()


class Explanations(unittest.TestCase):
    def test_actions_point_at_what_is_missing(self):
        env = Environment()
        auth = linkdl.explain(Failure(linkdl.AUTH, "x"), env, [])
        self.assertEqual(auth.action, "cookies")
        self.assertIn("logged-in browser session", auth.message())
        geo = linkdl.explain(Failure(linkdl.GEO, "x"), env, [])
        self.assertEqual((geo.action, "proxy" in geo.message()), ("proxy", True))
        net = linkdl.explain(Failure(linkdl.NETWORK, "x", frozenset({"connect"})), env, ["patient"])
        self.assertEqual(net.action, "proxy")
        self.assertEqual(linkdl.explain(Failure(linkdl.UNSUPPORTED, "x"), env, []).action, "")
        imp = linkdl.explain(Failure(linkdl.IMPERSONATION, "x"), Environment(impersonation=False), [])
        self.assertIn("curl_cffi", imp.message())

    def test_messages_do_not_leak_engine_terms(self):
        env = Environment()
        for kind in (linkdl.NETWORK, linkdl.PROXY, linkdl.GEO, linkdl.AUTH, linkdl.BLOCKED, linkdl.IMPERSONATION,
                     linkdl.FORMAT, linkdl.FRAGMENT, linkdl.EXTRACTOR, linkdl.UNKNOWN, linkdl.UNSUPPORTED):
            text = linkdl.explain(Failure(kind, "raw"), env, []).message().lower()
            for word in ("yt-dlp", "extractor", "traceback", "exception", "--"):
                self.assertNotIn(word, text, f"{kind}: {text}")


# --------------------------------------------------------------------------- live: the real engine, a local server


def _make_clip(path: Path) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    done = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=160x120:rate=10",
         "-f", "lavfi", "-i", "sine=duration=3", "-c:v", "libvpx", "-b:v", "200k", "-c:a", "libvorbis", str(path)],
        capture_output=True)
    return done.returncode == 0 and path.exists()


class Site:
    """A local web server that serves one clip and misbehaves as scripted."""

    def __init__(self, clip: bytes, *, drop: int = 0, status: int = 0, need_cookie: str = "", need_header: str = "",
                 challenge: bool = False, slow: bool = False) -> None:
        self.clip, self.drop, self.status = clip, drop, status
        self.need_cookie, self.need_header, self.challenge, self.slow = need_cookie, need_header, challenge, slow
        self.requests = 0
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def _serve(self, head: bool) -> None:
                site.requests += 1
                if site.requests <= site.drop:
                    self.connection.close()  # no response at all: "Remote end closed connection"
                    return
                if site.status:
                    return self._reply(site.status, b"nope", {"Content-Type": "text/plain"})
                if site.need_header and not self.headers.get(site.need_header):
                    headers = {"Content-Type": "text/html"}
                    if site.challenge:
                        headers["cf-mitigated"] = "challenge"
                    return self._reply(403, b"<html><title>Just a moment...</title></html>", headers)
                if site.need_cookie and site.need_cookie not in (self.headers.get("Cookie") or ""):
                    return self._reply(401, b"Please log in", {"Content-Type": "text/plain"})
                self._reply(200, b"" if head else site.clip, {"Content-Type": "video/webm"}, len(site.clip))

            def _reply(self, code, body, headers, length=None):
                self.send_response(code)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(length if length is not None else len(body)))
                self.end_headers()
                if site.slow and body and code == 200:
                    for i in range(0, len(body), 2048):
                        try:
                            self.wfile.write(body[i:i + 2048])
                            self.wfile.flush()
                        except OSError:
                            return
                        time.sleep(0.05)
                else:
                    self.wfile.write(body)

            def do_GET(self):
                self._serve(False)

            def do_HEAD(self):
                self._serve(True)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/clip.webm"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _engine_ok() -> bool:
    try:
        ytdlp.load()
        return True
    except ytdlp.EngineUnavailable:
        return False


@unittest.skipUnless(_engine_ok(), "the local yt-dlp checkout is not available")
class LiveRecovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="chrysa-link-"))
        cls.clip_path = cls.tmp / "clip.webm"
        if not _make_clip(cls.clip_path):
            raise unittest.SkipTest("ffmpeg cannot make the test clip")
        cls.clip = cls.clip_path.read_bytes()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.out = Path(tempfile.mkdtemp(dir=self.tmp))
        self.events: list[dict] = []
        self.stop = threading.Event()

    def run_link(self, url, env=None, base=None):
        return linkdl.run(url, self.out, env or Environment(), lambda **c: self.events.append(c), self.stop, base=base)

    def steps(self, result):
        return [a.steps for a in result.attempts]

    def test_uses_the_local_checkout(self):
        root = ytdlp.checkout_dir()
        self.assertIsNotNone(root)
        self.assertTrue(Path(ytdlp.load().__file__).resolve().is_relative_to(root))

    def test_normal_download_first_try(self):
        with Site(self.clip) as site:
            result = self.run_link(site.url)
        self.assertTrue(result.ok, result.outcome)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(self.steps(result), [[]])
        self.assertEqual([p.name for p in self.out.iterdir()], result.names)
        self.assertEqual((self.out / result.names[0]).read_bytes(), self.clip)
        self.assertEqual(self.events[0]["message"], "Preparing…")
        progress = [e["progress"] for e in self.events if "progress" in e]
        self.assertTrue(progress and progress == sorted(progress), "progress comes from the engine and never goes back")
        self.assertTrue(any(e.get("size_total") == len(self.clip) for e in self.events))

    def test_network_failure_recovers_patiently(self):
        with Site(self.clip, drop=1) as site:  # the first connection is dropped, as by a flaky server
            result = self.run_link(site.url)
        self.assertTrue(result.ok, [a.line() for a in result.attempts])
        self.assertEqual(result.attempts[0].kind, linkdl.NETWORK)
        self.assertIn("patient", result.attempts[-1].steps)
        self.assertLessEqual(len(result.attempts), linkdl.MAX_ATTEMPTS)
        self.assertTrue(any(e.get("health") == "recovering" for e in self.events))

    def test_ipv6_symptom_recovers_with_ipv4(self):
        """The first attempt is forced onto IPv6 (a route this server, like many hosts, does not have)."""
        with Site(self.clip) as site:
            result = self.run_link(site.url, base={"source_address": "::"})
        self.assertTrue(result.ok, [a.line() for a in result.attempts])
        self.assertEqual(result.attempts[0].kind, linkdl.NETWORK)
        self.assertEqual(self.steps(result), [[], ["ipv4"]])

    def test_dead_system_proxy_falls_back_to_a_direct_connection(self):
        dead = "http://127.0.0.1:9"
        with Site(self.clip) as site, mock.patch.dict(os.environ, {"http_proxy": dead, "HTTP_PROXY": dead}):
            result = self.run_link(site.url, Environment(system_proxy=True))
        self.assertTrue(result.ok, [a.line() for a in result.attempts])
        self.assertEqual(result.attempts[0].kind, linkdl.PROXY)
        self.assertEqual(self.steps(result), [[], ["direct"]])

    def test_cloudflare_style_block_recovers_with_impersonation(self):
        if not ytdlp.impersonation_available():
            self.skipTest("curl_cffi is not installed")
        with Site(self.clip, need_header="sec-ch-ua", challenge=True) as site:  # a plain client sends no client hints
            result = self.run_link(site.url, Environment(impersonation=True))
        self.assertTrue(result.ok, [a.line() for a in result.attempts])
        self.assertEqual(result.attempts[0].kind, linkdl.IMPERSONATION)
        self.assertEqual(self.steps(result), [[], ["impersonate"]])

    def test_cloudflare_block_without_impersonation_explains_what_is_missing(self):
        with Site(self.clip, need_header="sec-ch-ua", challenge=True) as site:
            result = self.run_link(site.url, Environment(impersonation=False))
        self.assertFalse(result.ok)
        self.assertEqual(len(result.attempts), 1)
        self.assertIn("curl_cffi", result.outcome.message())

    def test_login_wall_without_browser_access_asks_for_it_and_does_not_retry(self):
        with Site(self.clip, need_cookie="session=ok") as site:
            result = self.run_link(site.url)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.outcome.action, "cookies")
        self.assertIn("logged-in browser session", result.outcome.message())

    def test_login_wall_with_browser_cookies_uses_them_locally(self):
        """A throw-away Firefox profile in a fake HOME holds the cookie; yt-dlp reads it, Chrysa never does."""
        home = self.out / "home"
        profile = home / ".mozilla" / "firefox" / "test.default"
        profile.mkdir(parents=True)
        (home / ".mozilla" / "firefox" / "profiles.ini").write_text(
            "[Profile0]\nName=default\nIsRelative=1\nPath=test.default\nDefault=1\n")
        db = sqlite3.connect(profile / "cookies.sqlite")
        db.execute("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT NOT NULL DEFAULT '', "
                   "name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER, lastAccessed INTEGER, "
                   "creationTime INTEGER, isSecure INTEGER, isHttpOnly INTEGER, inBrowserElement INTEGER DEFAULT 0, "
                   "sameSite INTEGER DEFAULT 0, rawSameSite INTEGER DEFAULT 0, schemeMap INTEGER DEFAULT 0)")
        db.execute("INSERT INTO moz_cookies (name, value, host, path, expiry, lastAccessed, creationTime, isSecure, "
                   "isHttpOnly) VALUES ('session', 'ok', '127.0.0.1', '/', 4102444800, 0, 0, 0, 0)")
        db.commit()
        db.close()
        with Site(self.clip, need_cookie="session=ok") as site, mock.patch.dict(os.environ, {"HOME": str(home)}):
            result = self.run_link(site.url, Environment(cookies_browser="firefox"))
        self.assertTrue(result.ok, [a.line() for a in result.attempts])
        self.assertEqual(self.steps(result), [[], ["cookies"]])
        self.assertFalse(any("session=ok" in a.detail for a in result.attempts), "a cookie value reached the log")

    def test_unsupported_and_missing_links_end_at_once(self):
        with Site(self.clip, status=404) as site:
            missing = self.run_link(site.url)
        self.assertEqual((missing.ok, len(missing.attempts), missing.attempts[0].kind), (False, 1, linkdl.UNAVAILABLE))
        self.assertEqual(missing.outcome.action, "")

    def test_recovery_is_finite_and_never_repeats_a_configuration(self):
        with Site(self.clip, drop=10_000) as site:
            result = self.run_link(site.url, Environment(proxy="http://127.0.0.1:9"))
        self.assertFalse(result.ok)
        self.assertLessEqual(len(result.attempts), linkdl.MAX_ATTEMPTS)
        configs = [tuple(a.steps) for a in result.attempts]
        self.assertEqual(len(configs), len(set(configs)), "a configuration was tried twice")
        self.assertTrue(result.outcome.message())

    def test_cancel_stops_and_leaves_nothing_behind(self):
        with Site(self.clip * 60, slow=True) as site:  # ~2 MB at ~40 KB/s
            threading.Timer(1.5, self.stop.set).start()
            result = self.run_link(site.url)
        self.assertFalse(result.ok)
        self.assertEqual(result.outcome.kind, linkdl.CANCELLED)
        downloads.remove_partial_files(self.out, [Path(p).name for p in result.paths], "")
        self.assertEqual(list(self.out.iterdir()), [])


@unittest.skipUnless(_engine_ok(), "the local yt-dlp checkout is not available")
class ThroughTheDownloadClient(unittest.TestCase):
    """Paste Link inside Chrysa's own download client, pipeline and validation."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="chrysa-link-"))
        if not _make_clip(cls.tmp / "clip.webm"):
            raise unittest.SkipTest("ffmpeg cannot make the test clip")
        cls.clip = (cls.tmp / "clip.webm").read_bytes()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def make_client(self):
        from chrysa import acquire

        self.library_dir = Path(tempfile.mkdtemp(dir=self.tmp))
        self.admitted: list[list[str]] = []
        self.updates: list[tuple[str, str]] = []
        self.pipeline = acquire.Pipeline(on_admitted=self.admitted.append, malware_scan=False)
        self.client = downloads.DownloadClient(self.library_dir, lambda d: self.updates.append((d.state, d.message)),
                                               pipeline=self.pipeline)

    def wait(self, download, states, timeout=60):
        end = time.time() + timeout
        while time.time() < end and download.state not in states:
            time.sleep(0.05)
        if download._thread:
            download._thread.join(5)
        self.assertIn(download.state, states, download.message)

    def test_download_is_validated_and_admitted_to_the_library_folder(self):
        self.make_client()
        with Site(self.clip) as site:
            download = self.client.start_link(site.url, directory=downloads.incoming_dir(self.library_dir),
                                              final_dir=self.library_dir)
            self.assertEqual(download.kind, "link")
            self.wait(download, ("verified", "failed", "rejected"))
        self.assertEqual(download.state, "verified", download.message)  # a 3 s clip passes: links may be short
        self.assertTrue(Path(download.admitted[0]).is_file())
        self.assertEqual(Path(download.admitted[0]).parent, self.library_dir)
        self.assertEqual(self.admitted, [download.admitted])
        self.assertEqual([p.name for p in downloads.incoming_dir(self.library_dir).iterdir()], [])
        states = [s for s, _ in self.updates]
        self.assertLess(states.index("downloading"), states.index("finished"))
        self.assertLess(states.index("finished"), states.index("validating"))

    def test_failure_is_reported_in_plain_words_with_an_action(self):
        self.make_client()
        with Site(self.clip, need_cookie="session=ok") as site:
            download = self.client.start_link(site.url, directory=downloads.incoming_dir(self.library_dir))
            self.wait(download, ("failed",))
        self.assertEqual(download.action, "cookies")
        self.assertIn("logged-in browser session", download.message)
        self.assertEqual(len(download.attempts), 1)

    def test_same_link_twice_is_refused_while_running(self):
        self.make_client()
        with Site(self.clip * 60, slow=True) as site:
            first = self.client.start_link(site.url, directory=downloads.incoming_dir(self.library_dir))
            with self.assertRaises(downloads.DownloadError):
                self.client.start_link(site.url)
            self.client.cancel_and_delete(first)
            self.wait(first, ("cancelled",))
        self.assertEqual(list(downloads.incoming_dir(self.library_dir).iterdir()), [])

    def test_cancel_and_delete_removes_partial_files(self):
        self.make_client()
        with Site(self.clip * 60, slow=True) as site:
            download = self.client.start_link(site.url, directory=downloads.incoming_dir(self.library_dir))
            time.sleep(1.2)
            self.client.cancel_and_delete(download)
            self.wait(download, ("cancelled",))
        self.assertEqual(list(downloads.incoming_dir(self.library_dir).iterdir()), [])

    def test_link_downloads_are_not_saved_for_resuming(self):
        import json
        self.make_client()
        self.client.state_file = self.tmp / f"state-{id(self)}.json"
        with Site(self.clip * 60, slow=True) as site:
            download = self.client.start_link(site.url, directory=downloads.incoming_dir(self.library_dir))
            self.client._save()
            self.assertEqual(json.loads(self.client.state_file.read_text()), [])
            self.client.cancel_and_delete(download)
            self.wait(download, ("cancelled",))


class ScriptedEngine(unittest.TestCase):
    """The real recovery loop (``linkdl.run``) driving a stand-in for yt-dlp that fails as scripted.

    For failures that cannot be reproduced safely and locally (geo restriction, a missing format, YouTube's JavaScript
    challenge). It proves the loop picks the right strategy and hands yt-dlp the right parameters; it does not prove
    that any particular site then lets the download through.
    """

    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="chrysa-fake-"))
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)
        self.params: list[dict] = []
        self.script: list = []
        test = self

        class YDL:
            def __init__(self, params):
                self.params = params
                test.params.append(params)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=True):
                step = test.script.pop(0)
                for warning in step.get("warnings", ()):
                    self.params["logger"].warning(warning)
                if "error" in step:
                    raise step["error"]
                path = test.out / "Video.mp4"
                path.write_bytes(b"x")
                return {"title": "Video", "requested_downloads": [{"filepath": str(path)}]}

        class Cancelled(Exception):
            pass

        class DownloadError(Exception):
            pass

        engine = mock.Mock()
        engine.YoutubeDL = YDL
        engine.utils.DownloadCancelled = Cancelled
        engine.utils.DownloadError = DownloadError
        patcher = mock.patch.object(ytdlp, "load", return_value=engine)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_link(self, env):
        return linkdl.run("https://example.com/v", self.out, env, lambda **c: None, threading.Event())

    def test_geo_restriction_with_a_proxy_checks_through_it_then_uses_it(self):
        geo = {"error": named("GeoRestrictedError", "This video is not available from your location")}
        self.script = [geo, geo, {}]
        result = self.run_link(Environment(proxy="http://proxy.example:3128"))
        self.assertTrue(result.ok)
        first, second, third = self.params
        self.assertNotIn("proxy", first)
        self.assertNotIn("geo_verification_proxy", first)
        self.assertEqual(second["geo_verification_proxy"], "http://proxy.example:3128")
        self.assertNotIn("proxy", second)
        self.assertEqual(third["proxy"], "http://proxy.example:3128")

    def test_geo_restriction_without_a_proxy_says_a_proxy_is_required(self):
        self.script = [{"error": named("GeoRestrictedError", "geo restriction")}]
        result = self.run_link(Environment())
        self.assertFalse(result.ok)
        self.assertEqual((len(self.params), result.outcome.action), (1, "proxy"))
        self.assertIn("proxy", result.outcome.message())
        self.assertFalse(any("proxy" in p for p in self.params), "a proxy was invented")

    def test_missing_format_is_relaxed_once(self):
        fmt = {"error": FakeError("Requested format is not available. Use --list-formats")}
        self.script = [fmt, {}]
        self.assertTrue(self.run_link(Environment()).ok)
        self.assertEqual(self.params[1]["format"], "b*")
        self.script = [fmt, fmt]
        self.params.clear()
        result = self.run_link(Environment())
        self.assertFalse(result.ok)
        self.assertEqual(len(self.params), 2)  # relaxed once, not repeated

    def test_youtube_js_challenge_enables_the_installed_runtime(self):
        fail = {"error": FakeError("[youtube] abc: Requested format is not available"),
                "warnings": ["[youtube] No supported JavaScript runtime could be found."]}
        self.script = [fail, {}]
        result = self.run_link(Environment(js_runtime="node"))
        self.assertTrue(result.ok)
        self.assertEqual(self.params[1]["js_runtimes"], {"node": {}})

    def test_geo_restriction_routes_through_tor_in_the_named_country(self):
        started = []

        def start_tor(countries, stop):
            started.append(countries)
            return "socks5h://127.0.0.1:19050"

        geo = {"error": named("GeoRestrictedError", "not available from your location", countries=["GB", "IE"])}
        self.script = [geo, {}]
        result = self.run_link(Environment(tor=True, start_tor=start_tor))
        self.assertTrue(result.ok)
        self.assertEqual(started, [("GB", "IE")])
        self.assertNotIn("proxy", self.params[0])
        self.assertEqual(self.params[1]["proxy"], "socks5h://127.0.0.1:19050")

    def test_tor_failing_to_start_is_explained_and_not_retried(self):
        def broken(countries, stop):
            raise geoproxy.TorUnavailable("no")

        self.script = [{"error": named("GeoRestrictedError", "geo restriction")}]
        result = self.run_link(Environment(tor=True, start_tor=broken))
        self.assertFalse(result.ok)
        self.assertEqual(len(self.params), 1)
        self.assertIn("still blocked", result.outcome.message())

    def test_geo_without_tor_tells_the_user_how_to_get_automatic_access(self):
        self.script = [{"error": named("GeoRestrictedError", "geo restriction")}]
        result = self.run_link(Environment(tor=False))
        self.assertIn("Install Tor", result.outcome.message())

    def test_ipv6_then_ipv4_parameters(self):
        self.script = [{"error": named("TransportError", "Address family for hostname not supported")}, {}]
        self.assertTrue(self.run_link(Environment()).ok)
        self.assertEqual(self.params[1]["source_address"], "0.0.0.0")

    def test_proxy_is_never_applied_to_the_first_attempt_and_secrets_stay_out_of_the_log(self):
        self.script = [{"error": FakeError("Sign in to confirm you're not a bot")}, {}]
        result = self.run_link(Environment(cookies_browser="firefox", proxy="http://user:secret@p.example:1"))
        self.assertTrue(result.ok)
        self.assertNotIn("proxy", self.params[0])
        self.assertNotIn("cookiesfrombrowser", self.params[0])
        self.assertEqual(self.params[1]["cookiesfrombrowser"], ("firefox",))
        self.assertNotIn("secret", " ".join(a.line() for a in result.attempts))

    def test_attempt_log_reads_like_the_spec(self):
        self.script = [{"error": named("TransportError", "Address family for hostname not supported")},
                       {"error": named("TransportError", "Connection refused")}, {}]
        result = self.run_link(Environment(resolves=lambda host, family: True))
        lines = [a.line() for a in result.attempts]
        self.assertTrue(lines[0].startswith("Attempt 1: default settings — failed (network)"), lines)
        self.assertTrue(lines[1].startswith("Attempt 2: IPv4 only — failed (network)"), lines)
        self.assertTrue(lines[-1].endswith("succeeded"), lines)


if __name__ == "__main__":
    unittest.main()
