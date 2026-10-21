"""
Tests for arm/labels/sleeve.py: sleeve id parsing and assignment, disc type naming and the print path,
with the database session and the printer command faked.

Importing ``arm.labels.sleeve`` pulls in ARM's config and Flask app, so a throwaway arm.yaml is written
first and pointed at with ARM_CONFIG_FILE; skipped when ARM cannot be imported on this host.
"""
import atexit
import importlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]

_TMP = tempfile.mkdtemp(prefix="arm_sleeve_test_")
_CONFIG = os.path.join(_TMP, "arm.yaml")
with open(_CONFIG, "w") as _handle:
    _handle.write(f"INSTALLPATH: \"{REPO_ROOT}\"\n"
                  f"DBFILE: \"{_TMP}/arm.db\"\n"
                  f"LOGPATH: \"{_TMP}/logs/\"\n"
                  f"ABCDE_CONFIG_FILE: \"{REPO_ROOT}/setup/.abcde.conf\"\n"
                  f"RAW_PATH: \"{_TMP}/raw/\"\nTRANSCODE_PATH: \"{_TMP}/transcode/\"\n"
                  f"COMPLETED_PATH: \"{_TMP}/completed/\"\n"
                  "DISABLE_LOGIN: true\nLABEL_ENABLED: true\n")
os.environ["ARM_CONFIG_FILE"] = _CONFIG

try:
    sleeve = importlib.import_module("arm.labels.sleeve")
    cfg = importlib.import_module("arm.config.config")
    IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on the host
    sleeve = cfg = None
    IMPORT_ERROR = error


# The SQLAlchemy engine binds to the DBFILE of whichever test module imports ARM first and keeps it
# for the whole run, so the folder is removed at interpreter exit, not at module teardown
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)


def _job(**overrides):
    base = dict(job_id=7, disctype="bluray", title="Jaws", year="1975", label="JAWS_UHD", disc_barcode_title=None,
                sleeve_id=None, label_printed=None, box=None, status="success",
                config=SimpleNamespace(NOTIFY_RIP=True))
    base.update(overrides)
    return SimpleNamespace(**base)


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class SleeveTestCase(unittest.TestCase):
    def setUp(self):
        os.environ["ARM_CONFIG_FILE"] = _CONFIG
        if cfg.arm_config_path != _CONFIG:
            importlib.reload(cfg)
        cfg.arm_config.update({"LABEL_ENABLED": True, "LABEL_AUTO_PRINT": True, "LABEL_TAPE_MM": 18,
                               "LABEL_PRINTER_CMD": "ptouch print -i {image}", "LABEL_USB": "", "LABEL_TIMEOUT": 60,
                               "LABEL_ID_PREFIX": "T6", "LABEL_MAX_MM": 90, "LABEL_FONT": ""})
        self.commit = mock.patch.object(sleeve, "commit", return_value=True)
        self.commit.start()

    def tearDown(self):
        self.commit.stop()

    def fake_ids(self, *ids):
        """Patch the sleeve-id query to return these existing ids."""
        query = mock.MagicMock()
        query.filter.return_value.all.return_value = [(value,) for value in ids]
        return mock.patch.object(sleeve.db.session, "query", return_value=query)


