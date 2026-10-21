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
                sleeve_id=None, box=None, status="success", config=SimpleNamespace(NOTIFY_RIP=True))
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
                               "LABEL_ID_PREFIX": "S", "LABEL_MAX_MM": 90, "LABEL_FONT": ""})
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
        self.assertEqual(sleeve.parse_sleeve_id("S0042"), ("S", 42))
        self.assertEqual(sleeve.parse_sleeve_id(" s42 "), ("S", 42))
        self.assertEqual(sleeve.parse_sleeve_id("42"), ("", 42))
        self.assertIsNone(sleeve.parse_sleeve_id("Jaws"))
        self.assertIsNone(sleeve.parse_sleeve_id(""))
        self.assertIsNone(sleeve.parse_sleeve_id(None))
        self.assertEqual(sleeve.normalise_sleeve_id("s42"), "S0042")
        self.assertEqual(sleeve.normalise_sleeve_id("S12345"), "S12345")
        self.assertIsNone(sleeve.normalise_sleeve_id("box 3"))

    def test_next_id_counts_from_the_highest_with_the_prefix(self):
        with self.fake_ids("S0001", "S0042", "S0007", "X0099", None):
            self.assertEqual(sleeve.next_sleeve_id(), "S0043")
            self.assertEqual(sleeve.next_sleeve_id("X"), "X0100")
            self.assertEqual(sleeve.next_sleeve_id("Z"), "Z0001")
        with self.fake_ids():
            self.assertEqual(sleeve.next_sleeve_id(), "S0001")

    def test_prefix_from_config_with_fallback(self):
        cfg.arm_config["LABEL_ID_PREFIX"] = "DVD"
        self.assertEqual(sleeve.id_prefix(), "DVD")
        cfg.arm_config["LABEL_ID_PREFIX"] = "toolong"
        self.assertEqual(sleeve.id_prefix(), "S")
        cfg.arm_config["LABEL_ID_PREFIX"] = ""
        with self.fake_ids("0003"):
            self.assertEqual(sleeve.next_sleeve_id(), "0004")

    def test_assign_keeps_an_existing_id(self):
        job = _job(sleeve_id="S0005")
        with self.fake_ids("S0009"):
            self.assertEqual(sleeve.assign_sleeve_id(job), "S0005")
        job = _job()
        with self.fake_ids("S0009"):
            self.assertEqual(sleeve.assign_sleeve_id(job), "S0010")
        self.assertEqual(job.sleeve_id, "S0010")

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

    def test_prints_and_cleans_up(self):
        job = _job()
        seen = {}

        def fake_print(path, template, usb, timeout):
            seen.update(path=path, template=template, usb=usb, timeout=timeout, existed=os.path.exists(path))
            return "ok"

        with self.fake_ids("S0001"), mock.patch.object(sleeve.printer, "print_image", side_effect=fake_print):
            printed, message = sleeve.print_label(job, reason="test")
        self.assertTrue(printed, message)
        self.assertEqual(job.sleeve_id, "S0002")
        self.assertEqual(message, "Printed label S0002")
        self.assertTrue(seen["existed"], "the PNG existed while the printer ran")
        self.assertFalse(os.path.exists(seen["path"]), "the PNG is removed afterwards")
        self.assertEqual((seen["template"], seen["usb"], seen["timeout"]), ("ptouch print -i {image}", None, 60))

    def test_printer_failure_keeps_the_id(self):
        job = _job()
        with self.fake_ids(), mock.patch.object(sleeve.printer, "print_image",
                                                side_effect=sleeve.printer.PrinterError("no printer found")):
            printed, message = sleeve.print_label(job)
        self.assertFalse(printed)
        self.assertEqual(job.sleeve_id, "S0001")
        self.assertIn("no printer found", message)
        self.assertIn("S0001", message)

    def test_render_failure_is_reported_not_raised(self):
        cfg.arm_config["LABEL_TAPE_MM"] = 13
        with self.fake_ids():
            printed, message = sleeve.print_label(_job())
        self.assertFalse(printed)
        self.assertIn("Could not render", message)

    def test_auto_print_rules(self):
        with mock.patch.object(sleeve, "print_label", return_value=(True, "Printed label S0001")) as printer:
            self.assertEqual(sleeve.auto_print(_job(disctype="music"))[0], False)
            cfg.arm_config["LABEL_AUTO_PRINT"] = False
            self.assertEqual(sleeve.auto_print(_job())[0], False)
            printer.assert_not_called()
            cfg.arm_config["LABEL_AUTO_PRINT"] = True
            self.assertEqual(sleeve.auto_print(_job(disctype="dvd")), (True, "Printed label S0001"))
            printer.assert_called_once()

    def test_backfill(self):
        jobs = [_job(job_id=1, title="A"), _job(job_id=2, title="B")]
        with mock.patch.object(sleeve, "jobs_without_sleeve_id", return_value=jobs), \
                mock.patch.object(sleeve, "next_sleeve_id", side_effect=["S0001", "S0002"]), \
                mock.patch.object(sleeve, "print_label", return_value=(True, "Printed")) as printer:
            results = sleeve.backfill()
        self.assertEqual([r["sleeve_id"] for r in results], ["S0001", "S0002"])
        self.assertEqual(printer.call_count, 2)
        self.assertEqual(jobs[1].sleeve_id, "S0002")
        jobs = [_job(job_id=3, title="C")]
        with mock.patch.object(sleeve, "jobs_without_sleeve_id", return_value=jobs), \
                mock.patch.object(sleeve, "next_sleeve_id", return_value="S0003"), \
                mock.patch.object(sleeve, "print_label") as printer:
            results = sleeve.backfill(dry_run=True)
        self.assertEqual(results[0]["message"], "dry run")
        self.assertIsNone(jobs[0].sleeve_id)
        printer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
