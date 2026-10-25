"""
Tests for arm_ripper.pick_main_feature / skip_transcode_movie: with SKIP_TRANSCODE the movie's main
feature is the ripped title with the most chapters (then the largest file, then the lowest title
number), falling back to the largest file when no track carries chapter information.

Importing ``arm.ripper.arm_ripper`` pulls in the whole of ARM (config, Flask app, database), so a
throwaway arm.yaml is written first and pointed at with ARM_CONFIG_FILE. Needs the Linux
dependencies ARM itself needs (libdiscid, pyudev); skipped when they are missing.
"""
import importlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]

_TMP = tempfile.mkdtemp(prefix="arm_mainfeature_test_")
_CONFIG = os.path.join(_TMP, "arm.yaml")
with open(_CONFIG, "w") as _handle:
    _handle.write(f"INSTALLPATH: \"{REPO_ROOT}\"\n"
                  f"DBFILE: \"{_TMP}/arm.db\"\n"
                  f"LOGPATH: \"{_TMP}/logs/\"\n"
                  f"ABCDE_CONFIG_FILE: \"{REPO_ROOT}/setup/.abcde.conf\"\n"
                  f"RAW_PATH: \"{_TMP}/raw/\"\nTRANSCODE_PATH: \"{_TMP}/transcode/\"\n"
                  f"COMPLETED_PATH: \"{_TMP}/completed/\"\n"
                  f"DISC_PHOTO_PATH: \"{_TMP}/photos/\"\n"
                  "DISABLE_LOGIN: true\n")
os.environ["ARM_CONFIG_FILE"] = _CONFIG

try:
    arm_ripper = importlib.import_module("arm.ripper.arm_ripper")
    IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on the host
    arm_ripper = None
    IMPORT_ERROR = error


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class _Tracks:
    """Stand-in for the job's dynamic ``tracks`` relationship (only filter_by is used)."""

    def __init__(self, tracks):
        self._tracks = list(tracks)

    def filter_by(self, **criteria):
        return [track for track in self._tracks
                if all(getattr(track, key) == value for key, value in criteria.items())]


def _track(number, filename, chapters=0, filesize=0, ripped=True, source="MakeMKV"):
    return SimpleNamespace(track_number=str(number), filename=filename, chapters=chapters,
                           filesize=filesize, ripped=ripped, source=source, main_feature=False)


