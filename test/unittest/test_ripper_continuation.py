"""
Tests for split films on the ripper side: a disc marked in the ui as the continuation of an earlier job's film
(side B of a flipper DVD, the second disc of a long film) has its main title joined onto that film's main
feature with ``mkvmerge -o joined feature + part``, never filed as a second ``Title (Year)`` copy beside it
(arm_ripper.apply_continuation_settings, continuation_folder, join_feature, file_continuation,
recheck_linked, runtime_check_skipped; utils.continuation_parent, move_files, check_for_wait).

Importing ``arm.ripper.arm_ripper`` pulls in the whole of ARM (config, Flask app, database), so a
throwaway arm.yaml is written first and pointed at with ARM_CONFIG_FILE. Needs the Linux
dependencies ARM itself needs (libdiscid, pyudev); skipped when they are missing. The test that joins
real Matroska files needs mkvmerge (mkvtoolnix) and is skipped without it.
"""
import importlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]

_TMP = tempfile.mkdtemp(prefix="arm_continuation_test_")
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
    cfg = importlib.import_module("arm.config.config")
    IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on the host
    arm_ripper = utils = cfg = None
    IMPORT_ERROR = error

MKVMERGE = shutil.which("mkvmerge")
FILM = "Das Boot (1981)"


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class _Tracks:
    """Stand-in for the job's dynamic ``tracks`` relationship (only filter_by is used)."""

    def __init__(self, tracks):
        self._tracks = list(tracks)

    def filter_by(self, **criteria):
        return _Tracks(track for track in self._tracks
                       if all(getattr(track, key) == value for key, value in criteria.items()))

    def count(self):
        return len(self._tracks)

    def __iter__(self):
        return iter(self._tracks)


def _track(number, length, chapters=0):
    return SimpleNamespace(track_number=str(number), filename=f"title_t{int(number):02}.mkv", length=length,
                           chapters=chapters, filesize=1000, ripped=True, source="MakeMKV", main_feature=False,
                           fps=25.0)


def _fake_updater(args, obj):
    for key, value in args.items():
        setattr(obj, key, value)
    return True


def _read(path):
    with open(path) as handle:
        return handle.read()


