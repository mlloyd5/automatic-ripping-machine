"""
Send a label bitmap to a P-touch printer through a command line tool.

The default tool is ``ptouch`` from ptouch-rs (https://github.com/vowstar/ptouch-rs), which drives the
PT-E310BT, PT-E560BT, PT-P710BT, PT-D610BT and most other USB P-touch models through libusb:
``ptouch print -i label.png``. The command is a template, so ``ptouch-print --image {image}`` or any
other tool that takes an image file works too. No ARM imports: this module is tested with a fake command.
"""
import shlex
import shutil
import subprocess
from typing import List, Optional, Tuple

DEFAULT_COMMAND = "ptouch print --job-timeout 20 -i {image}"
IMAGE_PLACEHOLDER = "{image}"
UNCONFIRMED_MARKER = "completion is unconfirmed"
"""
Released ptouch-rs (0.14.0) never switches the PT-E310BT's status notification on, so the label prints but
the tool waits for a "printing completed" status that never comes and exits 1 after --job-timeout with
"Label sent, but completion is unconfirmed". That outcome counts as printed here, and the short job
timeout in the default command keeps a queue flush at seconds per label. A build with the fix (proposed
upstream) returns as soon as the printer reports completion; drop --job-timeout for it.
"""


class PrinterError(Exception):
    """The label could not be printed; the message says why in words fit for a log line."""


def build_command(template: str, image_path: str, usb: Optional[str] = None) -> List[str]:
    """
    The argv to run for ``template``.

    ``{image}`` in the template is replaced by the PNG path (appended when the template has no
    placeholder). ``usb`` (ptouch-rs ``BUS:ADDRESS`` from ``ptouch doctor``) is passed as ``--usb``
    right after the program name when set; it only makes sense with the ptouch-rs tool.
    """
    template = (template or DEFAULT_COMMAND).strip() or DEFAULT_COMMAND
    parts = shlex.split(template)
    if IMAGE_PLACEHOLDER in template:
        argv = [image_path if part == IMAGE_PLACEHOLDER else part.replace(IMAGE_PLACEHOLDER, image_path)
                for part in parts]
    else:
        argv = parts + [image_path]
    if usb and str(usb).strip():
        argv = argv[:1] + ["--usb", str(usb).strip()] + argv[1:]
    return argv


def tool_available(template: str) -> Tuple[bool, str]:
    """(True, path) when the template's program is on PATH or an existing file, else (False, why)."""
    try:
        program = shlex.split((template or DEFAULT_COMMAND).strip() or DEFAULT_COMMAND)[0]
    except ValueError as error:
        return False, f"LABEL_PRINTER_CMD cannot be parsed: {error}"
    found = shutil.which(program)
    if found:
        return True, found
    return False, f"'{program}' is not installed or not on PATH (install ptouch-rs, see the Sleeve Labels wiki page)"


def print_image(image_path: str, template: str = DEFAULT_COMMAND, usb: Optional[str] = None,
                timeout: int = 60, runner=subprocess.run) -> str:
    """
    Print the PNG at ``image_path``.

    :param template: command template, see :func:`build_command`
    :param usb: ptouch-rs ``--usb BUS:ADDRESS`` selector, blank for the only printer
    :param timeout: seconds before the tool is killed; keep it above the tool's own --job-timeout
    :param runner: ``subprocess.run`` or a stand-in for tests
    :return: the tool's output, for the log
    :raises PrinterError: when the tool is missing, times out or exits non-zero
    """
    available, detail = tool_available(template)
    if not available:
        raise PrinterError(detail)
    argv = build_command(template, image_path, usb)
    try:
        result = runner(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PrinterError(f"printer command timed out after {timeout}s: {' '.join(argv)}") from None
    except OSError as error:
        raise PrinterError(f"printer command could not run: {error}") from None
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        if UNCONFIRMED_MARKER in output:
            return "label sent; the printer does not confirm completion (normal for the PT-E310BT)"
        summary = output.splitlines()[-1] if output else "no output"
        raise PrinterError(f"printer command exited {result.returncode}: {summary}")
    return output
