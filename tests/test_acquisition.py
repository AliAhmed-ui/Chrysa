"""From download to Library: performance monitoring, stalls and fallbacks, duplicates, quarantine and validation."""

import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from chrysa import acquire, downloads, presentation, releases, validate
from chrysa.database import Database
from chrysa.downloads import Download, DownloadClient, Monitor
from chrysa.history import History
from tests.helpers import LibraryCase, make_tree

HASH_A, HASH_B, HASH_C = "a" * 40, "b" * 40, "c" * 40
MAG = {h: f"magnet:?xt=urn:btih:{h}&dn=x" for h in (HASH_A, HASH_B, HASH_C)}
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
MB = 1 << 20


def make_video(path: Path, seconds: int = 70, audio: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=5"]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440"]
    command += ["-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast"]
    command += ["-c:a", "aac", "-shortest"] if audio else []
    subprocess.run(command + [str(path)], check=True)


_CACHE = Path(tempfile.mkdtemp(prefix="chrysa-media-"))


def real_video(name: str, **kwargs) -> bytes:
    """Bytes of a small real video (made once per kind)."""
    key = _CACHE / f"{'a' if kwargs.get('audio', True) else 'n'}{kwargs.get('seconds', 70)}{Path(name).suffix}"
    if not key.exists():
        make_video(key, **kwargs)
    return key.read_bytes()


class MonitorTests(unittest.TestCase):
    def feed(self, monitor, samples):
        health = None
        for t, done, speed, peers in samples:
            health = monitor.sample(done, speed, peers, now=t)
        return health

    def test_fast_download_is_healthy(self):
        m = Monitor(now=0)
        health = self.feed(m, [(t, (t - 1) * 8 * MB, 8 * MB, 20) for t in range(1, 60)])
        self.assertEqual(health, "healthy")
        self.assertAlmostEqual(m.average(now=59), 8 * MB, delta=MB)
        self.assertEqual(m.ttfd, 2)
        self.assertEqual(presentation.performance_label(m.average())[0], "⚡ Excellent")

    def test_slow_trickle_is_slow_not_stalled(self):
        m = Monitor(now=0)
        health = self.feed(m, [(t, t * 30 * 1024, 30 * 1024, 40) for t in range(1, 90)])
        self.assertEqual(health, "slow")  # 40 peers advertised, 30 KB/s measured: a poor performer

    def test_brief_pause_is_not_a_stall(self):
        m = Monitor(now=0)
        self.feed(m, [(t, t * MB, MB, 5) for t in range(1, 60)])
        self.assertEqual(self.feed(m, [(t, 59 * MB, 0, 5) for t in range(60, 70)]), "healthy")  # a dip
        self.assertEqual(self.feed(m, [(t, 59 * MB, 0, 5) for t in range(70, 200)]), "slow")  # long, but not yet stalled

    def test_sustained_silence_is_a_stall(self):
        m = Monitor(now=0)
        self.feed(m, [(t, t * MB, MB, 5) for t in range(1, 30)])
        self.assertEqual(self.feed(m, [(t, 29 * MB, 0, 0) for t in range(30, 30 + int(Monitor.STALL_AFTER) + 5)]),
                         "stalled")

    def test_no_first_byte_is_a_stall_eventually(self):
        m = Monitor(now=0)
        self.assertEqual(m.sample(0, 0, 0, now=60), "connecting")
        self.assertEqual(m.sample(0, 0, 0, now=Monitor.FIRST_DATA_TIMEOUT + 1), "stalled")

    def test_resumed_download_measures_only_new_data(self):
        m = Monitor(now=0)
        m.sample(500 * MB, 0, 3, now=1)  # already on disk from last time
        m.sample(510 * MB, MB, 3, now=11)
        self.assertAlmostEqual(m.average(now=11), MB, delta=MB / 10)


class FakeStore:
    """Records what the pipeline tells the history."""

    def __init__(self):
        self.events = []

    def start_download(self, meta):
        self.events.append(("start", meta.get("info_hash")))
        return len(self.events)

    def finish_download(self, row_id, status, **stats):
        self.events.append((status, stats.get("info_hash")))

    def was_verified(self, h):
        return False


