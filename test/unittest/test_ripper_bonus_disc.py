"""
Tests for bonus discs on the ripper side: a disc marked in the ui as the bonus disc of an earlier job is
ripped with BONUS_MINLENGTH in one pass, and every title it rips is filed in that film's extras folder as
``<disc label>_<file>``, never as the film's ``Title (Year).mkv`` (arm_ripper.apply_bonus_settings,
bonus_folder, file_bonus, move_files_post; utils.move_files, bonus_parent, copy_disc_photo).

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

_TMP = tempfile.mkdtemp(prefix="arm_bonus_test_")
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


def _track(number, length, chapters=0):
    return SimpleNamespace(track_number=str(number), filename=f"title_t{int(number):02}.mkv", length=length,
                           chapters=chapters, filesize=1000, ripped=True, source="MakeMKV", main_feature=False)


def _fake_updater(args, obj):
    for key, value in args.items():
        setattr(obj, key, value)
    return True


def _read(path):
    with open(path) as handle:
        return handle.read()


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class BonusFixture(unittest.TestCase):
    """A completed folder holding Monsters, Inc. (job 40, already ripped) and a bonus disc's raw folder."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bonus_", dir=_TMP)
        self.raw_path = os.path.join(self.root, "raw")
        self.completed = os.path.join(self.root, "completed")
        self.film_path = os.path.join(self.completed, "movies", "Monsters, Inc. (2001)")
        os.makedirs(self.raw_path)
        os.makedirs(os.path.join(self.film_path, "extras"))
        with open(os.path.join(self.film_path, "Monsters, Inc. (2001).mkv"), "w") as handle:
            handle.write("the film\n")
        with open(os.path.join(self.film_path, "extras", "title_t03.mkv"), "w") as handle:
            handle.write("the film disc's extra\n")
        self.parent = SimpleNamespace(job_id=40, title="Monsters, Inc.", year="2001", imdb_id="tt0198781",
                                      poster_url="http://poster", label="Monsters,_Inc.", stage="177000000000",
                                      video_type="movie", path=self.film_path, title_manual=None, films=[])
        self.patches = [mock.patch.object(utils, "database_updater", side_effect=_fake_updater),
                        mock.patch.object(utils, "set_permissions")]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def _job(self, tracks=(), parent=True, film=None, label="Monsters,_Inc.", extras_sub="extras", path=None):
        for track in tracks:
            with open(os.path.join(self.raw_path, track.filename), "w") as handle:
                handle.write(f"title {track.track_number}\n")
        parent = self.parent if parent else None
        return SimpleNamespace(job_id=41, video_type="movie", title="Monsters, Inc.", year="2001", title_manual=None,
                               imdb_id=None, poster_url=None, hasnicetitle=True, label=label, stage="177000100000",
                               tracks=_Tracks(tracks), films=[], path=path or self.film_path,
                               bonus_of_job_id=40 if parent else None, bonus_of=parent,
                               bonus_of_film_id=film.film_id if film else None, bonus_of_film=film,
                               disctype="dvd", devpath="/dev/sr0", errors=[], disc_photo=None, disc_barcode=None,
                               config=SimpleNamespace(MAINFEATURE=True, EXTRAS_SUB=extras_sub, DEST_EXT="mkv",
                                                      COMPLETED_PATH=self.completed, MINLENGTH="600",
                                                      MAXLENGTH="36000", SKIP_DUPLICATE_TITLES=True,
                                                      RIPMETHOD="best", SKIP_TRANSCODE=False, USE_FFMPEG=False,
                                                      TRANSCODE_PATH=os.path.join(self.root, "transcode"),
                                                      NOTIFY_RIP=False, NOTIFY_TRANSCODE=False))


class TestBonusParent(BonusFixture):
    def test_ordinary_job_is_not_a_bonus_disc(self):
        job = self._job(parent=False)
        self.assertEqual(utils.bonus_parent(job), (None, None))
        self.assertFalse(utils.is_bonus_disc(job))
        self.assertFalse(utils.is_bonus_disc(SimpleNamespace(job_id=1)), "a job without the columns at all")

    def test_parent_that_is_gone_is_ignored(self):
        job = self._job()
        job.bonus_of = None
        self.assertEqual(utils.bonus_parent(job), (None, None))

    def test_film_of_another_job_is_ignored(self):
        film = SimpleNamespace(film_id=7, job_id=99, position=2)
        self.assertEqual(utils.bonus_parent(self._job(film=film)), (self.parent, None))
        film.job_id = 40
        self.assertEqual(utils.bonus_parent(self._job(film=film)), (self.parent, film))

    def test_tag_is_the_label_made_safe_or_the_job_id(self):
        self.assertEqual(utils.bonus_tag(self._job(label="Monsters,_Inc.")), "Monsters_Inc")
        self.assertEqual(utils.bonus_tag(self._job(label="TOYDELUXE3")), "TOYDELUXE3")
        self.assertEqual(utils.bonus_tag(self._job(label=None)), "job41")
        self.assertEqual(utils.bonus_tag(self._job(label=" / ")), "job41")


