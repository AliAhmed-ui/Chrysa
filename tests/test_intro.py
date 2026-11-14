"""Intro detection: the season engine on synthetic audio (the 20 required situations), scoring, and the Library."""

import unittest
from functools import cache

from tests.helpers import MB, LibraryCase, make_tree
from chrysa import intro
from chrysa.probe import find_tool  # noqa: F401

try:
    import numpy as np
    from tests import synth
except ImportError:
    np = None

RATE = intro.RATE
MIN = 60.0


@cache
def theme(name: str):
    """Named recurring pieces of audio."""
    if name == "A":
        return synth.music(7, 72)
    if name == "A90":  # a longer cut of the same opening
        return synth.music(7, 90)
    if name == "A60":  # a shorter cut of it
        return synth.music(7, 90)[:60 * RATE]
    if name == "A_edit":  # a slightly edited cut: ten seconds replaced
        edited = synth.music(7, 72).copy()
        edited[30 * RATE:40 * RATE] = synth.music(99, 10)
        return edited
    if name == "B":  # a different opening (the season changes it)
        return synth.music(21, 66)
    if name == "song":  # a song that recurs in the middle of episodes
        return synth.music(33, 45)
    if name == "credits":  # credits music
        return synth.music(44, 70)
    if name == "stinger":  # the "previously on" sting
        return synth.music(55, 6)
    raise KeyError(name)


@cache
def make(seed: int, minutes: float, at, name, extras=(), gain=1.0, degrade=False, bed=0.02):
    """Features of one synthetic episode; *extras* is ``((theme name, at), ...)``."""
    audio = synth.episode(seed, minutes, at, theme(name) if name else None,
                          [(theme(n), t) for n, t in extras], gain, degrade, bed)
    return intro.features_from_samples(audio)


def season(specs, minutes=15.0, name="A", **common):
    """Judge a season; *specs* has one entry per episode: ``None`` (no intro), a start in seconds, or a dict
    ``{"at": seconds | None, "name": theme, "minutes": m, ...}``."""
    features = {}
    for number, spec in enumerate(specs, 1):
        options = dict(spec) if isinstance(spec, dict) else {"at": spec}
        options = {"at": None, "name": name, "minutes": minutes, **common, **options}
        if options["at"] is None:
            options["name"] = None
        options["extras"] = tuple(options.get("extras", ()))
        features[number] = make(number, options.pop("minutes"), options.pop("at"), options.pop("name"), **options)
    return intro.analyse(features)


@unittest.skipIf(np is None, "numpy is not installed")
class Pairs(unittest.TestCase):
    def test_a_shared_theme_is_found_wherever_it_is(self):
        a = synth.episode(1, 8, 20, theme("A"))
        b = synth.episode(2, 8, 330, theme("A"))
        found_a, found_b = intro.find_common_block(a, b)
        self.assertAlmostEqual(found_a.start, 20, delta=1)
        self.assertAlmostEqual(found_a.end, 92, delta=1)
        self.assertAlmostEqual(found_b.start, 330, delta=1)
        self.assertAlmostEqual(found_b.end, 402, delta=1)

    def test_unrelated_audio_and_short_jingles_match_nothing(self):
        self.assertIsNone(intro.find_common_block(synth.episode(1, 6, None), synth.episode(2, 6, None)))
        a = synth.episode(1, 6, 20, theme("stinger"))
        b = synth.episode(2, 6, 90, theme("stinger"))
        self.assertIsNone(intro.find_common_block(a, b))

    def test_a_short_dropout_does_not_split_a_match(self):
        a = synth.episode(1, 6, 20, theme("A"))
        b = synth.episode(2, 6, 120, theme("A"))
        b[int(150 * RATE):int(151 * RATE)] = 0.5  # one second of something else
        found_a, found_b = intro.find_common_block(a, b)
        self.assertAlmostEqual(found_b.end - found_b.start, 72, delta=2)

    def test_features_are_compact_and_survive_storage(self):
        features = make(1, 15, 60, "A")
        self.assertLess(len(features.to_blob()), features.frames * intro.BANDS)  # int8, then compressed
        again = intro.Features.from_blob(features.to_blob(), features.frames, features.duration)
        self.assertTrue((again.q == features.q).all())