class Fallback(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(downloads, "available_backend", return_value="aria2c")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = FakeStore()
        self.pipeline = acquire.Pipeline(self.store)
        self.pipeline.finalize = lambda d, emit: emit(d, state="verified")

    def run_client(self, behaviour, download_kwargs, wait_for=("verified", "failed")):
        done, states = threading.Event(), []

        def on_update(d):
            states.append((d.state, d.message, d.target))
            if d.state in wait_for:
                done.set()

        with mock.patch.object(DownloadClient, "_run_aria2", behaviour):
            client = DownloadClient(self.root, on_update, pipeline=self.pipeline)
            download = client.start(MAG[HASH_A], "Film", **download_kwargs)
            self.assertTrue(done.wait(5))
        return client, download, states

    def test_stalled_torrent_switches_to_an_alternative_once(self):
        def run(client, d):
            if d.info_hash == HASH_A:
                (d.directory / "Film.mkv").write_text("partial")
                d.files.append("Film.mkv")
                d.monitor = Monitor(now=0)  # nothing arrived for ages, no peers
                d.peers, d.progress = 0, 0.1
                if client._can_switch(d):
                    raise downloads._Stalled
            client._emit(d, state="finished")

        _client, download, states = self.run_client(run, {"alternatives": [MAG[HASH_B], MAG[HASH_C]]})
        self.assertEqual(download.info_hash, HASH_B)
        self.assertEqual(download.switches, 1)
        self.assertEqual(download.alternatives, [MAG[HASH_C]])
        self.assertFalse((self.root / "Film.mkv").exists())  # the stalled torrent's partial data is gone
        self.assertIn("switched to an equivalent release", " ".join(m for _, m, _ in states))
        self.assertEqual(self.store.events, [("start", HASH_A), ("stalled", HASH_A), ("start", HASH_B), ])

    def test_conservative_switching_rules(self):
        client = DownloadClient(self.root)
        d = Download(MAG[HASH_A], alternatives=[MAG[HASH_B]])
        d.monitor = Monitor(now=0)
        with mock.patch("time.monotonic", return_value=10_000):
            self.assertTrue(client._can_switch(d))
            d.progress = 0.6
            self.assertFalse(client._can_switch(d))  # half done: keep waiting
            d.progress, d.peers = 0.1, 4
            d.monitor.last_progress = d.monitor.started = 10_000 - Monitor.STALL_AFTER - 1
            d.monitor.first_data = 1
            self.assertFalse(client._can_switch(d))  # peers connected: give them longer
            d.peers, d.switches = 0, DownloadClient.MAX_SWITCHES
            self.assertFalse(client._can_switch(d))  # never flip-flop endlessly
            d.switches = 0
            d.monitor = Monitor(now=10_000)
            self.assertFalse(client._can_switch(d))  # just started: not stalled

    def test_never_switch_onto_a_running_download(self):
        hold = threading.Event()
        self.addCleanup(hold.set)

        def run(client, d):
            hold.wait(5)

        with mock.patch.object(DownloadClient, "_run_aria2", run):
            client = DownloadClient(self.root)
            client.start(MAG[HASH_B])
            d = Download(MAG[HASH_A], alternatives=[MAG[HASH_B]])
            d.monitor = Monitor(now=0)
            with mock.patch("time.monotonic", return_value=10_000):
                self.assertFalse(client._can_switch(d))


class Duplicates(LibraryCase):
    def test_same_torrent_twice_is_refused(self):
        hold = threading.Event()
        self.addCleanup(hold.set)
        with mock.patch.object(downloads, "available_backend", return_value="aria2c"), \
                mock.patch.object(DownloadClient, "_run_aria2", lambda c, d: hold.wait(5)):
            client = DownloadClient(self.tmp)
            client.start(MAG[HASH_A], "Film")
            with self.assertRaisesRegex(downloads.DownloadError, "Already downloading"):
                client.start(MAG[HASH_A].upper().replace("MAGNET:?XT=URN:BTIH:", "magnet:?xt=urn:btih:"))
            reason, hard = acquire.duplicate_reason(client, History(None), None, MAG[HASH_A], None, HASH_A)
            self.assertTrue(hard)
            release = releases.classify("Sintel 2010 1080p WEB")
            client.downloads[0].meta["release_key"] = acquire.release_key(release)
            other = releases.classify("Sintel.2010.720p.BluRay")
            reason, hard = acquire.duplicate_reason(client, History(None), None, MAG[HASH_B], other, HASH_B)
            self.assertEqual((hard, reason.startswith("An equivalent release")), (True, True))

    def test_library_and_history_duplicates_ask_first(self):
        make_tree(self.media, {"Sintel (2010)/Sintel.2010.1080p.mkv": 700 * MB,
                               "Breaking Bad/Season 2/Breaking.Bad.S02E03.mkv": 300 * MB})
        self.default_duration = 3000
        self.scan()
        client, history = DownloadClient(self.tmp), History(self.library.db)
        reason, hard = acquire.duplicate_reason(client, history, self.library.db, MAG[HASH_A],
                                                releases.classify("Sintel 2010 720p WEB-DL"), HASH_A)
        self.assertEqual((reason, hard), ("Already in your Library: Sintel (2010).", False))
        reason, _ = acquire.duplicate_reason(client, history, self.library.db, MAG[HASH_A],
                                             releases.classify("Breaking Bad S02E03 1080p"), HASH_A)
        self.assertIn("S02E03", reason)
        self.assertEqual(acquire.duplicate_reason(client, history, self.library.db, MAG[HASH_A],
                                                  releases.classify("Breaking Bad S02E04 1080p"), HASH_A), ("", False))
        row = history.start_download({"info_hash": HASH_C})
        history.finish_download(row, "verified")
        reason, hard = acquire.duplicate_reason(client, history, None, MAG[HASH_C], None, HASH_C)
        self.assertEqual((hard, "verified before" in reason), (False, True))


class Validation(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def check(self, files, names=None, **kwargs):
        make_tree(self.root, files)
        names = names or sorted({Path(f).parts[0] for f in files})
        return validate.validate(self.root, names, **kwargs)

    def test_signatures_not_extensions(self):
        cases = {
            "fake.mp4": b"\x00" * 64 + b"random bytes",
            "zip.mkv": b"PK\x03\x04" + b"\x00" * 100,
            "renamed.mkv": b"Just a text file pretending to be a film\n" * 10,
            "program.mp4": b"MZ\x90\x00" + b"\x00" * 100,
            "elf.avi": b"\x7fELF" + b"\x00" * 100,
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                report = self.check({f"{name}/{name}": content})
                self.assertEqual(report.verdict, validate.REJECTED)
        self.assertEqual(validate.sniff(self._write("x.bin", b"\x1aE\xdf\xa3" + b"\x00" * 60)), "matroska")
        self.assertEqual(validate.sniff(self._write("y.bin", b"\x00\x00\x00\x18ftypmp42")), "mp4")

    def _write(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_unwanted_files_are_removed_and_executable_content_rejects(self):
        report = self.check({"Pack/Film.mp4.exe": b"MZ" + b"\x00" * 50, "Pack/readme.txt": b"hello"},
                            expect_video=False)
        self.assertFalse((self.root / "Pack/Film.mp4.exe").exists())
        self.assertEqual(report.checks[0].label, "Unwanted files removed")
        report = self.check({"Pack2/notes.srt": b"MZ\x90\x00 disguised program"}, expect_video=False)
        self.assertEqual(report.verdict, validate.REJECTED)

    def test_no_video_is_rejected(self):
        self.assertEqual(self.check({"Pack/notes.txt": b"x"}).reason, "the download contains no video file")

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
    def test_real_mp4_and_mkv_with_subtitles_pass(self):
        files = {"Film/Film.mp4": real_video("a.mp4"), "Film/Film.srt": b"1\n00:00:01,000 --> 00:00:02,000\nHi\n",
                 "Show/E01.mkv": real_video("a.mkv")}
        with mock.patch.object(validate, "scan", return_value=(validate.SCAN_UNAVAILABLE, "ClamAV is not installed")):
            report = self.check(files)
        self.assertEqual(report.verdict, validate.VERIFIED, report.reason)
        labels = {c.label: c.state for c in report.checks}
        for label in ("Valid media container", "Video stream", "Audio stream", "Duration", "Resolution", "File integrity"):
            self.assertEqual(labels[label], validate.PASS, label)
        self.assertEqual(labels["Malware scan unavailable"], validate.SKIP)  # never claims "safe"
        self.assertTrue((self.root / "Film/Film.srt").exists())

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
    def test_corrupt_truncated_and_short_videos_fail(self):
        whole = real_video("a.mkv")
        self.assertIn("corrupt or truncated", self.check({"T/t.mkv": whole[:len(whole) // 4]}).reason)
        mp4 = real_video("a.mp4")
        self.assertEqual(self.check({"M/m.mp4": mp4[:len(mp4) // 4]}).verdict, validate.REJECTED)
        self.assertIn("sample", self.check({"S/s.mp4": real_video("s.mp4", seconds=10)}).reason)
        silent = self.check({"Q/q.mp4": real_video("q.mp4", audio=False)}, malware_scan=False)
        self.assertEqual(silent.verdict, validate.VERIFIED)
        self.assertIn((validate.WARN, "Audio stream"), [(c.state, c.label) for c in silent.checks])

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
    def test_clamav_results(self):
        files = {"Film/Film.mp4": real_video("a.mp4")}
        make_tree(self.root, files)

        def fake_run(stdout, code):
            def run(command, **kwargs):
                if Path(command[0]).name.startswith("clam"):
                    return subprocess.CompletedProcess(command, code, stdout=stdout, stderr="")
                return real(command, **kwargs)
            return run

        real = subprocess.run
        tools = {"clamscan": "/usr/bin/clamscan", "ffprobe": shutil.which("ffprobe"), "ffmpeg": shutil.which("ffmpeg")}
        with mock.patch.object(validate, "_tool", lambda name: tools.get(name)):
            with mock.patch.object(validate.subprocess, "run", fake_run("", 0)):
                report = validate.validate(self.root, ["Film"])
            self.assertEqual((report.verdict, report.scan), (validate.VERIFIED, validate.NO_THREAT))
            with mock.patch.object(validate.subprocess, "run", fake_run("/x/Film.mp4: Eicar-Signature FOUND", 1)):
                report = validate.validate(self.root, ["Film"])
            self.assertEqual((report.verdict, report.scan), (validate.REJECTED, validate.THREAT))
            self.assertIn("Eicar", report.reason)

    def test_ffprobe_unavailable(self):
        make_tree(self.root, {"Film/Film.mkv": b"\x1aE\xdf\xa3" + b"\x00" * 200})
        with mock.patch.object(validate, "_tool", lambda name: None):
            report = validate.validate(self.root, ["Film"])
        self.assertEqual(report.verdict, validate.UNVERIFIED)  # signature fine, streams not inspected
        self.assertEqual(report.scan, validate.SCAN_UNAVAILABLE)
        self.assertIn((validate.SKIP, "Video stream"), [(c.state, c.label) for c in report.checks])

    def test_never_leaves_the_folder(self):
        outside = Path(tempfile.mkdtemp()) / "keep.mkv"
        outside.write_bytes(b"x")
        (self.root / "link").symlink_to(outside.parent)
        validate.validate(self.root, ["../", "link", ".."])
        self.assertTrue(outside.exists())


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.final = Path(tempfile.mkdtemp())
        self.incoming = downloads.incoming_dir(self.final)
        self.db = Database(Path(tempfile.mkdtemp()) / "t.db")
        self.history = History(self.db)
        self.admitted = []
        self.pipeline = acquire.Pipeline(self.history, on_admitted=self.admitted.extend, malware_scan=False)

    def download(self, files):
        make_tree(self.incoming, files)
        d = Download(MAG[HASH_A], name="Film", directory=self.incoming, final_dir=self.final,
                     files=sorted({Path(f).parts[0] for f in files}), meta={"source": "TPB", "title": "Film"})
        d.monitor = Monitor(now=0)
        d.monitor.sample(0, 0, 3, now=1)
        d.monitor.sample(25 * MB, 5 * MB, 3, now=6)
        d.monitor.sample(50 * MB, 5 * MB, 3, now=11)
        self.pipeline.started(d)
        (self.incoming / f"{HASH_A}.torrent").write_bytes(b"d4:infodee")
        return d

    def emit(self, d, **changes):
        for k, v in changes.items():
            setattr(d, k, v)
        self.states.append(d.state)

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
    def test_verified_media_moves_into_the_library_folder(self):
        self.states = []
        (self.final / "Film").mkdir()  # an older copy is never overwritten
        d = self.download({"Film/Film.mp4": real_video("a.mp4"), "Film/Film.srt": b"1\n"})
        self.pipeline.finalize(d, self.emit)
        self.assertEqual(d.state, "verified")
        self.assertEqual(self.admitted, [str(self.final / "Film (2)")])
        self.assertTrue((self.final / "Film (2)" / "Film.mp4").exists())
        self.assertEqual(list(self.incoming.iterdir()), [])  # nothing left in quarantine, not even the .torrent
        self.assertIn("validating", self.states)
        row = self.db.connection().execute("SELECT status, avg_speed, info_hash FROM download_history").fetchone()
        self.assertEqual((row["status"], row["info_hash"]), ("verified", HASH_A))
        self.assertAlmostEqual(row["avg_speed"], 5 * MB, delta=MB)

    def test_rejected_media_never_reaches_the_library(self):
        self.states = []
        d = self.download({"Film/Film.mp4": b"PK\x03\x04 actually a zip"})
        self.pipeline.finalize(d, self.emit)
        self.assertEqual(d.state, "rejected")
        self.assertIn("NOT added to the Library", d.message)
        self.assertEqual(self.admitted, [])
        self.assertEqual(list(self.final.iterdir()), [self.incoming])
        self.assertFalse((self.incoming / "Film").exists())
        status = self.db.connection().execute("SELECT status FROM download_history").fetchone()[0]
        self.assertEqual(status, "rejected")

    def test_incoming_folder_is_hidden_from_the_library_scanner(self):
        self.assertTrue(downloads.INCOMING.startswith("."))


class CancelAndResume(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(downloads, "available_backend", return_value="aria2c")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cancel_deletes_quarantined_files_and_records_it(self):
        final = Path(tempfile.mkdtemp())
        incoming = downloads.incoming_dir(final)
        store, done, updates = FakeStore(), threading.Event(), []

        def run(client, d):
            (d.directory / "Film").mkdir()
            (d.directory / "Film" / "Film.mkv").write_text("partial")
            d.files.append("Film")
            client._emit(d, state="downloading")
            d._stop.wait(5)

        def on_update(d):
            updates.append(d.state)
            if d.state == "cancelled":
                done.set()

        with mock.patch.object(DownloadClient, "_run_aria2", run):
            client = DownloadClient(final, on_update, pipeline=acquire.Pipeline(store))
            d = client.start(MAG[HASH_A], directory=incoming, final_dir=final)
            while "downloading" not in updates:
                threading.Event().wait(0.01)
            client.cancel_and_delete(d)
            self.assertTrue(done.wait(5))
        self.assertFalse((incoming / "Film").exists())
        self.assertIn(("cancelled", HASH_A), store.events)

    def test_interrupted_downloads_remember_where_they_go(self):
        state, final = Path(tempfile.mkdtemp()) / "d.json", Path(tempfile.mkdtemp())
        gate = threading.Event()

        def hold(client, d):
            client._emit(d, state="downloading")
            d._stop.wait(5)
            client._emit(d, state="cancelled")
            gate.set()

        with mock.patch.object(DownloadClient, "_run_aria2", hold):
            client = DownloadClient(final, state_file=state)
            client.start(MAG[HASH_A], "Film", downloads.incoming_dir(final), final_dir=final,
                         alternatives=[MAG[HASH_B]], meta={"source": "YTS", "history_id": 7})
            client.shutdown()
            self.assertTrue(gate.wait(5))
        (entry,) = DownloadClient(final, state_file=state).saved()
        self.assertEqual(entry["final_dir"], str(final))
        self.assertEqual(entry["directory"], str(downloads.incoming_dir(final)))
        self.assertEqual(entry["alternatives"], [MAG[HASH_B]])
        self.assertEqual(entry["meta"], {"source": "YTS"})

    def test_aria2_resumes_instead_of_restarting(self):
        """The aria2 command line keeps --continue and the saved metadata, so a restart picks up the partial file."""
        captured = {}

        class Stop(Exception):
            pass

        def fake_popen(command, **kwargs):
            captured["command"] = command
            raise Stop

        client = DownloadClient(Path(tempfile.mkdtemp()))
        d = Download(MAG[HASH_A], directory=client.directory)
        with mock.patch.object(downloads.subprocess, "Popen", fake_popen), self.assertRaises(Stop):
            client._run_aria2_checked(d, MAG[HASH_A], [])
        for flag in ("--continue=true", "--bt-load-saved-metadata=true", "--allow-overwrite=false"):
            self.assertIn(flag, captured["command"])


if __name__ == "__main__":
    unittest.main()
