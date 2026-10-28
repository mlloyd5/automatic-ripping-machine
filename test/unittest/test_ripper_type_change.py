"""
Tests for a video type changed in the ui while the disc rips (after the manual-override wait): the rip is
filed under the type folder of the new type, completed/tv for a series and completed/movies for a movie,
not the one the rip started with (arm_ripper.rip_visual_media, retitle_final_directory, move_files_post;
utils.move_files).

The case is job 148 on the ts140: side A of Pride and Prejudice (1980), identified as the movie
"Letters-O-P-and-R" and set by hand to the series "Pride and Prejudice" during the rip. Its episodes went
to completed/movies/Pride and Prejudice (1980), which the completed-rip shipper sends to Plex as a movie.

Importing ``arm.ripper.arm_ripper`` pulls in the whole of ARM (config, Flask app, database), so a
throwaway arm.yaml is written first and pointed at with ARM_CONFIG_FILE. Needs the Linux
dependencies ARM itself needs (libdiscid, pyudev); skipped when they are missing.
"""
import importlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]

_TMP = tempfile.mkdtemp(prefix="arm_type_change_test_")
_CONFIG = os.path.join(_TMP, "arm.yaml")
with open(_CONFIG, "w") as _handle:
    _handle.write(f"INSTALLPATH: \"{REPO_ROOT}\"\n"
                  f"DBFILE: \"{_TMP}/arm.db\"\n"
                  f"LOGPATH: \"{_TMP}/logs/\"\n"
                  f"ABCDE_CONFIG_FILE: \"{REPO_ROOT}/setup/.abcde.conf\"\n"
                  f"RAW_PATH: \"{_TMP}/raw/\"\nTRANSCODE_PATH: \"{_TMP}/transcode/\"\n"
                  f"COMPLETED_PATH: \"{_TMP}/completed/\"\n"
                  f"DISC_PHOTO_PATH: \"{_TMP}/photos/\"\n"
                  "DISABLE_LOGIN: true\nSET_MEDIA_PERMISSIONS: false\n")
os.environ["ARM_CONFIG_FILE"] = _CONFIG

try:
    arm_ripper = importlib.import_module("arm.ripper.arm_ripper")
    utils = importlib.import_module("arm.ripper.utils")
    cfg = importlib.import_module("arm.config.config")
    IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on the host
    arm_ripper = utils = cfg = None
    IMPORT_ERROR = error


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class _Tracks:
    """Stand-in for the job's dynamic ``tracks`` relationship (only filter_by is used)."""

    def __init__(self, tracks):
        self._tracks = list(tracks)

    def filter_by(self, **criteria):
        return _Tracks(track for track in self._tracks
                       if all(getattr(track, key) == value for key, value in criteria.items()))

    def count(self):
        return len(self._tracks)

    def __iter__(self):
        return iter(self._tracks)


def _track(name):
    return SimpleNamespace(filename=name, ripped=True, source="MakeMKV", main_feature=False, length=3000,
                           chapters=0, filesize=1000, track_number=name[-6:-4])


def _fake_updater(args, obj, *_):
    for key, value in args.items():
        setattr(obj, key, value)
    return True


def _set_to_series(job):
    # What the ui's title search does for Pride and Prejudice (1980), the BBC serial
    job.title_manual, job.year, job.video_type = "Pride and Prejudice", "1980", "series"


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TypeChangeFixture(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="completed_", dir=_TMP)
        self.raw_path = os.path.join(self.root, "raw")
        self.completed = os.path.join(self.root, "completed")
        os.makedirs(self.raw_path)
        for patch in (mock.patch.object(utils, "database_updater", side_effect=_fake_updater),
                      mock.patch.dict(cfg.arm_config, {"ALLOW_DUPLICATES": True, "DELRAWFILES": False,
                                                       "DISC_PHOTO_COPY": False})):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _job(self, files, video_type="movie", title="Letters-O-P-and-R", year=""):
        for name in files:
            Path(self.raw_path, name).write_text(f"{name}\n")
        return SimpleNamespace(job_id=148, video_type=video_type, title=title, year=year, title_manual=None,
                               imdb_id=None, poster_url=None, hasnicetitle=True, label="PRIDE_PREJUDICE_A",
                               stage="177000148000", tracks=_Tracks(_track(name) for name in files), films=[],
                               path=None, bonus_of_job_id=None, bonus_of=None, bonus_of_film_id=None,
                               bonus_of_film=None, disctype="dvd", devpath="/dev/sr0", errors=[],
                               disc_photo=None, disc_barcode=None,
                               config=SimpleNamespace(MAINFEATURE=False, EXTRAS_SUB="extras", DEST_EXT="mkv",
                                                      COMPLETED_PATH=self.completed, RIPMETHOD="mkv",
                                                      SKIP_TRANSCODE=True, USE_FFMPEG=False,
                                                      TRANSCODE_PATH=os.path.join(self.root, "transcode"),
                                                      NOTIFY_RIP=False, NOTIFY_TRANSCODE=False,
                                                      GET_VIDEO_TITLE=False))

    def _folder(self, *parts, poster=True):
        folder = os.path.join(self.completed, *parts)
        os.makedirs(folder)
        if poster:
            Path(folder, "poster.png").write_bytes(b"png")
        return folder

    def _listing(self, *parts):
        """Every file under completed/<parts>, relative to it"""
        top = os.path.join(self.completed, *parts)
        return sorted(os.path.relpath(os.path.join(folder, name), top)
                      for folder, _dirs, names in os.walk(top) for name in names)


