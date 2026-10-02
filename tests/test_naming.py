"""Release-name parsing: the guessit glue in theia.naming."""

import unittest

from theia import naming


class MovieNames(unittest.TestCase):
    def check(self, name, title, year):
        parsed = naming.parse_dir(name)
        self.assertEqual((parsed.title, parsed.year), (title, year), name)

    def test_folder_names_with_release_tags(self):
        self.check("Heat (1995) [1080p]", "Heat", 1995)
        self.check("Tenet (2020) [1080p] [BluRay] [5.1] [YTS.MX]", "Tenet", 2020)
        self.check("An Affair To Remember (1957) [BluRay] [1080p] [YTS.AM]", "An Affair To Remember", 1957)
        self.check("Backrooms (2026) [1080p] [WEBRip] [5.1] [YTS.GG - YTS.BZ]", "Backrooms", 2026)
        self.check("Django Unchained (2012) [1080p]", "Django Unchained", 2012)

    def test_number_in_title_is_not_an_episode(self):
        self.check("Blade Runner 2049 (2017) [1080p] [YTS.AG]", "Blade Runner 2049", 2017)
        self.check("1917 (2019)", "1917", 2019)
        self.check("2001 A Space Odyssey (1968) [1080p]", "2001 A Space Odyssey", 1968)

    def test_punctuation_is_kept_for_title_year_names(self):
        self.check("When Harry Met Sally... (1989) [BluRay] [1080p] [YTS.AM]", "When Harry Met Sally...", 1989)
        self.check("Mission - Impossible (1996)", "Mission - Impossible", 1996)

    def test_scene_style_file_names(self):
        for name, title, year in [
            ("Heat.1995.1080p.BRrip.x264.YIFY.mp4", "Heat", 1995),
            ("Blade.Runner.2049.2017.1080p.BluRay.x264-[YTS.AG].mp4", "Blade Runner 2049", 2017),
            ("When.Harry.Met.Sally....1989.1080p.BluRay.x264-[YTS.AM].mp4", "When Harry Met Sally", 1989),
            ("Backrooms.2026.1080p.WEBRip.x264.AAC5.1-[YTS.GG - YTS.BZ].mp4", "Backrooms", 2026),
        ]:
            parsed = naming.parse(name)
            self.assertEqual((parsed.title, parsed.year), (title, year), name)
            self.assertTrue(parsed.technical, name)
            self.assertFalse(parsed.is_episode, name)

    def test_plain_show_folder_names(self):
        for name in ("Friends", "BEEF", "The 100", "24", "9-1-1 Lone Star"):
            self.assertEqual(naming.parse_dir(name).title, name)


class EpisodeNames(unittest.TestCase):
    def test_scene_episode(self):
        parsed = naming.parse(
            "Friends.S01E01.The.One.where.Monica.Gets.a.New.Roomate.720p.BluRay.2CH.x265.HEVC-PSA.mkv", "episode"
        )
        self.assertEqual(parsed.title, "Friends")
        self.assertEqual((parsed.season, parsed.episodes), (1, (1,)))
        self.assertEqual(parsed.episode_title, "The One where Monica Gets a New Roomate")
        self.assertTrue(parsed.is_episode)

    def test_dash_separated_episode_with_year(self):
        parsed = naming.parse(
            "BEEF (2023) - S01E01 - The Birds Don't Sing, They Screech in Pain (1080p NF WEB-DL x265 Silence).mkv",
            "episode",
        )
        self.assertEqual((parsed.title, parsed.year, parsed.season, parsed.episode), ("BEEF", 2023, 1, 1))
        self.assertEqual(parsed.episode_title, "The Birds Don't Sing, They Screech in Pain")

    def test_episode_without_title(self):
        parsed = naming.parse("This.Is.Us.S01E07.720p.AMZN.WEBRip.x264-GalaxyTV.mkv", "episode")
        self.assertEqual((parsed.title, parsed.season, parsed.episode), ("This Is Us", 1, 7))
        self.assertIsNone(parsed.episode_title)

    def test_double_episodes(self):
        for name, expected in [
            ("Friends.S09E23-E24.The.One.In.Barbados.720p.BluRay.2CH.x265.HEVC-PSA.mkv", (23, 24)),
            ("Friends.S10E17-18.The.Last.One.720p.BluRay.2CH.x265.HEVC-PSA.mkv", (17, 18)),
            ("Show.S01E01E02.mkv", (1, 2)),
        ]:
            parsed = naming.parse(name, "episode")
            self.assertEqual(parsed.episodes, expected, name)
            self.assertEqual((parsed.episode, parsed.episode_end), expected, name)

    def test_part_number_is_appended_to_the_title(self):
        parsed = naming.parse("Friends.S04E23.The.One.with.Ross's.Wedding.Part1.720p.BluRay.2CH.x265.HEVC-PSA.mkv", "episode")
        self.assertEqual(parsed.episode_title, "The One with Ross's Wedding (Part 1)")

    def test_alternative_markers(self):
        self.assertTrue(naming.parse("Show - 1x02 - Title.mkv").is_episode)
        absolute = naming.parse("[SubsPlease] Sousou no Frieren - 05 (1080p) [ABCD1234].mkv")
        self.assertEqual((absolute.marker, absolute.episodes), ("absolute", (5,)))
        self.assertTrue(absolute.is_numbered)
        self.assertFalse(absolute.is_episode)  # no season: not trusted on its own
        leading = naming.parse("01 - Pilot.mkv")
        self.assertEqual((leading.marker, leading.is_numbered), ("leading", True))


