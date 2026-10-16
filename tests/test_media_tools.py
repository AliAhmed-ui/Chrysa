"""ffprobe / ffmpeg integration and subtitle discovery.

The ffmpeg tests synthesise tiny clips, so they need ffmpeg installed and are
skipped otherwise.
"""

import subprocess
import threading
import unittest

from tests.helpers import TempDirCase, make_tree
from chrysa.probe import find_tool, probe, probe_many
from chrysa.subtitles import describe, find_external_subtitles
from chrysa.thumbnails import ThumbnailWorkers, cache_file, grab_frame, make_thumbnail

HAVE_FFMPEG = bool(find_tool("ffmpeg") and find_tool("ffprobe"))


def synthesize(path, source="testsrc=duration=6:size=320x180:rate=10"):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source,
         "-pix_fmt", "yuv420p", "-c:v", "mpeg4", "-q:v", "5", str(path)],
        check=True, stdin=subprocess.DEVNULL,
    )


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class FfmpegTools(TempDirCase):
    def test_probe_reads_duration_and_size(self):
        clip = self.media / "clip.mp4"
        synthesize(clip)
        info = probe(str(clip))
        self.assertTrue(info.ok)
        self.assertAlmostEqual(info.duration, 6.0, delta=0.3)
        self.assertEqual((info.width, info.height), (320, 180))

    def test_probe_of_garbage_is_empty_not_an_error(self):
        make_tree(self.media, {"broken.mkv": b"this is not a video"})
        info = probe(str(self.media / "broken.mkv"))
        self.assertEqual((info.ok, info.duration, info.width), (False, 0.0, 0))
        self.assertFalse(probe(str(self.media / "missing.mkv")).ok)

    def test_probe_many_reports_every_file(self):
        clips = []
        for name in ("a.mp4", "b.mp4", "c.mp4"):
            synthesize(self.media / name, "testsrc=duration=2:size=160x90:rate=5")
            clips.append(str(self.media / name))
        seen = {}
        probe_many(clips, lambda path, info: seen.__setitem__(path, info.duration))
        self.assertEqual(sorted(seen), sorted(clips))
        self.assertTrue(all(d > 1.5 for d in seen.values()))

    def test_thumbnail_is_a_jpeg_no_wider_than_requested(self):
        clip = self.media / "clip.mp4"
        synthesize(clip, "testsrc=duration=6:size=1280x720:rate=10")
        out = self.tmp / "thumbs" / "clip.jpg"
        self.assertTrue(make_thumbnail(str(clip), out, 6.0, width=640))
        self.assertEqual(out.read_bytes()[:3], b"\xff\xd8\xff")
        info = probe(str(out))
        self.assertEqual((info.width, info.height), (640, 360))
        self.assertEqual([p.name for p in out.parent.iterdir()], ["clip.jpg"], "no temp files left behind")

    def test_small_videos_are_not_upscaled(self):
        clip = self.media / "small.mp4"
        synthesize(clip)
        out = self.tmp / "small.jpg"
        self.assertTrue(make_thumbnail(str(clip), out, 6.0, width=640))
        self.assertEqual(probe(str(out)).width, 320)

    def test_dark_frame_is_detected_and_a_later_one_preferred(self):
        # Black for the first half, test pattern afterwards: 15 % and 30 % are black, 60 % is not.
        clip = self.media / "fade.mp4"
        synthesize(clip, "color=black:duration=10:size=320x180:rate=10[a];testsrc=duration=10:size=320x180:rate=10[b];[a][b]concat")
        dark = grab_frame(str(clip), self.tmp / "dark.jpg", 1.5, 320)
        bright = grab_frame(str(clip), self.tmp / "bright.jpg", 12.0, 320)
        self.assertLess(dark[0], 30)
        self.assertGreater(bright[0], 30)
        out = self.tmp / "fade.jpg"
        self.assertTrue(make_thumbnail(str(clip), out, 20.0, width=320))
        self.assertGreater(out.stat().st_size, (self.tmp / "dark.jpg").stat().st_size)

    def test_entirely_black_video_still_gets_a_thumbnail(self):
        clip = self.media / "black.mp4"
        synthesize(clip, "color=black:duration=4:size=320x180:rate=10")
        self.assertTrue(make_thumbnail(str(clip), self.tmp / "black.jpg", 4.0))

    def test_unreadable_video_yields_no_thumbnail(self):
        make_tree(self.media, {"broken.mkv": b"not a video"})
        out = self.tmp / "broken.jpg"
        self.assertFalse(make_thumbnail(str(self.media / "broken.mkv"), out, 0.0))
        self.assertFalse(out.exists())

    def test_worker_pool_generates_and_reports(self):
        good, bad = self.media / "good.mp4", self.media / "bad.mkv"
        synthesize(good)
        make_tree(self.media, {"bad.mkv": b"nope"})
        results, finished = {}, threading.Semaphore(0)

        def store(kind, target_id, path):
            results[(kind, target_id)] = path
            finished.release()

        cache = self.tmp / "cache"
        pool = ThumbnailWorkers(cache, store, workers=2)
        self.addCleanup(pool.stop)
        pool.enqueue("thumb", 1, str(good), 6.0, good.stat().st_size, good.stat().st_mtime)
        pool.enqueue("thumb", 2, str(bad), 0.0, 4, 0.0)
        pool.enqueue("backdrop", 7, str(good), 6.0, good.stat().st_size, good.stat().st_mtime, priority=0)
        for _ in range(3):
            self.assertTrue(finished.acquire(timeout=60))
        self.assertIsNone(results[("thumb", 2)])
        expected = cache_file(cache, str(good), good.stat().st_size, good.stat().st_mtime)
        self.assertEqual(results[("thumb", 1)], str(expected))
        self.assertTrue(results[("backdrop", 7)].endswith("-bd.jpg"))
        self.assertEqual(pool.pending(), 0)

    def test_cache_name_follows_the_file_identity(self):
        cache = self.tmp / "cache"
        first = cache_file(cache, "/v/a.mkv", 100, 5.0)
        self.assertEqual(first, cache_file(cache, "/v/a.mkv", 100, 5.9))
        self.assertNotEqual(first, cache_file(cache, "/v/a.mkv", 101, 5.0))
        self.assertNotEqual(first, cache_file(cache, "/v/b.mkv", 100, 5.0))
        self.assertEqual(first.suffix, ".jpg")


