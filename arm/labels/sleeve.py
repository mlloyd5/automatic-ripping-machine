"""
Sleeve ids and label printing for ARM jobs.

Every finished video rip gets a sleeve id (``S0001``, ``S0002``, ...) the first time a label is
printed for it, from the highest id already in the database; the id is separate from ``job_id`` so
a rebuilt database never hands a code that is already on a sleeve to a second disc. ``box`` records
where the sleeve was filed. Printing goes through :mod:`arm.labels.render` and
:mod:`arm.labels.printer`; a missing printer or library is logged and never raised to the rip.
"""
import logging
import os
import re
import tempfile
import time
from typing import List, Optional, Tuple

from sqlalchemy.exc import OperationalError

import arm.config.config as cfg
from arm.ui import db
from arm.models.job import Job, JobState
from arm.labels import render, printer

SLEEVE_ID_RE = re.compile(r"^([A-Za-z]{0,4})(\d{1,10})$")
"""A sleeve id is a short letter prefix and a number; the number is what increments."""
VIDEO_DISC_TYPES = ("dvd", "bluray")
DISC_TYPE_NAMES = {"dvd": "DVD", "bluray": "Blu-ray", "music": "CD", "data": "Data"}
UHD_HINT_RE = re.compile(r"(?<![A-Za-z0-9])(uhd|4k|ultra\s*hd)(?![A-Za-z0-9])", re.IGNORECASE)
"""Disc labels look like JAWS_UHD and product titles like "Jaws (4K Ultra HD)"; underscores are not word breaks."""
MAX_BOX_LENGTH = 64


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


def id_prefix() -> str:
    prefix = str(setting("LABEL_ID_PREFIX", "S") or "").strip()
    return prefix if SLEEVE_ID_RE.match(prefix + "1") else "S"


def parse_sleeve_id(value) -> Optional[Tuple[str, int]]:
    """``S0042`` -> ``("S", 42)``; ``None`` for anything that is not a sleeve id."""
    if value is None:
        return None
    match = SLEEVE_ID_RE.match(str(value).strip())
    if not match:
        return None
    return match.group(1).upper(), int(match.group(2))


def normalise_sleeve_id(value) -> Optional[str]:
    """What a scanner or a person typed, in the stored spelling: ``s42`` and ``S0042`` both -> ``S0042``."""
    parsed = parse_sleeve_id(value)
    if parsed is None:
        return None
    prefix, number = parsed
    return f"{prefix}{number:04d}"


def next_sleeve_id(prefix: Optional[str] = None) -> str:
    """The id after the highest one in the database with this prefix (``S0001`` when there is none)."""
    prefix = (prefix if prefix is not None else id_prefix()).upper()
    highest = 0
    for (sleeve_id,) in db.session.query(Job.sleeve_id).filter(Job.sleeve_id.isnot(None)).all():
        parsed = parse_sleeve_id(sleeve_id)
        if parsed and parsed[0] == prefix:
            highest = max(highest, parsed[1])
    return f"{prefix}{highest + 1:04d}"


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
    """The job's sleeve id, assigning and saving the next free one when it has none."""
    if job.sleeve_id:
        return job.sleeve_id
    job.sleeve_id = next_sleeve_id()
    if not commit():
        raise RuntimeError(f"could not save sleeve id {job.sleeve_id} for job {job.job_id}")
    logging.info(f"Job {job.job_id} assigned sleeve id {job.sleeve_id}")
    return job.sleeve_id


def set_box(job, box: Optional[str]) -> Optional[str]:
    """Record the box a sleeve was filed in (blank clears it) and save; returns the stored value."""
    cleaned = (box or "").strip()[:MAX_BOX_LENGTH] or None
    job.box = cleaned
    if not commit():
        raise RuntimeError(f"could not save box for job {job.job_id}")
    logging.info(f"Job {job.job_id} ({job.sleeve_id or 'no sleeve id'}) filed in box {cleaned!r}")
    return cleaned


def find_by_sleeve_id(value):
    """The job whose sleeve id matches ``value`` in any spelling, or ``None``."""
    sleeve_id = normalise_sleeve_id(value)
    if not sleeve_id:
        return None
    return Job.query.filter(Job.sleeve_id == sleeve_id).first()


def disc_type_name(job) -> str:
    """``UHD``, ``Blu-ray``, ``DVD``...: ARM calls UHD discs bluray, so the case barcode title is checked."""
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
    or the web UI. The sleeve id is assigned and saved even when printing fails, so the same id is
    printed on the next attempt.
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
        logging.warning(f"Sleeve label {job.sleeve_id} for job {job.job_id} not printed: {error}")
        return False, f"Label {job.sleeve_id} not printed: {error}"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    suffix = f" ({reason})" if reason else ""
    logging.info(f"Printed sleeve label {job.sleeve_id} ({width_mm} x {height_mm} mm) for job {job.job_id}{suffix}"
                 + (f": {output}" if output else ""))
    return True, f"Printed label {job.sleeve_id}"


def auto_print(job) -> Tuple[bool, str]:
    """Called by the ripper after a video rip: print when LABEL_ENABLED and LABEL_AUTO_PRINT are on."""
    if not auto_print_enabled():
        return False, "automatic label printing is off"
    if (job.disctype or "").lower() not in VIDEO_DISC_TYPES:
        return False, f"no sleeve label for {job.disctype} discs"
    return print_label(job, reason="rip complete")


def jobs_without_sleeve_id(include_failed: bool = False):
    """Finished video jobs that never got a sleeve id, oldest first (the order they went into sleeves)."""
    query = Job.query.filter(Job.sleeve_id.is_(None), Job.disctype.in_(VIDEO_DISC_TYPES))
    if not include_failed:
        query = query.filter(Job.status == JobState.SUCCESS.value)
    return query.order_by(Job.job_id.asc()).all()


def backfill(print_labels: bool = True, dry_run: bool = False, include_failed: bool = False) -> List[dict]:
    """
    Assign sleeve ids to every finished video job without one, and print their labels.

    :param print_labels: also print each label (ids are assigned either way)
    :param dry_run: only report what would happen; nothing is saved or printed
    :return: one dict per job: ``job_id``, ``sleeve_id``, ``title``, ``printed``, ``message``
    """
    results = []
    for job in jobs_without_sleeve_id(include_failed=include_failed):
        entry = {"job_id": job.job_id, "title": job.title, "sleeve_id": None, "printed": False, "message": ""}
        if dry_run:
            entry["sleeve_id"] = next_sleeve_id()
            entry["message"] = "dry run"
            results.append(entry)
            continue
        entry["sleeve_id"] = assign_sleeve_id(job)
        if print_labels:
            entry["printed"], entry["message"] = print_label(job, reason="backfill")
        else:
            entry["message"] = "sleeve id assigned, not printed"
        results.append(entry)
    return results