class TestBonusSettings(BonusFixture):
    def test_rips_every_short_title_in_one_pass_with_the_films_title(self):
        job = self._job()
        job.title, job.year = "Monsters Inc Bonus", ""
        with mock.patch.dict(cfg.arm_config, {"BONUS_MINLENGTH": "30"}):
            self.assertTrue(arm_ripper.apply_bonus_settings(job))
        self.assertEqual((job.config.MINLENGTH, job.config.MAXLENGTH), ("30", "99999"))
        self.assertFalse(job.config.SKIP_DUPLICATE_TITLES)
        self.assertFalse(job.config.MAINFEATURE)
        self.assertEqual((job.title, job.year, job.imdb_id, job.poster_url, job.video_type),
                         ("Monsters, Inc.", "2001", "tt0198781", "http://poster", "movie"))

    def test_dvd_is_ripped_by_makemkv_and_transcoded_without_a_length_filter(self):
        # handbrake_all/ffmpeg_all pick titles by arm.yaml's MINLENGTH (600s here), not the job's
        for disctype in ("dvd", "bluray"):
            for method in ("best", "mkv", "backup", "backup_dvd"):
                for use_ffmpeg in (False, True):
                    job = self._job()
                    job.disctype, job.config.RIPMETHOD, job.config.USE_FFMPEG = disctype, method, use_ffmpeg
                    self.assertTrue(arm_ripper.apply_bonus_settings(job))
                    label = f"{disctype} {method} ffmpeg={use_ffmpeg}"
                    self.assertEqual(job.config.RIPMETHOD, "mkv", label)
                    self.assertTrue(arm_ripper.rip_with_mkv(job), label)
                    with mock.patch.object(arm_ripper.handbrake, "handbrake_mkv") as hb_mkv, \
                            mock.patch.object(arm_ripper.handbrake, "handbrake_all") as hb_all, \
                            mock.patch.object(arm_ripper.handbrake, "handbrake_main_feature") as hb_main, \
                            mock.patch.object(arm_ripper.ffmpeg, "ffmpeg_mkv") as ff_mkv, \
                            mock.patch.object(arm_ripper.ffmpeg, "ffmpeg_all") as ff_all, \
                            mock.patch.object(arm_ripper.ffmpeg, "ffmpeg_main_feature") as ff_main, \
                            mock.patch.object(arm_ripper.db.session, "commit"):
                        arm_ripper.start_transcode(job, "log", self.raw_path, self.root, 0)
                    self.assertEqual((ff_mkv if use_ffmpeg else hb_mkv).call_count, 1, label)
                    for other in (hb_all, hb_main, ff_all, ff_main, ff_mkv if not use_ffmpeg else hb_mkv):
                        other.assert_not_called()

    def test_film_of_a_double_feature_gives_its_own_title(self):
        film = SimpleNamespace(film_id=7, job_id=40, position=2, title="Toy Story 2", year="1999",
                               imdb_id="tt0120363", poster_url=None, folder_name="Toy Story 2 (1999)")
        job = self._job(film=film)
        self.assertTrue(arm_ripper.apply_bonus_settings(job))
        self.assertEqual((job.title, job.year, job.imdb_id), ("Toy Story 2", "1999", "tt0120363"))

    def test_ordinary_job_keeps_its_settings(self):
        job = self._job(parent=False)
        self.assertFalse(arm_ripper.apply_bonus_settings(job))
        self.assertEqual(job.config.MINLENGTH, "600")
        self.assertTrue(job.config.SKIP_DUPLICATE_TITLES)

    def test_minlength_default_and_bad_values(self):
        with mock.patch.dict(cfg.arm_config, {"BONUS_MINLENGTH": " 60 "}):
            self.assertEqual(arm_ripper.bonus_minlength(), 60)
        with mock.patch.dict(cfg.arm_config, {"BONUS_MINLENGTH": "a minute"}):
            self.assertEqual(arm_ripper.bonus_minlength(), arm_ripper.BONUS_MINLENGTH_DEFAULT)
        with mock.patch.dict(cfg.arm_config):
            cfg.arm_config.pop("BONUS_MINLENGTH", None)
            self.assertEqual(arm_ripper.bonus_minlength(), arm_ripper.BONUS_MINLENGTH_DEFAULT)


