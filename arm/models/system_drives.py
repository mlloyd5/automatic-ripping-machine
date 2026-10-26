"""Database Model for Drive Information on the System
"""

import ctypes
import enum
import fcntl
import logging
import os
import re
import struct
import time
from subprocess import CalledProcessError

from arm.ripper.ProcessHandler import arm_subprocess
from arm.ui import db


class CDS(enum.Enum):
    """CD Status

    ioctl defines may (not very likely) change.
    see https://github.com/torvalds/linux/blob/master/include/uapi/linux/cdrom.h
    """
    NO_INFO = 0
    """CDS_NO_INFO"""
    NO_DISC = 1
    """CDS_NO_INFO"""
    TRAY_OPEN = 2
    """CDS_TRAY_OPEN"""
    DRIVE_NOT_READY = 3
    """CDS_DRIVE_NOT_READY"""
    DISC_OK = 4
    """CDS_DISC_OK"""
    ERROR = None


CDROMEJECT = 0x5309
"""ioctl request: eject the disc / open the tray (linux/cdrom.h)"""
CDROMCLOSETRAY = 0x5319
"""ioctl request: close the tray (linux/cdrom.h)"""


SG_IO = 0x2285
"""ioctl request: pass a SCSI command through (scsi/sg.h)"""
_SG_IO_HDR = "iiBBHIPPPIIiPBBBBHHiII"
"""struct sg_io_hdr, interface 'S' (scsi/sg.h)"""
_SG_STATUS_OFFSET = struct.calcsize("iiBBHIPPPIIiP")
"""where sg_io_hdr.status sits, for reading the result back"""
SCSI_ALLOW_MEDIUM_REMOVAL = bytes.fromhex("1e 00 00 00 00 00")
SCSI_EJECT = bytes.fromhex("1b 00 00 00 02 00")
"""START STOP UNIT with LoEj set and Start clear: open the tray"""
SCSI_LOAD = bytes.fromhex("1b 00 00 00 03 00")
"""START STOP UNIT with LoEj and Start set: close the tray"""
TRAY_MOVE_TIMEOUT = 5.0
"""seconds to give a drive after a tray request before concluding it ignored it"""


