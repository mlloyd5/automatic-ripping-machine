#!/usr/bin/env python3
"""
ARM completed-rip shipper.

A sidecar for Automatic Ripping Machine (ARM) that moves finished rips from a
ripping box to a media box with rsync.  It runs from a systemd timer, outside
ARM itself, and never touches a job folder until ARM's database says the job
finished successfully.

Priority rules:

* ``COMPLETED_PATH/movies`` and ``COMPLETED_PATH/tv`` are shipped first, to the
  Plex library folders on the media box.
* ``COMPLETED_PATH/unidentified`` is shipped afterwards, one job folder at a
  time, to a separate staging folder and with its own ``--bwlimit``.  Before
  each unidentified folder the recognized folders are re-checked, so a freshly
  finished movie never waits behind more than one unidentified folder.
* An unidentified transfer never starts while a recognized folder is pending,
  including one whose rsync just failed.

Safety rules:

* Source files are removed only by ``rsync --remove-source-files``, each one
  once rsync has transferred it; the empty job folder is pruned only after rsync
  exited 0.  A failed or interrupted run leaves the job folder (and every file
  not yet transferred) in place and the next run resumes it.
* Interrupted files are kept in a ``.rsync-partial`` directory on the
  destination, never under their final name, so Plex never indexes a
  truncated file.
* Nothing already on the media box is overwritten.  ARM names every main
  feature ``<Title (Year)>.mkv``, so a second disc of a title the pool holds
  (the Blu-ray after the UHD, a re-rip) would otherwise replace the feature
  Plex serves, and ``--remove-source-files`` would then delete the only other
  copy.  Before a folder moves, rsync dry runs (a quick probe, then two
  checksum probes when a name is already there) find out which of its files
  the transfer would replace; if any, the folder ships as
  ``<Title (Year)> - <disc label>`` (or ``- job<id>``) beside the original,
  and if that collides too it stays on the ripper and the run exits 1.  The
  dry runs need no listing rights: a write-only destination key allows them.
  The checksum probes compare contents, so a different file that merely has
  the same size and time is a collision too, never mistaken for a resumed one.
* The destination name is decided once, before the transfer starts, and kept
  in ``catalog_spool_path`` as ``job<id>.destination`` until the folder has
  shipped, so a run that is interrupted part-way finishes in the same folder
  instead of probing again with only the files still left.
* A double feature's further films, which ARM files in folders of their own
  beside the job folder (the fork's ``film`` table), ship under the same rules
  as the job folder, each with its own destination name and catalog record.
* A lock file stops two runs overlapping.
* A bonus disc's extras (the fork marks a disc as the bonus disc of an earlier
  job) go into the film's folder on the media box, never beside it.  ARM files
  them into the film's local folder while that is still here, and the folder
  ships as the film's.  Once the film's folder has shipped, ARM files them in
  ``<film folder> - bonus job<id>`` instead, and that folder ships into
  ``<film folder>`` on the media box under the same overwrite rule: files are
  only added, and if one would replace a file there the folder stays.
* Only the standard library plus ``rsync`` are needed.

The shipper may run outside ARM's container (e.g. on the Docker or Proxmox
host): ``arm_completed_path`` is COMPLETED_PATH as ARM writes it into
``job.path`` and ``completed_path`` is the same folder as seen by this script.

Catalog records:

* With ``remote_catalog_path`` set, every recognized folder that ships also
  leaves a small TOML record there: what ARM knew about the disc (title, year,
  type, metadata id, disc label and type, sleeve id, case barcode) and every
  media file shipped with what its Matroska header says (size, duration,
  resolution, codecs, chapters).  A media catalog that keeps discs, cases and
  files as records can be fed from these instead of a separate probe.
* The record is built before the folder's rsync starts, while every file is
  still local, and written to ``catalog_spool_path`` as ``<name>.pending``.
  A run that is interrupted and resumed reuses that pending record instead of
  describing only the files still left.  Once rsync has delivered the whole
  folder the record is renamed to ``<name>.toml``, shipped with its own rsync
  and removed from the spool.  A record whose transfer failed waits in the
  spool and goes on the next run, before anything else.  Its ``library``,
  ``destination`` and file paths name the folder that actually received the
  files, suffix included.
* The record is optional metadata: nothing about building or spooling it can
  stop a folder from shipping.
"""

import argparse
import configparser
import datetime
import fcntl
import logging
import os
import re
import shlex
import socket
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mkvheader  # noqa: E402

RECOGNIZED = ("movies", "tv")
UNIDENTIFIED = "unidentified"
SUCCESS_STATUS = "success"
FINISHED_STATUSES = ("success", "fail")
VIDEO_TYPES = {"movies": "movie", "tv": "series"}
RECORD_SUFFIX = ".toml"
PENDING_SUFFIX = ".pending"
"""A record built before its folder's transfer finished; renamed to RECORD_SUFFIX once the folder has shipped."""
TMP_SUFFIX = ".tmp"
MEDIA_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".webm", ".avi", ".ts")
"""Files a job folder holds that belong in a media catalog; sidecars (disc.jpg, disc.barcode, posters) do not."""
UHD_MIN_WIDTH = 3000
HD_MIN_WIDTH = 1280
SUFFIX_MAX_LENGTH = 64
"""Longest disc label appended to a destination folder name that already holds a file the transfer would replace."""
TRANSFER_ITEM = re.compile(r"^[<>]f\S* (.+)$")
"""An ``rsync --itemize-changes`` line for a file the transfer sends (``<`` to a remote, ``>`` locally)."""
STATE_SUFFIX = ".destination"
"""A job folder's chosen destination name, kept in the spool from before its transfer until it has shipped."""
PROBE_CHECKSUM_RATE = 50 * 1024 * 1024
"""Bytes per second either end is assumed to checksum at least; sizes a collision probe's ``--timeout`` (see
:func:`probe_timeout`)."""

BONUS_COLUMNS = ("bonus_of_job_id",)
"""The fork's bonus disc column, read when present: the job whose film a disc's titles are extras of."""
BONUS_FOLDER = re.compile(r"^(?P<target>.+) - bonus job(?P<job_id>\d+)$")
"""ARM's folder for a bonus disc whose film folder had already shipped: ``<film folder> - bonus job<id>``."""
JOB_COLUMNS = ("job_id", "status", "path", "title", "title_manual", "year", "year_manual", "video_type",
               "imdb_id", "label", "disctype", "disc_barcode", "disc_barcode_title", "sleeve_id", "box",
               "stop_time", "stage", "start_time")
"""Columns read from ARM's job table when present; a stock ARM lacks the fork's disc_barcode and sleeve ones."""
FILM_COLUMNS = ("job_id", "position", "title", "year", "imdb_id", "track_number")
"""Columns read from the fork's film table (the 2nd+ films of a double-feature disc); a stock ARM has no such table."""
FILM_SUB_FOLDER = "movies"
FILM_FILED_SLACK = 15 * 60
"""Seconds after a job's stop_time that a film folder it filed may still have changed (see :func:`film_folder_for`)."""
"""ARM files a double feature's further films beside the job folder, under COMPLETED_PATH/movies."""

DEFAULTS = {
    "db_path": "/home/arm/db/arm.db",
    "completed_path": "/home/arm/media/completed",
    "arm_completed_path": "",
    "remote_host": "",
    "remote_movies_path": "",
    "remote_tv_path": "",
    "remote_unidentified_path": "",
    "remote_catalog_path": "",
    "catalog_spool_path": "",
    "ripper_name": "",
    "bwlimit_recognized": "0",
    "bwlimit_unidentified": "0",
    "ssh_options": "-o BatchMode=yes",
    "rsync_path": "rsync",
    "rsync_extra_args": "",
    "rsync_timeout": "300",
    "lock_file": "/tmp/arm-completed-shipper.lock",
    "post_ship_command": "",
}

log = logging.getLogger("arm-completed-shipper")
_warned = set()
"""Warnings :func:`warn_once` has logged this run; :func:`finished_jobs` runs several times per run."""


class ShipperError(Exception):
    """Configuration or environment problem that stops the run."""


class RecordError(Exception):
    """A catalog record could not be spooled (already logged, with its text); the folder still ships."""


# --------------------------------------------------------------------------- config

