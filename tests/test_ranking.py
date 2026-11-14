"""Release understanding, intent, safety, de-duplication and the ranking (seeders are not the whole story)."""

import tempfile
import time
import unittest
from pathlib import Path

from chrysa import bd, presentation, ranking, releases
from chrysa.database import Database
from chrysa.history import Evidence, History

H = [f"{i:040x}" for i in range(1, 20)]


def row(name, h, seeds=10, leech=2, size=2 << 30, source="TPB"):
    return {"source": source, "name": name, "size": size, "seeders": seeds, "leechers": leech, "magnet": bd.magnet(h, name)}


def ranked(rows, query, targets=(), history=None):
    return ranking.rank(rows, releases.parse_query(query), list(targets), history or History(None), {})


class Tags(unittest.TestCase):
    def test_reads_only_what_is_written(self):
        tags = ranking.parse_tags("The.Incredible.Hulk.2008.1080p.BluRay.x264.DTS-HD.MA.5.1-FGT")
        self.assertEqual((tags.resolution, tags.source, tags.codec, tags.audio, tags.group),
                         ("1080p", "BluRay", "H.264", "DTS-HD 5.1", "FGT"))
        tags = ranking.parse_tags("Planet.Earth.II.2016.2160p.UHD.BluRay.HDR10.HEVC.TrueHD.Atmos.7.1-WiLDCaT")
        self.assertEqual((tags.resolution, tags.hdr, tags.codec, tags.audio), ("2160p", "HDR10", "HEVC", "Atmos 7.1"))
        tags = ranking.parse_tags("The Office US S03 1080p WEB-DL DDP5.1 H.264-NTb")
        self.assertEqual((tags.source, tags.audio, tags.group), ("WEB-DL", "EAC3 5.1", "NTb"))
        bare = ranking.parse_tags("Some Home Video")
        self.assertEqual(bare, ranking.Tags())  # nothing invented
        self.assertEqual(ranking.parse_tags("Hulk 2003 HDCAM x264").source, "CAM")
        self.assertEqual(ranking.parse_tags("[SubsPlease] Frieren - 05 (1080p) [AB12].mkv").container, "MKV")
        self.assertEqual(ranking.parse_tags("Interstellar (2014) [1080p bluray x264]", "YTS").group, "YTS")

    def test_display_titles(self):
        for name, shown in [
            ("The.Incredible.Hulk.2008.1080p.BluRay.x264-FGT", "The Incredible Hulk (2008)"),
            ("Breaking Bad S02E03 720p HDTV x264-KILLERS [eztv]", "Breaking Bad · S02E03"),
            ("The Office US S03 1080p WEB-DL", "The Office US · Season 3"),
            ("Breaking.Bad.S01-S05.Complete.1080p", "Breaking Bad · Seasons 1–5"),
        ]:
            self.assertEqual(ranking.display_title(name, releases.classify(name)), shown)


class Intent(unittest.TestCase):
    def test_from_query_and_titles(self):
        q = releases.parse_query
        movie = releases.Title("Interstellar", 2014, False)
        show = releases.Title("The Office", 2005, True)
        self.assertEqual(ranking.detect_intent(q("Interstellar"), [movie]), ranking.INTENT_MOVIE)
        self.assertEqual(ranking.detect_intent(q("Breaking Bad S02E03"), []), ranking.INTENT_EPISODE)
        self.assertEqual(ranking.detect_intent(q("The Office season 3"), [show]), ranking.INTENT_SEASON)
        self.assertEqual(ranking.detect_intent(q("Planet Earth 1080p"), []), ranking.INTENT_DOC)
        self.assertEqual(ranking.detect_intent(q("hulk"), []), ranking.INTENT_ANY)  # unsure: mixed results
        mixed = [releases.Title("Hulk", 2003, False), releases.Title("She-Hulk", 2022, True)]
        self.assertEqual(ranking.detect_intent(q("hulk"), mixed), ranking.INTENT_ANY)

    def test_exact_episode_beats_season_pack_for_an_episode_search(self):
        rows = [row("Breaking Bad S02 1080p BluRay x264-GRP", H[0], seeds=400, size=30 << 30),
                row("Breaking Bad S02E03 1080p BluRay x264-GRP", H[1], seeds=60, size=2 << 30)]
        result = ranked(rows, "breaking bad s02e03")
        self.assertEqual(result.candidates[0].release.kind, releases.EPISODE)

    def test_mixed_results_when_unsure(self):
        rows = [row("Hulk 2003 1080p BluRay x264", H[0]), row("Hulk S01E01 720p WEB", H[1])]
        self.assertEqual(len(ranked(rows, "hulk").candidates), 2)


