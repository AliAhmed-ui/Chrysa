"""Release classification, query understanding and the torrent file guard behind Browse."""

import unittest

from chrysa import releases, torrentguard
from chrysa.releases import EPISODE, MOVIE, SEASON, Title, classify, parse_query, relevance, target_titles


class Classify(unittest.TestCase):
    def test_movies_seasons_episodes(self):
        self.assertEqual(classify("Inception (2010) 1080p BrRip x264 - YIFY").kind, MOVIE)
        pack = classify("Breaking.Bad.S02.1080p.BluRay.x265")
        self.assertEqual((pack.kind, pack.season, pack.title), (SEASON, 2, "breaking bad"))
        self.assertEqual(classify("Game of Thrones Season 1-8 1080p").last_season, 8)
        self.assertEqual(classify("Breaking Bad Complete Series 1080p").kind, SEASON)
        episode = classify("The Office Season 8 Episode 19 DVDRip FiCO")
        self.assertEqual((episode.kind, episode.season, episode.episode), (EPISODE, 8, 19))

    def test_piracy_tool_words_are_refused(self):
        for name in ("Photoshop 2024 Crack", "Some Game 2020 PC repack Fitgirl", "Win10 Activator 2020", "Keygen 2021 1080p"):
            self.assertIsNone(classify(name), name)

    def test_unmarked_titles_are_kept_as_other(self):
        self.assertEqual(classify("Inception").kind, releases.OTHER)

    def test_real_titles_with_scary_words_survive(self):
        self.assertEqual(classify("Office Space 1999 1080p BluRay").kind, MOVIE)
        self.assertEqual(classify("Patch Adams 1998 720p BluRay").kind, MOVIE)


class Understanding(unittest.TestCase):
    def test_query_qualifiers(self):
        query = parse_query("Breaking Bad s02")
        self.assertEqual((query.text, query.season), ("Breaking Bad", 2))
        self.assertEqual(parse_query("dune 2021").year, 2021)

    def test_partial_title_expands_and_filters(self):
        titles = [Title("Breaking Bad", 2008, True), Title("Breaking Away", 1979, False), Title("Dune", 2021, False)]
        query = parse_query("breaking")
        targets = target_titles(query, titles)
        self.assertEqual([t.name for t in targets], ["Breaking Bad", "Breaking Away"])
        good, spin_off = classify("Breaking Bad S01 1080p"), classify("Breaking Bad Parody 2015 720p BluRay")
        self.assertGreater(relevance(good, query, targets), relevance(spin_off, query, targets))
        self.assertEqual(relevance(classify("Better Call Saul S01 1080p"), query, targets), 0)

    def test_season_qualifier_rejects_other_seasons_and_movies(self):
        query = parse_query("breaking bad s02")
        targets = [Title("Breaking Bad", 2008, True)]
        self.assertGreater(relevance(classify("Breaking Bad S02 1080p"), query, targets), 0)
        self.assertEqual(relevance(classify("Breaking Bad S03 1080p"), query, targets), 0)
        self.assertGreater(relevance(classify("Breaking Bad S01-S05 1080p"), query, targets), 0)


class Ranking(unittest.TestCase):
    def test_english_before_foreign_whatever_the_seeders(self):
        english, italian = "Inception 2010 English XViD", "Inception 2010 Italian DVDRip"
        keys = [releases.rank_key(classify(n), 4, seeds, n) for n, seeds in ((english, 10), (italian, 5000))]
        self.assertGreater(keys[0], keys[1])
        self.assertFalse(releases.is_foreign("Movie 2010 ENG-ITA 1080p"))
        self.assertTrue(releases.is_foreign("Movie 2010 [French] DVDRip"))


class Guard(unittest.TestCase):
    def test_only_video_and_companions_are_wanted(self):
        paths = ["Film/film.mkv", "Film/setup.exe", "Film/film.srt", "Film/run.bat", "../evil.mkv"]
        self.assertEqual(torrentguard.wanted_indices(paths), [0, 2])

    def test_no_video_means_refused(self):
        with self.assertRaises(torrentguard.UnsafeTorrent):
            torrentguard.wanted_indices(["a/setup.exe", "a/readme.txt"])

    def test_bencode_file_list(self):
        data = b"d4:infod4:name4:Film5:filesld6:lengthi1e4:pathl8:film.mkveed6:lengthi1e4:pathl9:setup.exeeeeee"
        self.assertEqual(torrentguard.file_paths(data), ["Film/film.mkv", "Film/setup.exe"])


if __name__ == "__main__":
    unittest.main()
