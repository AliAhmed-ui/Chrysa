"""Folder classification, on synthetic trees shaped like real download folders."""

import os
import unittest
from collections import Counter

from tests.helpers import MB, TempDirCase, make_tree
from chrysa.scanner import KIND_EPISODE, KIND_MOVIE, KIND_OTHER, Scanner

GB = 1024 * MB


class ScannerCase(TempDirCase):
    def scan(self, *roots):
        result = Scanner().scan(roots or [self.media])
        self.by_name = {os.path.basename(v.path): v for v in result.videos}
        return result

    def titles(self, result, kind):
        return sorted(v.title for v in result.of_kind(kind))

    def shows(self, result):
        """``{show title: {season: episode count}}``"""
        shows: dict[str, Counter] = {}
        for video in result.of_kind(KIND_EPISODE):
            shows.setdefault(video.show_title, Counter())[video.season] += 1
        return {title: dict(sorted(seasons.items())) for title, seasons in shows.items()}


class MovieFolders(ScannerCase):
    def test_release_folders_become_movies_with_clean_titles(self):
        make_tree(self.media, {
            "Heat (1995) [1080p]/Heat.1995.1080p.BRrip.x264.YIFY.mp4": 2 * GB,
            "Heat (1995) [1080p]/Heat.1995.1080p.BRrip.x264.YIFY.srt": b"1\n",
            "Heat (1995) [1080p]/WWW.YIFY-TORRENTS.COM.jpg": 130_000,
            "Blade Runner 2049 (2017) [1080p] [YTS.AG]/Blade.Runner.2049.2017.1080p.BluRay.x264-[YTS.AG].mp4": 2 * GB,
            "Tenet (2020) [1080p] [BluRay] [5.1] [YTS.MX]/Tenet.2020.1080p.BluRay.x264.AAC5.1-[YTS.MX].mp4": 2 * GB,
            "An Affair To Remember (1957) [BluRay] [1080p] [YTS.AM]/An.Affair.To.Remember.1957.1080p.BluRay.x264-[YTS.AM].mp4": GB,
            "When Harry Met Sally... (1989) [BluRay] [1080p] [YTS.AM]/When.Harry.Met.Sally....1989.1080p.BluRay.x264-[YTS.AM].mp4": GB,
        })
        result = self.scan()
        movies = {(v.title, v.year) for v in result.of_kind(KIND_MOVIE)}
        self.assertEqual(movies, {
            ("Heat", 1995), ("Blade Runner 2049", 2017), ("Tenet", 2020),
            ("An Affair To Remember", 1957), ("When Harry Met Sally...", 1989),
        })
        self.assertEqual(len(result.videos), 5)
        self.assertFalse(any(v.tentative for v in result.videos))

    def test_samples_trailers_and_promos_inside_a_release_are_ignored(self):
        make_tree(self.media, {
            "Movie (2019) [1080p]/Movie.2019.1080p.BluRay.x264-GRP.mkv": 2 * GB,
            "Movie (2019) [1080p]/sample.mkv": 20 * MB,
            "Movie (2019) [1080p]/Movie.2019.Trailer.mp4": 30 * MB,
            "Movie (2019) [1080p]/RARBG.com.mp4": 1 * MB,
            "Movie (2019) [1080p]/Sample/movie-sample.mkv": 25 * MB,
            "Movie (2019) [1080p]/Subs/English.srt": b"1\n",
            "Movie (2019) [1080p]/RARBG.txt": b"hello",
        })
        result = self.scan()
        self.assertEqual([(v.kind, v.title, v.year) for v in result.videos], [(KIND_MOVIE, "Movie", 2019)])
        reasons = {os.path.basename(p): why for p, why in result.ignored}
        self.assertEqual(reasons.get("sample.mkv"), "sample")
        self.assertEqual(reasons.get("Movie.2019.Trailer.mp4"), "trailer/extra")
        self.assertEqual(reasons.get("RARBG.com.mp4"), "smaller companion file")
        self.assertNotIn("movie-sample.mkv", self.by_name)

    def test_title_falls_back_to_the_file_when_the_folder_says_nothing(self):
        make_tree(self.media, {"New Folder 1080p/Inception.2010.1080p.BluRay.x264.mkv": GB})
        result = self.scan()
        self.assertEqual([(v.title, v.year) for v in result.of_kind(KIND_MOVIE)], [("Inception", 2010)])

    def test_collection_folder_lists_every_feature(self):
        make_tree(self.media, {
            "Trilogy (1999-2003) 1080p/The.Matrix.1999.1080p.BluRay.mkv": 2 * GB,
            "Trilogy (1999-2003) 1080p/The.Matrix.Reloaded.2003.1080p.BluRay.mkv": 2 * GB,
            "Trilogy (1999-2003) 1080p/The.Matrix.Revolutions.2003.1080p.BluRay.mkv": 2 * GB,
        })
        result = self.scan()
        self.assertEqual(self.titles(result, KIND_MOVIE), ["The Matrix", "The Matrix Reloaded", "The Matrix Revolutions"])

    def test_empty_folders_produce_nothing(self):
        (self.media / "Climax (2018) [BluRay] [1080p] [YTS.AM]").mkdir()
        (self.media / "Camera").mkdir()
        make_tree(self.media, {"Notes (2020)/readme.txt": b"x"})
        self.assertEqual(self.scan().videos, [])


