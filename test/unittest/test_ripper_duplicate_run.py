"""
Tests for arm.ripper.utils.duplicate_run_check: a new run takes a drive from a job whose process is
gone, without calling a finished job failed. On the ts140 the eject of job 199 (The Manchurian
Candidate) started the run that became job 202 three seconds before job 199 ended, and that run held
on to the drive as it was then while it waited for the next disc. It then marked the successful job
failed, so the completed shipper left its folder behind.

Builds a throwaway config and SQLite database under a temp folder like test_ui_bonus_disc.py and
skips itself when the ARM package cannot be imported on this host. Jobs are written straight into
the database (the Job constructor needs udev and a drive).
"""
import atexit
import importlib
import logging
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import psutil
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

_ORIGINAL_CONFIG_FILE = os.environ.get("ARM_CONFIG_FILE")
_TEMP_ROOT = tempfile.mkdtemp(prefix="arm-duplicate-run-test-")
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
})
_CONFIG_PATH = os.path.join(_TEMP_ROOT, "arm.yaml")
with open(_CONFIG_PATH, "w") as _handle:
    yaml.safe_dump(_template, _handle)
os.environ["ARM_CONFIG_FILE"] = _CONFIG_PATH

try:
    import arm.config.config as cfg
    from arm.ui import db
    import arm.ui.routes  # noqa: F401  (creates the database like runui.py does)
    from arm.models.job import Job
    from arm.models.system_drives import SystemDrives
    from arm.ripper import utils
    IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on the host
    IMPORT_ERROR = error

# The SQLAlchemy engine binds to the DBFILE of whichever test module imports ARM first and keeps it
# for the whole run, so the folder is removed at interpreter exit, not at module teardown
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)

DEVPATH = "/dev/sr1"
DEAD = dict(pid=29029, pid_hash=12345)  # never the hash of a real process, whatever runs as pid 29029


def alive():
    """The pid and pid_hash of a process that is still running: this one."""
    return dict(pid=os.getpid(), pid_hash=hash(psutil.Process(os.getpid())))


def make_job(**fields):
    """A Job row without the constructor (which reads udev and the drive)."""
    job = Job.__mapper__.class_manager.new_instance()
    defaults = dict(devpath=DEVPATH, status="success", disctype="dvd", title="The Manchurian Candidate",
                    start_time=datetime(2026, 11, 2, 16, 54), **DEAD)
    defaults.update(fields)
    for key, value in defaults.items():
        setattr(job, key, value)
    db.session.add(job)
    db.session.commit()
    return job


def make_drive(job=None):
    """The drive at DEVPATH, with ``job`` as its current job."""
    drive = SystemDrives()
    drive.mount = DEVPATH
    drive.job_id_current = job.job_id if job else None
    db.session.add(drive)
    db.session.commit()
    return drive


def write_elsewhere(statement, *parameters):
    """A write by another process: another connection to the database, unseen by this session."""
    connection = sqlite3.connect(cfg.arm_config["DBFILE"])
    with connection:
        connection.execute(statement, parameters)
    connection.close()


def release_elsewhere():
    """What the ending job's own process does when it ejects."""
    write_elsewhere("UPDATE system_drives SET job_id_previous = job_id_current, job_id_current = NULL "
                    "WHERE mount = ?", DEVPATH)


