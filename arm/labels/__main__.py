"""
Command line for sleeve labels, run from the ARM install with its virtualenv:

    python3 -m arm.labels pending                       list the labels waiting to be printed
    python3 -m arm.labels print --pending               print them, oldest first (stops at the first failure)
    python3 -m arm.labels print JOB_ID [JOB_ID ...]     print or reprint the labels of these jobs
    python3 -m arm.labels backfill [--no-print] [--dry-run] [--include-failed]
                                                        give every finished video rip a sleeve id, then print the queue
    python3 -m arm.labels preview JOB_ID out.png        render only, for checking the layout
    python3 -m arm.labels test out.png                  render a sample label without a job
"""
import argparse
import logging
import sys


def _report(entries):
    for entry in entries:
        print(f"job {entry['job_id']:>5}  {entry['sleeve_id'] or '-':<9} {entry['message']}  {entry['title']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m arm.labels", description="ARM sleeve labels")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pending", help="list the labels waiting to be printed")
    prt = sub.add_parser("print", help="print labels: the queue with --pending, or the given jobs")
    prt.add_argument("job_ids", type=int, nargs="*")
    prt.add_argument("--pending", action="store_true", help="print every queued label, oldest first")
    prt.add_argument("--keep-going", action="store_true", help="with --pending: try every label even after a failure")
    back = sub.add_parser("backfill", help="assign sleeve ids to finished video jobs without one, then print the queue")
    back.add_argument("--no-print", action="store_true", help="assign ids only; they wait in the queue")
    back.add_argument("--dry-run", action="store_true", help="show what would be assigned, change nothing")
    back.add_argument("--include-failed", action="store_true", help="also jobs that did not finish successfully")
    prev = sub.add_parser("preview", help="render a job's label to a PNG without printing")
    prev.add_argument("job_id", type=int)
    prev.add_argument("output")
    test = sub.add_parser("test", help="render a sample label to a PNG, no database needed")
    test.add_argument("output")
    test.add_argument("--tape", type=int, default=None, help="tape width in mm (default LABEL_TAPE_MM)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    from arm.labels import render, sleeve
    if args.command == "test":
        tape = args.tape or int(sleeve.setting("LABEL_TAPE_MM", 18))
        lines = render.label_text_lines("The Return of the King", "2003", "Blu-ray")
        image = render.render_label(sleeve.format_sleeve_id(sleeve.id_prefix(), 42), lines, tape_mm=tape,
                                    font_path=sleeve.setting("LABEL_FONT", "") or None)
        render.save_png(image, args.output)
        print(f"wrote {args.output} ({render.label_size_mm(image)} mm, font {render.find_font()})")
        return 0

    from arm.models.job import Job
    if args.command == "pending":
        jobs = sleeve.pending_jobs()
        for job in jobs:
            print(f"job {job.job_id:>5}  {job.sleeve_id:<9} {job.title or job.label or ''}")
        print(f"{len(jobs)} label(s) waiting; this machine's prefix is {sleeve.id_prefix()}")
        return 0
    if args.command == "backfill":
        results = sleeve.backfill(print_labels=not args.no_print, dry_run=args.dry_run,
                                  include_failed=args.include_failed)
        _report(results)
        print(f"{len(results)} job(s) given a sleeve id")
        return 1 if any(not r["printed"] and not args.no_print and not args.dry_run for r in results) else 0
    if args.command == "preview":
        job = Job.query.get(args.job_id)
        if job is None:
            print(f"no job {args.job_id}", file=sys.stderr)
            return 1
        image = sleeve.render_job_label(job)
        render.save_png(image, args.output)
        print(f"wrote {args.output} for {job.sleeve_id} ({render.label_size_mm(image)} mm)")
        return 0
    if args.pending:
        results = sleeve.print_pending(stop_on_error=not args.keep_going)
        _report(results)
        left = len(sleeve.pending_jobs())
        print(f"{sum(1 for r in results if r['printed'])} printed, {left} still pending")
        return 1 if left else 0
    if not args.job_ids:
        parser.error("give job ids or --pending")
    failures = 0
    for job_id in args.job_ids:
        job = Job.query.get(job_id)
        if job is None:
            print(f"no job {job_id}", file=sys.stderr)
            failures += 1
            continue
        printed, message = sleeve.print_label(job, reason="command line")
        print(f"job {job_id}: {message}")
        failures += 0 if printed else 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
