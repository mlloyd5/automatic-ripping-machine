"""
Sleeve ids, the print queue and label printing for ARM jobs.

Every finished video rip gets a sleeve id (``T6-0001``, ``T6-0002``, ...) as soon as the rip is done.
The letters are the machine prefix (``LABEL_ID_PREFIX``, by default from the hostname), so two ARM
boxes ripping at the same time never hand out the same id, and the number counts from the highest
one with that prefix already in the database; the id is separate from ``job_id`` so a rebuilt
database never reuses a code that is already on a sleeve.

``label_printed`` records when the label came out of the printer. A job with a sleeve id and no
``label_printed`` is the print queue: a rip done while the printer was on the other box, unplugged
or out of tape waits there until "Print pending" on the Boxes page or ``python3 -m arm.labels
print --pending`` flushes it. ``box`` records where the sleeve was filed. Printing goes through
:mod:`arm.labels.render` and :mod:`arm.labels.printer`; a missing printer or library is logged and
never raised to the rip.
"""
import datetime
import logging
import os
import re
import socket
import tempfile
import time
from typing import List, Optional, Tuple

from sqlalchemy.exc import OperationalError

import arm.config.config as cfg
from arm.ui import db
from arm.models.job import Job, JobState
from arm.labels import render, printer

SLEEVE_ID_RE = re.compile(r"^([A-Za-z0-9]{1,4})-(\d{1,10})$")
"""A sleeve id is a short machine prefix, a dash and a number; the number is what increments."""
NUMBER_ONLY_RE = re.compile(r"^\d{1,10}$")
"""A bare number typed by hand means "this machine's prefix" and that number."""
PREFIX_RE = re.compile(r"^[A-Za-z0-9]{1,4}$")
VIDEO_DISC_TYPES = ("dvd", "bluray")
DISC_TYPE_NAMES = {"dvd": "DVD", "bluray": "Blu-ray", "music": "CD", "data": "Data"}
UHD_HINT_RE = re.compile(r"(?<![A-Za-z0-9])(uhd|4k|ultra\s*hd)(?![A-Za-z0-9])", re.IGNORECASE)
"""Disc labels look like JAWS_UHD and product titles like "Jaws (4K Ultra HD)"; underscores are not word breaks."""
MAX_BOX_LENGTH = 64
FALLBACK_PREFIX = "S"


def setting(key: str, default=None):
    """``arm.yaml`` value for ``key``, or ``default`` when the key is missing (older config files)."""
    return cfg.arm_config.get(key, default)


def as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def enabled() -> bool:
    return as_bool(setting("LABEL_ENABLED", False))


def auto_print_enabled() -> bool:
    return enabled() and as_bool(setting("LABEL_AUTO_PRINT", True))


def prefix_from_hostname(hostname: Optional[str] = None) -> str:
    """
    The machine prefix derived from the hostname: its first two letters or digits, upper-cased.

    ``t620`` -> ``T6``, ``ts140`` -> ``TS``, ``arm`` -> ``AR``. Two boxes whose names start the same
    way need ``LABEL_ID_PREFIX`` set by hand.
    """
    name = hostname if hostname is not None else socket.gethostname()
    chars = [c for c in (name or "") if c.isalnum()][:2]
    return "".join(chars).upper() or FALLBACK_PREFIX


def id_prefix() -> str:
    """``LABEL_ID_PREFIX`` when set and valid, else the prefix from the hostname."""
    configured = str(setting("LABEL_ID_PREFIX", "") or "").strip().upper()
    if configured and PREFIX_RE.match(configured):
        return configured
    if configured:
        logging.warning(f"LABEL_ID_PREFIX {configured!r} is not 1-4 letters or digits; using the hostname prefix")
    return prefix_from_hostname()


def parse_sleeve_id(value, default_prefix: Optional[str] = None) -> Optional[Tuple[str, int]]:
    """
    ``T6-0042`` -> ``("T6", 42)``; a bare number uses ``default_prefix`` when given; else ``None``.
    """
    if value is None:
        return None
    text = str(value).strip()
    match = SLEEVE_ID_RE.match(text)
    if match:
        return match.group(1).upper(), int(match.group(2))
    if default_prefix and NUMBER_ONLY_RE.match(text):
        return default_prefix.upper(), int(text)
    return None


def format_sleeve_id(prefix: str, number: int) -> str:
    return f"{prefix.upper()}-{number:04d}"


