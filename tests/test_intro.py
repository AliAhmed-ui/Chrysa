"""Intro detection: the DSP engine, the cache and the per-season driver."""

import unittest

from tests.helpers import MB, LibraryCase, make_tree
from chrysa import intro

try:
    import numpy as np
except ImportError:
    np = None

RATE = intro.RATE


def episode_audio(theme, theme_at, seconds=200, seed=0, noise=0.05):
    """Unique 'dialogue' with the theme mixed in at *theme_at* seconds."""
    rng = np.random.default_rng(seed)
    audio = rng.normal(0, 0.2, seconds * RATE).astype(np.float32)
    start = int(theme_at * RATE)
    audio[start:start + len(theme)] = theme + rng.normal(0, noise, len(theme)).astype(np.float32)
    return audio


@unittest.skipIf(np is None, "numpy is not installed")
class Engine(unittest.TestCase):
    def setUp(self):
        self.theme = np.random.default_rng(99).normal(0, 0.3, 45 * RATE).astype(np.float32)

    def test_finds_the_shared_theme_at_different_offsets(self):
        a = episode_audio(self.theme, 20, seed=1)
        b = episode_audio(self.theme, 75, seed=2)
        found = intro.find_common_block(a, b)
        self.assertIsNotNone(found)
        in_a, in_b = found
        self.assertAlmostEqual(in_a.start, 20, delta=1)
        self.assertAlmostEqual(in_a.end, 65, delta=1)
        self.assertAlmostEqual(in_b.start, 75, delta=1)
        self.assertAlmostEqual(in_b.end, 120, delta=1)

    def test_works_when_the_other_episode_comes_first(self):
        a = episode_audio(self.theme, 100, seed=3)
        b = episode_audio(self.theme, 5, seed=4)
        in_a, in_b = intro.find_common_block(a, b)
        self.assertAlmostEqual(in_a.start, 100, delta=1)
        self.assertAlmostEqual(in_b.start, 5, delta=1)

    def test_nothing_in_common_finds_nothing(self):
        self.assertIsNone(intro.find_common_block(episode_audio(self.theme, 20, seed=5), np.random.default_rng(6).normal(0, 0.2, 200 * RATE).astype(np.float32)))

    def test_a_short_shared_jingle_is_not_an_intro(self):
        jingle = self.theme[:6 * RATE]
        self.assertIsNone(intro.find_common_block(episode_audio(jingle, 20, seed=7), episode_audio(jingle, 50, seed=8)))

    def test_a_short_dropout_does_not_split_the_intro(self):
        a = episode_audio(self.theme, 20, seed=1)
        b = episode_audio(self.theme, 75, seed=2)
        b[int(95 * RATE):int(96 * RATE)] = 0.7  # one second of something else
        in_a, in_b = intro.find_common_block(a, b)
        self.assertAlmostEqual(in_b.end - in_b.start, 45, delta=1.5)


@unittest.skipIf(np is None, "numpy is not installed")
class Season(LibraryCase):
    def setUp(self):
        super().setUp()
        make_tree(self.media, {f"Show/S01/Show.S01E{n:02d}.720p.mkv": 200 * MB for n in range(1, 4)})
        make_tree(self.media, {"Lone/S01/Lone.S01E01.720p.mkv": 200 * MB})
        self.default_duration = 1320.0
        self.scan()
        theme = np.random.default_rng(99).normal(0, 0.3, 45 * RATE).astype(np.float32)
        self.audio = {}
        for n, at in ((1, 20), (2, 60), (3, 35)):
            self.audio[f"Show.S01E{n:02d}.720p.mkv"] = episode_audio(theme, at, seed=n)
        self.audio["Lone.S01E01.720p.mkv"] = episode_audio(theme, 10, seed=9)
        original = intro.extract_audio
        intro.extract_audio = lambda path, *a, **k: self.audio.get(path.rsplit("/", 1)[-1])
        self.addCleanup(setattr, intro, "extract_audio", original)

    def show_episodes(self):
        return self.library.episodes([s for s in self.library.shows() if s.title == "Show"][0].id)

    def run_jobs(self):
        for episodes, done in self.library.intro_jobs():
            intro.analyse_season(episodes, done, lambda m, f: self.library.save_intro(m, (f.start, f.end) if f else None))

    def test_every_episode_of_a_season_gets_its_own_window_and_it_is_cached(self):
        self.assertEqual(len(self.library.intro_jobs()), 1)  # the lone episode has nobody to compare with
        self.run_jobs()
        episodes = self.show_episodes()
        starts = [round(self.library.intro_for(e)[0]) for e in episodes]
        self.assertEqual(starts, [20, 60, 35])
        self.assertEqual(self.library.intro_for(episodes[0])[1] - self.library.intro_for(episodes[0])[0], 45)
        # Cached: nothing left to analyse for that season, and no audio is read again.
        calls = []
        intro.extract_audio = lambda *a, **k: calls.append(a) or None
        self.assertEqual(self.library.intro_jobs(), [])
        self.run_jobs()
        self.assertEqual(calls, [])

    def test_a_changed_file_is_analysed_again(self):
        self.run_jobs()
        episode = self.show_episodes()[0]
        self.assertIsNotNone(self.library.intro_for(episode))
        self.library.db.connection().execute("UPDATE intros SET size = size + 1 WHERE media_id = ?", (episode.id,))
        self.assertIsNone(self.library.intro_for(episode))
        self.assertEqual(len(self.library.intro_jobs()), 1)
