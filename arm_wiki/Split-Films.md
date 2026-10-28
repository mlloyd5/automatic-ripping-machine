# Split Films

Some films do not fit on one side of a disc. A "flipper" DVD carries the film on both sides (the
director's cut of Das Boot: 108 minutes on side A, the other 101 on side B), and a long film can come
on two discs. Left to itself A.R.M. rips each side as a film of its own:

- Side B is identified as the same film (or not at all: both sides are often labelled `DVD_VIDEO`),
  its main title is named `<Title (Year)>.mkv`, and it is filed as a second copy, which the
  [completed-rip shipper](https://github.com/automatic-ripping-machine/automatic-ripping-machine/tree/main/scripts/completed_shipper)
  ships as `<Title (Year)> - <disc label>` beside the first. Plex shows the two halves as two
  versions of the film.
- Neither side's main title fits the film's runtime, so the runtime check may take either side for
  another film of the same title that runs about as long, and rename it.

Marking the second side as the **continuation** of the first side's job joins the two into one
`<Title (Year)>.mkv`, with the chapters running on.

## Marking the discs

Click **Split Film** on the disc's card on the home page, or on its job page. The Custom Title and
Title Search pages link there too.

**Side B (the continuation).** While the disc waits for manual identification (`MANUAL_WAIT`), or
while it rips, pick the job of side A from the list (the latest successful DVD and Blu-ray rips that
are not series, bonus discs or continuations, newest first) and save. The job takes side A's title,
year and metadata id, a title fixed by hand included. **Not a continuation** clears the choice.

**Side A (optional, but recommended).** If you know the disc is a flipper when it goes in, press
**Continues on another side or disc** on side A's page while it rips. Its main title is then not
checked against the film's runtime, and the completed-rip shipper keeps its folder on the ripper
until side B has been joined onto it. Marking side B marks side A too, but only from then on: by
then the shipper (every 15 minutes) may have moved side A to the media server, and side A's runtime
check has run.

The choices are stored on the jobs: `continuation_of_job_id` on side B, `continues` on side A.

## What the ripper does with it

Side B is ripped with its own settings, like any film. Its main title is picked as for a film (the
title with the most chapters among the feature-length ones, or HandBrake's main feature) and
appended to side A's main feature:

```
mkvmerge -o ".<Title (Year)>.joining.mkv" "<Title (Year)>.mkv" + title_t00.mkv
```

The output is written beside side A's file under a dot name and renamed over it once `mkvmerge`
succeeded (exit 0, or 1 for warnings), so the film is never left half written. mkvmerge runs side B's
chapters on from the end of side A's in one edition. A feature that was not Matroska (`DEST_EXT: mp4`)
is replaced by the joined `.mkv`. mkvtoolnix is in the A.R.M. Docker image; a native install needs
the `mkvtoolnix` package.

Side B's other titles are side A's film's extras, named `<disc label>_<title file>` like a
[bonus disc's](Bonus-Discs), so they never replace side A's extras of the same name (unless
`MAINFEATURE` is on, or `EXTRAS_SUB` is `None`). Its disc photo is copied as
`disc-continuation-<disc label>.jpg`; its poster and case barcode are not copied.

No `completed/movies/<Title (Year)>` folder is made for side B. Once the film is whole, side A's
`continues` mark is cleared, and the shipper takes the folder on its next run.

A third disc is the continuation of the first disc's job too, ripped after the second has been
joined. Mark the first disc **continues** again before the second one finishes, or the shipper may
take the film in between.

### When the two cannot be joined

- **Side A has left the ripper** (shipped, or moved by hand): side B's main title is filed as
  `<Title (Year)> - pt2.mkv` in `<side A's folder> - continuation job<id>`, and a notification says
  so. The completed-rip shipper never ships that folder: on the media server it would be the second
  version again. Join the two by hand where side A is now and delete the folder.
- **mkvmerge failed** (the two sides' tracks do not match, for example): the same, but side A stays
  in `COMPLETED_PATH`, held back from the shipper, so both parts are side by side on the ripper:

  ```
  mkvmerge -o joined.mkv "<Title (Year)>.mkv" + "<Title (Year)> - continuation job<id>/<Title (Year)> - pt2.mkv"
  ```

  Replace `<Title (Year)>.mkv` with `joined.mkv`, delete the continuation folder, and clear the mark
  with **Clear the mark** on side A's Split Film page.

## The runtime check

After a MakeMKV rip, A.R.M. compares the film's runtime from the metadata provider with the disc's
feature-length titles, and looks for another film of the same title when it does not fit (a Blu-ray's
year is when it was authored, so a search can find the wrong film of that title). It is skipped
for a continuation and for a job marked `continues`. The joined film is not checked either: the
provider's runtime is often the theatrical cut's (149 minutes for Das Boot), and a film split over
two sides is typically a longer cut. A side A that was not marked in time can still be renamed by
the check: fix its title by hand (Custom Title) before marking side B.
