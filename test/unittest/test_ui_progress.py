"""
Tests for the Active Rips progress and ETA calculation from a MakeMKV progress file (arm/ui/json_api).
Like test_ui_boxes.py this builds a throwaway config and SQLite database under a temp folder and
skips itself when the ARM package cannot be imported on this host.
"""
import atexit
import importlib
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_ORIGINAL_CONFIG_FILE = os.environ.get("ARM_CONFIG_FILE")
_TEMP_ROOT = tempfile.mkdtemp(prefix="arm-ui-progress-test-")
for _sub in ("logs/progress", "db"):
    os.makedirs(os.path.join(_TEMP_ROOT, _sub))
with open(REPO_ROOT / "setup" / "arm.yaml") as _handle:
    _template = yaml.safe_load(_handle)
_template.update({
    "INSTALLPATH": str(REPO_ROOT),
    "DBFILE": os.path.join(_TEMP_ROOT, "db", "arm.db"),
    "LOGPATH": os.path.join(_TEMP_ROOT, "logs") + "/",
    "ABCDE_CONFIG_FILE": str(REPO_ROOT / "setup" / ".abcde.conf"),
    "DISABLE_LOGIN": True,
})
_CONFIG_PATH = os.path.join(_TEMP_ROOT, "arm.yaml")
with open(_CONFIG_PATH, "w") as _handle:
    yaml.safe_dump(_template, _handle)
os.environ["ARM_CONFIG_FILE"] = _CONFIG_PATH

try:
    import arm.config.config as cfg
    from arm.ui import app, db
    import arm.ui.routes  # noqa: F401  (creates the database like runui.py does)
    from arm.ui import json_api
    from arm.models.job import Job
    IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on the host
    IMPORT_ERROR = error

atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)


def tearDownModule():
    if _ORIGINAL_CONFIG_FILE is None:
        os.environ.pop("ARM_CONFIG_FILE", None)
    else:
        os.environ["ARM_CONFIG_FILE"] = _ORIGINAL_CONFIG_FILE


PROGRESS = """PRGT:5018,0,"Scanning CD-ROM devices"
PRGC:5018,0,"Scanning CD-ROM devices"
PRGV:0,0,65536
PRGC:5017,0,"Saving to MKV file"
PRGV:32768,32768,65536
"""


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestMakeMkvProgress(unittest.TestCase):
    def setUp(self):
        os.environ["ARM_CONFIG_FILE"] = _CONFIG_PATH
        if cfg.arm_config_path != _CONFIG_PATH:
            importlib.reload(cfg)
        self.ctx = app.app_context()
        self.ctx.push()
        job = Job.__mapper__.class_manager.new_instance()
        for key, value in dict(devpath="/dev/sr1", status="ripping", disctype="dvd", title="Jaws", year="1975",
                               label="JAWS", start_time=datetime(2026, 10, 25, 23, 0), no_of_titles=2,
                               hasnicetitle=True, ejected=False, updated=False, video_type="movie").items():
            setattr(job, key, value)
        db.session.add(job)
        db.session.commit()
        self.job = job
        self.progress_dir = os.path.join(cfg.arm_config["LOGPATH"], "progress")
        with open(os.path.join(self.progress_dir, f"{job.job_id}.log"), "w") as handle:
            handle.write(PROGRESS)

    def tearDown(self):
        db.session.delete(self.job)
        db.session.commit()
        self.ctx.pop()

    def test_batch_info_is_created_on_the_first_poll_and_the_eta_follows(self):
        batch_file = os.path.join(self.progress_dir, f"{self.job.job_id}.log.batchinfo")
        self.assertFalse(os.path.exists(batch_file))

        first = json_api.process_makemkv_logfile(self.job, {})
        self.assertEqual(first["progress"], "50.00")
        self.assertTrue(os.path.exists(batch_file), "the first poll must start the batch info file")
        with open(batch_file) as handle:
            self.assertRegex(handle.read().strip(), r"^BINF:\d{10},1,2,5017$")
        self.assertEqual(self.job.stage, "Track 1/2<br>Saving to MKV file")

        second = json_api.process_makemkv_logfile(self.job, {})
        self.assertEqual(second["progress"], "50.00")
        self.assertNotEqual(self.job.eta, "Unknown")
        self.assertRegex(self.job.eta, r"^\d\dh\d\dm\d\ds$")

    def test_stage_change_appends_a_new_batch_line(self):
        json_api.process_makemkv_logfile(self.job, {})
        with open(os.path.join(self.progress_dir, f"{self.job.job_id}.log"), "a") as handle:
            handle.write('PRGC:3104,0,"Decrypting data"\nPRGV:49152,49152,65536\n')
        json_api.process_makemkv_logfile(self.job, {})
        batch_file = os.path.join(self.progress_dir, f"{self.job.job_id}.log.batchinfo")
        with open(batch_file) as handle:
            lines = [line for line in handle.read().splitlines() if line]
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith(",3104"))
        self.assertEqual(self.job.stage, "Track 1/2<br>Decrypting data")


if __name__ == "__main__":
    unittest.main()
