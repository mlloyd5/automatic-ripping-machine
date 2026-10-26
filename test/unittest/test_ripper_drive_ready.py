"""
Tests for the drive-ready wait, the direct disc probe behind Job.parse_udev, the mount lookup
and the ioctl eject, with the hardware and subprocesses mocked.

Importing these modules pulls in the whole of ARM (config, Flask app, database), so a throwaway
arm.yaml is written first and pointed at with ARM_CONFIG_FILE. Needs the Linux dependencies ARM
itself needs (libdiscid, pyudev); skipped when they are missing.
"""
import ctypes
import importlib
import itertools
import json
import logging
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]

_TMP = tempfile.mkdtemp(prefix="arm_drive_ready_test_")
_CONFIG = os.path.join(_TMP, "arm.yaml")
with open(_CONFIG, "w") as _handle:
    _handle.write(f"INSTALLPATH: \"{REPO_ROOT}\"\n"
                  f"DBFILE: \"{_TMP}/arm.db\"\n"
                  f"LOGPATH: \"{_TMP}/logs/\"\n"
                  f"ABCDE_CONFIG_FILE: \"{REPO_ROOT}/setup/.abcde.conf\"\n"
                  f"RAW_PATH: \"{_TMP}/raw/\"\nTRANSCODE_PATH: \"{_TMP}/transcode/\"\n"
                  f"COMPLETED_PATH: \"{_TMP}/completed/\"\n"
                  f"DISC_PHOTO_PATH: \"{_TMP}/photos/\"\n"
                  "DISABLE_LOGIN: true\nMETADATA_PROVIDER: \"tmdb\"\n")
os.environ["ARM_CONFIG_FILE"] = _CONFIG

