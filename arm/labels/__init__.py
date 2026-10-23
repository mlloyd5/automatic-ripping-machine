"""
Sleeve labels: print a label for each ripped disc on a Brother P-touch tape printer and track which
box the sleeve was filed in.

- ``render``  draws the label bitmap (QR code of the sleeve id, the id in large text, title, year, disc
  type). Pure Pillow, no ARM imports, so it is testable anywhere.
- ``printer`` hands a bitmap to the ``ptouch`` command line tool (ptouch-rs) or any command that
  takes an image file. No ARM imports either.
- ``sleeve``  ties them to ARM: assigns sleeve ids, prints for a job, backfills old jobs.
"""