def _job(tracks, mainfeature=False, extras_sub="extras"):
    return SimpleNamespace(job_id=3, video_type="movie", title="Some Film", year="2001", hasnicetitle=True,
                           tracks=_Tracks(tracks),
                           config=SimpleNamespace(MAINFEATURE=mainfeature, EXTRAS_SUB=extras_sub))


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestPickMainFeature(unittest.TestCase):
    def setUp(self):
        self.raw_path = tempfile.mkdtemp(prefix="raw_", dir=_TMP)

    def tearDown(self):
        shutil.rmtree(self.raw_path, ignore_errors=True)

    def _write(self, sizes):
        """Create files of the given sizes (name -> bytes) in raw_path and return os.listdir()."""
        for name, size in sizes.items():
            with open(os.path.join(self.raw_path, name), "wb") as handle:
                handle.write(b"\0" * size)
        return sorted(os.listdir(self.raw_path))

    def test_most_chapters_beats_largest_file(self):
        files = self._write({"title_t00.mkv": 5000, "title_t01.mkv": 3000, "title_t02.mkv": 100})
        job = _job([_track(0, "title_t00.mkv", chapters=1, filesize=5000),
                    _track(1, "title_t01.mkv", chapters=18, filesize=3000),
                    _track(2, "title_t02.mkv", chapters=3, filesize=100)])
        with self.assertLogs(level="INFO") as logs:
            self.assertEqual(arm_ripper.pick_main_feature(files, job, self.raw_path), "title_t01.mkv")
        self.assertTrue(any("title 1" in line and "18 chapters" in line and "most chapters" in line
                            for line in logs.output), logs.output)

    def test_chapter_tie_goes_to_larger_file_on_disk(self):
        # MakeMKV's size estimate says title 0 is bigger, but on disk title 1 is: the disk wins
        files = self._write({"title_t00.mkv": 2000, "title_t01.mkv": 4000})
        job = _job([_track(0, "title_t00.mkv", chapters=12, filesize=9000),
                    _track(1, "title_t01.mkv", chapters=12, filesize=1000)])
        with self.assertLogs(level="INFO") as logs:
            self.assertEqual(arm_ripper.pick_main_feature(files, job, self.raw_path), "title_t01.mkv")
        self.assertTrue(any("tied on chapters" in line and "larger file" in line for line in logs.output),
                        logs.output)

    def test_full_tie_goes_to_lowest_track_number(self):
        files = self._write({"title_t03.mkv": 2000, "title_t10.mkv": 2000, "title_t07.mkv": 2000})
        job = _job([_track(10, "title_t10.mkv", chapters=8), _track(3, "title_t03.mkv", chapters=8),
                    _track(7, "title_t07.mkv", chapters=8)])
        with self.assertLogs(level="INFO") as logs:
            self.assertEqual(arm_ripper.pick_main_feature(files, job, self.raw_path), "title_t03.mkv")
        self.assertTrue(any("lower title number" in line for line in logs.output), logs.output)

    def test_no_chapter_info_falls_back_to_largest_file(self):
        files = self._write({"title_t00.mkv": 100, "title_t01.mkv": 7000, "title_t02.mkv": 3000})
        job = _job([_track(0, "title_t00.mkv", chapters=0), _track(1, "title_t01.mkv", chapters=0),
                    _track(2, "title_t02.mkv", chapters=None)])
        with self.assertLogs(level="INFO") as logs:
            self.assertEqual(arm_ripper.pick_main_feature(files, job, self.raw_path), "title_t01.mkv")
        self.assertTrue(any("No chapter info" in line and "largest file" in line for line in logs.output),
                        logs.output)

    def test_no_tracks_at_all_falls_back_to_largest_file(self):
        files = self._write({"a.mkv": 10, "b.mkv": 20})
        self.assertEqual(arm_ripper.pick_main_feature(files, _job([]), self.raw_path), "b.mkv")

    def test_only_ripped_tracks_with_a_file_are_considered(self):
        files = self._write({"title_t00.mkv": 5000, "title_t02.mkv": 500})
        job = _job([_track(0, "title_t00.mkv", chapters=2),
                    # skipped duplicate: never ripped, so no file for it
                    _track(1, "title_t01.mkv", chapters=30, ripped=False),
                    # marked ripped but the file is gone
                    _track(3, "title_t03.mkv", chapters=25),
                    _track(2, "title_t02.mkv", chapters=9)])
        self.assertEqual(arm_ripper.pick_main_feature(files, job, self.raw_path), "title_t02.mkv")


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestSkipTranscodeMovie(unittest.TestCase):
    def setUp(self):
        self.raw_path = tempfile.mkdtemp(prefix="raw_", dir=_TMP)
        for name, size in {"title_t00.mkv": 5000, "title_t01.mkv": 3000, "title_t02.mkv": 100}.items():
            with open(os.path.join(self.raw_path, name), "wb") as handle:
                handle.write(b"\0" * size)
        self.files = sorted(os.listdir(self.raw_path))
        self.tracks = [_track(0, "title_t00.mkv", chapters=1), _track(1, "title_t01.mkv", chapters=18),
                       _track(2, "title_t02.mkv", chapters=3)]

    def tearDown(self):
        shutil.rmtree(self.raw_path, ignore_errors=True)

    def _moves(self, job):
        with mock.patch.object(arm_ripper.utils, "move_files") as move_files:
            arm_ripper.skip_transcode_movie(self.files, job, self.raw_path)
        return [(call.args[1], call.args[3]) for call in move_files.call_args_list]

    def test_main_feature_moved_as_main_and_the_rest_as_extras(self):
        self.assertEqual(self._moves(_job(self.tracks)),
                         [("title_t00.mkv", False), ("title_t01.mkv", True), ("title_t02.mkv", False)])

    def test_mainfeature_setting_moves_only_the_main_feature(self):
        self.assertEqual(self._moves(_job(self.tracks, mainfeature=True)), [("title_t01.mkv", True)])

    def test_extras_sub_none_leaves_extras_in_place(self):
        self.assertEqual(self._moves(_job(self.tracks, extras_sub="None")), [("title_t01.mkv", True)])


if __name__ == "__main__":
    unittest.main()
