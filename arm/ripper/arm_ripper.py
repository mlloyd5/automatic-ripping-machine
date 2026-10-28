""" Main file for running DVDs/Blu-rays/CDs/data ?
It would help clear up main and make things easier to find
"""
import sys
import os
import shutil
import logging
import re
import subprocess
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace

# If the arm module can't be found, add the folder this file is in to PYTHONPATH
# This is a bad workaround for non-existent packaging
if find_spec("arm") is None:
    sys.path.append(str(Path(__file__).parents[2]))

import arm.config.config as cfg  # noqa E402
from arm.ripper import utils, makemkv, handbrake, ffmpeg, identify  # noqa E402
from arm.ui import app, db, constants  # noqa E402
from arm.models.job import JobState  # noqa E402

# A ripped title must be at least this fraction of the longest title (by runtime, or by file size when
# no runtime is known) to be considered as the main feature. Keeps a many-chapter bonus reel from winning.
MAIN_FEATURE_MIN_FRACTION = 0.7

# A ripped title is taken for a film of a double feature when its runtime is within this fraction of
# the runtime the metadata provider gives (PAL discs run 4% fast), and never closer than this many seconds.
RUNTIME_TOLERANCE_FRACTION = 0.06
RUNTIME_TOLERANCE_MIN = 180

# Minimum title length for a bonus disc when arm.yaml has no BONUS_MINLENGTH
BONUS_MINLENGTH_DEFAULT = 45
# Files of a bonus disc's rip that are filed as extras
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".avi", ".ts", ".webm")
# mkvmerge exits 1 when it wrote the file but has warnings (a track's codec private data differs, say)
MKVMERGE_OK = (0, 1)
# When the identified film's runtime fits none of the disc's feature-length titles, the runtimes of
# at most this many other films with the disc's title are looked up (one provider request each).
RUNTIME_CHECK_MAX_CANDIDATES = 6


def rip_visual_media(have_dupes, job, logfile, protection):
    """
    Main ripping function for dvd and Blu-rays, movies or series
    \n
    :param have_dupes: Does this disc already exist in the database
    :param job: Current job
    :param logfile: Current logfile
    :param protection: Does the disc have 99 track protection
    :return: None
    """
    # A bonus disc rips every short title and files them all as the film's extras; a continuation (side B,
    # disc 2) has its main title joined onto the film's. Both are filed with an earlier job's film
    linked = apply_linked_settings(job)
    # A double feature rips every film, even with MAINFEATURE on (which still keeps its extras out)
    main_features_only = not linked and apply_collection_settings(job)
    # Fix the sub-folder type - (movie|tv|unknown)
    type_sub_folder = utils.convert_job_type(job.video_type)
    # Fix the job title - Title (Year) | Title
    job_title = utils.fix_job_title(job)

    if linked:
        # Not completed/<type>/<Title (Year)>: that is the film's folder, which may have shipped already,
        # and an empty transcode folder of that name would stop the next rip of the title as a duplicate
        transcode_out_path = os.path.join(job.config.TRANSCODE_PATH, type_sub_folder,
                                          f"{job_title} - {linked} job{job.job_id}")
        utils.make_dir(transcode_out_path)
        final_directory = linked_folder(job, linked)
    else:
        # We need to check/construct the final path, and the transcode path
        transcode_out_path = os.path.join(job.config.TRANSCODE_PATH, type_sub_folder, job_title)
        final_directory = os.path.join(job.config.COMPLETED_PATH, type_sub_folder, job_title)

        # Check folders for already ripped jobs -> creates folder
        transcode_out_path = utils.check_for_dupe_folder(have_dupes, transcode_out_path, job)
        # If dupes rips is disabled this might kill the run
        final_directory = utils.check_for_dupe_folder(have_dupes, final_directory, job)
    transcode_folder = transcode_out_path
    linked_transcode_path = transcode_folder if linked else None

    # Update the job.path with the final directory
    utils.database_updater({'path': final_directory}, job)
    # Save poster image from disc if enabled (a bonus disc's or a continuation's would replace the film's)
    if not linked:
        utils.save_disc_poster(final_directory, job)

    logging.info(f"Processing files to: {transcode_out_path}")
    makemkv_out_path = None
    transcode_in_path = str(job.devpath)
    # Do we need to use MakeMKV - Blu-rays, protected dvd's, and dvd with mainfeature off
    use_make_mkv = rip_with_mkv(job, protection)
    logging.debug(f"Using MakeMKV: [{use_make_mkv}]")
    if use_make_mkv:
        logging.info("************* Ripping disc with MakeMKV *************")
        # Run MakeMKV and get path to output
        job.status = JobState.VIDEO_RIPPING.value
        db.session.commit()
        try:
            makemkv_out_path = makemkv.makemkv(job)
        except Exception as mkv_error:  # noqa: E722
            raise utils.RipperException("Error while running MakeMKV") from mkv_error

        # The titles' lengths are known now, so the identification can be checked against them.
        # The rip is done by now: a failed check must not fail the job.
        try:
            check_runtime(job)
        except Exception as error:
            logging.error(f"Checking the runtime of {job.title} failed: {error}", exc_info=True)
        if job.config.NOTIFY_RIP:
            utils.notify(job, constants.NOTIFY_TITLE, f"{job.title} rip complete. Starting transcode. ")
        logging.info("************* Ripping with MakeMKV completed *************")
        # point HB/FFMPEG to the path MakeMKV ripped to
        transcode_in_path = makemkv_out_path
    # Begin transcoding section - only transcode if skip_transcode is false
    start_transcode(job, logfile, transcode_in_path, transcode_out_path, protection)

    # --------------- POST PROCESSING ---------------
    # If ripped with MakeMKV remove the 'out' folder and set the raw as the output
    logging.debug(f"Transcode status: [{job.config.SKIP_TRANSCODE}] and MakeMKV Status: [{use_make_mkv}]")
    if job.config.SKIP_TRANSCODE and use_make_mkv:
        utils.delete_raw_files([transcode_out_path])
        transcode_out_path = transcode_in_path

    # Marked (or no longer marked) as a bonus disc or continuation in the ui since the wait: file it accordingly
    linked, final_directory, linked_transcode_path = recheck_linked(
        job, linked, final_directory, transcode_folder, linked_transcode_path, type_sub_folder, job_title)

    # Update final path if user has set a custom/manual title, or check_runtime found a better match.
    # Not for a bonus disc or continuation: its titles go to the film's folder whatever the job is called
    logging.debug(f"Job title status: [{job.title_manual}]")
    if not linked:
        final_directory = retitle_final_directory(have_dupes, job, final_directory, type_sub_folder, job_title)

    # Move to final folder (a continuation that could not be joined goes to a folder of its own: job.path)
    move_files_post(transcode_out_path, job, main_features_only)
    if linked:
        final_directory = job.path
        # Beside the film's own disc.jpg; the case barcode and poster are the film's already
        utils.copy_disc_photo(final_directory, job, linked_photo_name(job, linked))
    else:
        # Keep the webcam photo of the disc and the barcode scanned from its case with the rip
        utils.copy_disc_photo(final_directory, job)
        utils.copy_disc_barcode(final_directory, job)
        # Movie the movie poster if we have one - no longer needed, now handled by save_movie_poster
        utils.move_movie_poster(final_directory, transcode_out_path)
    # Scan Emby if arm.yaml requires it
    utils.scan_emby()
    # Set permissions if arm.yaml requires it
    utils.set_permissions(final_directory)
    # If set in the arm.yaml remove the raw files
    utils.delete_raw_files([transcode_in_path, transcode_out_path, makemkv_out_path])
    if linked_transcode_path:
        # Kept by DELRAWFILES: false, but empty and named after no title, so of no use to anyone
        remove_empty_dir(linked_transcode_path)
    # report errors if any
    notify_exit(job)
    logging.info("************* ARM processing complete *************")