def load_config(path, overrides=None):
    """Read the INI config file and return a dict of settings with defaults applied."""
    # No interpolation: post_ship_command may well contain a literal % (URL encoding, date +%s).
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_dict({"shipper": DEFAULTS})
    if path:
        if not os.path.exists(path):
            raise ShipperError(f"config file not found: {path}")
        parser.read(path)
    cfg = dict(parser["shipper"])
    for key, value in (overrides or {}).items():
        if value is not None:
            cfg[key] = str(value)
    # arm_completed_path is the path ARM itself uses (and writes into job.path); it may
    # not exist on this box, so it is only normalised, never resolved.
    cfg["arm_completed_path"] = os.path.normpath(cfg["arm_completed_path"] or cfg["completed_path"])
    cfg["completed_path"] = os.path.realpath(cfg["completed_path"])
    for key in ("remote_movies_path", "remote_tv_path", "remote_unidentified_path"):
        if not cfg[key]:
            raise ShipperError(f"{key} must be set")
    # Catalog records are optional; the spool sits beside movies/, tv/ and unidentified/,
    # which the shipper never lists, and the ripper is named after this box by default.
    cfg["catalog_spool_path"] = os.path.realpath(cfg["catalog_spool_path"]
                                                 or os.path.join(cfg["completed_path"], "catalog"))
    cfg["ripper_name"] = cfg["ripper_name"].strip() or socket.gethostname().split(".")[0]
    return cfg


def remote_root_for(cfg, sub_folder):
    """Return the destination directory for one COMPLETED_PATH sub-folder."""
    return {
        "movies": cfg["remote_movies_path"],
        "tv": cfg["remote_tv_path"],
        UNIDENTIFIED: cfg["remote_unidentified_path"],
    }[sub_folder]


def bwlimit_for(cfg, sub_folder):
    """Return the rsync --bwlimit (KiB/s, 0 = unlimited) for one sub-folder."""
    key = "bwlimit_unidentified" if sub_folder == UNIDENTIFIED else "bwlimit_recognized"
    return int(cfg[key] or 0)


# --------------------------------------------------------------------------- database

def read_jobs(db_path):
    """
    Every job row that has a path, as a dict of the :data:`JOB_COLUMNS` this database has.

    The DB is opened read-only; ARM keeps writing to it while we run.  Columns the
    fork added (sleeve id, barcode) are simply absent on a stock ARM database.
    Each row also carries ``films``: the job's rows of the fork's film table that
    ARM has filed (``track_number`` set) beyond the first, by position; empty for
    an ordinary job and on a database without that table.
    """
    if not os.path.exists(db_path):
        raise ShipperError(f"ARM database not found: {db_path}")
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        present = {row[1] for row in conn.execute("PRAGMA table_info(job)")}
        columns = [column for column in JOB_COLUMNS + BONUS_COLUMNS if column in present]
        if "path" not in columns or "status" not in columns:
            raise ShipperError(f"{db_path} has no job table with path and status columns")
        rows = conn.execute(f"SELECT {', '.join(columns)} FROM job WHERE path IS NOT NULL AND path != ''").fetchall()
        films = read_films(conn)
    finally:
        conn.close()
    jobs = [dict(zip(columns, row)) for row in rows]
    for job in jobs:
        job["films"] = films.get(job.get("job_id"), [])
    return jobs


def read_films(conn):
    """
    ``{job id: [film row, ...]}`` for the filed films after the first of every double-feature job.

    The first film (lowest position) is the job's own title and lives in
    ``job.path``; a film without a ``track_number`` was never filed (no ripped
    title matched its runtime), so it has no folder.  Empty when the database
    has no film table.
    """
    present = {row[1] for row in conn.execute("PRAGMA table_info(film)")}
    if not {"job_id", "position", "title", "track_number"} <= present:
        return {}
    columns = [column for column in FILM_COLUMNS if column in present]
    by_job = {}
    for row in conn.execute(f"SELECT {', '.join(columns)} FROM film ORDER BY job_id, position"):
        film = dict(zip(columns, row))
        by_job.setdefault(film["job_id"], []).append(film)
    return {job_id: [film for film in films[1:] if _clean(film["track_number"]) and _clean(film["title"])]
            for job_id, films in by_job.items()}


def warn_once(message, *args):
    """``log.warning`` a message only the first time this run (see :func:`run`)."""
    key = (message, args)
    if key not in _warned:
        _warned.add(key)
        log.warning(message, *args)


def film_folder_name(film):
    """``Title (Year)``, or the title alone when the year is unknown: ARM's ``Film.folder_name``."""
    year = _clean(film.get("year"))
    if year and year != "0000":
        return f"{film['title']} ({year})"
    return str(film["title"])


def film_folder_for(job, film, completed_path):
    """
    The local folder ARM filed one of a double feature's further films in, or None when there is none to claim.

    ARM files it in ``COMPLETED_PATH/movies/<Title (Year)>``, or, when that
    folder already existed, in ``<Title (Year)>_<job.stage>``, with the stage
    as it was at filing time: the job's start stamp (``time() * 100``), unique
    to it, so a suffixed folder of that name is the film's whenever it exists.
    The plain folder is the film's only if it last changed while the job ran:
    one older than the job's start is the folder that made ARM add the suffix,
    someone else's rip, and is never claimed, even once the suffixed one has
    shipped; one newer than the job's ``stop_time`` (plus
    :data:`FILM_FILED_SLACK`) was filed by a later rip of the same disc.  A
    job without a ``stop_time`` (still running, or killed) keeps claiming.

    ARM's web UI (anything that polls its job list) overwrites ``job.stage``
    with rip progress text on most jobs, so the suffix is often unknown.  The
    job's start is then ``job.start_time``, which nothing overwrites; a plain
    folder older than it is still left alone, and the suffixed folder, which
    cannot be named, stays for a hand transfer.  A numeric stage is preferred
    for the comparison: it is an epoch, while ``start_time`` is ARM's local
    time, read here in the shipper's own timezone (see :func:`_run_window`).
    With neither, nothing is claimed.
    """
    name = film_folder_name(film)
    plain = os.path.realpath(os.path.join(completed_path, FILM_SUB_FOLDER, name))
    stage = _clean(job.get("stage"))
    if stage and stage.isdigit():
        suffixed = os.path.realpath(os.path.join(completed_path, FILM_SUB_FOLDER, f"{name}_{stage}"))
        if os.path.isdir(suffixed):
            return suffixed
    started, stopped = _run_window(job)
    if started is None:
        warn_once("not claiming %s for film %s of job %s: the job has neither a numeric stage nor a start time "
                  "to tell its folder from an older one; move it by hand", plain, film.get("position"),
                  job.get("job_id"))
        return None
    try:
        changed = os.stat(plain).st_mtime
    except OSError:
        return plain  # gone (shipped) or never there: nothing to claim either way
    if changed < started:
        log.debug("not claiming %s for film %s of job %s: it predates the job", plain, film.get("position"),
                  job.get("job_id"))
        return None
    if stopped is not None and changed > stopped + FILM_FILED_SLACK:
        log.debug("not claiming %s for film %s of job %s: it changed after the job finished", plain,
                  film.get("position"), job.get("job_id"))
        return None
    return plain


def _run_window(job):
    """
    ``(start, stop)`` of a job's run in seconds since the epoch; either is None when unknown.

    With a numeric ``stage`` (the start stamp, an epoch) the stop is that plus
    ``stop_time - start_time``, both ARM's local time, so the window does not
    depend on the shipper's timezone; without ``start_time`` the stop is
    ``stop_time`` read in it.  With progress text in ``stage`` both ends come
    from ``start_time`` and ``stop_time``, read in the shipper's timezone.
    """
    stage = _clean(job.get("stage"))
    start_time, stop_time = _datetime(job.get("start_time")), _datetime(job.get("stop_time"))
    if stage and stage.isdigit():
        started = int(stage) / 100
        if start_time and stop_time:
            return started, started + (stop_time - start_time).total_seconds()
        return started, _epoch(job.get("stop_time"))
    return _epoch(job.get("start_time")), _epoch(job.get("stop_time"))