def _scsi_command(devpath, cdb, timeout_ms=15000):
    """
    Send one SCSI command with no data phase to the drive through SG_IO.

    START STOP UNIT and PREVENT/ALLOW MEDIUM REMOVAL are on the kernel's list of commands a
    user with write access to the device may send, so the arm user needs no capability.

    :raise OSError: when the device cannot be opened or the drive reports a check condition
    """
    command = ctypes.create_string_buffer(bytes(cdb), len(cdb))
    sense = ctypes.create_string_buffer(32)
    header = struct.pack(_SG_IO_HDR, ord("S"), -1, len(cdb), 32, 0, 0, 0,
                         ctypes.addressof(command), ctypes.addressof(sense), timeout_ms,
                         0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    request = ctypes.create_string_buffer(header, len(header))
    handle = os.open(devpath, os.O_RDWR | os.O_NONBLOCK)
    try:
        fcntl.ioctl(handle, SG_IO, request)
    finally:
        os.close(handle)
    result = struct.unpack_from("BBBBHH", request.raw, _SG_STATUS_OFFSET)
    status, host_status, driver_status = result[0], result[4], result[5]
    if status or host_status or driver_status:
        raise OSError(f"SCSI command {cdb.hex()} on {devpath} failed: status 0x{status:02x}, "
                      f"host 0x{host_status:04x}, driver 0x{driver_status:04x}")


def _scsi_tray(devpath, close=False):
    """
    Open or close the tray by SCSI command, after lifting a PREVENT MEDIUM REMOVAL.

    udev's cdrom_id locks the medium in on every media change, and some drives then ignore
    the CDROMEJECT ioctl although the kernel reports it as done; the explicit ALLOW plus
    START STOP UNIT is what eject --scsi sends and what got the trays open on such a drive.
    """
    _scsi_command(devpath, SCSI_ALLOW_MEDIUM_REMOVAL)
    _scsi_command(devpath, SCSI_LOAD if close else SCSI_EJECT)


def _tray_ioctl(devpath, request):
    """
    Send a tray request (CDROMEJECT or CDROMCLOSETRAY) straight to the drive.

    This is what ``eject --cdrom`` does, minus eject(1)'s mount-table lookup. That lookup breaks
    where the device node is itself a mount target: Proxmox passes a drive into an LXC by
    bind-mounting a tiny tmpfs at ``/dev/sr0``, so eject(1) decides "none" is mounted there and
    gives up without touching the drive.

    :raise OSError: when the device cannot be opened or the drive rejects the request
    """
    handle = os.open(devpath, os.O_RDONLY | os.O_NONBLOCK)
    try:
        fcntl.ioctl(handle, request, 0)
    finally:
        os.close(handle)


def _medium_readable(devpath, logger=logging):
    """
    True when a plain (blocking) ``open()`` of the device succeeds, i.e. the drive will serve reads.

    The CDROM_DRIVE_STATUS ioctl says DISC_OK a few seconds before that, and an HL-DT-ST DT80N
    keeps saying DISC_OK after an eject with the tray open; ``mount``, ``blkid`` and ``cdrom_id``
    all need the open to work, so this is what "ready" has to mean.
    """
    try:
        handle = os.open(devpath, os.O_RDONLY)
    except OSError as err:
        logger.debug(f"'{devpath}' cannot be opened for reading yet: {err}")
        return False
    except TypeError as err:
        logger.critical(f"Possible Database Inconsistency for {devpath}: '{err}'")
        return False
    os.close(handle)
    return True


def _tray_status(devpath, logger=logging):
    """
    Get the Status of the CDROM Drive.

    Note: This should be considered as internal function to the
          SystemDrives class and should not be called directly.

    Parameters
    ----------
    devpath: str
        path to cdrom

    Returns
    -------
    int or None
        Returns `None` if a (known) error occured and `int` on
        success.  The values are defined in the linux kernel and
        referenced here:

        - `CDS_NO_INFO`
        - `CDS_NO_DISC`
        - `CDS_TRAY_OPEN`
        - `CDS_DRIVE_NOT_READY`
        - `CDS_DISC_OK`

        ioctl defines may (not very likely) change. See
        [linux/cdrom.h](https://github.com/torvalds/linux/blob/master/include/uapi/linux/cdrom.h)
        for specifics.
    """
    try:
        disk_check = os.open(devpath, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError as err:
        logger.critical(f"Possibly Stale Mount Points detected: '{err}'")
        return None  # should be cleared in `ui.settings.DriveUtils.drives_update`
    except TypeError as err:
        logger.critical(f"Possible Database Inconsistency for {devpath}: '{err:s}'")
        return None  # inconsistency in SystemDrives.mount
    except OSError as err:
        # Sometimes ARM will log errors opening hard drives. this check should stop it
        if re.search(r'hd[a-j]|sd[a-j]|loop\d|nvme\d', devpath):
            logger.critical(f"The device '{devpath}' is not an optical drive")
            return None  # inconsistency in SystemDrives.mount
        # We queried a mount path that is not present any more.
        if 'No such device or address' in str(err):
            logger.critical(f"Drive Mount Path does not exist: '{err}'")
            return None  # drive mount path missing. Should get cleared by `ui.settings.DriveUtils.drives_update`
        raise err
    try:
        return fcntl.ioctl(disk_check, 0x5326, 0)
    except OSError as err:
        logger.warning(f"Failed to check status for '{devpath}': {err:s}")
        return None
    finally:
        os.close(disk_check)


class SystemDrives(db.Model):  # pylint: disable=too-many-instance-attributes
    """
    Class to hold the system cd/dvd/Blu-ray drive information
    """
    drive_id = db.Column(db.Integer, index=True, primary_key=True)

    # static information:
    serial_id = db.Column(db.String(100))  # maker+serial (static identification)
    maker = db.Column(db.String(25))
    model = db.Column(db.String(50))
    serial = db.Column(db.String(25))
    connection = db.Column(db.String(5))
    read_cd = db.Column(db.Boolean)
    read_dvd = db.Column(db.Boolean)
    read_bd = db.Column(db.Boolean)

    # dynamic information (subject to change):
    mount = db.Column(db.String(100))  # mount point (may change on startup)
    firmware = db.Column(db.String(10))
    location = db.Column(db.String(255))
    stale = db.Column(db.Boolean)  # indicate that this drive was not found.
    mdisc = db.Column(db.SmallInteger)
    _tray = None  # Numeric Tray Status

    # cross references:
    job_id_current = db.Column(db.Integer, db.ForeignKey("job.job_id"))
    job_id_previous = db.Column(db.Integer, db.ForeignKey("job.job_id"))
    drive_mode = db.Column(db.String(100))
    # relationship - join current and previous jobs to the jobs table
    job_current = db.relationship("Job", foreign_keys=[job_id_current],
                                  backref=db.backref("drive", uselist=False))
    job_previous = db.relationship("Job", foreign_keys=[job_id_previous])  # No backref, one-way only

    # user input:
    name = db.Column(db.String(100))
    description = db.Column(db.Unicode(200))

    def __init__(self):
        # mark drive info as outdated
        self.stale = True
        self.mdisc = None

        # cross references
        self.job_id_current = None
        self.job_id_previous = None

        # user input
        self.description = ""
        self.name = ""
        self.drive_mode = "auto"

    def update(self, drive):
        """
        Update database object with drive information

        Parameters
        ----------
        drive: arm.ui.settings.DriveUtils.Drive
        """
        # static information
        self.serial_id = drive.serial_id
        self.maker = drive.maker
        self.model = drive.model
        self.serial = drive.serial
        self.connection = drive.connection
        self.read_cd = drive.read_cd
        self.read_dvd = drive.read_dvd
        self.read_bd = drive.read_bd
        # dynamic information
        self.mount = drive.mount
        self.firmware = drive.firmware
        self.location = drive.location
        # mark drive info as updated
        self.stale = False

    @property
    def type(self):
        """find the Drive type (CD, DVD, Blu-ray) from the udev values"""
        temp = ""
        if self.read_cd:
            temp += "CD"
        if self.read_dvd:
            temp += "/DVD"
        if self.read_bd:
            temp += "/BluRay"
        return temp

    def tray_status(self):
        """Query and update tray status.
        """
        if self.stale:
            self.tray = None
        else:
            self.tray = _tray_status(self.mount)
        logging.debug(f"Drive '{self.mount}': tray status '{self.tray}'")
        return self.tray

    def wait_until_ready(self, timeout, interval=1.0, logger=logging, sleep=time.sleep, clock=time.monotonic):
        """
        Poll the tray status until the drive reports a readable disc or ``timeout`` seconds pass.

        udev starts ARM when the tray closes, which is well before a slow drive can read the disc:
        an HL-DT-ST DT80N reports DRIVE_NOT_READY (with a flicker of TRAY_OPEN) for about 16 s
        after the close with a dual-layer DVD.

        :param timeout: seconds to keep polling
        :param interval: seconds between polls
        :param logger: where progress goes (the job log does not exist yet at this point)
        :param sleep: injectable for tests
        :param clock: injectable for tests
        :return: ``(ready, seen)``: whether the disc can be read, and the distinct tray states seen in order
        """
        deadline = clock() + max(float(timeout), 0.0)
        seen = []
        last = None
        while True:
            status = self.tray_status()
            readable = status == CDS.DISC_OK and _medium_readable(self.mount, logger)
            if not seen or seen[-1] != status:
                seen.append(status)
            if (status, readable) != last:
                last = (status, readable)
                if readable:
                    logger.info(f"Drive [{self.mount}] has a readable disc")
                elif status == CDS.DISC_OK:
                    logger.info(f"Drive [{self.mount}] reports a disc but cannot read it yet; waiting up to {timeout}s")
                else:
                    logger.info(f"Drive [{self.mount}] is not ready (tray status: {status.name}); "
                                f"waiting up to {timeout}s")
            if readable:
                return True, seen
            remaining = deadline - clock()
            if remaining <= 0:
                return False, seen
            sleep(min(interval, remaining))

    @property
    def tray(self):
        """Tray Status EnumItem
        """
        return CDS(self._tray)

    @tray.setter
    def tray(self, value):
        self._tray = CDS(value).value

    @property
    def open(self):
        """Drive tray open"""
        return self.tray == CDS.TRAY_OPEN

    @property
    def ready(self):
        """Drive has medium loaded and is ready for reading."""
        return self.tray == CDS.DISC_OK

    def eject(self, method="eject"):
        """Open or close the drive

        The tray ioctl goes to the drive first; [eject](https://man7.org/linux/man-pages/man1/eject.1.html)
        (``--cdrom --scsi``) is only the fallback for drives that reject it, since eject(1) itself
        fails wherever the device node is a mount target (see ``_tray_ioctl``).

        Parameters
        ----------
        method: str: eject (default), close, toggle

        Returns
        -------
        str or None
            Returns `None` if no (known) error occurred and `str` with the error
            message otherwise.
        """
        try:
            close = method == "close" or (method == "toggle" and self.tray_status() == CDS.TRAY_OPEN)
            error = self._eject_ioctl(close)
            if error is None:
                return None
            logging.warning(f"Tray ioctl did not move '{self.mount}' ({error}); trying a SCSI eject")
            error = self._eject_scsi(close)
            if error is None:
                return None
            logging.warning(f"SCSI eject failed for '{self.mount}' ({error}); trying eject(1)")
            return self._eject_command(method)
        finally:
            self.release_current_job()

    def _tray_moved(self, close, sleep=time.sleep, clock=time.monotonic):
        """Poll the tray status until it shows the request took effect, or TRAY_MOVE_TIMEOUT passes."""
        deadline = clock() + TRAY_MOVE_TIMEOUT
        while True:
            status = self.tray_status()
            if close and status != CDS.TRAY_OPEN:
                return True
            if not close and status not in (CDS.DISC_OK, CDS.ERROR):
                return True
            if clock() >= deadline:
                return False
            sleep(0.5)

    def _eject_ioctl(self, close):
        """Send the tray request straight to the drive and check the tray moved; error text or None."""
        try:
            _tray_ioctl(self.mount, CDROMCLOSETRAY if close else CDROMEJECT)
        except (OSError, TypeError) as err:
            return str(err)
        if not self._tray_moved(close):
            return f"tray status still {self.tray.name} after {TRAY_MOVE_TIMEOUT:.0f}s"
        return None

    def _eject_scsi(self, close):
        """Unlock the medium and move the tray by SCSI command; error text or None."""
        try:
            _scsi_tray(self.mount, close)
        except (OSError, TypeError) as err:
            return str(err)
        if not self._tray_moved(close):
            return f"tray status still {self.tray.name} after {TRAY_MOVE_TIMEOUT:.0f}s"
        return None

    def _eject_command(self, method):
        """Run eject(1) for ``method``; error text or None."""
        methods = {
            "eject": [],
            "close": ["--trayclose"],
            "toggle": ["--traytoggle"],
        }
        options = ["--cdrom", "--scsi"]  # exclude floppy and tape drives
        cmd = ["eject", "--verbose"] + options + methods[method] + [self.mount]
        try:
            arm_subprocess(cmd, check=True)
        except CalledProcessError as err:
            return err.output or err.stderr
        return None

    def debug(self, logger=logging):
        """
        Report the current drive status (debug)
        """
        logger.debug("*********")
        logger.debug(f"Name: '{self.name}'")
        logger.debug(f"Drive: '{self.maker} {self.model} ({self.firmware})'")
        logger.debug(f"Type: {self.type}")
        logger.debug(f"Description: '{self.description}'")
        logger.debug(f"Mount: '{self.mount}'")
        jobs = (
            ("Current", self.job_id_current, self.job_current),
            ("Previous", self.job_id_previous, self.job_previous),
        )
        for job_name, job_id, job in jobs:
            logger.debug(f"Job {job_name}: {job_id})")
            if job_id and job is not None:
                logger.debug(f"Job - Status: {job.status}")
                logger.debug(f"Job - Type: {job.video_type}")
                logger.debug(f"Job - Title: {job.title}")
                logger.debug(f"Job - Year: {job.year}")
        logger.debug("*********")

    @property
    def processing(self):
        """Drive has an associated job."""
        return self.job_current is not None

    def release_current_job(self):
        if self.job_id_current is None:
            return
        logging.info("Releasing current job from drive")
        # Preserve as previous job
        self.job_id_previous = self.job_id_current
        # Resetting current job to None in order to release it
        self.job_id_current = None

    def new_job(self, job_id):
        """new job assigned to the drive, update with new job id, and previous job_id"""
        logging.info("Creating new job for drive.")
        self.release_current_job()
        self.job_id_current = job_id
