"""
Flask test-client tests for the double feature page (arm/ui/jobs/double_feature.py, /doublefeature): the
films of a disc are picked one by one from a title search and stored as the job's Film rows, the job takes
the first film's title once there are two, and a title fix forgets them (unless it names the first film).

Like test_ui_bonus_disc.py this builds a throwaway config and SQLite database (made by running the
migrations) under a temp folder and skips itself when the ARM package cannot be imported on this host.
Jobs are written straight into the database (the Job constructor needs udev and a drive).
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
_TEMP_ROOT = tempfile.mkdtemp(prefix="arm-ui-double-feature-test-")
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
    from arm.ui.jobs import double_feature
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
    defaults = dict(devpath="/dev/sr1", status="waiting", disctype="dvd", title="DVD_VIDEO", year="",
                    label="DVD_VIDEO", start_time=datetime(2026, 10, 27, 12, 0), hasnicetitle=False,
                    ejected=False, updated=False, video_type="movie")
    defaults.update(fields)
    for key, value in defaults.items():
        setattr(job, key, value)
    db.session.add(job)
    db.session.commit()
    return job


STUART = {"Title": "Stuart Little", "Year": "1999", "imdbID": "tt0164912", "id": 10137, "Type": "movie",
          "Poster": "http://image.tmdb.org/stuart.jpg"}
STUART_2 = {"Title": "Stuart Little 2", "Year": "2002", "imdbID": "tt0243585", "id": 9488, "Type": "movie",
            "Poster": "http://image.tmdb.org/stuart2.jpg"}
RUNTIMES = {10137: 84 * 60, 9488: 78 * 60}


def as_form(result):
    return {"action": "add", "title": result["Title"], "year": result["Year"], "imdb_id": result["imdbID"],
            "tmdb_id": str(result["id"]), "poster": result["Poster"]}


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM web UI cannot be imported here: {IMPORT_ERROR}")
class TestDoubleFeaturePage(unittest.TestCase):
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
        self.disc = make_job()
        for patch in (mock.patch.dict(cfg.arm_config, {"METADATA_PROVIDER": "tmdb"}),
                      mock.patch("arm.ui.metadata.tmdb_runtime", side_effect=RUNTIMES.get)):
            patch.start()
            self.addCleanup(patch.stop)

    def _post(self, data, job=None):
        response = self.client.post("/doublefeature", data={"job_id": (job or self.disc).job_id, **data},
                                    follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        return response.data.decode()

    def _films(self, job=None):
        db.session.commit()  # end this session's read, or it keeps seeing the rows the requests changed
        return [(film.position, film.title, film.year, film.runtime)
                for film in double_feature.films_of(Job.query.get((job or self.disc).job_id))]

    def _mark(self):
        self._post(as_form(STUART))
        self._post(as_form(STUART_2))

    def test_search_offers_films_to_add(self):
        search = {"Search": [STUART, dict(STUART, Title="Stuart Little", Type="series", imdbID="tt9999999")]}
        with mock.patch("arm.ui.utils.metadata_selector", return_value=search) as selector:
            html = self.client.get(f"/doublefeature?job_id={self.disc.job_id}&title=Stuart+Little&year=1999"
                                   ).data.decode()
        selector.assert_called_once_with("search", "Stuart Little", "1999")
        self.assertIn("No films yet", html)
        self.assertIn('name="imdb_id" value="tt0164912"', html)
        self.assertIn('name="tmdb_id" value="10137"', html)
        self.assertIn("Add as film 1", html)
        self.assertNotIn("tt9999999", html)  # series are not films

    def test_search_retries_without_the_year(self):
        with mock.patch("arm.ui.utils.metadata_selector", side_effect=[{"Search": []}, {"Search": [STUART]}]) as sel:
            self.assertEqual(double_feature.search("Stuart Little", "2005"), [STUART])
        self.assertEqual([call.args for call in sel.call_args_list],
                         [("search", "Stuart Little", "2005"), ("search", "Stuart Little", "")])

    def test_two_films_make_a_double_feature_named_after_the_first(self):
        html = self._post(as_form(STUART))
        self.assertIn("Added Stuart Little (1999) as film 1", html)
        self.assertIn("not a double feature yet", html)
        job = Job.query.get(self.disc.job_id)
        self.assertEqual((job.title, job.title_manual), ("DVD_VIDEO", None))  # one film is not a double feature
        html = self._post(as_form(STUART_2))
        self.assertIn("Added Stuart Little 2 (2002) as film 2", html)
        self.assertEqual(self._films(), [(1, "Stuart Little", "1999", 5040), (2, "Stuart Little 2", "2002", 4680)])
        job = Job.query.get(self.disc.job_id)
        self.assertEqual((job.title, job.title_manual, job.year, job.imdb_id, job.poster_url, job.hasnicetitle),
                         ("Stuart Little", "Stuart Little", "1999", "tt0164912", "http://image.tmdb.org/stuart.jpg",
                          True))
        self.assertEqual(Notifications.query.count(), 2)
        with mock.patch("arm.ui.utils.metadata_selector", return_value=None):
            detail = self.client.get(f"/jobdetail?job_id={self.disc.job_id}").data.decode()
        self.assertIn("This disc carries 2 films", detail)
        self.assertIn(f"doublefeature?job_id={self.disc.job_id}", detail)

    def test_reorder_remove_and_clear(self):
        self._mark()
        film_2 = Film.query.filter_by(job_id=self.disc.job_id, position=2).one()
        self.assertIn("is film 1", self._post({"action": "up", "film_id": film_2.film_id}))
        self.assertEqual([film[1] for film in self._films()], ["Stuart Little 2", "Stuart Little"])
        self.assertEqual(Job.query.get(self.disc.job_id).title, "Stuart Little 2")
        self.assertIn("is the first film already", self._post({"action": "up", "film_id": film_2.film_id}))
        self.assertIn("Removed Stuart Little 2 (2002)", self._post({"action": "remove", "film_id": film_2.film_id}))
        self.assertEqual(self._films(), [(1, "Stuart Little", "1999", 5040)])
        self.assertIn("no longer a double feature (1 film(s) removed)", self._post({"action": "clear"}))
        self.assertEqual(self._films(), [])

    def test_unknown_runtime_is_flagged(self):
        html = self._post(dict(as_form(STUART), tmdb_id="1"))
        self.assertIn("runtime is unknown", html)
        self.assertEqual(self._films(), [(1, "Stuart Little", "1999", None)])

    def test_omdb_runtime_is_looked_up_by_imdb_id(self):
        with mock.patch.dict(cfg.arm_config, {"METADATA_PROVIDER": "omdb"}), \
                mock.patch("arm.ui.metadata.omdb_runtime", return_value=5040) as omdb:
            self._post(as_form(STUART))
        omdb.assert_called_once_with("tt0164912")
        self.assertEqual(self._films()[0][3], 5040)

    def test_refusals(self):
        self._post(as_form(STUART))
        self.assertIn("is film 1 of this disc already", self._post(as_form(STUART)))
        self.assertIn("is film 1 of this disc already", self._post(dict(as_form(STUART), imdb_id="")))
        self.assertIn("Pick a film", self._post(dict(as_form(STUART_2), title="")))
        self.assertIn("is not an IMDb id", self._post(dict(as_form(STUART_2), imdb_id="javascript:1")))
        self.assertIn("is not a year", self._post(dict(as_form(STUART_2), year="abcd")))
        self.assertIn("Unknown action", self._post({"action": "explode"}))
        other = make_job(label="OTHER")
        self._post(as_form(STUART_2), job=other)
        foreign = Film.query.filter_by(job_id=other.job_id).one()
        self.assertIn("is not a film of job", self._post({"action": "remove", "film_id": foreign.film_id}))
        self.assertIn("is not a film of job", self._post({"action": "remove", "film_id": "abc"}))
        finished = make_job(status="success", label="DONE")
        self.assertIn("has finished", self._post(as_form(STUART), job=finished))
        bonus = make_job(label="BONUS", bonus_of_job_id=finished.job_id)
        self.assertIn("bonus disc", self._post(as_form(STUART), job=bonus))
        music = make_job(label="ALBUM", disctype="music")
        self.assertIn("not a DVD or Blu-ray", self._post(as_form(STUART), job=music))
        self.assertEqual(self._films(), [(1, "Stuart Little", "1999", 5040)])
        # a poster that is no web address is dropped
        self._post(dict(as_form(STUART_2), poster="javascript:alert(1)"))
        self.assertIsNone(Film.query.filter_by(job_id=self.disc.job_id, position=2).one().poster_url)
        self.assertEqual(self.client.get("/doublefeature?job_id=9999").status_code, 404)
        self.assertEqual(self.client.get("/doublefeature?job_id=abc").status_code, 404)

    def test_title_fix_forgets_the_films(self):
        self._mark()
        response = self.client.get(f"/updatetitle?job_id={self.disc.job_id}&title=Stuart+Little+2&year=2002"
                                   "&imdbID=tt0243585&type=movie&poster=http://x")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._films(), [])
        self.assertEqual(Job.query.get(self.disc.job_id).title, "Stuart Little 2")

    def test_title_fix_to_the_first_film_keeps_them(self):
        self._mark()
        self.client.get(f"/updatetitle?job_id={self.disc.job_id}&title=Stuart+Little&year=1999&imdbID=tt0164912"
                        "&type=movie&poster=http://x")
        self.assertEqual(len(self._films()), 2)

    def test_custom_title_forgets_the_films_unless_it_is_the_first(self):
        self._mark()
        self.client.get(f"/customTitle?job_id={self.disc.job_id}&title=Stuart+Little&year=1999")
        self.assertEqual(len(self._films()), 2)
        response = self.client.get(f"/customTitle?job_id={self.disc.job_id}&title=Something+Else&year=1999")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._films(), [])

    def test_same_title_ignores_the_ripper_hyphens(self):
        self.assertTrue(double_feature.same_title("Stuart-Little-2", "Stuart Little 2"))
        self.assertFalse(double_feature.same_title("Stuart-Little", "Stuart Little 2"))
        self.assertFalse(double_feature.same_title("", ""))
        film = Film(self.disc.job_id, 1, "Stuart-Little", "1999", None)
        db.session.add(film)
        db.session.add(Film(self.disc.job_id, 2, "Stuart-Little-2", "2002", None))
        db.session.commit()
        self.assertIn("is film 1 of this disc already", self._post(dict(as_form(STUART), imdb_id="")))
        self.assertEqual(double_feature.clear_for_title_fix(Job.query.get(self.disc.job_id), title="Stuart Little"), 0)

    def test_finished_job_keeps_its_films(self):
        self._mark()
        job = Job.query.get(self.disc.job_id)
        job.status = "success"
        db.session.commit()
        self.assertEqual(double_feature.clear_for_title_fix(job, imdb_id="tt0000001"), 0)
        self.assertEqual(len(self._films()), 2)

    def test_title_pages_link_to_the_double_feature_page(self):
        for page in ("customTitle", "titlesearch"):
            html = self.client.get(f"/{page}?job_id={self.disc.job_id}").data.decode()
            self.assertIn(f"doublefeature?job_id={self.disc.job_id}", html, page)
            self.assertIn("Two or more films on this disc?", html, page)
        self._mark()
        html = self.client.get(f"/titlesearch?job_id={self.disc.job_id}").data.decode()
        self.assertIn("marked as a double feature of 2 films", html)


if __name__ == "__main__":
    unittest.main()