try:
    system_drives = importlib.import_module("arm.models.system_drives")
    job_module = importlib.import_module("arm.models.job")
    identify = importlib.import_module("arm.ripper.identify")
    utils = importlib.import_module("arm.ripper.utils")
    cfg = importlib.import_module("arm.config.config")
    IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on the host
    system_drives = job_module = identify = utils = cfg = None
    IMPORT_ERROR = error


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class FakeClock:
    """A monotonic clock that only moves when the fake sleep is called."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _switch_config():
    os.environ["ARM_CONFIG_FILE"] = _CONFIG
    if cfg.arm_config_path != _CONFIG:
        importlib.reload(cfg)


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestWaitUntilReady(unittest.TestCase):
    def setUp(self):
        _switch_config()
        self.drive = system_drives.SystemDrives()
        self.drive.stale = False
        self.drive.mount = "/dev/sr0"
        self.clock = FakeClock()

    def _wait(self, statuses, timeout, readable=None):
        readable = itertools.repeat(True) if readable is None else iter(readable)
        with mock.patch.object(system_drives, "_tray_status", side_effect=statuses), \
                mock.patch.object(system_drives, "_medium_readable", side_effect=lambda *_a: next(readable)) as opened:
            result = self.drive.wait_until_ready(timeout, logger=logging, sleep=self.clock.sleep, clock=self.clock)
        self.opens = opened.call_count
        return result

    def test_waits_through_loading_states_until_disc_ok(self):
        CDS = system_drives.CDS
        ready, seen = self._wait([3, 2, 3, 3, 4, 4], timeout=60)
        self.assertTrue(ready)
        self.assertEqual(seen, [CDS.DRIVE_NOT_READY, CDS.TRAY_OPEN, CDS.DRIVE_NOT_READY, CDS.DISC_OK])
        self.assertTrue(self.drive.ready)
        self.assertEqual(self.clock.sleeps, [1.0, 1.0, 1.0, 1.0])
        self.assertEqual(self.opens, 1)  # only tried once the status allowed it

    def test_returns_at_once_when_already_ready(self):
        ready, seen = self._wait([4], timeout=60)
        self.assertTrue(ready)
        self.assertEqual(seen, [system_drives.CDS.DISC_OK])
        self.assertEqual(self.clock.sleeps, [])

    def test_disc_ok_is_not_enough_until_the_device_opens(self):
        # the HL-DT-ST DT80N says DISC_OK seconds before mount/blkid/cdrom_id can open the device
        ready, seen = self._wait([4, 4, 4, 4], timeout=60, readable=[False, False, True])
        self.assertTrue(ready)
        self.assertEqual(seen, [system_drives.CDS.DISC_OK])
        self.assertEqual(self.clock.sleeps, [1.0, 1.0])
        self.assertEqual(self.opens, 3)

    def test_disc_ok_but_never_readable_times_out(self):
        ready, seen = self._wait(itertools.repeat(4), timeout=5, readable=itertools.repeat(False))
        self.assertFalse(ready)
        self.assertEqual(seen, [system_drives.CDS.DISC_OK])
        self.assertTrue(self.drive.ready)  # the ioctl's opinion, which is why main() uses the result instead
        self.assertEqual(self.clock.sleeps, [1.0] * 5)

    def test_gives_up_at_timeout(self):
        ready, seen = self._wait(itertools.repeat(2), timeout=5)
        self.assertFalse(ready)
        self.assertEqual(seen, [system_drives.CDS.TRAY_OPEN])
        self.assertFalse(self.drive.ready)
        self.assertEqual(sum(self.clock.sleeps), 5.0)
        self.assertEqual(self.clock.sleeps, [1.0] * 5)
        self.assertEqual(self.opens, 0)

    def test_unreadable_status_is_recorded_as_error(self):
        ready, seen = self._wait([None, 4], timeout=10)
        self.assertTrue(ready)
        self.assertEqual(seen, [system_drives.CDS.ERROR, system_drives.CDS.DISC_OK])


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestMediumReadable(unittest.TestCase):
    def test_open_success_and_failure(self):
        with mock.patch.object(system_drives.os, "open", return_value=7) as opener, \
                mock.patch.object(system_drives.os, "close") as closer:
            self.assertTrue(system_drives._medium_readable("/dev/sr0"))
        opener.assert_called_once_with("/dev/sr0", os.O_RDONLY)
        closer.assert_called_once_with(7)
        with mock.patch.object(system_drives.os, "open", side_effect=OSError(123, "No medium found")):
            self.assertFalse(system_drives._medium_readable("/dev/sr0"))
        self.assertFalse(system_drives._medium_readable(None))


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestEject(unittest.TestCase):
    def setUp(self):
        _switch_config()
        self.drive = system_drives.SystemDrives()
        self.drive.stale = False
        self.drive.mount = "/dev/sr0"
        self.drive.job_id_current = 5
        self.no_wait = mock.patch.object(system_drives, "TRAY_MOVE_TIMEOUT", 0.0)
        self.no_wait.start()

    def tearDown(self):
        self.no_wait.stop()

    def test_ioctl_is_enough_when_the_tray_opens(self):
        with mock.patch.object(system_drives, "_tray_ioctl") as ioctl, \
                mock.patch.object(system_drives, "_tray_status", return_value=2), \
                mock.patch.object(system_drives, "_scsi_tray") as scsi, \
                mock.patch.object(system_drives, "arm_subprocess") as run:
            self.assertIsNone(self.drive.eject())
        ioctl.assert_called_once_with("/dev/sr0", system_drives.CDROMEJECT)
        scsi.assert_not_called()
        run.assert_not_called()
        self.assertIsNone(self.drive.job_id_current)
        self.assertEqual(self.drive.job_id_previous, 5)

    def test_close_and_toggle_pick_the_request(self):
        with mock.patch.object(system_drives, "_tray_ioctl") as ioctl, \
                mock.patch.object(system_drives, "_scsi_tray") as scsi, \
                mock.patch.object(system_drives, "_tray_status", side_effect=[4, 2, 4]):
            self.assertIsNone(self.drive.eject("close"))   # closed: status reads 4
            self.assertIsNone(self.drive.eject("toggle"))  # toggle reads 2 (open) so it closes, then reads 4
        scsi.assert_not_called()
        self.assertEqual([call.args[1] for call in ioctl.call_args_list],
                         [system_drives.CDROMCLOSETRAY, system_drives.CDROMCLOSETRAY])

    def test_scsi_when_the_ioctl_says_ok_but_nothing_moves(self):
        # the drive's medium is locked in: the ioctl returns 0 and the tray stays put
        with mock.patch.object(system_drives, "_tray_ioctl") as ioctl, \
                mock.patch.object(system_drives, "_tray_status", side_effect=[4, 2]), \
                mock.patch.object(system_drives, "_scsi_tray") as scsi, \
                mock.patch.object(system_drives, "arm_subprocess") as run:
            self.assertIsNone(self.drive.eject())
        ioctl.assert_called_once()
        scsi.assert_called_once_with("/dev/sr0", False)
        run.assert_not_called()

    def test_eject_command_is_the_last_resort(self):
        with mock.patch.object(system_drives, "_tray_ioctl", side_effect=OSError(5, "Input/output error")), \
                mock.patch.object(system_drives, "_scsi_tray", side_effect=OSError("check condition")), \
                mock.patch.object(system_drives, "_tray_status", return_value=4), \
                mock.patch.object(system_drives, "arm_subprocess") as run:
            self.assertIsNone(self.drive.eject())
        run.assert_called_once_with(["eject", "--verbose", "--cdrom", "--scsi", "/dev/sr0"], check=True)

    def test_reports_the_command_error(self):
        error = subprocess.CalledProcessError(1, ["eject"], output="eject: none: not found")
        with mock.patch.object(system_drives, "_tray_ioctl", side_effect=OSError(5, "boom")), \
                mock.patch.object(system_drives, "_scsi_tray", side_effect=OSError("no")), \
                mock.patch.object(system_drives, "_tray_status", return_value=4), \
                mock.patch.object(system_drives, "arm_subprocess", side_effect=error):
            self.assertEqual(self.drive.eject(), "eject: none: not found")
        self.assertIsNone(self.drive.job_id_current)


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestScsiCommand(unittest.TestCase):
    def _run(self, status=0, host=0, driver=0):
        captured = {}

        def fake_ioctl(fd, request, buf):
            captured["request"] = request
            header = list(struct.unpack(system_drives._SG_IO_HDR, buf.raw))
            captured["cdb_len"], captured["timeout"] = header[2], header[9]
            captured["cdb"] = ctypes.string_at(header[7], header[2])
            struct.pack_into("BBBBHH", buf, system_drives._SG_STATUS_OFFSET, status, 0, 0, 0, host, driver)
            return 0
        with mock.patch.object(system_drives.os, "open", return_value=9) as opener, \
                mock.patch.object(system_drives.os, "close"), \
                mock.patch.object(system_drives.fcntl, "ioctl", side_effect=fake_ioctl):
            system_drives._scsi_tray("/dev/sr1")
        opener.assert_called_with("/dev/sr1", os.O_RDWR | os.O_NONBLOCK)
        return captured

    def test_sends_allow_then_eject(self):
        sent = []
        with mock.patch.object(system_drives, "_scsi_command", side_effect=lambda d, cdb: sent.append(cdb)):
            system_drives._scsi_tray("/dev/sr1")
            system_drives._scsi_tray("/dev/sr1", close=True)
        self.assertEqual(sent, [system_drives.SCSI_ALLOW_MEDIUM_REMOVAL, system_drives.SCSI_EJECT,
                                system_drives.SCSI_ALLOW_MEDIUM_REMOVAL, system_drives.SCSI_LOAD])

    def test_header_and_success(self):
        captured = self._run()
        self.assertEqual(captured["request"], system_drives.SG_IO)
        self.assertEqual(captured["cdb_len"], 6)
        self.assertEqual(captured["cdb"], system_drives.SCSI_EJECT)
        self.assertEqual(captured["timeout"], 15000)

    def test_check_condition_raises(self):
        with self.assertRaises(OSError):
            self._run(status=2)
        with self.assertRaises(OSError):
            self._run(host=1)


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestReadMediaProperties(unittest.TestCase):
    CDROM_ID_DVD = "ID_CDROM=1\nID_CDROM_DVD=1\nID_CDROM_MEDIA=1\nID_CDROM_MEDIA_DVD=1\nID_CDROM_MEDIA_STATE=complete\n"
    BLKID_DVD = "ID_FS_LABEL=COAL_MINERS_DAUGHTER\nID_FS_LABEL_ENC=COAL_MINERS_DAUGHTER\nID_FS_TYPE=udf\n"

    def _run(self, outputs):
        def fake_run(cmd, **_kwargs):
            returncode, stdout = outputs[os.path.basename(cmd[0])]
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")
        return mock.patch.object(job_module.subprocess, "run", side_effect=fake_run)

    def _tools(self):
        return mock.patch.object(job_module, "_find_tool",
                                 side_effect=lambda name, _fallbacks: f"/fake/{name}")

    def test_dvd_with_label(self):
        with self._tools(), self._run({"cdrom_id": (0, self.CDROM_ID_DVD), "blkid": (0, self.BLKID_DVD)}):
            props = job_module.read_media_properties("/dev/sr0")
        self.assertEqual(props["ID_CDROM_MEDIA_DVD"], "1")
        self.assertEqual(props["ID_FS_LABEL"], "COAL_MINERS_DAUGHTER")
        self.assertEqual(props["ID_FS_TYPE"], "udf")

    def test_no_medium_gives_nothing(self):
        with self._tools(), self._run({"cdrom_id": (0, "ID_CDROM=1\nID_CDROM_DVD=1\n"), "blkid": (0, self.BLKID_DVD)}):
            self.assertEqual(job_module.read_media_properties("/dev/sr0"), {})

    def test_blkid_failure_keeps_the_media_type(self):
        with self._tools(), self._run({"cdrom_id": (0, self.CDROM_ID_DVD), "blkid": (2, "")}):
            props = job_module.read_media_properties("/dev/sr0")
        self.assertEqual(props["ID_CDROM_MEDIA_DVD"], "1")
        self.assertNotIn("ID_FS_LABEL", props)

    def test_missing_helper_or_crash_gives_nothing(self):
        with mock.patch.object(job_module, "_find_tool", return_value=None):
            self.assertEqual(job_module.read_media_properties("/dev/sr0"), {})
        with self._tools(), mock.patch.object(job_module.subprocess, "run", side_effect=OSError("gone")):
            self.assertEqual(job_module.read_media_properties("/dev/sr0"), {})


class FakeDevice:
    def __init__(self, props):
        self._props = props

    def items(self):
        return self._props.items()


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestParseUdev(unittest.TestCase):
    STALE = {"DEVNAME": "/dev/sr0", "ID_CDROM": "1", "ID_CDROM_DVD": "1"}
    DVD = {"ID_CDROM_MEDIA": "1", "ID_CDROM_MEDIA_DVD": "1", "ID_FS_LABEL": "COAL_MINERS_DAUGHTER",
           "ID_FS_TYPE": "udf"}

    def setUp(self):
        _switch_config()

    def _job(self, udev_props, probed):
        with mock.patch.object(job_module.pyudev.Devices, "from_device_file", return_value=FakeDevice(udev_props)), \
                mock.patch.object(job_module, "read_media_properties", return_value=probed) as probe, \
                mock.patch.object(job_module.subprocess, "check_output", return_value=b"LSDVD\n"):
            job = job_module.Job("/dev/sr0")
        probe.assert_called_once_with("/dev/sr0")
        return job

    def test_probe_fills_in_for_a_stale_udev_database(self):
        job = self._job(self.STALE, self.DVD)
        self.assertEqual(job.disctype, "dvd")
        self.assertEqual(job.label, "COAL_MINERS_DAUGHTER")

    def test_udev_is_used_when_the_probe_sees_nothing(self):
        job = self._job({**self.STALE, **self.DVD}, {})
        self.assertEqual(job.disctype, "dvd")
        self.assertEqual(job.label, "COAL_MINERS_DAUGHTER")

    def test_probe_replaces_the_previous_disc_in_udev(self):
        audio = {"ID_CDROM_MEDIA": "1", "ID_CDROM_MEDIA_CD": "1", "ID_CDROM_MEDIA_TRACK_COUNT_AUDIO": "12"}
        job = self._job({**self.STALE, **self.DVD}, audio)
        self.assertEqual(job.disctype, "music")
        self.assertIsNone(job.label)

    def test_nothing_anywhere_is_unknown(self):
        job = self._job(self.STALE, {})
        self.assertEqual(job.disctype, "unknown")


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestFindMount(unittest.TestCase):
    def _findmnt(self, returncode, filesystems=None):
        stdout = json.dumps({"filesystems": filesystems}) if filesystems is not None else ""
        return mock.patch.object(identify.subprocess, "run",
                                 return_value=subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=""))

    def test_nothing_mounted(self):
        with self._findmnt(1) as run:
            self.assertIsNone(identify.find_mount("/dev/sr0"))
        self.assertEqual(run.call_args.args[0], ["findmnt", "--json", "--source", "/dev/sr0"])

    def test_device_node_as_target_is_not_a_mountpoint(self):
        # what the LXC passthrough bind mount looks like if it ever matched
        with self._findmnt(0, [{"target": "/dev/sr0", "source": "none[/dev/sr0]", "fstype": "tmpfs"}]):
            self.assertIsNone(identify.find_mount("/dev/sr0"))

    def test_real_mount_is_returned(self):
        with self._findmnt(0, [{"target": _TMP, "source": "/dev/sr0", "fstype": "udf"}]):
            self.assertEqual(identify.find_mount("/dev/sr0"), _TMP)

    def test_findmnt_missing(self):
        with mock.patch.object(identify.subprocess, "run", side_effect=OSError("no findmnt")):
            self.assertIsNone(identify.find_mount("/dev/sr0"))


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestUnmountDisc(unittest.TestCase):
    def test_unmounts_by_mountpoint_when_there_is_one(self):
        job = SimpleNamespace(devpath="/dev/sr1", mountpoint=_TMP)
        with mock.patch.object(identify, "arm_subprocess") as run:
            identify.unmount_disc(job)
        run.assert_called_once_with(["umount", _TMP])

    def test_falls_back_to_the_device(self):
        for mountpoint in ("", None, "/dev/sr1", "/nonexistent/mnt"):
            job = SimpleNamespace(devpath="/dev/sr1", mountpoint=mountpoint)
            with mock.patch.object(identify, "arm_subprocess") as run:
                identify.unmount_disc(job)
            run.assert_called_once_with(["umount", "/dev/sr1"])


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestJobProcessAlive(unittest.TestCase):
    def test_states(self):
        me = os.getpid()
        live = SimpleNamespace(pid=me, pid_hash=hash(utils.psutil.Process(me)))
        self.assertTrue(utils.job_process_alive(live))
        self.assertFalse(utils.job_process_alive(SimpleNamespace(pid=me, pid_hash=12345)))  # pid reused
        self.assertFalse(utils.job_process_alive(SimpleNamespace(pid=None, pid_hash=None)))
        with mock.patch.object(utils.psutil, "pid_exists", return_value=False):
            self.assertFalse(utils.job_process_alive(live))


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestQuietExceptions(unittest.TestCase):
    def test_hierarchy(self):
        for cls in (utils.EmptyDriveException, utils.UnlistedDriveException, utils.DuplicateRunException):
            self.assertTrue(issubclass(cls, utils.NothingToDoException))
            self.assertTrue(issubclass(cls, utils.RipperException))


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestDriveReadyTimeout(unittest.TestCase):
    def setUp(self):
        _switch_config()
        self.saved = cfg.arm_config.get("DRIVE_READY_TIMEOUT")

    def tearDown(self):
        if self.saved is None:
            cfg.arm_config.pop("DRIVE_READY_TIMEOUT", None)
        else:
            cfg.arm_config["DRIVE_READY_TIMEOUT"] = self.saved

    def test_default_and_values(self):
        cfg.arm_config.pop("DRIVE_READY_TIMEOUT", None)
        self.assertEqual(utils.drive_ready_timeout(), 60)
        cfg.arm_config["DRIVE_READY_TIMEOUT"] = "90"
        self.assertEqual(utils.drive_ready_timeout(), 90)
        cfg.arm_config["DRIVE_READY_TIMEOUT"] = 0
        self.assertEqual(utils.drive_ready_timeout(), 1)
        cfg.arm_config["DRIVE_READY_TIMEOUT"] = "soon"
        self.assertEqual(utils.drive_ready_timeout(), 60)

    def test_setup_default_matches(self):
        self.assertEqual(cfg.arm_config.get("DRIVE_READY_TIMEOUT"), 60)


if __name__ == "__main__":
    unittest.main()