def stored_job(job_id):
    """The job as the database has it, not as this session remembers it."""
    db.session.expire_all()
    return db.session.get(Job, job_id)


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM cannot be imported here: {IMPORT_ERROR}")
class TestDuplicateRunCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["ARM_CONFIG_FILE"] = _CONFIG_PATH
        if cfg.arm_config_path != _CONFIG_PATH:
            importlib.reload(cfg)

    def setUp(self):
        SystemDrives.query.delete()
        Job.query.delete()
        db.session.commit()

    def assert_drive_released(self, job_id):
        db.session.expire_all()
        drive = SystemDrives.query.filter_by(mount=DEVPATH).one()
        self.assertIsNone(drive.job_id_current)
        self.assertEqual(drive.job_id_previous, job_id)

    def test_finished_job_keeps_its_status(self):
        """A success left on a drive stays a success: the shipper goes by it."""
        job_id = make_job(status="success").job_id
        make_drive(db.session.get(Job, job_id))
        with self.assertLogs(level=logging.INFO) as logs:
            utils.duplicate_run_check(DEVPATH)
        self.assertEqual(stored_job(job_id).status, "success")
        self.assert_drive_released(job_id)
        self.assertNotIn("marking it failed", "\n".join(logs.output))
        self.assertIn("finished without releasing the drive", "\n".join(logs.output))

    def test_failed_job_stays_failed_and_is_not_marked_again(self):
        job_id = make_job(status="fail", errors="MakeMKV gave up").job_id
        make_drive(db.session.get(Job, job_id))
        with self.assertLogs(level=logging.INFO) as logs:
            utils.duplicate_run_check(DEVPATH)
        self.assertEqual(stored_job(job_id).status, "fail")
        self.assert_drive_released(job_id)
        self.assertNotIn("marking it failed", "\n".join(logs.output))

    def test_unfinished_job_with_a_dead_process_is_marked_failed(self):
        """Killed by a container restart mid rip, the two real cases being statuses info and active."""
        for status in ("ripping", "info", "active", "transcoding", "waiting_transcode"):
            with self.subTest(status=status):
                self.setUp()
                job_id = make_job(status=status).job_id
                make_drive(db.session.get(Job, job_id))
                with self.assertLogs(level=logging.WARNING) as logs:
                    utils.duplicate_run_check(DEVPATH)
                self.assertEqual(stored_job(job_id).status, "fail")
                self.assert_drive_released(job_id)
                self.assertIn(f"({job_id}, {status}) has no live process (pid 29029); marking it failed",
                              "\n".join(logs.output))

    def test_live_job_stops_the_new_run(self):
        job_id = make_job(status="ripping", **alive()).job_id
        make_drive(db.session.get(Job, job_id))
        with self.assertRaises(utils.DuplicateRunException):
            utils.duplicate_run_check(DEVPATH)
        self.assertEqual(stored_job(job_id).status, "ripping")
        self.assertEqual(SystemDrives.query.filter_by(mount=DEVPATH).one().job_id_current, job_id)

    def test_unknown_and_idle_drives_are_left_alone(self):
        utils.duplicate_run_check(DEVPATH)  # no such drive
        make_drive()
        utils.duplicate_run_check(DEVPATH)
        self.assertIsNone(SystemDrives.query.filter_by(mount=DEVPATH).one().job_id_current)

    def test_drive_released_while_this_run_waited_for_the_disc(self):
        """
        Jobs 199 and 202: setup() loaded the drive at 17:27:01, job 199 committed its release at 17:27:03
        and ended, and this check ran at 17:27:48. The job is not looked at, let alone marked failed.
        """
        job_id = make_job(status="ripping").job_id
        make_drive(db.session.get(Job, job_id))
        db.session.expire_all()
        loaded_by_setup = SystemDrives.query.filter_by(mount=DEVPATH).one()
        self.assertEqual(loaded_by_setup.job_id_current, job_id)

        release_elsewhere()
        write_elsewhere("UPDATE job SET status = 'success' WHERE job_id = ?", job_id)
        with self.assertNoLogs(level=logging.INFO):
            utils.duplicate_run_check(DEVPATH)
        self.assertEqual(stored_job(job_id).status, "success")
        self.assert_drive_released(job_id)

    def test_release_seen_even_with_the_old_job_loaded_on_the_drive(self):
        """A job_current the session already loaded must go the same way as the id it came from."""
        job_id = make_job(status="transcoding", **alive()).job_id
        drive = make_drive(db.session.get(Job, job_id))
        self.assertTrue(drive.processing)

        release_elsewhere()
        utils.duplicate_run_check(DEVPATH)
        self.assertIsNone(drive.job_current)

    def test_drive_released_by_a_job_still_transcoding(self):
        """
        A job goes on after its eject when it transcodes, for hours, with its process alive: the disc
        that goes in next is not a duplicate of it, however early this run started.
        """
        job_id = make_job(status="transcoding", **alive()).job_id
        make_drive(db.session.get(Job, job_id))
        db.session.expire_all()
        loaded_by_setup = SystemDrives.query.filter_by(mount=DEVPATH).one()
        self.assertEqual(loaded_by_setup.job_id_current, job_id)

        release_elsewhere()
        utils.duplicate_run_check(DEVPATH)
        self.assertEqual(stored_job(job_id).status, "transcoding")
        self.assert_drive_released(job_id)


def tearDownModule():
    if _ORIGINAL_CONFIG_FILE is None:
        os.environ.pop("ARM_CONFIG_FILE", None)
    else:
        os.environ["ARM_CONFIG_FILE"] = _ORIGINAL_CONFIG_FILE


if __name__ == "__main__":
    unittest.main()
