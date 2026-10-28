"""
Tests for arm_ripper.file_collection / match_tracks_to_films: a disc identified as several films
(a double feature) files each ripped title whose runtime matches a film as that film's main
feature, the first film in the job folder and the others in their own folders.

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

_TMP = tempfile.mkdtemp(prefix="arm_collection_test_")
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


def _track(number, length, chapters=0, filesize=1000, ripped=True, fps=29.97):
    return SimpleNamespace(track_number=str(number), filename=f"title_t{int(number):02}.mkv", length=length,
                           chapters=chapters, filesize=filesize, ripped=ripped, source="MakeMKV", main_feature=False,
                           fps=fps)


def _film(position, title, year, runtime):
    folder = f"{title} ({year})" if year else title
    return SimpleNamespace(position=position, title=title, year=year, runtime=runtime, folder_name=folder,
                           imdb_id=None, track_number=None)


STUART_LITTLE = (84 * 60, 78 * 60)  # runtimes of the 1999 film and the 2002 sequel


def _films(first_runtime=STUART_LITTLE[0], second_runtime=STUART_LITTLE[1]):
    return [_film(1, "Stuart-Little", "1999", first_runtime), _film(2, "Stuart-Little-2", "2002", second_runtime)]


CATS_AND_DOGS = 2760  # TMDB gives both films of the Secret Life of Cats and Dogs DVD 46 minutes


def _cats_and_dogs():
    return [_film(1, "The Secret Life of Cats", "2014", CATS_AND_DOGS),
            _film(2, "The Secret Life of Dogs", "2013", CATS_AND_DOGS)]


def _numbers(matched):
    return {position: track.track_number for position, track in matched.items()}


def _fake_updater(args, obj):
    for key, value in args.items():
        setattr(obj, key, value)
    return True


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestMatchTracksToFilms(unittest.TestCase):
    def test_closest_runtime_wins_and_each_is_used_once(self):
        films = _films()
        # the 4900s title is within tolerance of both films; the two others are closer
        tracks = [_track(0, 5000), _track(1, 4700), _track(2, 4900)]
        matched = arm_ripper.match_tracks_to_films(tracks, films)
        self.assertEqual({position: track.track_number for position, track in matched.items()}, {1: "0", 2: "1"})

    def test_pal_disc_is_corrected_for_and_a_short_matches_nothing(self):
        films = _films()
        # PAL plays the 84 min film in 80.6 min, nearer the 78 min sequel than itself until the 25 fps is
        # allowed for; a 30 min bonus reel matches nothing
        matched = arm_ripper.match_tracks_to_films([_track(0, 4838, fps=25.0), _track(1, 1800, fps=25.0)], films)
        self.assertEqual({position: track.track_number for position, track in matched.items()}, {1: "0"})
        matched = arm_ripper.match_tracks_to_films([_track(0, 4838, fps=25.0), _track(1, 4493, fps=25.0)], films)
        self.assertEqual({position: track.track_number for position, track in matched.items()}, {1: "0", 2: "1"})
        # the same lengths off an NTSC disc are the sequel and nothing
        matched = arm_ripper.match_tracks_to_films([_track(0, 4838), _track(1, 4493)], films)
        self.assertEqual({position: track.track_number for position, track in matched.items()}, {2: "0"})

    def test_unknown_runtimes_and_lengths_do_not_match(self):
        films = _films(first_runtime=None)
        matched = arm_ripper.match_tracks_to_films([_track(0, 5065), _track(1, 4656), _track(2, None)], films)
        self.assertEqual({position: track.track_number for position, track in matched.items()}, {2: "1"})

    def test_distinct_runtimes_decide_over_disc_order(self):
        # Stuart Little's 84 and 78 minutes are told apart, even with the sequel first on the disc
        self.assertEqual(arm_ripper.runtime_alike_films(_films()), [])
        matched = arm_ripper.match_tracks_to_films([_track(0, 4656), _track(1, 5065)], _films())
        self.assertEqual(_numbers(matched), {1: "1", 2: "0"})

    def test_equal_runtimes_are_told_apart_by_disc_order(self):
        # both titles fit both films; the second title is the closer fit, but not to the second film
        films = _cats_and_dogs()
        self.assertEqual([[film.position for film in group] for group in arm_ripper.runtime_alike_films(films)],
                         [[1, 2]])
        with self.assertLogs(level="INFO") as logs:
            matched = arm_ripper.match_tracks_to_films([_track(0, 2700), _track(1, 2780)], films)
        self.assertEqual(_numbers(matched), {1: "0", 2: "1"})
        self.assertTrue(any("by disc order" in line for line in logs.output), logs.output)

    def test_runtimes_within_tolerance_of_each_other_are_told_apart_by_disc_order(self):
        films = [_film(1, "First", "2001", 2760), _film(2, "Second", "2002", 2800)]
        # by closeness the first title would be the second film
        matched = arm_ripper.match_tracks_to_films([_track(0, 2805), _track(1, 2750)], films)
        self.assertEqual(_numbers(matched), {1: "0", 2: "1"})

    def test_alike_films_not_all_matched_are_left_for_disc_order(self):
        # only the second title fits a 46 minute film; it is no more one film than the other
        films = _cats_and_dogs()
        tracks = [_track(0, 3152), _track(1, 2900)]
        matched = arm_ripper.match_tracks_to_films(tracks, films)
        self.assertEqual(matched, {})
        self.assertEqual(_numbers(arm_ripper.match_by_disc_order(tracks, films, matched)), {1: "0", 2: "1"})


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestMatchByDiscOrder(unittest.TestCase):
    def test_secret_life_of_cats_and_dogs(self):
        # ts140 job 135: both titles run well over the 46 minutes TMDB gives the films
        films = _cats_and_dogs()
        tracks = [_track(0, 3152), _track(1, 2998), _track(2, 620)]
        matched = arm_ripper.match_tracks_to_films(tracks, films)
        self.assertEqual(matched, {})
        with self.assertLogs(level="INFO") as logs:
            paired = arm_ripper.match_by_disc_order(tracks, films, matched)
        self.assertEqual(_numbers(paired), {1: "0", 2: "1"})
        self.assertTrue(any("is The Secret Life of Dogs (2013) by disc order" in line for line in logs.output),
                        logs.output)

    def test_fills_in_around_films_runtime_placed(self):
        films = _cats_and_dogs() + [_film(3, "Stuart-Little", "1999", STUART_LITTLE[0])]
        tracks = [_track(0, 3152), _track(1, 5065), _track(2, 2998)]
        matched = arm_ripper.match_tracks_to_films(tracks, films)
        self.assertEqual(_numbers(matched), {3: "1"})
        self.assertEqual(_numbers(arm_ripper.match_by_disc_order(tracks, films, matched)), {1: "0", 2: "2"})

    def test_needs_as_many_feature_length_titles_as_films(self):
        films = _cats_and_dogs()
        self.assertEqual(arm_ripper.match_by_disc_order([_track(0, 3152), _track(1, 1200)], films, {}), {})
        self.assertEqual(arm_ripper.match_by_disc_order([_track(0, 3152), _track(1, 2998), _track(2, 3300)],
                                                        films, {}), {})

    def test_leaves_a_single_film_or_unknown_runtimes_to_the_main_feature_rule(self):
        tracks = [_track(0, 3152), _track(1, 2998)]
        films = _cats_and_dogs()
        self.assertEqual(arm_ripper.match_by_disc_order(tracks, films, {2: tracks[1]}), {})
        films[1].runtime = None
        self.assertEqual(arm_ripper.match_by_disc_order(tracks, films, {}), {})


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestFileCollection(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="filing_", dir=_TMP)
        self.raw_path = os.path.join(self.root, "raw")
        self.completed = os.path.join(self.root, "completed")
        os.makedirs(self.raw_path)
        self.patches = [mock.patch.object(arm_ripper.utils, "database_updater", side_effect=_fake_updater),
                        mock.patch.object(arm_ripper.utils, "set_permissions")]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def _job(self, tracks, films, mainfeature=False, extras_sub="extras", video_type="movie",
             title="Stuart-Little", year="1999"):
        for track in tracks:
            with open(os.path.join(self.raw_path, track.filename), "wb") as handle:
                handle.write(b"\0" * track.filesize)
        return SimpleNamespace(job_id=27, video_type=video_type, title=title, year=year, title_manual=None,
                               hasnicetitle=True, stage="171", tracks=_Tracks(tracks), films=films,
                               path=os.path.join(self.completed, "movies", f"{title} ({year})"),
                               config=SimpleNamespace(MAINFEATURE=mainfeature, EXTRAS_SUB=extras_sub,
                                                      COMPLETED_PATH=self.completed, DEST_EXT="mkv"))

    def _file(self, *parts):
        return os.path.join(self.completed, "movies", *parts)

    def test_each_film_gets_its_own_folder(self):
        tracks = [_track(0, 5065, chapters=9), _track(1, 4656, chapters=8), _track(2, 1500, chapters=1)]
        job = self._job(tracks, _films())
        self.assertTrue(arm_ripper.file_collection(self.raw_path, job, os.listdir(self.raw_path)))
        self.assertTrue(os.path.isfile(self._file("Stuart-Little (1999)", "Stuart-Little (1999).mkv")))
        self.assertTrue(os.path.isfile(self._file("Stuart-Little-2 (2002)", "Stuart-Little-2 (2002).mkv")))
        self.assertTrue(os.path.isfile(self._file("Stuart-Little (1999)", "extras", "title_t02.mkv")))
        self.assertEqual(os.listdir(self.raw_path), [])
        self.assertEqual([film.track_number for film in job.films], ["0", "1"])

    def test_first_film_without_runtime_falls_back_to_the_main_feature_rule(self):
        tracks = [_track(0, 5065, chapters=9), _track(1, 4656, chapters=8)]
        job = self._job(tracks, _films(first_runtime=None))
        with self.assertLogs(level="INFO") as logs:
            self.assertTrue(arm_ripper.file_collection(self.raw_path, job, os.listdir(self.raw_path)))
        self.assertTrue(os.path.isfile(self._file("Stuart-Little (1999)", "Stuart-Little (1999).mkv")))
        self.assertTrue(os.path.isfile(self._file("Stuart-Little-2 (2002)", "Stuart-Little-2 (2002).mkv")))
        self.assertTrue(any("main-feature rule" in line for line in logs.output), logs.output)

    def test_no_runtimes_at_all_files_like_a_single_film(self):
        tracks = [_track(0, 5065, chapters=9), _track(1, 4656, chapters=8)]
        job = self._job(tracks, _films(first_runtime=None, second_runtime=None))
        with self.assertLogs(level="INFO") as logs:
            self.assertTrue(arm_ripper.file_collection(self.raw_path, job, os.listdir(self.raw_path)))
        self.assertTrue(os.path.isfile(self._file("Stuart-Little (1999)", "Stuart-Little (1999).mkv")))
        self.assertTrue(os.path.isfile(self._file("Stuart-Little (1999)", "extras", "title_t01.mkv")))
        self.assertFalse(os.path.exists(self._file("Stuart-Little-2 (2002)")))
        self.assertTrue(any("No ripped title matched the runtime of Stuart-Little-2 (2002)" in line
                            for line in logs.output), logs.output)

    def test_films_runtime_cannot_place_are_filed_by_disc_order(self):
        # ts140 job 135 filed the dogs film as an extra of the cats film
        tracks = [_track(0, 3152, chapters=6), _track(1, 2998, chapters=6), _track(2, 620, chapters=1)]
        job = self._job(tracks, _cats_and_dogs(), title="The Secret Life of Cats", year="2014")
        with self.assertLogs(level="INFO") as logs:
            self.assertTrue(arm_ripper.file_collection(self.raw_path, job, os.listdir(self.raw_path)))
        cats, dogs = "The Secret Life of Cats (2014)", "The Secret Life of Dogs (2013)"
        self.assertTrue(os.path.isfile(self._file(cats, f"{cats}.mkv")))
        self.assertTrue(os.path.isfile(self._file(dogs, f"{dogs}.mkv")))
        self.assertEqual(os.listdir(self._file(cats, "extras")), ["title_t02.mkv"])
        self.assertEqual([film.track_number for film in job.films], ["0", "1"])
        self.assertFalse(any("main-feature rule" in line or "not filed" in line for line in logs.output),
                         logs.output)

    def test_mainfeature_leaves_extras_in_place(self):
        tracks = [_track(0, 5065, chapters=9), _track(1, 4656, chapters=8), _track(2, 1500, chapters=1)]
        job = self._job(tracks, _films(), mainfeature=True)
        self.assertTrue(arm_ripper.file_collection(self.raw_path, job, os.listdir(self.raw_path)))
        self.assertTrue(os.path.isfile(self._file("Stuart-Little-2 (2002)", "Stuart-Little-2 (2002).mkv")))
        self.assertEqual(os.listdir(self.raw_path), ["title_t02.mkv"])

    def test_existing_film_folder_is_not_overwritten(self):
        os.makedirs(self._file("Stuart-Little-2 (2002)"))
        tracks = [_track(0, 5065, chapters=9), _track(1, 4656, chapters=8)]
        job = self._job(tracks, _films())
        self.assertTrue(arm_ripper.file_collection(self.raw_path, job, os.listdir(self.raw_path)))
        self.assertTrue(os.path.isfile(self._file("Stuart-Little-2 (2002)_171", "Stuart-Little-2 (2002).mkv")))

    def test_ordinary_jobs_are_left_to_the_usual_filing(self):
        tracks = [_track(0, 5065, chapters=9)]
        self.assertFalse(arm_ripper.file_collection(self.raw_path, self._job(tracks, []), os.listdir(self.raw_path)))
        self.assertFalse(arm_ripper.file_collection(self.raw_path, self._job(tracks, _films()[:1]),
                                                    os.listdir(self.raw_path)))
        self.assertFalse(arm_ripper.file_collection(self.raw_path, self._job(tracks, _films(), video_type="series"),
                                                    os.listdir(self.raw_path)))
        self.assertEqual(os.listdir(self.raw_path), ["title_t00.mkv"])

    def test_lifted_mainfeature_files_the_films_without_extras(self):
        tracks = [_track(0, 5065, chapters=9), _track(1, 4656, chapters=8), _track(2, 1500, chapters=1)]
        job = self._job(tracks, _films())
        self.assertTrue(arm_ripper.file_collection(self.raw_path, job, os.listdir(self.raw_path),
                                                   main_features_only=True))
        self.assertTrue(os.path.isfile(self._file("Stuart-Little (1999)", "Stuart-Little (1999).mkv")))
        self.assertTrue(os.path.isfile(self._file("Stuart-Little-2 (2002)", "Stuart-Little-2 (2002).mkv")))
        self.assertFalse(os.path.exists(self._file("Stuart-Little (1999)", "extras")))
        self.assertEqual(os.listdir(self.raw_path), ["title_t02.mkv"])


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestApplyCollectionSettings(unittest.TestCase):
    """With MAINFEATURE on only one title is ripped, so a double feature's other films would be lost"""

    def setUp(self):
        patch = mock.patch.object(arm_ripper.utils, "database_updater", side_effect=_fake_updater)
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def _job(films, mainfeature=True, video_type="movie"):
        return SimpleNamespace(job_id=27, video_type=video_type, films=films,
                               config=SimpleNamespace(MAINFEATURE=mainfeature))

    def test_lifts_mainfeature_for_a_double_feature(self):
        job = self._job(_films())
        self.assertTrue(arm_ripper.apply_collection_settings(job))
        self.assertFalse(job.config.MAINFEATURE)

    def test_leaves_other_jobs_alone(self):
        for job in (self._job(_films(), mainfeature=False), self._job(_films()[:1]), self._job([]),
                    self._job(_films(), video_type="series")):
            before = job.config.MAINFEATURE
            self.assertFalse(arm_ripper.apply_collection_settings(job))
            self.assertEqual(job.config.MAINFEATURE, before)

    def test_move_files_post_passes_main_features_only_on(self):
        job = self._job(_films())
        job.tracks = _Tracks([])
        with mock.patch.object(arm_ripper.utils, "is_bonus_disc", return_value=False), \
                mock.patch.object(arm_ripper.os, "listdir", return_value=[]), \
                mock.patch.object(arm_ripper, "file_collection", return_value=True) as filing:
            arm_ripper.move_files_post("/raw", job, True)
        filing.assert_called_once_with("/raw", job, [], True)

    def test_move_files_post_restores_mainfeature_when_no_longer_a_double_feature(self):
        for films in ([], _films()[:1]):
            job = self._job(films, mainfeature=False)
            job.tracks = _Tracks([])
            with mock.patch.object(arm_ripper.utils, "is_bonus_disc", return_value=False), \
                    mock.patch.object(arm_ripper.os, "listdir", return_value=[]):
                arm_ripper.move_files_post("/raw", job, True)
            self.assertTrue(job.config.MAINFEATURE, films)

    def test_move_files_post_leaves_mainfeature_alone_when_it_was_not_lifted(self):
        job = self._job(_films()[:1], mainfeature=False)
        job.tracks = _Tracks([])
        with mock.patch.object(arm_ripper.utils, "is_bonus_disc", return_value=False), \
                mock.patch.object(arm_ripper.os, "listdir", return_value=[]):
            arm_ripper.move_files_post("/raw", job, False)
        self.assertFalse(job.config.MAINFEATURE)


if __name__ == "__main__":
    unittest.main()