def _fake_mkvmerge(returncode=0):
    """subprocess.run for ``mkvmerge -o out a + b``: writes the inputs one after the other into out."""
    def run(command, **_kwargs):
        if returncode < 2:
            with open(command[2], "w") as out:
                for name in command[3:]:
                    if name != "+":
                        out.write(_read(name))
        return SimpleNamespace(returncode=returncode, stdout="mkvmerge says hello\n")
    return run


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class ContinuationFixture(unittest.TestCase):
    """A completed folder holding side A of Das Boot (job 76, already ripped) and side B's raw folder."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="continuation_", dir=_TMP)
        self.raw_path = os.path.join(self.root, "raw")
        self.completed = os.path.join(self.root, "completed")
        self.film_path = os.path.join(self.completed, "movies", FILM)
        self.feature = os.path.join(self.film_path, f"{FILM}.mkv")
        os.makedirs(self.raw_path)
        os.makedirs(os.path.join(self.film_path, "extras"))
        with open(self.feature, "w") as handle:
            handle.write("side A\n")
        with open(os.path.join(self.film_path, "extras", "title_t01.mkv"), "w") as handle:
            handle.write("side A's extra\n")
        self.parent = SimpleNamespace(job_id=76, title="Das Boot", year="1981", imdb_id="tt0082096",
                                      poster_url="http://poster", label="DVD_VIDEO", stage="177000000000",
                                      video_type="movie", hasnicetitle=True, path=self.film_path,
                                      title_manual=None, continues=True, films=[])
        self.patches = [mock.patch.object(utils, "database_updater", side_effect=_fake_updater),
                        mock.patch.object(utils, "set_permissions"),
                        mock.patch.object(utils, "notify")]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def _job(self, tracks=(), parent=True, label="DVD_VIDEO", path=None, mainfeature=False):
        for track in tracks:
            with open(os.path.join(self.raw_path, track.filename), "w") as handle:
                handle.write(f"side B title {track.track_number}\n" * (int(track.length) // 100))
        parent = self.parent if parent else None
        return SimpleNamespace(job_id=77, video_type="movie", title="DVD_VIDEO", year="", title_manual=None,
                               imdb_id=None, poster_url=None, hasnicetitle=False, label=label, stage="177000100000",
                               tracks=_Tracks(tracks), films=[], path=path or self.film_path,
                               continuation_of_job_id=76 if parent else None, continuation_of=parent,
                               continues=None, bonus_of_job_id=None, bonus_of=None, bonus_of_film_id=None,
                               bonus_of_film=None, disctype="dvd", devpath="/dev/sr1", errors=[], disc_photo=None,
                               disc_barcode=None,
                               config=SimpleNamespace(MAINFEATURE=mainfeature, EXTRAS_SUB="extras", DEST_EXT="mkv",
                                                      COMPLETED_PATH=self.completed, MINLENGTH="600",
                                                      MAXLENGTH="36000", SKIP_DUPLICATE_TITLES=True,
                                                      RIPMETHOD="mkv", SKIP_TRANSCODE=False, USE_FFMPEG=False,
                                                      TRANSCODE_PATH=os.path.join(self.root, "transcode"),
                                                      NOTIFY_RIP=False, NOTIFY_TRANSCODE=False,
                                                      GET_VIDEO_TITLE=False))

    def _side_b(self, mainfeature=False):
        """Side B: the rest of the film (title 0) and a short extra (title 1)."""
        return self._job([_track(0, 6000, chapters=20), _track(1, 300, chapters=1)], mainfeature=mainfeature)

    def _part_folder(self):
        return f"{self.film_path} - continuation job77"

    def _joining(self, returncode=0):
        """Patches for join_feature with a stand-in mkvmerge."""
        return (mock.patch.object(arm_ripper.shutil, "which", return_value="/usr/bin/mkvmerge"),
                mock.patch.object(arm_ripper.subprocess, "run", side_effect=_fake_mkvmerge(returncode)))


class TestContinuationParent(ContinuationFixture):
    def test_ordinary_job_is_not_a_continuation(self):
        job = self._job(parent=False)
        self.assertIsNone(utils.continuation_parent(job))
        self.assertFalse(utils.is_continuation(job))
        self.assertFalse(utils.is_continuation(SimpleNamespace(job_id=1)), "a job without the columns at all")

    def test_parent_that_is_gone_is_ignored(self):
        job = self._job()
        job.continuation_of = None
        self.assertIsNone(utils.continuation_parent(job))

    def test_bonus_disc_mark_wins(self):
        job = self._job()
        job.bonus_of_job_id, job.bonus_of = 40, SimpleNamespace(job_id=40)
        self.assertFalse(utils.is_continuation(job))
        self.assertEqual(arm_ripper.linked_kind(job), "bonus")
        self.assertEqual(arm_ripper.linked_kind(self._job()), "continuation")
        self.assertIsNone(arm_ripper.linked_kind(self._job(parent=False)))


class TestContinuationSettings(ContinuationFixture):
    def test_takes_the_films_title_and_keeps_its_own_rip_settings(self):
        job = self._job()
        before = dict(vars(job.config))
        self.assertTrue(arm_ripper.apply_continuation_settings(job))
        self.assertEqual((job.title, job.year, job.imdb_id, job.poster_url, job.video_type, job.hasnicetitle),
                         ("Das Boot", "1981", "tt0082096", "http://poster", "movie", True))
        self.assertEqual(vars(job.config), before)
        self.assertEqual(arm_ripper.apply_linked_settings(self._job()), "continuation")

    def test_title_fixed_by_hand_on_the_first_side(self):
        self.parent.title, self.parent.title_manual = "DVD_VIDEO", "Das Boot"
        job = self._job()
        arm_ripper.apply_continuation_settings(job)
        self.assertEqual(utils.fix_job_title(job), FILM)

    def test_ordinary_job_keeps_its_settings(self):
        job = self._job(parent=False)
        self.assertFalse(arm_ripper.apply_continuation_settings(job))
        self.assertEqual(job.title, "DVD_VIDEO")
        self.assertIsNone(arm_ripper.apply_linked_settings(job))


class TestContinuationFolder(ContinuationFixture):
    def test_film_folder_while_its_feature_is_here(self):
        job = self._job()
        self.assertEqual(arm_ripper.continuation_feature(job), self.feature)
        self.assertEqual(arm_ripper.continuation_folder(job), self.film_path)
        self.assertEqual(arm_ripper.linked_folder(job, "continuation"), self.film_path)

    def test_part_folder_once_the_film_has_shipped(self):
        shutil.rmtree(self.film_path)
        job = self._job()
        self.assertIsNone(arm_ripper.continuation_feature(job))
        self.assertEqual(arm_ripper.continuation_folder(job), self._part_folder())

    def test_part_folder_while_the_film_is_shipping(self):
        # The shipper removes each file once sent, the folder last: the feature can be gone before the folder
        os.remove(self.feature)
        self.assertEqual(arm_ripper.continuation_folder(self._job()), self._part_folder())

    def test_feature_named_after_the_folder_when_the_title_was_fixed_later(self):
        self.parent.title_manual = "Das Boot - Director's Cut"
        stamped = f"{self.film_path}_177000000000"
        os.rename(self.film_path, stamped)
        self.parent.path = stamped
        self.assertEqual(arm_ripper.continuation_feature(self._job()), os.path.join(stamped, f"{FILM}.mkv"))

    def test_photo_name(self):
        self.assertEqual(arm_ripper.linked_photo_name(self._job(), "continuation"), "disc-continuation-DVD_VIDEO.jpg")


class TestJoinFeature(ContinuationFixture):
    def setUp(self):
        super().setUp()
        self.part = os.path.join(self.raw_path, "title_t00.mkv")
        with open(self.part, "w") as handle:
            handle.write("side B\n")

    def test_joins_the_part_onto_the_feature(self):
        which, run = self._joining()
        with which, run as runner:
            self.assertEqual(arm_ripper.join_feature(self.feature, self.part), self.feature)
        command = runner.call_args.args[0]
        self.assertEqual(command, ["/usr/bin/mkvmerge", "-o", os.path.join(self.film_path, f".{FILM}.joining.mkv"),
                                   self.feature, "+", self.part])
        self.assertEqual(_read(self.feature), "side A\nside B\n")
        self.assertFalse(os.path.exists(self.part))
        self.assertEqual(sorted(os.listdir(self.film_path)), [f"{FILM}.mkv", "extras"])

    def test_warnings_are_a_success(self):
        which, run = self._joining(returncode=1)
        with which, run:
            self.assertEqual(arm_ripper.join_feature(self.feature, self.part), self.feature)
        self.assertEqual(_read(self.feature), "side A\nside B\n")

    def test_failure_leaves_both_parts_as_they_were(self):
        which, run = self._joining(returncode=2)
        with which, run:
            self.assertIsNone(arm_ripper.join_feature(self.feature, self.part))
        self.assertEqual(_read(self.feature), "side A\n")
        self.assertEqual(_read(self.part), "side B\n")
        self.assertEqual(sorted(os.listdir(self.film_path)), [f"{FILM}.mkv", "extras"])

    def test_half_written_output_of_a_failure_is_removed(self):
        def run(command, **_kwargs):
            with open(command[2], "w") as out:
                out.write("half")
            return SimpleNamespace(returncode=2, stdout="Error: The file could not be appended\n")

        with mock.patch.object(arm_ripper.shutil, "which", return_value="/usr/bin/mkvmerge"), \
                mock.patch.object(arm_ripper.subprocess, "run", side_effect=run):
            self.assertIsNone(arm_ripper.join_feature(self.feature, self.part))
        self.assertEqual(sorted(os.listdir(self.film_path)), [f"{FILM}.mkv", "extras"])

    def test_without_mkvmerge(self):
        with mock.patch.object(arm_ripper.shutil, "which", return_value=None), \
                mock.patch.object(arm_ripper.subprocess, "run") as run:
            self.assertIsNone(arm_ripper.join_feature(self.feature, self.part))
        run.assert_not_called()
        self.assertEqual(_read(self.part), "side B\n")

    def test_feature_that_is_not_matroska_becomes_an_mkv(self):
        mp4 = os.path.join(self.film_path, f"{FILM}.mp4")
        os.rename(self.feature, mp4)
        which, run = self._joining()
        with which, run:
            self.assertEqual(arm_ripper.join_feature(mp4, self.part), self.feature)
        self.assertEqual(sorted(os.listdir(self.film_path)), [f"{FILM}.mkv", "extras"])


@unittest.skipIf(MKVMERGE is None, "mkvmerge (mkvtoolnix) is not installed")
class TestJoinRealMatroska(ContinuationFixture):
    """join_feature with the real mkvmerge, on Matroska files holding a subtitle track and chapters."""

    def _mkv(self, path, start, chapters):
        srt = path + ".srt"
        with open(srt, "w") as handle:
            for number in range(1, 4):
                second = number * 10
                handle.write(f"{number}\n00:00:{second:02},000 --> 00:00:{second + 5:02},000\n{start} {number}\n\n")
        chapter_file = path + ".chapters.txt"
        with open(chapter_file, "w") as handle:
            for number, second in enumerate(chapters, 1):
                handle.write(f"CHAPTER{number:02}=00:00:{second:02}.000\nCHAPTER{number:02}NAME={start} {number}\n")
        subprocess.run([MKVMERGE, "-q", "-o", path, "--chapters", chapter_file, srt], check=True)
        os.remove(srt)
        os.remove(chapter_file)

    def _identify(self, path):
        return json.loads(subprocess.run([MKVMERGE, "-J", path], check=True, stdout=subprocess.PIPE).stdout)

    def test_one_file_with_the_chapters_run_on(self):
        os.remove(self.feature)
        self._mkv(self.feature, "side A", [0, 20])
        part = os.path.join(self.raw_path, "title_t00.mkv")
        self._mkv(part, "side B", [0, 20])
        first = self._identify(self.feature)["container"]["properties"]["duration"]
        self.assertEqual(arm_ripper.join_feature(self.feature, part), self.feature)
        joined = self._identify(self.feature)
        self.assertGreater(joined["container"]["properties"]["duration"], 1.9 * first)
        self.assertEqual([chapter["num_entries"] for chapter in joined["chapters"]], [4])
        self.assertEqual(len(joined["tracks"]), 1)
        self.assertFalse(os.path.exists(part))


class TestFileContinuation(ContinuationFixture):
    def _file(self, job, returncode=0):
        which, run = self._joining(returncode)
        with which, run:
            return arm_ripper.file_continuation(self.raw_path, job, os.listdir(self.raw_path))

    def test_main_title_is_joined_and_extras_are_named_after_the_disc(self):
        job = self._side_b()
        self.assertEqual(self._file(job), self.feature)
        self.assertTrue(_read(self.feature).startswith("side A\nside B title 0\n"))
        self.assertEqual(sorted(os.listdir(self.film_path)), [f"{FILM}.mkv", "extras"])
        # Side A's extra is not replaced by side B's title of the same name
        self.assertEqual(sorted(os.listdir(os.path.join(self.film_path, "extras"))),
                         ["DVD_VIDEO_title_t01.mkv", "title_t01.mkv"])
        self.assertEqual(_read(os.path.join(self.film_path, "extras", "title_t01.mkv")), "side A's extra\n")
        self.assertFalse(self.parent.continues, "the film is whole: the shipper may take it")
        utils.notify.assert_not_called()

    def test_mainfeature_files_no_extras(self):
        job = self._side_b(mainfeature=True)
        self._file(job)
        self.assertEqual(os.listdir(os.path.join(self.film_path, "extras")), ["title_t01.mkv"])

    def test_film_shipped_already(self):
        shutil.rmtree(self.film_path)
        job = self._side_b()
        job.path = self._part_folder()
        self.assertIsNone(self._file(job))
        self.assertEqual(job.path, self._part_folder())
        self.assertEqual(sorted(os.listdir(self._part_folder())), [f"{FILM} - pt2.mkv", "extras"])
        self.assertTrue(_read(os.path.join(self._part_folder(), f"{FILM} - pt2.mkv")).startswith("side B title 0"))
        self.assertEqual(os.listdir(os.path.join(self._part_folder(), "extras")), ["DVD_VIDEO_title_t01.mkv"])
        self.assertFalse(self.parent.continues, "nothing left on the ripper to hold")
        self.assertIn("has left the ripper", utils.notify.call_args.args[2])

    def test_failed_join_keeps_the_film_held(self):
        job = self._side_b()
        self.assertIsNone(self._file(job, returncode=2))
        self.assertEqual(_read(self.feature), "side A\n")
        self.assertEqual(job.path, self._part_folder())
        self.assertEqual(sorted(os.listdir(self._part_folder())), [f"{FILM} - pt2.mkv", "extras"])
        self.assertTrue(self.parent.continues, "the film waits on the ripper to be joined by hand")
        self.assertIn('"continues" is cleared on job 76', utils.notify.call_args.args[2])

    def test_handbrake_main_feature(self):
        job = self._side_b()
        for track in job.tracks:
            track.source = "HandBrake"
        list(job.tracks)[1].main_feature = True
        self.assertEqual(arm_ripper.continuation_main_title(self.raw_path, job, ["title_t00.mkv", "title_t01.mkv"]),
                         "title_t01.mkv")
        list(job.tracks)[1].main_feature = False
        self.assertEqual(arm_ripper.continuation_main_title(self.raw_path, job, ["title_t00.mkv", "title_t01.mkv"]),
                         "title_t00.mkv", "the largest file")

    def test_nothing_ripped(self):
        job = self._job()
        self.assertIsNone(self._file(job))
        self.assertEqual(_read(self.feature), "side A\n")
        self.assertTrue(self.parent.continues)

    def test_move_files_never_files_a_continuation_as_the_film(self):
        job = self._side_b()
        utils.move_files(self.raw_path, "title_t00.mkv", job, True)
        self.assertEqual(_read(self.feature), "side A\n")
        self.assertIn("DVD_VIDEO_title_t00.mkv", os.listdir(os.path.join(self.film_path, "extras")))


class TestRipContinuation(ContinuationFixture):
    """rip_visual_media for a continuation, marked before the rip or while it runs."""

    def _rip(self, job, mark=lambda _job: None, poster=True):
        def save_poster(final_directory, _job):
            if poster:
                with open(os.path.join(final_directory, "poster.png"), "w") as handle:
                    handle.write("png\n")

        job.config.SKIP_TRANSCODE = True
        which, run = self._joining()
        with mock.patch.object(arm_ripper.db.session, "refresh", side_effect=lambda _job: mark(job)), \
                mock.patch.object(arm_ripper.db.session, "commit"), \
                mock.patch.object(utils, "save_disc_poster", side_effect=save_poster) as poster, \
                mock.patch.object(arm_ripper.makemkv, "makemkv", return_value=self.raw_path), \
                mock.patch.object(arm_ripper, "retitle_final_directory",
                                  side_effect=lambda _dupes, _job, folder, _sub, _title: folder) as retitle, \
                mock.patch.object(utils, "scan_emby"), which, run, \
                mock.patch.dict(cfg.arm_config, {"DELRAWFILES": False, "ALLOW_DUPLICATES": True,
                                                 "DISC_PHOTO_COPY": False}):
            arm_ripper.rip_visual_media(False, job, "log", 0)
        return poster, retitle

    def _mark(self, job):
        job.continuation_of_job_id, job.continuation_of = 76, self.parent

    def test_marked_before_the_rip(self):
        job = self._side_b()
        poster, retitle = self._rip(job)
        poster.assert_not_called()
        retitle.assert_not_called()
        self.assertEqual(job.path, self.film_path)
        self.assertTrue(_read(self.feature).startswith("side A\nside B title 0\n"))
        self.assertEqual(os.listdir(os.path.join(self.completed, "movies")), [FILM])
        self.assertEqual(os.listdir(os.path.join(job.config.TRANSCODE_PATH, "movies")), [])

    def test_marked_before_the_rip_once_the_film_has_shipped(self):
        shutil.rmtree(self.film_path)
        job = self._side_b()
        self._rip(job)
        self.assertEqual(job.path, self._part_folder())
        self.assertEqual(os.listdir(os.path.join(self.completed, "movies")), [os.path.basename(self._part_folder())])
        self.assertIn(f"{FILM} - pt2.mkv", os.listdir(self._part_folder()))

    def test_marked_during_the_rip(self):
        job = self._side_b()
        job.continuation_of_job_id = job.continuation_of = None
        own = os.path.join(self.completed, "unidentified", "DVD_VIDEO")
        job.video_type = "unknown"
        poster, retitle = self._rip(job, self._mark)
        poster.assert_called_once_with(own, job)
        retitle.assert_not_called()
        # The folder made for the job (holding only the disc's poster) is gone, not left to block a re-rip
        self.assertFalse(os.path.exists(own))
        self.assertEqual(job.path, self.film_path)
        self.assertTrue(_read(self.feature).startswith("side A\nside B title 0\n"))

    def test_film_shipped_during_the_rip(self):
        job = self._side_b()

        def ship(_job):
            if os.path.isdir(self.film_path):
                shutil.rmtree(self.film_path)

        self._rip(job, ship)
        self.assertEqual(job.path, self._part_folder())
        self.assertEqual(os.listdir(os.path.join(self.completed, "movies")), [os.path.basename(self._part_folder())])

    def test_cleared_during_the_rip(self):
        def clear(job):
            job.continuation_of_job_id = job.continuation_of = None

        job = self._side_b()
        _poster, retitle = self._rip(job, clear)
        retitle.assert_called_once()
        own = f"{self.film_path}_{job.stage}"
        self.assertEqual(job.path, own)
        self.assertIn(f"{FILM}.mkv", os.listdir(own))
        self.assertEqual(_read(self.feature), "side A\n")


class TestCheckRuntime(ContinuationFixture):
    """
    arm_ripper.check_runtime leaves both parts of a split film alone: Das Boot's director's cut runs 209 minutes
    over two sides of 108 and 101, and the provider gives 149, so neither side (nor both) fits.
    """

    def _check(self, job):
        identify = arm_ripper.identify
        job.imdb_id = self.parent.imdb_id
        job.title, job.year = "Das Boot", "1981"
        job.config.GET_VIDEO_TITLE = True
        with mock.patch.object(identify, "identified_runtime", return_value=149 * 60) as runtime, \
                mock.patch.object(identify, "runtime_candidates", return_value=[]) as candidates, \
                mock.patch.object(identify, "apply_search_result") as apply:
            result = arm_ripper.check_runtime(job)
        return result, (runtime, candidates, apply)

    def test_continuation_is_not_checked(self):
        result, calls = self._check(self._job([_track(0, 6060)]))
        self.assertFalse(result)
        for call in calls:
            call.assert_not_called()

    def test_first_side_marked_continues_is_not_checked(self):
        job = self._job([_track(0, 6470)], parent=False)
        job.continues = True
        result, calls = self._check(job)
        self.assertFalse(result)
        for call in calls:
            call.assert_not_called()
        utils.notify.assert_not_called()

    def test_same_disc_not_marked_is_checked(self):
        job = self._job([_track(0, 6470)], parent=False)
        _result, (runtime, candidates, _apply) = self._check(job)
        runtime.assert_called_once_with(job)
        candidates.assert_called_once_with(job)


class TestManualWait(ContinuationFixture):
    def test_marking_a_continuation_ends_the_wait(self):
        job = self._job()
        job.config.MANUAL_WAIT, job.config.MANUAL_WAIT_TIME = True, 60
        with mock.patch.object(utils.time, "sleep") as sleep, mock.patch.object(utils.db.session, "refresh"):
            utils.check_for_wait(job)
        self.assertEqual(sleep.call_count, 1)


if __name__ == "__main__":
    unittest.main()
