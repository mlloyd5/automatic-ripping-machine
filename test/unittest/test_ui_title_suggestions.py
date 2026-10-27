"""
Tests for the title suggestions on the Title Search / Custom Title pages (arm/ui/jobs ocr_suggestion)
when the vision model read the disc label.

Importing ``arm.ui`` loads the ARM config, the Flask app and the SQLite database, so this module
writes a throwaway config under a temp folder first and skips itself when the ARM package cannot
be imported, e.g. on a machine without the Linux libraries ARM needs. Run it in the ARM Docker
image or on an ARM install.
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

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

_ORIGINAL_CONFIG_FILE = os.environ.get("ARM_CONFIG_FILE")
_TEMP_ROOT = tempfile.mkdtemp(prefix="arm-ui-titles-test-")
for _sub in ("logs", "db", "photos"):
    os.makedirs(os.path.join(_TEMP_ROOT, _sub))
with open(REPO_ROOT / "setup" / "arm.yaml") as _handle:
    _template = yaml.safe_load(_handle)
_template.update({
    "INSTALLPATH": str(REPO_ROOT),
    "DBFILE": os.path.join(_TEMP_ROOT, "db", "arm.db"),
    "LOGPATH": os.path.join(_TEMP_ROOT, "logs") + "/",
    "ABCDE_CONFIG_FILE": str(REPO_ROOT / "setup" / ".abcde.conf"),
    "DISC_PHOTO_PATH": os.path.join(_TEMP_ROOT, "photos") + "/",
    "DISABLE_LOGIN": True,
})
_CONFIG_PATH = os.path.join(_TEMP_ROOT, "arm.yaml")
with open(_CONFIG_PATH, "w") as _handle:
    yaml.safe_dump(_template, _handle)
os.environ["ARM_CONFIG_FILE"] = _CONFIG_PATH

try:
    import arm.config.config as cfg
    from arm.ui import app, db
    import arm.ui.routes  # noqa: F401  (creates the database and the UI settings row like runui.py does)
    from arm.ui.jobs import jobs
    from arm.models.job import Job
    IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on the host
    IMPORT_ERROR = error

# What qwen2.5vl:7b read off job 34's disc photo on the ts140
TOON_TIME = {"title": "Toon Time", "subtitle": "Ultimate Cartoon Collection", "year": "2011",
             "studio": "Echo Bridge Home Entertainment", "disc_number": None, "catalog_number": "73619",
             "format": "DVD", "other_text": None, "confidence": 0.95, "rotation": 0}


def tearDownModule():
    shutil.rmtree(_TEMP_ROOT, ignore_errors=True)
    if _ORIGINAL_CONFIG_FILE is None:
        os.environ.pop("ARM_CONFIG_FILE", None)
    else:
        os.environ["ARM_CONFIG_FILE"] = _ORIGINAL_CONFIG_FILE


def _job(**overrides):
    base = dict(disc_photo_vision=None, disc_barcode=None, disc_barcode_title=None, disc_photo_ocr=None)
    base.update(overrides)
    return SimpleNamespace(**base)


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM web UI cannot be imported here: {IMPORT_ERROR}")
class TestOcrSuggestion(unittest.TestCase):
    def test_vision_offers_only_the_full_title(self):
        """The ripper named the rip after the title with its subtitle: that leads, the bare title is not offered."""
        ocr = jobs.ocr_suggestion(_job(disc_photo_vision=json.dumps(TOON_TIME)))
        self.assertEqual(ocr["candidates"], ["Toon Time: Ultimate Cartoon Collection"])
        self.assertEqual(ocr["year"], "2011")
        self.assertEqual(ocr["vision"]["titles"][-1], "Toon Time: Ultimate Cartoon Collection")
        self.assertIsNone(ocr["barcode"])

    def test_vision_leads_the_barcode_and_ocr(self):
        job = _job(disc_photo_vision=json.dumps(TOON_TIME), disc_barcode="096009736194",
                   disc_barcode_title="59 Cartoon Classics (DVD)", disc_photo_ocr="TOON TIME\n")
        with mock.patch.object(jobs.disc_photo, "clean_ocr_text", return_value=(["TOON TIME", "CARTOON"], None)):
            ocr = jobs.ocr_suggestion(job)
        self.assertEqual(ocr["candidates"], ["Toon Time: Ultimate Cartoon Collection", "59 Cartoon Classics",
                                             "TOON TIME", "CARTOON"])
        self.assertEqual(ocr["year"], "2011")
        self.assertEqual(ocr["barcode"]["title"], "59 Cartoon Classics")

    def test_vision_without_a_subtitle_or_unreadable(self):
        ocr = jobs.ocr_suggestion(_job(disc_photo_vision=json.dumps(dict(TOON_TIME, subtitle=None, year=None))))
        self.assertEqual((ocr["candidates"], ocr["year"]), (["Toon Time"], None))
        self.assertIsNone(jobs.ocr_suggestion(_job(disc_photo_vision="not json")))
        self.assertIsNone(jobs.ocr_suggestion(None))


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM web UI cannot be imported here: {IMPORT_ERROR}")
class TestTitleSearchPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # another test module may have imported ARM with its own throwaway config: switch to ours
        os.environ["ARM_CONFIG_FILE"] = _CONFIG_PATH
        if cfg.arm_config_path != _CONFIG_PATH:
            importlib.reload(cfg)
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["LOGIN_DISABLED"] = True
        cls.client = app.test_client()

    def setUp(self):
        with app.app_context(), mock.patch.object(Job, "parse_udev"), mock.patch.object(Job, "get_pid"):
            job = Job("/dev/sr1")
            job.disctype = "dvd"
            job.label = "CARTOONS_D3"
            job.title = job.title_auto = "Toon-Time--Ultimate-Cartoon-Collection"
            job.year = ""
            job.disc_photo_vision = json.dumps(TOON_TIME)
            job.disc_barcode = "096009736194"
            job.disc_barcode_title = "59 Cartoon Classics (DVD)"
            db.session.add(job)
            db.session.commit()
            self.job_id = job.job_id

    def tearDown(self):
        with app.app_context():
            Job.query.filter_by(job_id=self.job_id).delete()
            db.session.commit()

    def test_title_box_is_pre_filled_with_the_name_the_ripper_gave(self):
        html = self.client.get(f"/titlesearch?job_id={self.job_id}").data.decode()
        self.assertIn('name="title" value="Toon Time: Ultimate Cartoon Collection"', html)
        self.assertIn('name="year" value="2011"', html)
        # the Disc label link and the suggestion list offer the full title, the bare one nowhere
        self.assertEqual(html.count('data-title="Toon Time: Ultimate Cartoon Collection"'), 2)
        self.assertNotIn('data-title="Toon Time"', html)
        self.assertIn('data-title="59 Cartoon Classics"', html)
        self.assertIn("95% sure", html)


if __name__ == "__main__":
    unittest.main()
