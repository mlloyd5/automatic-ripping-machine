"""
Tests for arm.ripper.utils.job_dupe_check: a new disc takes the title of a previous successful rip
with the same label, except when the label is generic (DVD_VIDEO, BDROM) and the disc is not the
same one. On the t620 an Anger Management DVD labelled DVD_VIDEO took "Fools Rush In" from an
earlier DVD_VIDEO rip, then was refused as a duplicate of it.

Builds a throwaway config and SQLite database under a temp folder like test_ui_bonus_disc.py and
skips itself when the ARM package cannot be imported on this host. Jobs are written straight into
the database (the Job constructor needs udev and a drive).
"""
import atexit
import importlib
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

_ORIGINAL_CONFIG_FILE = os.environ.get("ARM_CONFIG_FILE")
_TEMP_ROOT = tempfile.mkdtemp(prefix="arm-dupe-check-test-")
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
    from arm.ripper import utils
    IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on the host
    IMPORT_ERROR = error

# The SQLAlchemy engine binds to the DBFILE of whichever test module imports ARM first and keeps it
# for the whole run, so the folder is removed at interpreter exit, not at module teardown
atexit.register(shutil.rmtree, _TEMP_ROOT, ignore_errors=True)


def tearDownModule():
    if _ORIGINAL_CONFIG_FILE is None:
        os.environ.pop("ARM_CONFIG_FILE", None)
    else:
        os.environ["ARM_CONFIG_FILE"] = _ORIGINAL_CONFIG_FILE


def make_job(**fields):
    """A Job row without the constructor (which reads udev and the drive)."""
    job = Job.__mapper__.class_manager.new_instance()
    defaults = dict(devpath="/dev/sr2", status="success", disctype="dvd", title="Fools Rush In", year="1997",
                    label="DVD_VIDEO", crc_id="bbd3159935117a6f", start_time=datetime(2026, 10, 26, 12, 0),
                    hasnicetitle=True, ejected=True, updated=False, video_type="movie", poster_url="")
    defaults.update(fields)
    for key, value in defaults.items():
        setattr(job, key, value)
    db.session.add(job)
    db.session.commit()
    return job


def new_disc(**fields):
    """The job for the disc just inserted: identified as nothing but its label."""
    defaults = dict(status="active", title=fields.get("label", "DVD_VIDEO"), year=None, hasnicetitle=False,
                    video_type="unknown", ejected=False, start_time=datetime(2026, 10, 26, 23, 35))
    defaults.update(fields)
    return make_job(**defaults)


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM cannot be imported here: {IMPORT_ERROR}")
class TestJobDupeCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["ARM_CONFIG_FILE"] = _CONFIG_PATH
        if cfg.arm_config_path != _CONFIG_PATH:
            importlib.reload(cfg)

    def setUp(self):
        Job.query.delete()
        db.session.commit()

    def test_generic_label_other_disc_keeps_its_label(self):
        """Anger Management (another DVD_VIDEO disc) is not Fools Rush In and not a duplicate of it."""
        make_job()
        job = new_disc(label="DVD_VIDEO", crc_id="707f03b441553ed9")
        self.assertFalse(utils.job_dupe_check(job))
        self.assertEqual(job.title, "DVD_VIDEO")
        self.assertFalse(job.hasnicetitle)

    def test_generic_label_same_disc_takes_previous_title(self):
        """The Fools Rush In disc put back in is still known by its hash."""
        make_job()
        job = new_disc(label="DVD_VIDEO", crc_id="bbd3159935117a6f")
        self.assertTrue(utils.job_dupe_check(job))
        self.assertEqual((job.title, job.year, job.video_type), ("Fools Rush In", "1997", "movie"))
        self.assertTrue(job.hasnicetitle)

    def test_generic_label_without_hash_is_not_matched(self):
        """A Blu-ray labelled BDROM has no CRC64 to tell it apart, so nothing is borrowed."""
        make_job(label="BDROM", disctype="bluray", crc_id=None, title="RED", year="2010")
        job = new_disc(label="BDROM", disctype="bluray", crc_id=None)
        self.assertFalse(utils.job_dupe_check(job))
        self.assertEqual(job.title, "BDROM")

    def test_generic_label_identified_bluray_matches_same_film(self):
        """A BDROM Blu-ray that bdmt_eng.xml names RED (2010) is the RED disc ripped before: a duplicate."""
        make_job(label="BDROM", disctype="bluray", crc_id=None, title="RED", year="2010")
        job = new_disc(label="BDROM", disctype="bluray", crc_id=None, title="RED", year="2010", hasnicetitle=True,
                       video_type="movie")
        self.assertTrue(utils.job_dupe_check(job))
        self.assertEqual((job.title, job.year), ("RED", "2010"))

    def test_generic_label_identified_bluray_other_film_is_not_matched(self):
        """A BDROM Blu-ray named as another film does not take RED's title and is not its duplicate."""
        make_job(label="BDROM", disctype="bluray", crc_id=None, title="RED", year="2010")
        job = new_disc(label="BDROM", disctype="bluray", crc_id=None, title="Inception", year="2010",
                       hasnicetitle=True, video_type="movie")
        self.assertFalse(utils.job_dupe_check(job))
        self.assertEqual((job.title, job.year), ("Inception", "2010"))

    def test_real_label_matches_on_label_alone(self):
        """A label that names the film still matches a previous rip of another pressing."""
        make_job(label="BLAZING_SADDLES", crc_id="1111111111111111", title="Blazing Saddles", year="1974")
        job = new_disc(label="BLAZING_SADDLES", crc_id="2222222222222222")
        self.assertTrue(utils.job_dupe_check(job))
        self.assertEqual((job.title, job.year), ("Blazing Saddles", "1974"))


if __name__ == "__main__":
    unittest.main()