class ShowFolders(ScannerCase):
    def test_show_with_season_subfolders(self):
        make_tree(self.media, {
            "Friends/Info.txt": b"x",
            "Friends/Friends.Sample.mkv": 12 * MB,
            "Friends/S01/Friends.S01E01.The.One.where.Monica.Gets.a.New.Roomate.720p.BluRay.2CH.x265.HEVC-PSA.mkv": 200 * MB,
            "Friends/S01/Friends.S01E02.The.One.With.the.Sonogram.at.the.End.720p.BluRay.2CH.x265.HEVC-PSA.mkv": 200 * MB,
            "Friends/S09/Friends.S09E23-E24.The.One.In.Barbados.720p.BluRay.2CH.x265.HEVC-PSA.mkv": 400 * MB,
            "Friends/S10/Friends.S10E17-18.The.Last.One.720p.BluRay.2CH.x265.HEVC-PSA.mkv": 400 * MB,
            "BEEF/Season 1/BEEF (2023) - S01E01 - The Birds Don't Sing, They Screech in Pain (1080p NF WEB-DL x265 Silence).mkv": GB,
            "BEEF/Season 2/BEEF.S02E01.1080p.WEB.h264-ETHEL.mkv": GB,
            "BEEF/Season 2/BEEF.S02E03.1080p.WEB.h264-ETHEL[EZTVx.to].mkv": GB,
            "BEEF/Season 2/BEEF S02E08 It Will Stay This Way and You Will Obey 1080p NF WEB-DL DDP5 1 Atmos H 264-FLUX.mkv": GB,
        })
        result = self.scan()
        self.assertEqual(self.shows(result), {"Friends": {1: 2, 9: 1, 10: 1}, "BEEF": {1: 1, 2: 3}})
        self.assertEqual(result.of_kind(KIND_MOVIE) + result.of_kind(KIND_OTHER), [])
        self.assertIn(("sample"), [why for _p, why in result.ignored])

        pilot = self.by_name["Friends.S01E01.The.One.where.Monica.Gets.a.New.Roomate.720p.BluRay.2CH.x265.HEVC-PSA.mkv"]
        self.assertEqual((pilot.season, pilot.episode, pilot.title), (1, 1, "The One where Monica Gets a New Roomate"))
        self.assertEqual(pilot.show_dir, str(self.media / "Friends"))
        double = self.by_name["Friends.S09E23-E24.The.One.In.Barbados.720p.BluRay.2CH.x265.HEVC-PSA.mkv"]
        self.assertEqual((double.episode, double.episode_end), (23, 24))
        untitled = self.by_name["BEEF.S02E01.1080p.WEB.h264-ETHEL.mkv"]
        self.assertEqual((untitled.title, untitled.show_year), ("Episode 1", 2023))
        titled = self.by_name["BEEF S02E08 It Will Stay This Way and You Will Obey 1080p NF WEB-DL DDP5 1 Atmos H 264-FLUX.mkv"]
        self.assertEqual(titled.title, "It Will Stay This Way and You Will Obey")

    def test_season_folder_spellings(self):
        make_tree(self.media, {
            "Show/Season 1/Show.S01E01.mkv": 300 * MB,
            "Show/season2/Show.S02E01.mkv": 300 * MB,
            "Show/S03/Show.S03E01.mkv": 300 * MB,
            "Show/Season 04 (1080p BluRay x265)/Show.S04E01.mkv": 300 * MB,
            "Show/Show Season 5/Show.S05E01.mkv": 300 * MB,
            "Show/Specials/Show.S00E01.Behind.The.Scenes.mkv": 300 * MB,
        })
        self.assertEqual(self.shows(self.scan()), {"Show": {0: 1, 1: 1, 2: 1, 3: 1, 4: 1, 5: 1}})

    def test_season_number_comes_from_the_folder_when_files_lack_it(self):
        make_tree(self.media, {
            "Some Show/Season 2/01 - Pilot.mkv": 300 * MB,
            "Some Show/Season 2/02 - The Second One.mkv": 300 * MB,
        })
        result = self.scan()
        self.assertEqual(self.shows(result), {"Some Show": {2: 2}})
        self.assertEqual(sorted((v.episode, v.title) for v in result.videos), [(1, "Pilot"), (2, "The Second One")])

    def test_loose_video_beside_season_folders_is_a_special(self):
        make_tree(self.media, {
            "Friends/S01/Friends.S01E01.Pilot.mkv": 200 * MB,
            "Friends/Friends.The.Reunion.2021.1080p.WEB.h264.mkv": GB,
        })
        self.scan()
        special = self.by_name["Friends.The.Reunion.2021.1080p.WEB.h264.mkv"]
        self.assertEqual((special.kind, special.season, special.episode, special.title), (KIND_EPISODE, 0, None, "The Reunion"))

    def test_single_season_scene_pack(self):
        pack = "This.Is.Us.S01.COMPLETE.720p.AMZN.WEBRip.x264-GalaxyTV[TGx]"
        make_tree(self.media, {
            f"{pack}/[TGx]Downloaded from torrentgalaxy.to .txt": b"x",
            **{f"{pack}/This.Is.Us.S01E{n:02d}.720p.AMZN.WEBRip.x264-GalaxyTV.mkv": 300 * MB for n in range(1, 19)},
        })
        result = self.scan()
        self.assertEqual(self.shows(result), {"This Is Us": {1: 18}})
        self.assertEqual({v.title for v in result.videos}, {f"Episode {n}" for n in range(1, 19)})

    def test_packs_of_one_show_in_separate_folders_merge(self):
        make_tree(self.media, {
            "Show.S01.1080p.WEB-DL.x264-GRP/Show.S01E01.1080p.WEB-DL.x264-GRP.mkv": GB,
            "Show.S02.1080p.WEB-DL.x264-GRP/Show.S02E01.1080p.WEB-DL.x264-GRP.mkv": GB,
            "show.s03e01.720p.hdtv.x264-grp.mkv": GB,
        })
        result = self.scan()
        self.assertEqual(self.shows(result), {"Show": {1: 1, 2: 1, 3: 1}})
        self.assertEqual(len({v.show_key for v in result.videos}), 1)

    def test_flat_folder_of_episodes_named_after_the_show(self):
        make_tree(self.media, {
            "Chernobyl (2019)/Chernobyl.S01E01.1.23.45.1080p.BluRay.mkv": GB,
            "Chernobyl (2019)/Chernobyl.S01E02.Please.Remain.Calm.1080p.BluRay.mkv": GB,
            "Chernobyl (2019)/Chernobyl.S01E03.1080p.BluRay.mkv": GB,
        })
        result = self.scan()
        self.assertEqual(self.shows(result), {"Chernobyl": {1: 3}})
        self.assertEqual({v.show_year for v in result.videos}, {2019})

    def test_numbered_files_in_a_plain_folder_form_a_series(self):
        make_tree(self.media, {
            "Python Course/01 - Introduction.mp4": 100 * MB,
            "Python Course/02 - Variables.mp4": 100 * MB,
            "Python Course/03 - Functions.mp4": 100 * MB,
        })
        result = self.scan()
        self.assertEqual(self.shows(result), {"Python Course": {1: 3}})
        self.assertEqual(sorted((v.episode, v.title) for v in result.videos),
                         [(1, "Introduction"), (2, "Variables"), (3, "Functions")])

    def test_single_episode_release_folder(self):
        make_tree(self.media, {"Show.S01E04.1080p.WEB.h264-GRP/show.s01e04.1080p.web.h264-grp.mkv": GB})
        result = self.scan()
        self.assertEqual(self.shows(result), {"Show": {1: 1}})
        self.assertEqual(result.videos[0].episode, 4)

    def test_mixed_shows_in_one_folder_are_separated(self):
        make_tree(self.media, {
            "TV/Alpha.S01E01.720p.mkv": GB, "TV/Alpha.S01E02.720p.mkv": GB,
            "TV/Bravo.S02E05.720p.mkv": GB, "TV/Bravo.S02E06.720p.mkv": GB,
        })
        self.assertEqual(self.shows(self.scan()), {"Alpha": {1: 2}, "Bravo": {2: 2}})


