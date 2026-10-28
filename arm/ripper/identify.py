#!/usr/bin/env python3
"""Identification of dvd/bluray"""

import os
import logging
import urllib
import re
import datetime
import unicodedata
import json
import subprocess
import tempfile
from ast import literal_eval

import pydvdid
import requests
import xmltodict
import xml.parsers.expat
import arm.config.config as cfg
from arm.models import Job, Film

from arm.ripper import utils
# The title helpers live in utils, which job_dupe_check needs them in and which this module imports
from arm.ripper.utils import (_fold_accents, _title_words, _normalise_title,  # noqa: F401
                              GENERIC_DISC_LABELS, is_placeholder_title)
from arm.ripper import disc_photo
from arm.ripper import disc_vision
from arm.ripper.ProcessHandler import arm_subprocess
from arm.ui import db

# flake8: noqa: W605
from arm.ui import utils as ui_utils
from arm.ui import metadata as ui_metadata


def find_mount(devpath: str) -> str | None:
    """
    Find an existing, readable mountpoint of the disc in ``devpath`` by calling ``findmnt``

    Only mounts whose *source* is the device count. With a Proxmox LXC device passthrough the
    device node is itself the target of a tiny tmpfs bind mount, and asking ``findmnt`` about the
    path alone returns that mount, with ``/dev/sr0`` as the "mountpoint" of the disc.

    :return: Absolute path of mountpoint as ``str`` if any, else ``None``
    """
    try:
        result = subprocess.run(["findmnt", "--json", "--source", devpath],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        logging.debug(f"findmnt could not be run: {error}")
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None  # findmnt exits 1 when nothing is mounted from the device
    try:
        mountpoints = json.loads(result.stdout)
    except ValueError:
        logging.debug(f"findmnt returned no JSON: {result.stdout!r}")
        return None
    for mountpoint in mountpoints.get("filesystems") or []:
        target = mountpoint.get("target")
        if target and os.path.isdir(target) and os.access(target, os.R_OK):
            return target
    return None


def check_mount(job: Job) -> bool:
    """
    Check if there is a suitable mount for ``job``, if not, try to mount

    :return: ``True`` if mount exists now, ``False`` otherwise
    """
    if mountpoint := find_mount(job.devpath):
        logging.info(f"Found disc {job.devpath} mounted at {mountpoint}")
        job.mountpoint = mountpoint
    else:
        logging.info(f"Trying to mount disc at {job.devpath}...")
        arm_subprocess(["mount", "--source", job.devpath])
        if mountpoint := find_mount(job.devpath):
            logging.info(f"Successfully mounted disc to {mountpoint}")
            job.mountpoint = mountpoint
        else:
            logging.error("Disc was not and could not be mounted. Rip might fail.")
            return False
    return True


def unmount_disc(job):
    """
    Unmount the disc so the drive is free (to eject, and so the next disc is not read through
    this mount). By mountpoint, not device: where the device node is itself a bind-mount target
    (Proxmox LXC passthrough) ``umount /dev/sr1`` tries to unmount that tmpfs instead and fails
    with "must be superuser", leaving the mount in place for the next disc.
    """
    target = job.mountpoint if job.mountpoint and os.path.isdir(str(job.mountpoint)) else job.devpath
    arm_subprocess(["umount", target])


def identify(job):
    """
    Identify disc attributes

    Video discs are tried, cheapest and most precise first: the case barcode, a volume label that
    names a collection of films, the disc's own metadata (a DVD's CRC64 record, a Blu-ray's
    bdmt_eng.xml title), the vision model's reading of the disc photo (only when that metadata
    gave no title: it costs a model round trip), then the label search. A disc still not
    identified is named after the label the model read, else the barcode's product title.
    """
    logging.debug("Identify Entry point --- job ----")

    mounted = check_mount(job)

    # get_disc_type() checks local files, no need to run unless we can mount
    if mounted:
        # Check with the job class to get the correct disc type
        job.get_disc_type(utils.find_file("HVDVD_TS", job.mountpoint))

    if job.disctype in ["dvd", "bluray"]:

        logging.info("Disc identified as video")

        if cfg.arm_config["GET_VIDEO_TITLE"]:
            # The case barcode is the UPC of this exact release, so it goes first: a disc label
            # like "ALASKA" has hundreds of matches online, the barcode's product title has one.
            res = False
            try:
                res = identify_from_barcode(job)
            except Exception as error:
                logging.error(f"Barcode lookup failed: {error}", exc_info=True)
            if not res:
                res = identify_collection_from_label(job)
            # The disc's own metadata (a DVD's CRC64 record, a Blu-ray's bdmt_eng.xml) is free and
            # comes next; only when it has no title is the photo sent to the vision model
            # (identify_vision_step), ahead of the label search on "CARTOONS_D3".
            if job.disctype == "dvd":
                res = identify_dvd(job, title_known=res) or res
            if job.disctype == "bluray" and not res:
                res = identify_bluray(job)
                if not job.hasnicetitle and (not res or str(job.title) in _label_titles(job)):
                    # no bdmt_eng.xml title, only the label: get_video_details would search that
                    res = identify_vision_step(job) or res
            if res:
                get_video_details(job)
            else:
                job.hasnicetitle = False
                db.session.commit()
            if not job.hasnicetitle:
                title_from_disc_photo(job) or title_from_barcode_product(job)

            logging.info(f"Disc title Post ident -  title:{job.title} "
                         f"year:{job.year} video_type:{job.video_type} "
                         f"disctype: {job.disctype}")
            logging.debug(f"identify.job.end ---- \n\r{job.pretty_table()}")
    unmount_disc(job)


def identify_bluray(job):
    """ Get's Blu-Ray title by parsing XML in bdmt_eng.xml """

    try:
        with open(job.mountpoint + '/BDMV/META/DL/bdmt_eng.xml', "rb") as xml_file:
            xml_data = xml_file.read()

            try:
                doc = xmltodict.parse(xml_data)
            except xml.parsers.expat.ExpatError as error:
                lines = xml_data.decode("utf-8", errors="replace").splitlines()

                offending = ""
                if 1 <= error.lineno <= len(lines):
                    offending = lines[error.lineno - 1].strip()

                logging.warning(
                    "Malformed Blu-ray metadata XML at line %d column %d: %s\n"
                    "Offending line: %s\n"
                    "Attempting automatic repair.",
                    error.lineno,
                    error.offset,
                    error,
                    offending,
                )

                repaired = repair_bluray_xml(xml_data)
                try:
                    doc = xmltodict.parse(repaired)
                    logging.info("Successfully repaired malformed Blu-ray metadata XML.")
                except xml.parsers.expat.ExpatError as error:
                    logging.exception(
                        "Blu-ray metadata XML remains malformed at line %d column %d: %s. "
                        "Using disc label '%s'.",
                        error.lineno,
                        error.offset,
                        error,
                        job.label,
                    )
                    doc = None
    except OSError as error:
        logging.error("Disc is a bluray, but bdmt_eng.xml could not be found. "
                      "Disc cannot be identified.  Error "
                      f"number is: {error.errno}")
        # Maybe call OMdb with label when we can't find any ident on disc ?
        # Attempt to parse label
        if str(job.label) == "":
            job.title = str(job.label)
            job.year = ""
            db.session.commit()
            return False
        else:
            bluray_title = str(job.label)
            bluray_title = bluray_title.replace('_', ' ')
            bluray_title = bluray_title.title()
            job.title = job.title_auto = bluray_title
            job.year = ""
            db.session.commit()
            return True

    if doc is None:
        bluray_title = str(job.label)
    else:
        try:
            bluray_title = doc['disclib']['di:discinfo']['di:title']['di:name']
            if not bluray_title:
                bluray_title = str(job.label)
        except KeyError:
            bluray_title = str(job.label)
            logging.error(
                "Could not parse title from bdmt_eng.xml file. "
                "Using disc label '%s'.",
                job.label,
            )

    bluray_modified_timestamp = os.path.getmtime(job.mountpoint + '/BDMV/META/DL/bdmt_eng.xml')
    bluray_year = (datetime.datetime.fromtimestamp(bluray_modified_timestamp).strftime('%Y'))

    bluray_title = unicodedata.normalize('NFKD', str(bluray_title)).encode('ascii', 'ignore').decode()

    bluray_title = bluray_title.replace(' - Blu-rayTM', '')
    bluray_title = bluray_title.replace(' Blu-rayTM', '')
    bluray_title = bluray_title.replace(' - BLU-RAYTM', '')
    bluray_title = bluray_title.replace(' - BLU-RAY', '')
    bluray_title = bluray_title.replace(' - Blu-ray', '')

    bluray_title = utils.clean_for_filename(bluray_title)

    job.title = job.title_auto = bluray_title
    job.year = job.year_auto = bluray_year
    db.session.commit()

    return True


def identify_dvd(job, title_known=False):
    """ Manipulates the DVD title and calls OMDB to try and
    lookup the title

    :param title_known: the title was already found (from the case barcode, or the label naming
                        a collection): still compute the CRC and check for track 99, but do not
                        replace it from the CRC database or the label search

    When the CRC64 database does not know the disc either, the vision model reads the disc photo
    (:func:`identify_vision_step`) before the label search: a title it finds online is kept like
    one from the barcode.
    """

    logging.debug(f"\n\r{job.pretty_table()}")
    # Some older DVDs aren't actually labelled
    if not job.label or job.label == "":
        job.label = "not identified"
    try:
        crc64 = pydvdid.compute(str(job.mountpoint))
        dvd_title = f"{job.label}_{crc64}"
        logging.info(f"DVD CRC64 hash is: {crc64}")
        job.crc_id = str(crc64)
        urlstring = f"https://1337server.pythonanywhere.com/api/v1/?mode=s&crc64={crc64}"
        logging.debug(urlstring)
        dvd_info_xml = urllib.request.urlopen(urlstring).read()
        arm_api_json = json.loads(dvd_info_xml)
        logging.debug(f"dvd xml - {arm_api_json}")
        logging.debug(f"results = {arm_api_json['results']}")
        if arm_api_json['success'] and title_known:
            logging.info(f"CRC64 database knows this disc as {arm_api_json['results']['0']['title']}; "
                         f"keeping the barcode's {job.title}")
        elif arm_api_json['success']:
            logging.info("Found crc64 id from online API")
            logging.info(f"title is {arm_api_json['results']['0']['title']}")
            args = {
                'title': arm_api_json['results']['0']['title'],
                'title_auto': arm_api_json['results']['0']['title'],
                'year': arm_api_json['results']['0']['year'],
                'year_auto': arm_api_json['results']['0']['year'],
                'imdb_id': arm_api_json['results']['0']['imdb_id'],
                'imdb_id_auto': arm_api_json['results']['0']['imdb_id'],
                'video_type': arm_api_json['results']['0']['video_type'],
                'video_type_auto': arm_api_json['results']['0']['video_type'],
                'poster_url': arm_api_json['results']['0']['poster_img'],
                'poster_url_auto': arm_api_json['results']['0']['poster_img'],
                'hasnicetitle': True
            }
            utils.database_updater(args, job)
    except Exception as error:
        logging.error(f"Pydvdid failed with the error: {error}")
        dvd_title = str(job.label)

    logging.debug(f"dvd_title_label: {dvd_title}")
    # in this block we want to strip out any chars that might be bad
    # strip all non-numeric chars and use that for year
    year = re.sub(r"\D", "", str(job.year)) if job.year else None
    # next line is not really needed, but we don't want to leave an x somewhere
    dvd_title = job.label.replace("16x9", "")
    logging.debug(f"dvd_title ^a-z _-: {dvd_title}")
    # rip out any SKU's at the end of the line
    dvd_title = re.sub(r"SKU\b", "", dvd_title)
    logging.debug(f"dvd_title SKU$: {dvd_title}")

    if not title_known and not job.hasnicetitle:
        title_known = identify_vision_step(job)

    # Do we really need metaselector if we have got from ARM online db?
    if title_known:
        logging.info(f"Title already identified: {job.title} ({job.year}), skipping the label search")
    else:
        try:
            dvd_info_xml, refused = search_and_update(job, dvd_title, year)
            if refused and year:
                dvd_info_xml = None  # the exact title may be filed under another year, or none
            logging.debug(f"DVD_INFO_XML: {dvd_info_xml}")
            identify_loop(job, dvd_info_xml, dvd_title, year)
        except Exception:
            dvd_info_xml = None
            logging.debug("Cant connect to online service!")
    # Failsafe so that we always have a title.
    if job.title is None or job.title == "None":
        job.title = str(job.label)
        job.year = None

    # Track 99 detection
    tracks = lsdvd_tracks(job)
    logging.debug(f"Detected {len(tracks)} tracks")
    if len(tracks) == 99:
        job.has_track_99 = True
        if cfg.arm_config["PREVENT_99"]:
            raise utils.RipperException("Track 99 found and PREVENT_99 is enabled")

    return True


def lsdvd_tracks(job):
    """
    The DVD's titles as lsdvd lists them (dicts with ``ix`` and ``length`` in seconds, among others).
    lsdvd runs once per job: the series check (:func:`series_instead`) may need the titles before
    the track 99 detection at the end of identify_dvd does.

    :return: list, empty when lsdvd failed
    """
    cached = getattr(job, '_lsdvd_tracks', None)
    if cached is not None:
        return cached
    tracks = []
    # -Oy means output a python dict
    output = arm_subprocess(["lsdvd", "-Oy", job.devpath])
    if output:
        try:
            # literal_eval only accepts literals so we have to adjust the output slightly
            tracks = literal_eval(re.sub(r"^.*\{", "{", output)).get("track", [])
        except (SyntaxError, ValueError, AttributeError) as e:
            logging.error("Failed to parse lsdvd output", exc_info=e)
    job._lsdvd_tracks = tracks
    return tracks


def get_video_details(job):
    """ Clean up title and year.  Get video_type, imdb_id, poster_url from
    omdbapi.com webservice.\n

    job = Instance of Job class\n
    """
    title = job.title

    # Set out title from the job.label
    # return if not identified
    logging.debug(f"Title = {title}")
    if title == "not identified" or title is None or title == "":
        logging.info("Disc couldn't be identified")
        return
    title = re.sub('[_ ]', "+", title.strip())

    # strip all non-numeric chars and use that for year
    if job.year is None:
        year = ""
    else:
        year = re.sub(r"\D", "", str(job.year))

    logging.debug(f"Title: {title} | Year: {year}")
    logging.debug(f"Calling webservice with title: {title} and year: {year}")

    try:
        identify_loop(job, None, title, year)
    except Exception as error:
        logging.info(f"Identification failed with the error: {error}. Continuing...")


def vote_count(result) -> int:
    """A search result's TMDB vote count (0 for OMDB results, which have none)."""
    try:
        return int(result.get("vote_count") or 0)
    except (TypeError, ValueError):
        return 0


def best_search_result(results, title=None, year=None, exact_only=False):
    """
    Pick the result a title search most likely meant.

    The metadata providers rank by their own popularity, which puts "Home Alone 3" first for
    the query "Home Alone" and the 2022 documentary first for "Coal Miners Daughter". When the
    query is a real title (a case barcode's product title, a disc label), a result whose title
    is exactly the query beats everything else; among several exact matches (remakes, shorts
    with the same name) the one with the most votes wins, after the year when one was asked
    for. With no exact match the provider's first result stands, as before, unless
    ``exact_only`` is set: a provider can return a page of results none of which is the film
    (TMDB has nothing titled WALL·E for the query "WALL-E", only "East of Wall"), and a caller
    that knows the query is the real title would rather have no answer than that one.

    :param results: the ``Search`` list in OMDB style (``Title``, ``Year``), TMDB results keep
                    their ``vote_count``
    :param exact_only: return None instead of the provider's first result when no title matches
    :return: one entry of ``results``
    """
    if not results:
        return None
    wanted = _normalise_title(title)
    if not wanted:
        return None if exact_only else results[0]
    exact = [r for r in results if _normalise_title(r.get("Title") or r.get("title")) == wanted]
    if year:
        same_year = [r for r in exact if str(r.get("Year") or "")[:4] == str(year)[:4]]
        exact = same_year or exact
    if not exact:
        return None if exact_only else results[0]
    return max(exact, key=_rank_search_result)


def _rank_search_result(result):
    """Vote count, then whether the result has a year at all: the order among equally good titles."""
    return vote_count(result), bool(str(result.get("Year") or "").strip())


def update_job(job, search_results, title=None, year=None):
    """
    used to update a successfully found job
    :param job: job obj
    :param search_results: json returned from metadata provider
    :param title: the title that was searched for, to prefer an exact match (see best_search_result);
                  when the job carries a product title from its case barcode, or a title the vision
                  model read off the disc photo surely enough to name the disc, only an exact match
                  counts
    :param year: the year that was searched for
    :return: None if error or nothing acceptable
    """
    # logging.debug(f"s =======  {search_results}")
    if 'Search' not in search_results or not search_results['Search']:
        return None
    # Once the case barcode has named the product, a label search is only a guess at the real
    # title: a page of near misses ("Cartoons D3" finds only "Greatest Cartoons of the Golden
    # Era Vol. 3") must not outrank the product title (see title_from_barcode_product). Only a
    # product title that can name the disc counts: one that is all packaging ("DVD") leaves the
    # label search guessing as before, since nothing would take the guess's place. A title the
    # vision model read off the disc label counts the same way (see title_from_disc_photo): with
    # neither, the label search takes the provider's first result as it always has.
    product, _ = barcode_product_title(job)
    read = disc_label_read_title(job)
    best = best_search_result(search_results['Search'], title, year, exact_only=bool(product or read))
    if best is None:
        source = f"the disc label reads '{read}'" if read else f"the case barcode says the disc is '{product}'"
        logging.info(f"Search for '{title}' found nothing titled exactly that; {source}, "
                     f"so '{search_results['Search'][0]['Title']}' is not taken")
        return None
    if best is not search_results['Search'][0]:
        logging.info(f"Search for '{title}' returned '{search_results['Search'][0]['Title']}' first; "
                     f"taking the exact match '{best['Title']}' ({best['Year']})")
    return apply_search_result(job, best)


def apply_search_result(job, best, check_series=True):
    """
    Make one search result the job's title, year, type, IMDb id and poster.

    A film result gives way to a TV entry of the same title and year when the disc looks like that
    series' episodes (see :func:`series_instead`).

    :param best: a search result in OMDB style (``Title``, ``Year``, ``Type``, ``imdbID``, ``Poster``)
    :param check_series: False to take the film as it is (check_runtime picks a film by its runtime)
    :return: what ``utils.database_updater`` returns
    """
    series = series_instead(job, best) if check_series else None
    if series:
        logging.info(f"TMDB has '{best['Title']}' ({best['Year']}) as both a film and a series; the disc "
                     f"looks like episodes, so taking the series (TMDB tv {series.get('id')})")
        best = series
    new_year = best['Year']
    title = utils.clean_for_filename(best['Title'])
    logging.debug(f"Webservice successful.  New title is {title}.  New Year is: {new_year}")
    args = {
        'year_auto': str(new_year),
        'year': str(new_year),
        'title_auto': title,
        'title': title,
        'video_type_auto': best['Type'],
        'video_type': best['Type'],
        'imdb_id_auto': best['imdbID'],
        'imdb_id': best['imdbID'],
        'poster_url_auto': best['Poster'],
        'poster_url': best['Poster'],
        'hasnicetitle': True
    }
    return utils.database_updater(args, job)


def series_instead(job, best):
    """
    The TV entry to take instead of a film search result: TMDB can have the same title as a film
    and as a series, first dated the same day, and its film search finds only the film (tmdb_search
    asks for TV entries only when there is no film at all). The Amazing World of Dinosaurs (2012) is
    a 24 minute film on TMDB and a series of 6 episodes; its DVD (ts140 job 146) has three titles of
    22 minutes and was filed as the film.

    Only a TV entry titled exactly like the film, with the film's year when it has one, counts,
    and only when the disc looks like its episodes (:func:`looks_like_episodes`).

    :param best: a search result in OMDB style; only TMDB films are checked
    :return: the TV entry in OMDB style (``Type`` "series"), or None to keep the film
    """
    provider = str(cfg.arm_config.get('METADATA_PROVIDER', '')).lower()
    if provider != "tmdb" or best.get('Type') != "movie" or not best.get('id'):
        return None
    wanted, year = _normalise_title(best.get('Title')), str(best.get('Year') or "")[:4]

    def twin(result):
        return (_normalise_title(result.get('name')) == wanted
                and (not year or str(result.get('first_air_date') or "")[:4] == year))
    try:
        twins = ui_metadata.tmdb_tv_search(best['Title'], twin)
        if not twins:
            return None
        series = max(twins, key=_rank_search_result)
        details = ui_metadata.tmdb_tv_details(series['id']) or {}
        film = film_runtime(best)
        lengths = dvd_title_lengths(job)
    except Exception as error:
        logging.debug(f"Looking for a series titled '{best.get('Title')}' failed: {error}")
        return None
    episodes = looks_like_episodes(lengths, film, details.get('episode_runtimes'), details.get('episodes'))
    logging.info(f"TMDB has '{series['Title']}' ({series['Year']}) as a series too "
                 f"({details.get('episodes') or 'unknown'} episodes); the film runs "
                 f"{film // 60 if film else 'unknown'} min, the disc's titles "
                 f"{', '.join(str(length // 60) for length in sorted(lengths, reverse=True)[:8]) or 'unknown'} min: "
                 f"{'episodes' if episodes else 'keeping the film'}")
    return series if episodes else None


def dvd_title_lengths(job):
    """
    The lengths in seconds of a DVD's titles, from lsdvd: identification runs before MakeMKV lists
    the titles, so a Blu-ray's are not known yet (an empty list).
    """
    if getattr(job, 'disctype', None) != "dvd" or not getattr(job, 'devpath', None):
        return []
    lengths = []
    for track in lsdvd_tracks(job):
        try:
            lengths.append(int(float(track.get('length') or 0)))
        except (TypeError, ValueError, AttributeError):
            continue
    return [length for length in lengths if length > 0]


# A title shorter than this is a menu, a trailer or an extra, not an episode
EPISODE_MIN_LENGTH = 5 * 60
# Titles within this fraction of the shortest of them run as long as each other: one series' episodes
EPISODE_LENGTH_SPREAD = 0.1
# TMDB's episode runtime is the slot more than the episode (30 minutes for 22), so it fits loosely
EPISODE_RUNTIME_TOLERANCE = 0.3
# A film runs as long as a title within this fraction of its runtime (a PAL disc plays 4% fast)
FILM_RUNTIME_TOLERANCE = 0.1
# Never closer than this many seconds, as in arm_ripper's runtime check
RUNTIME_TOLERANCE_MIN = 180
# The longest a short film runs (the Academy's 40 minutes): a film entry no longer may be one episode
SHORT_FILM_MAX = 40 * 60


def _runs_about(runtime, length, fraction):
    """Whether a title of ``length`` seconds runs as long as ``runtime`` seconds, within ``fraction`` of it"""
    return abs(length - runtime) <= max(RUNTIME_TOLERANCE_MIN, runtime * fraction)


def episode_titles(lengths):
    """
    The largest set of at least two titles that run as long as each other (within
    EPISODE_LENGTH_SPREAD), shortest first, leaving out titles too short to be episodes.

    :return: list of lengths, empty when no two titles are alike
    """
    titles = sorted(length for length in lengths if length >= EPISODE_MIN_LENGTH)
    group = []
    for start, shortest in enumerate(titles):
        alike = [length for length in titles[start:] if length <= shortest * (1 + EPISODE_LENGTH_SPREAD)]
        if len(alike) > len(group):
            group = alike
    return group if len(group) >= 2 else []


def looks_like_episodes(lengths, film_runtime_seconds, episode_runtimes, episodes):
    """
    Whether a disc is a series' episodes rather than the film of the same title.

    With the disc's title lengths: a film longer than a short with a title that runs as long as it
    is on the disc, extras and second cuts beside it notwithstanding. Otherwise several titles of
    the same length are episodes when that length is the series' episode runtime, or when the film
    runs no more than half as long as they do together. Without them (a Blu-ray before MakeMKV) only
    TMDB can say: a film entry no longer than a short, beside a series of several episodes, is
    taken for one of the episodes.

    :param lengths: the disc's title lengths in seconds, possibly empty
    :param film_runtime_seconds: the film's runtime, or None when TMDB has none
    :param episode_runtimes: the series' episode runtimes in seconds, possibly empty
    :param episodes: the number of episodes the series has, or None
    """
    titles = [length for length in lengths or [] if length >= EPISODE_MIN_LENGTH]
    if not titles:
        return bool(film_runtime_seconds and film_runtime_seconds <= SHORT_FILM_MAX and (episodes or 0) >= 2)
    if film_runtime_seconds and film_runtime_seconds > SHORT_FILM_MAX and any(
            _runs_about(film_runtime_seconds, length, FILM_RUNTIME_TOLERANCE) for length in titles):
        return False
    group = episode_titles(titles)
    if not group:
        return False
    episode_length = group[len(group) // 2]
    if any(_runs_about(runtime, episode_length, EPISODE_RUNTIME_TOLERANCE) for runtime in episode_runtimes or []):
        return True
    return bool(film_runtime_seconds and film_runtime_seconds * 2 <= sum(group))


def metadata_selector(job, title=None, year=None):
    """
    Used to switch between OMDB or TMDB as the metadata provider\n
    - TMDB returned queries are converted into the OMDB format

    :param job: The job class
    :param title: this can either be a search string or movie/show title
    :param year: the year of movie/show release

    :return: json/dict object or None
    """
    return search_and_update(job, title, year)[0]


def search_and_update(job, title=None, year=None, cache=None):
    """
    :func:`metadata_selector`, also saying whether update_job refused every result the provider
    returned (only an exact match counts once a case barcode has named the product, see
    update_job). A refused response is an answer, but not one the job took.

    :param cache: a dict shared by the searches of one :func:`search_with_year_retries` chain, so
                  the yearless search below and the chain's own yearless attempt ask the provider once
    :return: ``(search results or None, refused)``
    """
    # A disc whose label could not be read is given the placeholder
    # "not identified" in identify_dvd(). Searching for that string finds real
    # films with unhelpful names -- TMDB matches a 2018 Brazilian film titled
    # "Prefiro nao ser identificada" -- and update_job() then overwrites
    # job.title with it. The guard in get_video_details() cannot catch this,
    # because by the time it runs job.title is the bogus match rather than the
    # placeholder. Refuse the search here instead, where every caller passes. The same goes for
    # labels an authoring tool left at its default, like DVD_VIDEO (see is_placeholder_title).
    if is_placeholder_title(title):
        logging.info(f"Not searching for placeholder title: {title!r}")
        return None, False
    provider = cfg.arm_config['METADATA_PROVIDER'].lower()
    if provider not in ("tmdb", "omdb"):
        logging.debug(cfg.arm_config['METADATA_PROVIDER'])
        logging.debug("unknown provider - doing nothing, saying nothing. Getting Kryten")
        return None, False
    logging.debug(f"provider {provider}")

    def search(search_year):
        if cache is None:
            return provider_search(provider, title, search_year)
        key = (str(title), str(search_year) if search_year else None)
        if key not in cache:
            cache[key] = provider_search(provider, title, search_year)
        return cache[key]

    search_results = search(year)
    # The year can be wrong. A Blu-ray's comes from the date of its bdmt_eng.xml, which is when
    # that release was authored: 2016 for a pressing of RED 2 (2013), and TMDB filtered to 2016
    # has no RED 2, so its first result "High & Low The Red Rain" was taken. When the year
    # search returns films but none whose title even starts with the query, and the same search
    # without the year finds the exact title, drop the year. A label is often the start of the
    # title: "Stagecoach" in 2017 finds "Stagecoach: The Texas Jack Story", the film on the disc,
    # where the yearless search would take the 1939 "Stagecoach". An empty year search is left
    # to identify_loop, which tries the year before next.
    if year and has_results(search_results) and not has_title_starting_with(search_results, title):
        without_year = search(None)
        if has_exact_match(without_year, title):
            logging.info(f"Search for '{title}' in {year} has no exact title match; "
                         "searching without the year found one")
            search_results, year = without_year, None
    applied = None
    if search_results is not None:
        applied = update_job(job, search_results, title, year)
    return search_results, applied is None and has_results(search_results)


def provider_search(provider, title, year):
    """
    Search the metadata provider ("tmdb" or "omdb"); the results are in OMDB format, or None.

    An unreachable TMDB raises MetadataUnavailable rather than looking like "no results", so
    identify_loop's fallback searches stop at the first one instead of each waiting out the timeout.
    """
    if provider == "tmdb":
        return ui_utils.tmdb_search(title, year, raise_unreachable=True)
    return ui_utils.call_omdb_api(str(title), str(year))


def has_results(search_results) -> bool:
    """Whether a search returned anything at all."""
    return bool(search_results and search_results.get('Search'))


def has_title_starting_with(search_results, title) -> bool:
    """Whether a search returned a result whose title starts with ``title`` (compared as in best_search_result)."""
    wanted = _normalise_title(title)
    return has_results(search_results) and any(
        _normalise_title(result.get("Title") or result.get("title")).startswith(wanted)
        for result in search_results['Search'])


def has_exact_match(search_results, title) -> bool:
    """Whether a search returned a result titled exactly ``title`` (see best_search_result)."""
    if not search_results or not search_results.get('Search'):
        return False
    return best_search_result(search_results['Search'], title, exact_only=True) is not None


def identify_loop(job, response, title, year):
    """

    :param job:
    :param response:
    :param title:
    :param year:
    """
    # handle failures
    # this is a little kludgy, but it kind of works...
    logging.debug(f"Response = {response}")
    if response is None:
        # with the year, the year before, then without a year
        response = search_with_year_retries(job, title, year)

        if response is None:
            while response is None and title.find("-") > 0:
                title = title.rsplit('-', 1)[0]
                logging.debug(f"Trying title: {title}")
                response = metadata_selector(job, title, year)
                logging.debug(f"response: {response}")

            # if still fail, then try slicing off the last word in a loop
            while response is None and title.count('+') > 0:
                title = title.rsplit('+', 1)[0]
                logging.debug(f"Trying title: {title}")
                response = metadata_selector(job, title, year)
                logging.debug(f"response: {response}")
                if response is None:
                    # Still failing - try the words we have without year
                    logging.debug("Removing year...")
                    response = metadata_selector(job, title)


def search_with_year_retries(job, title, year=None):
    """
    Search a title with the year, the year before it (a disc often comes out the year after the
    film), then without a year, stopping at the first attempt the provider answers. An answer
    whose every result update_job refused (exact matches only once a case barcode has named the
    product) does not stop the retries: the exact title may only turn up under another year, or
    none (a Blu-ray's metadata file gives its own date, not the film's). Two exceptions keep
    metadata_selector's year rule: a refused year search with a title that starts with the query
    ends the chain (the year is right and the label is the start of a longer title, so a yearless
    exact match would be another film: "Stagecoach" 1939 for the 2016 Texas Jack Story), and no
    search is sent twice (metadata_selector's own yearless search is the chain's last attempt).

    :return: the response that was taken, else the last refused one, else None (no attempt got
             an answer, so callers may shorten the title and try again)
    """
    attempts = [str(year), str(int(year) - 1)] if year and str(year).isdigit() else []
    attempts.append(None)
    cache = {}
    refused_response = None
    for attempt in attempts:
        logging.debug(f"Trying title: {title} year: {attempt}")
        response, refused = search_and_update(job, title, attempt, cache)
        logging.debug(f"response: {response}")
        if response is None:
            continue
        if not refused:
            return response
        refused_response = response
        if attempt and has_title_starting_with(response, title):
            logging.info(f"Search for '{title}' in {attempt} found titles that start with it but none exactly "
                         "it; not searching other years")
            break
    return refused_response


def repair_bluray_xml(xml_data):
    # some Sunrise discs have malformed XML where href=foo instead of href="foo"
    xml_data = re.sub(
        rb'(?<!")href=([^"\s>][^\s>]*)',
        rb'href="\1"',
        xml_data,
    )
    return xml_data


def _preprocess_for_ocr(photo_path):
    """
    Produce a cleaner image for tesseract: grayscale, upscale small photos and threshold.

    Uses OpenCV when it is installed (the camera daemon needs it anyway) and falls back to the
    original photo otherwise.

    :return: path to the image tesseract should read, and whether it is a temp file to delete
    """
    try:
        import cv2  # noqa: F401  pylint: disable=import-outside-toplevel
    except ImportError:
        return photo_path, False
    try:
        image = cv2.imread(photo_path, cv2.IMREAD_GRAYSCALE)
        if image is None:
            return photo_path, False
        height, width = image.shape[:2]
        if max(height, width) < 1600:
            scale = 1600 / max(height, width)
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        image = cv2.GaussianBlur(image, (3, 3), 0)
        _, image = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        handle, temp_path = tempfile.mkstemp(prefix="arm_ocr_", suffix=".png")
        os.close(handle)
        cv2.imwrite(temp_path, image)
        return temp_path, True
    except Exception as error:  # cv2 raises its own error type
        logging.debug(f"OCR preprocessing failed, using raw photo: {error}")
        return photo_path, False


def ocr_disc_photo(photo_path, timeout=90):
    """
    Run tesseract over a disc photo and return the raw text, or None when OCR is unavailable.

    tesseract's progress chatter goes to stderr, which is discarded so it never ends up in
    the text we search with.
    """
    source, is_temp = _preprocess_for_ocr(photo_path)
    cmd = ["tesseract", source, "stdout", "--psm", "11"]
    logging.debug(f"Running OCR: {cmd}")
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except FileNotFoundError:
        logging.warning("tesseract is not installed, disc photo OCR is unavailable")
        return None
    except subprocess.TimeoutExpired:
        logging.warning(f"tesseract timed out after {timeout}s on {photo_path}")
        return None
    finally:
        if is_temp:
            try:
                os.remove(source)
            except OSError:
                pass
    if result.returncode != 0:
        logging.warning(f"tesseract exited with {result.returncode} on {photo_path}")
        return None
    return result.stdout


def identify_from_photo(job):
    """
    Fallback identification: read the disc label from the webcam photo of the disc.

    Only runs for video discs that the normal identification could not match
    (``hasnicetitle`` is False), when DISC_PHOTO_OCR is enabled and the job has a photo.
    The raw OCR text is kept on the job so the UI can show it on the title fix page even
    when no online match is found. Once the case barcode has named the product the OCR
    candidates are held to the same rule as the label search (see update_job): only a result
    titled exactly like a candidate is taken, so a misread photo cannot replace the product
    title the disc was named after. When the vision model already read the label well enough to
    name the disc (:func:`title_from_disc_photo`) OCR does not run at all: tesseract reads far
    less off the same photo, and its first fuzzy match would replace the better name.

    :return: True when a title was found from the photo
    """
    if not cfg.arm_config.get("DISC_PHOTO_OCR", False):
        return False
    if job.disctype not in ["dvd", "bluray"] or not job.disc_photo or job.hasnicetitle:
        return False
    vision_title = disc_label_read_title(job)
    if vision_title:
        logging.info(f"The vision model read the disc label as '{vision_title}'; not running OCR on the photo")
        return False
    photo_path = os.path.join(cfg.arm_config.get("DISC_PHOTO_PATH", ""), job.disc_photo)
    if not os.path.isfile(photo_path):
        logging.warning(f"Disc photo {photo_path} is missing, skipping OCR")
        return False

    logging.info(f"Disc not identified, trying OCR on disc photo {job.disc_photo}")
    text = ocr_disc_photo(photo_path)
    if not text:
        return False
    utils.database_updater({'disc_photo_ocr': text}, job)
    candidates, year = disc_photo.clean_ocr_text(text)
    logging.info(f"OCR title candidates: {candidates} year hint: {year}")

    for candidate in candidates:
        try:
            response = search_with_year_retries(job, candidate, year)
        except ui_metadata.MetadataUnavailable as error:
            logging.info(f"Metadata provider unreachable, not trying the other OCR candidates: {error}")
            return False
        except Exception as error:
            logging.debug(f"Metadata lookup for OCR candidate '{candidate}' failed: {error}")
            response = None
        # A response is not a match: update_job may have refused every result (a barcode named
        # the product and none is exactly the candidate), so only the job itself can say.
        if response and job.hasnicetitle:
            logging.info(f"Identified disc from photo as '{job.title}' ({job.year}) via '{candidate}'")
            return True
    logging.info("OCR text did not match any title online")
    return False


def disc_label_read_title(job):
    """
    The title the vision model read off the disc photo, when it was sure enough of it to name the
    disc (:data:`disc_vision.NAME_CONFIDENCE`, see :func:`title_from_disc_photo`), else None.
    """
    title, _ = disc_vision.label_title(disc_vision.parse_answer(getattr(job, 'disc_photo_vision', None)))
    return title


def identify_vision_step(job):
    """
    :func:`identify_from_photo_vision`, which must never stop identification.

    :return: True when a title was found from the photo
    """
    try:
        return identify_from_photo_vision(job)
    except Exception as error:
        logging.error(f"Disc photo vision lookup failed: {error}", exc_info=True)
        return False


def identify_from_photo_vision(job):
    """
    Identify the disc from what a vision model reads on its label in the webcam photo.

    Runs for video discs not yet identified (``hasnicetitle`` is False) that have a photo, when
    DISC_PHOTO_VISION_URL is set: after the case barcode, and after the disc's own metadata (a
    DVD's CRC64 record, a Blu-ray's bdmt_eng.xml title) has found nothing, since those cost no
    model round trip. It comes before the label search, since the printed label names the release
    better than its volume label does.
    What the model read is kept on the job (JSON) so the title fix pages can offer it and
    :func:`title_from_disc_photo` can name the disc when nothing online matches. The title (and
    the title with its subtitle) is searched online with the year read, exact matches only.

    :return: True when a title was found from the photo
    """
    url = str(cfg.arm_config.get("DISC_PHOTO_VISION_URL", "") or "").strip()
    if not url:
        return False
    if job.disctype not in ["dvd", "bluray"] or not job.disc_photo or job.hasnicetitle:
        return False
    photo_path = os.path.join(cfg.arm_config.get("DISC_PHOTO_PATH", ""), job.disc_photo)
    if not os.path.isfile(photo_path):
        logging.warning(f"Disc photo {photo_path} is missing, skipping the vision model")
        return False

    model = str(cfg.arm_config.get("DISC_PHOTO_VISION_MODEL", "") or disc_vision.DEFAULT_MODEL)
    try:
        timeout = int(cfg.arm_config.get("DISC_PHOTO_VISION_TIMEOUT") or disc_vision.DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = disc_vision.DEFAULT_TIMEOUT
    logging.info(f"Disc not identified, asking vision model {model} at {url} about disc photo {job.disc_photo}")
    answer = disc_vision.read_disc_photo(photo_path, url, model, timeout)
    if not answer:
        logging.info("Vision model could not read a title off the disc photo")
        return False
    logging.info(f"Vision model read title {answer['title']!r} subtitle {answer['subtitle']!r} "
                 f"year {answer['year']!r} studio {answer['studio']!r} "
                 f"(confidence {answer['confidence']:.2f}, rotated {answer.get('rotation')} degrees)")
    utils.database_updater({'disc_photo_vision': json.dumps(answer)}, job)

    for title in disc_vision.search_titles(answer):
        best = search_film(title, answer['year'])
        if best:
            apply_search_result(job, best)
            logging.info(f"Identified disc from photo as '{job.title}' ({job.year}) via '{title}'")
            return True
    logging.info("No title online is exactly what the vision model read; "
                 "leaving the disc to its CRC64 record and label")
    return False


def title_from_disc_photo(job):
    """
    Name a disc nothing online matched after what the vision model read on its label.

    Runs last in :func:`identify` for video discs still without a nice title, before
    :func:`title_from_barcode_product`: the disc names itself more precisely than its case does
    (job 34's case said "59 Cartoon Classics", the disc "Toon Time: Ultimate Cartoon Collection").
    A title the disc itself supplied (a Blu-ray's bdmt_eng.xml) is kept, and a read the model was
    not reasonably sure of is ignored. As with the product title the year goes with the title:
    the one read, or none, never the date of a Blu-ray's metadata file. ``hasnicetitle`` stays
    False: this is a name, not an identification.

    :return: True when the title was set from the photo
    """
    if job.hasnicetitle or not getattr(job, 'disc_photo_vision', None):
        return False
    if job.title not in (None, "", "not identified") and str(job.title) not in _label_titles(job):
        return False
    title, year = disc_vision.label_title(disc_vision.parse_answer(job.disc_photo_vision))
    if not title:
        return False
    title = utils.clean_for_filename(title)
    year = year or ""
    args = {'title': title, 'title_auto': title, 'year': year, 'year_auto': year}
    logging.info(f"Nothing online matched the disc; naming it after its label as read from the photo: "
                 f"'{title}' ({year or 'no year'})")
    utils.database_updater(args, job)
    return True


def _provider_search(title, year=None):
    """
    Ask the configured metadata provider about a title without touching the job.

    :return: the provider's response in OMDB style (``Search`` list), or None
    """
    if is_placeholder_title(title):
        return None
    provider = str(cfg.arm_config.get('METADATA_PROVIDER', '')).lower()
    if provider == "tmdb":
        return ui_utils.tmdb_search(str(title), str(year) if year else None)
    if provider == "omdb":
        return ui_utils.call_omdb_api(str(title), str(year) if year else None)
    logging.debug(f"unknown metadata provider {provider!r}, not searching for {title!r}")
    return None


def search_film(title, year=None):
    """
    The provider's result whose title is this film title (see best_search_result): with the
    year, with the year before it (a disc often comes out the year after the film), then without
    a year. ``title`` is taken to be the film's real title (a case barcode's product title, one
    film of a double feature), so a page of results that merely resemble it is no match.

    :return: one search result in OMDB style (``Title``, ``Year``, ``imdbID``, ...) or None
    """
    attempts = [str(year), str(int(year) - 1)] if year and str(year).isdigit() else []
    attempts.append(None)
    for attempt in attempts:
        try:
            response = _provider_search(title, attempt)
        except Exception as error:
            logging.debug(f"Metadata lookup for '{title}' ({attempt}) failed: {error}")
            response = None
        if response and response.get('Search'):
            best = best_search_result(response['Search'], title, attempt, exact_only=True)
            if best:
                return best
    return None


_SUBTITLE_SEPARATOR_RE = re.compile(r"\s+-\s+|:\s+")


def split_brand_subtitle(title):
    """
    A product title's brand prefix and subtitle, when it has both.

    ``"Thomas the Tank Engine and Friends - Best of James"`` is ``("Thomas", "Best of James")``: the
    first word of the prefix that is not an article, and everything after the first " - " or ": ".

    :return: ``(brand word, subtitle)`` or None when the title has no such separator
    """
    parts = _SUBTITLE_SEPARATOR_RE.split(str(title or ""), maxsplit=1)
    if len(parts) != 2:
        return None
    prefix, subtitle = parts[0].strip(), parts[1].strip()
    words = [word for word in re.findall(r"[A-Za-z0-9]+", _fold_accents(prefix))
             if word.lower() not in ("a", "an", "the")]
    if not words or not _title_words(subtitle):
        return None
    return words[0], subtitle


def search_film_by_subtitle(title, year=None):
    """
    The provider's result for a product title whose brand prefix is worded differently online.

    A case barcode gives "Thomas the Tank Engine and Friends - Best of James" where TMDB has
    "Thomas & Friends: Best of James", so no title is exactly the product title (see search_film).
    When the product title splits into a brand prefix and a subtitle, the subtitle alone is
    searched and a result counts when its title's last words are the subtitle's words and one of
    its words is the prefix's first word: "The Best of Mickie James" and "James Last: Best of the
    70's" do not qualify, and neither does a title where the brand ("Ed") or the subtitle ("Ice")
    only turns up inside a longer word ("Remastered", "Justice"). Several such results rank as
    best_search_result ranks exact matches: the year when one is known, then the vote count.

    :return: one search result in OMDB style, or None
    """
    split = split_brand_subtitle(title)
    if not split:
        return None
    brand, subtitle = split
    wanted_brand, wanted_subtitle = brand.lower(), _title_words(subtitle)

    def qualifies(result):
        found = _title_words(result.get("Title") or result.get("title"))
        return found[-len(wanted_subtitle):] == wanted_subtitle and wanted_brand in found

    attempts = [str(year), str(int(year) - 1)] if year and str(year).isdigit() else []
    attempts.append(None)
    for attempt in attempts:
        try:
            response = _provider_search(subtitle, attempt)
        except Exception as error:
            logging.debug(f"Metadata lookup for '{subtitle}' ({attempt}) failed: {error}")
            response = None
        matches = [r for r in (response or {}).get('Search') or [] if qualifies(r)]
        if attempt:
            same_year = [r for r in matches if str(r.get("Year") or "")[:4] == attempt[:4]]
            matches = same_year or matches
        if matches:
            return max(matches, key=_rank_search_result)
    return None


def film_runtime(result):
    """
    The runtime of a search result, in seconds, from the provider that returned it (a search
    result carries no runtime; TMDB needs the film's details, OMDB a lookup by IMDb id).

    :return: seconds or None
    """
    provider = str(cfg.arm_config.get('METADATA_PROVIDER', '')).lower()
    try:
        if provider == "tmdb" and result.get('id'):
            return ui_metadata.tmdb_runtime(result['id'])
        if provider == "omdb" and result.get('imdbID'):
            return ui_metadata.omdb_runtime(result['imdbID'])
    except Exception as error:
        logging.debug(f"Runtime lookup for '{result.get('Title')}' failed: {error}")
    return None


class RuntimeLookupError(Exception):
    """The provider could not be asked for a film's runtime: network error, rate limit, bad API key"""


def _lookup_json(url):
    """GET a provider URL; any failure to get an answer is a RuntimeLookupError"""
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as error:
        raise RuntimeLookupError(str(error)) from error


def identified_runtime(job):
    """
    The runtime, in seconds, of the film the job is identified as (looked up by its IMDb id).

    "No runtime" and "could not ask" are kept apart: None means the provider answered and has no
    runtime for a film with that id (or it is a series), while a failed lookup raises, so a
    transient error is not taken for an unknown film (check_runtime then leaves the job alone).

    :return: seconds or None
    :raises RuntimeLookupError: when the provider could not be asked
    """
    if not job.imdb_id:
        return None
    provider = str(cfg.arm_config.get('METADATA_PROVIDER', '')).lower()
    if provider == "omdb":
        found = _lookup_json(f"https://www.omdbapi.com/?i={urllib.parse.quote(str(job.imdb_id))}"
                             f"&r=json&apikey={cfg.arm_config['OMDB_API_KEY']}")
        if found.get('Response') == "False":
            if "incorrect imdb id" in str(found.get('Error', '')).lower():
                return None
            raise RuntimeLookupError(found.get('Error') or "OMDb gave no answer")
        return ui_metadata.parse_runtime(found.get('Runtime')) if found.get('Type') == "movie" else None
    if provider != "tmdb":
        return None
    api_key = cfg.arm_config['TMDB_API_KEY']
    found = _lookup_json(f"https://api.themoviedb.org/3/find/{urllib.parse.quote(str(job.imdb_id))}"
                         f"?api_key={api_key}&external_source=imdb_id")
    movies = found.get('movie_results') or []
    if not movies:
        logging.debug(f"TMDB has no film with IMDb id {job.imdb_id}")
        return None
    details = _lookup_json(f"https://api.themoviedb.org/3/movie/{movies[0]['id']}?api_key={api_key}")
    return int(details['runtime']) * 60 if details.get('runtime') else None


def _names_title(result, query) -> bool:
    """Whether a search result is titled ``query``, alone or before a subtitle ("Stagecoach: The Texas Jack Story")."""
    wanted = _normalise_title(query)
    title = str(result.get("Title") or result.get("title") or "")
    main_title = re.split(r":|\s[-–]\s", title, maxsplit=1)[0]
    return bool(wanted) and wanted in (_normalise_title(title), _normalise_title(main_title))


def runtime_candidates(job):
    """
    The films a disc could be instead of the one it was identified as, most voted for first: search
    results (without a year) titled like the disc label or the job title, alone or followed by a
    subtitle. Leaving the year out matters for a Blu-ray, whose year is the date its release was
    authored: TMDB filtered to 2017 has no RED (2010), only a 15 minute short called Red. Only films
    count: a series' runtime is an episode's, and taking one would make the movie rip a series.

    :return: search results in OMDB style; the identified film itself is left out
    """
    queries = []
    for text in (str(job.label or "").replace("_", " "), job.title):
        if not is_placeholder_title(text) and _normalise_title(text) not in map(_normalise_title, queries):
            queries.append(text)
    seen, candidates = {job.imdb_id}, []
    for query in queries:
        try:
            response = _provider_search(query)
        except Exception as error:
            logging.debug(f"Metadata lookup for '{query}' failed: {error}")
            continue
        for result in (response or {}).get('Search') or []:
            key = result.get('imdbID') or result.get('id')
            # A series is no film for a movie disc: TMDB's search falls back to TV, OMDb's lists both
            if key in seen or result.get('Type') != "movie" or not _names_title(result, query):
                continue
            seen.add(key)
            candidates.append(result)
    return sorted(candidates, key=vote_count, reverse=True)


def store_films(job, films):
    """
    Replace the job's film rows with these, in disc order.

    :param films: search results in OMDB style with a ``runtime`` key (seconds or None)
    """
    for existing in list(job.films):
        db.session.delete(existing)
    db.session.commit()
    for position, film in enumerate(films, start=1):
        utils.database_adder(Film(job_id=job.job_id, position=position,
                                  title=utils.clean_for_filename(str(film['Title'])), year=str(film['Year']),
                                  imdb_id=film.get('imdbID'), video_type=film.get('Type') or "movie",
                                  poster_url=film.get('Poster'), runtime=film.get('runtime')))


def identify_collection(job, titles, source):
    """
    Identify each film of a disc that carries several (a double feature) and record them on the job.

    The first film becomes the job's title, year and output folder, as it would be for a single
    film; the rest are filed in their own folders once ripped (``arm_ripper.file_collection``
    matches the ripped titles to the films by runtime). Needs at least two distinct films found,
    otherwise nothing is changed and the caller searches the text as one title.

    :param titles: the film titles in disc order (``disc_photo.split_collection_title``)
    :param source: where the titles came from, for the log
    :return: True when the disc was identified as a collection
    """
    found = []
    for title in titles:
        best = search_film(title)
        if not best:
            logging.info(f"No match online for '{title}' ({source})")
            continue
        if best.get('imdbID') and best['imdbID'] in [film.get('imdbID') for film in found]:
            logging.info(f"'{title}' ({source}) matched the same film as an earlier title, skipping it")
            continue
        best = dict(best)
        best['runtime'] = film_runtime(best)
        logging.info(f"'{title}' ({source}) is '{best['Title']}' ({best['Year']}), "
                     f"runtime {best['runtime'] or 'unknown'}")
        found.append(best)
    if len(found) < 2:
        return False
    store_films(job, found)
    first = found[0]
    args = {
        'year_auto': str(first['Year']), 'year': str(first['Year']),
        'title_auto': utils.clean_for_filename(str(first['Title'])), 'title': utils.clean_for_filename(str(first['Title'])),
        'video_type_auto': first.get('Type') or "movie", 'video_type': first.get('Type') or "movie",
        'imdb_id_auto': first.get('imdbID'), 'imdb_id': first.get('imdbID'),
        'poster_url_auto': first.get('Poster'), 'poster_url': first.get('Poster'),
        'hasnicetitle': True,
    }
    utils.database_updater(args, job)
    logging.info(f"Disc carries {len(found)} films ({source}): "
                 + ", ".join(f"{film['Title']} ({film['Year']})" for film in found)
                 + f"; the job is filed as the first, {job.title} ({job.year})")
    return True


def identify_collection_from_label(job):
    """
    A disc label such as ``STUART_LITTLE_1_AND_2`` names its films: identify them as a collection.

    Runs for video discs that are not yet identified, after the case barcode and before the
    label search.

    :return: True when the disc was identified as a collection
    """
    if job.hasnicetitle or not job.label or job.label == "not identified":
        return False
    titles = disc_photo.split_collection_title(job.label)
    if len(titles) < 2:
        return False
    logging.info(f"Disc label '{job.label}' names several films: {titles}")
    try:
        return identify_collection(job, titles, f"label {job.label}")
    except Exception as error:
        logging.error(f"Collection lookup from the label failed: {error}", exc_info=True)
        return False


def identify_from_barcode(job):
    """
    Identify the disc from the UPC/EAN scanned from its case.

    Runs when BARCODE_LOOKUP is enabled, the job has a barcode and the disc is not yet
    identified (``hasnicetitle`` is False): first thing for video discs, since the barcode
    names the exact release, and as the fallback for music discs. Video discs go through
    upcitemdb and then the metadata search, which must find the product title itself (see
    search_film): the barcode outranks the disc's own CRC64 record and label, so a guess here
    would override a right answer there. Music discs ask MusicBrainz for a release with that
    barcode.
    The looked-up product title is kept on the job so the title fix pages can offer it even
    when no online match is found.

    :return: True when a title was found from the barcode
    """
    if not cfg.arm_config.get("BARCODE_LOOKUP", True):
        return False
    if not job.disc_barcode or job.hasnicetitle:
        return False
    if job.disctype in ["dvd", "bluray"]:
        return _identify_video_from_barcode(job)
    if job.disctype == "music":
        return _identify_music_from_barcode(job)
    return False


def _identify_video_from_barcode(job):
    logging.info(f"Disc not identified, looking up barcode {job.disc_barcode}")
    candidates = disc_photo.lookup_upc(job.disc_barcode, user_agent=f"arm/{job.arm_version}")
    if not candidates:
        logging.info("Barcode lookup returned no video products")
        return False
    best = candidates[0]
    logging.info(f"Barcode {job.disc_barcode} is '{best['raw']}' -> searching '{best['title']}' ({best['year']})")
    utils.database_updater({'disc_barcode_title': best['raw'][:256]}, job)
    titles = disc_photo.split_collection_title(best['raw'])
    if len(titles) > 1:
        if identify_collection(job, titles, f"barcode {job.disc_barcode}"):
            return True
        logging.info("Could not identify every film the product title names; searching it as one title")
    for candidate in candidates[:3]:
        best = search_film(candidate['title'], candidate['year'])
        if best:
            apply_search_result(job, best)
            logging.info(f"Identified disc from barcode as '{job.title}' ({job.year}) via '{candidate['title']}'")
            return True
    # The provider may word the brand differently ("Thomas & Friends" for "Thomas the Tank Engine
    # and Friends"): a result that ends with the product title's subtitle and names the brand is it.
    for candidate in candidates[:3]:
        best = search_film_by_subtitle(candidate['title'], candidate['year'])
        if best:
            brand, subtitle = split_brand_subtitle(candidate['title'])
            apply_search_result(job, best)
            logging.info(f"Identified disc from barcode as '{job.title}' ({job.year}): no title online is "
                         f"exactly '{candidate['title']}', but this one ends with its subtitle "
                         f"'{subtitle}' and names '{brand}'")
            return True
    logging.info("No title online is exactly the barcode's product title; "
                 "leaving the disc to its CRC64 record and label")
    return False


def barcode_product_title(job):
    """
    The title and year the case barcode's product title gives a video disc, cleaned of packaging
    words (``disc_photo.clean_upc_title``).

    :return: ``(title, year)``; ``(None, None)`` when the job has no product title or it is all
             packaging ("DVD", "Blu-ray Disc")
    """
    raw = getattr(job, 'disc_barcode_title', None)
    if not raw:
        return None, None
    return disc_photo.clean_upc_title(raw)


def _label_titles(job):
    """
    Every title the disc's own identification makes from its label when it finds nothing better:
    identify_dvd's failsafe keeps the label, identify_bluray without a bdmt_eng.xml title-cases it
    ("Cartoons D3") and with an unreadable one runs it through the filename cleaner.
    """
    label = str(job.label)
    ascii_label = unicodedata.normalize('NFKD', label).encode('ascii', 'ignore').decode()
    return {label, label.replace('_', ' ').title(), utils.clean_for_filename(ascii_label)}


def title_from_barcode_product(job):
    """
    Name a disc nothing online matched after its case barcode's product title.

    Runs last in :func:`identify` for video discs still without a nice title, after
    :func:`title_from_disc_photo` (the disc's own label, when a vision model read it). A disc whose
    barcode resolved to a product (``disc_barcode_title``) but whose title no provider has, and
    whose label found nothing exact either, would otherwise be filed under its label
    (``CARTOONS_D3``, or ``Cartoons D3`` for a Blu-ray); the product title ("59 Cartoon
    Classics") is the better name for the unidentified folder and the title fix page. A title
    the disc itself supplied (a Blu-ray's bdmt_eng.xml) is kept. The year goes with the title:
    the product's, or none, never the date of a Blu-ray's metadata file. ``hasnicetitle`` stays
    False: this is a name, not an identification.

    :return: True when the title was set from the product title
    """
    if job.hasnicetitle or not getattr(job, 'disc_barcode_title', None):
        return False
    if job.title not in (None, "", "not identified") and str(job.title) not in _label_titles(job):
        return False
    title, year = barcode_product_title(job)
    if not title:
        return False
    title = utils.clean_for_filename(title)
    year = year or ""
    args = {'title': title, 'title_auto': title, 'year': year, 'year_auto': year}
    logging.info(f"Nothing online matched the disc; naming it after its case barcode's product title: "
                 f"'{title}' ({year or 'no year'})")
    utils.database_updater(args, job)
    return True


def _identify_music_from_barcode(job):
    """
    Ask MusicBrainz for a release carrying this barcode and take artist and title from it.

    Both the 12 digit UPC-A and the 13 digit EAN-13 spelling are tried, since scanners and
    the MusicBrainz database disagree on which one is stored.
    """
    import musicbrainzngs as mb  # pylint: disable=import-outside-toplevel

    logging.info(f"Audio disc not identified, searching MusicBrainz for barcode {job.disc_barcode}")
    mb.set_useragent(app="arm", version=str(job.arm_version), contact="https://github.com/automatic-ripping-machine")
    releases = []
    for form in disc_photo.barcode_forms(job.disc_barcode):
        try:
            result = mb.search_releases(barcode=form, limit=5)
        except mb.WebServiceError as error:
            logging.warning(f"MusicBrainz barcode search failed: {error}")
            return False
        releases = result.get('release-list') or []
        if releases:
            break
        logging.debug(f"No MusicBrainz release with barcode {form}")
    if not releases:
        logging.info("No MusicBrainz release carries that barcode")
        return False
    release = releases[0]
    title = str(release.get('title') or "")
    artist = str(release.get('artist-credit-phrase') or "")
    if not title:
        return False
    artist_title = f"{artist} {title}".strip()
    year = str(release.get('date') or "")[:4]
    logging.info(f"Barcode {job.disc_barcode} is '{artist_title}' ({year or 'no year'}) on MusicBrainz")
    args = {
        'crc_id': release.get('id'),
        'title': artist_title,
        'title_auto': artist_title,
        'year': year,
        'year_auto': year,
        'video_type': "Music",
        'hasnicetitle': True,
        'disc_barcode_title': artist_title[:256],
    }
    utils.database_updater(args, job)
    return True
