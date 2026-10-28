"""
Bonus discs: mark a disc as the bonus (supplemental) disc of a recent job, so the ripper files every
title it rips as extras of that job's film (see arm.ripper.utils.bonus_parent) instead of identifying
it as a film of its own.
"""
from arm.ui import db
from arm.models.job import Job, JobState
from arm.models.film import Film

BONUS_CANDIDATES = 20
"""How many recent successful movie jobs the bonus disc page offers"""
CANDIDATE_DISCTYPES = ("dvd", "bluray")
"""Disc types whose rips a disc can be the bonus disc of"""


def film_label(title, year):
    """``Title (Year)``, or the title alone when the year is unknown"""
    if year and year not in ("0000", ""):
        return f"{title} ({year})"
    return str(title)


def bonus_candidates(job, limit=BONUS_CANDIDATES):
    """
    The jobs (newest finish first) a disc can be the bonus disc of: successful DVD and Blu-ray movie rips
    that are not bonus discs or continuations (the second side of a film) themselves, each with the later
    films of its double feature. The job's current choice is always included, however old.

    :return: list of dicts with 'job' and 'films' (the job's films after the first, by position)
    """
    query = Job.query.filter(Job.status == JobState.SUCCESS.value, Job.video_type == "movie",
                             Job.disctype.in_(CANDIDATE_DISCTYPES), Job.job_id != job.job_id,
                             Job.bonus_of_job_id.is_(None), Job.continuation_of_job_id.is_(None))
    jobs = query.order_by(Job.stop_time.desc(), Job.job_id.desc()).limit(limit).all()
    current = Job.query.get(job.bonus_of_job_id) if job.bonus_of_job_id else None
    if current is not None and current not in jobs:
        jobs.append(current)
    return [{'job': candidate, 'films': [film for film in candidate.films if int(film.position or 1) > 1]}
            for candidate in jobs]


def is_candidate(parent):
    """
    True when a job can be the film of a bonus disc: the bonus_candidates filter, checked on a posted choice
    too (a stale form or a hand-made POST), since the ripper files the extras in the job's finished folder.
    """
    return (parent.status == JobState.SUCCESS.value and parent.video_type == "movie"
            and parent.disctype in CANDIDATE_DISCTYPES)


def parse_choice(choice):
    """
    ``job:<id>``, ``film:<id>`` or ``none`` from the bonus disc form.

    :return: ('job' | 'film' | 'none', int id or None), or (None, None) when it is none of these
    """
    kind, _, value = str(choice or "").partition(":")
    if kind == "none" and not value:
        return "none", None
    if kind in ("job", "film") and value.isdigit():
        return kind, int(value)
    return None, None


def set_bonus_of(job, choice):
    """
    Store the bonus disc choice on a job that has not finished yet.

    Marking it also gives the job the film's title, year, metadata id and poster, as a title fix would
    (the ripper does the same when it picks the choice up), and clears a continuation mark, with the
    ``continues`` mark it set on its film's job (continuation.drop_continuation); clearing it leaves the
    title alone.

    :param job: the job of the disc in the drive
    :param choice: see parse_choice
    :return: (success, message)
    """
    if job.finished:
        return False, f"Job {job.job_id} has finished; its titles have been filed already"
    kind, ident = parse_choice(choice)
    if kind is None:
        return False, "Pick a job or film from the list"
    if kind == "none":
        job.bonus_of_job_id = job.bonus_of_film_id = None
        db.session.commit()
        return True, f"Job {job.job_id} is no longer a bonus disc"
    film = None
    if kind == "film":
        film = Film.query.get(ident)
        if film is None:
            return False, f"Film {ident} not found"
        parent = Job.query.get(film.job_id)
    else:
        parent = Job.query.get(ident)
    if parent is None or parent.job_id == job.job_id:
        return False, "That job cannot be the film of this bonus disc"
    if parent.bonus_of_job_id:
        return False, f"Job {parent.job_id} is a bonus disc itself"
    if parent.continuation_of_job_id:
        return False, f"Job {parent.job_id} continues the film of job {parent.continuation_of_job_id}; pick that job"
    if not is_candidate(parent):
        return False, (f"Job {parent.job_id} is not a finished DVD or Blu-ray film rip "
                       f"({parent.disctype} {parent.video_type}, {parent.status})")
    # Imported here: continuation.py imports this module
    from arm.ui.jobs.continuation import drop_continuation
    source = film if film is not None and int(film.position or 1) > 1 else parent
    job.bonus_of_job_id = parent.job_id
    job.bonus_of_film_id = film.film_id if film is not None else None
    released = drop_continuation(job)
    job.title, job.year, job.imdb_id = source.title, source.year, source.imdb_id
    job.poster_url = source.poster_url or parent.poster_url
    job.video_type = "movie"
    job.hasnicetitle = True
    db.session.commit()
    return True, (f"Job {job.job_id} is the bonus disc of {film_label(source.title, source.year)} "
                  f"(job {parent.job_id}, disc {parent.label}): every title it rips is filed as an extra{released}")