def _datetime(value):
    """A database timestamp as a naive datetime, or None."""
    text = _clean(value)
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def _epoch(value):
    """Seconds since the epoch for a database timestamp (ARM's local time, read as this host's), or None."""
    when = _datetime(value)
    if when is None:
        return None
    try:
        return when.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def film_job(job, film):
    """
    The row a further film's folder ships with: its parent job's, with the film's title, year and metadata id.

    ``film`` (its position) keeps the folder's destination state and catalog
    record apart from the job folder's and the other films'.
    """
    row = {key: value for key, value in job.items() if key != "films"}
    row.update(title=film.get("title"), title_manual=None, year=film.get("year"), year_manual=None,
               imdb_id=film.get("imdb_id"), film=film.get("position"))
    return row


def local_folder_for(job_path, completed_path, arm_completed_path=None):
    """
    Translate ``job.path`` to the same folder on this box, or None when it is not under COMPLETED_PATH.

    ``job.path`` holds the path as ARM sees it (``arm_completed_path``, e.g. a
    path inside ARM's container).  The rest is translated to ``completed_path``
    and resolved so it compares equal to what :func:`pending_folders` returns.
    """
    arm_root = os.path.normpath(arm_completed_path or completed_path)
    prefix = arm_root.rstrip(os.sep) + os.sep
    norm = os.path.normpath(job_path)
    if not norm.startswith(prefix):
        return None
    return os.path.realpath(os.path.join(completed_path, norm[len(prefix):]))


def finished_jobs(db_path, completed_path, arm_completed_path=None):
    """
    Return ``{local job folder: job row}`` for every folder under COMPLETED_PATH that is safe to ship.

    A folder qualifies when at least one job with that path has status
    ``success`` and no job with that path is still unfinished.  The row kept is
    the newest successful job, which is the one whose rip the folder holds.

    A double feature's further films (see :func:`read_films`) count as folders
    of their own, each with its parent job's status and a :func:`film_job` row,
    under the same rules: a film folder that is also another job's path waits
    for that job too.  Only the films of a job whose ``job.path`` is under
    COMPLETED_PATH are looked at, since ARM files them beside it.  A film row
    never represents a folder that is some job's own path: that folder ships
    with the newest successful such job, or not at all.  A folder that is no
    job's own and that films of more than one job claim ships with none of
    them (a warning names the jobs): :func:`film_folder_for` only lets a job
    claim a folder that changed while it ran, so those are jobs whose runs
    overlapped, the same double feature ripped on two drives.
    """
    by_folder = {}
    for job in read_jobs(db_path):
        local = local_folder_for(job["path"], completed_path, arm_completed_path)
        if local is None:
            continue
        by_folder.setdefault(local, []).append(job)
        for film in job["films"]:
            folder = film_folder_for(job, film, completed_path)
            if folder is not None:
                by_folder.setdefault(folder, []).append(film_job(job, film))

    shippable = {}
    for path, rows in by_folder.items():
        seen = {job["status"] for job in rows}
        # Every row waits (a film whose job is still ripping), but a film row stands for its folder only
        # when no job has that folder as its path; otherwise the folder holds that job's rip, not the film's.
        jobs = [job for job in rows if not job.get("film")] or rows
        if SUCCESS_STATUS not in {job["status"] for job in jobs}:
            continue
        if any(status not in FINISHED_STATUSES for status in seen):
            log.debug("skipping %s: another job on that path is still %s", path, sorted(seen))
            continue
        claimants = sorted({job.get("job_id") or 0 for job in jobs if job.get("film")})
        if len(claimants) > 1:
            # Two rips of one double feature at once (two drives): whose film this is cannot be told apart.
            warn_once("not shipping %s: it is no job's own folder and films of jobs %s, whose runs overlapped, all "
                      "claim it; move it by hand", path, ", ".join(str(job_id) for job_id in claimants))
            continue
        successes = [job for job in jobs if job["status"] == SUCCESS_STATUS]
        # A bonus disc's job never stands for a folder it shares with its film's job: ARM filed the
        # bonus disc's extras into the film's folder, which is still the film's rip
        own = [job for job in successes if not job.get("bonus_of_job_id")]
        shippable[path] = max(own or successes, key=lambda job: job.get("job_id") or 0)
    return shippable


def finished_job_paths(db_path, completed_path, arm_completed_path=None):
    """The set of local job folders under COMPLETED_PATH that are safe to ship (see :func:`finished_jobs`)."""
    return set(finished_jobs(db_path, completed_path, arm_completed_path))


def pending_folders(cfg, sub_folder, shippable):
    """List job folders in COMPLETED_PATH/<sub_folder> whose job finished successfully."""
    base = os.path.join(cfg["completed_path"], sub_folder)
    if not os.path.isdir(base):
        return []
    folders = []
    for name in sorted(os.listdir(base)):
        full = os.path.realpath(os.path.join(base, name))
        if not os.path.isdir(full):
            continue
        if full in shippable:
            folders.append(full)
        else:
            log.debug("not shippable yet: %s", full)
    return folders


# --------------------------------------------------------------------------- rsync

def destination(cfg, sub_folder, local_folder, remote_name=None):
    """
    Build the rsync destination string for one job folder.

    ``remote_name`` is the folder name on the media box; it defaults to the job
    folder's own name and differs only when :func:`choose_remote_name` had to
    step aside from a folder that already holds a file the transfer would replace.

    The remote path is deliberately not shell-quoted: rsync is run with ``-s``
    (``--protect-args``), so the path travels inside the rsync protocol and the
    remote shell never parses it.  Quoting it as well would make the quotes part
    of the path on rsync >= 3.2.4, which escapes remote args itself.
    """
    remote_dir = os.path.join(remote_root_for(cfg, sub_folder), remote_name or os.path.basename(local_folder))
    if cfg["remote_host"]:
        return f"{cfg['remote_host']}:{remote_dir}/"
    return remote_dir + os.sep


def remote_shell_args(cfg):
    """``-s -e ssh ...`` for a remote destination, nothing for a local one."""
    if not cfg["remote_host"]:
        return []
    ssh = "ssh"
    if cfg["ssh_options"]:
        ssh += " " + cfg["ssh_options"]
    # -s: send paths inside the protocol, safe from remote shell word splitting/quoting.
    return ["-s", "-e", ssh]


def build_rsync_command(cfg, sub_folder, local_folder, dry_run=False, verbose=False, remote_name=None):
    """Compose the full rsync argv for shipping one job folder."""
    # --partial-dir keeps an interrupted file out of the library folder (under its final
    # name Plex would index a truncated file); a dot-directory is ignored by Plex.
    cmd = [cfg["rsync_path"], "-a", "--partial-dir=.rsync-partial", "--remove-source-files",
           f"--timeout={int(cfg['rsync_timeout'])}"]
    if verbose:
        cmd.append("-v")
    if dry_run:
        cmd.append("--dry-run")
    bwlimit = bwlimit_for(cfg, sub_folder)
    if bwlimit > 0:
        cmd.append(f"--bwlimit={bwlimit}")
    cmd.extend(remote_shell_args(cfg))
    if cfg["rsync_extra_args"]:
        cmd.extend(shlex.split(cfg["rsync_extra_args"]))
    cmd.append(local_folder.rstrip(os.sep) + os.sep)
    cmd.append(destination(cfg, sub_folder, local_folder, remote_name))
    return cmd