@unittest.skipIf(np is None, "numpy is not installed")
class Cases(unittest.TestCase):
    """The behaviours that were asked for, one per situation. Times are checked against what was put into the audio."""

    def assertIntro(self, result, episode, start, length=72, delta=1.5):
        r = result.results[episode]
        self.assertTrue(r.present, f"episode {episode} should have an intro")
        self.assertGreaterEqual(r.confidence, intro.MIN_CONFIDENCE)
        self.assertAlmostEqual(r.start, start, delta=delta)
        self.assertAlmostEqual(r.end, start + length, delta=delta)

    def assertNone(self, result, episode):
        r = result.results[episode]
        self.assertFalse(r.present and r.confidence >= intro.MIN_CONFIDENCE, f"episode {episode} should show no intro")

    # --- where the intro is -------------------------------------------------------------------------------------

    def test_01_intro_at_30_seconds(self):
        result = season([30, 31, 29, 30])
        for episode, at in enumerate((30, 31, 29, 30), 1):
            self.assertIntro(result, episode, at)

    def test_02_intro_at_2_minutes(self):
        result = season([120, 118, 124, 121])
        for episode, at in enumerate((120, 118, 124, 121), 1):
            self.assertIntro(result, episode, at)

    def test_03_intro_at_5_minutes(self):
        result = season([300, 305, 296, 301])
        for episode, at in enumerate((300, 305, 296, 301), 1):
            self.assertIntro(result, episode, at)

    def test_04_intro_at_10_minutes(self):
        result = season([600, 604, 597, 601], minutes=30)
        for episode, at in enumerate((600, 604, 597, 601), 1):
            self.assertIntro(result, episode, at)

    def test_each_episode_has_its_own_position(self):
        result = season([112, 123, 291, None, 121])  # the example from the brief
        for episode, at in {1: 112, 2: 123, 3: 291, 5: 121}.items():
            self.assertIntro(result, episode, at)
        self.assertNone(result, 4)

    # --- missing intros -----------------------------------------------------------------------------------------

    def test_05_first_episode_has_no_intro(self):
        result = season([None, 120, 130, 300, 125])
        self.assertNone(result, 1)
        for episode, at in {2: 120, 3: 130, 4: 300, 5: 125}.items():
            self.assertIntro(result, episode, at)

    def test_06_second_episode_has_an_intro_when_only_it_and_its_neighbour_do(self):
        result = season([None, 112, 130])
        self.assertNone(result, 1)
        self.assertIntro(result, 2, 112)
        self.assertIntro(result, 3, 130)

    def test_07_some_episodes_have_no_intro(self):
        result = season([120, None, 130, None, 125, 300, None])
        for episode, at in {1: 120, 3: 130, 5: 125, 6: 300}.items():
            self.assertIntro(result, episode, at)
        for episode in (2, 4, 7):
            self.assertNone(result, episode)
        self.assertEqual(result.profiles[0].occurrences, 4)

    def test_19_a_special_without_an_intro_shows_nothing(self):
        result = season([120, 130, 125, {"at": None, "minutes": 40}, 122])
        self.assertNone(result, 4)
        self.assertIntro(result, 5, 122)

    # --- durations and structure --------------------------------------------------------------------------------

    def test_08_intro_length_is_independent_per_episode(self):
        specs = [{"at": 100, "name": "A90"}, {"at": 110, "name": "A60"}, {"at": 95, "name": "A90"},
                 {"at": 105, "name": "A60"}, {"at": 102, "name": "A90"}, {"at": 98, "name": "A60"}]
        result = season(specs)
        for episode, (at, length) in enumerate(((100, 90), (110, 60), (95, 90), (105, 60), (102, 90), (98, 60)), 1):
            self.assertIntro(result, episode, at, length, delta=2.5)

    def test_09_cold_open_then_intro(self):
        result = season([292, 305, 280, 318])  # stories of different length first
        for episode, at in enumerate((292, 305, 280, 318), 1):
            self.assertIntro(result, episode, at)

    def test_10_recap_then_intro(self):
        extras = (("stinger", 4),)
        result = season([{"at": 85, "extras": extras}, {"at": 96, "extras": extras}, {"at": 70, "extras": extras},
                         {"at": 90, "extras": extras}])
        for episode, at in enumerate((85, 96, 70, 90), 1):
            self.assertIntro(result, episode, at)  # the sting is not the intro

    def test_11_intro_then_normal_episode(self):
        result = season([5, 6, 4, 5])
        for episode, at in enumerate((5, 6, 4, 5), 1):
            self.assertIntro(result, episode, at)

    def test_18_double_length_episode(self):
        result = season([120, 125, {"at": 305, "minutes": 30}, 118])
        self.assertIntro(result, 3, 305)
        self.assertIntro(result, 1, 120)

    # --- things that recur but are not the intro ----------------------------------------------------------------

    def test_12_recurring_outro_music_is_not_the_intro(self):
        # 15-minute episodes whose credits music ends 12 s before the end
        credits = (("credits", 15 * 60 - 82),)
        result = season([60, 70, 65, 75], extras=credits)
        for episode, at in enumerate((60, 70, 65, 75), 1):
            self.assertIntro(result, episode, at)
        self.assertEqual(len(result.profiles), 1)
        only_credits = season([None] * 4, extras=credits)
        for episode in range(1, 5):
            self.assertNone(only_credits, episode)

    def test_13_a_song_recurring_in_the_middle_is_not_the_intro(self):
        songs = {1: 14 * 60, 2: 21 * 60, 3: 17 * 60, 4: 12 * 60}
        specs = [{"at": None, "minutes": 30, "extras": (("song", at),)} for at in songs.values()]
        result = season(specs)
        for episode in songs:
            self.assertNone(result, episode)
        with_intro = season([{"at": 90 + 7 * n, "minutes": 30, "extras": (("song", at),)}
                             for n, at in enumerate(songs.values())])
        for episode, at in enumerate((90, 97, 104, 111), 1):
            self.assertIntro(with_intro, episode, at)
        self.assertEqual(len(with_intro.profiles), 1)

    # --- the audio itself ---------------------------------------------------------------------------------------

    def test_14_different_volume(self):
        result = season([{"at": 60, "gain": 0.25}, {"at": 70, "gain": 2.5}, {"at": 65}, {"at": 62, "gain": 0.6}])
        for episode, at in enumerate((60, 70, 65, 62), 1):
            self.assertIntro(result, episode, at)

    def test_15_different_compression(self):
        result = season([{"at": 60, "degrade": True}, {"at": 70}, {"at": 65, "degrade": True}, {"at": 62}])
        for episode, at in enumerate((60, 70, 65, 62), 1):
            self.assertIntro(result, episode, at)

    def test_16_a_slightly_edited_intro_still_matches(self):
        result = season([{"at": 60}, {"at": 70}, {"at": 65, "name": "A_edit"}, {"at": 62}])
        for episode, at in enumerate((60, 70, 65, 62), 1):
            self.assertIntro(result, episode, at, delta=2.5)

    def test_17_the_intro_changes_halfway_through_the_season(self):
        specs = [{"at": 60 + n, "name": "A"} for n in (0, 4, 2, 6)] + [{"at": 80 + n, "name": "B"} for n in (0, 3, 5, 1)]
        result = season(specs)
        self.assertEqual(len(result.profiles), 2)
        variants = {e: r.variant for e, r in result.results.items()}
        self.assertEqual(len(set(variants[e] for e in (1, 2, 3, 4))), 1)
        self.assertEqual(len(set(variants[e] for e in (5, 6, 7, 8))), 1)
        self.assertNotEqual(variants[1], variants[5])
        for episode, (at, length) in enumerate(((60, 72), (64, 72), (62, 72), (66, 72), (80, 66), (83, 66), (85, 66), (81, 66)), 1):
            self.assertIntro(result, episode, at, length)

    # --- never guess --------------------------------------------------------------------------------------------

    def test_20_a_low_confidence_match_shows_nothing(self):
        # the theme is buried 18 dB under loud dialogue
        result = season([{"at": 60, "bed": 8.0, "name": "A"}, {"at": 70, "bed": 8.0}, {"at": 65, "bed": 8.0}], bed=8.0)
        shown = [e for e, r in result.results.items() if r.present and r.confidence >= intro.MIN_CONFIDENCE]
        self.assertEqual(shown, [])

    def test_unrelated_episodes_have_no_intro(self):
        result = season([None, None, None, None])
        self.assertEqual(result.profiles, [])
        self.assertFalse(any(r.present for r in result.results.values()))

    def test_two_episodes_are_enough_when_everything_agrees(self):
        result = season([60, 71])
        self.assertIntro(result, 1, 60)
        self.assertIntro(result, 2, 71)

    def test_confidence_grows_with_recurrence(self):
        few = season([None, 60, 70, None, None, None]).profiles
        many = season([60, 70, 65, 62, 75, 68]).profiles
        self.assertTrue(many)
        self.assertTrue(not few or many[0].confidence > few[0].confidence)