def linked_kind(job):
    """``"bonus"`` for a bonus disc, ``"continuation"`` for a continuation, None for an ordinary disc"""
    if utils.is_bonus_disc(job):
        return "bonus"
    if utils.is_continuation(job):
        return "continuation"
    return None


def apply_linked_settings(job):
    """
    Settings for a disc filed with an earlier job's film (apply_bonus_settings, apply_continuation_settings)\n
    :param job: Current job
    :return: linked_kind of the job
    """
    if apply_bonus_settings(job):
        return "bonus"
    if apply_continuation_settings(job):
        return "continuation"
    return None


def linked_folder(job, kind):
    """bonus_folder or continuation_folder, by linked_kind"""
    if kind == "bonus":
        folder = bonus_folder(job)
        logging.info(f"Bonus disc: filing every title in {os.path.join(folder, utils.bonus_extras_sub(job))}")
        return folder
    folder = continuation_folder(job)
    if folder == continuation_target_folder(job):
        logging.info(f"Continuation: joining the main title onto the film's in {folder}")
    else:
        logging.info(f"Continuation: the film's main feature is not in {continuation_target_folder(job)} (shipped "
                     f"already?); filing the main title in {folder} to be joined by hand")
    return folder


def linked_photo_name(job, kind):
    """bonus_photo_name, or ``disc-continuation-<disc label>.jpg`` for a continuation"""
    if kind == "bonus":
        return bonus_photo_name(job)
    return f"disc-continuation-{utils.bonus_tag(job)}.jpg"


def recheck_linked(job, linked, final_directory, transcode_folder, linked_transcode_path, type_sub_folder,
                   job_title):
    """
    Take up a bonus disc or continuation choice made, cleared or changed in the ui while the disc ripped\n
    :param linked: what the rip started as (apply_linked_settings)
    :param final_directory: the completed folder picked when the rip started
    :param transcode_folder: the transcode folder the rip used
    :param linked_transcode_path: the transcode folder to remove once empty, or None
    :return: (linked, final_directory, linked_transcode_path) to file the rip with
    """
    db.session.refresh(job)
    kind = linked_kind(job)
    if kind:
        if not linked:
            parent = job.bonus_of_job_id if kind == "bonus" else job.continuation_of_job_id
            logging.info(f"Job was marked as the {kind} of job {parent} during the rip; filing it with that film")
            # Before linked_folder: identified as the film itself once the film had shipped, this folder
            # has the film's own name, and linked_folder would take it for the film's folder
            remove_unused_final_dir(final_directory)
            linked_transcode_path = transcode_folder
        # The film's folder may have shipped (or the choice changed) since the rip started
        final_directory = linked_folder(job, kind)
        utils.database_updater({'path': final_directory}, job)
    elif linked:
        # Not in the film's folder, where the main feature would meet the film's own file
        logging.warning(f"Job is no longer marked as a {linked}; filing its titles the usual way")
        final_directory = utils.check_for_dupe_folder(
            False, os.path.join(job.config.COMPLETED_PATH, type_sub_folder, job_title), job)
        utils.database_updater({'path': final_directory}, job)
    return kind, final_directory, linked_transcode_path


def retitle_final_directory(have_dupes, job, final_directory, type_sub_folder, job_title):
    """
    Give the completed folder the job's title when it changed during the rip: a title set by hand
    in the meantime, or another film taken by check_runtime. The new folder goes through the same
    duplicate check as the first one, so a film ripped before keeps its folder (RED (2010) is
    already there when a disc first taken for Red (2017) turns out to be RED); the first folder,
    which holds no more than the disc's poster at this point, is removed whatever DELRAWFILES says\n
    :param have_dupes: Does this disc already exist in the database
    :param final_directory: the completed folder made for the title the rip started with
    :param job_title: that title (utils.fix_job_title at the start of the rip)
    :return: the completed folder to file the rip in
    """
    new_title = utils.fix_job_title(job)
    if new_title == job_title:
        return final_directory
    logging.info(f"Job title changed from {job_title} to {new_title}; renaming the completed folder")
    new_directory = utils.check_for_dupe_folder(
        have_dupes, os.path.join(job.config.COMPLETED_PATH, type_sub_folder, new_title), job)
    remove_superseded_folder(final_directory, new_directory)
    utils.database_updater({'path': new_directory}, job)
    return new_directory


def remove_superseded_folder(old_directory, new_directory):
    """
    Remove the completed folder a job no longer uses, moving the disc's poster (save_disc_poster)
    to the new one first. A folder holding anything else is left alone with a warning\n
    :param old_directory: the folder to remove
    :param new_directory: the job's folder now
    """
    poster = os.path.join(old_directory, "poster.png")
    try:
        if os.path.isfile(poster) and not os.path.exists(os.path.join(new_directory, "poster.png")):
            shutil.move(poster, os.path.join(new_directory, "poster.png"))
        os.rmdir(old_directory)
        logging.info(f"Removed the superseded folder {old_directory}")
    except FileNotFoundError:
        pass
    except OSError as error:
        logging.warning(f"Left the superseded folder {old_directory} in place: {error}")


