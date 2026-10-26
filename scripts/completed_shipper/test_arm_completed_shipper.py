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
import random
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import arm_completed_shipper as shipper  # noqa: E402
import mkvheader  # noqa: E402

FAKE_RSYNC = """#!/bin/sh
# Test double: log the call, optionally fail, otherwise run the real rsync.
# SHIPPER_TEST_INTERRUPT_FOR / _BEFORE: for a matching transfer (never a --dry-run probe), transfer (and
# remove) everything except the files matching the _BEFORE exclude pattern, then exit as if killed.
printf '%s\\n' "$*" >> "$SHIPPER_TEST_LOG"
if [ -n "$SHIPPER_TEST_FAIL_FOR" ]; then
    case "$*" in
        *"$SHIPPER_TEST_FAIL_FOR"*) echo "fake rsync: forced failure" >&2; exit 23 ;;
    esac
fi
if [ -n "$SHIPPER_TEST_INTERRUPT_FOR" ]; then
    case "$*" in
        *--dry-run*) ;;
        *"$SHIPPER_TEST_INTERRUPT_FOR"*)
            "$SHIPPER_TEST_REAL_RSYNC" --exclude="$SHIPPER_TEST_INTERRUPT_BEFORE" "$@"
            echo "fake rsync: interrupted" >&2; exit 20 ;;
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
        for name in ("SHIPPER_TEST_FAIL_FOR", "SHIPPER_TEST_INTERRUPT_FOR", "SHIPPER_TEST_INTERRUPT_BEFORE"):
            os.environ.pop(name, None)

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

    def _transfers(self):
        """The rsync calls that move files: every call but the collision probes (dry runs)."""
        return [c for c in self._calls() if "--dry-run" not in c]

    def _local(self, sub, name):
        return os.path.join(self.completed, sub, name)

    def _shipped(self, dest_sub, name):
        return os.path.join(self.dest, dest_sub, name)

    def _run(self, *extra):
        return shipper.main(["-c", self.conf, *extra])

    def _job_id(self, name):
        conn = sqlite3.connect(self.db)
        job_id = conn.execute("SELECT job_id FROM job WHERE path LIKE ?", (f"%{name}%",)).fetchone()[0]
        conn.close()
        return job_id

    def _interrupt(self, folder, before):
        """Make the next transfer of ``folder`` deliver everything but ``before``, then die."""
        os.environ["SHIPPER_TEST_INTERRUPT_FOR"] = folder
        os.environ["SHIPPER_TEST_INTERRUPT_BEFORE"] = before

    def _no_interrupt(self):
        for name in ("SHIPPER_TEST_INTERRUPT_FOR", "SHIPPER_TEST_INTERRUPT_BEFORE"):
            os.environ.pop(name, None)

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

        # The movies/ and tv/ root folders themselves survive.  With records off the spool held only
        # each folder's destination name while it moved, and nothing is left in it.
        for sub in ("movies", "tv", "unidentified"):
            self.assertTrue(os.path.isdir(os.path.join(self.completed, sub)))
        self.assertEqual(os.listdir(self.spool), [])

        calls = self._calls()
        transfers = self._transfers()
        self.assertEqual(len(transfers), 4)
        kinds = ["/completed/movies/" in c and "movies" or "/completed/tv/" in c and "tv" or "unidentified"
                 for c in transfers]
        self.assertEqual(kinds, ["movies", "tv", "unidentified", "unidentified"])
        for call in transfers[:2]:
            self.assertNotIn("--bwlimit", call)
            self.assertIn("--remove-source-files", call)
            self.assertIn("--partial-dir=.rsync-partial", call)
        for call in transfers[2:]:
            self.assertIn("--bwlimit=500", call)
        # Every transfer was preceded by its two collision probes: itemized dry runs, one of them
        # with --ignore-existing, that remove nothing and are not throttled.
        probes = [c for c in calls if "--dry-run" in c]
        self.assertEqual(len(probes), 8)
        self.assertEqual([c for c in calls if "--dry-run" not in c], transfers)
        self.assertEqual(calls.index(transfers[0]), 2)
        self.assertEqual(sum("--ignore-existing" in c for c in probes), 4)
        for probe in probes:
            self.assertIn("--itemize-changes", probe)
            for flag in ("--remove-source-files", "--partial-dir", "--bwlimit"):
                self.assertNotIn(flag, probe)

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
        # Pretend an earlier run copied one episode and was killed before removing the source.  rsync -a
        # leaves the delivered file with its source's size and time (copy2 does the same), which is how
        # the next run tells a resume from a file it would replace.
        partial = self._shipped("TV", "Show A (2010)")
        os.makedirs(partial)
        shutil.copy2(os.path.join(self._local("tv", "Show A (2010)"), "S01E01.mkv"), partial)
        self.assertEqual(self._run(), 0)
        self.assertTrue(os.path.isfile(os.path.join(partial, "S01E01.mkv")))
        self.assertTrue(os.path.isfile(os.path.join(partial, "S01E02.mkv")))
        self.assertFalse(os.path.exists(self._local("tv", "Show A (2010)")))
        self.assertEqual(os.listdir(os.path.join(self.dest, "TV")), ["Show A (2010)"])

    def test_dry_run_moves_nothing(self):
        rc = self._run("--dry-run")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(self._local("movies", "Movie A (2001)")))
        self.assertFalse(os.path.exists(self._shipped("Movies", "Movie A (2001)")))
        calls = self._calls()
        self.assertEqual(len(calls), 12)
        self.assertTrue(all("--dry-run" in c for c in calls))
        self.assertEqual(sum("--remove-source-files" in c for c in calls), 4)

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
        self.assertEqual(len(self._transfers()), 4)

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

    def test_probe_command_shape(self):
        cfg = dict(shipper.DEFAULTS, remote_host="root@t620", remote_movies_path="/tank/media/movies",
                   remote_tv_path="/t", remote_unidentified_path="/u", bwlimit_recognized="4000",
                   ssh_options="-o BatchMode=yes", rsync_extra_args="--chmod=D755,F644")
        cmd = shipper.build_probe_rsync_command(cfg, "movies", "/c/movies/Film (1999)", "Film (1999) - FILM_BD",
                                                ignore_existing=True)
        # --checksum: a different file with the source's size and time is a collision, not a resume.
        self.assertEqual(cmd[:6], ["rsync", "-a", "--dry-run", "--itemize-changes", "--checksum", "--timeout=300"])
        self.assertIn("--ignore-existing", cmd)
        self.assertIn("-s", cmd)
        self.assertEqual(cmd[cmd.index("-e") + 1], "ssh -o BatchMode=yes")
        # The transfer's extra arguments apply (an --exclude there must hold here too); nothing moves.
        self.assertIn("--chmod=D755,F644", cmd)
        for flag in ("--remove-source-files", "--partial-dir=.rsync-partial", "--bwlimit=4000"):
            self.assertNotIn(flag, cmd)
        self.assertEqual(cmd[-2:], ["/c/movies/Film (1999)/", "root@t620:/tank/media/movies/Film (1999) - FILM_BD/"])
        plain = shipper.build_probe_rsync_command(cfg, "movies", "/c/movies/Film (1999)", "Film (1999)")
        self.assertNotIn("--ignore-existing", plain)
        self.assertEqual(plain[-1], "root@t620:/tank/media/movies/Film (1999)/")
        self.assertEqual(shipper.destination(cfg, "movies", "/c/movies/Film (1999)"),
                         "root@t620:/tank/media/movies/Film (1999)/")
        # The transfer keeps rsync's quick check.
        self.assertNotIn("--checksum", shipper.build_rsync_command(cfg, "movies", "/c/movies/Film (1999)"))

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


# ---------------------------------------------------------------------- collision probes

class CollisionProbe(unittest.TestCase):
    def test_transferred_files_reads_both_rsync_dialects(self):
        output = "\n".join([
            "cd+++++++++ ./",
            ">f+++++++++ Movie A (2001).mkv",            # rsync 3.x, local destination, new file
            "cd+++++++++ extras/",
            ">f.st...... extras/trailer.mkv",            # existing file that differs
            "<f......... Movie B (2002).mkv",            # rsync 3.x over ssh: dry run into a folder not there yet
            ">f....... Movie C (2003).mkv",              # openrsync, same case
            ">f+++++++ extras/t 2.mkv",                  # openrsync, new file in an existing folder
            ".f...p..... perms only.mkv",                # attributes only: no content sent
            "cL+++++++++ link.mkv -> target",
            "created directory x",
            "",
        ])
        self.assertEqual(shipper.transferred_files(output),
                         ["Movie A (2001).mkv", "extras/trailer.mkv", "Movie B (2002).mkv", "Movie C (2003).mkv",
                          "extras/t 2.mkv"])
        self.assertEqual(shipper.transferred_files(""), [])

    def test_remote_folder_names_fall_back_from_label_to_job_id(self):
        job = {"job_id": 7, "label": " MOVIE/A:BD\t"}
        self.assertEqual(shipper.remote_folder_names("/c/movies/Movie A (2001)/", job),
                         ["Movie A (2001)", "Movie A (2001) - MOVIE A:BD", "Movie A (2001) - job7"])
        self.assertEqual(shipper.remote_folder_names("/c/movies/X", {"job_id": 3, "label": None}), ["X", "X - job3"])
        self.assertEqual(shipper.remote_folder_names("/c/movies/X", {"job_id": 3, "label": "  "}), ["X", "X - job3"])
        self.assertEqual(shipper.remote_folder_names("/c/movies/X", None), ["X"])
        long_label = "L" * 100
        names = shipper.remote_folder_names("/c/movies/X", {"job_id": 1, "label": long_label})
        self.assertEqual(names[1], "X - " + "L" * shipper.SUFFIX_MAX_LENGTH)

    def test_unidentified_and_label_named_folders_skip_the_label(self):
        # ARM names an unidentified folder after the disc label: "DVD_VIDEO - DVD_VIDEO" would say nothing.
        job = {"job_id": 5, "label": "DVD_VIDEO"}
        self.assertEqual(shipper.remote_folder_names("/c/unidentified/DVD_VIDEO", job, "unidentified"),
                         ["DVD_VIDEO", "DVD_VIDEO - job5"])
        self.assertEqual(shipper.remote_folder_names("/c/unidentified/DVD_VIDEO_1", job, "unidentified"),
                         ["DVD_VIDEO_1", "DVD_VIDEO_1 - job5"])
        self.assertEqual(shipper.remote_folder_names("/c/movies/DVD_VIDEO", job, "movies"),
                         ["DVD_VIDEO", "DVD_VIDEO - job5"])
        self.assertEqual(shipper.remote_folder_names("/c/movies/X", job, "movies"),
                         ["X", "X - DVD_VIDEO", "X - job5"])


class MediaBoxFixture(ShipperFixture):
    """Catalog records on, Movie A's job filled in, and helpers for what the media box already holds."""

    def setUp(self):
        super().setUp()
        os.makedirs(self.records)
        with open(self.conf, "a") as handle:
            handle.write(f"remote_catalog_path = {self.records}\nripper_name = ts140\n")
        self._set_job("Movie A (2001)", title="Movie A", year="2001", label="MOVIE_A_BD", disctype="bluray",
                      stop_time="2026-10-25 14:03:11")

    def _plant(self, dest_sub, folder, name, content="an earlier rip\n"):
        """A file on the media box as an earlier rip left it, old and different, so rsync would replace it."""
        path = os.path.join(self.dest, dest_sub, folder, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(content)
        os.utime(path, (1500000000, 1500000000))
        return path

    def _read(self, path):
        with open(path) as handle:
            return handle.read()

    def _dest_folders(self, dest_sub):
        return sorted(os.listdir(os.path.join(self.dest, dest_sub)))


class NoOverwrite(MediaBoxFixture):
    """The media box already holds a file the transfer would replace: it is kept and the rip lands beside it."""

    def test_existing_feature_is_kept_and_the_rip_lands_beside_it(self):
        kept = self._plant("Movies", "Movie A (2001)", "Movie A (2001).mkv")
        self.assertEqual(self._run(), 0)
        # The feature Plex serves is untouched and nothing else landed in its folder.
        self.assertEqual(self._read(kept), "an earlier rip\n")
        self.assertEqual(os.listdir(self._shipped("Movies", "Movie A (2001)")), ["Movie A (2001).mkv"])
        # The new rip was neither dropped nor deleted: it is complete under the disc label.
        beside = self._shipped("Movies", "Movie A (2001) - MOVIE_A_BD")
        self.assertEqual(self._read(os.path.join(beside, "Movie A (2001).mkv")), "content of Movie A (2001).mkv\n")
        self.assertEqual(self._read(os.path.join(beside, "extras", "trailer.mkv")), "content of extras/trailer.mkv\n")
        self.assertFalse(os.path.exists(self._local("movies", "Movie A (2001)")))
        self.assertEqual(self._dest_folders("Movies"), ["Movie A (2001)", "Movie A (2001) - MOVIE_A_BD"])
        # The catalog record names the folder that received the files; the main feature is still recognized.
        movie = next(n for n in os.listdir(self.records) if "-job1" in n)
        top, files = parse_record(self._read(os.path.join(self.records, movie)))
        self.assertEqual(top["library"], "movies/Movie A (2001) - MOVIE_A_BD")
        self.assertEqual(top["destination"], os.path.join(self.dest, "Movies", "Movie A (2001) - MOVIE_A_BD"))
        self.assertEqual([f["path"] for f in files],
                         ["movies/Movie A (2001) - MOVIE_A_BD/Movie A (2001).mkv",
                          "movies/Movie A (2001) - MOVIE_A_BD/extras/trailer.mkv"])
        self.assertEqual([f["main"] for f in files], [True, False])
        self.assertEqual(top["role"], "feature")
        # Two probes against ARM's name, two against the label name, then the one transfer, to the label name.
        movie_calls = [c for c in self._calls() if "/completed/movies/Movie A (2001)/" in c]
        self.assertEqual([("--dry-run" in c, c.endswith("/Movie A (2001) - MOVIE_A_BD/")) for c in movie_calls],
                         [(True, False), (True, False), (True, True), (True, True), (False, True)])
        # Other folders shipped as usual.
        self.assertTrue(os.path.isfile(os.path.join(self._shipped("TV", "Show A (2010)"), "S01E02.mkv")))
        self.assertFalse(os.path.exists(self._local("unidentified", "UNKNOWN_DISC")))

    def test_label_name_taken_too_falls_back_to_the_job_id(self):
        self._plant("Movies", "Movie A (2001)", "Movie A (2001).mkv")
        self._plant("Movies", "Movie A (2001) - MOVIE_A_BD", "Movie A (2001).mkv")
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._dest_folders("Movies"),
                         ["Movie A (2001)", "Movie A (2001) - MOVIE_A_BD", "Movie A (2001) - job1"])
        self.assertTrue(os.path.isfile(os.path.join(self._shipped("Movies", "Movie A (2001) - job1"), "extras",
                                                    "trailer.mkv")))
        self.assertFalse(os.path.exists(self._local("movies", "Movie A (2001)")))
        for folder in ("Movie A (2001)", "Movie A (2001) - MOVIE_A_BD"):
            self.assertEqual(self._read(os.path.join(self._shipped("Movies", folder), "Movie A (2001).mkv")),
                             "an earlier rip\n")

    def test_every_name_taken_leaves_the_folder_and_fails_the_run(self):
        taken = ("Movie A (2001)", "Movie A (2001) - MOVIE_A_BD", "Movie A (2001) - job1")
        for folder in taken:
            self._plant("Movies", folder, "Movie A (2001).mkv")
        self.assertEqual(self._run(), 1)
        # Nothing on the media box changed, the rip is still on the ripper, no transfer ran for it.
        self.assertEqual(self._dest_folders("Movies"), sorted(taken))
        for folder in taken:
            self.assertEqual(os.listdir(self._shipped("Movies", folder)), ["Movie A (2001).mkv"])
            self.assertEqual(self._read(os.path.join(self._shipped("Movies", folder), "Movie A (2001).mkv")),
                             "an earlier rip\n")
        self.assertEqual(self._read(os.path.join(self._local("movies", "Movie A (2001)"), "Movie A (2001).mkv")),
                         "content of Movie A (2001).mkv\n")
        self.assertTrue(os.path.isfile(os.path.join(self._local("movies", "Movie A (2001)"), "extras", "trailer.mkv")))
        self.assertFalse(any("Movie A (2001)" in c for c in self._transfers()))
        self.assertFalse(any("-job1" in n for n in os.listdir(self.records)))
        self.assertFalse(os.path.isdir(self.spool) and os.listdir(self.spool))
        # A recognized folder failed, so tv still shipped but no unidentified transfer started.
        self.assertTrue(os.path.isfile(os.path.join(self._shipped("TV", "Show A (2010)"), "S01E01.mkv")))
        self.assertTrue(os.path.isdir(self._local("unidentified", "UNKNOWN_DISC")))
        self.assertFalse(any("unidentified" in c for c in self._calls()))

    def test_another_disc_of_a_show_joins_its_folder(self):
        # Different file names are not a collision: a second disc's episodes join the season folder.
        kept = self._plant("TV", "Show A (2010)", "S01E03.mkv")
        self.assertEqual(self._run(), 0)
        show = self._shipped("TV", "Show A (2010)")
        self.assertEqual(sorted(os.listdir(show)), ["S01E01.mkv", "S01E02.mkv", "S01E03.mkv"])
        self.assertEqual(self._read(kept), "an earlier rip\n")
        self.assertEqual(self._dest_folders("TV"), ["Show A (2010)"])
        self.assertFalse(os.path.exists(self._local("tv", "Show A (2010)")))

    def test_unidentified_folder_of_a_repeated_label_lands_beside_the_first(self):
        # Unidentified folders are named after the disc label, which many discs share.
        # ARM names it after the label, so the label suffix is skipped and the job id used straight away.
        kept = self._plant("staging", "UNKNOWN_DISC", "title_t00.mkv")
        self._set_job("UNKNOWN_DISC", label="UNKNOWN_DISC")
        job_id = self._job_id("UNKNOWN_DISC")
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._read(kept), "an earlier rip\n")
        beside = self._shipped("staging", f"UNKNOWN_DISC - job{job_id}")
        self.assertEqual(self._read(os.path.join(beside, "title_t00.mkv")), "content of title_t00.mkv\n")
        self.assertFalse(os.path.exists(self._local("unidentified", "UNKNOWN_DISC")))
        self.assertEqual(self._dest_folders("staging"), ["UNKNOWN_2", "UNKNOWN_DISC", f"UNKNOWN_DISC - job{job_id}"])
        self.assertFalse(any("UNKNOWN_DISC - UNKNOWN_DISC" in c for c in self._calls()))

    def test_dry_run_names_the_free_folder_and_moves_nothing(self):
        self._plant("Movies", "Movie A (2001)", "Movie A (2001).mkv")
        self.assertEqual(self._run("--dry-run"), 0)
        self.assertTrue(os.path.isfile(os.path.join(self._local("movies", "Movie A (2001)"), "Movie A (2001).mkv")))
        self.assertEqual(self._dest_folders("Movies"), ["Movie A (2001)"])
        transfer = next(c for c in self._calls() if "--remove-source-files" in c and "Movie A (2001)" in c)
        self.assertTrue(transfer.endswith("/Movie A (2001) - MOVIE_A_BD/"), transfer)
        self.assertFalse(os.path.exists(self.spool))

    def test_same_size_and_time_but_different_content_is_a_collision(self):
        # rsync's quick check (size and time) would skip this file and --remove-source-files would then
        # delete the only copy of the new rip; the probes compare checksums instead.
        local = os.path.join(self._local("movies", "Movie A (2001)"), "Movie A (2001).mkv")
        impostor = "CONTENT OF MOVIE A (2001).MKV\n"
        self.assertEqual(len(impostor), os.path.getsize(local))
        kept = self._plant("Movies", "Movie A (2001)", "Movie A (2001).mkv", impostor)
        stat_local = os.stat(local)
        os.utime(kept, (stat_local.st_atime, stat_local.st_mtime))
        cfg = shipper.load_config(self.conf)
        self.assertEqual(shipper.colliding_files(cfg, "movies", self._local("movies", "Movie A (2001)"),
                                                 "Movie A (2001)"), ["Movie A (2001).mkv"])
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._read(kept), impostor)
        beside = self._shipped("Movies", "Movie A (2001) - MOVIE_A_BD")
        self.assertEqual(self._read(os.path.join(beside, "Movie A (2001).mkv")), "content of Movie A (2001).mkv\n")
        self.assertFalse(os.path.exists(self._local("movies", "Movie A (2001)")))

    # ------------------------------------------------------------------ interrupted after stepping aside

    def test_interrupted_collision_resumes_into_the_same_folder(self):
        # Run 1 steps aside to the label name, delivers (and removes) the feature, then dies before extras/.
        kept = self._plant("Movies", "Movie A (2001)", "Movie A (2001).mkv")
        self._interrupt("Movie A (2001)", "extras")
        self.assertEqual(self._run(), 1)
        beside = self._shipped("Movies", "Movie A (2001) - MOVIE_A_BD")
        local = self._local("movies", "Movie A (2001)")
        self.assertTrue(os.path.isfile(os.path.join(beside, "Movie A (2001).mkv")))
        self.assertFalse(os.path.exists(os.path.join(local, "Movie A (2001).mkv")))
        self.assertTrue(os.path.isfile(os.path.join(local, "extras", "trailer.mkv")))
        # The name was kept before the transfer started, beside the pending record built with it.
        state = os.path.join(self.spool, "job1.destination")
        self.assertEqual(shipper.read_record_key(state, "remote_name"), "Movie A (2001) - MOVIE_A_BD")
        self.assertEqual(shipper.read_record_key(state, "source"), "movies/Movie A (2001)")
        pending = os.path.join(self.spool, "20261025-140311-ts140-job1.pending")
        self.assertEqual(shipper.read_record_key(pending, "library"), "movies/Movie A (2001) - MOVIE_A_BD")

        # Run 2 sees only extras/ left.  Probing ARM's name would find no collision and split the rip;
        # it resumes into the kept name instead and never probes ARM's name.
        self._no_interrupt()
        os.remove(self.log)
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._read(os.path.join(beside, "extras", "trailer.mkv")), "content of extras/trailer.mkv\n")
        self.assertEqual(self._read(os.path.join(beside, "Movie A (2001).mkv")), "content of Movie A (2001).mkv\n")
        self.assertEqual(os.listdir(self._shipped("Movies", "Movie A (2001)")), ["Movie A (2001).mkv"])
        self.assertEqual(self._read(kept), "an earlier rip\n")
        self.assertFalse(os.path.exists(local))
        movie_calls = [c for c in self._calls() if "/completed/movies/Movie A (2001)/" in c]
        self.assertTrue(movie_calls)
        self.assertTrue(all(c.endswith("/Movie A (2001) - MOVIE_A_BD/") for c in movie_calls), movie_calls)
        # One record, built in run 1 while the feature was still local, naming the folder that holds both.
        movie = [n for n in os.listdir(self.records) if "-job1" in n]
        self.assertEqual(movie, ["20261025-140311-ts140-job1.toml"])
        top, files = parse_record(self._read(os.path.join(self.records, movie[0])))
        self.assertEqual(top["library"], "movies/Movie A (2001) - MOVIE_A_BD")
        self.assertEqual(top["destination"], beside)
        self.assertEqual(top["source"], "movies/Movie A (2001)")
        self.assertEqual(top["role"], "feature")
        self.assertEqual([f["path"] for f in files],
                         ["movies/Movie A (2001) - MOVIE_A_BD/Movie A (2001).mkv",
                          "movies/Movie A (2001) - MOVIE_A_BD/extras/trailer.mkv"])
        self.assertEqual([f["main"] for f in files], [True, False])
        self.assertFalse(os.listdir(self.spool))

    def test_interrupted_unidentified_collision_resumes_into_the_same_folder(self):
        self._job("unidentified", "DISC_X", "success", files=["title_t00.mkv", "zz/title_t01.mkv"])
        self._set_job("DISC_X", label="DISC_X")
        job_id = self._job_id("DISC_X")
        kept = self._plant("staging", "DISC_X", "title_t00.mkv")
        self._interrupt("DISC_X", "zz")
        self.assertEqual(self._run(), 1)
        beside = self._shipped("staging", f"DISC_X - job{job_id}")
        self.assertTrue(os.path.isfile(os.path.join(beside, "title_t00.mkv")))
        self.assertTrue(os.path.isfile(os.path.join(self.spool, f"job{job_id}.destination")))
        self._no_interrupt()
        self.assertEqual(self._run(), 0)
        self.assertEqual(sorted(os.listdir(beside)), ["title_t00.mkv", "zz"])
        self.assertEqual(self._read(os.path.join(beside, "zz", "title_t01.mkv")), "content of zz/title_t01.mkv\n")
        self.assertEqual(os.listdir(self._shipped("staging", "DISC_X")), ["title_t00.mkv"])
        self.assertEqual(self._read(kept), "an earlier rip\n")
        self.assertFalse(os.path.exists(self._local("unidentified", "DISC_X")))
        self.assertFalse(os.listdir(self.spool))

    def test_kept_name_that_now_collides_leaves_the_folder(self):
        # An earlier run chose the label name; meanwhile someone put a different feature there.  Stepping
        # aside again would split the rip, so the folder stays and the run fails.
        os.makedirs(self.spool)
        cfg = shipper.load_config(self.conf)
        self.assertTrue(shipper.save_destination_state(cfg, {"job_id": 1}, "movies/Movie A (2001)",
                                                       "Movie A (2001) - MOVIE_A_BD"))
        self._plant("Movies", "Movie A (2001) - MOVIE_A_BD", "Movie A (2001).mkv")
        with self.assertLogs("arm-completed-shipper", "ERROR") as logs:
            self.assertEqual(self._run(), 1)
        self.assertIn("job1.destination", "\n".join(logs.output))
        self.assertTrue(os.path.isfile(os.path.join(self._local("movies", "Movie A (2001)"), "Movie A (2001).mkv")))
        self.assertEqual(self._dest_folders("Movies"), ["Movie A (2001) - MOVIE_A_BD"])
        self.assertFalse(any("Movie A (2001)" in c for c in self._transfers()))
        self.assertTrue(os.path.isfile(os.path.join(self.spool, "job1.destination")))

    def test_state_file_wins_over_a_pending_record_that_disagrees(self):
        cfg = shipper.load_config(self.conf)
        job = shipper.shippable_folders(cfg)[os.path.realpath(self._local("movies", "Movie A (2001)"))]
        name, text = shipper.catalog_record(cfg, "movies", self._local("movies", "Movie A (2001)"), job)
        shipper.spool_record(cfg, name[:-len(".toml")] + ".pending", text)
        shipper.save_destination_state(cfg, job, "movies/Movie A (2001)", "Movie A (2001) - job1")
        with self.assertLogs("arm-completed-shipper", "WARNING") as logs:
            self.assertEqual(self._run(), 0)
        self.assertIn("following the former", "\n".join(logs.output))
        self.assertEqual(self._dest_folders("Movies"), ["Movie A (2001) - job1"])
        top, files = parse_record(self._read(os.path.join(self.records, "20261025-140311-ts140-job1.toml")))
        self.assertEqual(top["library"], "movies/Movie A (2001) - job1")
        self.assertEqual(top["destination"], self._shipped("Movies", "Movie A (2001) - job1"))
        self.assertEqual([f["path"] for f in files],
                         ["movies/Movie A (2001) - job1/Movie A (2001).mkv",
                          "movies/Movie A (2001) - job1/extras/trailer.mkv"])

    def test_stale_destination_names_are_dropped(self):
        os.makedirs(self.spool)
        cfg = shipper.load_config(self.conf)
        shipper.save_destination_state(cfg, {"job_id": 77}, "movies/Gone (1999)", "Gone (1999) - GONE")
        shipper.save_destination_state(cfg, {"job_id": 78}, "movies/Movie B (2002)", "Movie B (2002) - B")
        self.assertEqual(self._run("--dry-run"), 0)
        self.assertEqual(sorted(os.listdir(self.spool)), ["job77.destination", "job78.destination"])
        self.assertEqual(self._run(), 0)
        # Movie B is still ripping, so its kept name stays; Gone's folder is gone, so its name goes.
        self.assertEqual(os.listdir(self.spool), ["job78.destination"])

    def test_destination_names_are_kept_while_completed_path_is_not_mounted(self):
        # A missing movies/ directory means the completed volume is absent, not that the folders shipped.
        os.makedirs(self.spool)
        cfg = shipper.load_config(self.conf)
        shipper.save_destination_state(cfg, {"job_id": 77}, "movies/Gone (1999)", "Gone (1999) - GONE")
        with open(os.path.join(self.spool, "job78.destination"), "w") as handle:
            handle.write("# no source key at all\n")
        shutil.rmtree(os.path.join(self.completed, "movies"))
        with self.assertLogs("arm-completed-shipper", "WARNING") as logs:
            shipper.settle_destination_states(cfg)
        self.assertIn("not mounted", "\n".join(logs.output))
        self.assertIn("names no source folder", "\n".join(logs.output))
        self.assertEqual(sorted(os.listdir(self.spool)), ["job77.destination", "job78.destination"])

    def test_probe_timeout_allows_for_the_checksums(self):
        # rsync is silent while it checksums, so the probes get rsync_timeout (30 here) plus 1 s per 50 MiB.
        cfg = shipper.load_config(self.conf)
        folder = self._local("movies", "Movie A (2001)")
        with open(os.path.join(folder, "Movie A (2001).mkv"), "r+b") as handle:
            handle.truncate(120 * 1024 * 1024)  # sparse: 120 MiB counts as 3 s
        self.assertEqual(shipper.probe_timeout(cfg, folder), 33)
        self.assertIn("--timeout=33", shipper.build_probe_rsync_command(cfg, "movies", folder, "Movie A (2001)"))
        self.assertIn("--timeout=30", shipper.build_rsync_command(cfg, "movies", folder))
        self.assertEqual(shipper.probe_timeout(cfg, os.path.join(self.tmp, "no-such-folder")), 30)
        self.assertEqual(shipper.probe_timeout(dict(cfg, rsync_timeout="0"), folder), 0)


