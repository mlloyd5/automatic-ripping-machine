#!/usr/bin/env python3
"""
End-to-end test for arm_completed_shipper.py.

Builds a fake COMPLETED_PATH and a temporary ARM sqlite database, points the
shipper at local destination folders and runs it through a small rsync wrapper
that records every call (and can be told to fail for a given folder).

Run with:  python3 -m unittest scripts/completed_shipper/test_arm_completed_shipper.py
Needs a real rsync on PATH.
"""

import os
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import arm_completed_shipper as shipper  # noqa: E402
import mkvheader  # noqa: E402

FAKE_RSYNC = """#!/bin/sh
# Test double: log the call, optionally fail, otherwise run the real rsync.
printf '%s\\n' "$*" >> "$SHIPPER_TEST_LOG"
if [ -n "$SHIPPER_TEST_FAIL_FOR" ]; then
    case "$*" in
        *"$SHIPPER_TEST_FAIL_FOR"*) echo "fake rsync: forced failure" >&2; exit 23 ;;
    esac
fi
exec "$SHIPPER_TEST_REAL_RSYNC" "$@"
"""


class ShipperFixture(unittest.TestCase):
    """A fake COMPLETED_PATH, ARM database, destination folders and rsync wrapper; no tests of its own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arm-shipper-test-")
        self.completed = os.path.join(self.tmp, "completed")
        self.dest = os.path.join(self.tmp, "media-box")
        for sub in ("movies", "tv", "unidentified"):
            os.makedirs(os.path.join(self.completed, sub))
        for sub in ("Movies", "TV", "staging"):
            os.makedirs(os.path.join(self.dest, sub))

        self.db = os.path.join(self.tmp, "arm.db")
        self._create_db()
        self.log = os.path.join(self.tmp, "rsync.log")
        self.fake_rsync = os.path.join(self.tmp, "rsync")
        with open(self.fake_rsync, "w") as handle:
            handle.write(FAKE_RSYNC)
        os.chmod(self.fake_rsync, os.stat(self.fake_rsync).st_mode | stat.S_IXUSR)
        real = shutil.which("rsync")
        self.assertIsNotNone(real, "rsync must be installed to run this test")
        os.environ["SHIPPER_TEST_LOG"] = self.log
        os.environ["SHIPPER_TEST_REAL_RSYNC"] = real
        os.environ.pop("SHIPPER_TEST_FAIL_FOR", None)

        self.conf = os.path.join(self.tmp, "shipper.conf")
        with open(self.conf, "w") as handle:
            handle.write(f"""[shipper]
