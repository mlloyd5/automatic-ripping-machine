"""
ARM route blueprint for sleeve labels and boxes
Covers
- boxes [GET]               file sleeves into a box, find a disc, list every box
- boxes/file [POST]         scan or type a sleeve id to file it in the box named on the page
- boxes/set [POST]          set the box of one job from its detail page
- boxes/print [POST]        print (or reprint) a job's sleeve label
- boxes/preview.png [GET]   the label as it would print, for checking the layout
- boxes/export.csv [GET]    every labelled disc with its box, as CSV

The sleeve id on a label is only a short code (S0042); the USB scanner types it followed by Enter,
so every form here submits on Enter and the sleeve id field keeps the focus.
"""
import csv
import io
from collections import OrderedDict

from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, abort, \
    make_response
from flask_login import login_required
from markupsafe import escape
from sqlalchemy import or_

from arm.ui import app
from arm.models.job import Job
from arm.labels import sleeve, render

route_boxes = Blueprint('route_boxes', __name__,
                        template_folder='templates',
                        static_folder='../static')

UNFILED = ""
"""Box key for labelled sleeves that are not in a box yet."""


def _job_or_404(job_id):
    try:
        job = Job.query.get(int(job_id))
    except (TypeError, ValueError):
        job = None
    if job is None:
        abort(404, f"No job {job_id}")
    return job


def labelled_jobs():
    """Every job with a sleeve id, by sleeve id."""
    return Job.query.filter(Job.sleeve_id.isnot(None)).order_by(Job.sleeve_id.asc()).all()


def group_by_box(jobs):
    """``{"Box A": [jobs...], "": [unfiled jobs...]}`` with the boxes in name order and unfiled last."""
    groups = {}
    for job in jobs:
        groups.setdefault(job.box or UNFILED, []).append(job)
    ordered = OrderedDict(sorted(((name, jobs) for name, jobs in groups.items() if name != UNFILED),
                                 key=lambda item: item[0].lower()))
    if UNFILED in groups:
        ordered[UNFILED] = groups[UNFILED]
    return ordered


def search_jobs(query):
    """Jobs matching a sleeve id (any spelling) or a title fragment; the exact sleeve id match comes first."""
    query = (query or "").strip()
    if not query:
        return []
    results = []
    exact = sleeve.find_by_sleeve_id(query)
    if exact is not None:
        results.append(exact)
    like = f"%{query}%"
    for job in Job.query.filter(or_(Job.title.ilike(like), Job.label.ilike(like), Job.box.ilike(like),
                                    Job.sleeve_id.ilike(like))).order_by(Job.job_id.desc()).limit(50).all():
        if job not in results:
            results.append(job)
    return results


@route_boxes.route('/boxes')
@login_required
def boxes():
    box = (request.args.get('box') or "").strip()[:sleeve.MAX_BOX_LENGTH]
    query = (request.args.get('q') or "").strip()
    jobs = labelled_jobs()
    return render_template('boxes/boxes.html',
                           box=box,
                           query=query,
                           results=search_jobs(query) if query else None,
                           groups=group_by_box(jobs),
                           labelled=len(jobs),
                           unfiled=sum(1 for job in jobs if not job.box),
                           enabled=sleeve.enabled(),
                           disc_type_name=sleeve.disc_type_name,
                           printer_ok=sleeve.printer.tool_available(
                               str(sleeve.setting("LABEL_PRINTER_CMD", sleeve.printer.DEFAULT_COMMAND))))


@route_boxes.route('/boxes/file', methods=['POST'])
@login_required
def file_sleeve():
    """Scan a sleeve: put the job it belongs to into the box named on the page."""
    box = (request.form.get('box') or "").strip()[:sleeve.MAX_BOX_LENGTH]
    code = (request.form.get('sleeve_id') or "").strip()
    if not box:
        flash("Name the box first, then scan the sleeves going into it.", "danger")
    elif not code:
        flash("Scan or type a sleeve id.", "danger")
    else:
        job = sleeve.find_by_sleeve_id(code)
        if job is None:
            flash(f"No disc has the sleeve id {escape(code)}.", "danger")
        else:
            try:
                sleeve.set_box(job, box)
                flash(f"{job.sleeve_id} {escape(job.title or job.label or '')} filed in {escape(box)}.", "success")
            except RuntimeError as error:
                app.logger.error(f"Could not file {code}: {error}")
                flash(f"Could not save: {error}", "danger")
    return redirect(url_for('route_boxes.boxes', box=box))


@route_boxes.route('/boxes/set', methods=['POST'])
@login_required
def set_box():
    """Set (or clear) the box of one job from its detail page."""
    job = _job_or_404(request.form.get('job_id'))
    try:
        stored = sleeve.set_box(job, request.form.get('box'))
        flash(f"Box set to {escape(stored)}." if stored else "Box cleared.", "success")
    except RuntimeError as error:
        app.logger.error(f"Could not set box for job {job.job_id}: {error}")
        flash(f"Could not save: {error}", "danger")
    return redirect(request.form.get('next') or url_for('route_jobs.jobdetail', job_id=job.job_id))


@route_boxes.route('/boxes/print', methods=['POST'])
@login_required
def print_label():
    """Print or reprint a job's sleeve label."""
    job = _job_or_404(request.form.get('job_id'))
    printed, message = sleeve.print_label(job, reason="web UI")
    flash(escape(message), "success" if printed else "danger")
    return redirect(request.form.get('next') or url_for('route_jobs.jobdetail', job_id=job.job_id))


@route_boxes.route('/boxes/preview.png')
@login_required
def preview():
    """The label as it would print. Assigns the sleeve id if the job has none (it is printed either way)."""
    job = _job_or_404(request.args.get('job_id'))
    try:
        image = sleeve.render_job_label(job)
    except ImportError as error:
        abort(503, f"Label libraries missing: {error}")
    except (ValueError, RuntimeError) as error:
        abort(500, str(error))
    buffer = io.BytesIO()
    render.save_png(image, buffer)
    buffer.seek(0)
    response = make_response(send_file(buffer, mimetype='image/png', download_name=f"{job.sleeve_id}.png"))
    response.headers['Cache-Control'] = 'no-store'
    return response


@route_boxes.route('/boxes/export.csv')
@login_required
def export_csv():
    """Every labelled disc with its box, for a spreadsheet or a backup of the index."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["sleeve_id", "title", "year", "disc_type", "box", "job_id", "ripped", "disc_barcode", "path"])
    for job in labelled_jobs():
        writer.writerow([job.sleeve_id, job.title or job.label or "", job.year or "", sleeve.disc_type_name(job),
                         job.box or "", job.job_id,
                         job.start_time.strftime("%Y-%m-%d") if job.start_time else "",
                         job.disc_barcode or "", job.path or ""])
    response = make_response(buffer.getvalue())
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    response.headers['Content-Disposition'] = 'attachment; filename=arm-sleeves.csv'
    return response


@route_boxes.app_template_filter('box_count')
def box_count(groups):
    """Number of named boxes in a :func:`group_by_box` result."""
    return sum(1 for name in groups if name != UNFILED)