class BonusDisc(MediaBoxFixture):
    """A bonus disc's extras join the film's folder on the media box, never a folder beside it."""

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db)
        conn.execute("ALTER TABLE job ADD COLUMN bonus_of_job_id INTEGER")
        conn.commit()
        conn.close()

    def _bonus(self, folder, files, status="success", label="MOVIE_A_BONUS"):
        """
        A bonus disc's job (of Movie A, job 1) filed in ``folder`` (``{job_id}`` is its job id), with ``files``.
        Returns (job id, folder name).
        """
        conn = sqlite3.connect(self.db)
        cursor = conn.execute("INSERT INTO job (status, video_type, title, year, label, disctype, stop_time, "
                              "bonus_of_job_id) VALUES (?, 'movie', 'Movie A', '2001', ?, 'dvd', "
                              "'2026-10-26 18:00:00', 1)", (status, label))
        job_id = cursor.lastrowid
        folder = folder.format(job_id=job_id)
        conn.execute("UPDATE job SET path = ? WHERE job_id = ?", (self._local("movies", folder), job_id))
        conn.commit()
        conn.close()
        if files:
            self._folder("movies", folder, files)
        return job_id, folder

    def _film_shipped(self):
        """Movie A's folder went to the media box before the bonus disc finished."""
        self._plant("Movies", "Movie A (2001)", "Movie A (2001).mkv", content="content of Movie A (2001).mkv\n")
        self._plant("Movies", "Movie A (2001)", "extras/trailer.mkv", content="content of extras/trailer.mkv\n")
        shutil.rmtree(self._local("movies", "Movie A (2001)"))

    def test_extras_join_the_shipped_film_folder(self):
        self._film_shipped()
        extras = ["extras/MOVIE_A_BONUS_title_t00.mkv", "extras/MOVIE_A_BONUS_title_t01.mkv",
                  "disc-bonus-MOVIE_A_BONUS.jpg"]
        job_id, holding = self._bonus("Movie A (2001) - bonus job{job_id}", extras)
        self.assertEqual(self._run(), 0)
        film = self._shipped("Movies", "Movie A (2001)")
        for rel in extras:
            self.assertEqual(self._read(os.path.join(film, rel)), f"content of {rel}\n")
        # The film's own files are untouched, and nothing landed beside the film's folder.
        self.assertEqual(self._read(os.path.join(film, "Movie A (2001).mkv")), "content of Movie A (2001).mkv\n")
        self.assertEqual(self._read(os.path.join(film, "extras", "trailer.mkv")), "content of extras/trailer.mkv\n")
        self.assertEqual(self._dest_folders("Movies"), ["Movie A (2001)"])
        self.assertFalse(os.path.exists(self._local("movies", holding)))
        # The record is the bonus disc's own, naming the film's folder and the job it belongs to.
        name = next(n for n in os.listdir(self.records) if f"-job{job_id}." in n)
        top, files = parse_record(self._read(os.path.join(self.records, name)))
        self.assertEqual((top["job"], top["bonus_of"], top["role"]), (job_id, 1, "bonus"))
        self.assertEqual(top["library"], "movies/Movie A (2001)")
        self.assertEqual(top["source"], f"movies/{holding}")
        self.assertEqual([f["path"] for f in files],
                         ["movies/Movie A (2001)/extras/MOVIE_A_BONUS_title_t00.mkv",
                          "movies/Movie A (2001)/extras/MOVIE_A_BONUS_title_t01.mkv"])
        self.assertFalse(any(f["main"] for f in files))
        transfer = next(c for c in self._transfers() if holding in c)
        self.assertTrue(transfer.endswith("/Movies/Movie A (2001)/"), transfer)

    def test_extra_that_would_replace_a_file_leaves_the_folder(self):
        self._film_shipped()
        kept = self._plant("Movies", "Movie A (2001)", "extras/MOVIE_A_BONUS_title_t00.mkv")
        _, holding = self._bonus("Movie A (2001) - bonus job{job_id}", ["extras/MOVIE_A_BONUS_title_t00.mkv"])
        self.assertEqual(self._run(), 1)
        self.assertEqual(self._read(kept), "an earlier rip\n")
        self.assertEqual(self._dest_folders("Movies"), ["Movie A (2001)"])
        self.assertTrue(os.path.isfile(os.path.join(self._local("movies", holding), "extras",
                                                    "MOVIE_A_BONUS_title_t00.mkv")))
        self.assertEqual([c for c in self._transfers() if holding in c], [])

    def test_extras_filed_in_the_film_folder_ship_with_the_film(self):
        # The bonus disc finished while the film's folder was still on the ripper: one folder, the film's rip.
        self._folder("movies", "Movie A (2001)", ["extras/MOVIE_A_BONUS_title_t00.mkv"])
        job_id, _ = self._bonus("Movie A (2001)", None)
        self.assertEqual(self._run(), 0)
        film = self._shipped("Movies", "Movie A (2001)")
        self.assertTrue(os.path.isfile(os.path.join(film, "Movie A (2001).mkv")))
        self.assertTrue(os.path.isfile(os.path.join(film, "extras", "MOVIE_A_BONUS_title_t00.mkv")))
        # Its record is the film's job's, not the bonus disc's.
        self.assertEqual([n for n in os.listdir(self.records) if f"-job{job_id}." in n], [])
        name = next(n for n in os.listdir(self.records) if "-job1." in n)
        top, files = parse_record(self._read(os.path.join(self.records, name)))
        self.assertEqual((top["job"], top["role"]), (1, "feature"))
        self.assertNotIn("bonus_of", top)
        self.assertIn("movies/Movie A (2001)/extras/MOVIE_A_BONUS_title_t00.mkv", [f["path"] for f in files])

    def test_film_folder_waits_for_its_bonus_disc(self):
        self._bonus("Movie A (2001)", None, status="ripping")
        self.assertEqual(self._run(), 0)
        self.assertTrue(os.path.isdir(self._local("movies", "Movie A (2001)")))
        self.assertEqual(self._dest_folders("Movies"), [])

    def test_bonus_remote_name_needs_the_bonus_job_itself(self):
        folder = self._local("movies", "Movie A (2001) - bonus job11")
        self.assertEqual(shipper.bonus_remote_name(folder, {"job_id": 11, "bonus_of_job_id": 1}), "Movie A (2001)")
        self.assertEqual(shipper.remote_folder_names(folder, {"job_id": 11, "bonus_of_job_id": 1, "label": "X"}),
                         ["Movie A (2001)"])
        # Not a bonus disc, another job's folder, or not ARM's holding name: shipped under its own name.
        self.assertIsNone(shipper.bonus_remote_name(folder, {"job_id": 11, "bonus_of_job_id": None}))
        self.assertIsNone(shipper.bonus_remote_name(folder, {"job_id": 12, "bonus_of_job_id": 1}))
        self.assertIsNone(shipper.bonus_remote_name(self._local("movies", "Movie A (2001)"),
                                                    {"job_id": 11, "bonus_of_job_id": 1}))


