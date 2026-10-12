"""Database sync, playback state and resume logic."""

import os
import shutil
import time
import unittest

from tests.helpers import MB, LibraryCase, make_tree
from theia.config import FINISHED_FRACTION, MIN_PLAYED_SECONDS
from theia.library import THUMB_FAILED, THUMB_PENDING, THUMB_READY, final_kind, next_up
from theia.models import KIND_EPISODE, KIND_MOVIE, KIND_OTHER, Media, format_clock, format_duration
from theia.scanner import ScannedVideo

GB = 1024 * MB


def friends(seasons=(1, 2), episodes=3):
    return {
        f"Friends/S{s:02d}/Friends.S{s:02d}E{e:02d}.The.One.Numbered.{s}{e}.720p.BluRay.x265-PSA.mkv": 200 * MB
        for s in seasons for e in range(1, episodes + 1)
    }


class Sync(LibraryCase):
    def test_first_scan_populates_the_library(self):
        make_tree(self.media, {
            "Heat (1995) [1080p]/Heat.1995.1080p.BRrip.x264.YIFY.mp4": 2 * GB,
            "The Invite (2026) [1080p]/The.Invite.2026.1080p.WEBRip.x264.mp4": 2 * GB,
            "ES_Project_Demo.mp4": 5 * MB,
            **friends(),
        })
        self.default_duration = 1320.0
        self.durations["Heat.1995.1080p.BRrip.x264.YIFY.mp4"] = 10227.5
        summary = self.scan()

        self.assertEqual((summary.movies, summary.shows, summary.episodes, summary.others), (2, 1, 6, 1))
        self.assertEqual((summary.added, summary.removed, summary.moved, summary.probed), (9, 0, 0, 9))
        self.assertEqual([m.title for m in self.library.movies()], ["Heat", "The Invite"])  # "The" ignored in sorting
        self.assertEqual(self.library.movies()[0].duration, 10227.5)
        self.assertEqual([m.title for m in self.library.others()], ["ES Project Demo"])

        (show,) = self.library.shows()
        self.assertEqual((show.title, show.season_count, show.episode_count), ("Friends", 2, 6))
        self.assertEqual(show.summary, "2 Seasons · 6 Episodes")
        self.assertEqual(self.library.seasons(show.id), [1, 2])
        self.assertEqual([e.episode_code for e in self.library.episodes(show.id, 2)], ["S2:E1", "S2:E2", "S2:E3"])
        self.assertEqual([e.episode_code for e in self.library.episodes(show.id)][:4], ["S1:E1", "S1:E2", "S1:E3", "S2:E1"])

    def test_rescan_without_changes_touches_nothing(self):
        make_tree(self.media, {"Heat (1995) [1080p]/Heat.1995.1080p.mp4": GB, **friends()})
        self.scan()
        ids = {m.path: m.id for m in self.library.movies() + self.library.episodes(self.library.shows()[0].id)}
        summary = self.scan()
        self.assertEqual((summary.added, summary.removed, summary.moved, summary.updated), (0, 0, 0, 0))
        self.assertFalse(summary.changed)
        self.assertEqual(self.probed, [], "unchanged files must not be probed again")
        after = {m.path: m.id for m in self.library.movies() + self.library.episodes(self.library.shows()[0].id)}
        self.assertEqual(ids, after)

    def test_deleted_file_disappears_and_empty_show_is_pruned(self):
        make_tree(self.media, {"Heat (1995) [1080p]/Heat.1995.1080p.mp4": GB, **friends(seasons=(1,), episodes=1)})
        self.scan()
        self.library.save_progress(self.library.movies()[0].id, 100, 1000)
        shutil.rmtree(self.media / "Friends")
        os.unlink(self.media / "Heat (1995) [1080p]/Heat.1995.1080p.mp4")
        make_tree(self.media, {"notes.txt": b"x"})  # (a wholly empty folder would look like an unmounted drive)
        summary = self.scan()
        self.assertEqual(summary.removed, 2)
        self.assertEqual((self.library.movies(), self.library.shows()), ([], []))
        rows = self.library.db.connection().execute("SELECT COUNT(*) FROM playback").fetchone()[0]
        self.assertEqual(rows, 0, "watch progress goes away with its file")

    def test_renamed_folder_keeps_watch_progress(self):
        make_tree(self.media, {"Heat (1995) [1080p]/Heat.1995.1080p.BRrip.x264.YIFY.mp4": 2 * GB})
        self.scan()
        movie = self.library.movies()[0]
        self.library.save_progress(movie.id, 1234.0, 10227.0)

        os.rename(self.media / "Heat (1995) [1080p]", self.media / "Heat (1995) [1080p] [BluRay] [YTS]")
        summary = self.scan()

        self.assertEqual((summary.added, summary.removed, summary.moved), (0, 0, 1))
        moved = self.library.movies()[0]
        self.assertEqual(moved.id, movie.id)
        self.assertIn("[BluRay] [YTS]", moved.path)
        self.assertEqual(moved.position, 1234.0)
        self.assertEqual(self.probed, [], "a moved file is the same file: no new probe")

    def test_renamed_file_is_matched_by_size_and_mtime(self):
        make_tree(self.media, {"old name.mkv": 700 * MB, "other.mkv": 300 * MB})
        self.scan()
        original = self.by_title("old name")
        self.library.save_progress(original.id, 50, 100)
        os.rename(self.media / "old name.mkv", self.media / "new name.mkv")
        summary = self.scan()
        self.assertEqual((summary.added, summary.removed, summary.moved), (0, 0, 1))
        renamed = self.by_title("new name")
        self.assertEqual((renamed.id, renamed.position), (original.id, 50))

    def test_replaced_file_is_probed_again_and_loses_its_thumbnail(self):
        make_tree(self.media, {"Heat (1995) [1080p]/Heat.1995.1080p.mp4": GB})
        self.durations["Heat.1995.1080p.mp4"] = 6000.0
        self.scan()
        movie = self.library.movies()[0]
        self.library.set_thumbnail(movie.id, "/tmp/thumb.jpg")

        make_tree(self.media, {"Heat (1995) [1080p]/Heat.1995.1080p.mp4": 2 * GB})  # a better encode
        self.durations["Heat.1995.1080p.mp4"] = 10227.0
        summary = self.scan()
        self.assertEqual((summary.added, summary.updated, summary.changed), (0, 1, True))
        replaced = self.library.movies()[0]
        self.assertEqual(replaced.id, movie.id)
        self.assertEqual((replaced.duration, replaced.size), (10227.0, 2 * GB))
        self.assertEqual((replaced.thumb_state, replaced.thumb_path), (THUMB_PENDING, None))

    def test_unavailable_folder_keeps_its_library_entries(self):
        external = self.tmp / "external-drive"
        make_tree(external, {"Tenet (2020) [1080p]/Tenet.2020.1080p.mp4": GB})
        make_tree(self.media, {"Heat (1995) [1080p]/Heat.1995.1080p.mp4": GB})
        self.library.add_folder(external)
        self.scan()
        tenet = self.by_title("Tenet")
        self.library.save_progress(tenet.id, 500, 9000)

        os.rename(external, self.tmp / "unplugged")
        summary = self.scan()
        self.assertEqual(summary.removed, 0)
        self.assertEqual(summary.missing_roots, [str(external)])
        self.assertEqual(self.by_title("Tenet").position, 500)

        os.rename(self.tmp / "unplugged", external)
        self.assertEqual((self.scan().added, self.by_title("Tenet").id), (0, tenet.id))

    def test_duration_settles_tentative_classifications(self):
        make_tree(self.media, {
            "Birthday 2024.mp4": 40 * MB,       # a year, but four minutes long
            "Inception.mkv": 2 * GB,            # no tags, but feature length
            "Road Trip 2019.mp4": 900 * MB,     # a year and feature length: stays a movie
            "short thing.mp4": 10 * MB,
        })
        self.durations.update({"Birthday 2024.mp4": 240, "Inception.mkv": 8880, "Road Trip 2019.mp4": 5400, "short thing.mp4": 30})
        self.scan()
        self.assertEqual({(m.title, m.year) for m in self.library.movies()}, {("Inception", None), ("Road Trip", 2019)})
        self.assertEqual({m.title for m in self.library.others()}, {"Birthday 2024", "short thing"})
        again = self.scan()  # and the verdict is stable across rescans
        self.assertFalse(again.changed)
        self.assertEqual(len(self.library.movies()), 2)

    def test_failed_probe_is_retried_only_on_request(self):
        make_tree(self.media, {"broken.mkv": 50 * MB})
        self.scan()
        self.scan()
        self.assertEqual(self.probed, [])
        self.durations["broken.mkv"] = 77.0
        self.scan(retry_failed=True)
        self.assertEqual(len(self.probed), 1)
        self.assertEqual(self.by_title("broken").duration, 77.0)

    def test_name_cache_survives_in_the_database(self):
        make_tree(self.media, friends())
        self.scan()
        cached = self.library.db.connection().execute("SELECT COUNT(*) FROM name_cache").fetchone()[0]
        self.assertGreater(cached, 6)


