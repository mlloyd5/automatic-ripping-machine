"""
Split films: mark a disc as the continuation of a recent job's film (side B of a flipper DVD, the second disc
of a film too long for one), so the ripper joins its main title onto that film's main feature (see
arm.ripper.arm_ripper.file_continuation) instead of filing it as a second copy of the film; and mark a job
whose film goes on on another side or disc (``continues``), so the completed-rip shipper holds its folder
until the rest has been joined onto it.
"""
from markupsafe import escape

from arm.ui import db
from arm.models.job import Job, JobState
from arm.ui.jobs.bonus import CANDIDATE_DISCTYPES, film_label

CONTINUATION_CANDIDATES = 20
"""How many recent successful jobs the split film page offers"""


def continuation_candidates(job, limit=CONTINUATION_CANDIDATES):
    """
    The jobs (newest finish first) a disc can be the continuation of: successful DVD and Blu-ray rips that
    are not series, bonus discs or continuations themselves (a third disc continues the first one's job
    too). An unidentified rip counts: its folder holds its main feature as well. The job's current choice
    is always included, however old.

    :return: list of Job
    """
    query = Job.query.filter(Job.status == JobState.SUCCESS.value, Job.video_type != "series",
                             Job.disctype.in_(CANDIDATE_DISCTYPES), Job.job_id != job.job_id,
                             Job.bonus_of_job_id.is_(None), Job.continuation_of_job_id.is_(None))
    jobs = query.order_by(Job.stop_time.desc(), Job.job_id.desc()).limit(limit).all()
    current = Job.query.get(job.continuation_of_job_id) if job.continuation_of_job_id else None
    if current is not None and current not in jobs:
        jobs.append(current)
    return jobs


def is_candidate(parent):
    """
    True when a job can be the film of a continuation: the continuation_candidates filter, checked on a posted
    choice too (a stale form or a hand-made POST), since the ripper joins onto the job's finished main feature.
    """
    return (parent.status == JobState.SUCCESS.value and parent.video_type != "series"
            and parent.disctype in CANDIDATE_DISCTYPES and not parent.bonus_of_job_id
            and not parent.continuation_of_job_id)


def parse_choice(choice):
    """
    ``job:<id>`` or ``none`` from the split film form.

    :return: ('job' | 'none', int id or None), or (None, None) when it is neither
    """
    kind, _, value = str(choice or "").partition(":")
    if kind == "none" and not value:
        return "none", None
    if kind == "job" and value.isdigit():
        return kind, int(value)
    return None, None


def set_continuation_of(job, choice):
    """
    Store the continuation choice on a job that has not finished yet.

    Marking it gives the job the film's title, year, metadata id and poster, as a title fix would (the
    ripper does the same when it picks the choice up), clears a bonus disc mark, and marks the film's job
    as ``continues``, so the completed-rip shipper holds the film's folder from now on. Clearing it clears
    that mark again and leaves the title alone.

    :param job: the job of the disc in the drive
    :param choice: see parse_choice
    :return: (success, message)
    """
    if job.finished:
        return False, f"Job {job.job_id} has finished; its titles have been filed already"
    kind, ident = parse_choice(choice)
    if kind is None:
        return False, "Pick a job from the list"
    if kind == "none":
        previous = Job.query.get(job.continuation_of_job_id) if job.continuation_of_job_id else None
        if previous is not None:
            previous.continues = False
        job.continuation_of_job_id = None
        db.session.commit()
        return True, f"Job {job.job_id} no longer continues another job's film"
    parent = Job.query.get(ident)
    if parent is None or parent.job_id == job.job_id:
        return False, "That job cannot be the film this disc continues"
    if not is_candidate(parent):
        return False, (f"Job {parent.job_id} is not a finished DVD or Blu-ray film rip of its own "
                       f"({parent.disctype} {parent.video_type}, {parent.status})")
    job.continuation_of_job_id = parent.job_id
    job.bonus_of_job_id = job.bonus_of_film_id = None
    job.title, job.year, job.imdb_id = parent.title_manual or parent.title, parent.year, parent.imdb_id
    job.poster_url = parent.poster_url
    job.video_type = parent.video_type
    job.hasnicetitle = parent.hasnicetitle
    parent.continues = True
    db.session.commit()
    # Flashed with |safe: a title fixed by hand, or a disc label, is shown as typed
    return True, (f"Job {job.job_id} continues {escape(film_label(job.title, job.year))} (job {parent.job_id}, "
                  f"disc {escape(parent.label)}): its main title is joined onto the film's")


def set_continues(job, continues):
    """
    Mark (or clear) a job whose film goes on on another side or disc. Marked before its rip ends, the job's
    runtime is not checked against the film's (its main title is only part of the film); while it is set,
    the completed-rip shipper holds the job's folder, so the next side can be joined onto it there.

    :param job: the job of the first side or disc
    :param continues: bool
    :return: (success, message)
    """
    if job.video_type == "series" or job.disctype not in CANDIDATE_DISCTYPES:
        return False, f"Job {job.job_id} is not a DVD or Blu-ray film rip"
    if continues and (job.bonus_of_job_id or job.continuation_of_job_id):
        return False, (f"Job {job.job_id} is a bonus disc or a continuation itself; mark the film's first "
                       "disc instead")
    job.continues = bool(continues)
    db.session.commit()
    if continues:
        return True, (f"Job {job.job_id} continues on another side or disc: its folder stays on the ripper until "
                      "that is joined onto it")
    return True, f"Job {job.job_id} no longer waits for another side or disc"
