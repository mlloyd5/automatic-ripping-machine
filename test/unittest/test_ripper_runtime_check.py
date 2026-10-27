"""
Tests for arm_ripper.check_runtime and the identify helpers it uses: once MakeMKV has listed the
disc's titles, the film the disc was identified as must run as long as one of its feature-length
titles, or another film with the disc's title that does is taken. Also the guard that keeps
generic disc labels like DVD_VIDEO from being searched for at all.

The search results and runtimes are TMDB's, as looked up for the discs that went wrong on the t620:
RED (2010) identified as a 2017 short called Red, and a Fools Rush In DVD labelled DVD_VIDEO
identified as "Backstreet Boys: All Access DVD".

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

_TMP = tempfile.mkdtemp(prefix="arm_runtime_check_test_")
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
    identify = importlib.import_module("arm.ripper.identify")
    IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on the host
    arm_ripper = identify = None
    IMPORT_ERROR = error


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def _result(title, year, votes, imdb_id):
    return {"Title": title, "Year": year, "vote_count": votes, "imdbID": imdb_id, "id": imdb_id,
            "Type": "movie", "Poster": ""}


RED_2010 = _result("RED", "2010", 7370, "tt1245526")
RED_2017_SHORT = _result("Red", "2017", 27, "tt6102080")
RED_2 = _result("RED 2", "2013", 4476, "tt1821694")
HIGH_AND_LOW = _result("High & Low The Red Rain", "2016", 18, "tt5923040")
RED_ANY_YEAR = [RED_2010, _result("Turning Red", "2022", 6100, "tt8097030"),
                _result("Red One", "2024", 3216, "tt14948432"), _result("Red Sparrow", "2018", 6531, "tt2873282"),
                RED_2017_SHORT]
STAGECOACH_1939 = _result("Stagecoach", "1939", 1290, "tt0031971")
TEXAS_JACK = _result("Stagecoach: The Texas Jack Story", "2016", 40, "tt5679402")
STAGECOACH_ANY_YEAR = [STAGECOACH_1939, _result("Stagecoach", "1966", 51, "tt0061015"),
                       _result("Stagecoach Outlaws", "1945", 2, "tt0038100"),
                       _result("Sonora Stagecoach", "1944", 2, "tt0037297"), TEXAS_JACK]
RUNTIMES = {"tt1245526": 111 * 60, "tt6102080": 15 * 60, "tt1821694": 116 * 60, "tt8097030": 100 * 60,
            "tt5923040": 110 * 60,
            "tt0031971": 96 * 60, "tt5679402": 90 * 60, "tt0061015": 115 * 60}


def _track(number, length, fps=23.976):
    return SimpleNamespace(track_number=str(number), length=length, fps=fps)


def _job(label, title, year, imdb_id, tracks):
    return SimpleNamespace(label=label, title=title, year=year, imdb_id=imdb_id, video_type="movie",
                           title_manual=None, tracks=tracks, films=[],
                           config=SimpleNamespace(GET_VIDEO_TITLE=True))


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestPlaceholderTitles(unittest.TestCase):
    def test_authoring_defaults_and_the_placeholder_name_no_film(self):
        for label in ("DVD_VIDEO", "DVDVIDEO", "DISC1", "Disc 2", "BDROM", "VIDEO_TS", "not identified", "", None):
            self.assertTrue(identify.is_placeholder_title(label), label)

    def test_real_titles_are_searched(self):
        for label in ("RED", "RED_2", "Stagecoach", "DVD Memories", "Disc Jockey", "MEETPARENTS"):
            self.assertFalse(identify.is_placeholder_title(label), label)

    def test_metadata_selector_does_not_search_for_dvd_video(self):
        with mock.patch.object(identify, "provider_search") as search:
            self.assertIsNone(identify.metadata_selector(SimpleNamespace(), "DVD_VIDEO", None))
        search.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestRuntimeCandidates(unittest.TestCase):
    def _candidates(self, job, pages):
        def search(title, year=None):
            self.assertIsNone(year, "candidates are searched for without the year")
            return {"Search": pages[title]}
        with mock.patch.object(identify, "_provider_search", side_effect=search):
            return identify.runtime_candidates(job)

    def test_exact_titles_without_the_identified_film(self):
        job = _job("RED", "Red", "2017", RED_2017_SHORT["imdbID"], [])
        self.assertEqual(self._candidates(job, {"RED": RED_ANY_YEAR}), [RED_2010])

    def test_label_followed_by_a_subtitle_counts(self):
        job = _job("Stagecoach", "Stagecoach", "1939", STAGECOACH_1939["imdbID"], [])
        titles = [result["Title"] for result in self._candidates(job, {"Stagecoach": STAGECOACH_ANY_YEAR})]
        self.assertEqual(titles, ["Stagecoach", "Stagecoach: The Texas Jack Story"])  # 1966 has the more votes

    def test_label_and_title_are_both_searched(self):
        job = _job("RED_2", "High-and-Low-The-Red-Rain", "2016", "tt5923040", [])
        pages = {"RED 2": [RED_2, RED_2010], "High-and-Low-The-Red-Rain": []}
        self.assertEqual(self._candidates(job, pages), [RED_2])

    def test_series_are_no_candidates(self):
        # TMDB's search falls back to TV, and OMDb's lists series beside the films: a 90 minute
        # miniseries episode would fit a film's title, and taking it would make the movie rip a series
        series = dict(_result("Red", "2019", 900, "tt9999001"), Type="series")
        episode = dict(_result("Red", "2008", 40, "tt9999002"), Type="episode")
        job = _job("RED", "Red", "2017", RED_2017_SHORT["imdbID"], [])
        self.assertEqual(self._candidates(job, {"RED": [series, RED_2010, episode, RED_2017_SHORT]}), [RED_2010])

    def test_generic_label_is_not_searched(self):
        job = _job("DVD_VIDEO", "Backstreet-Boys--All-Access-DVD", "1998", "tt0000001", [])
        pages = {"Backstreet-Boys--All-Access-DVD": []}
        self.assertEqual(self._candidates(job, pages), [])


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestCheckRuntime(unittest.TestCase):
    """RED's disc: the film twice (one copy without chapters) at 1:51:09, and a 13 minute extra."""

    RED_DISC = [_track(0, 6669), _track(1, 6669), _track(2, 811)]

    def setUp(self):
        patches = {
            "identified": mock.patch.object(identify, "identified_runtime",
                                            side_effect=lambda job: RUNTIMES.get(job.imdb_id)),
            "candidates": mock.patch.object(identify, "runtime_candidates"),
            "runtime": mock.patch.object(identify, "film_runtime",
                                         side_effect=lambda result: RUNTIMES.get(result["imdbID"])),
            "apply": mock.patch.object(identify, "apply_search_result"),
            "notify": mock.patch.object(arm_ripper.utils, "notify"),
        }
        self.mocks = {name: patch.start() for name, patch in patches.items()}
        for patch in patches.values():
            self.addCleanup(patch.stop)

    def test_wrong_film_of_the_same_title_is_replaced(self):
        self.mocks["candidates"].return_value = [RED_2010]
        job = _job("RED", "Red", "2017", RED_2017_SHORT["imdbID"], self.RED_DISC)
        self.assertTrue(arm_ripper.check_runtime(job))
        self.mocks["apply"].assert_called_once_with(job, RED_2010)
        self.mocks["notify"].assert_called_once_with(
            job, mock.ANY, "Identified as RED (2010) instead of Red (2017) by runtime")

    def test_closest_runtime_wins(self):
        # RED 2 (116 min) is within the tolerance of a 111 minute title too, but RED is closer
        self.mocks["candidates"].return_value = [RED_2, RED_2010]
        job = _job("RED", "Red", "2017", RED_2017_SHORT["imdbID"], self.RED_DISC)
        arm_ripper.check_runtime(job)
        self.mocks["apply"].assert_called_once_with(job, RED_2010)

    def test_fitting_film_is_kept_without_looking_further(self):
        job = _job("RED", "RED", "2010", RED_2010["imdbID"], self.RED_DISC)
        self.assertFalse(arm_ripper.check_runtime(job))
        self.mocks["candidates"].assert_not_called()
        self.mocks["apply"].assert_not_called()

    def test_stagecoach_1939_gives_way_to_the_texas_jack_story(self):
        self.mocks["candidates"].return_value = [_result("Stagecoach", "1966", 51, "tt0061015"), TEXAS_JACK]
        disc = [_track(0, 89 * 60 + 40), _track(1, 600)]
        job = _job("Stagecoach", "Stagecoach", "1939", STAGECOACH_1939["imdbID"], disc)
        self.assertTrue(arm_ripper.check_runtime(job))
        self.mocks["apply"].assert_called_once_with(job, TEXAS_JACK)

    def test_nothing_fits_keeps_the_title_and_tells_the_user(self):
        self.mocks["candidates"].return_value = [_result("Turning Red", "2022", 6100, "tt8097030")]
        job = _job("RED", "Red", "2017", RED_2017_SHORT["imdbID"], self.RED_DISC)
        self.assertFalse(arm_ripper.check_runtime(job))
        self.mocks["apply"].assert_not_called()
        self.mocks["notify"].assert_called_once()

    def test_loose_fit_gives_way_to_a_close_one(self):
        # RED 2's disc (115:49) was taken for "High & Low The Red Rain" (110 min): inside the 6%, but
        # RED 2, named like the label, runs 116 minutes
        self.mocks["candidates"].return_value = [RED_2, RED_2010]
        disc = [_track(0, 6949), _track(1, 2081), _track(2, 6949), _track(3, 811)]
        job = _job("RED_2", "High-and-Low-The-Red-Rain", "2016", HIGH_AND_LOW["imdbID"], disc)
        self.assertTrue(arm_ripper.check_runtime(job))
        self.mocks["apply"].assert_called_once_with(job, RED_2)
        self.mocks["notify"].assert_called_once()

    def test_loose_fit_stands_without_a_closer_one(self):
        self.mocks["candidates"].return_value = [RED_2010]  # 111 min: further off than the identified film
        disc = [_track(0, 6949)]
        job = _job("RED_2", "High-and-Low-The-Red-Rain", "2016", HIGH_AND_LOW["imdbID"], disc)
        self.assertFalse(arm_ripper.check_runtime(job))
        self.mocks["apply"].assert_not_called()
        self.mocks["notify"].assert_not_called()

    def test_extended_cut_on_the_disc_still_fits(self):
        # the theatrical cut is one of the feature-length titles, so the longer one is no reason to doubt
        job = _job("RED", "RED", "2010", RED_2010["imdbID"], [_track(0, 7500), _track(1, 6669)])
        self.assertFalse(arm_ripper.check_runtime(job))
        self.mocks["candidates"].assert_not_called()

    def test_left_alone(self):
        cases = {
            "title set by hand": dict(title_manual="Red"),
            "series": dict(video_type="series"),
            "not identified": dict(imdb_id=None),
            "double feature": dict(films=[SimpleNamespace(position=1)]),
            "no titles": dict(tracks=[]),
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                job = _job("RED", "Red", "2017", RED_2017_SHORT["imdbID"], self.RED_DISC)
                vars(job).update(overrides)
                self.assertFalse(arm_ripper.check_runtime(job))
        self.mocks["apply"].assert_not_called()

    def test_unknown_runtime_gives_way_to_a_close_fit(self):
        # the usual wrong pick is a little-known film without a runtime on TMDB
        self.mocks["candidates"].return_value = [RED_2, RED_2010]
        job = _job("RED", "Red", "2017", "tt-unknown", self.RED_DISC)
        self.assertTrue(arm_ripper.check_runtime(job))
        self.mocks["apply"].assert_called_once_with(job, RED_2010)

    def test_failed_lookup_leaves_the_identification_alone(self):
        # a network error or rate limit is no "unknown runtime": no candidate search, no switch
        self.mocks["identified"].side_effect = identify.RuntimeLookupError("429 Too Many Requests")
        self.mocks["candidates"].return_value = [RED_2010]
        job = _job("RED", "Red", "2017", RED_2017_SHORT["imdbID"], self.RED_DISC)
        with self.assertLogs(level="WARNING"):
            self.assertFalse(arm_ripper.check_runtime(job))
        self.mocks["candidates"].assert_not_called()
        self.mocks["apply"].assert_not_called()
        self.mocks["notify"].assert_not_called()

    def test_rerip_with_the_same_title_is_not_announced(self):
        # a re-rip copies the earlier job's title and year but not its IMDb id
        self.mocks["candidates"].return_value = [RED_2010]
        job = _job("RED", "RED", "2010", RED_2017_SHORT["imdbID"], self.RED_DISC)
        self.assertTrue(arm_ripper.check_runtime(job))
        self.mocks["apply"].assert_called_once_with(job, RED_2010)
        self.mocks["notify"].assert_not_called()

    def test_unknown_runtime_stands_against_a_loose_fit(self):
        # RED 2 (116 min) is 5 minutes off the 111 minute title: inside the 6%, but not close, so a
        # correct film TMDB has no runtime for keeps its title, and nobody is told
        self.mocks["candidates"].return_value = [RED_2, _result("Turning Red", "2022", 6100, "tt8097030")]
        job = _job("RED", "Red", "2017", "tt-unknown", self.RED_DISC)
        self.assertFalse(arm_ripper.check_runtime(job))
        self.mocks["apply"].assert_not_called()
        self.mocks["notify"].assert_not_called()

    def test_pal_disc_runs_the_film_four_percent_fast(self):
        # a 100 minute film is a 96 minute title at 25 fps
        self.mocks["identified"].side_effect = lambda job: 100 * 60
        job = _job("FILM", "Film", "2001", "tt0000100", [_track(0, 96 * 60, fps=25.0), _track(1, 600, fps=25.0)])
        self.assertFalse(arm_ripper.check_runtime(job))
        self.mocks["candidates"].assert_not_called()

    def test_pal_correction_only_applies_at_25_fps(self):
        # the same 96 minute title at 23.976 fps plays at film speed: a 100 minute film is 4 minutes
        # off it, a loose fit, so the other films of the title are looked at
        self.mocks["identified"].side_effect = lambda job: 100 * 60
        self.mocks["candidates"].return_value = []
        job = _job("FILM", "Film", "2001", "tt0000100", [_track(0, 96 * 60), _track(1, 600)])
        self.assertFalse(arm_ripper.check_runtime(job))
        self.mocks["candidates"].assert_called_once()

    def test_runtime_fit_at_25_and_23_976_fps(self):
        pal, film = [_track(0, 96 * 60, fps=25.0)], [_track(0, 96 * 60)]
        self.assertEqual(arm_ripper.runtime_fit(100 * 60, pal), 0)
        self.assertEqual(arm_ripper.runtime_fit(100 * 60, film), 4 * 60)
        # a 92 minute film is 88.3 minutes at 25 fps, 7.7 minutes short of the PAL title: no fit;
        # at 23.976 fps it is 4 minutes off, inside the 6% (a loose fit)
        self.assertIsNone(arm_ripper.runtime_fit(92 * 60, pal))
        self.assertEqual(arm_ripper.runtime_fit(92 * 60, film), 4 * 60)

    def test_backup_rip_without_title_lengths_is_not_checked(self):
        job = _job("RED", "Red", "2017", RED_2017_SHORT["imdbID"], [])
        with self.assertLogs(level="WARNING"):
            self.assertFalse(arm_ripper.check_runtime(job))
        self.mocks["identified"].assert_not_called()

    def test_disabled_title_lookup_is_not_checked(self):
        job = _job("RED", "Red", "2017", RED_2017_SHORT["imdbID"], self.RED_DISC)
        job.config.GET_VIDEO_TITLE = False
        self.assertFalse(arm_ripper.check_runtime(job))
        self.mocks["identified"].assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestIdentifiedRuntime(unittest.TestCase):
    """identified_runtime: /find by the IMDb id, then the film's details (TMDB) or OMDb's lookup by id."""

    RED = {"id": 39514, "title": "RED", "release_date": "2010-10-13"}

    def setUp(self):
        patch = mock.patch.dict(identify.cfg.arm_config, {"METADATA_PROVIDER": "tmdb", "TMDB_API_KEY": "x",
                                                          "OMDB_API_KEY": "y"})
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def _response(payload, status=200):
        response = mock.Mock(status_code=status)
        response.json.return_value = payload
        if status >= 400:
            response.raise_for_status.side_effect = identify.requests.HTTPError(f"{status} error")
        return response

    def _get(self, *responses):
        return mock.patch.object(identify.requests, "get", side_effect=list(responses))

    def _job(self, imdb_id="tt1245526"):
        return _job("RED", "RED", "2010", imdb_id, [])

    def test_film(self):
        with self._get(self._response({"movie_results": [self.RED], "tv_results": []}),
                       self._response({"id": 39514, "runtime": 111})) as get:
            self.assertEqual(identify.identified_runtime(self._job()), 111 * 60)
        self.assertIn("/movie/39514?", get.call_args_list[1].args[0])

    def test_film_without_a_runtime(self):
        with self._get(self._response({"movie_results": [self.RED], "tv_results": []}),
                       self._response({"id": 39514, "runtime": 0})):
            self.assertIsNone(identify.identified_runtime(self._job()))

    def test_series(self):
        with self._get(self._response({"movie_results": [], "tv_results": [{"id": 1399, "name": "Red"}]})) as get:
            self.assertIsNone(identify.identified_runtime(self._job("tt9999001")))
        get.assert_called_once()

    def test_unknown_to_tmdb(self):
        with self._get(self._response({"movie_results": [], "tv_results": []})) as get:
            self.assertIsNone(identify.identified_runtime(self._job("tt0000000")))
        get.assert_called_once()

    def test_failed_lookups_raise(self):
        failures = {
            "network error on /find": [identify.requests.ConnectionError("no route")],
            "rate limited on /find": [self._response({"status_code": 25}, status=429)],
            "network error on the details": [self._response({"movie_results": [self.RED]}),
                                             identify.requests.Timeout("timed out")],
            "not JSON": [mock.Mock(**{"json.side_effect": ValueError("bad json")})],
        }
        for name, responses in failures.items():
            with self.subTest(name), self._get(*responses):
                with self.assertRaises(identify.RuntimeLookupError):
                    identify.identified_runtime(self._job())

    def test_omdb(self):
        with mock.patch.dict(identify.cfg.arm_config, {"METADATA_PROVIDER": "omdb"}):
            with self._get(self._response({"Response": "True", "Type": "movie", "Runtime": "111 min"})):
                self.assertEqual(identify.identified_runtime(self._job()), 111 * 60)
            with self._get(self._response({"Response": "True", "Type": "series", "Runtime": "90 min"})):
                self.assertIsNone(identify.identified_runtime(self._job()))
            with self._get(self._response({"Response": "False", "Error": "Incorrect IMDb ID."})):
                self.assertIsNone(identify.identified_runtime(self._job()))
            with self._get(self._response({"Response": "False", "Error": "Request limit reached!"})):
                with self.assertRaises(identify.RuntimeLookupError):
                    identify.identified_runtime(self._job())


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestRetitleFinalDirectory(unittest.TestCase):
    """The completed folder after check_runtime (or a title set by hand) changed the job's title."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="completed_", dir=_TMP)
        self.movies = os.path.join(self.root, "movies")
        os.makedirs(self.movies)
        for patch in (mock.patch.dict(arm_ripper.utils.cfg.arm_config, {"ALLOW_DUPLICATES": True}),
                      mock.patch.object(arm_ripper.utils, "database_updater",
                                        side_effect=lambda args, job: vars(job).update(args))):
            patch.start()
            self.addCleanup(patch.stop)

    def _job(self):
        job = _job("RED", "Red", "2017", RED_2017_SHORT["imdbID"], [])
        job.config.COMPLETED_PATH = self.root
        job.stage = "170001"
        job.path = None
        return job

    def _first_folder(self, poster=True):
        folder = os.path.join(self.movies, "Red (2017)")
        os.makedirs(folder)
        if poster:
            Path(folder, "poster.png").write_bytes(b"png")
        return folder

    def test_unchanged_title_keeps_the_folder(self):
        job, folder = self._job(), self._first_folder()
        self.assertEqual(arm_ripper.retitle_final_directory(False, job, folder, "movies", "Red (2017)"), folder)
        self.assertTrue(os.path.isfile(os.path.join(folder, "poster.png")))
        self.assertIsNone(job.path)

    def test_new_title_moves_the_poster_and_removes_the_first_folder(self):
        job, folder = self._job(), self._first_folder()
        job.title, job.year = "RED", "2010"
        with mock.patch.dict(arm_ripper.utils.cfg.arm_config, {"DELRAWFILES": False}):
            new = arm_ripper.retitle_final_directory(False, job, folder, "movies", "Red (2017)")
        self.assertEqual(new, os.path.join(self.movies, "RED (2010)"))
        self.assertEqual(job.path, new)
        self.assertFalse(os.path.exists(folder))
        self.assertTrue(os.path.isfile(os.path.join(new, "poster.png")))

    def test_film_ripped_before_keeps_its_folder(self):
        existing = os.path.join(self.movies, "RED (2010)")
        os.makedirs(existing)
        Path(existing, "RED (2010).mkv").write_bytes(b"old rip")
        job, folder = self._job(), self._first_folder(poster=False)
        job.title, job.year = "RED", "2010"
        new = arm_ripper.retitle_final_directory(False, job, folder, "movies", "Red (2017)")
        self.assertEqual(new, existing + "_170001")
        self.assertTrue(os.path.isdir(new))
        self.assertEqual(os.listdir(existing), ["RED (2010).mkv"])
        self.assertFalse(os.path.exists(folder))

    def test_duplicate_rips_disabled_stops_at_the_existing_folder(self):
        os.makedirs(os.path.join(self.movies, "RED (2010)"))
        job, folder = self._job(), self._first_folder()
        job.title, job.year = "RED", "2010"
        with mock.patch.dict(arm_ripper.utils.cfg.arm_config, {"ALLOW_DUPLICATES": False}), \
                mock.patch.object(arm_ripper.utils, "notify"):
            with self.assertRaises(arm_ripper.utils.RipperException):
                arm_ripper.retitle_final_directory(True, job, folder, "movies", "Red (2017)")

    def test_folder_with_other_files_is_left(self):
        job, folder = self._job(), self._first_folder()
        Path(folder, "something.mkv").write_bytes(b"")
        job.title, job.year = "RED", "2010"
        with self.assertLogs(level="WARNING"):
            arm_ripper.retitle_final_directory(False, job, folder, "movies", "Red (2017)")
        self.assertEqual(os.listdir(folder), ["something.mkv"])

    def test_failed_poster_move_leaves_the_first_folder(self):
        job, folder = self._job(), self._first_folder()
        job.title, job.year = "RED", "2010"
        with mock.patch.object(arm_ripper.shutil, "move", side_effect=OSError("read-only file system")), \
                self.assertLogs(level="WARNING"):
            new = arm_ripper.retitle_final_directory(False, job, folder, "movies", "Red (2017)")
        self.assertEqual(new, os.path.join(self.movies, "RED (2010)"))
        self.assertTrue(os.path.isfile(os.path.join(folder, "poster.png")))

    def test_rip_visual_media_files_the_switched_film_in_its_own_folder(self):
        # RED (2010) was ripped before; this disc is taken for Red (2017) until check_runtime runs
        existing = os.path.join(self.movies, "RED (2010)")
        os.makedirs(existing)
        Path(existing, "RED (2010).mkv").write_bytes(b"old rip")
        job = self._job()
        job.config = SimpleNamespace(GET_VIDEO_TITLE=True, COMPLETED_PATH=self.root,
                                     TRANSCODE_PATH=os.path.join(self.root, "transcode"),
                                     NOTIFY_RIP=False, SKIP_TRANSCODE=False)
        job.devpath, job.status = "/dev/sr0", None
        filed_in = []

        def switch(job):
            job.title, job.year = "RED", "2010"

        def poster(folder, job):
            Path(folder, "poster.png").write_bytes(b"png")

        patches = [mock.patch.object(arm_ripper, "db"),
                   mock.patch.object(arm_ripper, "rip_with_mkv", return_value=True),
                   mock.patch.object(arm_ripper.makemkv, "makemkv", return_value=os.path.join(self.root, "raw")),
                   mock.patch.object(arm_ripper, "check_runtime", side_effect=switch),
                   mock.patch.object(arm_ripper, "start_transcode"),
                   mock.patch.object(arm_ripper, "move_files_post",
                                     side_effect=lambda path, job, *_: filed_in.append(job.path)),
                   mock.patch.object(arm_ripper, "notify_exit"),
                   mock.patch.object(arm_ripper.utils, "save_disc_poster", side_effect=poster),
                   mock.patch.dict(arm_ripper.utils.cfg.arm_config, {"DELRAWFILES": False})]
        patches += [mock.patch.object(arm_ripper.utils, name) for name in
                    ("copy_disc_photo", "copy_disc_barcode", "move_movie_poster", "scan_emby", "set_permissions")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        arm_ripper.rip_visual_media(False, job, None, 0)
        self.assertEqual(filed_in, [existing + "_170001"])
        self.assertEqual(os.listdir(existing), ["RED (2010).mkv"])
        self.assertFalse(os.path.exists(os.path.join(self.movies, "Red (2017)")))
        self.assertTrue(os.path.isfile(os.path.join(existing + "_170001", "poster.png")))


if __name__ == '__main__':
    unittest.main()
