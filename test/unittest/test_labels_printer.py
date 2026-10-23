"""Tests for arm/labels/printer.py with the printer command faked; no printer or ptouch-rs needed."""
import importlib.util
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("arm_labels_printer", REPO_ROOT / "arm" / "labels" / "printer.py")
printer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(printer)


class TestBuildCommand(unittest.TestCase):
    def test_default_template(self):
        self.assertEqual(printer.build_command(printer.DEFAULT_COMMAND, "/tmp/l.png"),
                         ["ptouch", "print", "-i", "/tmp/l.png"])

    def test_blank_template_uses_default(self):
        default = ["ptouch", "print", "-i", "/tmp/l.png"]
        self.assertEqual(printer.build_command("", "/tmp/l.png"), default)
        self.assertEqual(printer.build_command(None, "/tmp/l.png"), default)

    def test_placeholder_inside_an_argument_and_missing_placeholder(self):
        self.assertEqual(printer.build_command("tool --image={image}", "/tmp/l.png"), ["tool", "--image=/tmp/l.png"])
        self.assertEqual(printer.build_command("ptouch-print --image", "/tmp/l.png"),
                         ["ptouch-print", "--image", "/tmp/l.png"])

    def test_usb_selector_goes_after_the_program(self):
        self.assertEqual(printer.build_command("ptouch print -i {image}", "/tmp/l.png", usb="1:4"),
                         ["ptouch", "--usb", "1:4", "print", "-i", "/tmp/l.png"])
        self.assertEqual(printer.build_command("ptouch print -i {image}", "/tmp/l.png", usb="  "),
                         ["ptouch", "print", "-i", "/tmp/l.png"])

    def test_paths_with_spaces_survive(self):
        self.assertEqual(printer.build_command("'/opt/my tools/ptouch' print -i {image}", "/tmp/a b.png"),
                         ["/opt/my tools/ptouch", "print", "-i", "/tmp/a b.png"])


class TestToolAvailable(unittest.TestCase):
    def test_missing_program(self):
        ok, why = printer.tool_available("definitely-not-a-real-ptouch-binary print -i {image}")
        self.assertFalse(ok)
        self.assertIn("definitely-not-a-real-ptouch-binary", why)

    def test_present_program(self):
        ok, path = printer.tool_available("true {image}")
        self.assertTrue(ok)
        self.assertTrue(path.endswith("true"))

    def test_unparseable_template(self):
        ok, why = printer.tool_available("ptouch 'unterminated")
        self.assertFalse(ok)
        self.assertIn("parsed", why)


class TestPrintImage(unittest.TestCase):
    def _runner(self, returncode=0, stdout="", stderr="", raise_timeout=False, raise_os=False):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if raise_timeout:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            if raise_os:
                raise OSError("permission denied")
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        run.calls = calls
        return run

    def test_success_returns_output_and_passes_timeout(self):
        run = self._runner(stdout="printed 1 label\n")
        with mock.patch.object(printer.shutil, "which", return_value="/usr/bin/ptouch"):
            output = printer.print_image("/tmp/l.png", timeout=7, runner=run)
        self.assertEqual(output, "printed 1 label")
        argv, kwargs = run.calls[0]
        self.assertEqual(argv, ["ptouch", "print", "-i", "/tmp/l.png"])
        self.assertEqual(kwargs["timeout"], 7)

    def test_unconfirmed_completion_counts_as_printed(self):
        """The PT-E310BT never reports the job done; ptouch-rs then exits 1 after its job timeout."""
        message = ("Error: Label sent, but completion is unconfirmed. "
                   "Check the printer before retrying; the label may already have printed\n")
        run = self._runner(returncode=1, stderr=message)
        with mock.patch.object(printer.shutil, "which", return_value="/usr/bin/ptouch"):
            output = printer.print_image("/tmp/l.png", runner=run)
        self.assertIn("does not confirm", output)

    def test_missing_tool_is_a_printer_error_before_running(self):
        run = self._runner()
        with mock.patch.object(printer.shutil, "which", return_value=None):
            with self.assertRaises(printer.PrinterError) as ctx:
                printer.print_image("/tmp/l.png", runner=run)
        self.assertIn("not installed", str(ctx.exception))
        self.assertEqual(run.calls, [])

    def test_nonzero_exit_reports_last_output_line(self):
        run = self._runner(returncode=2, stderr="opening device\nerror: no printer found\n")
        with mock.patch.object(printer.shutil, "which", return_value="/usr/bin/ptouch"):
            with self.assertRaises(printer.PrinterError) as ctx:
                printer.print_image("/tmp/l.png", runner=run)
        self.assertIn("exited 2", str(ctx.exception))
        self.assertIn("no printer found", str(ctx.exception))

    def test_timeout_and_oserror(self):
        with mock.patch.object(printer.shutil, "which", return_value="/usr/bin/ptouch"):
            with self.assertRaises(printer.PrinterError) as ctx:
                printer.print_image("/tmp/l.png", timeout=3, runner=self._runner(raise_timeout=True))
            self.assertIn("timed out after 3s", str(ctx.exception))
            with self.assertRaises(printer.PrinterError) as ctx:
                printer.print_image("/tmp/l.png", runner=self._runner(raise_os=True))
            self.assertIn("could not run", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
