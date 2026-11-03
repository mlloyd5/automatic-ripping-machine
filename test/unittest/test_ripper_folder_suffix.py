"""
Tests for the suffix that sets a job's folders and log apart from older ones of the same name
(arm.models.job.folder_suffix), used by utils.check_for_dupe_folder, arm_ripper.file_film and
bonus_target_folder, makemkv.setup_rawpath and logger.setup_job_log.

The suffix is the job's start stamp, kept in job.stage. The web UI used to overwrite job.stage with rip
progress text, and its "/" made a nested folder: ts140 job 169 (The West, disc 2) found
completed/tv/The West (1996) already there and filed into
completed/tv/The West (1996)_Track 1/2<br>Saving to MKV file/, which the completed-rip shipper never sent.
A stage like that must still give one plain folder name.

Importing ``arm.ripper.arm_ripper`` pulls in the whole of ARM (config, Flask app, database), so a
throwaway arm.yaml is written first and pointed at with ARM_CONFIG_FILE. Needs the Linux
dependencies ARM itself needs (libdiscid, pyudev); skipped when they are missing.
"""
import importlib
import logging
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]

_TMP = tempfile.mkdtemp(prefix="arm_folder_suffix_test_")
_CONFIG = os.path.join(_TMP, "arm.yaml")
with open(_CONFIG, "w") as _handle:
    _handle.write(f"INSTALLPATH: \"{REPO_ROOT}\"\n"
                  f"DBFILE: \"{_TMP}/arm.db\"\n"
                  f"LOGPATH: \"{_TMP}/logs/\"\n"
                  f"ABCDE_CONFIG_FILE: \"{REPO_ROOT}/setup/.abcde.conf\"\n"
                  f"RAW_PATH: \"{_TMP}/raw/\"\nTRANSCODE_PATH: \"{_TMP}/transcode/\"\n"
                  f"COMPLETED_PATH: \"{_TMP}/completed/\"\n"
                  f"DISC_PHOTO_PATH: \"{_TMP}/photos/\"\n"
                  "DISABLE_LOGIN: true\nSET_MEDIA_PERMISSIONS: false\n")
os.environ["ARM_CONFIG_FILE"] = _CONFIG

try:
    arm_ripper = importlib.import_module("arm.ripper.arm_ripper")
    utils = importlib.import_module("arm.ripper.utils")
    makemkv = importlib.import_module("arm.ripper.makemkv")
    arm_logger = importlib.import_module("arm.ripper.logger")
    cfg = importlib.import_module("arm.config.config")
    from arm.models.job import folder_suffix
    IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on the host
    arm_ripper = utils = makemkv = arm_logger = cfg = folder_suffix = None
    IMPORT_ERROR = error


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


PROGRESS = "Track 1/2<br>Saving to MKV file"
"""What the UI wrote into job 169's stage"""
STARTED = datetime(2026, 11, 1, 20, 15, 30)
STAMP = "177000169000"


def _stamp(when):
    return str(round(when.timestamp() * 100))


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestFolderSuffix(unittest.TestCase):
    def test_numeric_stage_is_the_suffix(self):
        self.assertEqual(folder_suffix(SimpleNamespace(stage=STAMP, start_time=STARTED)), STAMP)

    def test_progress_text_gives_the_start_time_stamp(self):
        self.assertEqual(folder_suffix(SimpleNamespace(stage=PROGRESS, start_time=STARTED)), _stamp(STARTED))

    def test_other_ui_text_gives_the_start_time_stamp(self):
        for stage in ("Unknown -  [Errno 2] No such file or directory: '/home/arm/logs/progress/169.log'",
                      "1 of 1 - 1/2", "Transcoding", "Track:   1 /12", "", None):
            with self.subTest(stage=stage):
                self.assertEqual(folder_suffix(SimpleNamespace(stage=stage, start_time=STARTED)), _stamp(STARTED))

    def test_no_stage_or_start_time_gives_the_clock(self):
        before = round(time.time() * 100)
        suffix = folder_suffix(SimpleNamespace(stage=PROGRESS, start_time=None))
        self.assertTrue(suffix.isdigit(), suffix)
        self.assertGreaterEqual(int(suffix), before)
        self.assertLessEqual(int(suffix), round(time.time() * 100))


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class SuffixFixture(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="suffix_", dir=_TMP)
        self.completed = os.path.join(self.root, "completed")
        self.raw = os.path.join(self.root, "raw")
        patch = mock.patch.dict(cfg.arm_config, {"ALLOW_DUPLICATES": True})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _job(self, stage=PROGRESS, **extra):
        values = dict(job_id=169, title="The West", year="1996", video_type="series", label="THE_WEST_D2",
                      disctype="dvd", stage=stage, start_time=STARTED,
                      config=SimpleNamespace(COMPLETED_PATH=self.completed, RAW_PATH=self.raw, DEST_EXT="mkv"))
        values.update(extra)
        return SimpleNamespace(**values)

    def assertSingleLevel(self, path, parent, expected_name):
        """``path`` is ``parent/expected_name``, one folder that exists, with no progress text in its name."""
        self.assertEqual(os.path.dirname(path), parent)
        self.assertEqual(os.path.basename(path), expected_name)
        self.assertNotIn("<br>", path)
        self.assertNotIn("Track", os.path.basename(path))
        self.assertTrue(os.path.isdir(path), path)