db_path = {self.db}
completed_path = {self.completed}
remote_host =
remote_movies_path = {self.dest}/Movies
remote_tv_path = {self.dest}/TV
remote_unidentified_path = {self.dest}/staging
bwlimit_recognized = 0
bwlimit_unidentified = 500
rsync_path = {self.fake_rsync}
rsync_timeout = 30
lock_file = {self.tmp}/shipper.lock
post_ship_command = echo "$SHIPPER_TYPE $SHIPPER_FOLDER" >> {self.tmp}/hook.log
""")

        # Job folders and matching DB rows.
        self._job("movies", "Movie A (2001)", "success", files=["Movie A (2001).mkv", "extras/trailer.mkv"])
        self._job("movies", "Movie B (2002)", "active")           # still ripping: must stay
        self._job("movies", "Movie D (2004)", "fail")             # failed: must stay
        self._folder("movies", "Movie C (2003)")                  # no DB row at all: must stay
        self._job("movies", "Movie E (2005)", "success")
        self._job("movies", "Movie E (2005)", "ripping", make_folder=False)  # second job re-using the path
        self._job("tv", "Show A (2010)", "success", files=["S01E01.mkv", "S01E02.mkv"], trailing_slash=True)
        self._job("unidentified", "UNKNOWN_DISC", "success", files=["title_t00.mkv"])
        self._job("unidentified", "UNKNOWN_2", "success", files=["title_t00.mkv"])
        # A job living outside COMPLETED_PATH (e.g. music) must be ignored even though it is a success.
        self._row("success", os.path.join(self.tmp, "music", "Album"))
        self.spool = os.path.join(self.completed, "catalog")
        self.records = os.path.join(self.dest, "catalog")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ helpers
    def _create_db(self):
        conn = sqlite3.connect(self.db)
        conn.execute("""CREATE TABLE job (
            job_id INTEGER PRIMARY KEY, status VARCHAR(32), path VARCHAR(256),
            video_type VARCHAR(20), title VARCHAR(256), title_manual VARCHAR(256), year VARCHAR(4),
            year_manual VARCHAR(4), imdb_id VARCHAR(15), label VARCHAR(256), disctype VARCHAR(20),
            disc_barcode VARCHAR(32), disc_barcode_title VARCHAR(256), sleeve_id VARCHAR(16),
            box VARCHAR(64), stop_time DATETIME)""")
        conn.commit()
        conn.close()

    def _row(self, status, path):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO job (status, path, video_type, title) VALUES (?, ?, ?, ?)",
                     (status, path, "movie", os.path.basename(path)))
        conn.commit()
        conn.close()

    def _folder(self, sub, name, files=("main.mkv",)):
        folder = os.path.join(self.completed, sub, name)
        for rel in files:
            full = os.path.join(folder, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as handle:
                handle.write(f"content of {rel}\n")
        return folder

    def _job(self, sub, name, status, files=("main.mkv",), make_folder=True, trailing_slash=False):
        folder = os.path.join(self.completed, sub, name)
        if make_folder:
            self._folder(sub, name, files)
        self._row(status, folder + ("/" if trailing_slash else ""))
        return folder

    def _calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as handle:
            return [line.rstrip("\n") for line in handle if line.strip()]

    def _local(self, sub, name):
        return os.path.join(self.completed, sub, name)

    def _shipped(self, dest_sub, name):
        return os.path.join(self.dest, dest_sub, name)

    def _run(self, *extra):
        return shipper.main(["-c", self.conf, *extra])

    def _set_job(self, name, **fields):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE job SET %s WHERE path LIKE ?" % ", ".join(f"{k} = ?" for k in fields),
                     (*fields.values(), f"%{name}%"))
        conn.commit()
        conn.close()


class ShipperEndToEnd(ShipperFixture):
    def test_ships_recognized_first_then_unidentified(self):
        rc = self._run()
        self.assertEqual(rc, 0)

        # Finished recognized rips arrived intact and the local copies are gone.
        movie_a = self._shipped("Movies", "Movie A (2001)")
        self.assertTrue(os.path.isfile(os.path.join(movie_a, "Movie A (2001).mkv")))
        self.assertTrue(os.path.isfile(os.path.join(movie_a, "extras", "trailer.mkv")))
        self.assertTrue(os.path.isfile(os.path.join(self._shipped("TV", "Show A (2010)"), "S01E02.mkv")))
        self.assertFalse(os.path.exists(self._local("movies", "Movie A (2001)")))
        self.assertFalse(os.path.exists(self._local("tv", "Show A (2010)")))

        # Unidentified rips went to staging, throttled.
        for name in ("UNKNOWN_DISC", "UNKNOWN_2"):
            self.assertTrue(os.path.isfile(os.path.join(self._shipped("staging", name), "title_t00.mkv")))
            self.assertFalse(os.path.exists(self._local("unidentified", name)))

        # Unfinished, failed, unknown and re-used-path folders stay put.
        for name in ("Movie B (2002)", "Movie C (2003)", "Movie D (2004)", "Movie E (2005)"):
            self.assertTrue(os.path.isdir(self._local("movies", name)), name)
            self.assertFalse(os.path.exists(self._shipped("Movies", name)), name)

        # The movies/ and tv/ root folders themselves survive; no catalog spool appears when the
        # feature is off.
        for sub in ("movies", "tv", "unidentified"):
            self.assertTrue(os.path.isdir(os.path.join(self.completed, sub)))
        self.assertFalse(os.path.exists(self.spool))

        calls = self._calls()
        self.assertEqual(len(calls), 4)
        kinds = ["/completed/movies/" in c and "movies" or "/completed/tv/" in c and "tv" or "unidentified"
                 for c in calls]
        self.assertEqual(kinds, ["movies", "tv", "unidentified", "unidentified"])
        for call in calls[:2]:
            self.assertNotIn("--bwlimit", call)
            self.assertIn("--remove-source-files", call)
            self.assertIn("--partial-dir=.rsync-partial", call)
        for call in calls[2:]:
            self.assertIn("--bwlimit=500", call)

        with open(os.path.join(self.tmp, "hook.log")) as handle:
            hooks = handle.read().splitlines()
        self.assertEqual(hooks[:2], ["movies Movie A (2001)", "tv Show A (2010)"])
        self.assertCountEqual(hooks[2:], ["unidentified UNKNOWN_DISC", "unidentified UNKNOWN_2"])

    def test_second_run_is_a_noop(self):
        self.assertEqual(self._run(), 0)
        os.remove(self.log)
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._calls(), [])

    def test_recognized_failure_blocks_unidentified(self):
        os.environ["SHIPPER_TEST_FAIL_FOR"] = "Movie A (2001)"
        rc = self._run()
        self.assertEqual(rc, 1)
        # Failed folder is untouched locally, tv still shipped, unidentified never started.
        self.assertTrue(os.path.isfile(os.path.join(self._local("movies", "Movie A (2001)"), "Movie A (2001).mkv")))
        self.assertTrue(os.path.isfile(os.path.join(self._shipped("TV", "Show A (2010)"), "S01E01.mkv")))
        self.assertTrue(os.path.isdir(self._local("unidentified", "UNKNOWN_DISC")))
        self.assertFalse(any("unidentified" in c for c in self._calls()))

        # Once the problem clears, the next run finishes the job.
        os.environ.pop("SHIPPER_TEST_FAIL_FOR")
        self.assertEqual(self._run(), 0)
        self.assertFalse(os.path.exists(self._local("movies", "Movie A (2001)")))
        self.assertFalse(os.path.exists(self._local("unidentified", "UNKNOWN_DISC")))

    def test_unidentified_failure_does_not_block_the_rest(self):
        os.environ["SHIPPER_TEST_FAIL_FOR"] = "UNKNOWN_DISC"
        rc = self._run()
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.isdir(self._local("unidentified", "UNKNOWN_DISC")))
        self.assertFalse(os.path.exists(self._local("unidentified", "UNKNOWN_2")))
        self.assertTrue(os.path.isfile(os.path.join(self._shipped("staging", "UNKNOWN_2"), "title_t00.mkv")))

    def test_interrupted_transfer_resumes_on_rerun(self):
        # Pretend an earlier run copied one episode and was killed before removing the source.
        partial = self._shipped("TV", "Show A (2010)")
        os.makedirs(partial)
        shutil.copy(os.path.join(self._local("tv", "Show A (2010)"), "S01E01.mkv"), partial)
        self.assertEqual(self._run(), 0)
        self.assertTrue(os.path.isfile(os.path.join(partial, "S01E01.mkv")))
        self.assertTrue(os.path.isfile(os.path.join(partial, "S01E02.mkv")))
        self.assertFalse(os.path.exists(self._local("tv", "Show A (2010)")))

    def test_dry_run_moves_nothing(self):
        rc = self._run("--dry-run")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(self._local("movies", "Movie A (2001)")))
        self.assertFalse(os.path.exists(self._shipped("Movies", "Movie A (2001)")))
        calls = self._calls()
        self.assertEqual(len(calls), 4)
        self.assertTrue(all("--dry-run" in c for c in calls))

    def test_lock_prevents_overlapping_runs(self):
        cfg = shipper.load_config(self.conf)
        held = shipper.acquire_lock(cfg["lock_file"])
        try:
            self.assertEqual(self._run(), 2)
            self.assertEqual(self._calls(), [])
        finally:
            held.close()

    def _use_container_paths_in_db(self, arm_root="/home/arm/media/completed"):
        # ARM runs in a container: job.path holds the container path, not this box's path.
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE job SET path = replace(path, ?, ?)", (self.completed, arm_root))
        # Same layout under another prefix: must be ignored even though a local folder of that name exists.
        conn.execute("INSERT INTO job (status, path) VALUES ('success', '/srv/other/completed/movies/Movie C (2003)')")
        conn.commit()
        conn.close()

    def _assert_container_path_run(self):
        movie_a = self._shipped("Movies", "Movie A (2001)")
        self.assertTrue(os.path.isfile(os.path.join(movie_a, "extras", "trailer.mkv")))
        self.assertFalse(os.path.exists(self._local("movies", "Movie A (2001)")))
        self.assertTrue(os.path.isfile(os.path.join(self._shipped("TV", "Show A (2010)"), "S01E02.mkv")))
        for name in ("UNKNOWN_DISC", "UNKNOWN_2"):
            self.assertTrue(os.path.isfile(os.path.join(self._shipped("staging", name), "title_t00.mkv")))
            self.assertFalse(os.path.exists(self._local("unidentified", name)))
        for name in ("Movie B (2002)", "Movie C (2003)", "Movie D (2004)", "Movie E (2005)"):
            self.assertTrue(os.path.isdir(self._local("movies", name)), name)
            self.assertFalse(os.path.exists(self._shipped("Movies", name)), name)
        self.assertEqual(len(self._calls()), 4)

    def test_db_paths_from_container_are_mapped_to_local_path(self):
        self._use_container_paths_in_db()
        # Without the mapping nothing in the DB is under completed_path, so nothing ships.
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._calls(), [])
        with open(self.conf, "a") as handle:
            handle.write("arm_completed_path = /home/arm/media/completed/\n")
        self.assertEqual(self._run(), 0)
        self._assert_container_path_run()

    def test_arm_completed_path_cli_override(self):
        self._use_container_paths_in_db("/home/arm/media/completed")
        self.assertEqual(self._run("--arm-completed-path", "/home/arm/media/completed"), 0)
        self._assert_container_path_run()

    def test_job_newly_finished_between_passes_is_picked_up(self):
        # Movie B finishes while the shipper is working: a later pass must ship it.
        self.assertEqual(self._run(), 0)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE job SET status = 'success' WHERE path LIKE '%Movie B%'")
        conn.commit()
        conn.close()
        self.assertEqual(self._run(), 0)
        self.assertTrue(os.path.isfile(os.path.join(self._shipped("Movies", "Movie B (2002)"), "main.mkv")))


class RsyncCommandShape(unittest.TestCase):
    def test_remote_destination_uses_ssh_and_protects_args(self):
        cfg = dict(shipper.DEFAULTS, remote_host="arm@t620", remote_movies_path="/srv/plex/Movies",
                   remote_tv_path="/srv/plex/TV", remote_unidentified_path="/srv/staging",
                   bwlimit_unidentified="8000", ssh_options="-o BatchMode=yes -i /home/arm/.ssh/shipper",
                   rsync_extra_args="--chown=plex:plex")
        cmd = shipper.build_rsync_command(cfg, "unidentified", "/home/arm/media/completed/unidentified/DISC X")
        self.assertEqual(cmd[0], "rsync")
        self.assertIn("--bwlimit=8000", cmd)
        self.assertIn("--remove-source-files", cmd)
        self.assertIn("--partial-dir=.rsync-partial", cmd)
        self.assertNotIn("--partial", cmd)
        self.assertIn("--chown=plex:plex", cmd)
        # -s (--protect-args): the raw path travels inside the protocol, never through the remote shell.
        self.assertIn("-s", cmd)
        self.assertEqual(cmd[cmd.index("-e") + 1], "ssh -o BatchMode=yes -i /home/arm/.ssh/shipper")
        self.assertEqual(cmd[-2], "/home/arm/media/completed/unidentified/DISC X/")
        self.assertEqual(cmd[-1], "arm@t620:/srv/staging/DISC X/")
        self.assertEqual(shipper.destination(cfg, "unidentified", "/x/unidentified/DISC X"),
                         "arm@t620:/srv/staging/DISC X/")

    def test_recognized_without_bwlimit(self):
        cfg = dict(shipper.DEFAULTS, remote_movies_path="/m", remote_tv_path="/t", remote_unidentified_path="/u",
                   bwlimit_unidentified="8000")
        cmd = shipper.build_rsync_command(cfg, "movies", "/c/movies/Film (1999)")
        self.assertFalse(any(part.startswith("--bwlimit") for part in cmd))
        self.assertNotIn("-e", cmd)
        self.assertIn("--partial-dir=.rsync-partial", cmd)
        self.assertEqual(cmd[-1], "/m/Film (1999)/")

    def test_missing_destination_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "c.conf")
            with open(conf, "w") as handle:
                handle.write("[shipper]\nremote_movies_path = /m\n")
            with self.assertRaises(shipper.ShipperError):
                shipper.load_config(conf)

    def test_percent_in_config_is_literal(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "c.conf")
            hook = 'curl -fsS "http://t620:32400/library/sections/all/refresh?path=/tank/media/Movie%20A"; date +%s'
            with open(conf, "w") as handle:
                handle.write(f"[shipper]\nremote_movies_path = /m\nremote_tv_path = /t\n"
                             f"remote_unidentified_path = /u\npost_ship_command = {hook}\n")
            cfg = shipper.load_config(conf)
            self.assertEqual(cfg["post_ship_command"], hook)
            self.assertEqual(os.path.realpath(cfg["arm_completed_path"]), cfg["completed_path"])


# ---------------------------------------------------------------------- minimal Matroska files

def ebml(element_id, payload):
    """One EBML element: id bytes, a size vint, the payload."""
    if isinstance(payload, int):
        payload = payload.to_bytes(max(1, (payload.bit_length() + 7) // 8), "big")
    elif isinstance(payload, str):
        payload = payload.encode("utf-8")
    size = len(payload)
    length = 1
    while size >= (1 << (7 * length)) - 1:
        length += 1
    return bytes.fromhex(element_id) + (size | (1 << (7 * length))).to_bytes(length, "big") + payload


def make_mkv(path, width=3840, height=2160, seconds=5400.0, video="V_MPEGH/ISO/HEVC",
             audio=(("eng", "A_TRUEHD"), ("und", "A_AC3")), subs=2, chapters=3, title="Test Title"):
    """Write a Matroska file with just the header elements a catalog reads, then one Cluster."""
    header = ebml("1A45DFA3", ebml("4286", 1) + ebml("4282", "matroska") + ebml("4287", 4) + ebml("4285", 2))
    info = ebml("1549A966", ebml("2AD7B1", 1000000) + ebml("4489", struct.pack(">d", seconds * 1000)) + ebml("7BA9", title))
    tracks = ebml("AE", ebml("D7", 1) + ebml("83", 1) + ebml("86", video)
                  + ebml("E0", ebml("B0", width) + ebml("BA", height)))
    for number, (lang, codec) in enumerate(audio, 2):
        tracks += ebml("AE", ebml("D7", number) + ebml("83", 2) + ebml("86", codec) + ebml("22B59C", lang))
    for number in range(subs):
        tracks += ebml("AE", ebml("D7", 10 + number) + ebml("83", 0x11) + ebml("86", "S_TEXT/UTF8"))
    tracks = ebml("1654AE6B", tracks)
    atoms = b"".join(ebml("B6", ebml("73C4", 1000 + n) + ebml("91", n * 1000000)) for n in range(chapters))
    chapter_element = ebml("1043A770", ebml("45B9", ebml("45BC", 1) + atoms)) if chapters else b""
    cluster = ebml("1F43B675", ebml("E7", 0) + ebml("A3", b"\x81\x00\x00\x80" + b"\x00" * 64))
    with open(path, "wb") as handle:
        handle.write(header + ebml("18538067", info + tracks + chapter_element + cluster))
    return path


class MatroskaHeader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arm-mkvheader-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_probe_reads_info_tracks_and_chapters(self):
        path = make_mkv(os.path.join(self.tmp, "film.mkv"))
        info = mkvheader.probe(path)
        self.assertEqual(info["size"], os.path.getsize(path))
        self.assertAlmostEqual(info["seconds"], 5400.0)
        self.assertEqual((info["width"], info["height"]), (3840, 2160))
        self.assertEqual(info["video"], "MPEGH/ISO/HEVC")
        self.assertEqual(info["audio"], ["eng/TRUEHD", "und/AC3"])
        self.assertEqual(info["subs"], 2)
        self.assertEqual(info["chapters"], 3)
        self.assertEqual(info["title"], "Test Title")
        row = mkvheader.probe_row(path)
        self.assertTrue(row["probed"])
        self.assertEqual((row["seconds"], row["resolution"], row["audio"], row["subs"], row["chapters"]),
                         ("5400", "3840x2160", "eng/TRUEHD,und/AC3", "2", "3"))

    def test_dvd_sized_file_without_chapters_or_subs(self):
        path = make_mkv(os.path.join(self.tmp, "dvd.mkv"), width=720, height=480, seconds=100.5,
                        video="V_MPEG2", audio=(("fra", "A_AC3"),), subs=0, chapters=0, title="")
        row = mkvheader.probe_row(path)
        self.assertEqual((row["seconds"], row["resolution"], row["video"], row["audio"], row["subs"], row["chapters"]),
                         ("100", "720x480", "MPEG2", "fra/AC3", "0", "0"))

    def test_other_files_are_listed_by_size_only(self):
        path = os.path.join(self.tmp, "notes.mkv")
        with open(path, "w") as handle:
            handle.write("this is not a matroska file\n")
        with self.assertRaises(mkvheader.NotMatroska):
            mkvheader.probe(path)
        row = mkvheader.probe_row(path)
        self.assertFalse(row["probed"])
        self.assertEqual(row["size"], str(os.path.getsize(path)))
        self.assertEqual((row["seconds"], row["resolution"], row["video"], row["chapters"]), ("", "", "", ""))
        missing = mkvheader.probe_row(os.path.join(self.tmp, "absent.mkv"))
        self.assertEqual(missing["size"], "0")

    def test_command_line_prints_one_tsv_row_per_file(self):
        make_mkv(os.path.join(self.tmp, "a.mkv"))
        os.makedirs(os.path.join(self.tmp, "sub"))
        make_mkv(os.path.join(self.tmp, "sub", "b.mkv"))
        result = subprocess.run([sys.executable, mkvheader.__file__, self.tmp], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].split("\t")[3], "3840x2160")


# ---------------------------------------------------------------------- catalog records

def parse_record(text):
    """Enough TOML for the records this shipper writes: top-level keys and [[files]] tables."""
    top, files, current = {}, [], None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[[files]]":
            current = {}
            files.append(current)
            continue
        key, _, value = line.partition(" = ")
        if value.startswith('"'):
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        elif value in ("true", "false"):
            value = value == "true"
        else:
            value = int(value)
        (top if current is None else current)[key] = value
    return top, files


class CatalogRecords(ShipperFixture):
    def setUp(self):
        super().setUp()
        os.makedirs(self.records)
        with open(self.conf, "a") as handle:
            handle.write(f"remote_catalog_path = {self.records}\nripper_name = ts140\n")
        # Movie A's feature is a real (header-only) Matroska file; its trailer is not.
        make_mkv(os.path.join(self._local("movies", "Movie A (2001)"), "Movie A (2001).mkv"))
        self._set_job("Movie A (2001)", title="Movie A", year="2001", imdb_id="tt0000001", label="MOVIE_A_UHD",
                      disctype="bluray", disc_barcode="025192412363", disc_barcode_title="Movie A Trilogy (4K UHD)",
                      sleeve_id="TS-0007", box="Box 02", stop_time="2026-10-25 14:03:11")
        make_mkv(os.path.join(self._local("tv", "Show A (2010)"), "S01E01.mkv"), width=1920, height=1080,
                 seconds=1500, video="V_MPEG4/ISO/AVC", chapters=0)
        make_mkv(os.path.join(self._local("tv", "Show A (2010)"), "S01E02.mkv"), width=1920, height=1080,
                 seconds=1520, video="V_MPEG4/ISO/AVC", chapters=0)
        self._set_job("Show A (2010)", title="Show A", year="2010", video_type="series", label="SHOW_A_S1_D1",
                      disctype="bluray")

    def _records(self):
        return sorted(os.listdir(self.records))

    def _spooled(self):
        return sorted(os.listdir(self.spool)) if os.path.isdir(self.spool) else []

    def test_recognized_rips_leave_a_record_on_the_media_box(self):
        self.assertEqual(self._run(), 0)
        names = self._records()
        self.assertEqual(len(names), 2, names)
        self.assertEqual(self._spooled(), [])
        movie = next(n for n in names if "-job1" in n)
        self.assertTrue(movie.startswith("20261025-140311-ts140-job1"), movie)
        with open(os.path.join(self.records, movie)) as handle:
            top, files = parse_record(handle.read())
        self.assertEqual(top["ripper"], "ts140")
        self.assertEqual(top["job"], 1)
        self.assertEqual((top["type"], top["title"], top["year"], top["imdb"]), ("movie", "Movie A", 2001, "tt0000001"))
        self.assertEqual(top["library"], "movies/Movie A (2001)")
        self.assertEqual(top["destination"], os.path.join(self.dest, "Movies", "Movie A (2001)"))
        self.assertEqual((top["label"], top["format"], top["role"]), ("MOVIE_A_UHD", "uhd", "feature"))
        self.assertEqual((top["sleeve"], top["box"]), ("TS-0007", "Box 02"))
        self.assertEqual((top["barcode"], top["barcode_title"]), ("025192412363", "Movie A Trilogy (4K UHD)"))
        self.assertIn("shipped", top)
        self.assertEqual([f["path"] for f in files],
                         ["movies/Movie A (2001)/Movie A (2001).mkv", "movies/Movie A (2001)/extras/trailer.mkv"])
        feature, trailer = files
        self.assertTrue(feature["main"])
        self.assertEqual((feature["seconds"], feature["resolution"], feature["video"], feature["audio"],
                          feature["subs"], feature["chapters"], feature["title"]),
                         (5400, "3840x2160", "MPEGH/ISO/HEVC", "eng/TRUEHD,und/AC3", 2, 3, "Test Title"))
        self.assertFalse(trailer["main"])
        self.assertGreater(trailer["size"], 0)
        self.assertNotIn("resolution", trailer)

        show = next(n for n in names if n != movie)
        with open(os.path.join(self.records, show)) as handle:
            top, files = parse_record(handle.read())
        self.assertEqual((top["type"], top["title"], top["library"], top["format"], top["role"]),
                         ("series", "Show A", "tv/Show A (2010)", "bd", "feature"))
        self.assertNotIn("sleeve", top)
        self.assertNotIn("barcode", top)
        self.assertEqual([f["main"] for f in files], [False, False])
        # Unidentified rips shipped as before and left no record.
        self.assertFalse(os.path.exists(self._local("unidentified", "UNKNOWN_DISC")))

    def test_record_failure_keeps_it_spooled_and_ships_it_next_run(self):
        os.environ["SHIPPER_TEST_FAIL_FOR"] = "media-box/catalog/"
        self.assertEqual(self._run(), 1)
        # The folders themselves shipped; only the records are waiting.
        self.assertFalse(os.path.exists(self._local("movies", "Movie A (2001)")))
        self.assertTrue(os.path.isfile(os.path.join(self._shipped("TV", "Show A (2010)"), "S01E02.mkv")))
        self.assertEqual(self._records(), [])
        self.assertEqual(len(self._spooled()), 2)
        os.environ.pop("SHIPPER_TEST_FAIL_FOR")
        os.remove(self.log)
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self._records()), 2)
        self.assertEqual(self._spooled(), [])
        # Only the two record transfers ran: the folders were already gone.
        self.assertEqual(len(self._calls()), 2)
        self.assertTrue(all("/catalog/" in c and "--remove-source-files" not in c for c in self._calls()))

    def test_dry_run_writes_no_record(self):
        self.assertEqual(self._run("--dry-run"), 0)
        self.assertEqual(self._records(), [])
        self.assertFalse(os.path.exists(self.spool))

    def test_stock_arm_database_without_fork_columns(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE job2 AS SELECT job_id, status, path FROM job")
        conn.execute("DROP TABLE job")
        conn.execute("ALTER TABLE job2 RENAME TO job")
        conn.commit()
        conn.close()
        self.assertEqual(self._run(), 0)
        names = self._records()
        self.assertEqual(len(names), 2)
        movie = next(n for n in names if "-job1" in n)
        with open(os.path.join(self.records, movie)) as handle:
            top, files = parse_record(handle.read())
        # Title falls back to the folder name, the format to the picture size, ids to nothing.
        self.assertEqual((top["title"], top["format"], top["role"]), ("Movie A (2001)", "uhd", "feature"))
        for key in ("year", "imdb", "sleeve", "barcode", "box"):
            self.assertNotIn(key, top)
        self.assertEqual(top["label"], "")

    def test_folder_without_media_files_gets_no_record(self):
        self._job("movies", "Movie F (2006)", "success", files=["disc.jpg", "disc.barcode"])
        self.assertEqual(self._run(), 0)
        self.assertFalse(os.path.exists(self._local("movies", "Movie F (2006)")))
        self.assertEqual(len(self._records()), 2)
        for name in self._records():
            with open(os.path.join(self.records, name)) as handle:
                self.assertNotIn("Movie F", handle.read())

    def test_record_rsync_command_shape(self):
        cfg = dict(shipper.DEFAULTS, remote_host="root@t620", remote_catalog_path="/tank/rips/catalog",
                   ssh_options="-o BatchMode=yes -i /root/.ssh/arm-shipper", rsync_extra_args="--chmod=D755,F644")
        cmd = shipper.build_record_rsync_command(cfg, "/scratch/rips/completed/catalog/20261025-140311-ts140-job1.toml")
        self.assertEqual(cmd[0], "rsync")
        self.assertIn("-s", cmd)
        self.assertEqual(cmd[cmd.index("-e") + 1], "ssh -o BatchMode=yes -i /root/.ssh/arm-shipper")
        self.assertNotIn("--remove-source-files", cmd)
        self.assertNotIn("--chmod=D755,F644", cmd)
        self.assertFalse(any(part.startswith("--bwlimit") for part in cmd))
        self.assertEqual(cmd[-1], "root@t620:/tank/rips/catalog/")
        local = dict(cfg, remote_host="")
        self.assertEqual(shipper.build_record_rsync_command(local, "/spool/r.toml")[-1], "/tank/rips/catalog/")

    def test_toml_strings_are_escaped(self):
        self.assertEqual(shipper._toml_string('He said "hi" \\ there'), '"He said \\"hi\\" \\\\ there"')
        self.assertEqual(shipper._toml_string("tab\there"), '"tab\\u0009here"')


if __name__ == "__main__":
    unittest.main()
