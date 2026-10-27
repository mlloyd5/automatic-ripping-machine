"""
Double features: mark the disc in the drive as carrying several films, picked one by one from a title
search, when the ripper did not find them itself (identify.identify_collection reads them only from the
case barcode's product title, the disc label or the disc's own metadata).

The films are stored as the job's Film rows, the same rows identify_collection writes, so the ripper files
the rip as it files any double feature (arm_ripper.file_collection): each ripped title whose length fits
a film's runtime is that film's main feature, the first film in the job folder, the others in their own
``Title (Year)`` folders.
"""
import re

import arm.config.config as cfg
import arm.ui.utils as ui_utils
from arm.ui import app, db
from arm.ui import metadata
from arm.models.film import Film

VIDEO_DISCTYPES = ("dvd", "bluray")
"""Disc types that can carry a double feature"""
IMDB_ID_RE = re.compile(r"^tt\d{5,10}$")


def same_title(first, second):
    """
    Whether two cleaned titles name the same film: the ripper cleans ``Stuart Little`` to ``Stuart-Little``
    (identify_collection), the ui keeps the space
    """
    def words(title):
        return re.sub(r"[\s_-]+", " ", str(title or "")).strip().lower()
    return bool(words(first)) and words(first) == words(second)


def films_of(job):
    """The job's films in disc order"""
    return sorted(job.films, key=lambda film: int(film.position or 0))


def check_editable(job):
    """
    Why the films of a job cannot be changed, or None when they can: only a video disc that has not
    finished (once filed, the rows record where each film went) and is not a bonus disc (whose titles are
    all extras of another job's film, whatever this disc carries)

    :return: message or None
    """
    if job.finished:
        return f"Job {job.job_id} has finished; its titles have been filed already"
    if job.disctype not in VIDEO_DISCTYPES:
        return f"Job {job.job_id} is a {job.disctype} disc, not a DVD or Blu-ray"
    if job.bonus_of_job_id:
        return (f"Job {job.job_id} is marked as the bonus disc of job {job.bonus_of_job_id}; "
                "clear that first on its Bonus Disc page")
    return None


def search(title, year=None):
    """
    Films the metadata provider has for a title, with the year and then, when that finds none, without it

    :return: list of search results in OMDB style (``Title``, ``Year``, ``imdbID``, ``Poster``; TMDB's
             also carry its ``id``)
    """
    if not str(title or "").strip():
        return []
    attempts = [year, ""] if year else [""]
    for attempt in attempts:
        try:
            response = ui_utils.metadata_selector("search", str(title).strip(), str(attempt or "").strip())
        except Exception as error:
            app.logger.warning(f"Double feature search for '{title}' ({attempt}) failed: {error}")
            response = None
        results = [result for result in (response or {}).get('Search') or []
                   if str(result.get('Type', 'movie')).lower() == "movie"]
        if results:
            return results
    return []


def lookup_runtime(tmdb_id, imdb_id):
    """
    The film's runtime in seconds from the configured metadata provider (search results carry none), or
    None when it is unknown or the lookup failed
    """
    provider = str(cfg.arm_config.get('METADATA_PROVIDER', '')).lower()
    try:
        if provider == "tmdb" and str(tmdb_id or "").isdigit():
            return metadata.tmdb_runtime(int(tmdb_id))
        if provider == "omdb" and imdb_id:
            return metadata.omdb_runtime(imdb_id)
    except Exception as error:
        app.logger.warning(f"Runtime lookup for {imdb_id or tmdb_id} failed: {error}")
    return None


def sync_job(job):
    """
    Once the job has two films or more it is a double feature, filed as its first film: give the job that
    film's title, year, IMDb id and poster as a title fix would (setting the manual title also ends the
    ripper's manual wait, see utils.check_for_wait). With fewer films the job's title is left alone.
    """
    films = films_of(job)
    if len(films) < 2:
        db.session.commit()
        return
    first = films[0]
    job.title = job.title_manual = first.title
    job.year = job.year_manual = first.year
    job.imdb_id = job.imdb_id_manual = first.imdb_id
    job.poster_url = job.poster_url_manual = first.poster_url
    job.video_type = job.video_type_manual = "movie"
    job.hasnicetitle = True
    db.session.commit()


