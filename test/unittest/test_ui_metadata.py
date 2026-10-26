"""
Tests for the metadata search URLs in arm.ui.metadata with the network mocked.

Titles taken from a case barcode can hold "&", "#", "+" or "?" (job 36 on the ts140 was
"Thomas & Friends - Best of Gordon"). Interpolated raw into the URL the "&" ended the query at
"Thomas ", so the exact-title rule found nothing and the disc label GORDON matched the film
Gordon (2003). These tests check every search URL carries the whole title as one encoded
parameter.

Importing ``arm.ui`` pulls in the whole of ARM (config, Flask app, database), so a throwaway
arm.yaml is written first and pointed at with ARM_CONFIG_FILE, like test_ripper_identify_barcode.
Needs the Linux dependencies ARM itself needs (libdiscid, pyudev); skipped when they are missing.
"""
import importlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import requests

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
                  "DISABLE_LOGIN: true\nMETADATA_PROVIDER: \"tmdb\"\n"
                  "TMDB_API_KEY: \"tmdb-test-key\"\nOMDB_API_KEY: \"omdb-test-key\"\n")
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


TITLE = "Thomas & Friends - Best of Gordon"
NO_RESULTS = {"page": 1, "results": [], "total_pages": 0, "total_results": 0}
ONE_RESULT = {"page": 1, "total_pages": 1, "total_results": 1,
              "results": [{"id": 313746, "title": "Thomas & Friends: Best of Gordon", "release_date": "2003-01-01",
                           "poster_path": "/p.jpg", "backdrop_path": "/b.jpg", "overview": "Gordon's best"}]}


def _response(payload):
    return SimpleNamespace(text=json.dumps(payload), status_code=200)


def _sent_query(call):
    """The query string TMDB would receive for a mocked requests.get call, as parse_qs sees it"""
    url = call.args[0]
    prepared = requests.Request("GET", url, params=call.kwargs.get("params")).prepare()
    split = urlsplit(prepared.url)
    return split.scheme + "://" + split.netloc + split.path, parse_qs(split.query, keep_blank_values=True)


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestTmdbSearchUrls(unittest.TestCase):
    def setUp(self):
        cfg.arm_config["TMDB_API_KEY"] = "tmdb-test-key"
        cfg.arm_config["OMDB_API_KEY"] = "omdb-test-key"

    def test_ampersand_in_the_title_reaches_tmdb_as_one_query_parameter(self):
        with mock.patch.object(metadata.requests, "get", return_value=_response(ONE_RESULT)) as get:
            results, poster_base, response = metadata.tmdb_fetch_results(TITLE, None, "tmdb-test-key")
        self.assertEqual(results["total_results"], 1)
        self.assertEqual(poster_base, "https://image.tmdb.org/t/p/original")
        get.assert_called_once()
        endpoint, query = _sent_query(get.call_args)
        self.assertEqual(endpoint, "https://api.themoviedb.org/3/search/movie")
        self.assertEqual(query["query"], [TITLE])
        self.assertEqual(query["api_key"], ["tmdb-test-key"])
        self.assertNotIn("year", query)
        # the raw URL used to end the query at the ampersand, leaving "Thomas " and a stray parameter
        self.assertNotIn("Friends - Best of Gordon", query)
        self.assertNotIn(" Friends - Best of Gordon", query)
        self.assertIn("query=Thomas+%26+Friends+-+Best+of+Gordon", requests.Request(
            "GET", get.call_args.args[0], params=get.call_args.kwargs.get("params")).prepare().url)

    def test_year_is_sent_as_its_own_parameter(self):
        with mock.patch.object(metadata.requests, "get", return_value=_response(ONE_RESULT)) as get:
            metadata.tmdb_fetch_results("Who Framed Roger Rabbit?", "1988", "tmdb-test-key")
        endpoint, query = _sent_query(get.call_args)
        self.assertEqual(endpoint, "https://api.themoviedb.org/3/search/movie")
        self.assertEqual(query["query"], ["Who Framed Roger Rabbit?"])
        self.assertEqual(query["year"], ["1988"])

    def test_tv_fallback_in_tmdb_search_encodes_the_title(self):
        with mock.patch.object(metadata.requests, "get",
                               side_effect=[_response(NO_RESULTS), _response(NO_RESULTS)]) as get:
            self.assertIsNone(metadata.tmdb_search(TITLE))
        self.assertEqual(get.call_count, 2)
        endpoint, query = _sent_query(get.call_args_list[1])
        self.assertEqual(endpoint, "https://api.themoviedb.org/3/search/tv")
        self.assertEqual(query["query"], [TITLE])
        self.assertEqual(query["api_key"], ["tmdb-test-key"])

    def test_tv_fallback_in_get_tmdb_poster_encodes_the_title(self):
        with mock.patch.object(metadata.requests, "get",
                               side_effect=[_response(NO_RESULTS), _response(NO_RESULTS)]) as get:
            self.assertIsNone(metadata.get_tmdb_poster("Tom & Jerry", "2021"))
        self.assertEqual(get.call_count, 2)
        endpoint, query = _sent_query(get.call_args_list[0])
        self.assertEqual((endpoint, query["query"], query["year"]),
                         ("https://api.themoviedb.org/3/search/movie", ["Tom & Jerry"], ["2021"]))
        endpoint, query = _sent_query(get.call_args_list[1])
        self.assertEqual((endpoint, query["query"]), ("https://api.themoviedb.org/3/search/tv", ["Tom & Jerry"]))


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestOmdbSearchUrls(unittest.TestCase):
    def setUp(self):
        cfg.arm_config["OMDB_API_KEY"] = "omdb-test-key"

    def test_poster_search_encodes_the_title(self):
        payload = {"Search": [{"Poster": "https://img/p.jpg", "imdbID": "tt0000001"}]}
        with mock.patch.object(metadata.urllib.request, "urlopen",
                               return_value=io.BytesIO(json.dumps(payload).encode())) as urlopen:
            self.assertEqual(metadata.get_omdb_poster(TITLE, "2004"), ("https://img/p.jpg", "tt0000001"))
        urlopen.assert_called_once()
        split = urlsplit(urlopen.call_args.args[0])
        query = parse_qs(split.query)
        self.assertEqual(split.netloc + split.path, "www.omdbapi.com/")
        self.assertEqual(query["s"], [TITLE])
        self.assertEqual(query["y"], ["2004"])
        self.assertEqual(query["apikey"], ["omdb-test-key"])
        self.assertNotIn(" Friends - Best of Gordon", query)

    def test_title_search_encodes_the_title(self):
        with mock.patch.object(metadata.urllib.request, "urlopen",
                               return_value=io.BytesIO(json.dumps({"Response": "False", "Error": "x"}).encode())) \
                as urlopen:
            self.assertIsNone(metadata.call_omdb_api(title=TITLE, year="2004"))
        query = parse_qs(urlsplit(urlopen.call_args.args[0]).query)
        self.assertEqual(query["s"], [TITLE])
        self.assertEqual(query["y"], ["2004"])


if __name__ == "__main__":
    unittest.main()
