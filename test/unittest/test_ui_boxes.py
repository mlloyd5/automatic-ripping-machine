"""
Flask test-client tests for the Boxes page and the sleeve label JSON API (arm/ui/boxes).

Like test_ui_camera.py this builds a throwaway config and SQLite database under a temp folder and
skips itself when the ARM package cannot be imported on this host. Jobs are written straight into
the database (the Job constructor needs udev and a drive); printing is faked.
"""
import atexit
import importlib
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

_ORIGINAL_CONFIG_FILE = os.environ.get("ARM_CONFIG_FILE")
_TEMP_ROOT = tempfile.mkdtemp(prefix="arm-ui-boxes-test-")
for _sub in ("logs", "db"):
    os.makedirs(os.path.join(_TEMP_ROOT, _sub))
with open(REPO_ROOT / "setup" / "arm.yaml") as _handle:
    _template = yaml.safe_load(_handle)
_template.update({
    "INSTALLPATH": str(REPO_ROOT),
    "DBFILE": os.path.join(_TEMP_ROOT, "db", "arm.db"),
    "LOGPATH": os.path.join(_TEMP_ROOT, "logs") + "/",
    "ABCDE_CONFIG_FILE": str(REPO_ROOT / "setup" / ".abcde.conf"),
    "DISABLE_LOGIN": True,
    "LABEL_ENABLED": True,
    "LABEL_ID_PREFIX": "T6",
})
_CONFIG_PATH = os.path.join(_TEMP_ROOT, "arm.yaml")
with open(_CONFIG_PATH, "w") as _handle:
    yaml.safe_dump(_template, _handle)
os.environ["ARM_CONFIG_FILE"] = _CONFIG_PATH

try:
    import arm.config.config as cfg
    from arm.ui import app, db
    import arm.ui.routes  # noqa: F401  (creates the database like runui.py does)
    from arm.models.job import Job
    from arm.labels import sleeve
    IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on the host
    IMPORT_ERROR = error


# The SQLAlchemy engine binds to the DBFILE of whichever test module imports ARM first and keeps it
# for the whole run, so the folder is removed at interpreter exit, not at module teardown
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)


def tearDownModule():
    if _ORIGINAL_CONFIG_FILE is None:
        os.environ.pop("ARM_CONFIG_FILE", None)
    else:
        os.environ["ARM_CONFIG_FILE"] = _ORIGINAL_CONFIG_FILE