class TestCheckForDupeFolder(SuffixFixture):
    """utils.check_for_dupe_folder, which named job 169's folder."""

    def setUp(self):
        super().setUp()
        self.tv = os.path.join(self.completed, "tv")
        self.existing = os.path.join(self.tv, "The West (1996)")
        os.makedirs(self.existing)

    def test_the_west_disc_2(self):
        new = utils.check_for_dupe_folder(False, self.existing, self._job())
        self.assertSingleLevel(new, self.tv, f"The West (1996)_{_stamp(STARTED)}")
        self.assertEqual(sorted(os.listdir(self.tv)), ["The West (1996)", f"The West (1996)_{_stamp(STARTED)}"])

    def test_numeric_stage_is_kept(self):
        new = utils.check_for_dupe_folder(False, self.existing, self._job(stage=STAMP))
        self.assertSingleLevel(new, self.tv, f"The West (1996)_{STAMP}")

    def test_free_folder_has_no_suffix(self):
        free = os.path.join(self.tv, "The West (1997)")
        self.assertEqual(utils.check_for_dupe_folder(False, free, self._job()), free)
        self.assertTrue(os.path.isdir(free))


class TestFileFilm(SuffixFixture):
    """arm_ripper.file_film and bonus_target_folder: a later film of a double feature beside an older folder."""

    def setUp(self):
        super().setUp()
        self.movies = os.path.join(self.completed, "movies")
        os.makedirs(os.path.join(self.movies, "IX (2002)"))
        self.out_path = os.path.join(self.root, "transcode")
        os.makedirs(self.out_path)
        Path(self.out_path, "B1_t01.mkv").write_bytes(b"film 2")
        self.film = SimpleNamespace(film_id=2, job_id=169, position=2, folder_name="IX (2002)")

    def _parent(self):
        return self._job(title="Land Before Time VIII", year="2001", video_type="movie")

    def test_film_beside_an_older_folder(self):
        film_path = arm_ripper.file_film(self.out_path, "B1_t01.mkv", self.film, self._parent())
        self.assertSingleLevel(film_path, self.movies, f"IX (2002)_{_stamp(STARTED)}")
        self.assertEqual(sorted(os.listdir(self.movies)), ["IX (2002)", f"IX (2002)_{_stamp(STARTED)}"])
        self.assertTrue(os.path.isfile(os.path.join(film_path, "IX (2002).mkv")))

    def test_bonus_disc_finds_the_film_folder_again(self):
        parent = self._parent()
        film_path = arm_ripper.file_film(self.out_path, "B1_t01.mkv", self.film, parent)
        bonus = self._job(job_id=170, bonus_of_job_id=169, bonus_of=parent, bonus_of_film_id=2,
                          bonus_of_film=self.film)
        self.assertEqual(arm_ripper.bonus_target_folder(bonus), film_path)

    def test_bonus_disc_without_a_suffixed_folder_uses_the_plain_one(self):
        bonus = self._job(job_id=170, bonus_of_job_id=169, bonus_of=self._parent(), bonus_of_film_id=2,
                          bonus_of_film=self.film)
        self.assertEqual(arm_ripper.bonus_target_folder(bonus), os.path.join(self.movies, "IX (2002)"))


class TestSetupRawpath(SuffixFixture):
    """makemkv.setup_rawpath: a raw folder of the same title left from an earlier rip."""

    def test_existing_raw_folder(self):
        existing = os.path.join(self.raw, "The West")
        os.makedirs(existing)
        raw_path = makemkv.setup_rawpath(self._job(), existing)
        self.assertSingleLevel(raw_path, self.raw, f"The West_{_stamp(STARTED)}")

    def test_new_raw_folder(self):
        raw_path = makemkv.setup_rawpath(self._job(), os.path.join(self.raw, "The West"))
        self.assertEqual(raw_path, os.path.join(self.raw, "The West"))
        self.assertTrue(os.path.isdir(raw_path))


class TestSetupJobLog(SuffixFixture):
    """logger.setup_job_log: a log of the same label left from an earlier rip."""

    def setUp(self):
        super().setUp()
        self.logpath = os.path.join(self.root, "logs")
        os.makedirs(self.logpath)
        Path(self.logpath, "THE_WEST_D2.log").write_text("disc 2, first try\n")
        root_logger = logging.getLogger()
        handlers = root_logger.handlers[:]
        self.addCleanup(setattr, root_logger, "handlers", handlers)
        for patch in (mock.patch.dict(cfg.arm_config, {"LOGPATH": self.logpath}),
                      mock.patch.object(arm_logger, "_create_file_handler", return_value=logging.NullHandler())):
            patch.start()
            self.addCleanup(patch.stop)

    def test_existing_log(self):
        job = self._job()
        log_full = arm_logger.setup_job_log(job)
        self.assertEqual(job.logfile, f"THE_WEST_D2_{_stamp(STARTED)}.log")
        self.assertEqual(log_full, os.path.join(self.logpath, job.logfile))

    def test_numeric_stage_is_kept(self):
        job = self._job(stage=STAMP)
        arm_logger.setup_job_log(job)
        self.assertEqual(job.logfile, f"THE_WEST_D2_{STAMP}.log")


if __name__ == "__main__":
    unittest.main()
