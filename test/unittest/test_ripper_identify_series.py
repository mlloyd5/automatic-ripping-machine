"""
Tests for identify.series_instead: a TMDB film result gives way to the TV entry of the same title
and year when the disc looks like that series' episodes. The case is ts140 job 146, a DVD of The
Amazing World of Dinosaurs (2012): TMDB has a 24 minute film (221236) and a series of 6 episodes
(132011), both first dated 2012-04-03, and the disc has three titles of 22 minutes. TMDB's film
search finds only the film, so the disc was filed as one.

Importing ``arm.ripper.identify`` pulls in the whole of ARM (config, Flask app, database), so a
throwaway arm.yaml is written first and pointed at with ARM_CONFIG_FILE. Needs the Linux
dependencies ARM itself needs (libdiscid, pyudev); skipped when they are missing.
"""
import importlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]

_TMP = tempfile.mkdtemp(prefix="arm_identify_series_test_")
_CONFIG = os.path.join(_TMP, "arm.yaml")
with open(_CONFIG, "w") as _handle:
    _handle.write(f"INSTALLPATH: \"{REPO_ROOT}\"\n"
                  f"DBFILE: \"{_TMP}/arm.db\"\n"
                  f"LOGPATH: \"{_TMP}/logs/\"\n"
                  f"ABCDE_CONFIG_FILE: \"{REPO_ROOT}/setup/.abcde.conf\"\n"
                  f"RAW_PATH: \"{_TMP}/raw/\"\nTRANSCODE_PATH: \"{_TMP}/transcode/\"\n"
                  f"COMPLETED_PATH: \"{_TMP}/completed/\"\n"
                  f"DISC_PHOTO_PATH: \"{_TMP}/photos/\"\n"
                  "DISABLE_LOGIN: true\nMETADATA_PROVIDER: \"tmdb\"\nTMDB_API_KEY: \"testkey\"\n"
                  "DISC_PHOTO_VISION_URL: \"\"\n")
os.environ["ARM_CONFIG_FILE"] = _CONFIG
os.makedirs(os.path.join(_TMP, "photos"), exist_ok=True)
with open(os.path.join(_TMP, "photos", "sr1_20261027-120000.jpg"), "wb") as _handle:
    _handle.write(b"\xff\xd8 not really a jpeg")