class KeptNameWithoutRecords(ShipperFixture):
    """Records off: the destination name is still kept in the spool across an interrupted transfer."""

    def test_interrupted_collision_resumes_into_the_same_folder(self):
        self._set_job("Movie A (2001)", label="MOVIE_A_BD")
        planted = os.path.join(self.dest, "Movies", "Movie A (2001)", "Movie A (2001).mkv")
        os.makedirs(os.path.dirname(planted))
        with open(planted, "w") as handle:
            handle.write("an earlier rip\n")
        self._interrupt("Movie A (2001)", "extras")
        self.assertEqual(self._run(), 1)
        self.assertEqual(os.listdir(self.spool), ["job1.destination"])
        self._no_interrupt()
        self.assertEqual(self._run(), 0)
        beside = self._shipped("Movies", "Movie A (2001) - MOVIE_A_BD")
        self.assertTrue(os.path.isfile(os.path.join(beside, "Movie A (2001).mkv")))
        self.assertTrue(os.path.isfile(os.path.join(beside, "extras", "trailer.mkv")))
        self.assertEqual(os.listdir(self._shipped("Movies", "Movie A (2001)")), ["Movie A (2001).mkv"])
        self.assertFalse(os.path.exists(self._local("movies", "Movie A (2001)")))
        self.assertEqual(os.listdir(self.spool), [])


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