def make_job(**fields):
    """A Job row without the constructor (which reads udev and the drive)."""
    job = Job.__mapper__.class_manager.new_instance()
    defaults = dict(devpath="/dev/sr0", status="success", disctype="bluray", title="Jaws", year="1975",
                    label="JAWS", start_time=datetime(2026, 10, 20, 12, 0), hasnicetitle=True, ejected=True,
                    updated=False, video_type="movie")
    defaults.update(fields)
    for key, value in defaults.items():
        setattr(job, key, value)
    db.session.add(job)
    db.session.commit()
    return job


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM web UI cannot be imported here: {IMPORT_ERROR}")
class TestBoxesPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["ARM_CONFIG_FILE"] = _CONFIG_PATH
        if cfg.arm_config_path != _CONFIG_PATH:
            importlib.reload(cfg)
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["LOGIN_DISABLED"] = True
        cls.client = app.test_client()

    def setUp(self):
        cfg.arm_config["LABEL_ENABLED"] = True
        Job.query.delete()
        db.session.commit()
        cfg.arm_config["LABEL_ID_PREFIX"] = "T6"
        self.jaws = make_job(title="Jaws", sleeve_id="T6-0001", box="Box 01",
                             label_printed=datetime(2026, 10, 20, 12, 30))
        self.alien = make_job(title="Alien", year="1979", sleeve_id="T6-0002", box=None, label="ALIEN_UHD",
                              label_printed=None)
        self.unlabelled = make_job(title="Heat", year="1995", disctype="dvd")

    def test_page_lists_boxes_and_unfiled(self):
        response = self.client.get("/boxes")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode()
        self.assertIn("2 labelled discs", html)
        self.assertIn("1 box,", html)
        self.assertIn("1 not filed yet", html)
        self.assertIn("<strong>Box 01</strong>", html)
        self.assertIn("Not filed in a box yet", html)
        self.assertIn("T6-0002", html)
        self.assertIn(">UHD<", html)
        self.assertNotIn("Heat", html, "jobs without a sleeve id are not listed")
        self.assertIn('href="boxes"', html, "nav link")
        self.assertIn("Labels to print", html)
        self.assertIn("1 waiting", html)
        self.assertIn("ids start with <code>T6-</code>", html)

    def test_disabled_warning(self):
        cfg.arm_config["LABEL_ENABLED"] = False
        html = self.client.get("/boxes").data.decode()
        self.assertIn("Sleeve labels are off", html)

    def find(self, query):
        """The 'Find a disc' card only (the boxes are listed under it on the same page)."""
        html = self.client.get(f"/boxes?q={query}").data.decode()
        return html.split("<strong>Find a disc</strong>")[1].split('<div class="card mb-3">')[0]

    def test_find_by_sleeve_id_and_title(self):
        card = self.find("t6-1")
        self.assertIn("Jaws (1975)", card)
        self.assertNotIn("Alien (1979)", card)
        self.assertIn("Alien (1979)", self.find("2"), "a bare number means this machine's prefix")
        card = self.find("ali")
        self.assertIn("Alien (1979)", card)
        self.assertIn("not filed", card)
        self.assertNotIn("Jaws (1975)", card)
        self.assertIn("Nothing matches", self.find("nothing-here"))

    def test_file_a_sleeve_into_a_box(self):
        response = self.client.post("/boxes/file", data={"box": "Box 02", "sleeve_id": "t6-2"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        html = response.data.decode()
        self.assertIn("T6-0002 Alien filed in Box 02", html)
        self.assertEqual(Job.query.get(self.alien.job_id).box, "Box 02")
        self.assertIn('value="Box 02"', html, "the box name stays in the form for the next scan")

    def test_file_rejects_unknown_id_and_missing_box(self):
        html = self.client.post("/boxes/file", data={"box": "Box 02", "sleeve_id": "T6-0404"},
                                follow_redirects=True).data.decode()
        self.assertIn("No disc has the sleeve id T6-0404", html)
        html = self.client.post("/boxes/file", data={"box": "", "sleeve_id": "T6-0002"},
                                follow_redirects=True).data.decode()
        self.assertIn("Name the box first", html)
        self.assertIsNone(Job.query.get(self.alien.job_id).box)

    def test_set_box_from_job_page(self):
        response = self.client.post("/boxes/set", data={"job_id": self.jaws.job_id, "box": "Shelf"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"job_id={self.jaws.job_id}", response.headers["Location"])
        self.assertEqual(Job.query.get(self.jaws.job_id).box, "Shelf")
        self.client.post("/boxes/set", data={"job_id": self.jaws.job_id, "box": ""})
        self.assertIsNone(Job.query.get(self.jaws.job_id).box)
        self.assertEqual(self.client.post("/boxes/set", data={"job_id": "999"}).status_code, 404)

    def test_print_from_job_page(self):
        with mock.patch.object(sleeve.printer, "print_image", return_value="ok") as printer:
            response = self.client.post("/boxes/print", data={"job_id": self.unlabelled.job_id}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        printer.assert_called_once()
        job = Job.query.get(self.unlabelled.job_id)
        self.assertEqual(job.sleeve_id, "T6-0003", "next free id, assigned on first print")
        self.assertIsNotNone(job.label_printed)
        self.assertIn("Printed label T6-0003", response.data.decode())

    def test_print_failure_is_shown(self):
        with mock.patch.object(sleeve.printer, "print_image", side_effect=sleeve.printer.PrinterError("unplugged")):
            response = self.client.post("/boxes/print", data={"job_id": self.jaws.job_id}, follow_redirects=True)
        html = response.data.decode()
        self.assertIn("not printed (pending): unplugged", html)

    def test_print_pending(self):
        with mock.patch.object(sleeve.printer, "print_image", return_value="ok") as printer:
            html = self.client.post("/boxes/print_pending", follow_redirects=True).data.decode()
        printer.assert_called_once()
        self.assertIn("Printed 1 label: T6-0002", html)
        self.assertIsNotNone(Job.query.get(self.alien.job_id).label_printed)
        self.assertNotIn("Labels to print", html)
        html = self.client.post("/boxes/print_pending", follow_redirects=True).data.decode()
        self.assertIn("No labels are waiting", html)
        make_job(title="Heat", sleeve_id="T6-0003")
        make_job(title="Ronin", sleeve_id="T6-0004")
        with mock.patch.object(sleeve.printer, "print_image",
                               side_effect=sleeve.printer.PrinterError("out of tape")) as printer:
            html = self.client.post("/boxes/print_pending", follow_redirects=True).data.decode()
        printer.assert_called_once()
        self.assertIn("Printed 0, then Label T6-0003 not printed (pending): out of tape. 2 still pending.", html)

    def test_preview_png(self):
        try:
            import PIL  # noqa: F401
            import qrcode  # noqa: F401
        except ImportError:
            self.skipTest("Pillow and qrcode are not installed")
        response = self.client.get(f"/boxes/preview.png?job_id={self.jaws.job_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")
        self.assertTrue(response.data.startswith(b"\x89PNG"))

    def test_export_csv(self):
        response = self.client.get("/boxes/export.csv")
        self.assertEqual(response.status_code, 200)
        lines = response.data.decode().splitlines()
        self.assertEqual(lines[0], "sleeve_id,title,year,disc_type,box,job_id,ripped,label_printed,disc_barcode,path")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[1].startswith("T6-0001,Jaws,1975,Blu-ray,Box 01,"))
        self.assertIn(",2026-10-20,2026-10-20 12:30,", lines[1])
        self.assertTrue(lines[2].startswith("T6-0002,Alien,1979,UHD,,"))
        self.assertIn(",2026-10-20,,", lines[2])

    def test_job_detail_shows_sleeve_and_box(self):
        with mock.patch("arm.ui.utils.metadata_selector", return_value=None):
            html = self.client.get(f"/jobdetail?job_id={self.jaws.job_id}").data.decode()
        self.assertIn("<strong>T6-0001</strong>", html)
        self.assertIn("printed 2026-10-20 12:30", html)
        self.assertIn('value="Box 01"', html)
        self.assertIn("Reprint label", html)
        with mock.patch("arm.ui.utils.metadata_selector", return_value=None):
            html = self.client.get(f"/jobdetail?job_id={self.alien.job_id}").data.decode()
        self.assertIn("label pending", html)
        self.assertIn("Print label", html)

    def test_json_api(self):
        response = self.client.get(f"/json?mode=set_box&job={self.alien.job_id}&box=Box%2009")
        payload = response.get_json()
        self.assertTrue(payload["success"], payload)
        self.assertEqual(payload["box"], "Box 09")
        self.assertEqual(Job.query.get(self.alien.job_id).box, "Box 09")
        self.assertFalse(self.client.get("/json?mode=set_box&job=999&box=x").get_json()["success"])
        with mock.patch.object(sleeve.printer, "print_image", return_value="ok"):
            payload = self.client.get(f"/json?mode=print_label&job={self.jaws.job_id}").get_json()
        self.assertTrue(payload["success"], payload)
        self.assertEqual(payload["sleeve_id"], "T6-0001")
        payload = self.client.get("/json?mode=pending_labels").get_json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["prefix"], "T6")
        self.assertEqual(payload["jobs"][0]["sleeve_id"], "T6-0002")
        with mock.patch.object(sleeve.printer, "print_image", return_value="ok"):
            payload = self.client.get("/json?mode=print_pending").get_json()
        self.assertTrue(payload["success"], payload)
        self.assertEqual(payload["printed"], ["T6-0002"])
        self.assertEqual(payload["pending"], 0)


if __name__ == "__main__":
    unittest.main()
