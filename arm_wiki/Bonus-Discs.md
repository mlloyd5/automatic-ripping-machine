# Bonus Discs

Many films come with a second disc of extras: a "bonus disc" or "supplemental disc" in a special
edition or box set. Left to itself A.R.M. treats it as a film of its own, and gets it wrong in
several ways at once:

- The disc is often labelled like the film disc (`Monsters,_Inc.` on both), or the case barcode
  names the film, so it is identified as the film.
- Most featurettes are shorter than `MINLENGTH` (600 s by default), so they are not ripped at all.
- The longest title (a making-of) is taken for the "main feature" and named `<Title (Year)>.mkv`,
  the same name as the film's file, which it then replaces.

Marking the disc as the **bonus disc** of the film's job fixes all three for that job only.

## Marking a disc

While the disc waits for manual identification (`MANUAL_WAIT`, 60 s by default), click
**Bonus Disc** on its card on the home page, or on its job page. The Custom Title and Title Search
pages link there too. The page lists the latest successful DVD and Blu-ray movie rips, newest
first, with their disc label, drive and finish time. A double feature (see
[Collection filing](#double-features-and-box-sets)) lists its second and later films under its job.
Pick the film the disc belongs to and save.

Marking a disc also gives its job the film's title, year and metadata id, so notifications and the
job list name the film. **Not a bonus disc** clears the choice.

The choice is stored on the job (`bonus_of_job_id`, and `bonus_of_film_id` for a film of a double
feature).

## What the ripper does with it

When the wait ends with the disc marked, the job's own config (not `arm.yaml`) is changed to:

| Setting | Value | Why |
|---|---|---|
| `MINLENGTH` | `BONUS_MINLENGTH` (`45` s by default) | Featurettes are often a few minutes long |
| `MAXLENGTH` | `99999` | One MakeMKV `mkv ... all` pass over the disc |
| `SKIP_DUPLICATE_TITLES` | `false` | Skipping duplicates rips title by title, and MakeMKV re-scans the whole disc for every title (11 minutes per scan on a slow drive) |
| `MAINFEATURE` | `false` | There is no main feature: everything is an extra |
| `RIPMETHOD` | `mkv` | A DVD is ripped by MakeMKV too, and any transcode (`SKIP_TRANSCODE: false`) is HandBrake's or FFmpeg's pass over the ripped files, which keeps every title. The other paths pick titles from the disc by `arm.yaml`'s `MINLENGTH`/`MAXLENGTH`, not the job's, and would skip every featurette; a `backup`/`backup_dvd` Blu-ray backup leaves no title files to file |

Every ripped title is then filed in the film's `EXTRAS_SUB` folder (`extras` when `EXTRAS_SUB` is
`None`), named `<disc label>_<title file>`, for example
`Monsters, Inc. (2001)/extras/Monsters_Inc_title_t03.mkv`. A name that is already taken gets `-2`,
`-3`, ... The film's own files are never touched. The disc photo is copied as
`disc-bonus-<disc label>.jpg` beside the film's `disc.jpg`; the case barcode and poster are not
copied (they are the film's already).

No `completed/movies/<Title (Year)>` or `transcode/movies/<Title (Year)>` folder is made for the
bonus disc. An empty leftover transcode folder of the film's name would make the next rip of the
film stop with "Duplicate rips are disabled".

A disc marked after the wait (while it is ripping) is still filed as extras, but it is ripped with
the usual settings, so short titles and duplicate titles are missing. The completed folder made for
it when the rip started (holding at most the disc's `poster.png`) is removed.

### Where the extras go

- **The film's folder is still in `COMPLETED_PATH`**: into its extras folder. The bonus disc's job
  gets the film's folder as its path.
- **The film's folder has gone** (moved to the media server by the
  [completed-rip shipper](https://github.com/automatic-ripping-machine/automatic-ripping-machine/tree/main/scripts/completed_shipper)
  for example): into `<film folder> - bonus job<id>/extras/` beside where it was. The shipper adds
  that folder's files to the film's folder on the media server, never replacing a file there. If
  you move rips by hand, move the extras into the film's folder yourself.

The `-2`, `-3` names only see the local folder. Once the film has shipped, a second bonus disc with
the same disc label (two copies of one disc, or a re-rip) has the same `<disc label>_title_tNN.mkv`
names as the first one's extras on the media server, and there is no other name to fall back to: the
shipper leaves its folder on the ripper. Rename or move those extras by hand.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `BONUS_MINLENGTH` | `"45"` | Minimum title length, in seconds, for a disc marked as a bonus disc |

## Double features and box sets

A box set whose films are on separate discs (Toy Story 1 and 2 on `TOY1DELUXE` and `TOY2_43`)
gives each film its own job: point the supplemental disc at the film it belongs to. A disc that
carries several films (a double feature) has one job and a row per film. The bonus disc page lists
those films under the job, so a supplemental disc can be filed with the second film rather than the
first.
See [Double Features](Double-Features) for how such a disc is found, or marked by hand.