class LooseFiles(ScannerCase):
    def test_root_level_files(self):
        make_tree(self.media, {
            "ES_Project_Demo.mp4": 5 * MB,
            "Inception.2010.1080p.BluRay.x264.mkv": 2 * GB,
            "Birthday 2024.mp4": 50 * MB,
            "Screencast From 2026-09-08 18-45-12.mp4": 9 * MB,
            "VID_20250908_184512.mp4": 9 * MB,
            "notes.txt": b"x",
            "cover.jpg": 50_000,
        })
        result = self.scan()
        demo = self.by_name["ES_Project_Demo.mp4"]
        self.assertEqual((demo.kind, demo.title), (KIND_OTHER, "ES Project Demo"))
        movie = self.by_name["Inception.2010.1080p.BluRay.x264.mkv"]
        self.assertEqual((movie.kind, movie.title, movie.year), (KIND_MOVIE, "Inception", 2010))
        # A bare year makes a movie only tentatively; the running time decides later.
        maybe = self.by_name["Birthday 2024.mp4"]
        self.assertEqual((maybe.kind, maybe.tentative, maybe.alt_title), (KIND_MOVIE, True, "Birthday 2024"))
        for recording in ("Screencast From 2026-09-08 18-45-12.mp4", "VID_20250908_184512.mp4"):
            video = self.by_name[recording]
            self.assertEqual((video.kind, video.tentative), (KIND_OTHER, False), recording)
        self.assertEqual(len(result.videos), 5)

    def test_plain_folders_are_walked_as_containers(self):
        make_tree(self.media, {
            "Movies/Heat (1995) [1080p]/Heat.1995.1080p.BRrip.x264.YIFY.mp4": 2 * GB,
            "Movies/Enemy.2013.1080p.BluRay.x264.YIFY.mp4": GB,
            "TV Shows/Friends/Season 1/Friends.S01E01.Pilot.mkv": 200 * MB,
            "Camera/2025/clip one.mp4": 30 * MB,
            "Camera/holiday.mov": 30 * MB,
        })
        result = self.scan()
        self.assertEqual(self.titles(result, KIND_MOVIE), ["Enemy", "Heat"])
        self.assertEqual(self.shows(result), {"Friends": {1: 1}})
        self.assertEqual(self.titles(result, KIND_OTHER), ["clip one", "holiday"])

    def test_hidden_and_non_video_files_are_skipped(self):
        make_tree(self.media, {
            ".hidden/secret.mkv": GB, ".thumb.mp4": MB, "movie.nfo": b"x", "song.mp3": MB,
            "empty.mkv": 0, "real.mkv": MB,
        })
        self.assertEqual([os.path.basename(v.path) for v in self.scan().videos], ["real.mkv"])

    def test_missing_root_is_reported_not_fatal(self):
        make_tree(self.media, {"a.mkv": MB})
        gone = self.tmp / "unplugged-drive"
        result = self.scan(self.media, gone)
        self.assertEqual(result.scanned_roots, [str(self.media)])
        self.assertEqual(result.missing_roots, [str(gone)])
        self.assertEqual(len(result.videos), 1)

    def test_symlink_loop_does_not_hang(self):
        make_tree(self.media, {"Stuff/a.mkv": MB})
        os.symlink(self.media, self.media / "Stuff" / "loop")
        self.assertEqual(len(self.scan().videos), 1)


