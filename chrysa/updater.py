"""Daily update check and self-update, for installs made with ``git clone``.

Chrysa's code lives in a git checkout, and everything the user owns (library database,
watch history, thumbnails, downloaded details) lives in ``~/.local/share/chrysa``, so updating
means fast-forwarding the checkout. ``run.sh`` then refreshes the Python requirements on the next
launch, and the database migrates itself (see :mod:`chrysa.database`).

The update only ever *fast-forwards*. It is refused, with an explanation, when files in the
checkout were edited or local commits exist, so it can never overwrite someone's changes. A copy
without git (a ZIP download) cannot update itself; it is told that a newer version exists.

This is one of the few places Chrysa uses the network: one ``git fetch`` per day.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import __version__

APP_ROOT = Path(__file__).resolve().parent.parent
REPO_URL = "https://github.com/AliAhmed-ui/Chrysa"
VERSION_URL = "https://raw.githubusercontent.com/AliAhmed-ui/Chrysa/main/chrysa/__init__.py"
CHECK_INTERVAL = 24 * 3600  # seconds between automatic checks
LAST_CHECK_SETTING = "update.last_check"
AUTO_SETTING = "update.auto"
DISABLE_ENV = "CHRYSA_NO_UPDATE_CHECK"  # set to 1 to switch the automatic check off (used by the tests)

CURRENT, UPDATED, AVAILABLE, BLOCKED, OFFLINE, FAILED = "current", "updated", "available", "blocked", "offline", "failed"

StopFn = Callable[[], bool]
GitFn = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(slots=True)
class UpdateResult:
    status: str
    message: str
    old_version: str = __version__
    new_version: str | None = None

    @property
    def reached_github(self) -> bool:
        """Whether the check got an answer (so there is no need to repeat it before tomorrow)."""
        return self.status not in (OFFLINE, FAILED)


def parse_version(text: str | None) -> tuple[int, ...]:
    """``1.2.0`` -> (1, 2, 0); anything unreadable -> ()."""
    return tuple(int(part) for part in re.findall(r"\d+", text or "")[:4])


def version_in(source: str) -> str | None:
    found = re.search(r"""__version__\s*=\s*['"]([^'"]+)['"]""", source)
    return found.group(1) if found else None


def due(last_check: str | None, now: float | None = None) -> bool:
    """True when the last successful check is a day old (or never happened)."""
    try:
        return (now if now is not None else time.time()) - float(last_check) >= CHECK_INTERVAL
    except (TypeError, ValueError):
        return True


def is_git_checkout(root: Path = APP_ROOT) -> bool:
    return (root / ".git").exists()


def run_git(root: Path, *args: str, timeout: float = 60.0, should_stop: StopFn = lambda: False):
    """Run ``git -C root args`` without prompts or console windows; stops early when asked to."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        ["git", "-C", str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env, creationflags=flags,
    )
    waited = 0.0
    while True:
        if should_stop():
            process.kill()
            process.communicate()
            raise TimeoutError("git was stopped")
        try:
            out, err = process.communicate(timeout=0.2)
            return subprocess.CompletedProcess(process.args, process.returncode, out, err)
        except subprocess.TimeoutExpired:
            waited += 0.2
            if waited >= timeout:
                process.kill()
                process.communicate()
                raise TimeoutError("git took too long") from None


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Chrysa update check"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read(65536).decode("utf-8", "replace")


def check_for_update(
    apply: bool = True,
    root: Path = APP_ROOT,
    should_stop: StopFn = lambda: False,
    git: GitFn = run_git,
    fetch_text: Callable[[str], str] = _fetch_text,
    current_version: str = __version__,
) -> UpdateResult:
    """Look for a newer Chrysa and, with ``apply``, install it. Never raises."""
    try:
        if is_git_checkout(root):
            return _update_checkout(apply, root, should_stop, git, current_version)
        return _check_copy(fetch_text, current_version)
    except FileNotFoundError:  # git is not installed: treat the folder like a plain download
        try:
            return _check_copy(fetch_text, current_version)
        except Exception as error:
            return _unreachable(error, current_version)
    except Exception as error:
        return _unreachable(error, current_version)


def _unreachable(error: Exception, current_version: str) -> UpdateResult:
    if isinstance(error, (urllib.error.URLError, TimeoutError, OSError)):
        return UpdateResult(OFFLINE, "Could not reach GitHub to check for updates.", current_version)
    return UpdateResult(FAILED, f"Update check failed: {type(error).__name__}: {error}", current_version)


def _check_copy(fetch_text: Callable[[str], str], current_version: str) -> UpdateResult:
    latest = version_in(fetch_text(VERSION_URL))
    if latest and parse_version(latest) > parse_version(current_version):
        return UpdateResult(
            AVAILABLE,
            f"Chrysa {latest} is available. This copy was not installed with git, so it cannot update itself: "
            f"get it from {REPO_URL} (git clone makes later updates automatic).",
            current_version, latest,
        )
    return UpdateResult(CURRENT, "Chrysa is up to date.", current_version, latest)


def _update_checkout(apply: bool, root: Path, should_stop: StopFn, git: GitFn, current_version: str) -> UpdateResult:
    def run(*args: str, timeout: float = 60.0):
        return git(root, *args, timeout=timeout, should_stop=should_stop)

    fetched = run("fetch", "--quiet", "origin")
    if fetched.returncode != 0:
        return UpdateResult(OFFLINE, "Could not reach GitHub to check for updates.", current_version)
    upstream = run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    target = upstream.stdout.strip() if upstream.returncode == 0 and upstream.stdout.strip() else "origin/main"
    if run("rev-parse", "--verify", "--quiet", target).returncode != 0:
        return UpdateResult(FAILED, f"Update check failed: no branch {target} to compare with.", current_version)

    counts = run("rev-list", "--left-right", "--count", f"HEAD...{target}")
    ahead, behind = (int(n) for n in counts.stdout.split()[:2])
    if behind == 0:
        return UpdateResult(CURRENT, "Chrysa is up to date.", current_version)

    shown = run("show", f"{target}:chrysa/__init__.py")
    new_version = version_in(shown.stdout) if shown.returncode == 0 else None
    label = f"Chrysa {new_version}" if new_version and new_version != current_version else "A newer Chrysa"
    if not apply:
        return UpdateResult(
            AVAILABLE, f"{label} is available ({behind} new changes). Switch on More > Update Automatically, "
            "or run `git pull`, to install it.", current_version, new_version,
        )
    if ahead:
        return UpdateResult(
            BLOCKED, f"{label} is available, but this copy has {ahead} local commit(s), so it was not updated "
            "automatically. Run `git pull` yourself.", current_version, new_version,
        )
    dirty = run("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        return UpdateResult(
            BLOCKED, f"{label} is available, but files in the Chrysa folder were edited, so it was not updated "
            "automatically. Commit or stash your changes, then run `git pull`.", current_version, new_version,
        )
    merged = git(root, "merge", "--ff-only", target, timeout=120.0)  # never interrupted half-way
    if merged.returncode != 0:
        detail = (merged.stderr or merged.stdout).strip().splitlines()[-1:] or ["unknown error"]
        return UpdateResult(FAILED, f"Update failed: {detail[0]}", current_version, new_version)
    return UpdateResult(
        UPDATED, f"{label} was installed ({behind} change{'s' if behind != 1 else ''}). "
        "Restart Chrysa to start using it.", current_version, new_version,
    )