class TestBonusFolder(BonusFixture):
    def test_film_folder_while_it_is_here(self):
        self.assertEqual(arm_ripper.bonus_folder(self._job()), self.film_path)

    def test_holding_folder_once_the_film_has_shipped(self):
        shutil.rmtree(self.film_path)
        self.assertEqual(arm_ripper.bonus_folder(self._job()), f"{self.film_path} - bonus job41")

    def test_later_film_of_a_double_feature(self):
        film = SimpleNamespace(film_id=7, job_id=40, position=2, title="Toy Story 2", year="1999",
                               folder_name="Toy Story 2 (1999)")
        plain = os.path.join(self.completed, "movies", "Toy Story 2 (1999)")
        os.makedirs(plain)
        self.assertEqual(arm_ripper.bonus_folder(self._job(film=film)), plain)
        # file_film added the parent's stage when the plain folder was taken
        suffixed = f"{plain}_{self.parent.stage}"
        os.makedirs(suffixed)
        self.assertEqual(arm_ripper.bonus_folder(self._job(film=film)), suffixed)

    def test_first_film_is_the_parent_folder(self):
        film = SimpleNamespace(film_id=6, job_id=40, position=1, title="Toy Story", year="1995",
                               folder_name="Toy Story (1995)")
        self.assertEqual(arm_ripper.bonus_folder(self._job(film=film)), self.film_path)


class TestFileBonus(BonusFixture):
    def test_every_title_is_an_extra_and_the_film_is_untouched(self):
        # The longest title (a making-of) would be the "main feature" of an ordinary rip
        tracks = [_track(0, 2700, chapters=12), _track(1, 300), _track(3, 90)]
        job = self._job(tracks)
        with open(os.path.join(self.raw_path, "poster.png"), "w") as handle:
            handle.write("not a video\n")
        arm_ripper.move_files_post(self.raw_path, job)
        self.assertEqual(_read(os.path.join(self.film_path, "Monsters, Inc. (2001).mkv")), "the film\n")
        self.assertEqual(sorted(os.listdir(self.film_path)), ["Monsters, Inc. (2001).mkv", "extras"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.film_path, "extras"))),
                         ["Monsters_Inc_title_t00.mkv", "Monsters_Inc_title_t01.mkv", "Monsters_Inc_title_t03.mkv",
                          "title_t03.mkv"])
        self.assertEqual(_read(os.path.join(self.film_path, "extras", "title_t03.mkv")), "the film disc's extra\n")
        self.assertEqual(_read(os.path.join(self.film_path, "extras", "Monsters_Inc_title_t03.mkv")), "title 3\n")
        self.assertEqual(os.listdir(self.raw_path), ["poster.png"])

    def test_name_taken_by_an_earlier_bonus_disc_gets_a_number(self):
        with open(os.path.join(self.film_path, "extras", "Monsters_Inc_title_t00.mkv"), "w") as handle:
            handle.write("an earlier bonus disc\n")
        job = self._job([_track(0, 600)])
        self.assertEqual(arm_ripper.file_bonus(self.raw_path, job, os.listdir(self.raw_path)), 1)
        extras = os.path.join(self.film_path, "extras")
        self.assertEqual(_read(os.path.join(extras, "Monsters_Inc_title_t00.mkv")), "an earlier bonus disc\n")
        self.assertEqual(_read(os.path.join(extras, "Monsters_Inc_title_t00-2.mkv")), "title 0\n")

    def test_extras_sub_none_still_files_in_extras(self):
        job = self._job([_track(0, 600)], extras_sub="None")
        utils.move_files(self.raw_path, "title_t00.mkv", job, True)
        self.assertTrue(os.path.isfile(os.path.join(self.film_path, "extras", "Monsters_Inc_title_t00.mkv")))

    def test_holding_folder_gets_the_extras_folder(self):
        shutil.rmtree(self.film_path)
        job = self._job([_track(0, 600)], path=f"{self.film_path} - bonus job41")
        arm_ripper.move_files_post(self.raw_path, job)
        self.assertEqual(os.listdir(job.path), ["extras"])
        self.assertEqual(os.listdir(os.path.join(job.path, "extras")), ["Monsters_Inc_title_t00.mkv"])

    def test_ordinary_job_is_filed_as_before(self):
        job = self._job([_track(0, 600)], parent=False, path=os.path.join(self.completed, "movies", "Heat (1995)"))
        job.title, job.year = "Heat", "1995"
        arm_ripper.move_files_post(self.raw_path, job)
        self.assertEqual(os.listdir(job.path), ["Heat (1995).mkv"])


class TestBonusPhoto(BonusFixture):
    def test_photo_is_kept_beside_the_films_own(self):
        photos = os.path.join(_TMP, "photos")
        os.makedirs(photos, exist_ok=True)
        with open(os.path.join(photos, "bonus.jpg"), "w") as handle:
            handle.write("jpeg\n")
        with open(os.path.join(self.film_path, "disc.jpg"), "w") as handle:
            handle.write("the film disc\n")
        job = self._job()
        job.disc_photo = "bonus.jpg"
        with mock.patch.dict(cfg.arm_config, {"DISC_PHOTO_COPY": True, "DISC_PHOTO_PATH": photos}):
            copied = utils.copy_disc_photo(self.film_path, job, arm_ripper.bonus_photo_name(job))
        self.assertEqual(copied, os.path.join(self.film_path, "disc-bonus-Monsters_Inc.jpg"))
        self.assertEqual(_read(copied), "jpeg\n")
        self.assertEqual(_read(os.path.join(self.film_path, "disc.jpg")), "the film disc\n")