def normalise_sleeve_id(value, default_prefix: Optional[str] = None) -> Optional[str]:
    """
    What a scanner or a person typed, in the stored spelling.

    ``t6-42``, ``T6-0042`` and, given this machine's prefix, ``42`` all become ``T6-0042``.
    """
    parsed = parse_sleeve_id(value, default_prefix)
    if parsed is None:
        return None
    return format_sleeve_id(*parsed)


def next_sleeve_id(prefix: Optional[str] = None) -> str:
    """The id after the highest one in the database with this prefix (``T6-0001`` when there is none)."""
    prefix = (prefix if prefix is not None else id_prefix()).upper()
    highest = 0
    for (sleeve_id,) in db.session.query(Job.sleeve_id).filter(Job.sleeve_id.isnot(None)).all():
        parsed = parse_sleeve_id(sleeve_id)
        if parsed and parsed[0] == prefix:
            highest = max(highest, parsed[1])
    return format_sleeve_id(prefix, highest + 1)


def commit(retries: int = 5, wait: float = 1.0) -> bool:
    """Commit the session, retrying a few times when SQLite reports the database locked."""
    for attempt in range(1, retries + 1):
        try:
            db.session.commit()
            return True
        except OperationalError as error:
            db.session.rollback()
            logging.warning(f"Database locked while saving sleeve data (try {attempt} of {retries}): {error}")
            time.sleep(wait)
    return False


def assign_sleeve_id(job) -> str:
    """The job's sleeve id, assigning and saving the next free one when it has none (it is then pending)."""
    if job.sleeve_id:
        return job.sleeve_id
    job.sleeve_id = next_sleeve_id()
    job.label_printed = None
    if not commit():
        raise RuntimeError(f"could not save sleeve id {job.sleeve_id} for job {job.job_id}")
    logging.info(f"Job {job.job_id} assigned sleeve id {job.sleeve_id}")
    return job.sleeve_id


def mark_printed(job, when: Optional[datetime.datetime] = None) -> None:
    job.label_printed = when or datetime.datetime.now()
    if not commit():
        logging.warning(f"Label {job.sleeve_id} printed but the database would not record it; it stays pending")


def set_box(job, box: Optional[str]) -> Optional[str]:
    """Record the box a sleeve was filed in (blank clears it) and save; returns the stored value."""
    cleaned = (box or "").strip()[:MAX_BOX_LENGTH] or None
    job.box = cleaned
    if not commit():
        raise RuntimeError(f"could not save box for job {job.job_id}")
    logging.info(f"Job {job.job_id} ({job.sleeve_id or 'no sleeve id'}) filed in box {cleaned!r}")
    return cleaned


def find_by_sleeve_id(value):
    """The job whose sleeve id matches ``value`` in any spelling (a bare number means this machine), or ``None``."""
    sleeve_id = normalise_sleeve_id(value, default_prefix=id_prefix())
    if not sleeve_id:
        return None
    return Job.query.filter(Job.sleeve_id == sleeve_id).first()


def pending_jobs():
    """Jobs with a sleeve id whose label has not been printed, oldest first (the print queue)."""
    return Job.query.filter(Job.sleeve_id.isnot(None), Job.label_printed.is_(None)).order_by(Job.job_id.asc()).all()


def disc_type_name(job) -> str:
    """``UHD``, ``Blu-ray``, ``DVD``...: ARM calls UHD discs bluray, so the disc label and barcode title are checked."""
    name = DISC_TYPE_NAMES.get((job.disctype or "").lower(), "")
    hints = " ".join(str(value) for value in (getattr(job, "disc_barcode_title", None), job.title, job.label)
                     if value)
    if name == "Blu-ray" and UHD_HINT_RE.search(hints):
        return "UHD"
    return name


def label_lines(job) -> List[str]:
    """The text lines under the sleeve id for this job."""
    title = job.title if job.title and job.title.lower() != "not identified" else (job.label or "")
    return render.label_text_lines(title, job.year, disc_type_name(job))


def render_job_label(job):
    """The label bitmap for ``job``; assigns a sleeve id when it has none."""
    sleeve_id = assign_sleeve_id(job)
    return render.render_label(sleeve_id, label_lines(job), tape_mm=int(setting("LABEL_TAPE_MM", 18)),
                               font_path=setting("LABEL_FONT", "") or None,
                               max_mm=float(setting("LABEL_MAX_MM", 90)))