def start_transcode(job, logfile, raw_in_path, transcode_out_path, protection):
    """
    This checks if transcoding is enabled for the job and then passes it off to the correct
    transcoding function, first if handbrake or ffmpegshould be used, then how it should be ripped\n
    :param raw_in_path: HandBrake in path (makeMKV_out_path|/dev/sr0)
    :param transcode_out_path: Path HandBrake should put the files (transcode_path)
    :param job: Current job
    :param logfile: Current logfile
    :param protection: If disc has 99 track protection
    :return: None
    """
    if job.config.SKIP_TRANSCODE:
        logging.info("Transcoding is disabled, skipping transcode")
        return None

    # Update db with transcoding status
    utils.database_updater({'status': "transcoding"}, job)
    # Use FFMPEG or HandBrake depending on arm.yaml setting
    if job.config.USE_FFMPEG:
        logging.info("************* Starting Transcode With FFMPEG *************")
        # If it was ripped with MakeMKV or we are doing a mkv rip then run the ffmpeg_mkv function
        if rip_with_mkv(job, protection) and job.config.RIPMETHOD == "mkv":
            logging.debug(f"ffmpeg_mkv: {raw_in_path}, {transcode_out_path}")
            ffmpeg.ffmpeg_mkv(raw_in_path, transcode_out_path, job)
        # Otherwise if it is a movie and mainfeature is enabled then run ffmpeg_main_feature
        elif job.video_type == "movie" and job.config.MAINFEATURE and job.hasnicetitle:
            logging.debug(f"ffmpeg_main_feature: {raw_in_path}, {transcode_out_path}")
            ffmpeg.ffmpeg_main_feature(raw_in_path, transcode_out_path, job)
            db.session.commit()
        # Finally if it is a series or mainfeature is disabled run ffmpeg_all to transcode all tracks
        else:
            logging.debug(f"ffmpeg_all: {raw_in_path}, {transcode_out_path}")
            ffmpeg.ffmpeg_all(raw_in_path, transcode_out_path, job)
            db.session.commit()
        logging.info("************* Finished Transcode With FFMPEG *************")
        # After transcoding update db status back to active
        utils.database_updater({'status': "active"}, job)
        return True

    elif not job.config.USE_FFMPEG:
        logging.info("************* Starting Transcode With HandBrake *************")
        # If it was ripped with MakeMKV or we are doing a mkv rip then run the handbrake_mkv function
        if rip_with_mkv(job, protection) and job.config.RIPMETHOD == "mkv":
            logging.debug(f"handbrake_mkv: {raw_in_path}, {transcode_out_path}, {logfile}")
            handbrake.handbrake_mkv(raw_in_path, transcode_out_path, logfile, job)
        # Otherwise if it is a movie and mainfeature is enabled then run handbrake_main_feature
        elif job.video_type == "movie" and job.config.MAINFEATURE and job.hasnicetitle:
            logging.debug(f"handbrake_main_feature: {raw_in_path}, {transcode_out_path}, {logfile}")
            handbrake.handbrake_main_feature(raw_in_path, transcode_out_path, logfile, job)
            db.session.commit()
        # Finally if it is a series or mainfeature is disabled run handbrake_all to transcode all tracks
        else:
            logging.debug(f"handbrake_all: {raw_in_path}, {transcode_out_path}, {logfile}")
            handbrake.handbrake_all(raw_in_path, transcode_out_path, logfile, job)
            db.session.commit()
        logging.info("************* Finished Transcode With HandBrake *************")
        # After transcoding update db status back to active
        utils.database_updater({'status': "active"}, job)
        return True
    else:
        logging.info("Invalid transcoding option selected. Skipping transcode."
                     "Set USE_FFMPEG with valid boolean value fix error")
        return None


def notify_exit(job):
    """
    Notify post transcoding - ARM finished\n
    Includes any errors
    :param job: current job
    :return: None
    """
    if job.config.NOTIFY_TRANSCODE:
        if job.errors:
            errlist = ', '.join(job.errors)
            utils.notify(job, constants.NOTIFY_TITLE,
                         f" {job.title} processing completed with errors. "
                         f"Title(s) {errlist} failed to complete. ")
            logging.info(f"Transcoding completed with errors.  Title(s) {errlist} failed to complete. ")
        else:
            utils.notify(job, constants.NOTIFY_TITLE, f"{job.title} {constants.PROCESS_COMPLETE}")


def move_files_post(transcode_out_path, job, main_features_only=False):
    """
    Logic for moving files post transcoding\n
    if series move all to 1 folder\n
    if movie check what source we got them from, for MakeMKV use skip_transcode_movie to pick the main feature\n
    if a bonus disc, everything is an extra of the film it belongs to (file_bonus)\n
    if a continuation, its main title is joined onto the film's (file_continuation)\n
    :param transcode_out_path: This should either be the RAW_PATH from MakeMKV, /dev/srX or TRANSCODE_PATH
    :param job: current job
    :param main_features_only: a double feature whose MAINFEATURE apply_collection_settings lifted: file
        the films only, no extras (and with MAINFEATURE back on when it is no longer a double feature)
    :return: None
    """
    tracks = job.tracks.filter_by(ripped=True)  # .order_by(job.tracks.length.desc())
    if utils.is_bonus_disc(job):
        file_bonus(transcode_out_path, job, os.listdir(transcode_out_path))
    elif utils.is_continuation(job):
        file_continuation(transcode_out_path, job, os.listdir(transcode_out_path))
    elif job.video_type == "series":
        for track in tracks:
            utils.move_files(transcode_out_path, track.filename, job, False)
    elif file_collection(transcode_out_path, job, os.listdir(transcode_out_path), main_features_only):
        pass  # a double feature: each film went to its own folder
    else:
        if main_features_only:
            # No longer a double feature (cleared, or down to one film, during the rip): file it with the
            # MAINFEATURE the job started with, which apply_collection_settings lifted for the rip
            logging.info("Not a double feature any more: filing with MAINFEATURE on again")
            utils.database_updater({'MAINFEATURE': True}, job.config)
        for track in tracks:
            if tracks.count() == 1:
                utils.move_files(transcode_out_path, track.filename, job, True)
            else:
                # If source is MakeMKV we know the mainfeature will be wrong let skip_transcode_movie handle it
                if track.source == "MakeMKV":
                    skip_transcode_movie(os.listdir(transcode_out_path), job, transcode_out_path)
                    break
                # If HandBrake was used we can pass track.main_feature
                utils.move_files(transcode_out_path, track.filename, job, track.main_feature)


def bonus_minlength():
    """BONUS_MINLENGTH from arm.yaml in whole seconds, BONUS_MINLENGTH_DEFAULT when unset or not a number"""
    value = cfg.arm_config.get("BONUS_MINLENGTH", BONUS_MINLENGTH_DEFAULT)
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        logging.warning(f"BONUS_MINLENGTH {value!r} is not a number of seconds; using {BONUS_MINLENGTH_DEFAULT}")
        return BONUS_MINLENGTH_DEFAULT


def apply_bonus_settings(job):
    """
    Settings for a disc marked in the ui as the bonus disc of an earlier job (utils.bonus_parent)\n
    The job takes the film's title, year and metadata id, and its config (this job's only) rips every
    title of at least BONUS_MINLENGTH seconds in one MakeMKV pass: no maximum length, no duplicate-title
    skipping (which rips title by title, re-scanning the disc for every one) and no main feature\n
    RIPMETHOD is "mkv" so a DVD is ripped by MakeMKV too (rip_with_mkv) and any transcode is
    handbrake_mkv/ffmpeg_mkv of the ripped files: handbrake_all/ffmpeg_all (the other methods, or a DVD
    under mkv with MAINFEATURE on) pick titles by arm.yaml's MINLENGTH/MAXLENGTH, not this job's, and
    would skip every featurette; a "backup"/"backup_dvd" Blu-ray backup is a disc structure that
    file_bonus has no video files to file from\n
    :param job: Current job
    :return: True when the job is a bonus disc
    """
    parent, film = utils.bonus_parent(job)
    if parent is None:
        return False
    source = film or parent
    minlength = bonus_minlength()
    name = film.folder_name if film is not None else utils.fix_job_title(parent)
    logging.info(f"Bonus disc of job {parent.job_id} ({name}, disc {parent.label}): "
                 f"ripping every title of at least {minlength}s and filing them all as extras")
    if job.config.RIPMETHOD != "mkv":
        logging.info(f"Bonus disc: ripping with RIPMETHOD mkv instead of {job.config.RIPMETHOD}")
    utils.database_updater({'MINLENGTH': str(minlength), 'MAXLENGTH': "99999", 'SKIP_DUPLICATE_TITLES': False,
                            'MAINFEATURE': False, 'RIPMETHOD': "mkv"}, job.config)
    utils.database_updater({'title': source.title, 'year': source.year, 'imdb_id': source.imdb_id,
                            'poster_url': source.poster_url, 'video_type': "movie", 'hasnicetitle': True}, job)
    return True


