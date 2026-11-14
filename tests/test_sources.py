"""Source adapters: parser contracts, failure kinds, health states, overrides and failure isolation in search.

Everything runs offline against small fixtures shaped like the real pages. Set ``CHRYSA_LIVE=1`` to also run the
live contract check against every real source (useful after a site changes).
"""

import json
import os
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from chrysa import bd, search, sources
from chrysa.database import Database
from chrysa.history import History

H1 = "a" * 40
H2 = "b" * 40

X1337_ROW = '''<tr>
<td class="coll-1 name"><a href="/sub/tv/HD/1/" class="icon"><i class="flaticon-hd"></i></a><a href="/torrent/3250239/{slug}/">{name}</a></td>
<td class="coll-2 seeds">{seeds}</td>
<td class="coll-3 leeches">{leech}</td>
<td class="coll-date">Sep. 25th  '18</td>
<td class="coll-4 size mob-uploader">559 MB</td>
</tr>'''
X1337_PAGE = '<html><title>Download x | 1337x</title><div class="col-9 page-content search-page"><table class="table-list"><thead><tr><th>name</th></tr></thead><tbody>{rows}</tbody></table></div></html>'

LIME_ROW = ('<tr bgcolor="#F4F4F4"><td class="tdleft"><div class="tt-name"><a href="http://itorrents.net/torrent/{hash}.torrent'
            '?title=x" rel="nofollow" class="csprite_dl14"></a><a href="/{slug}-torrent-157833.html">{name}</a></div></td>'
            '<td class="tdnormal">1 Year+ - in Movies</a></td><td class="tdnormal">4.16 GB</td><td class="tdseed">{seeds}</td>'
            '<td class="tdleech">0</td><td class="tdright"></td></tr>')
LIME_AD = ('<tr bgcolor="#F4F4F4"><td class="tdleft"><div class="tt-name"><a href="/leet2/?search=1&keyword=x" rel="nofollow">'
           '<b>x</b> Direct Download</a></div></td><td class="tdseed">4704 KB/Sec</td></tr>')
LIME_PAGE = "<html><title>x Torrents Download - LimeTorrents</title><table class='table2'>{rows}</table>{extra}</html>"

NYAA_RSS = '''<?xml version="1.0"?><rss xmlns:nyaa="https://nyaa.si/xmlns/nyaa" version="2.0"><channel><title>Nyaa</title>
<item><title>[Group] Show - 01 (1080p).mkv</title><nyaa:infoHash>{h}</nyaa:infoHash><nyaa:seeders>12</nyaa:seeders>
<nyaa:leechers>3</nyaa:leechers><nyaa:size>1.4 GiB</nyaa:size></item></channel></rss>'''