def seek_head(entries):
    """A SeekHead for (element id hex, position) pairs; 8-byte positions keep its size fixed."""
    return ebml("114D9B74", b"".join(
        ebml("4DBB", ebml("53AB", bytes.fromhex(element)) + ebml("53AC", position.to_bytes(8, "big")))
        for element, position in entries))


def make_mkv(path, width=3840, height=2160, seconds=5400.0, video="V_MPEGH/ISO/HEVC",
             audio=(("eng", "A_TRUEHD"), ("und", "A_AC3")), subs=2, chapters=3, title="Test Title",
             chapters_after_cluster=False, seek_position=None):
    """
    Write a Matroska file with just the header elements a catalog reads, then one Cluster.

    With ``chapters_after_cluster`` the Chapters element follows the Cluster and a SeekHead at the
    top of the Segment points at it (or at ``seek_position``, when given).
    """
    header = ebml("1A45DFA3", ebml("4286", 1) + ebml("4282", "matroska") + ebml("4287", 4) + ebml("4285", 2))
    info = ebml("1549A966", ebml("2AD7B1", 1000000) + ebml("4489", struct.pack(">d", seconds * 1000))
                + ebml("7BA9", title))
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
    if chapters_after_cluster:
        head_size = len(seek_head([("1043A770", 0)]))
        if seek_position is None:
            seek_position = head_size + len(info + tracks + cluster)
        segment = seek_head([("1043A770", seek_position)]) + info + tracks + cluster + chapter_element
    else:
        segment = info + tracks + chapter_element + cluster
    with open(path, "wb") as handle:
        handle.write(header + ebml("18538067", segment))
    return path