def bonus_target_folder(job):
    """
    The folder of the film a bonus disc belongs to: the parent job's own folder (job.path), or, for a
    later film of its double feature, the film's ``Title (Year)`` folder beside it (``_<stage>``
    suffixed when file_film had to)\n
    :param job: Current job (a bonus disc)
    :return: path
    """
    parent, film = utils.bonus_parent(job)
    type_sub_folder = utils.convert_job_type("movie")
    if film is not None and int(film.position or 1) > 1:
        plain = os.path.join(job.config.COMPLETED_PATH, type_sub_folder, film.folder_name)
        suffixed = f"{plain}_{parent.stage}"
        return suffixed if os.path.isdir(suffixed) else plain
    if parent.path:
        return parent.path
    return os.path.join(job.config.COMPLETED_PATH, type_sub_folder, utils.fix_job_title(parent))


def bonus_folder(job):
    """
    Where a bonus disc's extras are filed: the film's folder (bonus_target_folder) while it is still in
    the completed path, else ``<film folder> - bonus job<id>`` beside where it was. The completed-rip
    shipper moves finished folders to the media box and removes them; a holding folder of that name
    ships into the film's folder there, where a recreated film folder would pass for the film's own rip\n
    :param job: Current job (a bonus disc)
    :return: path
    """
    target = bonus_target_folder(job)
    if os.path.isdir(target):
        return target
    return f"{target.rstrip(os.sep)} - bonus job{job.job_id}"


def bonus_photo_name(job):
    """``disc-bonus-<disc label>.jpg``: a bonus disc's photo, kept beside the film's own disc.jpg"""
    return f"disc-bonus-{utils.bonus_tag(job)}.jpg"


def file_bonus(out_path, job, files):
    """
    File a bonus disc's rip: every video file is an extra of the film's folder (job.path), named
    ``<disc label>_<file>`` by utils.move_files, never ``<Title (Year)>.mkv``\n
    :param out_path: RAW_PATH or TRANSCODE_PATH holding the ripped files
    :param job: Current job (a bonus disc)
    :param files: os.listdir(out_path)
    :return: number of files filed
    """
    videos = sorted(name for name in files if name.lower().endswith(VIDEO_EXTENSIONS)
                    and not name.startswith(".") and os.path.isfile(os.path.join(out_path, name)))
    logging.info(f"Bonus disc: filing {len(videos)} title(s) as extras in {job.path}")
    for name in videos:
        utils.move_files(out_path, name, job, False)
    return len(videos)


def remove_unused_final_dir(path):
    """
    Remove the completed folder a job made for itself before it was marked as a bonus disc mid-rip.
    check_for_dupe_folder made it (an existing folder gets a new ``_<stage>`` name, or stops the rip), so
    it is never a folder that was there before (the film's own) and holds at most save_disc_poster's
    poster.png: left behind it would never ship (no job has it as its path) and would stop the next rip
    of that title as a duplicate. Called before bonus_folder, which would otherwise take a folder of the
    film's name made by this job for the film's own folder.\n
    :param path: the job's final directory from before the mark
    :return: None
    """
    if os.path.isdir(path):
        logging.info(f"Removing {path} ({', '.join(sorted(os.listdir(path))) or 'empty'}): "
                     "the job's titles are filed as extras instead")
        shutil.rmtree(path, ignore_errors=True)


def remove_empty_dir(path):
    """Remove a folder this job made when nothing was put in it; anything else is left alone"""
    try:
        os.rmdir(path)
        logging.info(f"Removed empty folder {path}")
    except OSError:
        pass


def apply_continuation_settings(job):
    """
    Settings for a disc marked in the ui as the continuation of an earlier job's film
    (utils.continuation_parent): side B of a flipper, or the second disc of a film too long for one.
    The job takes the film's title (a title fixed by hand included), year, type, metadata id and poster;
    it is ripped with its own settings, and file_continuation joins its main title onto the film's\n
    :param job: Current job
    :return: True when the job is a continuation
    """
    parent = utils.continuation_parent(job)
    if parent is None:
        return False
    logging.info(f"Continuation of job {parent.job_id} ({utils.fix_job_title(parent)}, disc {parent.label}): "
                 "its main title is joined onto the film's main feature")
    utils.database_updater({'title': parent.title_manual or parent.title, 'year': parent.year,
                            'imdb_id': parent.imdb_id, 'poster_url': parent.poster_url,
                            'video_type': parent.video_type, 'hasnicetitle': parent.hasnicetitle}, job)
    return True


def continuation_target_folder(job):
    """
    The folder of the film a continuation belongs to: the parent job's own folder (job.path)\n
    :param job: Current job (a continuation)
    :return: path
    """
    parent = utils.continuation_parent(job)
    if parent.path:
        return parent.path
    return os.path.join(job.config.COMPLETED_PATH, utils.convert_job_type(parent.video_type),
                        utils.fix_job_title(parent))


def continuation_feature(job):
    """
    The film's main feature a continuation is joined onto: ``<Title (Year)>.<ext>`` of the parent job in
    its folder (continuation_target_folder), named by the job's title or, for a title fixed after the rip,
    the folder's (less a ``_<stage>`` suffix); None when it is not there: the completed-rip shipper has
    moved it to the media box, or the parent's rip filed no main feature\n
    :param job: Current job (a continuation)
    :return: path or None
    """
    parent = utils.continuation_parent(job)
    folder = continuation_target_folder(job).rstrip(os.sep)
    stems = [utils.fix_job_title(parent), os.path.basename(folder), re.sub(r"_\d+$", "", os.path.basename(folder))]
    for stem in dict.fromkeys(stems):
        for extension in VIDEO_EXTENSIONS:
            feature = os.path.join(folder, stem + extension)
            if os.path.isfile(feature):
                return feature
    return None


def continuation_part_folder(job):
    """``<film folder> - continuation job<id>``: where a continuation is filed when it cannot be joined"""
    return f"{continuation_target_folder(job).rstrip(os.sep)} - continuation job{job.job_id}"


def continuation_folder(job):
    """
    Where a continuation is filed: the film's folder while its main feature is still in the completed
    path (the job's path then holds the folder back from the completed-rip shipper until the join is
    done), else continuation_part_folder. The shipper never ships that folder: the media box would
    show its file as a second version of the film, so it waits on the ripper to be joined by hand\n
    :param job: Current job (a continuation)
    :return: path
    """
    if continuation_feature(job):
        return continuation_target_folder(job)
    return continuation_part_folder(job)


