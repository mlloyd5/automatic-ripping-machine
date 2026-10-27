"""
Tests for identify.identify_loop's fallback searches against a TMDB that cannot be reached.

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

_TMP = tempfile.mkdtemp(prefix="arm_identify_loop_test_")
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
    base = dict(job_id=7, disctype="bluray", title="THE LORD OF THE RINGS", year="2001", label="LOTR")
    base.update(overrides)
    return SimpleNamespace(**base)


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestIdentifyLoopWithTmdbDown(unittest.TestCase):
    def setUp(self):
        os.environ["ARM_CONFIG_FILE"] = _CONFIG
        if cfg.arm_config_path != _CONFIG:
            importlib.reload(cfg)
        cfg.arm_config["METADATA_PROVIDER"] = "tmdb"
        cfg.arm_config["TMDB_API_KEY"] = "testkey"

    def test_identify_loop_stops_after_one_request_when_tmdb_times_out(self):
        """Without stopping, this title's fallbacks made 11 requests, each waiting out the 30 s timeout."""
        with mock.patch.object(metadata.requests, "get", side_effect=metadata.requests.Timeout("timed out")) as get:
            with self.assertRaises(metadata.MetadataUnavailable):
                identify.identify_loop(_job(), None, "THE+LORD+OF+THE+RINGS", "2001")
        self.assertEqual(get.call_count, 1)

    def test_connection_error_also_stops_the_loop(self):
        with mock.patch.object(metadata.requests, "get",
                               side_effect=metadata.requests.ConnectionError("refused")) as get:
            with self.assertRaises(metadata.MetadataUnavailable):
                identify.identify_loop(_job(), None, "THOMAS+GETS+ALONG", "")
        self.assertEqual(get.call_count, 1)

    def test_get_video_details_carries_on_after_one_request(self):
        job = _job()
        with mock.patch.object(metadata.requests, "get", side_effect=metadata.requests.Timeout("timed out")) as get:
            identify.get_video_details(job)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(job.title, "THE LORD OF THE RINGS")

    def test_a_tmdb_error_answer_still_tries_the_fallbacks(self):
        """TMDB answering with its own error (say a bad key) is not a transport failure; the loop goes on."""
        answer = SimpleNamespace(text=json.dumps({"status_code": 7, "status_message": "Invalid API key"}),
                                 status_code=401)
        with mock.patch.object(metadata.requests, "get", return_value=answer) as get:
            identify.identify_loop(_job(), None, "THOMAS+GETS+ALONG", "")
        self.assertGreater(get.call_count, 1)

    def test_ui_search_still_gives_no_results(self):
        with mock.patch.object(metadata.requests, "get", side_effect=metadata.requests.Timeout("timed out")):
            self.assertIsNone(metadata.tmdb_search("THE LORD OF THE RINGS", "2001"))


if __name__ == "__main__":
    unittest.main()