class Folders(LibraryCase):
    def test_duplicate_and_nested_folders_are_rejected(self):
        make_tree(self.media, {"Movies/Heat (1995) [1080p]/Heat.1995.1080p.mp4": GB})
        added, message = self.library.add_folder(self.media)
        self.assertIsNone(added)
        self.assertIn("already", message)
        added, message = self.library.add_folder(self.media / "Movies")
        self.assertIsNone(added)
        self.assertIn("covered", message)
        added, message = self.library.add_folder(self.tmp / "nowhere")
        self.assertIsNone(added)
        self.assertEqual(len(self.library.folders()), 1)

    def test_parent_folder_takes_over_children_and_their_history(self):
        make_tree(self.media, {"Heat (1995) [1080p]/Heat.1995.1080p.mp4": GB})
        self.scan()
        movie = self.library.movies()[0]
        self.library.save_progress(movie.id, 300, 6000)

        added, _message = self.library.add_folder(self.tmp)  # parent of self.media
        self.assertIsNotNone(added)
        self.assertEqual([f.path for f in self.library.folders()], [str(self.tmp)])
        self.scan()
        same = self.library.movies()[0]
        self.assertEqual((same.id, same.position), (movie.id, 300))

    def test_removing_a_folder_removes_its_media(self):
        make_tree(self.media, {"Heat (1995) [1080p]/Heat.1995.1080p.mp4": GB, **friends()})
        self.scan()
        (folder,) = self.library.folders()
        self.assertEqual(folder.media_count, 7)
        self.library.remove_folder(folder.id)
        self.assertEqual((self.library.movies(), self.library.shows(), self.library.folders()), ([], [], []))

    def test_an_emptied_folder_is_kept_as_offline_not_wiped(self):
        make_tree(self.media, {"Heat (1995) [1080p]/Heat.1995.1080p.mp4": GB})
        self.scan()
        heat = self.library.movies()[0]
        self.library.save_progress(heat.id, 500.0, 1000.0)
        shutil.rmtree(self.media / "Heat (1995) [1080p]")  # an unmounted drive leaves an empty folder
        summary = self.scan()
        self.assertEqual(summary.removed, 0)
        self.assertEqual(summary.missing_roots, [str(self.media)])
        self.assertEqual(self.library.media(heat.id).position, 500.0)

    def test_replaced_file_forgets_a_position_from_the_old_one(self):
        make_tree(self.media, {"Heat (1995) [1080p]/Heat.1995.1080p.mp4": GB})
        self.durations["Heat.1995.1080p.mp4"] = 1000.0
        self.scan()
        heat = self.library.movies()[0]
        self.library.save_progress(heat.id, 500.0, 1000.0)
        make_tree(self.media, {"Heat (1995) [1080p]/Heat.1995.1080p.mp4": GB + 4096})
        self.durations["Heat.1995.1080p.mp4"] = 6000.0
        self.scan()
        self.assertEqual(self.library.media(heat.id).position, 0.0)

    def test_default_folder_is_added_only_once(self):
        self.library.ensure_default_folder()
        count = len(self.library.folders())
        for folder in self.library.folders():
            self.library.remove_folder(folder.id)
        self.library.ensure_default_folder()
        self.assertEqual(self.library.folders(), [], "a library the user emptied stays empty")
        self.assertGreaterEqual(count, 1)