def continuation_main_title(out_path, job, videos):
    """
    The continuation's own main feature among its ripped files, by the rule move_files_post uses for a
    film: the only file, pick_main_feature for a MakeMKV rip, HandBrake's main feature, else the largest\n
    :param out_path: folder holding the ripped files
    :param videos: the video files in it
    :return: file name
    """
    if len(videos) == 1:
        return videos[0]
    tracks = [track for track in job.tracks.filter_by(ripped=True) if track.filename in videos]
    if any(track.source == "MakeMKV" for track in tracks):
        return pick_main_feature(videos, job, out_path)
    main = next((track.filename for track in tracks if track.main_feature), None)
    return main or utils.find_largest_file(videos, out_path)


def join_feature(feature, part):
    """
    Append ``part`` to the film's main feature with ``mkvmerge -o joined feature + part``: one file whose
    chapters run on from the first part's. The output is written beside the feature under a dot name and
    renamed over it once mkvmerge succeeded, so the feature is never left half written; a feature that was
    not Matroska is replaced by the joined ``.mkv``. ``part`` is removed once joined\n
    :param feature: the film's main feature
    :param part: the continuation's main title
    :return: the joined file, or None when it could not be joined (the feature and part are left as they were)
    """
    mkvmerge = shutil.which("mkvmerge")
    if mkvmerge is None:
        logging.error("mkvmerge (mkvtoolnix) is not installed; cannot join the continuation onto the film")
        return None
    folder, name = os.path.split(feature)
    joined = os.path.join(folder, f".{os.path.splitext(name)[0]}.joining.mkv")
    if os.path.exists(joined):
        # Left by a join that never ended (ARM stopped while mkvmerge ran)
        logging.info(f"Removing {joined}, left by an earlier join that did not finish")
        os.remove(joined)
    command = [mkvmerge, "-o", joined, feature, "+", part]
    logging.info(f"Joining {part} onto {feature}: {' '.join(command)}")
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    except OSError as error:
        logging.error(f"Could not run mkvmerge: {error}")
        result = None
    if result is None or result.returncode not in MKVMERGE_OK or not os.path.isfile(joined):
        if result is not None:
            logging.error(f"mkvmerge exited {result.returncode}; the film is left as it was:\n{result.stdout[-2000:]}")
        if os.path.exists(joined):
            os.remove(joined)
        return None
    if result.returncode:
        logging.warning(f"mkvmerge finished with warnings:\n{result.stdout[-2000:]}")
    target = os.path.splitext(feature)[0] + ".mkv"
    os.replace(joined, target)
    if target != feature:
        os.remove(feature)
    os.remove(part)
    logging.info(f"Joined the continuation onto {target} ({os.path.getsize(target)} bytes)")
    return target


def file_continuation(out_path, job, files):
    """
    File a continuation's rip. Its main title (continuation_main_title) is joined onto the film's main
    feature (join_feature) when that is still in the completed path. Otherwise, or when the join fails,
    it is filed as ``<Title (Year)> - pt2.<ext>`` in continuation_part_folder, the job's path from then on,
    and the user is told to join it by hand. Its other titles are extras of the same folder, named
    ``<disc label>_<file>`` like a bonus disc's, unless MAINFEATURE is on or EXTRAS_SUB is none.\n
    The parent job's ``continues`` mark, which holds the film's folder back from the completed-rip
    shipper, is cleared once the film is whole, or once its main feature has left the ripper, unless
    another disc marked as the film's continuation has still to finish (a third disc on another drive).
    After a failed join, or when nothing was ripped, it is kept, so the film waits on the ripper (beside
    the part to be joined by hand) and the user is told\n
    :param out_path: RAW_PATH or TRANSCODE_PATH holding the ripped files
    :param job: Current job (a continuation)
    :param files: os.listdir(out_path)
    :return: the joined feature, or None when the main title was filed as a part
    """
    videos = sorted(name for name in files if name.lower().endswith(VIDEO_EXTENSIONS)
                    and not name.startswith(".") and os.path.isfile(os.path.join(out_path, name)))
    parent = utils.continuation_parent(job)
    if not videos:
        logging.warning(f"Continuation: no video files in {out_path}; nothing to join")
        if parent.continues:
            held = (f"The film's folder is held on the ripper until \"continues\" is cleared on job {parent.job_id}: "
                    "rip the disc again, or clear the mark to ship the film as it is.")
        else:
            held = "Rip the disc again to join it."
        utils.notify(job, constants.NOTIFY_TITLE,
                     f"{utils.fix_job_title(parent)}: no title of the second part was ripped (all shorter than "
                     f"MINLENGTH?), so nothing was joined onto the film. {held}")
        return None
    main = continuation_main_title(out_path, job, videos)
    feature = continuation_feature(job) if job.path == continuation_target_folder(job) else None
    joined = join_feature(feature, os.path.join(out_path, main)) if feature else None
    if joined is None:
        part_folder = continuation_part_folder(job)
        utils.database_updater({'path': part_folder}, job)
        utils.make_dir(part_folder)
        stem = utils.fix_job_title(parent)
        part = os.path.join(part_folder, utils.unused_name(part_folder, f"{stem} - pt2{os.path.splitext(main)[1]}"))
        utils.move_files_main(os.path.join(out_path, main), part, part_folder)
        where = feature or os.path.join(continuation_target_folder(job), f"{stem}.mkv")
        logging.warning(f"Continuation filed as {part}; join it onto the film by hand: "
                        f"mkvmerge -o joined.mkv \"{where}\" + \"{part}\"")
        if feature:
            held = (f"mkvmerge could not join it. The film's folder is held on the ripper until "
                    f"\"continues\" is cleared on job {parent.job_id}")
        else:
            held = "the film's main feature has left the ripper"
        utils.notify(job, constants.NOTIFY_TITLE,
                     f"{stem}: the second part was not joined onto the film: {held}. It is in {part_folder}, "
                     "which is not shipped; join it by hand.")
    for name in videos:
        if name == main:
            continue
        if job.config.MAINFEATURE:
            logging.info(f"MAINFEATURE IS {job.config.MAINFEATURE} - Skipping move of {name}")
        elif str(job.config.EXTRAS_SUB).lower() != "none":
            utils.move_files(out_path, name, job, False)
        else:
            logging.info(f"Not moving extra: \"{name}\" - Sub folder is not set or named incorrectly")
    if joined is not None or feature is None:
        if utils.continuation_pending(parent, job):
            logging.info(f"Job {parent.job_id} stays marked \"continues\": another disc marked as the film's "
                         "continuation has still to finish")
        else:
            utils.database_updater({'continues': False}, parent)
    return joined


def apply_collection_settings(job):
    """
    Settings for a double feature (collection_films: identified by identify.identify_collection or marked in
    the ui): with MAINFEATURE on only one title would be ripped (makemkv rip_mainfeature, or HandBrake's
    main feature), and every other film of the disc would be lost. This job's config gets MAINFEATURE off,
    so every title of at least MINLENGTH is ripped and file_collection can match them to the films; the
    titles no film claims are then left out as MAINFEATURE would have left them\n
    A disc marked as a double feature after the rip started is ripped with the settings it started with\n
    :param job: Current job
    :return: True when MAINFEATURE was lifted, so the rip is to be filed without extras
    """
    films = collection_films(job)
    if job.video_type != "movie" or len(films) < 2 or not job.config.MAINFEATURE:
        return False
    logging.info(f"Disc carries {len(films)} films: ripping every title instead of the main feature only, "
                 "and filing the films without extras")
    utils.database_updater({'MAINFEATURE': False}, job.config)
    return True


