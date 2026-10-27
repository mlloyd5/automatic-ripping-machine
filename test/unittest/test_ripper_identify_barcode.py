"""
Tests for identify.identify_from_barcode with the network and database mocked.

Importing ``arm.ripper.identify`` pulls in the whole of ARM (config, Flask app, database), so a
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

_TMP = tempfile.mkdtemp(prefix="arm_identify_test_")
_CONFIG = os.path.join(_TMP, "arm.yaml")
with open(_CONFIG, "w") as _handle:
    _handle.write(f"INSTALLPATH: \"{REPO_ROOT}\"\n"
                  f"DBFILE: \"{_TMP}/arm.db\"\n"
                  f"LOGPATH: \"{_TMP}/logs/\"\n"
                  f"ABCDE_CONFIG_FILE: \"{REPO_ROOT}/setup/.abcde.conf\"\n"
                  f"RAW_PATH: \"{_TMP}/raw/\"\nTRANSCODE_PATH: \"{_TMP}/transcode/\"\n"
                  f"COMPLETED_PATH: \"{_TMP}/completed/\"\n"
                  f"DISC_PHOTO_PATH: \"{_TMP}/photos/\"\n"
                  "DISABLE_LOGIN: true\nMETADATA_PROVIDER: \"tmdb\"\nBARCODE_LOOKUP: true\n")
os.environ["ARM_CONFIG_FILE"] = _CONFIG

try:
    identify = importlib.import_module("arm.ripper.identify")
    cfg = importlib.import_module("arm.config.config")
    IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - depends on the host
    identify = cfg = None
    IMPORT_ERROR = error


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def _job(**overrides):
    base = dict(job_id=7, disctype="dvd", disc_barcode="085391163926", hasnicetitle=False,
                title="not identified", year="", arm_version="test", label="LOGICAL_VOLUME")
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_updater(args, job):
    if isinstance(args, dict):
        for key, value in args.items():
            setattr(job, key, value)
    return True


def _result(title, year, votes=0, **extra):
    base = {"Title": title, "Year": year, "Type": "movie", "imdbID": f"tt{abs(hash(title + year)) % 10**7:07d}",
            "Poster": f"https://img/{title}.jpg", "vote_count": votes}
    base.update(extra)
    return base


HOME_ALONE = [_result("Home Alone 3", "1997", 1500), _result("Home Sweet Home Alone", "2021", 700),
              _result("Home Alone 2: Lost in New York", "1992", 6000), _result("Home, Alone", "", 2),
              _result("Home Alone", "2023", 1), _result("Home Alone", "1990", 11000),
              _result("Home Alone", "1969", 3)]

# What TMDB really returns for the WALL-E case barcode's product title: the hyphen hides the film
# (job 29 on the ts140 was filed as East of Wall); with the year it turns up.
WALL_E_HYPHEN = [_result("East of Wall", "2025", 30), _result("End of the Wall", "2026", 0),
                 _result("Wall Engravings", "1968", 36), _result("Eyes Behind the Wall", "1977", 22)]
WALL_E_YEAR = [_result("WALL\u00b7E's Treasures & Trinkets", "2008", 7), _result("Wall Street", "1987", 2358),
               _result("WALL\u00b7E", "2008", 21096), _result("BURN\u00b7E", "2008", 837)]

# Job 37 on the ts140: barcode 013131210392 is "Thomas the Tank Engine and Friends - Best of James [DVD]",
# TMDB words the brand "Thomas & Friends", and the query "Best of James" is led by a wrestling compilation.
BEST_OF_JAMES = [_result("The Best of Mickie James", "2020", 1),
                 _result("Thomas & Friends: The Best of James", "2012", 0),
                 _result("Thomas & Friends: Best of James", "2002", 3),
                 _result("James Last: Best of the 70's", "1998", 2)]
THOMAS_PRODUCT = {"title": "Thomas the Tank Engine and Friends - Best of James", "year": None,
                  "raw": "Thomas the Tank Engine and Friends - Best of James [DVD]"}


def disc_photo_candidates(raw):
    """What lookup_upc gives for a product with this title: the title cleaned as upcitemdb's would be."""
    return identify.disc_photo.parse_upc_response({"code": "OK", "items": [
        {"title": raw, "category": "Media > Movies & TV > DVDs"}]})


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestBestSearchResult(unittest.TestCase):
    def test_exact_title_beats_the_providers_first_result(self):
        best = identify.best_search_result(HOME_ALONE, "Home Alone")
        self.assertEqual((best["Title"], best["Year"]), ("Home Alone", "1990"))

    def test_year_narrows_the_exact_matches(self):
        best = identify.best_search_result(HOME_ALONE, "Home Alone", "1969")
        self.assertEqual(best["Year"], "1969")
        # a year nothing matches falls back to the best exact match
        best = identify.best_search_result(HOME_ALONE, "Home Alone", "1975")
        self.assertEqual(best["Year"], "1990")

    def test_punctuation_and_case_do_not_matter(self):
        results = [_result("Coal Miner's Daughter: A Celebration of the Life and Music of Loretta Lynn", "2022", 9),
                   _result("Coal Miner's Daughter", "1980", 400)]
        best = identify.best_search_result(results, "COAL MINERS DAUGHTER")
        self.assertEqual(best["Year"], "1980")

    def test_no_exact_match_keeps_the_first_result(self):
        best = identify.best_search_result(HOME_ALONE, "Home")
        self.assertEqual(best["Title"], "Home Alone 3")
        self.assertEqual(identify.best_search_result(HOME_ALONE, None)["Title"], "Home Alone 3")
        self.assertIsNone(identify.best_search_result([], "Home Alone"))

    def test_exact_only_refuses_a_page_of_near_misses(self):
        self.assertIsNone(identify.best_search_result(WALL_E_HYPHEN, "WALL-E", exact_only=True))
        self.assertIsNone(identify.best_search_result(HOME_ALONE, None, exact_only=True))
        self.assertEqual(identify.best_search_result(WALL_E_YEAR, "WALL-E", "2008", exact_only=True)["Title"],
                         "WALL\u00b7E")
        self.assertEqual(identify.best_search_result(HOME_ALONE, "Home Alone", exact_only=True)["Year"], "1990")

    def test_update_job_uses_the_best_match(self):
        job = _job()
        with mock.patch.object(identify.utils, "database_updater", side_effect=_fake_updater):
            identify.update_job(job, {"Search": HOME_ALONE}, "Home Alone")
        self.assertEqual((job.title, job.year), ("Home-Alone", "1990"))  # the ripper's filename cleaner
        with mock.patch.object(identify.utils, "database_updater", side_effect=_fake_updater):
            identify.update_job(job, {"Search": HOME_ALONE})
        self.assertEqual((job.title, job.year), ("Home-Alone-3", "1997"))
        self.assertIsNone(identify.update_job(job, {"Search": []}, "Home Alone"))


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestBarcodeComesFirst(unittest.TestCase):
    """identify() asks the case barcode before the CRC database and the label search."""

    def setUp(self):
        os.environ["ARM_CONFIG_FILE"] = _CONFIG
        if cfg.arm_config_path != _CONFIG:
            importlib.reload(cfg)
        cfg.arm_config["GET_VIDEO_TITLE"] = True
        self.calls = []
        self.patches = [
            mock.patch.object(identify, "check_mount", return_value=True),
            mock.patch.object(identify, "unmount_disc"),
            mock.patch.object(identify, "get_video_details"),
            mock.patch.object(identify.utils, "find_file", return_value=False),
            mock.patch.object(identify.db.session, "commit"),
            mock.patch.object(identify, "identify_bluray",
                              side_effect=lambda job: self.calls.append("bluray") or True),
            mock.patch.object(identify, "identify_dvd", side_effect=self._dvd),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def _dvd(self, job, title_known=False):
        self.calls.append(("dvd", title_known))
        return True

    def _video_job(self, disctype):
        job = _job(disctype=disctype, devpath="/dev/sr1", mountpoint="/mnt/dev/sr1", video_type="movie")
        job.get_disc_type = lambda found: None
        job.pretty_table = lambda: ""
        return job

    def test_dvd_barcode_hit_skips_the_label_search(self):
        with mock.patch.object(identify, "identify_from_barcode",
                               side_effect=lambda job: self.calls.append("barcode") or True):
            identify.identify(self._video_job("dvd"))
        self.assertEqual(self.calls, ["barcode", ("dvd", True)])

    def test_dvd_barcode_miss_falls_back_to_the_disc(self):
        with mock.patch.object(identify, "identify_from_barcode",
                               side_effect=lambda job: self.calls.append("barcode") or False):
            identify.identify(self._video_job("dvd"))
        self.assertEqual(self.calls, ["barcode", ("dvd", False)])

    def test_bluray_is_only_read_when_the_barcode_missed(self):
        with mock.patch.object(identify, "identify_from_barcode", return_value=True):
            identify.identify(self._video_job("bluray"))
        self.assertEqual(self.calls, [])
        with mock.patch.object(identify, "identify_from_barcode", return_value=False):
            identify.identify(self._video_job("bluray"))
        self.assertEqual(self.calls, ["bluray"])

    def test_barcode_failure_never_stops_identification(self):
        with mock.patch.object(identify, "identify_from_barcode", side_effect=RuntimeError("upcitemdb down")):
            identify.identify(self._video_job("dvd"))
        self.assertEqual(self.calls, [("dvd", False)])


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestIdentifyFromBarcode(unittest.TestCase):
    def setUp(self):
        # another test module may have imported ARM with its own throwaway config: switch to ours
        os.environ["ARM_CONFIG_FILE"] = _CONFIG
        if cfg.arm_config_path != _CONFIG:
            importlib.reload(cfg)
        cfg.arm_config["BARCODE_LOOKUP"] = True
        self.updater = mock.patch.object(identify.utils, "database_updater", side_effect=_fake_updater)
        self.updater.start()

    def tearDown(self):
        self.updater.stop()

    def test_skips_when_disabled_or_not_needed(self):
        with mock.patch.object(identify.disc_photo, "lookup_upc") as lookup:
            cfg.arm_config["BARCODE_LOOKUP"] = False
            self.assertFalse(identify.identify_from_barcode(_job()))
            cfg.arm_config["BARCODE_LOOKUP"] = True
            self.assertFalse(identify.identify_from_barcode(_job(disc_barcode=None)))
            self.assertFalse(identify.identify_from_barcode(_job(hasnicetitle=True)))
            self.assertFalse(identify.identify_from_barcode(_job(disctype="data")))
        lookup.assert_not_called()

    def test_video_title_found_from_upc(self):
        candidates = [{"title": "Inception", "year": "2010", "raw": "Inception (Blu-ray + DVD, 2010)"}]
        inception = [_result("Inception: The Cobol Job", "2010", 300), _result("Inception", "2010", 36000)]
        asked = []

        def fake_search(title, year=None):
            asked.append((title, year))
            return {"Search": inception} if title == "Inception" else None

        job = _job()
        with mock.patch.object(identify.disc_photo, "lookup_upc", return_value=candidates) as lookup, \
                mock.patch.object(identify, "_provider_search", side_effect=fake_search):
            self.assertTrue(identify.identify_from_barcode(job))
        lookup.assert_called_once_with("085391163926", user_agent="arm/test")
        self.assertEqual(asked, [("Inception", "2010")])
        self.assertEqual((job.title, job.year, job.imdb_id, job.hasnicetitle),
                         ("Inception", "2010", inception[1]["imdbID"], True))
        self.assertEqual(job.disc_barcode_title, "Inception (Blu-ray + DVD, 2010)")

    def test_video_near_miss_is_not_taken_for_the_film(self):
        """TMDB has nothing titled WALL-E for "WALL-E": the barcode must not file the disc as East of Wall."""
        candidates = [{"title": "WALL-E", "year": None, "raw": "WALL-E (DVD) Disney Adventure  Animated"}]
        job = _job(disc_barcode="786936734911", label="WALL_E")
        with mock.patch.object(identify.disc_photo, "lookup_upc", return_value=candidates), \
                mock.patch.object(identify, "_provider_search", return_value={"Search": WALL_E_HYPHEN}) as search:
            self.assertFalse(identify.identify_from_barcode(job))
        search.assert_called_once_with("WALL-E", None)
        self.assertEqual((job.title, job.year, job.hasnicetitle), ("not identified", "", False))
        self.assertEqual(job.disc_barcode_title, "WALL-E (DVD) Disney Adventure  Animated")

    def test_video_brand_worded_differently_online_is_found_by_subtitle(self):
        """No title is exactly the product title: the subtitle's result that names the brand is taken."""
        asked = []

        def fake_search(title, year=None):
            asked.append((title, year))
            return {"Search": BEST_OF_JAMES} if title == "Best of James" else {"Search": []}

        job = _job(disc_barcode="013131210392", label="The_Best_of_James")
        with mock.patch.object(identify.disc_photo, "lookup_upc", return_value=[THOMAS_PRODUCT]), \
                mock.patch.object(identify, "_provider_search", side_effect=fake_search), \
                self.assertLogs(level="INFO") as logs:
            self.assertTrue(identify.identify_from_barcode(job))
        self.assertEqual(asked, [("Thomas the Tank Engine and Friends - Best of James", None), ("Best of James", None)])
        self.assertEqual((job.title, job.year, job.imdb_id, job.hasnicetitle),
                         ("Thomas-and-Friends--Best-of-James", "2002", BEST_OF_JAMES[2]["imdbID"], True))
        self.assertEqual(job.disc_barcode_title, THOMAS_PRODUCT["raw"])
        self.assertEqual([line for line in logs.output if "subtitle 'Best of James'" in line and "'Thomas'" in line
                          and "(2002)" in line], logs.output[-1:])

    def test_video_subtitle_result_without_the_brand_is_not_taken(self):
        """"Best of James" alone is not the disc: without the brand word the barcode must not guess."""
        no_brand = [r for r in BEST_OF_JAMES if "Thomas" not in r["Title"]] + [_result("The Best of James", "2012", 5)]
        job = _job(disc_barcode="013131210392", label="The_Best_of_James")
        with mock.patch.object(identify.disc_photo, "lookup_upc", return_value=[THOMAS_PRODUCT]), \
                mock.patch.object(identify, "_provider_search", return_value={"Search": no_brand}) as search:
            self.assertFalse(identify.identify_from_barcode(job))
        self.assertEqual([c.args[0] for c in search.call_args_list],
                         ["Thomas the Tank Engine and Friends - Best of James", "Best of James"])
        self.assertEqual((job.title, job.year, job.hasnicetitle), ("not identified", "", False))

    def test_video_product_title_without_a_separator_is_searched_once(self):
        candidates = [{"title": "Obscure Film", "year": None, "raw": "Obscure Film (DVD)"}]
        job = _job()
        with mock.patch.object(identify.disc_photo, "lookup_upc", return_value=candidates), \
                mock.patch.object(identify, "_provider_search", return_value={"Search": WALL_E_HYPHEN}) as search:
            self.assertFalse(identify.identify_from_barcode(job))
        search.assert_called_once_with("Obscure Film", None)
        self.assertEqual(job.title, "not identified")

    def test_search_film_by_subtitle(self):
        with mock.patch.object(identify, "_provider_search", return_value={"Search": BEST_OF_JAMES}):
            best = identify.search_film_by_subtitle("Thomas the Tank Engine and Friends - Best of James")
            self.assertEqual((best["Title"], best["Year"]), ("Thomas & Friends: Best of James", "2002"))
            # a known year narrows the qualifying results before the vote count decides
            best = identify.search_film_by_subtitle("Thomas & Friends: Best of James", "2012")
            self.assertEqual(best["Year"], "2012")
            self.assertIsNone(identify.search_film_by_subtitle("Best of James"))
            self.assertIsNone(identify.search_film_by_subtitle("The - Best of James"))
        self.assertEqual(identify.split_brand_subtitle("The Simpsons: Season 1"), ("Simpsons", "Season 1"))
        self.assertEqual(identify.split_brand_subtitle("Thomas & Friends - Best of James"), ("Thomas", "Best of James"))
        self.assertIsNone(identify.split_brand_subtitle("Blade Runner 2049"))
        self.assertIsNone(identify.split_brand_subtitle("Mission:Impossible"))  # no space after the colon
        self.assertIsNone(identify.split_brand_subtitle(None))

    def test_subtitle_match_needs_whole_words(self):
        """A two letter brand ("Ed") inside "Remastered", or the subtitle "Ice" ending "Justice", is no match."""
        remastered = [_result("The Remastered Christmas Special", "2010", 50)]
        with mock.patch.object(identify, "_provider_search", return_value={"Search": remastered}):
            self.assertIsNone(identify.search_film_by_subtitle("Ed, Edd n Eddy - Christmas Special"))
        with mock.patch.object(identify, "_provider_search",
                               return_value={"Search": remastered + [_result("Ed, Edd n Eddy: Christmas Special",
                                                                             "2005", 1)]}):
            self.assertEqual(identify.search_film_by_subtitle("Ed, Edd n Eddy - Christmas Special")["Year"], "2005")
        justice = [_result("Batman and the Fight for Justice", "2015", 40)]
        with mock.patch.object(identify, "_provider_search", return_value={"Search": justice}):
            self.assertIsNone(identify.search_film_by_subtitle("Batman - Ice"))
        with mock.patch.object(identify, "_provider_search",
                               return_value={"Search": justice + [_result("Batman: Ice", "2016", 2)]}):
            self.assertEqual(identify.search_film_by_subtitle("Batman - Ice")["Title"], "Batman: Ice")

    def test_accented_titles_online_match_the_folded_product_title(self):
        """clean_upc_title folds "Pokémon" to "Pokemon": both the exact and the subtitle search must too."""
        pokemon = [_result("The First Movie", "2009", 20), _result("Pok\u00e9mon: The First Movie", "1998", 900)]
        self.assertEqual(identify.best_search_result(pokemon, "Pokemon: The First Movie", exact_only=True)["Year"],
                         "1998")
        with mock.patch.object(identify, "_provider_search", return_value={"Search": pokemon}):
            self.assertEqual(identify.search_film_by_subtitle("Pokemon - The First Movie")["Year"], "1998")
        self.assertEqual(identify.split_brand_subtitle("Pok\u00e9mon - The First Movie"),
                         ("Pokemon", "The First Movie"))
        candidates = disc_photo_candidates("Pok\u00e9mon: The First Movie (DVD, 1999)")
        job = _job()
        with mock.patch.object(identify.disc_photo, "lookup_upc", return_value=candidates), \
                mock.patch.object(identify, "_provider_search", return_value={"Search": pokemon}) as search:
            self.assertTrue(identify.identify_from_barcode(job))
        search.assert_called_once_with("Pokemon: The First Movie", "1999")
        self.assertEqual((job.year, job.imdb_id), ("1998", pokemon[1]["imdbID"]))

    def test_video_en_dash_product_title_is_found_by_subtitle(self):
        """upcitemdb writes "Thomas the Tank Engine and Friends \u2013 Best of James": the dash still splits."""
        raw = "Thomas the Tank Engine and Friends \u2013 Best of James [DVD]"
        asked = []

        def fake_search(title, year=None):
            asked.append((title, year))
            return {"Search": BEST_OF_JAMES} if title == "Best of James" else {"Search": []}

        job = _job(disc_barcode="013131210392", label="The_Best_of_James")
        with mock.patch.object(identify.disc_photo, "lookup_upc", return_value=disc_photo_candidates(raw)), \
                mock.patch.object(identify, "_provider_search", side_effect=fake_search):
            self.assertTrue(identify.identify_from_barcode(job))
        self.assertEqual(asked, [("Thomas the Tank Engine and Friends - Best of James", None), ("Best of James", None)])
        self.assertEqual((job.year, job.imdb_id), ("2002", BEST_OF_JAMES[2]["imdbID"]))
        self.assertEqual(job.disc_barcode_title, raw)

    def test_video_subtitle_search_tries_the_year_before(self):
        """A 2003 disc of a 2002 film: the subtitle search finds it on its second query, with the year before."""
        asked = []

        def fake_search(title, year=None):
            asked.append((title, year))
            return {"Search": BEST_OF_JAMES} if (title, year) == ("Best of James", "2002") else {"Search": []}

        job = _job(disc_barcode="013131210392")
        with mock.patch.object(identify.disc_photo, "lookup_upc", return_value=[dict(THOMAS_PRODUCT, year="2003")]), \
                mock.patch.object(identify, "_provider_search", side_effect=fake_search):
            self.assertTrue(identify.identify_from_barcode(job))
        full = THOMAS_PRODUCT["title"]
        self.assertEqual(asked, [(full, "2003"), (full, "2002"), (full, None),
                                 ("Best of James", "2003"), ("Best of James", "2002")])
        self.assertEqual((job.year, job.imdb_id), ("2002", BEST_OF_JAMES[2]["imdbID"]))

    def test_video_subtitle_search_failure_moves_on_to_the_next_query(self):
        """A provider error on one subtitle query is that query's miss, not the end of the barcode path."""
        asked = []

        def fake_search(title, year=None):
            asked.append((title, year))
            if (title, year) == ("Best of James", "2002"):
                raise RuntimeError("tmdb timed out")
            return {"Search": BEST_OF_JAMES} if (title, year) == ("Best of James", None) else {"Search": []}

        job = _job(disc_barcode="013131210392")
        with mock.patch.object(identify.disc_photo, "lookup_upc", return_value=[dict(THOMAS_PRODUCT, year="2002")]), \
                mock.patch.object(identify, "_provider_search", side_effect=fake_search):
            self.assertTrue(identify.identify_from_barcode(job))
        self.assertEqual(asked[-3:], [("Best of James", "2002"), ("Best of James", "2001"), ("Best of James", None)])
        self.assertEqual((job.year, job.imdb_id), ("2002", BEST_OF_JAMES[2]["imdbID"]))

    def test_video_no_online_match_keeps_product_title(self):
        candidates = [{"title": "Obscure Film", "year": None, "raw": "Obscure Film (DVD)"}]
        job = _job()
        with mock.patch.object(identify.disc_photo, "lookup_upc", return_value=candidates), \
                mock.patch.object(identify, "_provider_search", return_value=None):
            self.assertFalse(identify.identify_from_barcode(job))
        self.assertEqual(job.disc_barcode_title, "Obscure Film (DVD)")
        self.assertEqual(job.title, "not identified")

    def test_video_lookup_failure_is_a_miss(self):
        candidates = [{"title": "Inception", "year": "2010", "raw": "Inception (DVD)"}]
        job = _job()
        with mock.patch.object(identify.disc_photo, "lookup_upc", return_value=candidates), \
                mock.patch.object(identify, "_provider_search", side_effect=RuntimeError("tmdb down")):
            self.assertFalse(identify.identify_from_barcode(job))
        self.assertEqual(job.title, "not identified")

    def test_video_lookup_empty(self):
        job = _job()
        with mock.patch.object(identify.disc_photo, "lookup_upc", return_value=[]), \
                mock.patch.object(identify, "_provider_search") as search:
            self.assertFalse(identify.identify_from_barcode(job))
        search.assert_not_called()

    def test_music_release_found_on_musicbrainz(self):
        release = {"id": "abc-123", "title": "Rumours", "artist-credit-phrase": "Fleetwood Mac", "date": "1977-02-04"}
        fake_mb = mock.Mock()
        fake_mb.search_releases.return_value = {"release-list": [release]}
        fake_mb.WebServiceError = Exception
        job = _job(disctype="music", disc_barcode="081227973308")
        with mock.patch.dict("sys.modules", {"musicbrainzngs": fake_mb}):
            self.assertTrue(identify.identify_from_barcode(job))
        fake_mb.search_releases.assert_called_once_with(barcode="081227973308", limit=5)
        self.assertEqual(job.title, "Fleetwood Mac Rumours")
        self.assertEqual(job.year, "1977")
        self.assertEqual(job.crc_id, "abc-123")
        self.assertTrue(job.hasnicetitle)

    def test_music_release_found_under_ean13_form(self):
        release = {"id": "def-456", "title": "Rumours", "artist-credit-phrase": "Fleetwood Mac", "date": "1977"}
        fake_mb = mock.Mock()
        fake_mb.search_releases.side_effect = lambda barcode, limit: {
            "release-list": [release] if barcode == "0081227973308" else []}
        fake_mb.WebServiceError = Exception
        job = _job(disctype="music", disc_barcode="081227973308")
        with mock.patch.dict("sys.modules", {"musicbrainzngs": fake_mb}):
            self.assertTrue(identify.identify_from_barcode(job))
        self.assertEqual([c.kwargs["barcode"] for c in fake_mb.search_releases.call_args_list],
                         ["081227973308", "0081227973308"])
        self.assertEqual(job.title, "Fleetwood Mac Rumours")
        self.assertEqual(job.crc_id, "def-456")

    def test_music_no_release(self):
        fake_mb = mock.Mock()
        fake_mb.search_releases.return_value = {"release-list": []}
        fake_mb.WebServiceError = Exception
        job = _job(disctype="music")
        with mock.patch.dict("sys.modules", {"musicbrainzngs": fake_mb}):
            self.assertFalse(identify.identify_from_barcode(job))
        self.assertEqual(fake_mb.search_releases.call_count, 2, "both UPC-A and EAN-13 spellings are tried")
        self.assertEqual(job.title, "not identified")


if __name__ == '__main__':
    unittest.main()


def _runtime(title):
    return {"Stuart Little": 84 * 60, "Stuart Little 2": 78 * 60}.get(title)


STUART_LITTLE = {
    "Stuart Little": [_result("Stuart Little 2", "2002", 2000, id=11), _result("Stuart Little", "1999", 3000, id=10)],
    "Stuart Little 2": [_result("Stuart Little 2", "2002", 2000, id=11)],
}


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestIdentifyCollection(unittest.TestCase):
    """A product title or label naming several films identifies each of them."""

    def setUp(self):
        os.environ["ARM_CONFIG_FILE"] = _CONFIG
        if cfg.arm_config_path != _CONFIG:
            importlib.reload(cfg)
        cfg.arm_config["BARCODE_LOOKUP"] = True
        cfg.arm_config["METADATA_PROVIDER"] = "tmdb"
        self.stored = []
        self.patches = [
            mock.patch.object(identify.utils, "database_updater", side_effect=_fake_updater),
            mock.patch.object(identify, "store_films", side_effect=lambda job, films: self.stored.append(films)),
            mock.patch.object(identify, "film_runtime", side_effect=lambda result: _runtime(result["Title"])),
            mock.patch.object(identify, "_provider_search",
                              side_effect=lambda title, year=None: {"Search": STUART_LITTLE[title]}
                              if title in STUART_LITTLE else None),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def test_each_film_is_found_and_the_first_names_the_job(self):
        job = _job()
        self.assertTrue(identify.identify_collection(job, ["Stuart Little", "Stuart Little 2"], "test"))
        self.assertEqual((job.title, job.year, job.hasnicetitle), ("Stuart-Little", "1999", True))
        self.assertEqual(job.imdb_id, STUART_LITTLE["Stuart Little"][1]["imdbID"])
        self.assertEqual([(film["Title"], film["runtime"]) for film in self.stored[0]],
                         [("Stuart Little", 84 * 60), ("Stuart Little 2", 78 * 60)])

    def test_one_film_found_is_not_a_collection(self):
        job = _job()
        self.assertFalse(identify.identify_collection(job, ["Stuart Little", "Obscure Film"], "test"))
        self.assertFalse(identify.identify_collection(job, ["Stuart Little 2", "Stuart Little 2"], "test"))
        self.assertEqual((job.title, job.hasnicetitle), ("not identified", False))
        self.assertEqual(self.stored, [])

    def test_search_film_tries_the_year_the_year_before_then_none(self):
        asked = []
        with mock.patch.object(identify, "_provider_search",
                               side_effect=lambda title, year=None: asked.append(year) or None):
            self.assertIsNone(identify.search_film("Stuart Little", "2004"))
        self.assertEqual(asked, ["2004", "2003", None])
        self.assertEqual(identify.search_film("Stuart Little")["Year"], "1999")

    def test_barcode_product_title_naming_two_films(self):
        candidates = [{"title": "Stuart Little Stuart Little 2", "year": None,
                       "raw": "Stuart Little / Stuart Little 2 (DVD)"}]
        job = _job(disc_barcode="043396430099")
        asked = []
        with mock.patch.object(identify.disc_photo, "lookup_upc", return_value=candidates), \
                mock.patch.object(identify, "_provider_search",
                                  side_effect=lambda title, year=None: asked.append(title)
                                  or {"Search": STUART_LITTLE.get(title, [])}):
            self.assertTrue(identify.identify_from_barcode(job))
        self.assertEqual(asked, ["Stuart Little", "Stuart Little 2"])
        self.assertEqual((job.title, job.year), ("Stuart-Little", "1999"))
        self.assertEqual(job.disc_barcode_title, "Stuart Little / Stuart Little 2 (DVD)")

    def test_barcode_collection_miss_searches_the_product_title_as_one(self):
        candidates = [{"title": "Obscure Film Obscure Film 2", "year": None,
                       "raw": "Obscure Film / Obscure Film 2 (DVD)"}]
        job = _job()
        asked = []
        with mock.patch.object(identify.disc_photo, "lookup_upc", return_value=candidates), \
                mock.patch.object(identify, "_provider_search",
                                  side_effect=lambda title, year=None: asked.append(title) or None):
            self.assertFalse(identify.identify_from_barcode(job))
        self.assertEqual(asked, ["Obscure", "Obscure 2", "Obscure Film Obscure Film 2"])
        self.assertEqual(self.stored, [])

    def test_label_naming_two_films(self):
        with mock.patch.object(identify, "identify_collection", return_value=True) as collection:
            self.assertTrue(identify.identify_collection_from_label(_job(label="STUART_LITTLE_1_AND_2")))
            self.assertFalse(identify.identify_collection_from_label(_job(label="LOGICAL_VOLUME")))
            self.assertFalse(identify.identify_collection_from_label(_job(label="STUART_LITTLE_1_AND_2",
                                                                          hasnicetitle=True)))
        collection.assert_called_once_with(mock.ANY, ["STUART LITTLE", "STUART LITTLE 2"],
                                           "label STUART_LITTLE_1_AND_2")


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestRuntimeParsing(unittest.TestCase):
    def test_omdb_runtime_strings(self):
        self.assertEqual(identify.ui_metadata.parse_runtime("84 min"), 84 * 60)
        self.assertEqual(identify.ui_metadata.parse_runtime("1h 24min"), 84 * 60)
        self.assertEqual(identify.ui_metadata.parse_runtime("2 h"), 120 * 60)
        self.assertIsNone(identify.ui_metadata.parse_runtime("N/A"))
        self.assertIsNone(identify.ui_metadata.parse_runtime(None))


@unittest.skipIf(IMPORT_ERROR is not None, f"ARM could not be imported here: {IMPORT_ERROR}")
class TestStoreFilms(unittest.TestCase):
    """store_films writes real rows: the film table from the migration, read back through Job.films."""

    def test_rows_replace_earlier_ones_in_disc_order(self):
        os.environ["ARM_CONFIG_FILE"] = _CONFIG
        if cfg.arm_config_path != _CONFIG:
            importlib.reload(cfg)
        import arm.ui.routes  # noqa: F401  creates the database like runui.py does

        job = identify.Job.__mapper__.class_manager.new_instance()
        job.devpath, job.title, job.status = "/dev/sr9", "not identified", "active"
        identify.db.session.add(job)
        identify.db.session.commit()
        identify.store_films(job, [_result("Stuart Little 2", "2002", 2000, runtime=78 * 60)])
        identify.store_films(job, [_result("Stuart Little", "1999", 3000, runtime=84 * 60),
                                   _result("Stuart Little 2", "2002", 2000, runtime=None)])
        films = list(identify.Job.query.get(job.job_id).films)
        self.assertEqual([(film.position, film.title, film.year, film.runtime, film.folder_name, film.track_number)
                          for film in films],
                         [(1, "Stuart-Little", "1999", 84 * 60, "Stuart-Little (1999)", None),
                          (2, "Stuart-Little-2", "2002", None, "Stuart-Little-2 (2002)", None)])
        self.assertEqual(films[0].imdb_id, _result("Stuart Little", "1999")["imdbID"])
        self.assertEqual(films[1].job.job_id, job.job_id)