class TestMarkDuringRip(BonusFixture):
    """rip_visual_media when the bonus disc choice is made, or cleared, in the ui while the disc rips."""

    def _rip(self, job, mark):
        """Rip ``job`` (one title, MakeMKV, no transcode); the ui's choice changes (``mark``) during the rip."""
        def save_poster(final_directory, _job):
            with open(os.path.join(final_directory, "poster.png"), "w") as handle:
                handle.write("png\n")

        job.config.SKIP_TRANSCODE = True
        with mock.patch.object(arm_ripper.db.session, "refresh", side_effect=lambda _job: mark(job)), \
                mock.patch.object(arm_ripper.db.session, "commit"), \
                mock.patch.object(utils, "save_disc_poster", side_effect=save_poster) as poster, \
                mock.patch.object(arm_ripper.makemkv, "makemkv", return_value=self.raw_path), \
                mock.patch.object(utils, "scan_emby"), \
                mock.patch.dict(cfg.arm_config, {"DELRAWFILES": False, "ALLOW_DUPLICATES": True,
                                                 "DISC_PHOTO_COPY": False}):
            arm_ripper.rip_visual_media(False, job, "log", 0)
        return poster

    def _mark(self, job):
        job.bonus_of_job_id, job.bonus_of = 40, self.parent

    def test_marked_after_the_rip_started(self):
        job = self._job([_track(0, 600)], parent=False)
        job.title, job.year = "MONSTERS_BONUS", ""
        own = os.path.join(self.completed, "movies", "MONSTERS_BONUS")
        poster = self._rip(job, self._mark)
        poster.assert_called_once_with(own, job)
        # The folder made for the job (holding only the disc's poster) is gone, not left to block a re-rip
        self.assertFalse(os.path.exists(own))
        self.assertEqual(sorted(os.listdir(os.path.join(self.completed, "movies"))), ["Monsters, Inc. (2001)"])
        self.assertEqual(job.path, self.film_path)
        self.assertEqual(sorted(os.listdir(os.path.join(self.film_path, "extras"))),
                         ["Monsters_Inc_title_t00.mkv", "title_t03.mkv"])
        self.assertEqual(_read(os.path.join(self.film_path, "Monsters, Inc. (2001).mkv")), "the film\n")
        self.assertEqual(os.listdir(job.config.TRANSCODE_PATH + "/movies"), [])

    def test_marked_after_the_rip_started_once_the_film_has_shipped(self):
        shutil.rmtree(self.film_path)
        job = self._job([_track(0, 600)], parent=False)
        job.title, job.year = "MONSTERS_BONUS", ""
        self._rip(job, self._mark)
        holding = f"{self.film_path} - bonus job41"
        self.assertEqual(sorted(os.listdir(os.path.join(self.completed, "movies"))), [os.path.basename(holding)])
        self.assertEqual(os.listdir(os.path.join(holding, "extras")), ["Monsters_Inc_title_t00.mkv"])

    def test_cleared_after_the_rip_started(self):
        def clear(job):
            job.bonus_of_job_id = job.bonus_of = None

        job = self._job([_track(0, 600)])
        poster = self._rip(job, clear)
        poster.assert_not_called()
        # Filed the usual way, beside the film's folder rather than in it
        own = f"{self.film_path}_{job.stage}"
        self.assertEqual(job.path, own)
        self.assertEqual(os.listdir(own), ["Monsters, Inc. (2001).mkv"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.film_path, "extras"))), ["title_t03.mkv"])
        self.assertEqual(os.listdir(job.config.TRANSCODE_PATH + "/movies"), [])

    def test_unused_folder_is_never_the_bonus_folder(self):
        arm_ripper.remove_unused_final_dir(self.film_path, self.film_path + os.sep)
        self.assertTrue(os.path.isfile(os.path.join(self.film_path, "Monsters, Inc. (2001).mkv")))


class TestManualWait(BonusFixture):
    def test_marking_a_bonus_disc_ends_the_wait(self):
        job = self._job()
        job.config.MANUAL_WAIT, job.config.MANUAL_WAIT_TIME = True, 60
        with mock.patch.object(utils.time, "sleep") as sleep, mock.patch.object(utils.db.session, "refresh"):
            utils.check_for_wait(job)
        self.assertEqual(sleep.call_count, 1)


if __name__ == "__main__":
    unittest.main()