class Playback(LibraryCase):
    def setUp(self):
        super().setUp()
        make_tree(self.media, {
            "Heat (1995) [1080p]/Heat.1995.1080p.mp4": GB,
            "Tenet (2020) [1080p]/Tenet.2020.1080p.mp4": GB,
            "Enemy (2013) [1080p]/Enemy.2013.1080p.mp4": GB,
            **friends(seasons=(1, 2), episodes=3),
        })
        self.default_duration = 1000.0
        self.scan()
        self.show = self.library.shows()[0]
        self.episodes = self.library.episodes(self.show.id)

    def watch(self, media, fraction, when=None):
        self.library.save_progress(media.id, fraction * 1000.0, 1000.0)
        if when is not None:
            self.library.db.connection().execute(
                "UPDATE playback SET last_watched = ? WHERE media_id = ?", (when, media.id)
            )
        return self.library.media(media.id)

    def test_position_and_duration_round_trip(self):
        heat = self.by_title("Heat")
        self.library.save_progress(heat.id, 1234.5, 10227.25)
        saved = self.library.media(heat.id)
        self.assertEqual((saved.position, saved.played_duration), (1234.5, 10227.25))
        self.assertAlmostEqual(saved.last_watched, time.time(), delta=5)
        row = self.library.db.connection().execute(
            "SELECT position_seconds, duration_seconds FROM playback WHERE media_id = ?", (heat.id,)
        ).fetchone()
        self.assertEqual(tuple(row), (1234.5, 10227.25))

    def test_unknown_duration_does_not_erase_a_known_one(self):
        heat = self.by_title("Heat")
        self.library.save_progress(heat.id, 100, 5000)
        self.library.save_progress(heat.id, 200, 0)
        saved = self.library.media(heat.id)
        self.assertEqual((saved.position, saved.played_duration), (200, 5000))

    def test_thresholds(self):
        self.assertEqual((MIN_PLAYED_SECONDS, FINISHED_FRACTION), (5, 0.92))
        heat = self.by_title("Heat")
        for fraction, in_progress, finished, resume in [
            (0.0, False, False, 0.0),
            (0.004, False, False, 0.0),  # 4 s of 1000: under the minimum
            (0.005, True, False, 5.0),   # 5 s: resumes at exactly that second
            (0.019, True, False, 19.0),
            (0.5, True, False, 500.0),
            (0.92, True, False, 920.0),
            (0.921, False, True, 0.0),
            (1.0, False, True, 0.0),
        ]:
            media = self.watch(heat, fraction)
            self.assertEqual(
                (media.in_progress, media.is_finished, media.resume_position),
                (in_progress, finished, resume), f"at {fraction:.1%}",
            )

    def test_continue_watching_lists_partial_items_most_recent_first(self):
        heat, tenet, enemy = (self.by_title(t) for t in ("Heat", "Tenet", "Enemy"))
        self.watch(heat, 0.40, when=1000)
        self.watch(tenet, 0.004, when=4000)  # under 5 s: not listed
        self.watch(enemy, 0.97, when=5000)   # finished: not listed
        self.watch(self.episodes[1], 0.50, when=3000)
        self.watch(self.episodes[4], 0.92, when=2000)
        listed = self.library.continue_watching()
        self.assertEqual([m.id for m in listed], [self.episodes[1].id, heat.id])  # one card per show
        self.assertEqual(listed[0].full_title, "Friends  ·  S1:E2  ·  The One Numbered 12")
        self.assertEqual(listed[0].heading, "Friends")

    def test_continue_watching_moves_a_show_to_its_latest_episode(self):
        self.watch(self.episodes[0], 0.30, when=1000)
        self.watch(self.episodes[3], 0.60, when=2000)
        self.assertEqual([m.id for m in self.library.continue_watching()], [self.episodes[3].id])
        self.watch(self.episodes[3], 1.0, when=3000)  # finished: the card moves on to the next episode
        self.assertEqual([m.id for m in self.library.continue_watching()], [self.episodes[4].id])
        self.assertEqual(self.library.continue_watching()[0].resume_position, 0.0)

    def test_finished_last_episode_leaves_continue_watching(self):
        self.watch(self.episodes[-1], 1.0, when=1000)
        self.assertEqual(self.library.continue_watching(), [])

    def test_continue_watching_shows_latest_episode_despite_older_unfinished_one(self):
        self.watch(self.episodes[0], 0.49, when=1000)
        for offset, episode in enumerate(self.episodes[1:4]):
            self.watch(episode, 0.97, when=2000 + offset)
        self.assertEqual([m.id for m in self.library.continue_watching()], [self.episodes[4].id])
        self.watch(self.episodes[4], 0.05, when=3000)
        self.assertEqual([m.id for m in self.library.continue_watching()], [self.episodes[4].id])

    def test_skipping_through_episodes_does_not_move_continue_watching(self):
        self.watch(self.episodes[1], 0.55, when=1000)
        for episode in self.episodes[2:5]:  # a few seconds each, then Next
            self.library.save_progress(episode.id, 3.0, 1000.0)
        self.assertEqual([m.id for m in self.library.continue_watching()], [self.episodes[1].id])
        self.assertEqual(self.library.media(self.episodes[2].id).last_watched, None)
        self.library.save_progress(self.episodes[1].id, 2.0, 1000.0)  # restarted and left at once
        self.assertEqual(self.library.media(self.episodes[1].id).position, 550.0)

    def test_barely_started_rows_from_older_versions_are_ignored(self):
        self.watch(self.episodes[1], 0.55, when=1000)
        self.library.db.connection().execute(
            "INSERT INTO playback (media_id, position_seconds, duration_seconds, last_watched) VALUES (?, 3, 1000, 2000)",
            (self.episodes[2].id,),
        )
        self.assertEqual([m.id for m in self.library.continue_watching()], [self.episodes[1].id])

    def test_clear_progress_and_mark_watched(self):
        heat = self.by_title("Heat")
        self.watch(heat, 0.5)
        self.library.clear_progress(heat.id)
        self.assertEqual((self.library.media(heat.id).position, self.library.continue_watching()), (0.0, []))
        self.library.mark_watched(heat.id)
        self.assertTrue(self.library.media(heat.id).is_finished)

    def test_track_choices_are_remembered_per_file(self):
        heat = self.by_title("Heat")
        self.library.save_tracks(heat.id, 2, -1, None)
        self.library.save_progress(heat.id, 100, 1000)
        self.library.save_tracks(heat.id, 2, None, "/subs/English.srt")
        saved = self.library.media(heat.id)
        self.assertEqual((saved.audio_track, saved.subtitle_track, saved.subtitle_file, saved.position),
                         (2, None, "/subs/English.srt", 100))

    def test_progress_for_a_vanished_file_is_ignored(self):
        self.library.save_progress(987654, 10, 100)
        self.library.save_tracks(987654, 1, 1, None)

    def test_next_episode_follows_watching_order_across_seasons(self):
        codes = [e.episode_code for e in self.episodes]
        self.assertEqual(codes, ["S1:E1", "S1:E2", "S1:E3", "S2:E1", "S2:E2", "S2:E3"])
        self.assertEqual(self.library.next_episode(self.episodes[2]).episode_code, "S2:E1")
        self.assertIsNone(self.library.next_episode(self.episodes[-1]))
        self.assertIsNone(self.library.next_episode(self.by_title("Heat")))

    # -- what the show's main play button does ---------------------------

    def up(self):
        choice = self.library.next_up(self.show.id)
        return choice.episode.episode_code, choice.action, choice.start_position

    def test_next_up_starts_at_the_beginning_of_an_unwatched_show(self):
        self.assertEqual(self.up(), ("S1:E1", "start", 0.0))
        self.assertEqual(self.library.next_up(self.show.id).label, "Play S1:E1")

    def test_next_up_resumes_the_last_watched_episode(self):
        self.watch(self.episodes[0], 0.99, when=100)
        self.watch(self.episodes[1], 0.40, when=200)
        self.assertEqual(self.up(), ("S1:E2", "resume", 400.0))
        self.assertEqual(self.library.next_up(self.show.id).label, "Resume S1:E2")

    def test_next_up_moves_on_when_the_last_watched_episode_is_finished(self):
        self.watch(self.episodes[0], 0.99, when=100)
        self.watch(self.episodes[1], 0.93, when=200)
        self.assertEqual(self.up(), ("S1:E3", "next", 0.0))

    def test_next_up_crosses_into_the_next_season(self):
        self.watch(self.episodes[2], 1.0, when=100)
        self.assertEqual(self.up(), ("S2:E1", "next", 0.0))

    def test_next_up_skips_episodes_already_finished(self):
        self.watch(self.episodes[2], 1.0, when=50)    # S1:E3 seen earlier
        self.watch(self.episodes[1], 1.0, when=200)   # just finished S1:E2
        self.assertEqual(self.up(), ("S2:E1", "next", 0.0))

    def test_next_up_resumes_a_partially_watched_following_episode(self):
        self.watch(self.episodes[1], 0.30, when=50)
        self.watch(self.episodes[0], 0.95, when=200)
        self.assertEqual(self.up(), ("S1:E2", "resume", 300.0))

    def test_next_up_wraps_to_earlier_unfinished_episodes(self):
        for episode in self.episodes[1:]:
            self.watch(episode, 1.0, when=100)
        self.watch(self.episodes[-1], 1.0, when=500)
        self.assertEqual(self.up(), ("S1:E1", "next", 0.0))

    def test_next_up_offers_a_rewatch_when_everything_is_finished(self):
        for index, episode in enumerate(self.episodes):
            self.watch(episode, 0.99, when=100 + index)
        self.assertEqual(self.up(), ("S1:E1", "restart", 0.0))
        self.assertEqual(self.library.next_up(self.show.id).label, "Watch Again")

    def test_track_choices_alone_do_not_make_an_episode_the_last_watched(self):
        self.watch(self.episodes[1], 0.5, when=1000)
        self.library.save_tracks(self.episodes[3].id, 1, -1, None)  # opened, picked a track, left
        self.assertEqual(self.up(), ("S1:E2", "resume", 500.0))

    def test_a_few_seconds_of_a_later_episode_still_make_it_the_last_watched(self):
        self.watch(self.episodes[1], 0.5, when=1000)
        self.library.save_progress(self.episodes[3].id, 10, 1000)  # skipped ahead, watched 10 s
        self.assertEqual(self.up(), ("S2:E1", "resume", 10.0))
        self.assertEqual(self.library.continue_watching()[0].id, self.episodes[3].id)

    def test_a_glance_under_the_minimum_is_not_saved(self):
        self.watch(self.episodes[1], 0.5, when=1000)
        self.library.save_progress(self.episodes[3].id, 2, 1000)
        self.assertEqual(self.up(), ("S1:E2", "resume", 500.0))

    def test_next_up_of_empty_list(self):
        self.assertIsNone(next_up([]))