def collection_films(job):
    """
    The films a double-feature job was identified as, in disc order; empty for an ordinary job\n
    :param job: Current job
    :return: list of Film
    """
    try:
        films = list(job.films)
    except (AttributeError, TypeError):
        return []
    return sorted(films, key=lambda film: int(film.position or 0))


def expected_length(film, track):
    """
    The film's runtime as this title plays it: a PAL disc (25 fps) runs film material 4% fast, so
    an 84 minute film is an 80.6 minute title there, which is closer to a 78 minute sequel than to
    itself unless corrected for\n
    :param film: a Film with a runtime
    :param track: the ripped track (its fps says whether the disc is PAL)
    :return: seconds
    """
    runtime = int(film.runtime)
    try:
        fps = float(track.fps or 0)
    except (TypeError, ValueError):
        fps = 0.0
    if 24.5 <= fps <= 25.5:
        runtime = round(runtime * 24 / 25)
    return runtime


def match_tracks_to_films(tracks, films):
    """
    Pair the ripped titles with the films of a double feature by runtime\n
    Each title and film is used once; the closest runtimes pair first, and a title has to be within
    RUNTIME_TOLERANCE_FRACTION (at least RUNTIME_TOLERANCE_MIN seconds) of the film's runtime as the
    disc plays it (see expected_length). Films the provider gave no runtime for, and titles without
    a length, are not matched\n
    :param tracks: ripped tracks that have a file
    :param films: the job's films (collection_films)
    :return: dict of film position -> track
    """
    pairs = []
    for film in films:
        if not film.runtime:
            continue
        for track in tracks:
            if not track.length:
                continue
            expected = expected_length(film, track)
            tolerance = max(RUNTIME_TOLERANCE_MIN, expected * RUNTIME_TOLERANCE_FRACTION)
            difference = abs(int(track.length) - expected)
            if difference <= tolerance:
                pairs.append((difference, expected, film, track))
    pairs.sort(key=lambda pair: pair[0])
    matched, used_tracks = {}, set()
    for difference, expected, film, track in pairs:
        if film.position in matched or track.track_number in used_tracks:
            continue
        matched[film.position] = track
        used_tracks.add(track.track_number)
        pal = f" ({expected}s at {track.fps} fps)" if expected != int(film.runtime) else ""
        logging.info(f"Title {track.track_number} ({track.filename}, {track.length}s) is {film.folder_name}: "
                     f"{difference}s off its {film.runtime}s runtime{pal}")
    return matched


def feature_length_titles(job):
    """
    The disc's titles that are long enough to be the film: within MAIN_FEATURE_MIN_FRACTION of the
    longest, so a theatrical and an extended cut both count but the extras do not\n
    :return: list of tracks
    """
    tracks = [track for track in job.tracks if int(track.length or 0) > 0]
    if not tracks:
        return []
    longest = max(int(track.length) for track in tracks)
    return [track for track in tracks if int(track.length) >= longest * MAIN_FEATURE_MIN_FRACTION]


def runtime_fit(runtime, tracks):
    """
    How close the nearest of these titles comes to a film's runtime as the disc plays it (see
    expected_length), when it is within the tolerance match_tracks_to_films uses\n
    :param runtime: the film's runtime in seconds
    :return: the difference in seconds, or None when no title fits
    """
    best = None
    for track in tracks:
        expected = expected_length(SimpleNamespace(runtime=runtime), track)
        tolerance = max(RUNTIME_TOLERANCE_MIN, expected * RUNTIME_TOLERANCE_FRACTION)
        difference = abs(int(track.length) - expected)
        if difference <= tolerance and (best is None or difference < best):
            best = difference
    return best


def check_runtime(job):
    """
    Check the film the disc was identified as against the disc: its runtime has to fit one of the
    feature-length titles. The year of a Blu-ray is when the release was authored, so a search can
    find the wrong film of that title: RED in 2017 is a 15 minute short, not RED (2010), whose 111
    minutes the disc's main title runs. When the runtime does not fit, the other films with the
    disc's title are tried (identify.runtime_candidates) and the one whose runtime fits best is
    taken; when none fits, the title is kept and the user is told it may be wrong.

    A runtime more than RUNTIME_TOLERANCE_MIN off but within the tolerance only loosely fits: RED 2
    (116 min) once became "High & Low The Red Rain" (110 min), which is 6 minutes short of the disc
    and inside the 6%. Then the other films are tried too, and one is only taken when it fits
    closely (within RUNTIME_TOLERANCE_MIN); otherwise the identification stands without a word.
    A film the provider has no runtime for is treated the same way: a typical wrong pick is a
    little-known film nobody entered a runtime for, but a right one without a runtime must not give
    way to a film that only loosely fits. When the runtime could not be looked up at all (network
    error, rate limit), the identification is left alone.

    When another film is taken, the user is notified. Left alone: titles set by hand, series,
    double features (file_collection matches those by runtime), and rips without title lengths:
    only the mkv rip method lists the titles (makemkv.get_track_info); a Blu-ray ripped with
    RIPMETHOD backup is not checked, which is logged as a warning. Nor are the discs runtime_check_skipped
    names: a bonus disc, and either part of a film split over two sides or discs\n
    :return: True when the job was given another film
    """
    if runtime_check_skipped(job):
        return False
    if (not job.config.GET_VIDEO_TITLE or job.title_manual or job.video_type != "movie"
            or not job.imdb_id or collection_films(job)):
        return False
    tracks = feature_length_titles(job)
    if not tracks:
        logging.warning(f"MakeMKV listed no title lengths for {job.title} ({job.year}), as with RIPMETHOD backup; "
                        "the identification is not checked against the disc's runtime")
        return False
    try:
        runtime = identify.identified_runtime(job)
    except identify.RuntimeLookupError as error:
        logging.warning(f"Could not look up the runtime of {job.title} ({job.year}): {error}; not checked")
        return False
    main_title = max(int(track.length) for track in tracks)
    current = runtime_fit(runtime, tracks) if runtime else None
    if current is not None and current <= RUNTIME_TOLERANCE_MIN:
        logging.info(f"{job.title} ({job.year}) runs {runtime // 60} min, which fits the disc "
                     f"(main title {main_title // 60} min)")
        return False
    if not runtime:
        logging.info(f"No runtime known for {job.title} ({job.year}); looking for a film of that title "
                     f"that runs as long as the disc's main title ({main_title // 60} min)")
        limit = RUNTIME_TOLERANCE_MIN
    elif current is None:
        logging.warning(f"{job.title} ({job.year}) runs {runtime // 60} min but the disc's main title runs "
                        f"{main_title // 60} min; looking for a film of that title that fits")
        limit = None
    else:
        logging.info(f"{job.title} ({job.year}) runs {runtime // 60} min, {current}s off the disc's titles; "
                     "looking for a film of that title that fits better")
        limit = min(current, RUNTIME_TOLERANCE_MIN)
    best = best_runtime_candidate(job, tracks, limit)
    if best is None:
        if runtime and current is None:
            utils.notify(job, constants.NOTIFY_TITLE,
                         f"{job.title} ({job.year}) runs {runtime // 60} min, but the disc's main title runs "
                         f"{main_title // 60} min. The title may be wrong; check the completed folder.")
        return False
    identified = f"{job.title} ({job.year})"
    # A re-rip has the earlier job's title and year but not its IMDb id: then only the id changes
    renamed = f"{utils.clean_for_filename(best['Title'])} ({best['Year']})" != identified
    logging.info(f"Identifying the disc as {best['Title']} ({best['Year']}) instead of {identified}")
    identify.apply_search_result(job, best)
    if renamed:
        utils.notify(job, constants.NOTIFY_TITLE,
                     f"Identified as {best['Title']} ({best['Year']}) instead of {identified} by runtime")
    return True


