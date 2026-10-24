"""Fetch Details: the TVMaze and Wikipedia lookups, with the network replaced by stubs."""

import os
import unittest

from chrysa import metadata
from chrysa.metadata import NetworkError, TVMAZE, WIKIPEDIA_SEARCH, WIKIPEDIA_SUMMARY, best_film, clean_text
from tests.helpers import MB, LibraryCase, make_tree

FRIENDS = {"id": 431, "name": "Friends", "premiered": "1994-09-22", "summary": "<p>Six <b>friends</b> &amp; coffee.</p>",
           "rating": {"average": 8.46}, "image": {"medium": "m.jpg", "original": "https://img/friends.jpg"}}
EPISODES = [
    {"id": 1, "season": 1, "number": 1, "name": "The Pilot", "summary": "<p>Rachel runs.</p>", "airdate": "1994-09-22",
     "rating": {"average": 8.1}, "image": {"original": "https://img/e1.jpg"}},
    {"id": 2, "season": 1, "number": 2, "name": "The Sonogram", "summary": None, "airdate": "1994-09-29",
     "rating": {"average": None}, "image": None},
    {"id": 9, "season": 1, "number": None, "name": "A special", "summary": "<p>x</p>"},
]
IMAGES = [{"type": "poster", "main": True}, {"type": "background", "main": False,
          "resolutions": {"original": {"url": "https://img/bg.jpg"}}}]
HEAT_PAGES = [
    {"key": "Heat_2", "title": "Heat 2", "description": "2022 novel by Michael Mann"},
    {"key": "Heat_(1995_film)", "title": "Heat (1995 film)", "description": "1995 crime drama film by Michael Mann"},
]
HEAT_ARTICLE = {"type": "standard", "extract": "Heat is a 1995 American crime film.",
                "originalimage": {"source": "https://img/heat.jpg"}}


class FakeWeb:
    def __init__(self):
        self.calls: list[str] = []

    def get(self, url, params):
        self.calls.append(url)
        if url == f"{TVMAZE}/search/shows":
            return [{"score": 1, "show": FRIENDS}] if params["q"] == "Friends" else []
        if url == f"{TVMAZE}/shows/431/episodes":
            return EPISODES
        if url == f"{TVMAZE}/shows/431/images":
            return IMAGES
        if url == WIKIPEDIA_SEARCH:
            return {"pages": HEAT_PAGES if params["q"].startswith("Heat") else []}
        if url == WIKIPEDIA_SUMMARY + "Heat_(1995_film)":
            return HEAT_ARTICLE
        return None

    @staticmethod
    def download(url, target):
        with open(target, "wb") as handle:
            handle.write(b"jpg")
        return True


class Parsing(unittest.TestCase):
    def test_summaries_become_plain_text(self):
        self.assertEqual(clean_text("<p>Six <b>friends</b> &amp; coffee.</p>"), "Six friends & coffee.")
        self.assertIsNone(clean_text("<p> </p>"))
        self.assertIsNone(clean_text(None))

    def test_film_pages_are_picked_by_description_and_year(self):
        self.assertEqual(best_film(HEAT_PAGES, "Heat", 1995), "Heat_(1995_film)")
        self.assertIsNone(best_film(HEAT_PAGES, "Heat", 2001))
        self.assertIsNone(best_film([HEAT_PAGES[0]], "Heat", None))


