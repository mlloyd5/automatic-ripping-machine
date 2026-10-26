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
* A lock file stops two runs overlapping.
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
  spool and goes on the next run, before anything else.
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

JOB_COLUMNS = ("job_id", "status", "path", "title", "title_manual", "year", "year_manual", "video_type",
               "imdb_id", "label", "disctype", "disc_barcode", "disc_barcode_title", "sleeve_id", "box",
               "stop_time")
"""Columns read from ARM's job table when present; a stock ARM lacks the fork's disc_barcode and sleeve ones."""

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
    """
    if not os.path.exists(db_path):
        raise ShipperError(f"ARM database not found: {db_path}")
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        present = {row[1] for row in conn.execute("PRAGMA table_info(job)")}
        columns = [column for column in JOB_COLUMNS if column in present]
        if "path" not in columns or "status" not in columns:
            raise ShipperError(f"{db_path} has no job table with path and status columns")
        rows = conn.execute(f"SELECT {', '.join(columns)} FROM job WHERE path IS NOT NULL AND path != ''").fetchall()
    finally:
        conn.close()
    return [dict(zip(columns, row)) for row in rows]


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
    """
    by_folder = {}
    for job in read_jobs(db_path):
        local = local_folder_for(job["path"], completed_path, arm_completed_path)
        if local is not None:
            by_folder.setdefault(local, []).append(job)

    shippable = {}
    for path, jobs in by_folder.items():
        seen = {job["status"] for job in jobs}
        if SUCCESS_STATUS not in seen:
            continue
        if any(status not in FINISHED_STATUSES for status in seen):
            log.debug("skipping %s: another job on that path is still %s", path, sorted(seen))
            continue
        successes = [job for job in jobs if job["status"] == SUCCESS_STATUS]
        shippable[path] = max(successes, key=lambda job: job.get("job_id") or 0)
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

def destination(cfg, sub_folder, local_folder):
    """
    Build the rsync destination string for one job folder.

    The remote path is deliberately not shell-quoted: rsync is run with ``-s``
    (``--protect-args``), so the path travels inside the rsync protocol and the
    remote shell never parses it.  Quoting it as well would make the quotes part
    of the path on rsync >= 3.2.4, which escapes remote args itself.
    """
    remote_dir = os.path.join(remote_root_for(cfg, sub_folder), os.path.basename(local_folder))
    if cfg["remote_host"]:
        return f"{cfg['remote_host']}:{remote_dir}/"
    return remote_dir + os.sep


def build_rsync_command(cfg, sub_folder, local_folder, dry_run=False, verbose=False):
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
    if cfg["remote_host"]:
        ssh = "ssh"
        if cfg["ssh_options"]:
            ssh += " " + cfg["ssh_options"]
        # -s: send paths inside the protocol, safe from remote shell word splitting/quoting.
        cmd.extend(["-s", "-e", ssh])
    if cfg["rsync_extra_args"]:
        cmd.extend(shlex.split(cfg["rsync_extra_args"]))
    cmd.append(local_folder.rstrip(os.sep) + os.sep)
    cmd.append(destination(cfg, sub_folder, local_folder))
    return cmd


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


def run_post_ship_command(cfg, sub_folder, local_folder):
    """Run the optional hook after a folder has shipped (e.g. to trigger a Plex scan)."""
    hook = cfg["post_ship_command"]
    if not hook:
        return
    env = dict(os.environ, SHIPPER_TYPE=sub_folder, SHIPPER_FOLDER=os.path.basename(local_folder),
               SHIPPER_DESTINATION=destination(cfg, sub_folder, local_folder))
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
    """
    pending = None
    if job is not None and cfg["remote_catalog_path"] and sub_folder != UNIDENTIFIED:
        try:
            pending = prepare_record(cfg, sub_folder, local_folder, job, dry_run=dry_run)
        except Exception as err:  # optional metadata: never block the transfer
            if not isinstance(err, RecordError):
                log.exception("could not prepare the catalog record for %s; shipping the folder without it",
                              local_folder)
            if stats is not None:
                stats["failed"] += 1
    cmd = build_rsync_command(cfg, sub_folder, local_folder, dry_run=dry_run, verbose=verbose)
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
    if pending:
        finished = finish_record(pending)
        if finished and ship_record(cfg, finished):
            if stats is not None:
                stats["records"] += 1
        elif stats is not None:
            stats["failed"] += 1
    run_post_ship_command(cfg, sub_folder, local_folder)
    return True


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


def _job_tag(job):
    """``-job<id>``, the part of a record name that identifies its job."""
    return f"-job{job.get('job_id') or 0}"


def record_name(cfg, job, suffix=RECORD_SUFFIX):
    """``<stop time>-<ripper>-job<id>.toml``: sorts by rip time and is the same whenever the job is re-shipped."""
    stamp = _clean(job.get("stop_time"))
    try:
        when = datetime.datetime.fromisoformat(stamp) if stamp else datetime.datetime.now()
    except ValueError:
        when = datetime.datetime.now()
    return f"{when.strftime('%Y%m%d-%H%M%S')}-{cfg['ripper_name']}{_job_tag(job)}{suffix}"


def library_path(sub_folder, local_folder):
    """``<sub folder>/<job folder>``: the record's ``library`` key, and where the folder sits under completed_path."""
    return f"{sub_folder}/{os.path.basename(local_folder.rstrip(os.sep))}"


