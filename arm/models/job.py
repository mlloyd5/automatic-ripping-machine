import enum
import logging
import os
import psutil
import pyudev
import shutil
import subprocess
import time

from datetime import datetime as dt
from prettytable import PrettyTable
from sqlalchemy.ext.hybrid import hybrid_property

from arm.ripper import music_brainz
from arm.ui import db
import arm.config.config as cfg

# THESE IMPORTS ARE REQUIRED FOR THE db.Relationships to work
from arm.models.track import Track  # noqa: F401
from arm.models.config import Config  # noqa: F401
from arm.models.film import Film  # noqa: F401


def _disc_dir_exists(mountpoint, name):
    """Check if a directory name exists in a disc mountpoint.

    Uses os.listdir on the parent rather than os.path.isdir on the target,
    to handle burned UDF discs where the directory entry is visible in the
    listing but os.stat() fails with a stale file handle.
    """
    try:
        return name.upper() in [e.upper() for e in os.listdir(mountpoint)]
    except OSError:
        return False


def _listdir_safe(path):
    """Return os.listdir(path), or [] if the path is inaccessible."""
    try:
        return os.listdir(path)
    except OSError:
        return []


class JobState(str, enum.Enum):
    """Possible states for Job.status.

    The origin of the states is getting unclear at this point. Therefore, the
    possible `Job.status` states are defined as fixed enums to handle and group
    them better. Some come from CD, some from DVD ripping.

    Note: The timestamps could also be saved particularily for each step to
          show, for example, the pure transcoding time without the waiting
          time.
    """

    # Job Finished States
    SUCCESS = "success"
    FAILURE = "fail"

    # Manual wait (see job.config.MANUAL_WAIT)
    MANUAL_WAIT_STARTED = "waiting"

    # Job Initialized or Pending
    IDLE = "active"
    """An Idle Job may proceed to ripping or to finished.

    - When initializing a job, the job is set to active
    - After Handbrake finishes, Job is set to active
    - After ABCD finishes, Job is set to active
    """

    # Video Ripping States
    VIDEO_RIPPING = "ripping"
    """Indicate that makemkv is ripping."""
    VIDEO_WAITING = "waiting"
    """Indicate that the job waits for user input or for the next queue slot."""
    VIDEO_INFO = "info"
    """Indicate that the job calls makemkv info"""

    # Audio ripping states
    AUDIO_RIPPING = "ripping"

    # Transcoding states
    TRANSCODE_ACTIVE = "transcoding"
    TRANSCODE_WAITING = "waiting_transcode"


JOB_STATUS_FINISHED = {
    JobState.SUCCESS,
    JobState.FAILURE,
}
JOB_STATUS_RIPPING = {
    JobState.AUDIO_RIPPING,
    JobState.VIDEO_RIPPING,
    JobState.MANUAL_WAIT_STARTED,  # <-- not ripping, but undistinguishable
    JobState.VIDEO_WAITING,
    JobState.VIDEO_INFO,
}
JOB_STATUS_TRANSCODING = {
    JobState.TRANSCODE_ACTIVE,
    JobState.TRANSCODE_WAITING,
}


_CDROM_ID_PATHS = ("/lib/udev/cdrom_id", "/usr/lib/udev/cdrom_id")
_BLKID_PATHS = ("/sbin/blkid", "/usr/sbin/blkid", "/bin/blkid", "/usr/bin/blkid")
_MEDIA_PROPERTY_PREFIXES = ("ID_CDROM_MEDIA", "ID_FS_")