class FetchDetails(LibraryCase):
    def setUp(self):
        super().setUp()
        make_tree(self.media, {
            "Heat (1995)/Heat.1995.mkv": 2 * MB,
            "Obscure Film (2001)/Obscure.Film.2001.mkv": 2 * MB,
            "Friends/Season 1/Friends.S01E01.mkv": 2 * MB,
            "Friends/Season 1/Friends.S01E02.mkv": 2 * MB,
            "Friends/Season 1/Friends.S01E03.mkv": 2 * MB,
        })
        self.default_duration = 5000.0
        self.scan()
        self.web = FakeWeb()
        self.lines: list[tuple[str, str]] = []

    def run_fetch(self, **kwargs):
        kwargs.setdefault("get", self.web.get)
        kwargs.setdefault("download", self.web.download)
        kwargs.setdefault("online", lambda: True)
        kwargs.setdefault("pause", 0)
        return metadata.fetch_missing_details(
            self.library, lambda level, text: self.lines.append((level, text)), **kwargs
        )

    def test_saves_plots_and_artwork_without_any_key(self):
        summary = self.run_fetch()
        self.assertEqual((summary.shows, summary.episodes, summary.movies, summary.not_found), (1, 2, 1, 1))
        show = self.library.shows()[0]
        details = self.library.show_details(show.id)
        self.assertEqual((details.plot, details.rating, details.source_id), ("Six friends & coffee.", 8.5, 431))
        self.assertTrue(os.path.isfile(details.image_path))
        self.assertTrue(os.path.isfile(details.backdrop_path))
        pilot = self.library.episode_details(show.id)[self.library.episodes(show.id)[0].id]
        self.assertEqual((pilot.title, pilot.plot, pilot.air_date), ("The Pilot", "Rachel runs.", "1994-09-22"))
        self.assertTrue(os.path.isfile(pilot.image_path))
        heat = self.library.media_details(self.by_title("Heat").id)
        self.assertEqual(heat.plot, "Heat is a 1995 American crime film.")
        self.assertTrue(os.path.isfile(heat.image_path))

    def test_a_title_nobody_knows_is_recorded_so_it_is_not_asked_again(self):
        self.run_fetch()
        obscure = self.library.media_details(self.by_title("Obscure Film").id)
        self.assertIsNotNone(obscure)
        self.assertIsNone(obscure.plot)
        self.assertEqual(self.library.movies_missing_details(), [])
        self.web.calls.clear()
        self.assertEqual(self.run_fetch().fetched, 0)
        self.assertEqual(self.web.calls, [])

    def test_episode_without_a_plot_is_stored_and_not_refetched(self):
        self.run_fetch()
        show = self.library.shows()[0]
        sonogram = self.library.episode_details(show.id)[self.library.episodes(show.id)[1].id]
        self.assertIsNone(sonogram.plot)
        self.assertIsNone(sonogram.rating)
        self.assertEqual(self.library.shows_missing_details(), [])

    def test_only_the_new_episodes_are_fetched_on_the_next_run(self):
        self.run_fetch()
        make_tree(self.media, {"Friends/Season 1/Friends.S01E04.mkv": 2 * MB})
        self.scan()
        self.web.calls.clear()
        self.run_fetch()
        self.assertEqual(self.web.calls, [f"{TVMAZE}/shows/431/episodes"])

    def test_nothing_is_fetched_without_a_connection(self):
        summary = self.run_fetch(online=lambda: False)
        self.assertTrue(summary.offline)
        self.assertEqual(self.web.calls, [])

    def test_a_dropped_connection_ends_the_run_and_marks_nothing_as_looked_up(self):
        def drop(url, params):
            raise NetworkError("unreachable")

        summary = self.run_fetch(get=drop)
        self.assertTrue(summary.offline)
        self.assertEqual(self.library.movies_missing_details()[0].title, "Heat")

    def test_trouble_with_one_title_does_not_stop_the_others(self):
        def flaky(url, params):
            if url == f"{TVMAZE}/search/shows":
                raise metadata.MetadataError("HTTP 500")
            return self.web.get(url, params)

        summary = self.run_fetch(get=flaky)
        self.assertEqual((summary.failed, summary.movies), (1, 1))

    def test_stopping_keeps_what_was_saved(self):
        stops = iter([False, True])
        summary = self.run_fetch(should_stop=lambda: next(stops, True))
        self.assertTrue(summary.cancelled)
        self.assertTrue(self.library.shows_missing_details() or self.library.movies_missing_details())


if __name__ == "__main__":
    unittest.main()