@unittest.skipIf(np is None, "numpy is not installed")
class Scoring(unittest.TestCase):
    def test_duration_plausibility(self):
        plausible = intro._plausible_duration
        self.assertEqual(plausible(5), 0.0)
        self.assertEqual(plausible(60), 1.0)
        self.assertLess(plausible(15), plausible(30))
        self.assertLess(plausible(250), 0.6)
        self.assertLess(plausible(900), 0.2)

    def test_position_is_a_soft_signal_not_a_cutoff(self):
        early = intro.opening_score([60, 70], [2700, 2700])
        late = intro.opening_score([600, 610], [2700, 2700])  # ten minutes in
        wandering = intro.opening_score([840, 1260, 1020], [2700, 2700, 2700])
        self.assertGreater(early, 0.95)
        self.assertGreater(late, 0.8)  # a late intro is still plausible
        self.assertLess(wandering, 0.5)
        self.assertLess(intro.outro_score([20, 25], [2700, 2700]), 0.05)  # ends with the episode
        self.assertEqual(intro.outro_score([1500, 1600], [2700, 2700]), 1.0)
        self.assertGreater(intro.outro_score([250], [360]), 0.9)  # a short episode with an opening near its start

    def test_recurrence(self):
        self.assertEqual(intro.recurrence_score(1, 5), 0.0)
        self.assertLess(intro.recurrence_score(2, 10), intro.recurrence_score(7, 10))
        self.assertGreater(intro.recurrence_score(7, 10), 0.8)

    def test_members_only_weakly_tied_are_dropped(self):
        occurrences = []
        for a in (1, 2, 3, 4):  # a clique of four episodes
            for b in (1, 2, 3, 4):
                if a != b:
                    occurrences.append(intro._Occurrence(a, 100, 450, 0.9, b))
        occurrences.append(intro._Occurrence(5, 100, 450, 0.5, 1))  # one chance link
        occurrences.append(intro._Occurrence(1, 100, 450, 0.5, 5))
        features = {i: intro.Features(np.zeros((10, intro.BANDS), np.int8), 900.0) for i in range(1, 6)}
        members = intro._members(occurrences, features)
        self.assertEqual({m.episode for m in members}, {1, 2, 3, 4})