class TestSleeveIds(SleeveTestCase):
    def test_parse_and_normalise(self):
        self.assertEqual(sleeve.parse_sleeve_id("T6-0042"), ("T6", 42))
        self.assertEqual(sleeve.parse_sleeve_id(" t6-42 "), ("T6", 42))
        self.assertEqual(sleeve.parse_sleeve_id("TS-7"), ("TS", 7))
        self.assertIsNone(sleeve.parse_sleeve_id("42"), "a bare number needs a default prefix")
        self.assertEqual(sleeve.parse_sleeve_id("42", default_prefix="t6"), ("T6", 42))
        self.assertIsNone(sleeve.parse_sleeve_id("S0042"), "the old dash-less form is not an id")
        self.assertIsNone(sleeve.parse_sleeve_id("Jaws"))
        self.assertIsNone(sleeve.parse_sleeve_id(""))
        self.assertIsNone(sleeve.parse_sleeve_id(None))
        self.assertEqual(sleeve.normalise_sleeve_id("t6-42"), "T6-0042")
        self.assertEqual(sleeve.normalise_sleeve_id("T6-12345"), "T6-12345")
        self.assertEqual(sleeve.normalise_sleeve_id("42", default_prefix="TS"), "TS-0042")
        self.assertIsNone(sleeve.normalise_sleeve_id("box 3"))

    def test_prefix_from_hostname(self):
        self.assertEqual(sleeve.prefix_from_hostname("t620"), "T6")
        self.assertEqual(sleeve.prefix_from_hostname("ts140.lan"), "TS")
        self.assertEqual(sleeve.prefix_from_hostname("arm"), "AR")
        self.assertEqual(sleeve.prefix_from_hostname("-x"), "X")
        self.assertEqual(sleeve.prefix_from_hostname(""), "S")

    def test_prefix_from_config_with_fallback(self):
        self.assertEqual(sleeve.id_prefix(), "T6")
        cfg.arm_config["LABEL_ID_PREFIX"] = "dvd"
        self.assertEqual(sleeve.id_prefix(), "DVD")
        with mock.patch.object(sleeve, "prefix_from_hostname", return_value="HO"):
            cfg.arm_config["LABEL_ID_PREFIX"] = "toolong"
            self.assertEqual(sleeve.id_prefix(), "HO")
            cfg.arm_config["LABEL_ID_PREFIX"] = "a-b"
            self.assertEqual(sleeve.id_prefix(), "HO")
            cfg.arm_config["LABEL_ID_PREFIX"] = ""
            self.assertEqual(sleeve.id_prefix(), "HO")

    def test_next_id_counts_from_the_highest_with_the_prefix(self):
        with self.fake_ids("T6-0001", "T6-0042", "T6-0007", "TS-0099", None):
            self.assertEqual(sleeve.next_sleeve_id(), "T6-0043")
            self.assertEqual(sleeve.next_sleeve_id("TS"), "TS-0100")
            self.assertEqual(sleeve.next_sleeve_id("Z"), "Z-0001")
        with self.fake_ids():
            self.assertEqual(sleeve.next_sleeve_id(), "T6-0001")

    def test_assign_keeps_an_existing_id(self):
        job = _job(sleeve_id="T6-0005")
        with self.fake_ids("T6-0009"):
            self.assertEqual(sleeve.assign_sleeve_id(job), "T6-0005")
        job = _job()
        with self.fake_ids("T6-0009"):
            self.assertEqual(sleeve.assign_sleeve_id(job), "T6-0010")
        self.assertEqual(job.sleeve_id, "T6-0010")
        self.assertIsNone(job.label_printed, "a fresh id is pending")

    def test_assign_fails_loudly_when_the_save_fails(self):
        self.commit.stop()
        with mock.patch.object(sleeve, "commit", return_value=False), self.fake_ids():
            with self.assertRaises(RuntimeError):
                sleeve.assign_sleeve_id(_job())
        self.commit.start()

    def test_set_box_trims_and_clears(self):
        job = _job()
        self.assertEqual(sleeve.set_box(job, "  Box 03 "), "Box 03")
        self.assertEqual(job.box, "Box 03")
        self.assertIsNone(sleeve.set_box(job, "   "))
        self.assertIsNone(job.box)
        self.assertEqual(len(sleeve.set_box(job, "x" * 100)), sleeve.MAX_BOX_LENGTH)


class TestLabelContent(SleeveTestCase):
    def test_disc_type_names(self):
        self.assertEqual(sleeve.disc_type_name(_job(disctype="dvd")), "DVD")
        self.assertEqual(sleeve.disc_type_name(_job(disctype="bluray", label="JAWS")), "Blu-ray")
        self.assertEqual(sleeve.disc_type_name(_job(disctype="bluray", label="JAWS_UHD")), "UHD")
        self.assertEqual(sleeve.disc_type_name(_job(disctype="bluray", label="JAWS",
                                                    disc_barcode_title="Jaws (4K Ultra HD + Blu-ray)")), "UHD")
        self.assertEqual(sleeve.disc_type_name(_job(disctype="dvd", label="JAWS_UHD")), "DVD")
        self.assertEqual(sleeve.disc_type_name(_job(disctype=None)), "")

    def test_label_lines_fall_back_to_the_disc_label(self):
        self.assertEqual(sleeve.label_lines(_job(label="JAWS")), ["Jaws (1975)", "Blu-ray"])
        self.assertEqual(sleeve.label_lines(_job(title="not identified", year="", label="LOGICAL_VOLUME")),
                         ["LOGICAL_VOLUME", "Blu-ray"])


