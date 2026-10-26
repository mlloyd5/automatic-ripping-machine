"""
Flask test-client tests for the bonus disc page (arm/ui/jobs/bonus.py, /bonusdisc): it lists recent
successful movie rips with the later films of their double features, and marking a job stores the choice
(bonus_of_job_id / bonus_of_film_id, added by a migration) with the film's title.

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
_TEMP_ROOT = tempfile.mkdtemp(prefix="arm-ui-bonus-test-")
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
    from arm.ui.jobs import bonus
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
    defaults = dict(devpath="/dev/sr1", status="success", disctype="dvd", title="Monsters, Inc.", year="2001",
                    label="Monsters,_Inc.", start_time=datetime(2026, 10, 26, 12, 0),
                    stop_time=datetime(2026, 10, 26, 13, 0), hasnicetitle=True, ejected=True, updated=False,
                    video_type="movie", imdb_id="tt0198781", poster_url="http://monsters")
    defaults.update(fields)
    for key, value in defaults.items():
        setattr(job, key, value)
    db.session.add(job)
    db.session.commit()
    return job


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM web UI cannot be imported here: {IMPORT_ERROR}")
class TestBonusDiscPage(unittest.TestCase):
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
        self.monsters = make_job()
        self.toy_story = make_job(title="Toy Story", year="1995", label="TOY_STORY_1_2", devpath="/dev/sr0",
                                  disctype="bluray", stop_time=datetime(2026, 10, 26, 14, 0), imdb_id="tt0114709")
        db.session.add(Film(self.toy_story.job_id, 1, "Toy Story", "1995", "tt0114709", runtime=4860))
        self.sequel = Film(self.toy_story.job_id, 2, "Toy Story 2", "1999", "tt0120363", runtime=5520)
        self.sequel.track_number = "1"
        db.session.add(self.sequel)
        db.session.commit()
        make_job(title="Heat", year="1995", status="fail", label="HEAT")
        make_job(title="Show", video_type="series", label="SHOW_D1")
        make_job(title="Album", disctype="music", label="ALBUM")
        self.disc = make_job(title="Monsters, Inc.", status="waiting", label="Monsters,_Inc.", stop_time=None,
                             imdb_id=None, poster_url=None)

    def _page(self, job=None):
        response = self.client.get(f"/bonusdisc?job_id={(job or self.disc).job_id}")
        self.assertEqual(response.status_code, 200)
        return response.data.decode()

    def test_lists_recent_movie_rips_and_their_later_films(self):
        html = self._page()
        self.assertIn("Monsters,_Inc.", html)
        self.assertIn(f'value="job:{self.monsters.job_id}"', html)
        self.assertIn(f'value="job:{self.toy_story.job_id}"', html)
        self.assertIn(f'value="film:{self.sequel.film_id}"', html)
        self.assertIn("Toy Story 2 (1999)", html)
        self.assertIn("2026-10-26 14:00", html)
        self.assertIn("/dev/sr0", html)
        # newest first; failed, series, music and the job itself are not offered
        self.assertLess(html.index(f'value="job:{self.toy_story.job_id}"'),
                        html.index(f'value="job:{self.monsters.job_id}"'))
        for title in ("Heat", "Show", "Album"):
            self.assertNotIn(f"<strong>{title}", html)
        self.assertNotIn(f'value="job:{self.disc.job_id}"', html)
        self.assertRegex(html, r'id="bonus_none" value="none"\s+checked')

    def test_mark_as_the_bonus_disc_of_a_job(self):
        response = self.client.post("/bonusdisc", data={"job_id": self.disc.job_id,
                                                        "bonus_of": f"job:{self.monsters.job_id}"})
        self.assertEqual(response.status_code, 302)
        job = Job.query.get(self.disc.job_id)
        self.assertEqual((job.bonus_of_job_id, job.bonus_of_film_id), (self.monsters.job_id, None))
        self.assertEqual(job.bonus_of.job_id, self.monsters.job_id)
        self.assertEqual((job.title, job.year, job.imdb_id, job.poster_url, job.hasnicetitle),
                         ("Monsters, Inc.", "2001", "tt0198781", "http://monsters", True))
        self.assertEqual(Notifications.query.count(), 1)
        # a bonus disc is not offered as the film of another one, and its own choice stays ticked
        other = make_job(status="waiting", label="OTHER", stop_time=None)
        self.assertNotIn(f'value="job:{job.job_id}"', self._page(other))
        self.assertRegex(self._page(), rf'value="job:{self.monsters.job_id}"\s+checked')
        with mock.patch("arm.ui.utils.metadata_selector", return_value=None):
            detail = self.client.get(f"/jobdetail?job_id={job.job_id}").data.decode()
        self.assertIn("Bonus disc of", detail)

    def test_mark_as_the_bonus_disc_of_a_later_film(self):
        self.client.post("/bonusdisc", data={"job_id": self.disc.job_id, "bonus_of": f"film:{self.sequel.film_id}"})
        job = Job.query.get(self.disc.job_id)
        self.assertEqual((job.bonus_of_job_id, job.bonus_of_film_id), (self.toy_story.job_id, self.sequel.film_id))
        self.assertEqual(job.bonus_of_film.title, "Toy Story 2")
        self.assertEqual((job.title, job.year, job.imdb_id), ("Toy Story 2", "1999", "tt0120363"))

    def test_clear_the_choice(self):
        self.client.post("/bonusdisc", data={"job_id": self.disc.job_id, "bonus_of": f"job:{self.monsters.job_id}"})
        self.client.post("/bonusdisc", data={"job_id": self.disc.job_id, "bonus_of": "none"})
        job = Job.query.get(self.disc.job_id)
        self.assertIsNone(job.bonus_of_job_id)
        self.assertIsNone(job.bonus_of)

    def test_refusals(self):
        def post(choice, job=None):
            response = self.client.post("/bonusdisc", data={"job_id": (job or self.disc).job_id, "bonus_of": choice},
                                        follow_redirects=True)
            return response.data.decode()
        self.assertIn("Pick a job or film", post(""))
        self.assertIn("Pick a job or film", post("job:x"))
        self.assertIn("cannot be the film", post(f"job:{self.disc.job_id}"))
        self.assertIn("cannot be the film", post("job:9999"))
        self.assertIn("Film 9999 not found", post("film:9999"))
        self.assertIn("has finished", post(f"job:{self.toy_story.job_id}", job=self.monsters))
        self.assertIsNone(Job.query.get(self.disc.job_id).bonus_of_job_id)
        self.assertEqual(self.client.get("/bonusdisc?job_id=9999").status_code, 404)
        self.assertEqual(self.client.get("/bonusdisc?job_id=abc").status_code, 404)

    def test_candidates_are_limited_and_keep_the_current_choice(self):
        for number in range(5):
            make_job(title=f"Film {number}", stop_time=datetime(2026, 10, 27, number, 0))
        self.disc.bonus_of_job_id = self.monsters.job_id
        db.session.commit()
        candidates = bonus.bonus_candidates(self.disc, limit=3)
        self.assertEqual([c['job'].title for c in candidates], ["Film 4", "Film 3", "Film 2", "Monsters, Inc."])
        self.assertEqual(bonus.parse_choice("film:12"), ("film", 12))
        self.assertEqual(bonus.parse_choice("none"), ("none", None))
        self.assertEqual(bonus.parse_choice("job:"), (None, None))

    def test_title_pages_link_to_the_bonus_disc_page(self):
        for page in ("customTitle", "titlesearch"):
            html = self.client.get(f"/{page}?job_id={self.disc.job_id}").data.decode()
            self.assertIn(f"bonusdisc?job_id={self.disc.job_id}", html, page)


if __name__ == "__main__":
    unittest.main()