def runtime_check_skipped(job):
    """
    True (and logged) for a disc whose titles the film's runtime is not to be checked against, since it
    never fits them and the search for a film that does could rename the job: a bonus disc, which carries
    the film's IMDb id but whose titles are the extras; a continuation, whose main title is the rest of
    the film; and the first part of a split film (``continues``, marked before its rip ended), whose main
    title is only part of it. The parts are not checked joined either: a provider's runtime is often the
    theatrical cut's, and a film split over two sides is typically a longer one (Das Boot's 209 minute
    director's cut against the 149 minutes given for the film)\n
    :param job: Current job
    :return: bool
    """
    if utils.is_bonus_disc(job):
        logging.info(f"Bonus disc of job {job.bonus_of_job_id}: its titles are extras, so the runtime of "
                     f"{job.title} ({job.year}) is not checked against them")
        return True
    if utils.is_continuation(job):
        logging.info(f"Continuation of job {job.continuation_of_job_id}: its main title is the rest of "
                     f"{job.title} ({job.year}), so the film's runtime is not checked against it")
        return True
    if getattr(job, "continues", None):
        logging.info(f"{job.title} ({job.year}) continues on another side or disc: its main title is only part "
                     "of the film, so the film's runtime is not checked against it")
        return True
    return False


def best_runtime_candidate(job, tracks, limit=None):
    """
    The film with the disc's title whose runtime fits the feature-length titles best (the most
    voted for of equally close ones), of at most RUNTIME_CHECK_MAX_CANDIDATES
    (identify.runtime_candidates)\n
    :param tracks: the feature-length titles
    :param limit: when given, a film has to come within this many seconds of a title to count
    :return: a search result in OMDB style, or None when none fits
    """
    fits = []
    for candidate in identify.runtime_candidates(job)[:RUNTIME_CHECK_MAX_CANDIDATES]:
        candidate_runtime = identify.film_runtime(candidate)
        difference = runtime_fit(candidate_runtime, tracks) if candidate_runtime else None
        if limit is not None and difference is not None and difference > limit:
            difference = None
        logging.info(f"  {candidate['Title']} ({candidate['Year']}): "
                     f"{candidate_runtime // 60 if candidate_runtime else 'unknown'} min"
                     f"{'' if difference is None else ', fits'}")
        if difference is not None:
            fits.append((difference, -identify.vote_count(candidate), candidate))
    if not fits:
        return None
    return min(fits, key=lambda fit: fit[:2])[2]


def file_film(out_path, filename, film, job):
    """
    Move a ripped title into its own ``Title (Year)`` folder as that film's main feature\n
    :param out_path: folder holding the ripped file
    :param filename: the ripped file
    :param film: the Film it is
    :param job: Current job (for the completed path, the extension and permissions)
    :return: the film's folder
    """
    type_sub_folder = utils.convert_job_type(job.video_type)
    film_path = os.path.join(job.config.COMPLETED_PATH, type_sub_folder, film.folder_name)
    if utils.make_dir(film_path) is False:
        logging.info(f"Output directory \"{film_path}\" already exists.")
        film_path = film_path + "_" + str(job.stage)
        utils.make_dir(film_path)
    film_file = os.path.join(film_path, f"{film.folder_name}.{job.config.DEST_EXT}")
    logging.info(f"Track is the main title of {film.folder_name}. Moving '{os.path.join(out_path, filename)}' "
                 f"to {film_file}")
    utils.move_files_main(os.path.join(out_path, filename), film_file, film_path)
    utils.set_permissions(film_path)
    return film_path


def file_collection(out_path, job, files, main_features_only=False):
    """
    File the rip of a disc that carries several films (identify.identify_collection)\n
    Each ripped title whose runtime matches a film becomes that film's main feature: the first film's
    in the job folder, every other film's in its own ``Title (Year)`` folder under the same type
    folder. When the runtimes cannot place the first film, the usual main-feature rule
    (pick_main_feature) chooses it from the titles left over, so a rip is never worse than a single
    film's. Titles no film claims are extras of the job folder, as for a single film\n
    :param out_path: RAW_PATH or TRANSCODE_PATH holding the ripped files
    :param job: Current job
    :param files: os.listdir(out_path)
    :param main_features_only: file no extras (apply_collection_settings lifted MAINFEATURE for the rip)
    :return: True when the job is a collection and was filed here, False to file it the usual way
    """
    films = collection_films(job)
    if job.video_type != "movie" or len(films) < 2:
        return False
    present = set(files)
    ripped = [track for track in job.tracks.filter_by(ripped=True) if track.filename in present]
    logging.info(f"Disc carries {len(films)} films: " + ", ".join(film.folder_name for film in films))
    matched = match_tracks_to_films(ripped, films)
    leftover = [track for track in ripped if track.track_number not in
                {track.track_number for track in matched.values()}]
    first = films[0]
    if first.position not in matched and leftover:
        chosen = pick_main_feature([track.filename for track in leftover], job, out_path)
        matched[first.position] = next(track for track in leftover if track.filename == chosen)
        leftover = [track for track in leftover if track.filename != chosen]
        logging.info(f"No ripped title matched the runtime of {first.folder_name}; filing title "
                     f"{matched[first.position].track_number} as it by the main-feature rule")
    for film in films:
        track = matched.get(film.position)
        if track is None:
            logging.warning(f"No ripped title matched the runtime of {film.folder_name} "
                            f"({film.runtime or 'unknown'}s); it is not filed")
            continue
        if film.position == first.position:
            utils.move_files(out_path, track.filename, job, True)
        else:
            file_film(out_path, track.filename, film, job)
        utils.database_updater({'track_number': track.track_number}, film)
    for track in leftover:
        if job.config.MAINFEATURE or main_features_only:
            logging.info(f"MAINFEATURE IS {job.config.MAINFEATURE or 'lifted'} - Skipping move of {track.filename}")
        elif str(job.config.EXTRAS_SUB).lower() != "none":
            utils.move_files(out_path, track.filename, job, False)
        else:
            logging.info(f"Not moving extra: \"{track.filename}\" - Sub folder is not set or named incorrectly")
    return True


