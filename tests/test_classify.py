"""Folder-level TV show detection for yt-dlp style downloads with no season markers."""

from tests.helpers import MB, TempDirCase, make_tree
from chrysa import classify
from chrysa.scanner import KIND_EPISODE, KIND_OTHER, Scanner

GB = 1024 * MB
YT = "(Sub) {d} Sep 26 ｜ Digitally VGO TEL, Happilac Paints & Vince Shampoo ｜ Green TV [{i}].webm"


class FolderClassifier(TempDirCase):
    def scan(self):
        return Scanner().scan([self.media])

    def test_dated_ytdlp_drama_becomes_numbered_show(self):
        make_tree(self.media, {
            f"Mitti De Baway/Mitti De Baway Ep {n:02d} " + YT.format(d=22 + n, i=f"id{n}"): 300 * MB
            for n in (1, 2)
        })
        result = self.scan()
        episodes = sorted(result.of_kind(KIND_EPISODE), key=lambda v: v.episode)
        self.assertEqual([e.show_title for e in episodes], ["Mitti De Baway"] * 2)
        self.assertEqual([(e.episode, e.title) for e in episodes], [(1, "EP-1"), (2, "EP-2")])

    def test_other_numbering_styles(self):
        make_tree(self.media, {
            "Kahani/Kahani Episode 7 - Full.mp4": 200 * MB,
            "Kahani/Kahani Episode 8 - Full.mp4": 210 * MB,
            "Kahani/Kahani Episode 9 - Full.mp4": 190 * MB,
        })
        episodes = self.scan().of_kind(KIND_EPISODE)
        self.assertEqual(sorted(e.episode for e in episodes), [7, 8, 9])

    def test_dates_only_are_ordered_chronologically(self):
        make_tree(self.media, {
            "Talk Show/Talk Show 2026-09-03.mp4": 100 * MB,
            "Talk Show/Talk Show 2026-09-01.mp4": 100 * MB,
            "Talk Show/Talk Show 2026-09-02.mp4": 100 * MB,
        })
        episodes = self.scan().of_kind(KIND_EPISODE)
        got = {e.path.rsplit("/", 1)[1]: e.episode for e in episodes}
        self.assertEqual(got["Talk Show 2026-09-01.mp4"], 1)
        self.assertEqual(got["Talk Show 2026-09-03.mp4"], 3)

    def test_unrelated_movies_stay_loose(self):
        make_tree(self.media, {
            "Downloads/Heat.1995.mp4": 2 * GB,
            "Downloads/Casablanca.mp4": GB,
        })
        self.assertFalse(self.scan().of_kind(KIND_EPISODE))

    def test_camera_clips_are_not_a_show(self):
        make_tree(self.media, {
            f"Camera/VID_20260908_{n:02d}.mp4": 50 * MB for n in range(4)
        })
        result = self.scan()
        self.assertFalse(result.of_kind(KIND_EPISODE))
        self.assertEqual(len(result.of_kind(KIND_OTHER)), 4)

    def test_movie_release_folder_with_extras_is_not_a_show(self):
        verdict = classify.classify_folder(
            "Tenet (2020) [1080p] [BluRay]", ["Tenet.2020.1080p", "Tenet.2020.1080p.part2"], [GB, GB]
        )
        self.assertEqual(verdict.label, classify.OTHER)

    def test_episode_number_ignores_ids_and_dates(self):
        self.assertEqual(classify.episode_number("Show Ep 05 (Sub) 23 Sep 26 [Ep99xyz]"), 5)
        self.assertIsNone(classify.episode_number("Show 23 Sep 26 [abc]"))
