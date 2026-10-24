"""The daily update check and self-update, against real throw-away git repositories."""

import os
import subprocess
import unittest
import urllib.error
from pathlib import Path

from chrysa import updater
from chrysa.updater import AVAILABLE, BLOCKED, CURRENT, FAILED, OFFLINE, UPDATED
from tests.helpers import TempDirCase

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, env={**os.environ, **GIT_ENV}, capture_output=True, text=True, check=True,
    )
    return done.stdout


def write_version(repo: Path, version: str, extra: str = "") -> None:
    package = repo / "chrysa"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text(f'"""Chrysa."""\n\n__version__ = "{version}"\n')
    if extra:
        (repo / "notes.txt").write_text(extra)


class Parsing(unittest.TestCase):
    def test_versions_compare_numerically(self):
        self.assertGreater(updater.parse_version("1.10.0"), updater.parse_version("1.9.3"))
        self.assertEqual(updater.parse_version("junk"), ())
        self.assertEqual(updater.version_in('"""x"""\n__version__ = "2.0.1"\n'), "2.0.1")
        self.assertIsNone(updater.version_in("nothing here"))

    def test_the_check_is_due_once_a_day(self):
        self.assertTrue(updater.due(None))
        self.assertTrue(updater.due("garbage"))
        self.assertFalse(updater.due("1000", now=1000 + 3600))
        self.assertTrue(updater.due("1000", now=1000 + 24 * 3600))


class Checkout(TempDirCase):
    def setUp(self):
        super().setUp()
        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "--bare", str(self.origin))
        self.author = self.tmp / "author"
        git(self.tmp, "clone", str(self.origin), str(self.author))
        git(self.author, "checkout", "-b", "main")
        write_version(self.author, "1.0.0", "first")
        git(self.author, "add", ".")
        git(self.author, "commit", "-m", "one")
        git(self.author, "push", "-u", "origin", "main")
        git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        self.app = self.tmp / "app"  # what a user has on disk
        git(self.tmp, "clone", str(self.origin), str(self.app))

    def publish(self, version: str, note: str) -> None:
        write_version(self.author, version, note)
        git(self.author, "add", ".")
        git(self.author, "commit", "-m", note)
        git(self.author, "push")

    def check(self, **kwargs):
        kwargs.setdefault("current_version", "1.0.0")
        return updater.check_for_update(root=self.app, **kwargs)

    def test_up_to_date(self):
        self.assertEqual(self.check().status, CURRENT)

    def test_a_new_version_is_installed_by_fast_forward(self):
        self.publish("1.1.0", "second")
        result = self.check()
        self.assertEqual((result.status, result.new_version), (UPDATED, "1.1.0"))
        self.assertIn("Restart", result.message)
        self.assertEqual((self.app / "notes.txt").read_text(), "second")
        self.assertEqual(self.check().status, CURRENT)

    def test_an_update_without_a_version_bump_is_still_installed(self):
        self.publish("1.0.0", "tweak")
        result = self.check()
        self.assertEqual(result.status, UPDATED)
        self.assertEqual((self.app / "notes.txt").read_text(), "tweak")

    def test_with_auto_update_off_it_only_reports(self):
        self.publish("1.1.0", "second")
        result = self.check(apply=False)
        self.assertEqual((result.status, result.new_version), (AVAILABLE, "1.1.0"))
        self.assertEqual((self.app / "notes.txt").read_text(), "first")

    def test_edited_files_are_never_overwritten(self):
        self.publish("1.1.0", "second")
        (self.app / "notes.txt").write_text("my edit")
        result = self.check()
        self.assertEqual(result.status, BLOCKED)
        self.assertEqual((self.app / "notes.txt").read_text(), "my edit")

    def test_local_commits_block_the_automatic_update(self):
        self.publish("1.1.0", "second")
        (self.app / "mine.txt").write_text("x")
        git(self.app, "add", ".")
        git(self.app, "commit", "-m", "local")
        self.assertEqual(self.check().status, BLOCKED)

    def test_unreachable_remote_is_reported_as_offline(self):
        git(self.app, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        result = self.check()
        self.assertEqual(result.status, OFFLINE)
        self.assertFalse(result.reached_github)

    def test_stopping_kills_a_running_git(self):
        with self.assertRaises(TimeoutError):
            updater.run_git(self.app, "fetch", should_stop=lambda: True, timeout=30)


class PlainCopy(TempDirCase):
    """A ZIP download has no .git: it can only be told that something newer exists."""

    def check(self, remote_version, **kwargs):
        text = f'__version__ = "{remote_version}"\n'
        return updater.check_for_update(root=self.tmp, fetch_text=lambda url: text, current_version="1.0.0", **kwargs)

    def test_newer_release_is_announced_not_installed(self):
        result = self.check("1.2.0")
        self.assertEqual((result.status, result.new_version), (AVAILABLE, "1.2.0"))
        self.assertIn("git clone", result.message)

    def test_same_version_is_current(self):
        self.assertEqual(self.check("1.0.0").status, CURRENT)

    def test_network_failure_is_offline_and_never_raises(self):
        def fail(url):
            raise urllib.error.URLError("no route")

        self.assertEqual(
            updater.check_for_update(root=self.tmp, fetch_text=fail, current_version="1.0.0").status, OFFLINE
        )

    def test_garbage_answer_is_a_failure_not_a_crash(self):
        def explode(url):
            raise ValueError("bad")

        self.assertEqual(
            updater.check_for_update(root=self.tmp, fetch_text=explode, current_version="1.0.0").status, FAILED
        )


if __name__ == "__main__":
    unittest.main()