def _find_tool(name, fallbacks):
    """``shutil.which`` plus the places udev helpers live that are not on a user's PATH."""
    found = shutil.which(name)
    if found:
        return found
    for candidate in fallbacks:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def _run_properties(cmd, timeout=60):
    """Run a udev helper and parse its ``KEY=VALUE`` output; empty on any failure."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        logging.debug(f"{cmd[0]} could not be run: {error}")
        return {}
    if result.returncode != 0:
        logging.debug(f"{cmd[0]} exited {result.returncode}: {result.stderr.strip()}")
        return {}
    properties = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip():
            properties[key.strip()] = value.strip()
    return properties


def read_media_properties(devpath):
    """
    Ask the drive itself what is loaded, with the helpers udev uses: ``cdrom_id`` for the media
    type (ID_CDROM_MEDIA_DVD, ..._BD, ..._TRACK_COUNT_AUDIO) and ``blkid`` for ID_FS_LABEL.

    udev's own copy of these properties can be stale when ARM starts: in the Docker image ARM
    runs as the child of the udev worker for the drive, so the "disc ready" event that carries
    the properties is still queued behind ARM itself, and the udev database shows the previous
    state of the drive (nothing loaded, or the last disc).

    :return: ``{"ID_CDROM_MEDIA": "1", ...}``, or ``{}`` when no medium is reported
    """
    cdrom_id = _find_tool("cdrom_id", _CDROM_ID_PATHS)
    if cdrom_id is None:
        logging.debug("cdrom_id not found; relying on udev for the disc properties")
        return {}
    properties = _run_properties([cdrom_id, devpath])
    if properties.get("ID_CDROM_MEDIA") != "1":
        return {}
    blkid = _find_tool("blkid", _BLKID_PATHS)
    if blkid:
        properties.update(_run_properties([blkid, "-p", "-o", "udev", devpath]))
    return properties


class Job(db.Model):
    """
    Job Class hold most of the details for each job
    connects to track, config
    """
    job_id = db.Column(db.Integer, primary_key=True)
    arm_version = db.Column(db.String(20))
    crc_id = db.Column(db.String(63))
    logfile = db.Column(db.String(256))
    start_time = db.Column(db.DateTime)
    stop_time = db.Column(db.DateTime)
    job_length = db.Column(db.String(12))
    status = db.Column(db.String(32))
    """Now that we have JobState, we should migrate this column.
    status = db.Column(
        db.Enum(JobState, name="job_state_enum", native_enum=False, validate_strings=True),
        nullable=False
    )
    """
    stage = db.Column(db.String(63))
    no_of_titles = db.Column(db.Integer)
    title = db.Column(db.String(256))
    title_auto = db.Column(db.String(256))
    title_manual = db.Column(db.String(256))
    year = db.Column(db.String(4))
    year_auto = db.Column(db.String(4))
    year_manual = db.Column(db.String(4))
    video_type = db.Column(db.String(20))
    video_type_auto = db.Column(db.String(20))
    video_type_manual = db.Column(db.String(20))
    imdb_id = db.Column(db.String(15))
    imdb_id_auto = db.Column(db.String(15))
    imdb_id_manual = db.Column(db.String(15))
    poster_url = db.Column(db.String(256))
    poster_url_auto = db.Column(db.String(256))
    poster_url_manual = db.Column(db.String(256))
    disc_photo = db.Column(db.String(256))  # filename inside DISC_PHOTO_PATH
    disc_photo_ocr = db.Column(db.Text)  # raw OCR text from the disc photo
    disc_photo_vision = db.Column(db.Text)  # JSON the vision model read off the disc photo
    disc_barcode = db.Column(db.String(32))  # UPC/EAN scanned from the case before the tray closed
    disc_barcode_title = db.Column(db.String(256))  # product title the barcode looked up to
    sleeve_id = db.Column(db.String(16), index=True)  # code on the sleeve label (T6-0001...), assigned once
    label_printed = db.Column(db.DateTime)  # when the sleeve label was last printed; None = still to print
    box = db.Column(db.String(64))  # box the sleeve was filed in
    # No foreign key constraints: a film -> job -> film cycle would make Job.films ambiguous
    bonus_of_job_id = db.Column(db.Integer)
    """Set when this disc is the bonus disc of an earlier job: its titles are filed as that film's extras"""
    bonus_of_film_id = db.Column(db.Integer)
    """With bonus_of_job_id: the film of a double feature (a box set's supplemental disc) the extras belong to"""
    continuation_of_job_id = db.Column(db.Integer)
    """Set when this disc is the second side or disc of an earlier job's film: its main title is appended to it"""
    continues = db.Column(db.Boolean)
    """The film goes on on another side or disc still to be ripped: the completed shipper holds the folder till then"""
    devpath = db.Column(db.String(15))
    mountpoint = db.Column(db.String(20))
    hasnicetitle = db.Column(db.Boolean)
    errors = db.Column(db.Text)
    disctype = db.Column(db.String(20))  # dvd/bluray/data/music/unknown
    label = db.Column(db.String(256))
    path = db.Column(db.String(256))
    ejected = db.Column(db.Boolean)
    updated = db.Column(db.Boolean)
    pid = db.Column(db.Integer)
    pid_hash = db.Column(db.Integer)
    is_iso = db.Column(db.Boolean)
    manual_start = db.Column(db.Boolean)
    manual_mode = db.Column(db.Boolean)
    tracks = db.relationship('Track', backref='job', lazy='dynamic')
    films = db.relationship('Film', backref='job', lazy='dynamic', order_by='Film.position')
    """The films on a disc that carries several (double features); empty for an ordinary disc"""
    config = db.relationship('Config', uselist=False, backref="job")
    bonus_of = db.relationship('Job', primaryjoin='foreign(Job.bonus_of_job_id) == remote(Job.job_id)',
                               viewonly=True)
    """The job whose film this bonus disc belongs to; None for an ordinary disc"""
    bonus_of_film = db.relationship('Film', primaryjoin='foreign(Job.bonus_of_film_id) == Film.film_id',
                                    viewonly=True)
    continuation_of = db.relationship('Job', primaryjoin='foreign(Job.continuation_of_job_id) == remote(Job.job_id)',
                                      viewonly=True)
    """The job whose film this disc continues; None for an ordinary disc"""

    def __init__(self, devpath):
        """Return a disc object"""
        self.devpath = devpath
        self.mountpoint = ""
        self.hasnicetitle = False
        self.video_type = "unknown"
        self.ejected = False
        self.updated = False
        if cfg.arm_config['VIDEOTYPE'] != "auto":
            self.video_type = cfg.arm_config['VIDEOTYPE']
        self.parse_udev()
        self.get_pid()
        self.stage = str(round(time.time() * 100))
        self.manual_start = False
        self.manual_mode = False
        self.has_track_99 = False

        if self.disctype == "dvd" and not self.label:
            logging.info("No disk label Available. Trying lsdvd")
            command = f"lsdvd {devpath} | grep 'Disc Title' | cut -d ' ' -f 3-"
            lsdvdlbl = str(subprocess.check_output(command, shell=True).strip(), 'utf-8')
            self.label = lsdvdlbl

    def __str__(self):
        """Returns a string of the object"""

        return_string = self.__class__.__name__ + ": "
        for attr, value in self.__dict__.items():
            return_string = return_string + "(" + str(attr) + "=" + str(value) + ") "

        return return_string

    def __repr__(self):
        return f'<Job {self.label}>'

    def parse_udev(self):
        """Parse udev for properties of current disc"""
        context = pyudev.Context()
        device = pyudev.Devices.from_device_file(context, self.devpath)
        properties = dict(device.items())
        probed = read_media_properties(self.devpath)
        if probed:
            if "ID_CDROM_MEDIA" not in properties:
                logging.info("udev has no media properties for this drive yet; using what cdrom_id and blkid report")
            properties = {key: value for key, value in properties.items()
                          if not key.startswith(_MEDIA_PROPERTY_PREFIXES)}
            properties.update(probed)
        self.apply_disc_properties(properties)

    def apply_disc_properties(self, properties):
        """Set label and disctype from udev style ``ID_*`` properties"""
        self.disctype = "unknown"

        for key, value in properties.items():
            logging.debug(f"pyudev: {key}: {value}")
            if key == "ID_FS_LABEL":
                self.label = value
                if value == "iso9660":
                    self.disctype = "data"
            elif key == "ID_CDROM_MEDIA_BD":
                self.disctype = "bluray"
            elif key == "ID_CDROM_MEDIA_DVD":
                self.disctype = "dvd"
            elif key == "ID_CDROM_MEDIA_TRACK_COUNT_AUDIO":
                self.disctype = "music"
            else:
                continue

    def get_pid(self):
        """
        Get the jobs process id
        :return: None
        """
        pid = os.getpid()
        process_id = psutil.Process(pid)
        self.pid = pid
        self.pid_hash = hash(process_id)

    def get_disc_type(self, found_hvdvd_ts):
        """
        Checks/corrects the current disc-type
        :param found_hvdvd_ts:  gets pushed in from utils - saves importing utils
        :return: None
        """
        if self.disctype == "music":
            logging.debug("Disc is music.")
            self.label = music_brainz.main(self)
        elif _disc_dir_exists(self.mountpoint, "AUDIO_TS") and _listdir_safe(self.mountpoint + "/AUDIO_TS"):
            logging.debug(f"Found: {self.mountpoint}/AUDIO_TS")
            self.disctype = "data"
        elif _disc_dir_exists(self.mountpoint, "VIDEO_TS"):
            logging.debug(f"Found: {self.mountpoint}/VIDEO_TS")
            self.disctype = "dvd"
        elif _disc_dir_exists(self.mountpoint, "BDMV"):
            logging.debug(f"Found: {self.mountpoint}/BDMV")
            self.disctype = "bluray"
        elif _disc_dir_exists(self.mountpoint, "HVDVD_TS"):
            logging.debug(f"Found: {self.mountpoint}/HVDVD_TS")
            # do something here
        elif found_hvdvd_ts:
            logging.debug("Found file: HVDVD_TS")
            # do something here too
        else:
            logging.debug("Did not find valid dvd/bd files. Changing disc-type to 'data'")
            self.disctype = "data"

    def identify_audio_cd(self):
        """
        Get the title for audio cds to use for the logfile name.

        Needs the job class passed into it so it can be forwarded to mb

        return - only the logfile - setup_logging() adds the full path
        """
        # Use the music label if we can find it - defaults to music_cd.log
        disc_id = music_brainz.get_disc_id(self)
        logging.debug(f"music_id: {disc_id}")
        mb_title = music_brainz.get_title(disc_id, self)
        logging.debug(f"mm_title: {mb_title}")

        if mb_title == "not identified":
            self.label = self.title = mb_title
            return "music_cd"
        else:
            return mb_title

    def pretty_table(self):
        """Returns a string of the prettytable"""
        pretty_table = PrettyTable()
        pretty_table.field_names = ["Config", "Value"]
        pretty_table._max_width = {"Config": 50, "Value": 60}
        for attr, value in self.__dict__.items():
            if attr.startswith("_"):
                # SQLAlchemy's state, and what the ripper keeps on the job for itself (identify's lsdvd titles)
                continue
            if attr == "config":
                pretty_table.add_row([str(attr), str(value.pretty_table())])
            else:
                pretty_table.add_row([str(attr), str(value)])
        return str(pretty_table.get_string())

    def get_d(self):
        """
        Return a dict of class - exclude the _sa_instance_state and the other private attributes
        :return: dict containing all attribs from class
        """
        return_dict = {}
        for key, value in self.__dict__.items():
            if not key.startswith('_'):
                return_dict[str(key)] = str(value)
        return return_dict

    def eject(self):
        """Eject disc if it hasn't previously been ejected
        """
        if self.ejected:
            logging.debug("The drive associated with this job has already been ejected.")
            return
        if self.drive is None:
            logging.warning("No drive was backpopulated with this job!")
            return
        if not cfg.arm_config['AUTO_EJECT']:
            logging.info("Skipping auto eject")
            self.drive.release_current_job()  # release job without ejecting
            return
        self.drive.eject()
        self.ejected = True

    @hybrid_property
    def finished(self):
        return JobState(self.status) in JOB_STATUS_FINISHED

    @finished.expression
    def finished(cls):
        return cls.status.in_([js.value for js in JOB_STATUS_FINISHED])

    @property
    def idle(self):
        return JobState(self.status) == JobState.IDLE

    @property
    def ripping(self):
        return JobState(self.status) in JOB_STATUS_RIPPING

    @property
    def run_time(self):
        return abs(dt.now() - self.start_time).total_seconds()

    @property
    def ripping_finished(self):
        """Indicates that the ripping process has finished.

        Note: This usually means that we are transcoding and the drive is not
              currently used.
        """
        if self.finished:
            logging.info("Job is finished.")
            return True
        if not self.ripping:
            logging.info("Job is not ripping.")
            return True
        if self.drive is None:
            logging.info("No drive was backpopulated with this job!")
            return True
        if self.ejected:
            logging.info(f"Drive {self.devpath} was ejected. No ripping process active.")
            return True
        logging.info(f"Job is ripping {self.devpath}.")
        return False