class TestPrinting(SleeveTestCase):
    def test_disabled(self):
        cfg.arm_config["LABEL_ENABLED"] = False
        printed, message = sleeve.print_label(_job())
        self.assertFalse(printed)
        self.assertIn("disabled", message)

    def test_prints_marks_printed_and_cleans_up(self):
        job = _job()
        seen = {}

        def fake_print(path, template, usb, timeout):
            seen.update(path=path, template=template, usb=usb, timeout=timeout, existed=os.path.exists(path))
            return "ok"

        with self.fake_ids("T6-0001"), mock.patch.object(sleeve.printer, "print_image", side_effect=fake_print):
            printed, message = sleeve.print_label(job, reason="test")
        self.assertTrue(printed, message)
        self.assertEqual(job.sleeve_id, "T6-0002")
        self.assertEqual(message, "Printed label T6-0002")
        self.assertIsNotNone(job.label_printed)
        self.assertTrue(seen["existed"], "the PNG existed while the printer ran")
        self.assertFalse(os.path.exists(seen["path"]), "the PNG is removed afterwards")
        self.assertEqual((seen["template"], seen["usb"], seen["timeout"]), ("ptouch print -i {image}", None, 60))

    def test_printer_failure_keeps_the_id_pending(self):
        job = _job()
        with self.fake_ids(), mock.patch.object(sleeve.printer, "print_image",
                                                side_effect=sleeve.printer.PrinterError("no printer found")):
            printed, message = sleeve.print_label(job)
        self.assertFalse(printed)
        self.assertEqual(job.sleeve_id, "T6-0001")
        self.assertIsNone(job.label_printed)
        self.assertIn("no printer found", message)
        self.assertIn("pending", message)

    def test_render_failure_is_reported_not_raised(self):
        cfg.arm_config["LABEL_TAPE_MM"] = 13
        with self.fake_ids():
            printed, message = sleeve.print_label(_job())
        self.assertFalse(printed)
        self.assertIn("Could not render", message)

    def test_auto_print_assigns_the_id_even_without_printing(self):
        with mock.patch.object(sleeve, "print_label", return_value=(True, "Printed label T6-0001")) as printer:
            self.assertFalse(sleeve.auto_print(_job(disctype="music"))[0])
            cfg.arm_config["LABEL_ENABLED"] = False
            self.assertFalse(sleeve.auto_print(_job())[0])
            printer.assert_not_called()
            cfg.arm_config["LABEL_ENABLED"] = True
            cfg.arm_config["LABEL_AUTO_PRINT"] = False
            job = _job()
            with self.fake_ids("T6-0004"):
                printed, message = sleeve.auto_print(job)
            self.assertFalse(printed)
            self.assertEqual(job.sleeve_id, "T6-0005", "the id is handed out at rip time")
            self.assertIn("pending", message)
            printer.assert_not_called()
            cfg.arm_config["LABEL_AUTO_PRINT"] = True
            with self.fake_ids():
                self.assertEqual(sleeve.auto_print(_job(disctype="dvd")), (True, "Printed label T6-0001"))
            printer.assert_called_once()

    def test_print_pending_stops_at_the_first_failure(self):
        jobs = [_job(job_id=1, sleeve_id="T6-0001"), _job(job_id=2, sleeve_id="T6-0002"),
                _job(job_id=3, sleeve_id="T6-0003")]
        outcomes = {1: (True, "Printed label T6-0001"), 2: (False, "Label T6-0002 not printed (pending): out of tape"),
                    3: (True, "Printed label T6-0003")}
        with mock.patch.object(sleeve, "pending_jobs", return_value=jobs), \
                mock.patch.object(sleeve, "print_label", side_effect=lambda job, reason="": outcomes[job.job_id]):
            results = sleeve.print_pending()
            self.assertEqual([r["job_id"] for r in results], [1, 2])
            self.assertEqual([r["printed"] for r in results], [True, False])
            results = sleeve.print_pending(stop_on_error=False)
            self.assertEqual([r["job_id"] for r in results], [1, 2, 3])
            results = sleeve.print_pending(limit=1)
            self.assertEqual([r["job_id"] for r in results], [1])

    def test_backfill(self):
        jobs = [_job(job_id=1, title="A"), _job(job_id=2, title="B")]
        with mock.patch.object(sleeve, "jobs_without_sleeve_id", return_value=jobs), \
                mock.patch.object(sleeve, "next_sleeve_id", side_effect=["T6-0001", "T6-0002"]), \
                mock.patch.object(sleeve, "pending_jobs", return_value=jobs), \
                mock.patch.object(sleeve, "print_label", return_value=(True, "Printed")) as printer:
            results = sleeve.backfill()
        self.assertEqual([r["sleeve_id"] for r in results], ["T6-0001", "T6-0002"])
        self.assertEqual([r["printed"] for r in results], [True, True])
        self.assertEqual(printer.call_count, 2)
        self.assertEqual(jobs[1].sleeve_id, "T6-0002")
        jobs = [_job(job_id=3, title="C")]
        with mock.patch.object(sleeve, "jobs_without_sleeve_id", return_value=jobs), \
                mock.patch.object(sleeve, "next_sleeve_id", return_value="T6-0003"), \
                mock.patch.object(sleeve, "print_label") as printer:
            results = sleeve.backfill(dry_run=True)
            self.assertEqual(results[0]["message"], "dry run")
            self.assertIsNone(jobs[0].sleeve_id)
            results = sleeve.backfill(print_labels=False)
            self.assertEqual(jobs[0].sleeve_id, "T6-0003")
            self.assertIn("pending", results[0]["message"])
        printer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