class Parsers(unittest.TestCase):
    """The result contract for each source: title, valid magnet/info hash, parseable size, numeric swarm."""

    def assertContract(self, source, rows):
        outcome = sources.REGISTRY[source].check(rows)
        self.assertEqual(outcome.status, sources.OK, outcome.detail)
        self.assertEqual(outcome.rejected, 0)
        for row in outcome.rows:
            self.assertTrue(row["name"])
            self.assertTrue(sources.info_hash(row["magnet"]) or row["magnet"].startswith("http"))
            self.assertIsInstance(row["size"], int)

    def test_yts(self):
        data = {"status": "ok", "data": {"movies": [{"title_long": "Sintel (2010)", "torrents": [
            {"hash": H1.upper(), "quality": "1080p", "type": "web", "video_codec": "x264", "size_bytes": 900_000_000,
             "seeds": 20, "peers": 3, "date_uploaded_unix": 1_600_000_000}]}]}}
        rows = bd.parse_yts(data)
        self.assertEqual(rows[0]["name"], "Sintel (2010) [1080p web x264]")
        self.assertEqual(rows[0]["published"], 1_600_000_000)
        self.assertContract("YTS", rows)
        self.assertEqual(bd.parse_yts({"status": "ok", "data": {"movie_count": 0}}), [])
        with self.assertRaises(bd.SourceError) as caught:
            bd.parse_yts({"error": "moved"})
        self.assertEqual(caught.exception.status, bd.CHANGED)

    def test_tpb(self):
        rows = bd.parse_tpb([{"id": "1", "name": "Film 2010 1080p", "info_hash": H1, "size": "123", "seeders": "4",
                              "leechers": "1", "added": "1600000000"}])
        self.assertContract("TPB", rows)
        self.assertEqual(bd.parse_tpb([{"id": "0", "name": "No results returned", "info_hash": "0" * 40}]), [])

    def test_1337x_listing(self):
        page = X1337_PAGE.format(rows=X1337_ROW.format(slug="a", name="Film 2010 720p", seeds=30, leech=2)
                                 + X1337_ROW.format(slug="b", name="Film 2010 1080p", seeds=90, leech=5))
        rows = bd.parse_1337x_listing(page)
        self.assertEqual([r[0] for r in rows], ["Film 2010 1080p", "Film 2010 720p"])  # most seeded first
        self.assertEqual(rows[0][2:], (90, 5, 559 << 20))
        self.assertEqual(bd.parse_1337x_listing(X1337_PAGE.format(rows="")), [])  # genuinely nothing found

    def test_1337x_changed_structure_is_not_no_results(self):
        with self.assertRaises(bd.SourceError) as caught:
            bd.parse_1337x_listing("<html><div class='new-layout'>results moved</div></html>")
        self.assertEqual(caught.exception.status, bd.CHANGED)
        renamed = X1337_PAGE.format(rows='<tr><td class="name"><span data-href="/torrent/1/x/">Film</span></td></tr>')
        with self.assertRaisesRegex(bd.SourceError, "none could be read"):
            bd.parse_1337x_listing(renamed)

    def test_limetorrents_skips_injected_ads(self):
        page = LIME_PAGE.format(rows=LIME_AD + LIME_ROW.format(hash=H1.upper(), slug="Film", name="Film 2010 1080p", seeds=7),
                                extra="")
        rows = bd.parse_limetorrents(page)
        self.assertEqual([(r["name"], r["seeders"], r["size"]) for r in rows], [("Film 2010 1080p", 7, int(4.16 * (1 << 30)))])
        self.assertContract("LimeTorrents", rows)
        self.assertEqual(bd.parse_limetorrents(LIME_PAGE.format(rows=LIME_AD, extra="No results found")), [])
        with self.assertRaises(bd.SourceError) as caught:
            bd.parse_limetorrents("<html>This domain is for sale</html>")
        self.assertEqual(caught.exception.status, bd.CHANGED)

    def test_nyaa_and_subsplease_and_eztv(self):
        rows = bd.parse_nyaa(NYAA_RSS.format(h=H1))
        self.assertContract("Nyaa", rows)
        self.assertEqual(rows[0]["seeders"], 12)
        self.assertEqual(bd.parse_subsplease([]), [])
        rows = bd.parse_subsplease({"x": {"show": "Show", "episode": "05", "downloads": [
            {"res": "1080", "magnet": f"magnet:?xt=urn:btih:{H1}&xl=1400000000"}]}})
        outcome = sources.REGISTRY["SubsPlease"].check(rows)
        self.assertEqual(outcome.rows[0]["seeders"], None)  # SubsPlease publishes no swarm figures
        self.assertEqual(outcome.rows[0]["size"], 1_400_000_000)
        rows = bd.parse_eztv({"torrents": [{"hash": H1, "title": "Show S01E01 720p", "size_bytes": "5", "seeds": 3,
                                            "peers": 1, "date_released_unix": 1}]})
        self.assertContract("EZTV", rows)

    def test_contract_flags_broken_parsers(self):
        garbage = [{"name": "x", "magnet": "magnet:?xt=urn:btih:nothex", "size": 1, "seeders": 1, "leechers": 1}] * 4
        outcome = sources.REGISTRY["TPB"].check(garbage)
        self.assertEqual(outcome.status, bd.CHANGED)
        self.assertIn("parser broken", outcome.detail)
        outcome = sources.REGISTRY["TPB"].check([{"name": "", "magnet": f"magnet:?xt=urn:btih:{H1}", "size": 1,
                                                 "seeders": 1, "leechers": 1}])
        self.assertEqual((outcome.status, outcome.detail), (bd.NO_RESULTS, "1/1 rows broke the contract (missing title)"))
        bad_seeds = {"name": "x", "magnet": f"magnet:?xt=urn:btih:{H1}", "size": 1, "seeders": "lots", "leechers": 0}
        self.assertEqual(sources.contract_problem(bad_seeds), "seeders not numeric")

    def test_info_hash_forms(self):
        self.assertEqual(sources.info_hash(f"magnet:?xt=urn:btih:{H1.upper()}&dn=x"), H1)
        self.assertEqual(sources.info_hash("magnet:?xt=urn:btih:YNCKHTQCWBTRNJIV4WNAE52SJUQCZO5C"),
                         "c344a3ce02b06716a515e59a0277524d202cbba2")
        self.assertEqual(sources.info_hash("magnet:?xt=urn:btih:xyz"), "")