class Artwork(ScannerCase):
    def test_named_artwork_is_used(self):
        make_tree(self.media, {
            "Heat (1995) [1080p]/Heat.1995.1080p.mkv": GB,
            "Heat (1995) [1080p]/poster.jpg": 80_000,
            "Heat (1995) [1080p]/WWW.YIFY-TORRENTS.COM.jpg": 130_000,
            "Friends/folder.png": 90_000,
            "Friends/S01/Friends.S01E01.Pilot.mkv": 200 * MB,
            "Friends/S01/Friends.S01E01.Pilot.jpg": 40_000,
        })
        self.scan()
        self.assertEqual(self.by_name["Heat.1995.1080p.mkv"].cover, str(self.media / "Heat (1995) [1080p]/poster.jpg"))
        episode = self.by_name["Friends.S01E01.Pilot.mkv"]
        self.assertEqual(episode.show_cover, str(self.media / "Friends/folder.png"))
        self.assertEqual(episode.cover, str(self.media / "Friends/S01/Friends.S01E01.Pilot.jpg"))

    def test_torrent_site_adverts_are_not_artwork(self):
        make_tree(self.media, {
            "Heat (1995) [1080p]/Heat.1995.1080p.mkv": GB,
            "Heat (1995) [1080p]/WWW.YIFY-TORRENTS.COM.jpg": 130_000,
            "Tenet (2020) [1080p]/Tenet.2020.1080p.mkv": GB,
            "Tenet (2020) [1080p]/www.YTS.MX.jpg": 53_000,
            "Backrooms (2026) [1080p]/Backrooms.2026.1080p.mkv": GB,
            "Backrooms (2026) [1080p]/YTS.GG - Official site.jpg": 38_000,
        })
        self.assertEqual([v.cover for v in self.scan().videos], [None, None, None])

    def test_identical_image_in_unrelated_folders_is_an_advert(self):
        advert = b"\xff\xd8" + b"same-bytes" * 2000
        make_tree(self.media, {
            "Alpha (2001) [1080p]/Alpha.2001.1080p.mkv": GB, "Alpha (2001) [1080p]/art.jpg": advert,
            "Bravo (2002) [1080p]/Bravo.2002.1080p.mkv": GB, "Bravo (2002) [1080p]/art.jpg": advert,
            "Charlie (2003) [1080p]/Charlie.2003.1080p.mkv": GB,
            "Charlie (2003) [1080p]/art.jpg": b"\xff\xd8" + b"unique-art" * 2000,
        })
        self.scan()
        self.assertIsNone(self.by_name["Alpha.2001.1080p.mkv"].cover)
        self.assertIsNone(self.by_name["Bravo.2002.1080p.mkv"].cover)
        self.assertTrue(self.by_name["Charlie.2003.1080p.mkv"].cover.endswith("art.jpg"))


if __name__ == "__main__":
    unittest.main()