def folder_bytes(local_folder):
    """Total size of the files under a folder; a folder that cannot be walked counts as empty."""
    total = 0
    for root, _, files in os.walk(local_folder):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def probe_timeout(cfg, local_folder):
    """
    ``--timeout`` for a collision probe: ``rsync_timeout`` plus the time the checksums may take.

    rsync sends nothing while it checksums a file that exists on both ends with
    the same size (the feature an interrupted run already delivered, 70 GB for
    a UHD rip), and its I/O timeout fires during that silence, so the probe
    gets ``rsync_timeout`` plus one second per :data:`PROBE_CHECKSUM_RATE`
    bytes in the folder.  A ``rsync_timeout`` of 0 (no timeout) stays 0.
    """
    base = int(cfg["rsync_timeout"])
    if base <= 0:
        return 0
    return base + -(-folder_bytes(local_folder) // PROBE_CHECKSUM_RATE)


def build_probe_rsync_command(cfg, sub_folder, local_folder, remote_name, ignore_existing=False, checksum=True):
    """
    rsync argv for one collision probe: a dry run that itemizes what the real transfer would send.

    Same ssh and extra arguments as the transfer (an ``--exclude`` there must
    hold here too), no bandwidth cap, nothing removed, and a ``--timeout`` that
    allows for the checksums (:func:`probe_timeout`).  ``--checksum`` compares
    contents instead of size and time (see :func:`colliding_files`).  Only the
    short-flag cluster differs on the remote command line (``n`` and ``c`` join
    it: ``rsync --server -snlogDtprce.iLsfxCIvu`` with rsync 3.x and ``-s``; ``n``
    alone without ``checksum``), so a forced command written for the transfer
    accepts the probe too.
    """
    cmd = [cfg["rsync_path"], "-a", "--dry-run", "--itemize-changes"]
    if checksum:
        cmd.append("--checksum")
    cmd.append(f"--timeout={probe_timeout(cfg, local_folder) if checksum else int(cfg['rsync_timeout'])}")
    if ignore_existing:
        cmd.append("--ignore-existing")
    cmd.extend(remote_shell_args(cfg))
    if cfg["rsync_extra_args"]:
        cmd.extend(shlex.split(cfg["rsync_extra_args"]))
    cmd.append(local_folder.rstrip(os.sep) + os.sep)
    cmd.append(destination(cfg, sub_folder, local_folder, remote_name))
    return cmd


def transferred_files(itemized_output):
    """
    Names (relative to the transfer root) of the files an ``--itemize-changes`` run says it sends.

    Only the first two characters of the flags are read: a dry run reports files
    inside a folder that does not exist yet with dots instead of pluses (rsync
    over ssh and openrsync both do), so the flags cannot tell a new file from a
    replaced one.  :func:`colliding_files` gets that from a second probe instead.
    """
    names = []
    for line in itemized_output.splitlines():
        match = TRANSFER_ITEM.match(line)
        if match:
            names.append(match.group(1))
    return names


def colliding_files(cfg, sub_folder, local_folder, remote_name):
    """
    Files in the job folder that the real transfer into ``remote_name`` would replace on the media box.

    A quick probe, then two checksum dry runs when a name is already there (see
    below).  The checksum pair: without ``--ignore-existing`` rsync lists every file it would
    send (new ones and existing ones that differ); with it, only the files the
    destination lacks.  The difference is exactly what would be overwritten.
    Both probes run with ``--checksum``: rsync's usual quick check (size and
    time) would pass over a different file that happens to have the source's
    size and time, the transfer would skip it, and ``--remove-source-files``
    would then delete the only copy of the new rip.  A file an earlier,
    interrupted run already delivered has the source's contents and appears in
    neither list, so a resume is not a collision.  The transfer itself keeps
    the quick check.  Returns the sorted names, or None when a probe failed.

    rsync's sender checksums every file it lists, not only those the receiver
    has, so each ``--checksum`` probe reads the whole folder.  A probe with
    ``--ignore-existing`` and no ``--checksum`` comes first: when it would send
    every regular file in the folder, the destination has none of them and
    nothing can collide, and the checksum probes are skipped.  They run only
    when a name already exists there (a resume or a collision), or when the
    names do not compare (an ``--exclude`` in ``rsync_extra_args``, a name rsync
    escapes in its output), which only costs the reads.
    """
    def probe(ignore_existing, checksum=True):
        cmd = build_probe_rsync_command(cfg, sub_folder, local_folder, remote_name, ignore_existing, checksum)
        log.debug("rsync argv: %s", " ".join(shlex.quote(part) for part in cmd))
        result = subprocess.run(cmd, stdout=subprocess.PIPE, universal_newlines=True)
        if result.returncode != 0:
            log.error("rsync probe exited %d for %s -> %s; leaving local copy for the next run",
                      result.returncode, local_folder, cmd[-1])
            return None
        return set(transferred_files(result.stdout))

    absent = probe(True, checksum=False)
    if absent is None:
        return None
    if absent >= set(folder_files(local_folder)):
        return []
    would_send = probe(False)
    new_only = probe(True) if would_send is not None else None
    if new_only is None:
        return None
    return sorted(would_send - new_only)


def folder_files(local_folder):
    """Relative paths (``/`` separated) of the regular files under a folder, as rsync itemizes them."""
    found = []
    for root, _, files in os.walk(local_folder):
        for name in files:
            full = os.path.join(root, name)
            if os.path.isfile(full) and not os.path.islink(full):
                found.append(os.path.relpath(full, local_folder).replace(os.sep, "/"))
    return found


def bonus_remote_name(local_folder, job):
    """
    The film folder on the media box that a bonus disc's own folder ships into, or None for any other folder.

    ARM names it ``<film folder> - bonus job<id>`` (:data:`BONUS_FOLDER`) when
    the film's folder had shipped before the bonus disc finished.  Only a folder
    whose row is that job, marked as a bonus disc, counts.
    """
    if not job or not job.get("bonus_of_job_id"):
        return None
    match = BONUS_FOLDER.match(os.path.basename(local_folder.rstrip(os.sep)))
    if match is None or int(match.group("job_id")) != job.get("job_id"):
        return None
    return match.group("target")


def arm_folder_name(local_folder, job=None, sub_folder=None):
    """
    The name the media box should know a job folder by: its own, less a suffix that only avoided a local clash.

    That is the folder's basename, except for a folder ARM filed as
    ``<Title (Year)>_<stage>`` because ``<Title (Year)>`` already existed in
    completed: a double feature's further film (see :func:`film_folder_for`),
    or a job folder whose name an earlier job of the same disc took (a failed
    rip leaves its empty folder behind).  It ships as the plain
    ``<Title (Year)>``, the name ARM gave the main feature inside it.

    An unidentified folder (``sub_folder`` :data:`UNIDENTIFIED`) always keeps
    its own name: ARM names it after the disc label and stores the label as
    the job's title too, so a second ``DVD_VIDEO`` disc's ``DVD_VIDEO_<stage>``
    is a different disc, not a re-rip of the first.
    """
    folder = os.path.basename(local_folder.rstrip(os.sep))
    for plain in job_folder_titles(job, sub_folder):
        if folder.startswith(plain + "_") and folder[len(plain) + 1:].isdigit():
            return plain
    return folder


def job_folder_titles(job, sub_folder=None):
    """
    The plain folder names ARM may have filed a job folder under: ARM's ``fix_job_title`` for the manual
    title and the identified one, or :func:`film_folder_name` for a double feature's further film.
    None for an unidentified folder, whose "title" is only the disc label (see :func:`arm_folder_name`).
    """
    if not job or sub_folder == UNIDENTIFIED:
        return []
    if job.get("film"):
        return [film_folder_name(job)] if _clean(job.get("title")) else []
    year = _clean(job.get("year"))
    titles = [_clean(job.get("title_manual")), _clean(job.get("title"))]
    return [f"{title} ({year})" if year and year != "0000" else title for title in titles if title]


def remote_folder_names(local_folder, job, sub_folder=None):
    """
    Destination folder names to try for one job folder, in order.

    ARM's own name first (:func:`arm_folder_name`).  Then, for a folder that
    already holds a file the transfer would replace, the same name with the
    disc label appended and finally with the job id, so a second disc of a
    title the pool already has (the Blu-ray after the UHD, a re-rip) lands
    beside the first copy, never over it.  Plex groups both as versions of the one title.

    The label is skipped for an unidentified folder, which ARM names after the
    label itself (``DVD_VIDEO - DVD_VIDEO`` says nothing), and whenever it
    matches the folder name; those go straight to the job id.

    A bonus disc's own folder (:func:`bonus_remote_name`) has the film's folder
    as its only name: its extras belong in there, so when they would replace a
    file there the folder stays rather than landing beside it.
    """
    target = bonus_remote_name(local_folder, job)
    if target:
        return [target]
    folder = arm_folder_name(local_folder, job, sub_folder)
    names = [folder]
    label = _clean(job.get("label")) if job and sub_folder != UNIDENTIFIED else None
    if label:
        safe = " ".join(re.sub(r"[\\/\x00-\x1f\x7f]+", " ", label).split())[:SUFFIX_MAX_LENGTH].strip()
        if safe and safe != folder:
            names.append(f"{folder} - {safe}")
    job_id = job.get("job_id") if job else None
    if job_id:
        names.append(f"{folder} - job{job_id}")
    return names


def choose_remote_name(cfg, sub_folder, local_folder, job):
    """
    The destination folder name for one job folder: the first of :func:`remote_folder_names`
    that holds no file the transfer would replace.  None when all collide or a probe failed.
    """
    for name in remote_folder_names(local_folder, job, sub_folder):
        collisions = colliding_files(cfg, sub_folder, local_folder, name)
        if collisions is None:
            return None
        if not collisions:
            return name
        log.warning("%s already holds %s; not overwriting", destination(cfg, sub_folder, local_folder, name),
                    ", ".join(collisions))
    log.error("every destination name for %s already holds a file it would replace; leaving it for the next run",
              local_folder)
    return None


def destination_state_path(cfg, job):
    """
    ``<catalog_spool_path>/job<id>.destination``: where a job folder's chosen destination name is kept.

    A double feature's further film keeps its own, ``job<id>-film<position>.destination``.
    """
    return os.path.join(cfg["catalog_spool_path"], f"{_job_key(job)}{STATE_SUFFIX}")


def read_destination_state(cfg, job, source):
    """
    The destination name an earlier run chose for the job folder ``source`` (see :func:`library_path`), or None.

    A state file left by another folder with the same job id (an id reused
    after an ARM database reset) is ignored, with a warning.
    """
    path = destination_state_path(cfg, job)
    if not os.path.exists(path):
        return None
    recorded = read_record_key(path, "source")
    name = read_record_key(path, "remote_name")
    if recorded != source or not name or "/" in name or name in (".", ".."):
        log.warning("ignoring %s: it names %s -> %s, not a destination for %s", path, recorded, name, source)
        return None
    return name


def save_destination_state(cfg, job, source, remote_name):
    """Keep the destination name chosen for a job folder, atomically.  Returns False when the spool is not writable."""
    path = destination_state_path(cfg, job)
    text = "\n".join(["# Destination folder the ARM completed-rip shipper chose for one job folder; removed once",
                      "# the folder has shipped.  An interrupted transfer resumes into the same folder."]
                     + _toml_lines([("source", source), ("remote_name", remote_name)])) + "\n"
    try:
        os.makedirs(cfg["catalog_spool_path"], exist_ok=True)
        _write_atomically(path, text)
    except OSError as err:
        log.error("could not keep the destination name of %s in %s: %s", source, path, err)
        return False
    return True


def clear_destination_state(cfg, job):
    """Forget a job folder's destination name once it has shipped."""
    path = destination_state_path(cfg, job)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as err:
        log.warning("could not remove %s: %s", path, err)


def settle_destination_states(cfg, dry_run=False):
    """
    Drop destination names whose job folder is no longer in completed_path.

    That happens when a run was killed after the folder shipped but before the
    name was forgotten, and when a folder was removed by hand.  A name whose
    folder is still there is left for :func:`ship_folder` to resume with, and
    so is every name while the folder's category directory is missing
    (completed_path not mounted) or the state file cannot be read.
    """
    for path in _spooled(cfg, STATE_SUFFIX):
        source = read_record_key(path, "source")
        if not source:
            log.warning("%s names no source folder; leaving it in the spool", path)
            continue
        present = source_folder_present(cfg, source)
        if present:
            continue
        if present is None:
            log.warning("%s has no %s folder (not mounted?); leaving destination name %s in the spool",
                        cfg["completed_path"], source.split("/", 1)[0], os.path.basename(path))
            continue
        if dry_run:
            log.info("dry run: would drop %s (%s is gone)", path, source)
            continue
        log.info("%s is no longer in %s; dropping its destination name %s", source, cfg["completed_path"],
                 os.path.basename(path))
        try:
            os.remove(path)
        except OSError as err:
            log.warning("could not remove %s: %s", path, err)


def persisted_remote_name(cfg, sub_folder, source, job):
    """
    The destination name an earlier, unfinished run chose for a job folder, or None when there was none.

    Read from the job's ``.destination`` state file or, failing that, from the
    ``library`` key of its pending catalog record.  When the two disagree the
    state file wins, with a warning.
    """
    if job is None:
        return None
    state = read_destination_state(cfg, job, source)
    pending = pending_record_path(cfg, job, source)
    recorded = None
    if pending:
        library = read_record_key(pending, "library") or ""
        if library.startswith(sub_folder + "/") and "/" not in library[len(sub_folder) + 1:]:
            recorded = library[len(sub_folder) + 1:] or None
    if state and recorded and state != recorded:
        log.warning("%s ships as %s according to %s, but catalog record %s says %s; following the former",
                    source, state, os.path.basename(destination_state_path(cfg, job)), os.path.basename(pending),
                    recorded)
    return state or recorded


def resolve_remote_name(cfg, sub_folder, local_folder, job, dry_run=False, stats=None):
    """
    The destination folder name for one job folder, decided once and kept until the folder has shipped.

    A name an earlier, interrupted run chose (see :func:`persisted_remote_name`)
    is reused without probing ARM's own name again: with part of the folder
    already delivered and removed, that probe would see only the files still
    left and could send them somewhere else.  The kept name is still probed, so
    a file someone else put there meanwhile is caught (one this folder already
    delivered is identical and counts as a resume); then the folder stays and
    the run exits 1.  Otherwise :func:`choose_remote_name` picks a name, which
    is written to the spool before the transfer starts.  When it cannot be
    written, a folder shipping under ARM's own name goes anyway (a later probe
    picks the same name for a resumed transfer) but a renamed one stays.
    Returns None when the folder must not ship this run.
    """
    source = library_path(sub_folder, local_folder)
    persisted = persisted_remote_name(cfg, sub_folder, source, job)
    if persisted is not None:
        collisions = colliding_files(cfg, sub_folder, local_folder, persisted)
        if collisions is None:
            return None
        if collisions:
            log.error("%s, the destination an earlier run chose for %s, now holds %s, which this transfer would "
                      "replace; leaving it for the next run (removing %s and the job's .pending catalog record "
                      "from the spool lets the shipper choose again, which splits the rip)",
                      destination(cfg, sub_folder, local_folder, persisted), local_folder,
                      ", ".join(collisions), destination_state_path(cfg, job))
            return None
        log.info("resuming %s into %s, the destination an earlier run chose", local_folder, persisted)
        return persisted
    remote_name = choose_remote_name(cfg, sub_folder, local_folder, job)
    if remote_name is None or dry_run or job is None:
        return remote_name
    if not save_destination_state(cfg, job, source, remote_name):
        if remote_name != (bonus_remote_name(local_folder, job) or arm_folder_name(local_folder, job, sub_folder)):
            log.error("not shipping %s as %s without a record of that name; leaving it for the next run",
                      local_folder, remote_name)
            return None
        if stats is not None:
            stats["failed"] += 1
    return remote_name


def prune_empty_dirs(folder):
    """Remove `folder` and any empty sub-directories, bottom up.  Non-empty dirs are left alone."""
    for root, dirs, files in os.walk(folder, topdown=False):
        if files:
            log.warning("leaving %s in place, files remain: %s", root, files)
            continue
        try:
            os.rmdir(root)
        except OSError as err:
            log.warning("could not remove %s: %s", root, err)


def run_post_ship_command(cfg, sub_folder, local_folder, remote_name=None):
    """Run the optional hook after a folder has shipped (e.g. to trigger a Plex scan)."""
    hook = cfg["post_ship_command"]
    if not hook:
        return
    # SHIPPER_FOLDER is ARM's name for the job folder; SHIPPER_DESTINATION is where it landed,
    # which carries a suffix when that name was already taken.
    env = dict(os.environ, SHIPPER_TYPE=sub_folder, SHIPPER_FOLDER=os.path.basename(local_folder),
               SHIPPER_DESTINATION=destination(cfg, sub_folder, local_folder, remote_name))
    result = subprocess.run(["/bin/sh", "-c", hook], env=env)
    if result.returncode != 0:
        log.warning("post_ship_command exited %d for %s", result.returncode, local_folder)


def ship_folder(cfg, sub_folder, local_folder, dry_run=False, verbose=False, job=None, stats=None):
    """
    Rsync one job folder and remove the local copy on success.  Returns True on success.

    With ``job`` (its ARM database row) and ``remote_catalog_path`` set, a
    catalog record is spooled as ``.pending`` before rsync starts (or the one an
    interrupted earlier run left is reused), renamed to ``.toml`` once the
    folder has shipped completely, and shipped after the folder.  A record that
    fails to ship stays in the spool for the next run; the folder, already on
    the media box, still counts as shipped, but the run exits 1
    (``stats["failed"]``) so the problem is noticed.  A record that cannot be
    built or spooled never stops the folder itself from shipping.

    Nothing on the media box is overwritten: :func:`choose_remote_name` probes
    the destination first and steps aside to ``<name> - <disc label>`` (then
    ``<name> - job<id>``) when the folder already holds a file this transfer
    would replace.  When every name collides, or a probe fails, the folder stays
    and the run exits 1.  The record names the folder that actually received the
    files.  The name is decided once (:func:`resolve_remote_name`) and kept
    until the folder has shipped, so an interrupted transfer resumes into the
    same folder.
    """
    remote_name = resolve_remote_name(cfg, sub_folder, local_folder, job, dry_run, stats)
    if remote_name is None:
        return False
    target = bonus_remote_name(local_folder, job)
    renamed = remote_name != (target or arm_folder_name(local_folder, job, sub_folder))
    if renamed:
        log.warning("shipping %s as %s instead", local_folder, remote_name)
    elif target:
        log.info("%s holds a bonus disc's extras; adding them to %s", local_folder, remote_name)
    pending = None
    if job is not None and cfg["remote_catalog_path"] and sub_folder != UNIDENTIFIED:
        pending = prepare_record_safely(cfg, sub_folder, local_folder, job, remote_name, dry_run, stats)
    cmd = build_rsync_command(cfg, sub_folder, local_folder, dry_run=dry_run, verbose=verbose, remote_name=remote_name)
    log.info("shipping [%s] %s -> %s", sub_folder, local_folder, cmd[-1])
    log.debug("rsync argv: %s", " ".join(shlex.quote(part) for part in cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        log.error("rsync exited %d for %s; leaving local copy for the next run", result.returncode, local_folder)
        return False
    if dry_run:
        log.info("dry run: would remove %s", local_folder)
        return True
    prune_empty_dirs(local_folder)
    if os.path.exists(local_folder):
        log.error("rsync succeeded but %s still has content; not treating as shipped", local_folder)
        return False
    log.info("shipped and removed %s", local_folder)
    if job is not None:
        clear_destination_state(cfg, job)
    if renamed and stats is not None:
        stats["renamed"] += 1
    if pending:
        finished = finish_record(pending)
        if finished and ship_record(cfg, finished):
            if stats is not None:
                stats["records"] += 1
        elif stats is not None:
            stats["failed"] += 1
    run_post_ship_command(cfg, sub_folder, local_folder, remote_name)
    return True


def prepare_record_safely(cfg, sub_folder, local_folder, job, remote_name, dry_run=False, stats=None):
    """:func:`prepare_record`, but a record that cannot be built or spooled is logged and counted, never raised."""
    try:
        return prepare_record(cfg, sub_folder, local_folder, job, remote_name, dry_run=dry_run)
    except Exception as err:  # optional metadata: never block the transfer
        if not isinstance(err, RecordError):
            log.exception("could not prepare the catalog record for %s; shipping the folder without it",
                          local_folder)
        if stats is not None:
            stats["failed"] += 1
    return None


# --------------------------------------------------------------------------- catalog records

def _toml_string(value):
    """A TOML basic string literal for ``value``."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    escaped = "".join(ch if ch >= " " and ch != "\x7f" else "\\u%04X" % ord(ch) for ch in escaped)
    return f'"{escaped}"'


def _toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return _toml_string(value)


def _toml_lines(pairs):
    """``key = value`` lines for the (key, value) pairs whose value is not None."""
    return [f"{key} = {_toml_value(value)}" for key, value in pairs if value is not None]


def _clean(value):
    """A stripped string, or None for an empty or missing database value."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _job_year(job):
    year = _clean(job.get("year_manual")) or _clean(job.get("year"))
    return int(year) if year and year.isdigit() and year != "0000" else None


def _metadata_id(job):
    """(key, value) for ARM's metadata id: ``imdb`` for tt..., ``tmdb`` for a bare number, else nothing."""
    ident = _clean(job.get("imdb_id"))
    if not ident:
        return None, None
    if ident.lower().startswith("tt") and ident[2:].isdigit():
        return "imdb", ident.lower()
    if ident.isdigit():
        return "tmdb", int(ident)
    return "metadata_id", ident


def folder_media_files(local_folder):
    """Relative paths (``/`` separated, sorted) of the media files under a job folder."""
    found = []
    for root, dirs, files in os.walk(local_folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith(".") or not name.lower().endswith(MEDIA_EXTENSIONS):
                continue
            relative = os.path.relpath(os.path.join(root, name), local_folder)
            found.append(relative.replace(os.sep, "/"))
    return found


def disc_format(job, rows):
    """``dvd``, ``bd`` or ``uhd`` from ARM's disc type and the widest picture in the shipped files."""
    widths = []
    for row in rows:
        if row["resolution"]:
            try:
                widths.append(int(row["resolution"].split("x")[0]))
            except ValueError:
                pass
    widest = max(widths) if widths else 0
    disctype = (_clean(job.get("disctype")) or "").lower()
    if disctype == "dvd":
        return "dvd"
    if widest >= UHD_MIN_WIDTH:
        return "uhd"
    if disctype == "bluray" or widest >= HD_MIN_WIDTH:
        return "bd"
    return "dvd" if widest else "bd"


def _job_key(job):
    """``job<id>``, or ``job<id>-film<position>`` for a double feature's further film (see :func:`film_job`)."""
    key = f"job{job.get('job_id') or 0}"
    if job.get("film"):
        key += f"-film{job['film']}"
    return key


def _job_tag(job):
    """``-job<id>`` (``-job<id>-film<position>``), the part of a record name that identifies its job folder."""
    return f"-{_job_key(job)}"


def record_name(cfg, job, suffix=RECORD_SUFFIX):
    """``<stop time>-<ripper>-job<id>.toml``: sorts by rip time and is the same whenever the job is re-shipped."""
    stamp = _clean(job.get("stop_time"))
    try:
        when = datetime.datetime.fromisoformat(stamp) if stamp else datetime.datetime.now()
    except ValueError:
        when = datetime.datetime.now()
    return f"{when.strftime('%Y%m%d-%H%M%S')}-{cfg['ripper_name']}{_job_tag(job)}{suffix}"


def library_path(sub_folder, folder):
    """
    ``<sub folder>/<folder name>`` (a path's last component counts).

    For the destination name it is a record's ``library``; for the local job
    folder it is the record's ``source`` and the state file's, which is also
    where the folder sits under completed_path.
    """
    return f"{sub_folder}/{os.path.basename(folder.rstrip(os.sep))}"


def catalog_record(cfg, sub_folder, local_folder, job, remote_name=None):
    """
    Build the catalog record for one job folder about to ship.

    Reads the Matroska header of every media file in the folder, so it must run
    before rsync starts removing them.  ``remote_name`` is the folder on the
    media box that receives the files (:func:`arm_folder_name` unless that
    was taken); ``library``, ``destination`` and the file paths name it, and
    ``source`` the local folder.
    Returns ``(file name, TOML text)`` (the name ends in ``.toml``), or None
    when the folder holds no media file (nothing for a catalog to register).
    """
    files = folder_media_files(local_folder)
    if not files:
        log.warning("no media file in %s; no catalog record written", local_folder)
        return None
    folder = os.path.basename(local_folder.rstrip(os.sep))
    remote_name = remote_name or arm_folder_name(local_folder, job, sub_folder)
    library = library_path(sub_folder, remote_name)
    # ARM names the main feature after its own folder, whatever the destination is called, and after
    # the plain name in a folder that carries a _<stage> suffix.
    feature = arm_folder_name(local_folder, job, sub_folder)
    rows = []
    for relative in files:
        row = mkvheader.probe_row(os.path.join(local_folder, relative))
        stem = relative.rsplit(".", 1)[0]
        row["path"] = f"{library}/{relative}"
        row["main"] = "/" not in relative and stem == feature
        if not row["probed"]:
            log.debug("%s is not Matroska or unreadable; listed by size only", relative)
        rows.append(row)
    id_key, id_value = _metadata_id(job)
    title = _clean(job.get("title_manual")) or _clean(job.get("title")) or folder
    # A bonus disc's own folder holds only extras of the film it ships into, whatever its files look like
    # (a disc marked once its rip had started was ripped with MAINFEATURE on).  Not keyed on
    # bonus_of_job_id alone: a folder with no job of its own but the bonus disc's (the later film of a
    # double feature) still holds that film's feature.
    bonus_of = int(job["bonus_of_job_id"]) if bonus_remote_name(local_folder, job) else None
    if bonus_of is not None:
        role = "bonus"
    else:
        role = "feature" if sub_folder == "tv" or any(row["main"] for row in rows) else "bonus"
    lines = ["# Catalog record written by the ARM completed-rip shipper for one shipped rip.",
             "# What the ripper knew about the disc, then every media file it delivered with what the",
             "# file's Matroska header says.  Consumed by the media catalog's import; safe to re-import."]
    lines += _toml_lines([
        ("ripper", cfg["ripper_name"]),
        ("job", int(job.get("job_id") or 0)),
        ("film", int(job["film"]) if job.get("film") else None),
        ("shipped", datetime.datetime.now().replace(microsecond=0).isoformat()),
        ("type", VIDEO_TYPES.get(sub_folder, _clean(job.get("video_type")) or "unknown")),
        ("title", title),
        ("year", _job_year(job)),
        (id_key, id_value),
        ("library", library),
        ("destination", os.path.join(remote_root_for(cfg, sub_folder), remote_name)),
        ("source", library_path(sub_folder, folder)),
        ("label", _clean(job.get("label")) or ""),
        ("format", disc_format(job, rows)),
        ("role", role),
        ("bonus_of", bonus_of),
        ("sleeve", _clean(job.get("sleeve_id"))),
        ("box", _clean(job.get("box"))),
        ("barcode", _clean(job.get("disc_barcode"))),
        ("barcode_title", _clean(job.get("disc_barcode_title"))),
    ])
    for row in rows:
        lines += ["", "[[files]]"] + _toml_lines([
            ("path", row["path"]),
            ("main", row["main"]),
            ("size", int(row["size"])),
            ("seconds", int(row["seconds"]) if row["seconds"] else None),
            ("resolution", row["resolution"] or None),
            ("video", row["video"] or None),
            ("audio", row["audio"] or None),
            ("subs", int(row["subs"]) if row["subs"] else None),
            ("chapters", int(row["chapters"]) if row["chapters"] else None),
            ("title", row["title"] or None),
        ])
    return record_name(cfg, job), "\n".join(lines) + "\n"


def _write_atomically(path, text):
    """Write ``text`` to ``path`` via ``path.tmp`` and ``os.replace``, so a kill never leaves a truncated file."""
    tmp = path + TMP_SUFFIX
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def spool_record(cfg, name, text):
    """
    Write a record into the spool atomically.  Returns its path, or None when the spool is not writable.

    On failure the record's text is logged at error level, so it can still be
    recovered from the journal.
    """
    path = os.path.join(cfg["catalog_spool_path"], name)
    try:
        os.makedirs(cfg["catalog_spool_path"], exist_ok=True)
        _write_atomically(path, text)
    except OSError as err:
        log.error("could not write catalog record %s: %s; its text follows so it can be recovered:\n%s",
                  path, err, text)
        return None
    log.info("catalog record %s written", path)
    return path


_TOML_ESCAPES = {'"': '"', "\\": "\\", "b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r"}


def _toml_unescape(body):
    """The value of a TOML basic string's body (between the quotes), as :func:`_toml_string` escapes it."""
    def replace(match):
        code = match.group(1)
        if code[0] in "uU":
            return chr(int(code[1:], 16))
        return _TOML_ESCAPES.get(code, code)
    return re.sub(r'\\(u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)', replace, body)


def read_record_key(path, key):
    """
    One top-level key of a record file; None when the key or file is absent or the file is unreadable.

    A string value comes back unescaped; any other value (a number, a boolean)
    as its TOML text.  Only the top-level keys before the first ``[[files]]``
    table are looked at.  Enough TOML for the records this shipper writes,
    without needing ``tomllib`` (Python 3.11+).
    """
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("["):
                    break
                name, sep, value = line.partition(" = ")
                if not sep or name != key:
                    continue
                if len(value) >= 2 and value[0] == value[-1] == '"':
                    return _toml_unescape(value[1:-1])
                return value
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError) as err:
        log.warning("could not read catalog record %s: %s", path, err)
    return None


def _spooled(cfg, suffix):
    """Spool entries ending in ``suffix``, oldest name first."""
    spool = cfg["catalog_spool_path"]
    if not os.path.isdir(spool):
        return []
    return [os.path.join(spool, name) for name in sorted(os.listdir(spool)) if name.endswith(suffix)]


def record_source(path):
    """
    The local job folder a record was built from, as ``<sub folder>/<job folder>``.

    Its ``source`` key; a record written before that key existed falls back to
    ``library``, which then named the same folder.
    """
    return read_record_key(path, "source") or read_record_key(path, "library")


def pending_record_path(cfg, job, source=None):
    """
    The ``.pending`` record an earlier run spooled for ``job``, or None.

    Matched on the ``-job<id>.pending`` end of the name only, so a changed
    ripper name or rip-time format cannot orphan it.  With ``source`` (see
    :func:`library_path`) a pending record built from another folder (a job id
    reused after an ARM database reset) is not returned.  The record's
    ``library`` is not compared: it names the destination folder, which carries
    a suffix when ARM's name was taken.  When several match, the newest name
    wins.
    """
    tail = _job_tag(job) + PENDING_SUFFIX
    matches = [path for path in _spooled(cfg, tail) if source is None or record_source(path) == source]
    return matches[-1] if matches else None


def retarget_record(path, cfg, sub_folder, remote_name):
    """
    Point a pending record's ``library``, ``destination`` and file paths at ``remote_name``.

    Only needed when a pending record and the state file disagree about the
    destination (the state file wins).

    :raises RecordError: when the record cannot be rewritten (logged)
    """
    old = read_record_key(path, "library") or ""
    library = library_path(sub_folder, remote_name)
    replacements = {"library": library, "destination": os.path.join(remote_root_for(cfg, sub_folder), remote_name)}
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().split("\n")
        for index, line in enumerate(lines):
            key, sep, value = line.partition(" = ")
            if sep and key in replacements:
                lines[index] = _toml_lines([(key, replacements[key])])[0]
            elif sep and key == "path" and len(value) >= 2 and value[0] == value[-1] == '"':
                current = _toml_unescape(value[1:-1])
                if old and current.startswith(old + "/"):
                    lines[index] = _toml_lines([("path", library + current[len(old):])])[0]
        _write_atomically(path, "\n".join(lines))
    except OSError as err:
        log.error("could not point catalog record %s at %s: %s", path, library, err)
        raise RecordError(path) from err
    log.warning("catalog record %s now names %s, not %s", os.path.basename(path), library, old)


def prepare_record(cfg, sub_folder, local_folder, job, remote_name=None, dry_run=False):
    """
    The ``.pending`` record for a folder about to ship: the one an earlier run spooled, or a new one.

    Called before the folder's rsync starts, so a new record describes every
    file even when this transfer is interrupted and resumed later.  Returns
    the pending record's path, or None when there is nothing to ship (no media
    file, or a dry run).

    :raises RecordError: when the spool is not writable (the record's text has been logged)
    """
    existing = pending_record_path(cfg, job, library_path(sub_folder, local_folder))
    if existing:
        log.info("reusing catalog record %s from an earlier, unfinished transfer", existing)
        remote_name = remote_name or arm_folder_name(local_folder, job, sub_folder)
        if read_record_key(existing, "library") != library_path(sub_folder, remote_name):
            if dry_run:
                log.info("dry run: would point catalog record %s at %s", existing, remote_name)
            else:
                retarget_record(existing, cfg, sub_folder, remote_name)
        return existing
    built = catalog_record(cfg, sub_folder, local_folder, job, remote_name)
    if built is None:
        return None
    name, text = built
    name = name[:-len(RECORD_SUFFIX)] + PENDING_SUFFIX
    if dry_run:
        log.info("dry run: would write catalog record %s", name)
        log.debug("catalog record:\n%s", text)
        return None
    path = spool_record(cfg, name, text)
    if path is None:
        raise RecordError(name)
    return path


def finish_record(pending_path):
    """
    Turn a ``.pending`` record into a ``.toml`` one once its folder has shipped.  Returns the new path or None.

    ``shipped`` is set to now, the time the transfer finished.  Both steps use
    ``os.replace``, so a kill leaves either the (possibly updated) ``.pending``
    or the ``.toml``, never a truncated file.
    """
    final = pending_path[:-len(PENDING_SUFFIX)] + RECORD_SUFFIX
    try:
        with open(pending_path, encoding="utf-8") as handle:
            text = handle.read()
        stamp = _toml_lines([("shipped", datetime.datetime.now().replace(microsecond=0).isoformat())])[0]
        text, count = re.subn(r"(?m)^shipped = .*$", lambda _: stamp, text, count=1)
        if count:
            _write_atomically(pending_path, text)
        os.replace(pending_path, final)
    except OSError as err:
        log.error("could not finish catalog record %s: %s; the next run retries it", pending_path, err)
        return None
    return final


def source_folder_present(cfg, source):
    """
    Whether the job folder ``source`` (``<sub folder>/<folder>``, see :func:`library_path`) is still in completed_path.

    True when it is, False when it has gone, and None when its category folder
    (``movies``, ``tv``, ``unidentified``) is itself missing: then completed_path
    is probably not mounted, which says nothing about the job folder, and
    nothing that treats "gone" as "shipped" may act on it.
    """
    category = source.split("/", 1)[0]
    if not os.path.isdir(os.path.join(cfg["completed_path"], category)):
        return None
    return os.path.isdir(os.path.join(cfg["completed_path"], source))


def settle_pending_records(cfg, dry_run=False):
    """
    Finish ``.pending`` records whose job folder is no longer in completed_path.

    That happens when a run was killed after the folder shipped but before its
    record was renamed, and when a folder was removed by hand.  The shipper
    cannot list the media box to tell the two apart, so it assumes the folder
    shipped: the record becomes ``.toml`` (and ships with the others), with a
    warning naming it.  A pending record whose folder is still there is left
    for :func:`ship_folder` to reuse, and so is every pending record while the
    folder's category directory is missing (completed_path not mounted), since
    then nothing can be said about the folder.
    """
    for pending in _spooled(cfg, PENDING_SUFFIX):
        # The record's source names the local job folder; its library names the destination,
        # which differs when ARM's name was taken on the media box.
        source = record_source(pending)
        if not source:
            log.warning("catalog record %s names no source folder; leaving it in the spool", pending)
            continue
        present = source_folder_present(cfg, source)
        if present:
            continue
        if present is None:
            log.warning("%s has no %s folder (not mounted?); leaving catalog record %s in the spool",
                        cfg["completed_path"], source.split("/", 1)[0], os.path.basename(pending))
            continue
        if dry_run:
            log.info("dry run: would finish catalog record %s (%s is gone)", pending, source)
            continue
        log.warning("%s is no longer in %s; assuming it shipped and finishing its catalog record %s",
                    source, cfg["completed_path"], os.path.basename(pending))
        finish_record(pending)


def record_destination(cfg):
    remote_dir = cfg["remote_catalog_path"].rstrip("/") + "/"
    return f"{cfg['remote_host']}:{remote_dir}" if cfg["remote_host"] else remote_dir


def build_record_rsync_command(cfg, record_path):
    """rsync argv for one spooled record: the same ssh and timeout as the folders, no bwlimit, source kept."""
    cmd = [cfg["rsync_path"], "-t", f"--timeout={int(cfg['rsync_timeout'])}"]
    cmd.extend(remote_shell_args(cfg))
    cmd.append(record_path)
    cmd.append(record_destination(cfg))
    return cmd


def ship_record(cfg, record_path):
    """Copy one spooled record to remote_catalog_path and drop it from the spool.  Returns True on success."""
    cmd = build_record_rsync_command(cfg, record_path)
    log.debug("rsync argv: %s", " ".join(shlex.quote(part) for part in cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        log.error("rsync exited %d for catalog record %s; it stays in the spool for the next run",
                  result.returncode, record_path)
        return False
    try:
        os.remove(record_path)
    except OSError as err:
        log.warning("catalog record %s shipped but could not be removed from the spool: %s", record_path, err)
    log.info("catalog record %s -> %s", os.path.basename(record_path), cmd[-1])
    return True


def pending_records(cfg):
    """Finished (``.toml``) records waiting in the spool, oldest name first; never ``.pending`` or ``.tmp`` ones."""
    return _spooled(cfg, RECORD_SUFFIX)


def ship_pending_records(cfg, dry_run, stats):
    """Ship records left over from earlier runs.  Returns True when none is left."""
    if not cfg["remote_catalog_path"]:
        return True
    settle_pending_records(cfg, dry_run)
    all_clear = True
    for record_path in pending_records(cfg):
        if dry_run:
            log.info("dry run: would ship catalog record %s", record_path)
            continue
        if ship_record(cfg, record_path):
            stats["records"] += 1
        else:
            stats["failed"] += 1
            all_clear = False
    return all_clear


# --------------------------------------------------------------------------- orchestration

def shippable_folders(cfg):
    """finished_jobs() for the configured DB and path mapping."""
    return finished_jobs(cfg["db_path"], cfg["completed_path"], cfg["arm_completed_path"])


def ship_recognized(cfg, dry_run, verbose, stats):
    """Ship every pending movies/ and tv/ folder.  Returns True when none is left pending."""
    shippable = shippable_folders(cfg)
    all_clear = True
    for sub_folder in RECOGNIZED:
        for folder in pending_folders(cfg, sub_folder, shippable):
            if ship_folder(cfg, sub_folder, folder, dry_run, verbose, job=shippable[folder], stats=stats):
                stats["shipped"] += 1
            else:
                stats["failed"] += 1
                all_clear = False
    return all_clear


def run(cfg, dry_run=False, verbose=False):
    """One full pass.  Returns the exit code for the process."""
    stats = {"shipped": 0, "failed": 0, "records": 0, "renamed": 0}
    _warned.clear()
    attempted_unidentified = set()
    settle_destination_states(cfg, dry_run)
    ship_pending_records(cfg, dry_run, stats)
    while True:
        recognized_clear = ship_recognized(cfg, dry_run, verbose, stats)
        if dry_run:
            # Nothing moves in a dry run, so list the unidentified folders once and stop.
            shippable = shippable_folders(cfg)
            for folder in pending_folders(cfg, UNIDENTIFIED, shippable):
                ship_folder(cfg, UNIDENTIFIED, folder, dry_run, verbose, job=shippable[folder])
            break
        if not recognized_clear:
            log.warning("recognized folders still pending; not starting unidentified transfers this run")
            break
        shippable = shippable_folders(cfg)
        unidentified = [f for f in pending_folders(cfg, UNIDENTIFIED, shippable)
                        if f not in attempted_unidentified]
        if not unidentified:
            break
        # One unidentified folder at a time, then loop back and re-check movies/ and tv/.
        folder = unidentified[0]
        attempted_unidentified.add(folder)
        if ship_folder(cfg, UNIDENTIFIED, folder, dry_run, verbose, job=shippable[folder], stats=stats):
            stats["shipped"] += 1
        else:
            stats["failed"] += 1
    log.info("done: %d shipped (%d beside a folder of the same name), %d failed, %d catalog record(s) shipped",
             stats["shipped"], stats["renamed"], stats["failed"], stats["records"])
    return 1 if stats["failed"] else 0


def acquire_lock(lock_file):
    """Take an exclusive flock on lock_file, or raise ShipperError if another run holds it."""
    handle = open(lock_file, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ShipperError(f"another shipper run holds {lock_file}")
    return handle


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Ship finished ARM rips to a media box with rsync.")
    parser.add_argument("-c", "--config", default="/etc/arm-completed-shipper.conf",
                        help="INI config file (default: %(default)s)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="show what would ship; pass --dry-run to rsync")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging and rsync -v")
    parser.add_argument("--db-path", help="override db_path")
    parser.add_argument("--completed-path", help="override completed_path")
    parser.add_argument("--arm-completed-path", help="override arm_completed_path")
    parser.add_argument("--lock-file", help="override lock_file")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(args.config, {"db_path": args.db_path, "completed_path": args.completed_path,
                                        "arm_completed_path": args.arm_completed_path,
                                        "lock_file": args.lock_file})
        lock = acquire_lock(cfg["lock_file"])
    except ShipperError as err:
        log.error("%s", err)
        return 2
    try:
        return run(cfg, dry_run=args.dry_run, verbose=args.verbose)
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