class Browsing(LibraryCase):
    def setUp(self):
        super().setUp()
        make_tree(self.media, {
            "Heat (1995) [1080p]/Heat.1995.1080p.mp4": GB,
            "The Invite (2026) [1080p]/The.Invite.2026.1080p.mp4": GB,
            "ES_Project_Demo.mp4": 5 * MB,
            "Friends/S01/Friends.S01E01.The.One.With.the.Thumb.720p.mkv": 200 * MB,
            "Friends/S01/Friends.S01E02.The.One.With.the.Butt.720p.mkv": 200 * MB,
            "Friends/Friends.The.Reunion.2021.1080p.WEB.h264.mkv": GB,
            "Friends/extra bits/outtakes.mkv": 300 * MB,
        })
        self.scan()

    def test_search_matches_every_word_case_insensitively(self):
        found = self.library.search("heat")
        self.assertEqual([m.title for m in found["movies"]], ["Heat"])
        self.assertEqual(self.library.search("INVITE 2026")["movies"][0].title, "The Invite")
        self.assertEqual([m.title for m in self.library.search("demo")["others"]], ["ES Project Demo"])
        self.assertEqual(self.library.search("zzz"), {"shows": [], "movies": [], "others": [], "episodes": []})
        self.assertEqual(self.library.search("   ")["movies"], [])

    def test_search_finds_episodes_by_title_but_not_by_show_name_alone(self):
        self.assertEqual([e.title for e in self.library.search("thumb")["episodes"]], ["The One With the Thumb"])
        by_show = self.library.search("friends")
        self.assertEqual([s.title for s in by_show["shows"]], ["Friends"])
        self.assertEqual(by_show["episodes"], [])

    def test_specials_sort_after_regular_seasons(self):
        show = self.library.shows()[0]
        self.assertEqual(self.library.seasons(show.id), [1, 0])
        self.assertEqual(show.season_count, 1)
        specials = self.library.episodes(show.id, 0)
        self.assertEqual(sorted(e.title for e in specials), ["Outtakes", "The Reunion"])
        self.assertEqual(self.library.episodes(show.id)[-1].season, 0)
        self.assertEqual(specials[0].episode_code, "Special")

    def test_thumbnail_queue_puts_home_screen_items_first(self):
        jobs = self.library.thumbnail_jobs()
        self.assertEqual(len(jobs), 7)
        front = {os.path.basename(j.path) for j in jobs[:4]}
        self.assertEqual(front, {"Heat.1995.1080p.mp4", "The.Invite.2026.1080p.mp4", "ES_Project_Demo.mp4",
                                 "Friends.S01E01.The.One.With.the.Thumb.720p.mkv"})

    def test_thumbnail_states(self):
        heat = self.by_title("Heat")
        thumb = self.library.paths.thumbnails / "heat.jpg"
        thumb.write_bytes(b"jpeg")
        self.library.set_thumbnail(heat.id, str(thumb))
        self.library.set_thumbnail(self.by_title("ES Project Demo").id, None)
        self.assertEqual(self.by_title("Heat").image, str(thumb))
        self.assertEqual(self.by_title("ES Project Demo").thumb_state, THUMB_FAILED)
        self.assertEqual(len(self.library.thumbnail_jobs()), 5)

        thumb.unlink()  # cache wiped behind our back: the item is queued again
        self.assertEqual(len(self.library.thumbnail_jobs()), 6)
        self.assertEqual(self.by_title("Heat").thumb_state, THUMB_PENDING)

        self.scan(retry_failed=True)  # a manual rescan gives failures another chance
        self.assertEqual(len(self.library.thumbnail_jobs()), 7)

    def test_show_card_uses_the_first_episode_frame(self):
        show = self.library.shows()[0]
        self.assertIsNone(show.image)
        first = self.library.episodes(show.id)[0]
        self.assertEqual(show.first_media_id, first.id)
        thumb = self.library.paths.thumbnails / "pilot.jpg"
        thumb.write_bytes(b"jpeg")
        self.library.set_thumbnail(first.id, str(thumb))
        self.assertEqual(self.library.shows()[0].image, str(thumb))
        self.assertEqual(self.library.media(first.id).thumb_state, THUMB_READY)

    def test_unused_cached_images_are_pruned(self):
        old, kept, fresh = (self.library.paths.thumbnails / n for n in ("old.jpg", "kept.jpg", "fresh.jpg"))
        for path in (old, kept, fresh):
            path.write_bytes(b"jpeg")
        two_hours_ago = time.time() - 7200
        os.utime(old, (two_hours_ago, two_hours_ago))
        os.utime(kept, (two_hours_ago, two_hours_ago))
        self.library.set_thumbnail(self.by_title("Heat").id, str(kept))
        self.assertEqual(self.library.prune_thumbnails(), 1)
        self.assertEqual((old.exists(), kept.exists(), fresh.exists()), (False, True, True))

    def test_settings(self):
        self.assertEqual(self.library.setting("volume", "80"), "80")
        self.library.set_setting("volume", 65)
        self.assertEqual(self.library.setting("volume"), "65")
        self.assertTrue(self.library.flag("subtitles", True))
        self.library.set_flag("subtitles", False)
        self.assertFalse(self.library.flag("subtitles", True))


