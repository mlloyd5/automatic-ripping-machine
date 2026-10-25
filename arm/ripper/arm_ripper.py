""" Main file for running DVDs/Blu-rays/CDs/data ?
It would help clear up main and make things easier to find
"""
import sys
import os
import logging
from importlib.util import find_spec
from pathlib import Path

# If the arm module can't be found, add the folder this file is in to PYTHONPATH
# This is a bad workaround for non-existent packaging
if find_spec("arm") is None:
    sys.path.append(str(Path(__file__).parents[2]))

from arm.ripper import utils, makemkv, handbrake, ffmpeg  # noqa E402
from arm.ui import app, db, constants  # noqa E402
from arm.models.job import JobState  # noqa E402


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
    # Fix the sub-folder type - (movie|tv|unknown)
    type_sub_folder = utils.convert_job_type(job.video_type)
    # Fix the job title - Title (Year) | Title
    job_title = utils.fix_job_title(job)

    # We need to check/construct the final path, and the transcode path
    transcode_out_path = os.path.join(job.config.TRANSCODE_PATH, type_sub_folder, job_title)
    final_directory = os.path.join(job.config.COMPLETED_PATH, type_sub_folder, job_title)

    # Check folders for already ripped jobs -> creates folder
    transcode_out_path = utils.check_for_dupe_folder(have_dupes, transcode_out_path, job)
    # If dupes rips is disabled this might kill the run
    final_directory = utils.check_for_dupe_folder(have_dupes, final_directory, job)

    # Update the job.path with the final directory
    utils.database_updater({'path': final_directory}, job)
    # Save poster image from disc if enabled
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

    # Update final path if user has set a custom/manual title
    logging.debug(f"Job title status: [{job.title_manual}]")
    if job.title_manual:
        # Remove the old final dir
        utils.delete_raw_files([final_directory])
        job_title = utils.fix_job_title(job)
        final_directory = os.path.join(job.config.COMPLETED_PATH, type_sub_folder, job_title)
        # Update the job.path with the final directory
        utils.database_updater({'path': final_directory}, job)

    # Move to final folder
    move_files_post(transcode_out_path, job)
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
    # report errors if any
    notify_exit(job)
    logging.info("************* ARM processing complete *************")


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


def move_files_post(transcode_out_path, job):
    """
    Logic for moving files post transcoding\n
    if series move all to 1 folder\n
    if movie check what source we got them from, for MakeMKV use skip_transcode_movie to pick the main feature\n
    :param transcode_out_path: This should either be the RAW_PATH from MakeMKV, /dev/srX or TRANSCODE_PATH
    :param job: current job
    :return: None
    """
    tracks = job.tracks.filter_by(ripped=True)  # .order_by(job.tracks.length.desc())
    if job.video_type == "series":
        for track in tracks:
            utils.move_files(transcode_out_path, track.filename, job, False)
    else:
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


def pick_main_feature(files, job, raw_path):
    """
    Choose the main feature among the files MakeMKV ripped\n
    Prefers the ripped track with the most chapters, then the largest file, then the lowest track
    number. Falls back to the largest file in raw_path when none of the job's ripped tracks carry
    chapter information (HandBrake-sourced tracks, or MakeMKV reporting zero chapters everywhere)\n
    :param files: os.listdir(RAW_PATH)
    :param job: Current job
    :param raw_path: RAW_PATH of ripped mkv files (mkvoutpath)
    :return: file name of the main feature
    """
    present = set(files)
    candidates = [track for track in job.tracks.filter_by(ripped=True) if track.filename in present]
    if not any(int(track.chapters or 0) > 0 for track in candidates):
        largest_file_name = utils.find_largest_file(files, raw_path)
        logging.info(f"No chapter info on the ripped tracks ({len(candidates)} with files) - "
                     f"main feature is the largest file: {largest_file_name}")
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