def corrupt_language_size(path, declared=1 << 50):
    """Rewrite the first audio Language element ("eng") of a make_mkv file to declare ``declared`` bytes."""
    with open(path, "rb") as handle:
        data = handle.read()
    good = bytes.fromhex("22B59C") + b"\x83eng"
    bad = bytes.fromhex("22B59C") + ((1 << 56) | declared).to_bytes(8, "big") + b"eng"
    assert data.count(good) == 1
    with open(path, "wb") as handle:
        handle.write(data.replace(good, bad))
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

    def test_seekhead_is_followed_to_elements_after_the_cluster(self):
        path = make_mkv(os.path.join(self.tmp, "late.mkv"), chapters=5, chapters_after_cluster=True)
        info = mkvheader.probe(path)
        self.assertEqual(info["chapters"], 5)
        self.assertEqual((info["width"], info["title"]), (3840, "Test Title"))

    def test_seekhead_past_the_end_of_a_truncated_file_is_skipped(self):
        path = make_mkv(os.path.join(self.tmp, "short.mkv"), chapters_after_cluster=True, seek_position=1 << 40)
        info = mkvheader.probe(path)
        self.assertEqual((info["chapters"], info["width"], info["title"]), (0, 3840, "Test Title"))

    def test_absurd_seekhead_position_gives_the_size_only_row(self):
        path = make_mkv(os.path.join(self.tmp, "seek.mkv"), chapters_after_cluster=True, seek_position=1 << 63)
        with self.assertRaises(mkvheader.MalformedHeader):
            mkvheader.probe(path)
        with self.assertLogs("mkvheader", "WARNING"):
            row = mkvheader.probe_row(path)
        self.assertFalse(row["probed"])
        self.assertEqual(row["size"], str(os.path.getsize(path)))
        self.assertEqual(row["resolution"], "")

    def test_corrupt_element_size_is_refused_not_read(self):
        path = corrupt_language_size(make_mkv(os.path.join(self.tmp, "lang.mkv")))
        with self.assertRaises(mkvheader.MalformedHeader):
            mkvheader.probe(path)
        with self.assertLogs("mkvheader", "WARNING") as logs:
            row = mkvheader.probe_row(path)
        self.assertFalse(row["probed"])
        self.assertEqual(row["size"], str(os.path.getsize(path)))
        self.assertIn("lang.mkv", logs.output[0])

    def test_truncation_at_every_offset_never_escapes(self):
        path = make_mkv(os.path.join(self.tmp, "full.mkv"), chapters_after_cluster=True)
        with open(path, "rb") as handle:
            data = handle.read()
        cut = os.path.join(self.tmp, "cut.mkv")
        for length in range(len(data)):
            with open(cut, "wb") as handle:
                handle.write(data[:length])
            try:
                mkvheader.probe(cut)
            except (mkvheader.NotMatroska, mkvheader.MalformedHeader):
                pass
            with self.assertLogs("mkvheader", "DEBUG"):
                mkvheader.log.debug("keep assertLogs happy when probe_row logs nothing")
                self.assertEqual(mkvheader.probe_row(cut)["size"], str(length))

    def test_random_byte_flips_only_raise_parser_errors(self):
        path = make_mkv(os.path.join(self.tmp, "fuzz.mkv"))
        with open(path, "rb") as handle:
            data = handle.read()
        fuzzed = os.path.join(self.tmp, "fuzzed.mkv")
        rng = random.Random(11)
        for _ in range(1500):
            mutated = bytearray(data)
            mutated[rng.randrange(160)] = rng.randrange(256)
            with open(fuzzed, "wb") as handle:
                handle.write(mutated)
            try:
                mkvheader.probe(fuzzed)
            except (mkvheader.NotMatroska, mkvheader.MalformedHeader):
                pass


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

    # ------------------------------------------------------------------ pending records

    def _cfg(self):
        return shipper.load_config(self.conf)

    def _record_files(self, name):
        with open(os.path.join(self.records, name)) as handle:
            return parse_record(handle.read())

    def test_interrupted_transfer_keeps_the_record_built_from_the_complete_folder(self):
        # Run 1 delivers (and removes) the feature, then dies before extras/.
        os.environ["SHIPPER_TEST_INTERRUPT_FOR"] = "Movie A (2001)"
        os.environ["SHIPPER_TEST_INTERRUPT_BEFORE"] = "extras"
        self.assertEqual(self._run(), 1)
        local = self._local("movies", "Movie A (2001)")
        self.assertFalse(os.path.exists(os.path.join(local, "Movie A (2001).mkv")))
        self.assertTrue(os.path.isfile(os.path.join(local, "extras", "trailer.mkv")))
        spooled = self._spooled()
        self.assertEqual(spooled, ["20261025-140311-ts140-job1.pending", "job1.destination"])
        self.assertFalse(any(n.endswith(".toml") and "-job1" in n for n in self._records()))

        # Run 2 finishes the folder and ships the record written while the feature was still local.
        for name in ("SHIPPER_TEST_INTERRUPT_FOR", "SHIPPER_TEST_INTERRUPT_BEFORE"):
            os.environ.pop(name)
        self.assertEqual(self._run(), 0)
        self.assertFalse(os.path.exists(local))
        self.assertEqual(self._spooled(), [])
        top, files = self._record_files("20261025-140311-ts140-job1.toml")
        self.assertEqual([f["path"] for f in files],
                         ["movies/Movie A (2001)/Movie A (2001).mkv", "movies/Movie A (2001)/extras/trailer.mkv"])
        self.assertEqual([f["main"] for f in files], [True, False])
        self.assertEqual((top["role"], top["format"]), ("feature", "uhd"))
        self.assertEqual(files[0]["resolution"], "3840x2160")

    def test_record_is_spooled_pending_before_rsync_starts(self):
        seen = []
        real_run = subprocess.run

        def spy(cmd, *args, **kwargs):
            if "--remove-source-files" in cmd and "Movie A" in cmd[-2]:
                seen.append(sorted(os.listdir(self.spool)))
            return real_run(cmd, *args, **kwargs)

        with mock.patch.object(shipper.subprocess, "run", side_effect=spy):
            self.assertEqual(self._run(), 0)
        # Written atomically: the pending record and the destination name, no .tmp beside them.
        self.assertEqual(seen, [["20261025-140311-ts140-job1.pending", "job1.destination"]])
        self.assertEqual(self._spooled(), [])

    def test_spool_writes_are_atomic(self):
        cfg = self._cfg()
        path = shipper.spool_record(cfg, "x-job9.pending", 'library = "movies/X"\n')
        self.assertEqual(self._spooled(), ["x-job9.pending"])
        with open(path) as handle:
            self.assertEqual(handle.read(), 'library = "movies/X"\n')
        # A write that fails part-way leaves neither a truncated record nor its .tmp.
        with mock.patch.object(shipper.os, "replace", side_effect=OSError("disk full")):
            with self.assertLogs("arm-completed-shipper", "ERROR"):
                self.assertIsNone(shipper.spool_record(cfg, "y-job10.pending", "text"))
        self.assertEqual(self._spooled(), ["x-job9.pending"])

    def test_pending_and_tmp_records_are_never_shipped(self):
        os.makedirs(self.spool)
        # Movie A's folder is still in completed_path: its pending record waits for ship_folder.
        for name in ("20261025-140311-ts140-job1.pending", "20261025-140311-ts140-job1.pending.tmp",
                     "20261025-140311-ts140-job1.toml.tmp"):
            with open(os.path.join(self.spool, name), "w") as handle:
                handle.write('library = "movies/Movie A (2001)"\n')
        cfg = self._cfg()
        self.assertEqual(shipper.pending_records(cfg), [])
        stats = {"shipped": 0, "failed": 0, "records": 0}
        self.assertTrue(shipper.ship_pending_records(cfg, False, stats))
        self.assertEqual(self._records(), [])
        self.assertEqual(len(self._spooled()), 3)
        self.assertEqual(self._calls(), [])

    def test_pending_record_of_a_vanished_folder_is_finished_and_shipped(self):
        os.makedirs(self.spool)
        with open(os.path.join(self.spool, "20200101-000000-ts140-job77.pending"), "w") as handle:
            handle.write('ripper = "ts140"\njob = 77\nshipped = "2020-01-01T00:00:00"\n'
                         'library = "movies/Gone (1999)"\n\n[[files]]\npath = "movies/Gone (1999)/Gone (1999).mkv"\n')
        cfg = self._cfg()
        stats = {"shipped": 0, "failed": 0, "records": 0}
        with self.assertLogs("arm-completed-shipper", "WARNING") as logs:
            self.assertTrue(shipper.ship_pending_records(cfg, False, stats))
        self.assertIn("movies/Gone (1999)", "\n".join(logs.output))
        self.assertEqual(self._records(), ["20200101-000000-ts140-job77.toml"])
        self.assertEqual(self._spooled(), [])
        top, _ = self._record_files("20200101-000000-ts140-job77.toml")
        self.assertNotEqual(top["shipped"], "2020-01-01T00:00:00")

    def test_pending_record_is_kept_while_completed_path_is_not_mounted(self):
        # A missing movies/ directory means the completed volume is absent, not that the folder shipped.
        os.makedirs(self.spool)
        with open(os.path.join(self.spool, "20200101-000000-ts140-job77.pending"), "w") as handle:
            handle.write('ripper = "ts140"\njob = 77\nshipped = "2020-01-01T00:00:00"\n'
                         'library = "movies/Gone (1999)"\n\n[[files]]\npath = "movies/Gone (1999)/Gone (1999).mkv"\n')
        shutil.rmtree(os.path.join(self.completed, "movies"))
        cfg = self._cfg()
        stats = {"shipped": 0, "failed": 0, "records": 0}
        with self.assertLogs("arm-completed-shipper", "WARNING") as logs:
            self.assertTrue(shipper.ship_pending_records(cfg, False, stats))
        self.assertIn("not mounted", "\n".join(logs.output))
        self.assertEqual(self._spooled(), ["20200101-000000-ts140-job77.pending"])
        self.assertEqual(self._records(), [])

    def test_pending_record_lookup_and_key_reader(self):
        cfg = self._cfg()
        job = {"job_id": 1, "stop_time": "2026-10-25 14:03:11"}
        self.assertIsNone(shipper.pending_record_path(cfg, job))
        shipper.spool_record(cfg, "20261025-140311-old-name-job1.pending",
                             'title = "Say \\"hi\\" \\\\ \\u0009"\njob = 1\nlibrary = "movies/Movie A (2001)"\n'
                             'destination = "/m/Movie A (2001)"\n\n[[files]]\npath = "x"\n')
        shipper.spool_record(cfg, "20261025-140311-ts140-job11.pending", 'library = "movies/Other"\n')
        # Found by job id alone, whatever the ripper or time parts of the name say.
        found = shipper.pending_record_path(cfg, job)
        self.assertEqual(os.path.basename(found), "20261025-140311-old-name-job1.pending")
        self.assertEqual(shipper.pending_record_path(cfg, job, "movies/Movie A (2001)"), found)
        self.assertIsNone(shipper.pending_record_path(cfg, job, "movies/Somewhere Else"))
        self.assertEqual(shipper.read_record_key(found, "destination"), "/m/Movie A (2001)")
        self.assertEqual(shipper.read_record_key(found, "title"), 'Say "hi" \\ \t')
        self.assertEqual(shipper.read_record_key(found, "job"), "1")
        self.assertIsNone(shipper.read_record_key(found, "path"))  # inside [[files]], not top level
        self.assertIsNone(shipper.read_record_key(found + ".absent", "library"))
        self.assertEqual(shipper.record_name(cfg, job, shipper.PENDING_SUFFIX), "20261025-140311-ts140-job1.pending")

    def test_unwritable_spool_logs_the_record_and_still_ships(self):
        blocker = os.path.join(self.tmp, "not-a-dir")
        with open(blocker, "w") as handle:
            handle.write("")
        with open(self.conf, "a") as handle:
            handle.write(f"catalog_spool_path = {blocker}/spool\n")
        with self.assertLogs("arm-completed-shipper", "ERROR") as logs:
            self.assertEqual(self._run(), 1)
        self.assertFalse(os.path.exists(self._local("movies", "Movie A (2001)")))
        self.assertEqual(self._records(), [])
        text = "\n".join(logs.output)
        self.assertIn('title = "Movie A"', text)
        self.assertIn('path = "movies/Movie A (2001)/Movie A (2001).mkv"', text)

    def test_malformed_header_still_ships_the_folder_and_writes_a_record(self):
        corrupt_language_size(os.path.join(self._local("movies", "Movie A (2001)"), "Movie A (2001).mkv"))
        with self.assertLogs("mkvheader", "WARNING"):
            self.assertEqual(self._run(), 0)
        self.assertFalse(os.path.exists(self._local("movies", "Movie A (2001)")))
        self.assertTrue(os.path.isfile(os.path.join(self._shipped("Movies", "Movie A (2001)"), "Movie A (2001).mkv")))
        top, files = self._record_files("20261025-140311-ts140-job1.toml")
        self.assertTrue(files[0]["main"])
        self.assertGreater(files[0]["size"], 0)
        self.assertNotIn("resolution", files[0])
        self.assertEqual((top["role"], top["format"]), ("feature", "bd"))

    def test_record_that_cannot_be_built_never_blocks_the_folder(self):
        with mock.patch.object(shipper, "catalog_record", side_effect=RuntimeError("boom")):
            with self.assertLogs("arm-completed-shipper", "ERROR"):
                self.assertEqual(self._run(), 1)
        self.assertFalse(os.path.exists(self._local("movies", "Movie A (2001)")))
        self.assertFalse(os.path.exists(self._local("tv", "Show A (2010)")))
        self.assertFalse(os.path.exists(self._local("unidentified", "UNKNOWN_DISC")))
        self.assertEqual(self._records(), [])


if __name__ == "__main__":
    unittest.main()