class Safety(unittest.TestCase):
    def signals(self, name, size=2 << 30):
        release = releases.classify(name)
        return ranking.safety_signals(name, release.kind if release else "other", size, ranking.parse_tags(name))

    def test_clean_media(self):
        self.assertEqual(self.signals("Interstellar 2014 1080p BluRay x264-SPARKS"), [])
        self.assertEqual(self.signals("Windows on the World 2019 1080p WEB-DL"), [])  # one word is not a verdict

    def test_suspicious(self):
        self.assertIn("mentions a password", self.signals("Interstellar 2014 1080p password in txt"))
        self.assertIn("has a double extension", self.signals("Interstellar.2014.1080p.mkv.exe"))
        self.assertIn("looks like software, not a video", self.signals("Photo Editor v2.3.1 x64 portable"))
        self.assertIn("is far too small for its claimed quality", self.signals("Interstellar 2014 1080p BluRay", 50 << 20))
        self.assertIn("is a sample/trailer, not the full video", self.signals("Interstellar 2014 1080p Sample"))

    def test_unsafe_results_are_hidden_and_counted(self):
        rows = [row("Interstellar 2014 1080p BluRay x264", H[0]),
                row("Interstellar.2014.1080p.mkv.exe", H[1], seeds=900),
                row("Interstellar 2014 Keygen", H[2])]
        result = ranked(rows, "interstellar")
        self.assertEqual([c.info_hash for c in result.candidates], [H[0]])
        self.assertEqual((result.unsafe, result.unrelated), (1, 1))