try:
    identify = importlib.import_module("arm.ripper.identify")
    metadata = importlib.import_module("arm.ui.metadata")
    cfg = importlib.import_module("arm.config.config")
    IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on the host
    identify = metadata = cfg = None
    IMPORT_ERROR = error


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def _job(**overrides):
    base = dict(job_id=146, disctype="dvd", devpath="/dev/sr1", hasnicetitle=False, title="not identified",
                year="", label="AMAZING_WORLD_DINOSAURS", disc_barcode=None, disc_photo=None,
                disc_photo_vision=None, video_type=None, imdb_id=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_updater(args, job):
    for key, value in args.items():
        setattr(job, key, value)
    return True


def _lsdvd(*lengths):
    """lsdvd -Oy output as it prints it: one line per key, so identify's regex strips only "lsdvd = "."""
    tracks = ",\n".join(f"    {{\n      'ix' : {number},\n      'length' : {length:.3f},\n      'vts' : 1,\n    }}"
                        for number, length in enumerate(lengths, start=1))
    return (f"lsdvd = {{\n  'device' : '/dev/sr1',\n  'title' : 'AMAZING_WORLD_DINOSAURS',\n"
            f"  'track' : [\n{tracks}\n  ],\n}}\n")


# ts140 job 146: three episodes and a few seconds of menu
LSDVD_146 = _lsdvd(1319.04, 1318.2, 1317.6, 21.5)

DINOSAURS_FILM = {"Title": "The Amazing World of Dinosaurs", "Year": "2012", "Type": "movie", "id": 221236,
                  "imdbID": "tt2402123", "Poster": "https://img/film.jpg", "vote_count": 2}
DINOSAURS_SERIES = {"Title": "The Amazing World of Dinosaurs", "Year": "2012", "Type": "series", "id": 132011,
                    "imdbID": "tt2185012", "Poster": "https://img/series.jpg", "vote_count": 1}
# TMDB's tv search for the title, raw: the series, a namesake from another year and another series
TV_RAW = [{"id": 132011, "name": "The Amazing World of Dinosaurs", "first_air_date": "2012-04-03"},
          {"id": 9001, "name": "The Amazing World of Dinosaurs", "first_air_date": "2019-06-01"},
          {"id": 2342, "name": "Dinosaurs", "first_air_date": "1991-04-26"}]
SERIES_DETAILS = {"episode_runtimes": [], "episodes": 6}


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestLooksLikeEpisodes(unittest.TestCase):
    def test_job_146_three_titles_of_a_short_film(self):
        """No episode runtime on TMDB: the 24 minute film is far shorter than the three titles together."""
        self.assertTrue(identify.looks_like_episodes([1319, 1318, 1317, 21], 24 * 60, [], 6))

    def test_titles_that_run_as_long_as_the_series_episodes(self):
        """TMDB's 30 minute slot for 22 minute episodes fits; the film's runtime is not needed."""
        self.assertTrue(identify.looks_like_episodes([1320, 1325, 1310, 1330], None, [30 * 60], 4))

    def test_a_film_with_extras_stays_a_film(self):
        """Three 20 minute featurettes run as long as the series' episodes, but the film is on the disc."""
        self.assertFalse(identify.looks_like_episodes([6300, 1200, 1210, 1190, 120], 104 * 60, [20 * 60], 3))

    def test_two_cuts_of_a_film_are_no_episodes(self):
        self.assertFalse(identify.looks_like_episodes([6000, 6500, 300], 100 * 60, [], 8))
        self.assertFalse(identify.looks_like_episodes([6000, 6500, 300], None, [], 8))

    def test_titles_of_different_lengths_are_no_episodes(self):
        self.assertFalse(identify.looks_like_episodes([1319, 2700, 600], 24 * 60, [], 6))

    def test_alike_titles_with_nothing_to_compare_them_to_stay_a_film(self):
        self.assertFalse(identify.looks_like_episodes([1319, 1318], None, [], 6))
        self.assertFalse(identify.looks_like_episodes([1319, 1318], 50 * 60, [], 6))

    def test_without_title_lengths_only_a_short_film_beside_a_series_gives_way(self):
        self.assertTrue(identify.looks_like_episodes([], 24 * 60, [], 6))
        self.assertFalse(identify.looks_like_episodes([], 90 * 60, [], 6))
        self.assertFalse(identify.looks_like_episodes([], None, [], 6))
        self.assertFalse(identify.looks_like_episodes([], 24 * 60, [], 1))
        self.assertFalse(identify.looks_like_episodes([], 24 * 60, [], None))

    def test_episode_titles_takes_the_largest_alike_group(self):
        self.assertEqual(identify.episode_titles([2700, 1317, 1319, 1318, 2650, 200]), [1317, 1318, 1319])
        self.assertEqual(identify.episode_titles([1319, 2700]), [])


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestSeriesInstead(unittest.TestCase):
    def setUp(self):
        os.environ["ARM_CONFIG_FILE"] = _CONFIG
        if cfg.arm_config_path != _CONFIG:
            importlib.reload(cfg)
        cfg.arm_config["METADATA_PROVIDER"] = "tmdb"
        self.tv_searched = []
        self.lsdvd = mock.patch.object(identify, "arm_subprocess", return_value=LSDVD_146)
        self.patches = [
            mock.patch.object(identify.utils, "database_updater", side_effect=_fake_updater),
            mock.patch.object(identify.ui_metadata, "tmdb_tv_search", side_effect=self._tv_search),
            mock.patch.object(identify.ui_metadata, "tmdb_tv_details", return_value=dict(SERIES_DETAILS)),
            mock.patch.object(identify.ui_metadata, "tmdb_runtime", return_value=24 * 60),
        ]
        self.mocks = [patch.start() for patch in self.patches]
        self.run = self.lsdvd.start()

    def tearDown(self):
        self.lsdvd.stop()
        for patch in self.patches:
            patch.stop()

    def _tv_search(self, title, wanted=None):
        """tmdb_tv_search over TV_RAW: only the entries ``wanted`` keeps, in OMDB style."""
        self.tv_searched.append(title)
        return [dict(DINOSAURS_SERIES, id=raw["id"], Year=raw["first_air_date"][:4])
                for raw in TV_RAW if wanted is None or wanted(raw)]

    def test_job_146_dvd_is_taken_as_the_series(self):
        job = _job()
        identify.apply_search_result(job, dict(DINOSAURS_FILM))
        self.assertEqual(self.tv_searched, ["The Amazing World of Dinosaurs"])
        self.assertEqual((job.video_type, job.video_type_auto), ("series", "series"))
        self.assertEqual((job.imdb_id, job.year, job.hasnicetitle), ("tt2185012", "2012", True))
        self.assertEqual(job.poster_url, "https://img/series.jpg")
        self.mocks[2].assert_called_once_with(132011)

    def test_a_film_without_a_release_date_has_no_year_to_match(self):
        """TMDB's film search gives a film without a release date the year "0000", which no series has."""
        job = _job()
        identify.apply_search_result(job, dict(DINOSAURS_FILM, Year="0000"))
        self.assertEqual((job.video_type, job.imdb_id, job.year), ("series", "tt2185012", "2012"))
        self.mocks[2].assert_called_once_with(132011)

    def test_a_series_of_another_year_is_not_the_films_twin(self):
        job = _job()
        identify.apply_search_result(job, dict(DINOSAURS_FILM, Year="2015"))
        self.assertEqual(job.video_type, "movie")
        self.mocks[2].assert_not_called()
        self.run.assert_not_called()

    def test_a_film_with_extras_stays_a_film(self):
        """A 104 minute film with three 20 minute featurettes, and a 20 minute series of the same title and year."""
        self.run.return_value = _lsdvd(6262.1, 1201.0, 1210.5, 1189.9, 95.0)
        self.mocks[3].return_value = 104 * 60
        self.mocks[2].return_value = {"episode_runtimes": [20 * 60], "episodes": 3}
        job = _job()
        identify.apply_search_result(job, dict(DINOSAURS_FILM))
        self.assertEqual((job.video_type, job.imdb_id), ("movie", "tt2402123"))

    def test_bluray_goes_by_the_films_runtime_alone(self):
        """MakeMKV has not listed a Blu-ray's titles yet: the 24 minute film is taken for an episode."""
        job = _job(disctype="bluray")
        identify.apply_search_result(job, dict(DINOSAURS_FILM))
        self.assertEqual(job.video_type, "series")
        self.run.assert_not_called()
        self.mocks[3].return_value = 95 * 60
        job = _job(disctype="bluray")
        identify.apply_search_result(job, dict(DINOSAURS_FILM))
        self.assertEqual(job.video_type, "movie")

    def test_only_tmdb_films_are_checked(self):
        job = _job()
        identify.apply_search_result(job, dict(DINOSAURS_SERIES))
        identify.apply_search_result(job, {k: v for k, v in DINOSAURS_FILM.items() if k != "id"})
        cfg.arm_config["METADATA_PROVIDER"] = "omdb"
        try:
            identify.apply_search_result(job, dict(DINOSAURS_FILM))
        finally:
            cfg.arm_config["METADATA_PROVIDER"] = "tmdb"
        self.assertEqual(self.tv_searched, [])
        self.assertEqual(job.video_type, "movie")

    def test_check_runtime_takes_the_film_as_it_is(self):
        job = _job()
        identify.apply_search_result(job, dict(DINOSAURS_FILM), check_series=False)
        self.assertEqual((job.video_type, self.tv_searched), ("movie", []))

    def test_a_failed_lookup_keeps_the_film(self):
        self.mocks[1].side_effect = metadata.requests.ConnectionError("no route to host")
        job = _job()
        identify.apply_search_result(job, dict(DINOSAURS_FILM))
        self.assertEqual(job.video_type, "movie")

    def test_job_146_through_the_vision_model(self):
        """The read title finds the film; the disc's titles make it the series."""
        cfg.arm_config.update({"DISC_PHOTO_VISION_URL": "http://llm:11434", "DISC_PHOTO_VISION_MODEL": "qwen2.5vl:7b"})
        answer = {"title": "The Amazing World of Dinosaurs", "subtitle": None, "year": "2012", "studio": None,
                  "disc_number": "1", "catalog_number": None, "format": "DVD", "other_text": None,
                  "confidence": 0.95, "rotation": 0}
        job = _job(disc_photo="sr1_20261027-120000.jpg")
        try:
            with mock.patch.object(identify.disc_vision, "read_disc_photo", return_value=answer), \
                    mock.patch.object(identify, "_provider_search", return_value={"Search": [dict(DINOSAURS_FILM)]}):
                self.assertTrue(identify.identify_from_photo_vision(job))
        finally:
            cfg.arm_config["DISC_PHOTO_VISION_URL"] = ""
        self.assertEqual((job.video_type, job.imdb_id, job.year), ("series", "tt2185012", "2012"))


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestIdentifyDvdTracks(unittest.TestCase):
    """
    identify_dvd for job 146, its label search finding the film: the series check and the track 99
    detection share one lsdvd run. The CRC64 hash, the network, lsdvd and the database are stand-ins.
    """

    def setUp(self):
        os.environ["ARM_CONFIG_FILE"] = _CONFIG
        if cfg.arm_config_path != _CONFIG:
            importlib.reload(cfg)
        self.prevent_99 = cfg.arm_config.get("PREVENT_99")
        cfg.arm_config.update({"METADATA_PROVIDER": "tmdb", "PREVENT_99": False})
        crc_miss = SimpleNamespace(read=lambda: b'{"success": false, "mode": "s", "results": {}}')
        self.patches = [
            mock.patch.object(identify.utils, "database_updater", side_effect=_fake_updater),
            mock.patch.object(identify.pydvdid, "compute", return_value="7f3d5c9a1b2e4d60"),
            mock.patch.object(identify.urllib.request, "urlopen", return_value=crc_miss),
            mock.patch.object(identify.ui_utils, "tmdb_search", return_value={"Search": [dict(DINOSAURS_FILM)]}),
            mock.patch.object(identify.ui_metadata, "tmdb_tv_search", return_value=[dict(DINOSAURS_SERIES)]),
            mock.patch.object(identify.ui_metadata, "tmdb_tv_details", return_value=dict(SERIES_DETAILS)),
            mock.patch.object(identify.ui_metadata, "tmdb_runtime", return_value=24 * 60),
        ]
        for patch in self.patches:
            patch.start()
        self.lsdvd = mock.patch.object(identify, "arm_subprocess", return_value=LSDVD_146)
        self.run = self.lsdvd.start()

    def tearDown(self):
        self.lsdvd.stop()
        for patch in self.patches:
            patch.stop()
        cfg.arm_config["PREVENT_99"] = self.prevent_99

    @staticmethod
    def _dvd_job():
        job = _job(mountpoint="/mnt/dev/sr1", crc_id=None, has_track_99=False)
        job.pretty_table = lambda: ""
        return job

    def _identify_dvd(self):
        job = self._dvd_job()
        self.assertTrue(identify.identify_dvd(job))
        return job

    def test_job_146_series_check_and_track_99_detection_run_lsdvd_once(self):
        job = self._identify_dvd()
        self.assertEqual((job.video_type, job.imdb_id, job.has_track_99), ("series", "tt2185012", False))
        self.run.assert_called_once_with(["lsdvd", "-Oy", "/dev/sr1"])

    def test_track_99_is_found_in_the_same_lsdvd_run(self):
        self.run.return_value = _lsdvd(*[1319.0] * 99)
        job = self._identify_dvd()
        self.assertTrue(job.has_track_99)
        self.run.assert_called_once_with(["lsdvd", "-Oy", "/dev/sr1"])

    def test_prevent_99_stops_a_disc_with_99_titles(self):
        cfg.arm_config["PREVENT_99"] = True
        self.run.return_value = _lsdvd(*[1319.0] * 99)
        job = self._dvd_job()
        with self.assertRaisesRegex(identify.utils.RipperException, "PREVENT_99"):
            identify.identify_dvd(job)
        self.assertTrue(job.has_track_99)

    def test_unparsable_lsdvd_output_is_no_titles(self):
        """A DVD lsdvd could not read is judged like a Blu-ray, and the log says why the titles are unknown."""
        self.run.return_value = "lsdvd = {\n  'track' : [ { 'ix' : 1, 'length' : nan } ],\n}\n"
        with self.assertLogs(level="INFO") as logs:
            job = self._identify_dvd()
        self.assertEqual((job.video_type, job.has_track_99), ("series", False))
        self.assertTrue(any("lsdvd listed none" in line for line in logs.output), logs.output)
        self.run.assert_called_once()

    def test_the_lsdvd_titles_stay_out_of_the_jobs_dumps(self):
        """pretty_table is logged at every step: it would carry every title's audio, chapter and cell lists."""
        with mock.patch.object(identify.Job, "parse_udev"), mock.patch.object(identify.Job, "get_pid"):
            job = identify.Job("/dev/sr1")
        job.disctype = "dvd"
        self.assertEqual(len(identify.lsdvd_tracks(job)), 4)
        self.assertNotIn("_lsdvd_tracks", job.get_d())
        self.assertEqual(job.get_d()["devpath"], "/dev/sr1")
        self.assertNotIn("_lsdvd_tracks", job.pretty_table())
        self.assertNotIn("1319.04", job.pretty_table())
        self.assertIn("/dev/sr1", job.pretty_table())


def _response(payload):
    return SimpleNamespace(text=json.dumps(payload), status_code=200)


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestTmdbTv(unittest.TestCase):
    def setUp(self):
        os.environ["ARM_CONFIG_FILE"] = _CONFIG
        if cfg.arm_config_path != _CONFIG:
            importlib.reload(cfg)
        cfg.arm_config["TMDB_API_KEY"] = "testkey"

    def test_tv_search_keeps_the_wanted_entries_with_their_tv_imdb_ids(self):
        """A TV id asked for as a film would be another film's; only TV external ids are asked."""
        raw = [dict(entry, overview="", poster_path="/p.jpg", backdrop_path=None) for entry in TV_RAW]
        answers = [_response({"page": 1, "total_results": 3, "results": raw}), _response({"imdb_id": "tt2185012"})]
        with mock.patch.object(metadata.requests, "get", side_effect=answers) as get:
            found = metadata.tmdb_tv_search("The Amazing World of Dinosaurs",
                                            lambda result: result["first_air_date"].startswith("2012"))
        self.assertEqual([(r["Title"], r["Year"], r["Type"], r["imdbID"]) for r in found],
                         [("The Amazing World of Dinosaurs", "2012", "series", "tt2185012")])
        self.assertIn("/search/tv", get.call_args_list[0].args[0])
        self.assertIn("/tv/132011/external_ids", get.call_args_list[1].args[0])
        self.assertEqual(get.call_count, 2)

    def test_imdb_lookups_give_up_after_30_seconds(self):
        """The series check asks for each twin's IMDb id: a stalled TMDB must not hold up the ripper."""
        answers = [_response({"status_code": 34}), _response({"imdb_id": "tt2402123"})]
        with mock.patch.object(metadata.requests, "get", side_effect=answers) as get:
            self.assertEqual(metadata.tmdb_get_imdb(221236), "tt2402123")
        self.assertEqual([call.kwargs.get("timeout") for call in get.call_args_list], [30, 30])

    def test_tv_search_failure_is_no_entries(self):
        with mock.patch.object(metadata.requests, "get", side_effect=metadata.requests.Timeout("slow")):
            self.assertEqual(metadata.tmdb_tv_search("The Amazing World of Dinosaurs"), [])

    def test_tv_details_falls_back_to_the_latest_episode_runtime(self):
        details = {"id": 132011, "episode_run_time": [], "number_of_episodes": 6,
                   "last_episode_to_air": {"runtime": 23}, "next_episode_to_air": None}
        with mock.patch.object(metadata.requests, "get", return_value=_response(details)):
            self.assertEqual(metadata.tmdb_tv_details(132011), {"episode_runtimes": [23 * 60], "episodes": 6})
        with mock.patch.object(metadata.requests, "get", return_value=_response({"status_code": 34})):
            self.assertIsNone(metadata.tmdb_tv_details(132011))


if __name__ == "__main__":
    unittest.main()
