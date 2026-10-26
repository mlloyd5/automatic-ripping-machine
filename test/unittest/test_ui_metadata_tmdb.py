"""
Tests for the TMDB search request: the title must reach TMDB whole, whatever characters it has.

Importing ``arm.ui.metadata`` pulls in the whole of ARM (config, Flask app, database), so a
throwaway arm.yaml is written first and pointed at with ARM_CONFIG_FILE, as the other ripper
tests do. Needs the Linux dependencies ARM itself needs; skipped when they are missing.
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

_TMP = tempfile.mkdtemp(prefix="arm_metadata_test_")
_CONFIG = os.path.join(_TMP, "arm.yaml")
with open(_CONFIG, "w") as _handle:
    _handle.write(f"INSTALLPATH: \"{REPO_ROOT}\"\n"
                  f"DBFILE: \"{_TMP}/arm.db\"\n"
                  f"LOGPATH: \"{_TMP}/logs/\"\n"
                  f"ABCDE_CONFIG_FILE: \"{REPO_ROOT}/setup/.abcde.conf\"\n"
                  f"RAW_PATH: \"{_TMP}/raw/\"\nTRANSCODE_PATH: \"{_TMP}/transcode/\"\n"
                  f"COMPLETED_PATH: \"{_TMP}/completed/\"\n"
                  "DISABLE_LOGIN: true\nMETADATA_PROVIDER: \"tmdb\"\nTMDB_API_KEY: \"testkey\"\n")
os.environ["ARM_CONFIG_FILE"] = _CONFIG

try:
    metadata = importlib.import_module("arm.ui.metadata")
    cfg = importlib.import_module("arm.config.config")
    IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on the host
    metadata = cfg = None
    IMPORT_ERROR = error


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def _response(payload):
    return SimpleNamespace(text=json.dumps(payload), status_code=200)


GORDON = {"page": 1, "total_results": 1, "results": [
    {"id": 1, "title": "Thomas & Friends: Best of Gordon", "release_date": "2003-01-01", "overview": "",
     "poster_path": "/g.jpg", "backdrop_path": None, "vote_count": 1}]}
NOTHING = {"page": 1, "total_results": 0, "results": []}


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestTmdbSearchRequest(unittest.TestCase):
    def setUp(self):
        os.environ["ARM_CONFIG_FILE"] = _CONFIG
        if cfg.arm_config_path != _CONFIG:
            importlib.reload(cfg)
        cfg.arm_config["TMDB_API_KEY"] = "testkey"

    def test_ampersand_in_the_title_reaches_tmdb_whole(self):
        """Job 36 on the ts140: 'Thomas & Friends - Best of Gordon' used to be sent as 'Thomas '."""
        with mock.patch.object(metadata.requests, "get", return_value=_response(GORDON)) as get:
            payload, _, _ = metadata.tmdb_fetch_results("Thomas & Friends - Best of Gordon", None, "testkey")
        get.assert_called_once()
        url, kwargs = get.call_args[0][0], get.call_args[1]
        self.assertEqual(url, "https://api.themoviedb.org/3/search/movie")
        self.assertEqual(kwargs["params"], {"api_key": "testkey", "query": "Thomas & Friends - Best of Gordon"})
        self.assertEqual(payload["results"][0]["title"], "Thomas & Friends: Best of Gordon")

    def test_year_and_other_url_characters(self):
        with mock.patch.object(metadata.requests, "get", return_value=_response(NOTHING)) as get:
            metadata.tmdb_fetch_results("What's Up, Doc? #2", "1972", "testkey")
        self.assertEqual(get.call_args[1]["params"],
                         {"api_key": "testkey", "query": "What's Up, Doc? #2", "year": "1972"})

    def test_plus_signs_are_the_spaces_arm_writes(self):
        """identify.get_video_details sends 'THOMAS+GETS+ALONG'; encoded literally TMDB would see the pluses."""
        with mock.patch.object(metadata.requests, "get", return_value=_response(NOTHING)) as get:
            metadata.tmdb_search_request("movie", "THOMAS+GETS+ALONG", None, "testkey")
        self.assertEqual(get.call_args[1]["params"]["query"], "THOMAS GETS ALONG")

    def test_tv_fallback_is_encoded_the_same_way(self):
        with mock.patch.object(metadata.requests, "get",
                               side_effect=[_response(NOTHING), _response(NOTHING)]) as get:
            self.assertIsNone(metadata.tmdb_search("Tom & Jerry Kids", None))
        self.assertEqual([call[0][0] for call in get.call_args_list],
                         ["https://api.themoviedb.org/3/search/movie", "https://api.themoviedb.org/3/search/tv"])
        self.assertEqual(get.call_args_list[1][1]["params"], {"api_key": "testkey", "query": "Tom & Jerry Kids"})

    def test_tmdb_search_returns_the_arm_shape(self):
        with mock.patch.object(metadata.requests, "get", return_value=_response(GORDON)), \
                mock.patch.object(metadata, "tmdb_get_imdb", return_value="tt0000001"):
            result = metadata.tmdb_search("Thomas & Friends - Best of Gordon", None)
        self.assertEqual(result["Search"][0]["Title"], "Thomas & Friends: Best of Gordon")
        self.assertEqual(result["Search"][0]["imdbID"], "tt0000001")
        self.assertEqual(result["Search"][0]["Year"], "2003")


if __name__ == "__main__":
    unittest.main()