class Ranking(unittest.TestCase):
    def setUp(self):
        self.db = Database(Path(tempfile.mkdtemp()) / "t.db")
        self.history = History(self.db)

    def remember(self, h, speed, status="verified", source="TPB", group=""):
        row_id = self.history.start_download({"info_hash": h, "source": source, "release_group": group})
        self.history.finish_download(row_id, status, avg_speed=speed)

    def test_seeders_alone_do_not_decide(self):
        """1000 seeders that crawled before lose to 25 seeders that flew."""
        crowded, quick = H[0], H[1]
        rows = [row("Sintel 2010 1080p WEB-DL x264-AAA", crowded, seeds=1000),
                row("Sintel 2010 1080p WEB-DL x264-BBB", quick, seeds=25)]
        self.assertEqual(ranked(rows, "sintel").candidates[0].info_hash, crowded)  # no history: the swarm decides
        for _ in range(3):
            self.remember(crowded, 30 * 1024)
            self.remember(quick, 8 << 20)
        best = ranked(rows, "sintel", history=self.history).candidates[0]
        self.assertEqual(best.info_hash, quick)
        self.assertEqual(best.performance, "fast")
        self.assertTrue(any("Fast in past downloads" in text for _, text in best.reasons))

    def test_group_and_source_history_also_count(self):
        for _ in range(4):
            self.remember(H[9], 6 << 20, group="FASTGRP")
        evidence = self.history.evidence("", "Nyaa", "FASTGRP")
        self.assertEqual(evidence.scope, "group")
        self.assertGreater(evidence.speed, 5 << 20)
        self.assertGreater(evidence.confidence, 0.3)
        self.assertEqual(History(None).evidence(H[9]), Evidence())

    def test_failed_downloads_lower_success(self):
        self.remember(H[3], 1 << 20, status="stalled")
        self.remember(H[3], 1 << 20, status="rejected")
        self.assertEqual(self.history.evidence(H[3]).success, 0.0)
        self.assertEqual(self.history.source_reliability()["TPB"], 2 / 5)

    def test_verified_before(self):
        self.remember(H[4], 2 << 20)
        self.assertTrue(self.history.was_verified(H[4]))
        (c,) = ranked([row("Sintel 2010 1080p WEB-DL", H[4])], "sintel", history=self.history).candidates
        self.assertTrue(c.evidence.verified_before)
        self.assertIn(("✓ Verified before", presentation.GOOD), presentation.status_segments(c))

    def test_quality_and_cam(self):
        rows = [row("Sintel 2010 HDCAM x264", H[0], seeds=500), row("Sintel 2010 1080p BluRay x264", H[1], seeds=40)]
        result = ranked(rows, "sintel")
        self.assertEqual(result.candidates[0].info_hash, H[1])
        cam = result.candidates[1]
        self.assertTrue(any(tone == "bad" and "CAM" in text for tone, text in cam.reasons))

    def test_reuploads_collapse_and_become_fallbacks(self):
        rows = [row("Sintel 2010 1080p WEB-DL x264-GRP", H[0], seeds=50, size=2_000_000_000),
                row("Sintel 2010 1080p WEB-DL x264-GRP", H[1], seeds=3, size=2_005_000_000),
                row("Sintel 2010 1080p BluRay x264-OTHER", H[2], seeds=20, size=3_000_000_000)]
        result = ranked(rows, "sintel")
        self.assertEqual(result.unique, 2)
        best = result.candidates[0]
        self.assertEqual(best.info_hash, H[0])
        self.assertEqual(best.alternatives[0], rows[1]["magnet"])  # the re-upload first
        self.assertIn(rows[2]["magnet"], best.alternatives)  # then another 1080p release of the same film

    def test_reasons_and_scores_are_complete(self):
        (c,) = ranked([row("Sintel 2010 1080p WEB-DL x264 AAC-GRP", H[0], seeds=24, leech=8)], "sintel").candidates
        for key in ("media_match", "quality", "safety", "source", "availability", "performance", "metadata_confidence",
                    "overall"):
            self.assertIn(key, c.scores)
        texts = [t for _, t in c.reasons]
        self.assertIn("Healthy swarm (24 seeds, 8 peers)", texts)
        self.assertIn("No suspicious signs in the release name", texts)
        self.assertEqual(c.swarm, "healthy")

    def test_unknown_swarm_is_not_shown_as_dead(self):
        r = row("Sintel 2010 1080p", H[0])
        r["seeders"] = r["leechers"] = None
        (c,) = ranked([r], "sintel").candidates
        self.assertEqual(c.swarm, "")
        self.assertFalse(any("seeds" in text for text, _ in presentation.status_segments(c)))

    def test_old_thin_torrents_are_discounted(self):
        old = row("Sintel 2010 1080p WEB", H[0], seeds=4, size=1 << 30)
        new = row("Sintel 2010 1080p WEB", H[1], seeds=4, size=2 << 30)
        old["published"] = int(time.time()) - 6 * 365 * 86400
        result = {c.info_hash: c for c in ranked([old, new], "sintel").candidates}
        self.assertLess(result[H[0]].scores["availability"], result[H[1]].scores["availability"])


class Presentation(unittest.TestCase):
    def test_lines_show_only_known_fields(self):
        (c,) = ranked([row("Sintel 2010 1080p WEB-DL x264 AAC-GRP", H[0], seeds=18, leech=6, source="LimeTorrents")],
                      "sintel").candidates
        self.assertEqual(presentation.spec_line(c), "Movie • 1080p • WEB-DL • H.264 • AAC")
        texts = [t for t, _ in presentation.status_segments(c)]
        self.assertEqual(texts, ["2.0 GB", "● Healthy", "18 seeds / 6 peers", "LimeTorrents"])
        html = presentation.detail_html(c, dark=True)
        self.assertIn("WHY THIS RESULT", html)
        self.assertIn("Strong media match", html) if c.relevance >= 4 else self.assertIn("Title matches", html)
        self.assertNotIn("Container", html)  # unknown, so not shown

    def test_progress_and_sources_lines(self):
        line = presentation.progress_line(6, {"A": "ok", "B": "ok", "C": "no results", "D": "changed", "E": "offline"})
        self.assertEqual(line, "Searching 6 sources…   ✓ 3   ⚠ 1   ✕ 1")
        text, tone = presentation.sources_button_text({"A": {"state": "healthy"}, "B": {"state": "offline"},
                                                       "C": {"state": "disabled"}})
        self.assertEqual((text, tone), ("Sources 1/2", presentation.WARN))


if __name__ == "__main__":
    unittest.main()