def rip_with_mkv(current_job, protection=0):
    """
    Test to check if title was or should be ripped by MakeMKV\n
    :param current_job: current job
    :param protection: If the disc have 99 track protection
    :return: Bool
    """
    mkv_ripped = False
    # Rip bluray
    if current_job.disctype == "bluray":
        mkv_ripped = True
    # Rip dvd with mode: mkv and mainfeature: false
    if current_job.disctype == "dvd" and (not current_job.config.MAINFEATURE and current_job.config.RIPMETHOD == "mkv"):
        mkv_ripped = True
    # Rip dvds with skip transcode
    if current_job.disctype == "dvd" and current_job.config.SKIP_TRANSCODE:
        mkv_ripped = True
    # If dvd has 99 protection force MakeMKV to be used
    if protection and current_job.disctype == "dvd":
        mkv_ripped = True
    # if backup_dvd, always use mkv
    if current_job.config.RIPMETHOD == "backup_dvd":
        mkv_ripped = True
    return mkv_ripped


def _file_size(raw_path, filename, fallback=0):
    """
    Size of a ripped file on disk, or the given fallback (MakeMKV's estimate) when it cannot be read\n
    :param raw_path: RAW_PATH of ripped mkv files
    :param filename: file name inside raw_path
    :param fallback: size to report when the file cannot be read
    :return: int size in bytes
    """
    try:
        return os.stat(os.path.join(raw_path, filename)).st_size
    except OSError:
        return int(fallback or 0)


def _track_sort_key(raw_path):
    """
    Sort key for picking the main feature: most chapters, then largest file, then lowest track number.
    This is the ordering MakeMKV's MAINFEATURE path and find_duplicate_titles use\n
    :param raw_path: RAW_PATH of ripped mkv files (sizes are read from disk when possible)
    :return: key function for sorted()
    """
    def key(track):
        try:
            number = int(track.track_number)
        except (TypeError, ValueError):
            number = 9999
        return (-int(track.chapters or 0), -_file_size(raw_path, track.filename, track.filesize), number)
    return key


def _feature_length_tracks(candidates, raw_path):
    """
    Drop the ripped tracks that are too short to be the main feature\n
    A track must reach MAIN_FEATURE_MIN_FRACTION of the longest track's runtime; when a runtime is
    missing for any track, file size is compared instead. Keeps a bonus reel with one chapter per
    clip from beating the film on the chapter count\n
    :param candidates: ripped tracks that have a file in raw_path
    :param raw_path: RAW_PATH of ripped mkv files
    :return: the tracks long enough to be the main feature
    """
    if len(candidates) < 2:
        return candidates
    if all(int(track.length or 0) > 0 for track in candidates):
        measure, unit = "runtime", "s"

        def value(track):
            return int(track.length)
    else:
        measure, unit = "size", " bytes"

        def value(track):
            return _file_size(raw_path, track.filename, track.filesize)
    longest = max(value(track) for track in candidates)
    threshold = longest * MAIN_FEATURE_MIN_FRACTION
    kept = []
    for track in candidates:
        if value(track) >= threshold:
            kept.append(track)
        else:
            logging.info(f"Title {track.track_number} ({track.filename}) is too short to be the main feature: "
                         f"{measure} {value(track)}{unit} is under {MAIN_FEATURE_MIN_FRACTION:.0%} of the "
                         f"longest title ({longest}{unit}) - treating it as an extra")
    return kept


def pick_main_feature(files, job, raw_path):
    """
    Choose the main feature among the files MakeMKV ripped\n
    Only tracks that are feature-length count (see _feature_length_tracks). Among them prefers the
    one with the most chapters, then the largest file, then the lowest track number. Falls back to
    the largest file in raw_path when none of the feature-length tracks carry chapter information
    (HandBrake-sourced tracks, or MakeMKV reporting zero chapters everywhere)\n
    :param files: os.listdir(RAW_PATH)
    :param job: Current job
    :param raw_path: RAW_PATH of ripped mkv files (mkvoutpath)
    :return: file name of the main feature
    """
    present = set(files)
    ripped = [track for track in job.tracks.filter_by(ripped=True) if track.filename in present]
    candidates = _feature_length_tracks(ripped, raw_path)
    if not any(int(track.chapters or 0) > 0 for track in candidates):
        largest_file_name = utils.find_largest_file(files, raw_path)
        logging.info(f"No chapter info on the feature-length ripped tracks ({len(candidates)} of {len(ripped)} "
                     f"with files) - main feature is the largest file: {largest_file_name}")
        return largest_file_name
    ranked = sorted(candidates, key=_track_sort_key(raw_path))
    chosen = ranked[0]
    chosen_size = _file_size(raw_path, chosen.filename, chosen.filesize)
    reason = "most chapters"
    if len(ranked) > 1:
        runner_up = ranked[1]
        if int(runner_up.chapters or 0) == int(chosen.chapters or 0):
            reason = f"tied on chapters with title {runner_up.track_number}, larger file"
            if _file_size(raw_path, runner_up.filename, runner_up.filesize) == chosen_size:
                reason = f"tied on chapters and size with title {runner_up.track_number}, lower title number"
    logging.info(f"Main feature is title {chosen.track_number} ({chosen.filename}): {chosen.chapters} chapters, "
                 f"{chosen_size} bytes - chosen for {reason}")
    for track in ranked[1:]:
        logging.debug(f"Not the main feature: title {track.track_number} ({track.filename}) - "
                      f"{track.chapters} chapters, {_file_size(raw_path, track.filename, track.filesize)} bytes")
    return chosen.filename


def skip_transcode_movie(files, job, raw_path):
    """
    Only ran if job is a movie - pick the main feature (see pick_main_feature)\n
    Move everything else to extras folder\n
    If mainfeature is enabled skip moving everything but the main file\n
    :param files: os.listdir(RAW_PATH)
    :param job: Current job
    :param raw_path: RAW_PATH of ripped mkv files (mkvoutpath)
    :return: None
    """
    logging.debug(f"Videotype: {job.video_type}")
    # if video_type is movie, then move the main feature to media_dir
    # move the rest of the files to the extras' folder
    main_feature_name = pick_main_feature(files, job, raw_path)
    temp_path = os.path.join(raw_path, main_feature_name)
    if os.stat(temp_path).st_size <= 1:  # sanity check for filesize
        logging.info(f"{raw_path} is empty or very small size. - Folder size: {os.stat(temp_path).st_size}")
    for file in files:
        # move main into main folder
        # move others into extras folder
        if file == main_feature_name:
            # main feature
            utils.move_files(raw_path, file, job, True)
        else:
            # If mainfeature is enabled - skip to the next file
            if job.config.MAINFEATURE:
                logging.info(f"MAINFEATURE IS {job.config.MAINFEATURE} - Skipping move of {file}")
                continue
            # Other/extras
            if str(job.config.EXTRAS_SUB).lower() != "none":
                utils.move_files(raw_path, file, job, False)
            else:
                logging.info(f"Not moving extra: \"{file}\" - Sub folder is not set or named incorrectly")