class PureLogic(unittest.TestCase):
    def test_final_kind(self):
        def video(kind, tentative):
            return ScannedVideo(path="/x", root="/", size=1, mtime=0, kind=kind, title="Parsed",
                                tentative=tentative, alt_title="Plain Name")
        self.assertEqual(final_kind(video(KIND_MOVIE, True), 240), (KIND_OTHER, "Plain Name"))
        self.assertEqual(final_kind(video(KIND_MOVIE, True), 5400), (KIND_MOVIE, "Parsed"))
        self.assertEqual(final_kind(video(KIND_MOVIE, True), 0), (KIND_MOVIE, "Parsed"))      # unknown length
        self.assertEqual(final_kind(video(KIND_MOVIE, False), 240), (KIND_MOVIE, "Parsed"))   # a real release folder
        self.assertEqual(final_kind(video(KIND_OTHER, True), 7200), (KIND_MOVIE, "Plain Name"))
        self.assertEqual(final_kind(video(KIND_OTHER, False), 7200), (KIND_OTHER, "Parsed"))  # a long screen recording
        self.assertEqual(final_kind(video(KIND_EPISODE, False), 60), (KIND_EPISODE, "Parsed"))

    def test_episode_codes(self):
        def episode(**kw):
            return Media(id=1, path="/x", kind=KIND_EPISODE, title="T", **kw).episode_code
        self.assertEqual(episode(season=1, episode=3), "S1:E3")
        self.assertEqual(episode(season=9, episode=23, episode_end=24), "S9:E23-24")
        self.assertEqual(episode(season=0, episode=None), "Special")
        self.assertEqual(episode(season=2, episode=None), "S2")
        self.assertEqual(Media(id=1, path="/x", kind=KIND_MOVIE, title="Heat", year=1995).full_title, "Heat (1995)")

    def test_formatting(self):
        self.assertEqual([format_clock(s) for s in (0, 65, 754, 3600, 5025.4)], ["0:00", "1:05", "12:34", "1:00:00", "1:23:45"])
        self.assertEqual([format_duration(s) for s in (0, 14, 1369, 3600, 10227)], ["", "14s", "23m", "1h", "2h 50m"])


if __name__ == "__main__":
    unittest.main()