def renumber(job):
    """Positions 1, 2, ... in the current order, after a film was removed or moved"""
    for position, film in enumerate(films_of(job), start=1):
        film.position = position


def add_film(job, form):
    """
    Add a film picked from the search results as the next film of the disc.

    :param form: the posted search result: ``title``, ``year``, ``imdb_id``, ``tmdb_id``, ``poster``
    :return: (success, message)
    """
    if (reason := check_editable(job)) is not None:
        return False, reason
    title = ui_utils.clean_for_filename(str(form.get('title') or '').strip())
    year = str(form.get('year') or '').strip()[:4]
    imdb_id = str(form.get('imdb_id') or '').strip()
    poster = str(form.get('poster') or '').strip()
    if not title:
        return False, "Pick a film from the search results"
    if year and not year.isdigit():
        return False, f"'{year}' is not a year"
    if imdb_id and not IMDB_ID_RE.match(imdb_id):
        return False, f"'{imdb_id}' is not an IMDb id"
    if not poster.startswith(("http://", "https://")):
        poster = None
    films = films_of(job)
    for film in films:
        if (imdb_id and film.imdb_id == imdb_id) or (same_title(film.title, title) and film.year == (year or None)):
            return False, f"{film.folder_name} is film {film.position} of this disc already"
    runtime = lookup_runtime(form.get('tmdb_id'), imdb_id or None)
    film = Film(job.job_id, len(films) + 1, title, year or None, imdb_id or None, poster_url=poster,
                runtime=runtime)
    db.session.add(film)
    db.session.flush()
    sync_job(job)
    message = f"Added {film.folder_name} as film {film.position} of job {job.job_id}"
    if runtime is None:
        message += (". Its runtime is unknown, so no ripped title can be matched to it"
                    + (" except by the main feature rule" if film.position == 1 else ""))
    return True, message


def _film_of(job, film_id):
    film = Film.query.get(int(film_id)) if str(film_id or "").isdigit() else None
    return film if film is not None and film.job_id == job.job_id else None


def remove_film(job, film_id):
    """:return: (success, message)"""
    if (reason := check_editable(job)) is not None:
        return False, reason
    if (film := _film_of(job, film_id)) is None:
        return False, f"Film {film_id} is not a film of job {job.job_id}"
    name = film.folder_name
    db.session.delete(film)
    db.session.flush()
    renumber(job)
    sync_job(job)
    return True, f"Removed {name} from job {job.job_id}"


def move_film_up(job, film_id):
    """Move a film one place up; the first film is the one filed in the job's own folder. :return: (success, message)"""
    if (reason := check_editable(job)) is not None:
        return False, reason
    if (film := _film_of(job, film_id)) is None:
        return False, f"Film {film_id} is not a film of job {job.job_id}"
    films = films_of(job)
    index = films.index(film)
    if index == 0:
        return False, f"{film.folder_name} is the first film already"
    films[index - 1], films[index] = films[index], films[index - 1]
    for position, each in enumerate(films, start=1):
        each.position = position
    sync_job(job)
    return True, f"{film.folder_name} is film {film.position} of job {job.job_id}"


def clear_films(job):
    """Not a double feature: remove every film; the job keeps its title. :return: (success, message)"""
    if (reason := check_editable(job)) is not None:
        return False, reason
    count = Film.query.filter_by(job_id=job.job_id).delete()
    db.session.commit()
    return True, f"Job {job.job_id} is no longer a double feature ({count} film(s) removed)"


def clear_for_title_fix(job, imdb_id=None, title=None):
    """
    A title fix says which one film the disc is: forget the films a double feature was identified as,
    unless the new title is the first of them (the film the job is filed as anyway). A finished job keeps
    its films, which record where each was filed.

    :return: the number of films removed
    """
    if job.finished:
        return 0
    films = films_of(job)
    if not films:
        return 0
    first = films[0]
    if imdb_id and first.imdb_id:
        if imdb_id == first.imdb_id:
            return 0
    elif same_title(title, first.title):
        return 0  # no IMDb id on one side (TMDB may have none): compare the titles
    app.logger.info(f"Title fix of job {job.job_id}: it is no longer a double feature of "
                    + ", ".join(film.folder_name for film in films))
    count = Film.query.filter_by(job_id=job.job_id).delete()
    db.session.commit()
    return count