def print_label(job, reason: str = "") -> Tuple[bool, str]:
    """
    Print the sleeve label for ``job``.

    Never raises: the result is ``(printed, message)`` with a message fit for the log, a notification
    or the web UI. The sleeve id is assigned and saved even when printing fails, so the job sits in
    the print queue with the same id until the label comes out.
    """
    if not enabled():
        return False, "Sleeve labels are disabled (LABEL_ENABLED is false)"
    try:
        image = render_job_label(job)
    except ImportError as error:
        return False, f"Label libraries missing ({error}); pip install -r requirements-labels.txt"
    except Exception as error:
        logging.error(f"Could not render the sleeve label for job {job.job_id}: {error}", exc_info=True)
        return False, f"Could not render the label: {error}"
    width_mm, height_mm = render.label_size_mm(image)
    handle, path = tempfile.mkstemp(prefix=f"arm_label_{job.sleeve_id}_", suffix=".png")
    os.close(handle)
    try:
        render.save_png(image, path)
        output = printer.print_image(path, template=str(setting("LABEL_PRINTER_CMD", printer.DEFAULT_COMMAND)),
                                     usb=setting("LABEL_USB", "") or None,
                                     timeout=int(setting("LABEL_TIMEOUT", 60)))
    except printer.PrinterError as error:
        logging.warning(f"Sleeve label {job.sleeve_id} for job {job.job_id} not printed, left pending: {error}")
        return False, f"Label {job.sleeve_id} not printed (pending): {error}"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    mark_printed(job)
    suffix = f" ({reason})" if reason else ""
    logging.info(f"Printed sleeve label {job.sleeve_id} ({width_mm} x {height_mm} mm) for job {job.job_id}{suffix}"
                 + (f": {output}" if output else ""))
    return True, f"Printed label {job.sleeve_id}"


def auto_print(job) -> Tuple[bool, str]:
    """
    Called by the ripper after a video rip.

    With LABEL_ENABLED the sleeve id is assigned now, whatever happens next, so the notification can
    carry it and the job is in the print queue. With LABEL_AUTO_PRINT the label is printed too.
    """
    if not enabled():
        return False, "sleeve labels are off"
    if (job.disctype or "").lower() not in VIDEO_DISC_TYPES:
        return False, f"no sleeve label for {job.disctype} discs"
    try:
        sleeve_id = assign_sleeve_id(job)
    except RuntimeError as error:
        logging.error(str(error))
        return False, str(error)
    if not auto_print_enabled():
        return False, f"Sleeve id {sleeve_id} assigned, label pending (LABEL_AUTO_PRINT is off)"
    return print_label(job, reason="rip complete")


def print_pending(limit: Optional[int] = None, stop_on_error: bool = True) -> List[dict]:
    """
    Print every queued label, oldest first.

    :param limit: print at most this many
    :param stop_on_error: stop at the first failure (a printer that is missing fails every label the
        same way, and the queue keeps its order); False tries every job
    :return: one dict per job tried: ``job_id``, ``sleeve_id``, ``title``, ``printed``, ``message``
    """
    results = []
    for job in pending_jobs()[:limit]:
        printed, message = print_label(job, reason="print queue")
        results.append({"job_id": job.job_id, "sleeve_id": job.sleeve_id, "title": job.title,
                        "printed": printed, "message": message})
        if not printed and stop_on_error:
            break
    return results


def jobs_without_sleeve_id(include_failed: bool = False):
    """Finished video jobs that never got a sleeve id, oldest first (the order they went into sleeves)."""
    query = Job.query.filter(Job.sleeve_id.is_(None), Job.disctype.in_(VIDEO_DISC_TYPES))
    if not include_failed:
        query = query.filter(Job.status == JobState.SUCCESS.value)
    return query.order_by(Job.job_id.asc()).all()


def backfill(print_labels: bool = True, dry_run: bool = False, include_failed: bool = False) -> List[dict]:
    """
    Assign sleeve ids to every finished video job without one (they join the print queue), then
    print the queue unless ``print_labels`` is False.

    :param dry_run: only report what would be assigned; nothing is saved or printed
    :return: one dict per job assigned: ``job_id``, ``sleeve_id``, ``title``, ``printed``, ``message``
    """
    results = []
    for job in jobs_without_sleeve_id(include_failed=include_failed):
        entry = {"job_id": job.job_id, "title": job.title, "sleeve_id": None, "printed": False, "message": ""}
        if dry_run:
            entry["sleeve_id"] = next_sleeve_id()
            entry["message"] = "dry run"
        else:
            entry["sleeve_id"] = assign_sleeve_id(job)
            entry["message"] = "sleeve id assigned, label pending"
        results.append(entry)
    if print_labels and not dry_run:
        printed = {r["job_id"]: r for r in print_pending(stop_on_error=True)}
        for entry in results:
            if entry["job_id"] in printed:
                entry.update(printed=printed[entry["job_id"]]["printed"], message=printed[entry["job_id"]]["message"])
    return results
