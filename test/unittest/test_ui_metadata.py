"""
Tests for the OMDb search URLs in arm.ui.metadata with the network mocked.

Titles taken from a case barcode can hold "&", "#", "+" or "?" (job 36 on the ts140 was
"Thomas & Friends - Best of Gordon"). Interpolated raw into the URL the "&" ended the query at
"Thomas ". These tests check the OMDb search URLs carry the whole title as one encoded
parameter. The TMDB searches are covered by test_ui_metadata_tmdb.

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
from unittest import mock
from urllib.parse import parse_qs, urlsplit

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