class Failures(unittest.TestCase):
    def test_error_kinds(self):
        cases = [
            (urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution")), bd.OFFLINE),
            (urllib.error.URLError(ConnectionRefusedError()), bd.OFFLINE),
            (ConnectionResetError(), bd.OFFLINE),
            (socket.timeout(), bd.TIMEOUT),
            (urllib.error.URLError(socket.timeout()), bd.TIMEOUT),
            (urllib.error.HTTPError("u", 429, "", {}, None), bd.BLOCKED),
            (urllib.error.HTTPError("u", 403, "", {}, None), bd.BLOCKED),
            (urllib.error.HTTPError("u", 404, "", {}, None), bd.CHANGED),
            (urllib.error.HTTPError("u", 502, "", {}, None), bd.OFFLINE),
            (json.JSONDecodeError("x", "doc", 0), bd.INVALID_RESPONSE),
            (KeyError("title"), bd.PARSE_FAILURE),
            (bd.SourceError(bd.BLOCKED, "challenge"), bd.BLOCKED),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                self.assertEqual(bd.classify_error(error)[0], expected)

    def test_challenge_pages(self):
        self.assertTrue(bd.is_challenge("<html><head><title>Just a moment...</title>"))
        self.assertTrue(bd.is_challenge("<script>window._cf_chl_opt={}</script>"))
        # Cloudflare injects this script into ordinary pages; it must not count as a block.
        self.assertFalse(bd.is_challenge('<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'))

    def test_get_raises_blocked_on_challenge(self):
        class Response:
            headers = {}

            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def geturl(self):
                return "https://x"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=Response(b"<title>Just a moment...</title>")):
            with self.assertRaises(bd.SourceError) as caught:
                bd.get("https://x")
        self.assertEqual(caught.exception.status, bd.BLOCKED)
        with mock.patch("urllib.request.urlopen", return_value=Response(b"<html>oops</html>")):
            with self.assertRaises(bd.SourceError) as caught:
                bd.get_json("https://x")
        self.assertEqual(caught.exception.status, bd.INVALID_RESPONSE)

    def test_mirrors_fall_through_and_remember_the_good_one(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            if "dead" in url:
                raise urllib.error.URLError(socket.gaierror(-3, "dns"))
            if "parked" in url:
                return "<html>domain for sale</html>"
            return "<html><div class='search-page'></div></html>"

        bd.SCHEMA["Test"] = {"version": 1, "hosts": ["dead.example", "parked.example", "good.example"], "markers": ["search-page"]}
        self.addCleanup(bd.SCHEMA.pop, "Test")
        self.addCleanup(bd._good_host.pop, "Test", None)
        with mock.patch.object(bd, "get", fake_get):
            _, base = bd.first_page("/q", source="Test", looks_right=bd._markers("Test"))
            self.assertEqual(base, "https://good.example")
            calls.clear()
            bd.first_page("/q", source="Test", looks_right=bd._markers("Test"))
        self.assertEqual(calls, ["https://good.example/q"])  # the mirror that worked is tried first

    def test_every_mirror_failing_reports_the_most_telling_cause(self):
        def fake_get(url, **kwargs):
            if "a." in url:
                raise urllib.error.URLError(socket.gaierror(-3, "dns"))
            raise bd.SourceError(bd.BLOCKED, "anti-bot challenge page")

        bd.SCHEMA["Test"] = {"version": 1, "hosts": ["a.example", "b.example"]}
        self.addCleanup(bd.SCHEMA.pop, "Test")
        with mock.patch.object(bd, "get", fake_get), self.assertRaises(bd.SourceError) as caught:
            bd.first_page("/q", source="Test")
        self.assertEqual(caught.exception.status, bd.BLOCKED)

    def test_source_search_never_raises(self):
        def boom(q):
            raise urllib.error.URLError(socket.timeout())

        outcome = sources.Source("Flaky", boom).search("x")
        self.assertEqual(outcome.status, bd.TIMEOUT)
        self.assertFalse(outcome.ok)


class Health(unittest.TestCase):
    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "t.db")
        sources.configure(History(self.db))
        self.addCleanup(sources.configure, History(None))
        self.addCleanup(sources._memory.clear)

    def ok(self, n=10):
        return sources.Outcome("TPB", sources.OK, [{}] * n, total=n)

    def test_states(self):
        self.assertEqual(sources.next_state({}, self.ok())[0], sources.HEALTHY)
        self.assertEqual(sources.next_state({}, sources.Outcome("TPB", bd.CHANGED, detail="x"))[0], sources.CHANGED)
        self.assertEqual(sources.next_state({}, sources.Outcome("TPB", bd.OFFLINE))[0], sources.OFFLINE)
        timeout = sources.Outcome("TPB", bd.TIMEOUT)
        self.assertEqual(sources.next_state({}, timeout)[0], sources.DEGRADED)  # one slow answer is not "offline"
        self.assertEqual(sources.next_state({"consecutive_failures": 1}, timeout)[0], sources.OFFLINE)
        sloppy = sources.Outcome("TPB", sources.OK, [{}] * 8, total=10, rejected=2)
        self.assertEqual(sources.next_state({}, sloppy)[0], sources.DEGRADED)
        self.assertEqual(sources.next_state({}, self.ok(0), suspicious_empty=True)[0], sources.DEGRADED)

    def test_diagnostics_are_stored(self):
        sources.update_health({"TPB": sources.Outcome("TPB", bd.OFFLINE, detail="name lookup failed", elapsed=0.2)})
        sources.update_health({"TPB": sources.Outcome("TPB", bd.OFFLINE, detail="name lookup failed", elapsed=0.2)})
        row = History(self.db).source_rows()["TPB"]
        self.assertEqual((row["state"], row["consecutive_failures"], row["last_error"]),
                         (sources.OFFLINE, 2, "name lookup failed"))
        self.assertIsNone(row["last_success"])
        sources.update_health({"TPB": self.ok(12)})
        row = History(self.db).source_rows()["TPB"]
        self.assertEqual((row["state"], row["consecutive_failures"], row["last_result_count"]), (sources.HEALTHY, 0, 12))
        self.assertIsNotNone(row["last_success"])
        self.assertEqual(row["schema_version"], bd.SCHEMA["TPB"]["version"])
        table = {r["source"]: r for r in sources.status_table()}
        self.assertEqual(table["TPB"]["state"], sources.HEALTHY)
        self.assertEqual(table["Nyaa"]["state"], sources.UNKNOWN)

    def test_suspicious_zero_results(self):
        empty = sources.Outcome("1337x", bd.NO_RESULTS)
        self.assertTrue(sources.is_suspicious_empty("1337x", empty, 40, {"avg_result_count": 9}))
        self.assertFalse(sources.is_suspicious_empty("1337x", empty, 40, {"avg_result_count": 0.5}))  # never finds much
        self.assertFalse(sources.is_suspicious_empty("1337x", empty, 3, {"avg_result_count": 9}))  # others found little too
        listed_but_unrelated = sources.Outcome("1337x", bd.NO_RESULTS, filtered=20)
        self.assertFalse(sources.is_suspicious_empty("1337x", listed_but_unrelated, 40, {"avg_result_count": 9}))

    def test_filtered_rows_are_counted(self):
        def lister(q, keep=lambda name: True):
            bd._local.parsed = 5
            return []

        outcome = sources.Source("Lister", lister, frozenset({sources.TEXT, sources.DETAIL_PAGES})).search("x", keep=bool)
        self.assertEqual((outcome.status, outcome.filtered), (bd.NO_RESULTS, 5))

    def test_disable_is_remembered(self):
        sources.set_enabled("Nyaa", False)
        self.addCleanup(sources.set_enabled, "Nyaa", True)
        sources.REGISTRY["Nyaa"].enabled = True
        sources.configure(History(self.db))  # a new session reads it back
        self.assertFalse(sources.REGISTRY["Nyaa"].enabled)
        self.assertEqual({r["source"]: r["state"] for r in sources.status_table()}["Nyaa"], sources.DISABLED)


class Overrides(unittest.TestCase):
    def setUp(self):
        self.saved = json.loads(json.dumps(bd.SCHEMA))
        self.addCleanup(lambda: (bd.SCHEMA.clear(), bd.SCHEMA.update(self.saved)))
        self.addCleanup(setattr, sources.REGISTRY["Nyaa"], "enabled", True)
        self.file = Path(tempfile.mkdtemp()) / "sources.json"

    def test_hosts_timeout_and_disable(self):
        self.file.write_text(json.dumps({"version": 1, "sources": {
            "1337x": {"hosts": ["1337x.example", "not a host!", "../etc"], "timeout": 9},
            "Nyaa": {"disabled": True},
            "Bogus": {"hosts": ["x.example"]},
        }}))
        notes = sources.load_overrides(self.file)
        self.assertEqual(bd.SCHEMA["1337x"]["hosts"], ["1337x.example"])
        self.assertEqual(bd.SCHEMA["1337x"]["timeout"], 9)
        self.assertFalse(sources.REGISTRY["Nyaa"].enabled)
        self.assertIn("unknown source 'Bogus' ignored", notes)

    def test_wrong_version_or_garbage_is_ignored(self):
        self.file.write_text(json.dumps({"version": 99, "sources": {"1337x": {"hosts": ["evil.example"]}}}))
        sources.load_overrides(self.file)
        self.assertNotIn("evil.example", bd.SCHEMA["1337x"]["hosts"])
        self.file.write_text("{not json")
        self.assertTrue(sources.load_overrides(self.file)[0].startswith("ignored"))
        self.assertEqual(sources.load_overrides(self.file.with_name("missing.json")), [])


def _row(source, name, h, seeds=10, size=2 << 30):
    return {"source": source, "name": name, "size": size, "seeders": seeds, "leechers": 1,
            "magnet": bd.magnet(h, name)}


class SearchIsolation(unittest.TestCase):
    """Some sources failing, slow or broken never hides the ones that work."""

    def setUp(self):
        search.clear_cache()
        search.configure(History(None))
        self.saved = dict(sources.REGISTRY)
        self.addCleanup(lambda: (sources.REGISTRY.clear(), sources.REGISTRY.update(self.saved)))
        self.addCleanup(sources._memory.clear)
        sources.REGISTRY.clear()
        patcher = mock.patch("chrysa.releases.lookup_titles", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def register(self, name, fn):
        sources.register(sources.Source(name, fn))

    def test_offline_changed_and_malformed_sources_do_not_break_search(self):
        def offline(q):
            raise urllib.error.URLError(socket.gaierror(-3, "dns"))

        def changed(q):
            raise bd.SourceError(bd.CHANGED, "result table not found")

        def malformed(q):
            return json.loads("<html>")

        self.register("Good", lambda q: [_row("Good", "Sintel 2010 1080p WEB-DL x264-GRP", H1)])
        self.register("Also", lambda q: [_row("Also", "Sintel 2010 720p BluRay x264-OTH", H2)])
        self.register("Offline", offline)
        self.register("Changed", changed)
        self.register("Malformed", malformed)
        events = []
        found = search.search("sintel", progress=events.append)
        self.assertEqual(len(found), 2)
        report = search.last_report
        self.assertEqual(report.answered, 2)
        self.assertEqual({n: o.status for n, o in report.outcomes.items()},
                         {"Good": "ok", "Also": "ok", "Offline": bd.OFFLINE, "Changed": bd.CHANGED,
                          "Malformed": bd.INVALID_RESPONSE})
        self.assertEqual(report.health["Changed"]["state"], sources.CHANGED)
        self.assertEqual(events[0], {"kind": "start", "sources": ["Good", "Also", "Offline", "Changed", "Malformed"]})
        self.assertEqual(len([e for e in events if e["kind"] == "source"]), 5)

    def test_every_source_down_is_an_error_not_no_results(self):
        def offline(q):
            raise urllib.error.URLError(socket.gaierror(-3, "dns"))

        self.register("A", offline)
        self.register("B", offline)
        with self.assertRaisesRegex(search.SearchError, "No source could be reached"):
            search.search("sintel")

    def test_empty_results_are_just_empty(self):
        self.register("A", lambda q: [])
        self.assertEqual(search.search("sintel"), [])
        self.assertEqual(search.last_report.outcomes["A"].status, bd.NO_RESULTS)

    def test_slow_source_is_dropped_at_the_deadline(self):
        import threading
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.register("Fast", lambda q: [_row("Fast", "Sintel 2010 1080p WEB-DL", H1)])
        self.register("Slow", lambda q: gate.wait(10) and [])
        with mock.patch.object(search.TorrentSourcesProvider, "DEADLINE", 0.5):
            found = search.search("sintel")
        self.assertEqual(len(found), 1)
        self.assertEqual(search.last_report.outcomes["Slow"].status, bd.TIMEOUT)

    def test_duplicates_across_sources_merge(self):
        self.register("A", lambda q: [_row("A", "Sintel 2010 1080p WEB-DL x264-GRP", H1, seeds=5)])
        self.register("B", lambda q: [_row("B", "Sintel.2010.1080p.WEB-DL.x264-GRP", H1.upper(), seeds=40)])
        (result,) = search.search("sintel")
        self.assertEqual(sorted(result.candidate.sources), ["A", "B"])
        self.assertEqual(result.candidate.seeders, 40)
        self.assertEqual(search.last_report.unique, 1)

    def test_results_are_cached_briefly(self):
        calls = []
        self.register("A", lambda q: calls.append(q) or [_row("A", "Sintel 2010 1080p", H1)])
        search.search("sintel")
        search.search("sintel")
        self.assertEqual(calls, ["sintel"])
        search.clear_cache()
        search.search("sintel")
        self.assertEqual(len(calls), 2)

    def test_failures_are_not_cached(self):
        calls = []

        def flaky(q):
            calls.append(q)
            if len(calls) == 1:
                raise socket.timeout()
            return [_row("A", "Sintel 2010 1080p", H1)]

        self.register("A", flaky)
        with self.assertRaises(search.SearchError):
            search.search("sintel")
        self.assertEqual(len(search.search("sintel")), 1)


@unittest.skipUnless(os.environ.get("CHRYSA_LIVE") == "1", "set CHRYSA_LIVE=1 to check the real sources")
class LiveContract(unittest.TestCase):
    def test_each_source_still_honours_the_contract(self):
        for name, source in sources.REGISTRY.items():
            with self.subTest(source=name):
                outcome = source.health_check()
                self.assertTrue(outcome.ok, f"{name}: {outcome.status} {outcome.detail}")
                self.assertGreaterEqual(outcome.parse_rate, 0.9, outcome.detail)


if __name__ == "__main__":
    unittest.main()