def catalog_record(cfg, sub_folder, local_folder, job):
    """
    Build the catalog record for one job folder about to ship.

    Reads the Matroska header of every media file in the folder, so it must run
    before rsync starts removing them.  Returns ``(file name, TOML text)`` (the
    name ends in ``.toml``), or None when the folder holds no media file
    (nothing for a catalog to register).
    """
    files = folder_media_files(local_folder)
    if not files:
        log.warning("no media file in %s; no catalog record written", local_folder)
        return None
    folder = os.path.basename(local_folder.rstrip(os.sep))
    library = library_path(sub_folder, local_folder)
    rows = []
    for relative in files:
        row = mkvheader.probe_row(os.path.join(local_folder, relative))
        stem = relative.rsplit(".", 1)[0]
        row["path"] = f"{library}/{relative}"
        row["main"] = "/" not in relative and stem == folder
        if not row["probed"]:
            log.debug("%s is not Matroska or unreadable; listed by size only", relative)
        rows.append(row)
    id_key, id_value = _metadata_id(job)
    title = _clean(job.get("title_manual")) or _clean(job.get("title")) or folder
    lines = ["# Catalog record written by the ARM completed-rip shipper for one shipped rip.",
             "# What the ripper knew about the disc, then every media file it delivered with what the",
             "# file's Matroska header says.  Consumed by the media catalog's import; safe to re-import."]
    lines += _toml_lines([
        ("ripper", cfg["ripper_name"]),
        ("job", int(job.get("job_id") or 0)),
        ("shipped", datetime.datetime.now().replace(microsecond=0).isoformat()),
        ("type", VIDEO_TYPES.get(sub_folder, _clean(job.get("video_type")) or "unknown")),
        ("title", title),
        ("year", _job_year(job)),
        (id_key, id_value),
        ("library", library),
        ("destination", os.path.join(remote_root_for(cfg, sub_folder), folder)),
        ("label", _clean(job.get("label")) or ""),
        ("format", disc_format(job, rows)),
        ("role", "feature" if sub_folder == "tv" or any(row["main"] for row in rows) else "bonus"),
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


def pending_record_path(cfg, job, library=None):
    """
    The ``.pending`` record an earlier run spooled for ``job``, or None.

    Matched on the ``-job<id>.pending`` end of the name only, so a changed
    ripper name or rip-time format cannot orphan it.  With ``library`` (see
    :func:`library_path`) a pending record whose ``library`` key names another
    folder (a job id reused after an ARM database reset) is not returned.
    When several match, the newest name wins.
    """
    tail = _job_tag(job) + PENDING_SUFFIX
    matches = [path for path in _spooled(cfg, tail)
               if library is None or read_record_key(path, "library") == library]
    return matches[-1] if matches else None


def prepare_record(cfg, sub_folder, local_folder, job, dry_run=False):
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
        return existing
    built = catalog_record(cfg, sub_folder, local_folder, job)
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
        library = read_record_key(pending, "library")
        if not library:
            log.warning("catalog record %s names no library folder; leaving it in the spool", pending)
            continue
        present = source_folder_present(cfg, library)
        if present:
            continue
        if present is None:
            log.warning("%s has no %s folder (not mounted?); leaving catalog record %s in the spool",
                        cfg["completed_path"], library.split("/", 1)[0], os.path.basename(pending))
            continue
        if dry_run:
            log.info("dry run: would finish catalog record %s (%s is gone)", pending, library)
            continue
        log.warning("%s is no longer in %s; assuming it shipped and finishing its catalog record %s",
                    library, cfg["completed_path"], os.path.basename(pending))
        finish_record(pending)


def record_destination(cfg):
    remote_dir = cfg["remote_catalog_path"].rstrip("/") + "/"
    return f"{cfg['remote_host']}:{remote_dir}" if cfg["remote_host"] else remote_dir


def build_record_rsync_command(cfg, record_path):
    """rsync argv for one spooled record: the same ssh and timeout as the folders, no bwlimit, source kept."""
    cmd = [cfg["rsync_path"], "-t", f"--timeout={int(cfg['rsync_timeout'])}"]
    if cfg["remote_host"]:
        ssh = "ssh"
        if cfg["ssh_options"]:
            ssh += " " + cfg["ssh_options"]
        cmd.extend(["-s", "-e", ssh])
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
    stats = {"shipped": 0, "failed": 0, "records": 0}
    attempted_unidentified = set()
    ship_pending_records(cfg, dry_run, stats)
    while True:
        recognized_clear = ship_recognized(cfg, dry_run, verbose, stats)
        if dry_run:
            # Nothing moves in a dry run, so list the unidentified folders once and stop.
            shippable = shippable_folders(cfg)
            for folder in pending_folders(cfg, UNIDENTIFIED, shippable):
                ship_folder(cfg, UNIDENTIFIED, folder, dry_run, verbose)
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
        if ship_folder(cfg, UNIDENTIFIED, folder, dry_run, verbose, stats=stats):
            stats["shipped"] += 1
        else:
            stats["failed"] += 1
    log.info("done: %d shipped, %d failed, %d catalog record(s) shipped", stats["shipped"], stats["failed"],
             stats["records"])
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