@unittest.skipIf(np is None, "numpy is not installed")
class Season(LibraryCase):
    """The Library side: caching, thresholds, incremental learning, invalidation."""

    def setUp(self):
        super().setUp()
        make_tree(self.media, {f"Show/S01/Show.S01E{n:02d}.720p.mkv": 200 * MB for n in range(1, 10)})
        make_tree(self.media, {"Lone/S01/Lone.S01E01.720p.mkv": 200 * MB})
        self.default_duration = 900.0
        self.scan()
        self.starts = {1: 60, 2: 75, 3: None, 4: 64, 5: 70, 6: 62, 7: 90, 8: 66, 9: 61}
        self.extracted = []
        original = intro.extract_features

        def fake(path, should_stop=lambda: False):
            name = path.rsplit("/", 1)[-1]
            self.extracted.append(name)
            number = int(name.split("E")[1][:2])
            if name.startswith("Lone"):
                return make(50, 15, 40, "A")
            at = self.starts[number]
            return make(number, 15, at, "A" if at else None)

        intro.extract_features = fake
        self.addCleanup(setattr, intro, "extract_features", original)

    def episodes(self):
        return self.library.episodes([s for s in self.library.shows() if s.title == "Show"][0].id)

    def run_jobs(self):
        found = []
        for job in self.library.intro_jobs():
            intro.analyse_season(job, self.library, on_found=found.append)
        return found

    def test_each_episode_gets_its_own_window_and_it_is_cached(self):
        self.assertEqual(len(self.library.intro_jobs()), 1)  # the lone episode has nobody to compare with
        found = self.run_jobs()
        episodes = self.episodes()
        windows = {e.episode: self.library.intro_for(e) for e in episodes}
        for number, at in self.starts.items():
            if at is None:
                self.assertIsNone(windows[number], f"episode {number} has no intro")
            else:
                self.assertAlmostEqual(windows[number][0], at, delta=1.5)
                self.assertAlmostEqual(windows[number][1] - windows[number][0], 72, delta=1.5)
        self.assertEqual(len(found), 8)
        # Cached: nothing left to analyse, and no file is decoded again.
        self.extracted.clear()
        self.assertEqual(self.library.intro_jobs(), [])
        self.run_jobs()
        self.assertEqual(self.extracted, [])

    def test_a_season_has_a_profile_with_its_counts(self):
        self.run_jobs()
        show = [s for s in self.library.shows() if s.title == "Show"][0]
        (profile,) = self.library.intro_profiles(show.id, 1)
        self.assertEqual((profile.occurrences, profile.episodes), (8, 9))
        self.assertGreater(profile.confidence, 0.85)
        self.assertAlmostEqual(profile.duration, 72, delta=1.5)

    def test_it_learns_as_episodes_arrive_without_decoding_again(self):
        late = {n: self.starts[n] for n in (8, 9)}
        for n in late:
            self.starts[n] = None
        original = intro.extract_features
        # Pretend episodes 8 and 9 are not in the library yet.
        show_dir = self.media / "Show" / "S01"
        hidden = []
        for n in late:
            path = show_dir / f"Show.S01E{n:02d}.720p.mkv"
            hidden.append((path, path.with_suffix(".hold")))
            path.rename(path.with_suffix(".hold"))
        self.scan()
        self.run_jobs()
        show = [s for s in self.library.shows() if s.title == "Show"][0]
        (profile,) = self.library.intro_profiles(show.id, 1)
        self.assertEqual(profile.episodes, 7)
        for n in late:
            self.starts[n] = {8: 66, 9: 61}[n]
        for path, hold in hidden:
            hold.rename(path)
        self.scan()
        self.extracted.clear()
        self.run_jobs()
        self.assertEqual(sorted(self.extracted), ["Show.S01E08.720p.mkv", "Show.S01E09.720p.mkv"])  # only the new ones
        (profile,) = self.library.intro_profiles(show.id, 1)
        self.assertEqual((profile.occurrences, profile.episodes), (8, 9))
        windows = {e.episode: self.library.intro_for(e) for e in self.episodes()}
        self.assertAlmostEqual(windows[8][0], 66, delta=1.5)
        self.assertAlmostEqual(windows[9][0], 61, delta=1.5)
        intro.extract_features = original

    def test_low_confidence_is_never_returned(self):
        self.run_jobs()
        episode = self.episodes()[0]
        self.assertIsNotNone(self.library.intro_for(episode))
        self.library.db.connection().execute("UPDATE episode_intros SET confidence = ? WHERE media_id = ?",
                                             (intro.MIN_CONFIDENCE - 0.01, episode.id))
        self.assertIsNone(self.library.intro_for(episode))

    def test_a_changed_file_or_algorithm_is_analysed_again(self):
        self.run_jobs()
        episode = self.episodes()[0]
        self.library.db.connection().execute("UPDATE episode_intros SET size = size + 1 WHERE media_id = ?", (episode.id,))
        self.assertIsNone(self.library.intro_for(episode))
        self.assertEqual(len(self.library.intro_jobs()), 1)
        self.run_jobs()
        self.assertIsNotNone(self.library.intro_for(episode))
        self.library.set_setting("intro_algorithm", intro.ALGORITHM - 1)
        self.assertEqual(len(self.library.intro_jobs()), 1)  # old results and features were dropped
        self.assertIsNone(self.library.intro_for(episode))

    def test_unreadable_audio_is_retried_later_not_forever(self):
        intro.extract_features = lambda *a, **k: None
        self.run_jobs()
        self.assertEqual(self.library.intro_jobs(), [])  # recorded as unreadable: not retried on every scan
        self.library.db.connection().execute("UPDATE episode_intros SET detected_at = detected_at - 200000")
        self.assertEqual(len(self.library.intro_jobs()), 1)  # ... but tried again after a day

    def test_a_deleted_show_takes_its_intro_data_with_it(self):
        self.run_jobs()
        con = self.library.db.connection()
        self.assertGreater(con.execute("SELECT COUNT(*) FROM intro_features").fetchone()[0], 0)
        con.execute("DELETE FROM media")
        for table in ("intro_features", "episode_intros"):
            self.assertEqual(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(np is None or find_tool("ffmpeg") is None, "numpy or ffmpeg is not installed")
class RealFiles(LibraryCase):
    """Real mkv files through the real decoder: lossy codecs, bitrates and channel layouts that differ per episode."""

    CODECS = (["-c:a", "aac", "-b:a", "96k"], ["-c:a", "libmp3lame", "-b:a", "48k", "-ac", "1"],
              ["-c:a", "libopus", "-b:a", "40k"], ["-c:a", "aac", "-b:a", "160k", "-ac", "2"], ["-c:a", "aac", "-b:a", "64k"])
    STARTS = (40, 75, None, 52, 61)  # episode 3 has no intro

    def setUp(self):
        super().setUp()
        import subprocess
        import wave

        folder = self.media / "Show" / "S01"
        folder.mkdir(parents=True)
        for number, (at, codec) in enumerate(zip(self.STARTS, self.CODECS), 1):
            audio = synth.episode(number, 6, at, theme("A") if at else None, gain=(0.5, 1.0, 1.6)[number % 3])
            wav = self.tmp / f"e{number}.wav"
            with wave.open(str(wav), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(RATE)
                out.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
            target = folder / f"Show.S01E{number:02d}.720p.mkv"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x48:r=2", "-i", str(wav),
                            "-t", "360", "-c:v", "libx264", "-preset", "ultrafast", *codec, "-shortest", str(target)], check=True)
        self.default_duration = 360.0
        self.scan()

    def test_intros_are_found_through_real_decoding(self):
        for job in self.library.intro_jobs():
            intro.analyse_season(job, self.library)
        episodes = self.library.episodes([s for s in self.library.shows() if s.title == "Show"][0].id)
        windows = {e.episode: self.library.intro_for(e) for e in episodes}
        self.assertIsNone(windows[3])
        for number, at in ((1, 40), (2, 75), (4, 52), (5, 61)):
            self.assertIsNotNone(windows[number], f"episode {number}")
            self.assertAlmostEqual(windows[number][0], at, delta=2.0)
            self.assertAlmostEqual(windows[number][1] - windows[number][0], 72, delta=2.5)