class TestRetitleFinalDirectoryType(TypeChangeFixture):
    """retitle_final_directory when the job's video type changed during the rip."""

    def test_series_set_by_hand_moves_to_tv(self):
        job = self._job([])
        folder = self._folder("movies", "Letters-O-P-and-R")
        _set_to_series(job)
        new = arm_ripper.retitle_final_directory(False, job, folder, "movies", "Letters-O-P-and-R")
        self.assertEqual(new, os.path.join(self.completed, "tv", "Pride and Prejudice (1980)"))
        self.assertEqual(job.path, new)
        self.assertFalse(os.path.exists(folder))
        self.assertTrue(os.path.isfile(os.path.join(new, "poster.png")))

    def test_type_alone_changed_moves_the_folder(self):
        # Same title, so the folder used to be kept: still under movies
        job = self._job([], title="Pride and Prejudice", year="1980")
        folder = self._folder("movies", "Pride and Prejudice (1980)")
        job.video_type = "series"
        new = arm_ripper.retitle_final_directory(False, job, folder, "movies", "Pride and Prejudice (1980)")
        self.assertEqual(new, os.path.join(self.completed, "tv", "Pride and Prejudice (1980)"))
        self.assertEqual(job.path, new)
        self.assertFalse(os.path.exists(folder))

    def test_movie_set_by_hand_moves_to_movies(self):
        job = self._job([], video_type="series", title="Heat", year="1995")
        folder = self._folder("tv", "Heat (1995)")
        job.video_type = "movie"
        new = arm_ripper.retitle_final_directory(False, job, folder, "tv", "Heat (1995)")
        self.assertEqual(new, os.path.join(self.completed, "movies", "Heat (1995)"))
        self.assertFalse(os.path.exists(folder))

    def test_unchanged_title_and_type_keep_the_folder(self):
        job = self._job([], title="Heat", year="1995")
        folder = self._folder("movies", "Heat (1995)")
        self.assertEqual(arm_ripper.retitle_final_directory(False, job, folder, "movies", "Heat (1995)"), folder)
        self.assertTrue(os.path.isfile(os.path.join(folder, "poster.png")))
        self.assertIsNone(job.path)

    def test_series_already_in_tv_gets_its_own_folder(self):
        existing = self._folder("tv", "Pride and Prejudice (1980)", poster=False)
        Path(existing, "A1_t00.mkv").write_bytes(b"side B")
        job = self._job([])
        folder = self._folder("movies", "Letters-O-P-and-R")
        _set_to_series(job)
        new = arm_ripper.retitle_final_directory(False, job, folder, "movies", "Letters-O-P-and-R")
        self.assertEqual(new, f"{existing}_{job.stage}")
        self.assertEqual(os.listdir(existing), ["A1_t00.mkv"])


class TestRipVisualMediaTypeChange(TypeChangeFixture):
    """rip_visual_media with the type changed in the ui while MakeMKV ripped (seen at db.session.refresh)."""

    def _rip(self, job, change):
        def save_poster(final_directory, _job):
            Path(final_directory, "poster.png").write_bytes(b"png")

        with mock.patch.object(arm_ripper.db.session, "refresh", side_effect=lambda _job: change(job)), \
                mock.patch.object(arm_ripper.db.session, "commit"), \
                mock.patch.object(arm_ripper, "rip_with_mkv", return_value=True), \
                mock.patch.object(arm_ripper.makemkv, "makemkv", return_value=self.raw_path), \
                mock.patch.object(utils, "save_disc_poster", side_effect=save_poster), \
                mock.patch.object(utils, "scan_emby"), \
                mock.patch.object(utils, "set_permissions"):
            arm_ripper.rip_visual_media(False, job, "log", 0)

    def test_job_148_episodes_go_to_tv(self):
        job = self._job(["A1_t00.mkv", "A1_t01.mkv", "A1_t02.mkv"])
        self._rip(job, _set_to_series)
        show = os.path.join(self.completed, "tv", "Pride and Prejudice (1980)")
        self.assertEqual(job.path, show)
        # Episodes straight in the show's folder (no extras folder for a series), with the disc's poster
        self.assertEqual(self._listing("tv"), [os.path.join("Pride and Prejudice (1980)", name) for name in
                                               ("A1_t00.mkv", "A1_t01.mkv", "A1_t02.mkv", "poster.png")])
        self.assertEqual(self._listing("movies"), [])
        self.assertEqual(os.listdir(os.path.join(self.completed, "movies")), [])

    def test_series_set_to_movie_goes_to_movies(self):
        job = self._job(["title_t00.mkv"], video_type="series", title="Heat", year="1995")

        def to_movie(job):
            job.video_type = "movie"

        self._rip(job, to_movie)
        self.assertEqual(job.path, os.path.join(self.completed, "movies", "Heat (1995)"))
        self.assertEqual(self._listing("movies"), [os.path.join("Heat (1995)", "Heat (1995).mkv"),
                                                   os.path.join("Heat (1995)", "poster.png")])
        self.assertEqual(os.listdir(os.path.join(self.completed, "tv")), [])

    def test_no_change_files_as_before(self):
        job = self._job(["title_t00.mkv"], title="Heat", year="1995")
        self._rip(job, lambda _job: None)
        self.assertEqual(self._listing(), [os.path.join("movies", "Heat (1995)", "Heat (1995).mkv"),
                                           os.path.join("movies", "Heat (1995)", "poster.png")])


if __name__ == '__main__':
    unittest.main()