class SubtitleLabels(unittest.TestCase):
    def test_labels(self):
        for text, label in [
            ("English", "English"),
            ("SDH.eng.HI", "English (SDH)"),
            ("SDH.eng", "English (SDH)"),
            ("Latin American.spa", "Spanish (Latin American)"),
            ("Saudi Arabia.ara", "Arabic (Saudi Arabia)"),
            ("Forced.eng", "English (Forced)"),
            ("Regular.ita", "Italian"),
            ("2_English", "English"),
            (".en", "English"),
            (".fre", "French"),
            (".hi", "Hindi"),
            ("", ""),
            (".commentary", "commentary"),
        ]:
            self.assertEqual(describe(text), label, text)


class SubtitleDiscovery(TempDirCase):
    def labels(self, video):
        return [(s.label, s.sidecar) for s in find_external_subtitles(str(self.media / video))]

    def test_a_longer_episode_number_does_not_lend_its_subtitle_to_a_shorter_one(self):
        make_tree(self.media, {
            "Show S01E1.mkv": 1000, "Show S01E10.mkv": 1000, "Show S01E10.en.srt": b"1\n", "Show S01E1.en.srt": b"1\n",
        })
        self.assertEqual(self.labels("Show S01E1.mkv"), [("English", False)])

    def test_movie_folder_with_sidecar_and_subs_folder(self):
        folder = "Backrooms (2026) [1080p] [WEBRip] [5.1] [YTS.GG - YTS.BZ]"
        stem = "Backrooms.2026.1080p.WEBRip.x264.AAC5.1-[YTS.GG - YTS.BZ]"
        make_tree(self.media, {
            f"{folder}/{stem}.mp4": 1000,
            f"{folder}/{stem}.srt": b"1\n",
            f"{folder}/Subs/English.srt": b"1\n",
            f"{folder}/Subs/Latin American.spa.srt": b"1\n",
            f"{folder}/Subs/Saudi Arabia.ara.srt": b"1\n",
            f"{folder}/Subs/SDH.eng.HI.srt": b"1\n",
            f"{folder}/YTSYifyUP... (TOR).txt": b"x",
        })
        self.assertEqual(self.labels(f"{folder}/{stem}.mp4"), [
            ("Subtitles", True),
            ("Arabic (Saudi Arabia)", False),
            ("English", False),
            ("English (SDH)", False),
            ("Spanish (Latin American)", False),
        ])

    def test_language_suffix_on_a_sidecar(self):
        make_tree(self.media, {"Movie.2019.mkv": 10, "Movie.2019.en.srt": b"1\n", "Movie.2019.fr.forced.ass": b"1\n",
                               "Unrelated.srt": b"1\n", "Movie.2019.nfo": b"x"})
        self.assertEqual(self.labels("Movie.2019.mkv"), [("English", False), ("French (Forced)", False)])

    def test_shared_subs_folder_is_not_guessed_at_for_episodes(self):
        make_tree(self.media, {
            "Show/Show.S01E01.mkv": 10, "Show/Show.S01E02.mkv": 10,
            "Show/Show.S01E01.srt": b"1\n",
            "Show/Subs/English.srt": b"1\n",                    # whose is it? nobody's
            "Show/Subs/Show.S01E02.eng.srt": b"1\n",
            "Show/Subs/Show.S01E01/2_English.srt": b"1\n",
            "Show/Subs/Show.S01E01/3_French.srt": b"1\n",
        })
        self.assertEqual(self.labels("Show/Show.S01E01.mkv"), [("Subtitles", True), ("English", False), ("French", False)])
        self.assertEqual(self.labels("Show/Show.S01E02.mkv"), [("English", False)])

    def test_no_subtitles(self):
        make_tree(self.media, {"lonely.mkv": 10})
        self.assertEqual(self.labels("lonely.mkv"), [])
        self.assertEqual(find_external_subtitles("/nonexistent/dir/video.mkv"), [])


if __name__ == "__main__":
    unittest.main()
