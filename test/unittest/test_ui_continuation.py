"""
Flask test-client tests for the split film page (arm/ui/jobs/continuation.py, /splitfilm): it lists recent
successful rips a disc can continue, marking a job stores the choice (continuation_of_job_id, added by a
migration) with the film's title and marks the film's job as ``continues``, and a job can be marked as
continuing on another side or disc by itself.

Like test_ui_boxes.py this builds a throwaway config and SQLite database (made by running the migrations)
under a temp folder and skips itself when the ARM package cannot be imported on this host. Jobs are
written straight into the database (the Job constructor needs udev and a drive).
"""
import atexit
import importlib
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

_ORIGINAL_CONFIG_FILE = os.environ.get("ARM_CONFIG_FILE")
_TEMP_ROOT = tempfile.mkdtemp(prefix="arm-ui-continuation-test-")
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
    from arm.ui import app, db
    import arm.ui.routes  # noqa: F401  (creates the database like runui.py does)
    from arm.models.job import Job
    from arm.models.film import Film
    from arm.models.notifications import Notifications
    from arm.ui.jobs import continuation
    from arm.ripper import utils as ripper_utils
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
    defaults = dict(devpath="/dev/sr1", status="success", disctype="dvd", title="Das Boot", year="1981",
                    label="DVD_VIDEO", start_time=datetime(2026, 10, 27, 12, 0),
                    stop_time=datetime(2026, 10, 27, 14, 0), hasnicetitle=True, ejected=True, updated=False,
                    video_type="movie", imdb_id="tt0082096", poster_url="http://das-boot")
    defaults.update(fields)
    for key, value in defaults.items():
        setattr(job, key, value)
    db.session.add(job)
    db.session.commit()
    return job


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM web UI cannot be imported here: {IMPORT_ERROR}")
class TestSplitFilmPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["ARM_CONFIG_FILE"] = _CONFIG_PATH
        if cfg.arm_config_path != _CONFIG_PATH:
            importlib.reload(cfg)
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["LOGIN_DISABLED"] = True
        cls.client = app.test_client()

    def setUp(self):
        Film.query.delete()
        Job.query.delete()
        Notifications.query.delete()
        db.session.commit()
        self.side_a = make_job()
        self.heat = make_job(title="Heat", year="1995", label="HEAT", disctype="bluray", devpath="/dev/sr0",
                             stop_time=datetime(2026, 10, 27, 15, 0), imdb_id="tt0113277")
        self.unidentified = make_job(title="LONG_FILM_D1", year="", video_type="unknown", label="LONG_FILM_D1",
                                     stop_time=datetime(2026, 10, 27, 9, 0), imdb_id=None, hasnicetitle=False)
        make_job(title="Up", status="fail", label="UP")
        make_job(title="Show", video_type="series", label="SHOW_D1")
        make_job(title="Album", disctype="music", label="ALBUM")
        self.bonus_disc = make_job(title="Heat", label="HEAT_BONUS", bonus_of_job_id=self.heat.job_id)
        self.side_b = make_job(status="waiting", label="DVD_VIDEO", title="DVD_VIDEO", year="", stop_time=None,
                               imdb_id=None, poster_url=None, hasnicetitle=False, video_type="unknown")

    def _page(self, job=None):
        response = self.client.get(f"/splitfilm?job_id={(job or self.side_b).job_id}")
        self.assertEqual(response.status_code, 200)
        return response.data.decode()

    def _post(self, job=None, **data):
        return self.client.post("/splitfilm", data={"job_id": (job or self.side_b).job_id, **data},
                                follow_redirects=True).data.decode()

    def test_lists_recent_rips(self):
        html = self._page()
        for job in (self.side_a, self.heat, self.unidentified):
            self.assertIn(f'value="job:{job.job_id}"', html)
        self.assertIn("Das Boot (1981)", html)
        self.assertIn("2026-10-27 15:00", html)
        # newest first; failed, series, music, bonus discs and the job itself are not offered
        self.assertLess(html.index(f'value="job:{self.heat.job_id}"'), html.index(f'value="job:{self.side_a.job_id}"'))
        for title in ("Up", "Show", "Album"):
            self.assertNotIn(f"<strong>{title}", html)
        self.assertNotIn(f'value="job:{self.bonus_disc.job_id}"', html)
        self.assertNotIn(f'value="job:{self.side_b.job_id}"', html)
        self.assertRegex(html, r'id="continuation_none" value="none"\s+checked')

    def test_mark_as_the_continuation_of_a_job(self):
        response = self.client.post("/splitfilm", data={"job_id": self.side_b.job_id,
                                                        "continuation_of": f"job:{self.side_a.job_id}"})
        self.assertEqual(response.status_code, 302)
        job = Job.query.get(self.side_b.job_id)
        self.assertEqual(job.continuation_of_job_id, self.side_a.job_id)
        self.assertEqual(job.continuation_of.job_id, self.side_a.job_id)
        self.assertEqual((job.title, job.year, job.imdb_id, job.poster_url, job.video_type, job.hasnicetitle),
                         ("Das Boot", "1981", "tt0082096", "http://das-boot", "movie", True))
        # The film's folder is held by the shipper from now on
        self.assertTrue(Job.query.get(self.side_a.job_id).continues)
        self.assertEqual(Notifications.query.count(), 1)
        # A continuation is offered neither as the film of another one nor of a bonus disc
        other = make_job(status="waiting", label="OTHER", stop_time=None)
        self.assertNotIn(f'value="job:{job.job_id}"', self._page(other))
        bonus_page = self.client.get(f"/bonusdisc?job_id={other.job_id}").data.decode()
        self.assertNotIn(f'value="job:{job.job_id}"', bonus_page)
        self.assertRegex(self._page(), rf'value="job:{self.side_a.job_id}"\s+checked')
        with mock.patch("arm.ui.utils.metadata_selector", return_value=None):
            detail = self.client.get(f"/jobdetail?job_id={job.job_id}").data.decode()
            first = self.client.get(f"/jobdetail?job_id={self.side_a.job_id}").data.decode()
        self.assertIn("Continues the film of", detail)
        self.assertIn(f"splitfilm?job_id={job.job_id}", detail)
        self.assertIn("continues on another side or disc", first)

    def test_title_fixed_by_hand_on_the_first_side(self):
        self.unidentified.title_manual, self.unidentified.year = "Lawrence of Arabia", "1962"
        db.session.commit()
        self._post(continuation_of=f"job:{self.unidentified.job_id}")
        job = Job.query.get(self.side_b.job_id)
        self.assertEqual((job.title, job.year, job.video_type), ("Lawrence of Arabia", "1962", "unknown"))

    def test_marking_clears_a_bonus_disc_mark_and_the_other_way_round(self):
        self.side_b.bonus_of_job_id = self.heat.job_id
        db.session.commit()
        self._post(continuation_of=f"job:{self.side_a.job_id}")
        job = Job.query.get(self.side_b.job_id)
        self.assertEqual((job.bonus_of_job_id, job.continuation_of_job_id), (None, self.side_a.job_id))
        self.assertTrue(Job.query.get(self.side_a.job_id).continues)
        html = self.client.post("/bonusdisc", data={"job_id": job.job_id, "bonus_of": f"job:{self.heat.job_id}"},
                                follow_redirects=True).data.decode()
        job = Job.query.get(self.side_b.job_id)
        self.assertEqual((job.bonus_of_job_id, job.continuation_of_job_id), (self.heat.job_id, None))
        # Side A no longer waits for this disc: the shipper may take it
        self.assertFalse(Job.query.get(self.side_a.job_id).continues)
        self.assertIn(f"job {self.side_a.job_id}'s folder is no longer held", html)

    def test_clear_the_choice(self):
        self._post(continuation_of=f"job:{self.side_a.job_id}")
        html = self._post(continuation_of="none")
        job = Job.query.get(self.side_b.job_id)
        self.assertIsNone(job.continuation_of_job_id)
        self.assertIsNone(job.continuation_of)
        self.assertFalse(Job.query.get(self.side_a.job_id).continues)
        # A mark set on side A by hand cannot be told from this one, so the message says it is gone
        self.assertIn(f"job {self.side_a.job_id}'s folder is no longer held", html)
        self.assertIn("mark it again on its Split Film page", html)

    def test_picking_another_film_clears_the_first_ones_mark(self):
        self._post(continuation_of=f"job:{self.side_a.job_id}")
        html = self._post(continuation_of=f"job:{self.heat.job_id}")
        self.assertEqual(Job.query.get(self.side_b.job_id).continuation_of_job_id, self.heat.job_id)
        self.assertFalse(Job.query.get(self.side_a.job_id).continues, "held by the shipper for good otherwise")
        self.assertTrue(Job.query.get(self.heat.job_id).continues)
        self.assertIn(f"job {self.side_a.job_id}'s folder is no longer held", html)
        # Saving the same choice again leaves the mark alone
        html = self._post(continuation_of=f"job:{self.heat.job_id}")
        self.assertTrue(Job.query.get(self.heat.job_id).continues)
        self.assertNotIn("no longer held", html)

    def test_the_films_mark_stays_while_another_disc_continues_it(self):
        third = make_job(status="ripping", stop_time=None, label="DVD_VIDEO_3",
                         continuation_of_job_id=self.side_a.job_id)
        self._post(continuation_of=f"job:{self.side_a.job_id}")
        self.assertTrue(ripper_utils.continuation_pending(self.side_a, self.side_b))
        html = self._post(continuation_of="none")
        self.assertTrue(Job.query.get(self.side_a.job_id).continues)
        self.assertNotIn("no longer held", html)
        self.assertFalse(ripper_utils.continuation_pending(self.side_a, third), "side B is no longer marked")
        # Once the other disc has finished (joined, or failed) the film no longer waits for it
        third.status = "success"
        db.session.commit()
        self.assertFalse(ripper_utils.continuation_pending(self.side_a, self.side_b))

    def test_refusals(self):
        self.assertIn("Pick a job from the list", self._post(continuation_of=""))
        self.assertIn("Pick a job from the list", self._post(continuation_of="film:1"))
        self.assertIn("cannot be the film", self._post(continuation_of=f"job:{self.side_b.job_id}"))
        self.assertIn("cannot be the film", self._post(continuation_of="job:9999"))
        self.assertIn("has finished", self._post(self.heat, continuation_of=f"job:{self.side_a.job_id}"))
        # Only what the page offers (a stale form or a hand-made POST)
        refused = [make_job(title="Up", status="fail", label="UP2"),
                   make_job(title="Show", video_type="series", label="SHOW_D2"),
                   make_job(title="Album", disctype="music", label="ALBUM2"),
                   make_job(title="Up", status="active", label="UP3", stop_time=None),
                   self.bonus_disc]
        for parent in refused:
            html = self._post(continuation_of=f"job:{parent.job_id}")
            self.assertIn("is not a finished DVD or Blu-ray film rip", html, parent.label)
        self.assertIsNone(Job.query.get(self.side_b.job_id).continuation_of_job_id)
        self.assertEqual(self.client.get("/splitfilm?job_id=9999").status_code, 404)
        self.assertEqual(self.client.get("/splitfilm?job_id=abc").status_code, 404)

    def test_typed_title_is_escaped_in_the_message(self):
        self.side_a.title_manual = "<b>Das Boot</b>"
        db.session.commit()
        html = self._post(continuation_of=f"job:{self.side_a.job_id}")
        self.assertIn("&lt;b&gt;Das Boot&lt;/b&gt;", html)
        self.assertNotIn("<b>Das Boot</b>", html)

    def test_mark_the_first_side_as_continuing(self):
        ripping = make_job(status="ripping", stop_time=None, label="DVD_VIDEO_A")
        self.assertIn("continues on another side or disc", self._post(ripping, continues="yes"))
        self.assertTrue(Job.query.get(ripping.job_id).continues)
        self.assertIn("Clear the mark", self._page(ripping))
        # A finished job too: the shipper holds its folder while it is set
        self._post(self.side_a, continues="yes")
        self.assertTrue(Job.query.get(self.side_a.job_id).continues)
        self.assertIn("no longer waits", self._post(self.side_a, continues="no"))
        self.assertFalse(Job.query.get(self.side_a.job_id).continues)
        series = make_job(title="Show", video_type="series", label="SHOW_D3")
        self.assertIn("is not a DVD or Blu-ray film rip", self._post(series, continues="yes"))
        self.assertIn("bonus disc or a continuation itself", self._post(self.bonus_disc, continues="yes"))
        self.assertFalse(Job.query.get(self.bonus_disc.job_id).continues)

    def test_candidates_are_limited_and_keep_the_current_choice(self):
        for number in range(5):
            make_job(title=f"Film {number}", stop_time=datetime(2026, 10, 28, number, 0))
        self.side_b.continuation_of_job_id = self.unidentified.job_id
        db.session.commit()
        candidates = continuation.continuation_candidates(self.side_b, limit=3)
        self.assertEqual([job.title for job in candidates], ["Film 4", "Film 3", "Film 2", "LONG_FILM_D1"])
        self.assertEqual(continuation.parse_choice("job:12"), ("job", 12))
        self.assertEqual(continuation.parse_choice("none"), ("none", None))
        self.assertEqual(continuation.parse_choice("film:12"), (None, None))

    def test_pages_link_to_the_split_film_page(self):
        for page in ("customTitle", "titlesearch"):
            html = self.client.get(f"/{page}?job_id={self.side_b.job_id}").data.decode()
            self.assertIn(f"splitfilm?job_id={self.side_b.job_id}", html, page)
        with mock.patch("arm.ui.utils.metadata_selector", return_value=None):
            detail = self.client.get(f"/jobdetail?job_id={self.side_a.job_id}").data.decode()
        self.assertIn(f"splitfilm?job_id={self.side_a.job_id}", detail)


if __name__ == "__main__":
    unittest.main()