class NotEpisodes(unittest.TestCase):
    """Names guessit would happily turn into episodes but which are nothing of the sort."""

    def test_recordings_named_after_dates(self):
        for name in (
            "Screencast From 2026-09-08 18-45-12.mp4",
            "2025-09-08 18-45-12.mkv",
            "WhatsApp Video 2025-09-08 at 18.45.12.mp4",
            "OBS 2025-01-02 10-11-12.mkv",
            "VID_20250908_184512.mp4",
        ):
            parsed = naming.parse(name)
            self.assertTrue(parsed.dated, name)
            self.assertFalse(parsed.is_episode, name)
            self.assertFalse(parsed.is_numbered, name)
            self.assertEqual(parsed.episodes, (), name)

    def test_resolution_is_not_a_1x02_marker(self):
        self.assertEqual(naming.parse("SampleVideo_1280x720_1mb.mp4").marker, "")

    def test_untagged_names(self):
        for name in ("ES_Project_Demo.mp4", "Inception.mkv", "clip 1.mp4", "Lecture 12.mp4"):
            parsed = naming.parse(name)
            self.assertFalse(parsed.is_episode, name)
            self.assertFalse(parsed.technical, name)
            self.assertIsNone(parsed.year, name)


class Flags(unittest.TestCase):
    def test_sample_and_trailer(self):
        self.assertTrue(naming.parse("Friends.Sample.mkv").sample)
        self.assertTrue(naming.parse("sample.mp4").sample)
        self.assertFalse(naming.parse("SampleVideo_1280x720_1mb.mp4").sample)
        self.assertTrue(naming.parse("Movie.Trailer.mp4").extra)
        self.assertFalse(naming.parse("Heat.1995.1080p.BRrip.x264.YIFY.mp4").sample)


class SeasonFolders(unittest.TestCase):
    def test_season_folder_names(self):
        for name, number in [
            ("Season 1", 1), ("season1", 1), ("S01", 1), ("s1", 1), ("Season 10", 10),
            ("Season.02", 2), ("Series 3", 3), ("Season 01 (1080p BluRay x265)", 1),
            ("Specials", 0), ("Season 0", 0),
        ]:
            self.assertEqual(naming.season_of_dir(name), number, name)

    def test_folders_that_are_not_seasons(self):
        for name in ("Subs", "Extras", "Sherlock", "S.W.A.T", "Se7en (1995)", "S01E01 - Pilot", "Featurettes", "S1mple"):
            self.assertIsNone(naming.season_of_dir(name), name)

    def test_season_folder_carrying_the_show_name(self):
        key = naming.show_key("Friends")
        self.assertEqual(naming.season_of_dir("Friends Season 2", key), 2)
        self.assertEqual(naming.season_of_dir("Friends.S03.720p.BluRay.x265-PSA", key), 3)
        # Another show's pack sitting in this folder is not one of its seasons.
        self.assertIsNone(naming.season_of_dir("This.Is.Us.S01.COMPLETE.720p.AMZN.WEBRip.x264-GalaxyTV[TGx]", key))

    def test_season_pack_number(self):
        self.assertEqual(naming.season_pack_number("This.Is.Us.S01.COMPLETE.720p.AMZN.WEBRip.x264-GalaxyTV[TGx]"), 1)
        self.assertEqual(naming.season_pack_number("Friends Season 2"), 2)
        self.assertIsNone(naming.season_pack_number("Heat (1995) [1080p]"))
        self.assertIsNone(naming.season_pack_number("Show.S01E01.1080p.WEB-GRP"))


class Helpers(unittest.TestCase):
    def test_clean_stem(self):
        self.assertEqual(naming.clean_stem("ES_Project_Demo"), "ES Project Demo")
        self.assertEqual(naming.clean_stem("some.dotted.name"), "some dotted name")
        self.assertEqual(naming.clean_stem("Screencast From 2026-09-08 18-45-12"), "Screencast From 2026-09-08 18-45-12")

    def test_show_key_ignores_case_and_punctuation(self):
        self.assertEqual(naming.show_key("This.Is.Us"), naming.show_key("this is us"))
        self.assertEqual(naming.show_key("BEEF"), "beef")

    def test_sort_title_drops_leading_article(self):
        self.assertEqual(naming.sort_title("The Invite"), "invite")
        self.assertEqual(naming.sort_title("An Affair To Remember"), "affair to remember")
        self.assertEqual(naming.sort_title("Tenet"), "tenet")

    def test_strip_show_prefix(self):
        self.assertEqual(naming.strip_show_prefix("Friends The Reunion", "Friends"), "The Reunion")
        self.assertEqual(naming.strip_show_prefix("Pilot", "Friends"), "Pilot")

    def test_cache_round_trip(self):
        naming.clear_cache()
        first = naming.parse("Heat.1995.1080p.BRrip.x264.YIFY.mp4")
        rows = list(naming.drain_new_entries())
        self.assertEqual(len(rows), 1)
        naming.clear_cache()
        self.assertEqual(naming.load_cache(rows), 1)
        self.assertEqual(naming.parse("Heat.1995.1080p.BRrip.x264.YIFY.mp4"), first)
        self.assertEqual(list(naming.drain_new_entries()), [])  # served from cache, nothing new


if __name__ == "__main__":
    unittest.main()
