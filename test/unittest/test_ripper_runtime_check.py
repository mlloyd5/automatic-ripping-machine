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
                           title_manual=None, tracks=tracks, films=[])


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
            "config": mock.patch.dict(arm_ripper.cfg.arm_config, {"GET_VIDEO_TITLE": True}),
        }
        self.mocks = {name: patch.start() for name, patch in patches.items()}
        for patch in patches.values():
            self.addCleanup(patch.stop)

    def test_wrong_film_of_the_same_title_is_replaced(self):
        self.mocks["candidates"].return_value = [RED_2010]
        job = _job("RED", "Red", "2017", RED_2017_SHORT["imdbID"], self.RED_DISC)
        self.assertTrue(arm_ripper.check_runtime(job))
        self.mocks["apply"].assert_called_once_with(job, RED_2010)
        self.mocks["notify"].assert_not_called()

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
        self.mocks["notify"].assert_not_called()

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

    def test_unknown_runtime_is_not_checked(self):
        job = _job("RED", "Red", "2017", "tt-unknown", self.RED_DISC)
        self.assertFalse(arm_ripper.check_runtime(job))
        self.mocks["candidates"].assert_not_called()

    def test_disabled_title_lookup_is_not_checked(self):
        with mock.patch.dict(arm_ripper.cfg.arm_config, {"GET_VIDEO_TITLE": False}):
            job = _job("RED", "Red", "2017", RED_2017_SHORT["imdbID"], self.RED_DISC)
            self.assertFalse(arm_ripper.check_runtime(job))
        self.mocks["identified"].assert_not_called()


if __name__ == '__main__':
    unittest.main()
