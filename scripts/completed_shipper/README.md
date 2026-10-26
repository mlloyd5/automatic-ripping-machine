# ARM completed-rip shipper

A small sidecar that moves finished rips from an ARM ripping box to a media box
with `rsync`.  It lives next to ARM, not inside it: ARM keeps sorting rips into
`COMPLETED_PATH/movies`, `tv` and `unidentified` exactly as before, and this
script ships those folders over the network once ARM's database says the job is
done.

Typical setup: a box that only rips (here called **ts140**) and a box that runs
Plex (**t620**).  Recognized rips go straight into the Plex library folders on
the t620 at full speed.  Unidentified rips go to a staging folder on the t620
for you to rename by hand, at lower priority and with a bandwidth cap.

## What it does on every run

1. Opens ARM's sqlite database read-only and collects every `job` whose `path`
   is under `arm_completed_path` (ARM's own `COMPLETED_PATH`) and whose
   `status` is `success`, translated to the same folder under `completed_path`.  A folder is only
   eligible if no other job (for example a re-rip of the same disc) is still
   unfinished on that same path.  Logs are never tailed.
2. Ships every eligible folder in `movies/`, then `tv/`, with
   `rsync -a -s --partial-dir=.rsync-partial --remove-source-files`.  First,
   though, two rsync dry runs ask the media box whether the destination folder
   already holds a file the transfer would replace (see **Never overwriting**
   below); if it does, the folder ships as `Title (Year) - <disc label>`
   beside the original instead.  The name is kept in the spool until the
   folder has shipped, so an interrupted transfer finishes in the same folder.
   After rsync exits 0 the now-empty job folder is pruned.  Anything else is
   left in place for the next run.
3. Only when no recognized folder is pending, ships **one** eligible
   `unidentified/` folder with `--bwlimit=bwlimit_unidentified`, then goes back
   to step 1.  A movie that finishes while a long unidentified transfer is
   running therefore waits for at most that one folder.  If a recognized rsync
   failed, no unidentified transfer is started at all in that run.
4. With `remote_catalog_path` set, leaves a **catalog record** for every
   movie or tv folder it shipped: a small TOML file with what ARM knew about
   the disc and what the shipped files' Matroska headers say (see below).
5. Exits 0 when everything shipped, 1 when a transfer failed (systemd marks the
   unit failed so you notice), 2 on configuration problems or when another run
   still holds the lock.

Interrupting the script, rebooting, or a dropped link are all safe: rsync keeps
partial files in a hidden `.rsync-partial` folder inside the destination job
folder (never under their final name), nothing local is deleted before rsync
has transferred it, and the next timer run simply resumes.  Running it twice in a row is a no-op.

Folders whose job is still `active`, `ripping`, `transcoding`, `waiting` or
`fail`, and folders ARM knows nothing about, are never touched.

### Never overwriting what the media box holds

ARM names every movie's main feature `<Title (Year)>.mkv`, so the second disc
of a title the pool already has (the Blu-ray after the UHD, or a re-rip) would
land on the feature Plex serves, and `--remove-source-files` would then delete
the only other copy.  The shipper therefore never replaces a file on the media
box:

* Before a folder moves, two rsync dry runs (`-a --dry-run --itemize-changes
  --checksum`, the second with `--ignore-existing`) list what the transfer
  would send.  Files in the first list but not the second exist at the
  destination and differ, so the real transfer would replace them.  The dry
  runs compare checksums, not rsync's usual size and modification time: a
  different file that happens to have the same size and time would otherwise
  be skipped by the transfer, and `--remove-source-files` would delete the
  only copy of the new rip.  A file an interrupted run already delivered has
  its source's contents, appears in neither list, and resumes in place as
  before.  (The checksums cost a read of both copies of every file that
  already exists at the destination, which is only the case on a resume or a
  collision; the transfer itself keeps the quick check.)  Different file names
  are no collision either: a second disc of a TV season joins the season
  folder.
* On a collision the folder ships as `<Title (Year)> - <disc label>` (the
  disc's volume label, e.g. `Movie A (2001) - MOVIE_A_BD`), or
  `<Title (Year)> - job<id>` when that name is taken as well.  Plex groups the
  two folders as versions of one title, since the file inside is still called
  `<Title (Year)>.mkv`; which one it serves, and whether both stay, is your
  call, not the shipper's.  The catalog record names the folder that actually
  received the files.
* When every name is taken, or a probe fails, the folder stays on the ripper,
  the log names the files that collided, and the run exits 1 (which also holds
  back unidentified transfers, like any failed recognized folder).
* Unidentified folders go straight to the job id (`DVD_VIDEO - job42`): ARM
  names them after the disc label already, and many discs share labels like
  `DVD_VIDEO`.  The label is skipped the same way for any folder whose name
  equals it.
* The name is decided once, before the transfer starts, and written
  atomically to `catalog_spool_path` as `job<id>.destination` (the local job
  folder as `source = "movies/<folder>"` and the chosen `remote_name`).  A run
  that is interrupted after delivering part of the folder leaves it there, and
  the next run resumes into that name without probing ARM's name again: with
  the feature already delivered and removed, that probe would see only the
  extras and send them into the original folder, splitting the rip.  The kept
  name is still probed, so a file someone else put there in the meantime is
  caught (the rip stays, exit 1); files this folder already delivered count as
  a resume.  The state file is removed once the folder has shipped, and at
  the start of a run when its job folder is no longer in `completed_path`.
  When a pending catalog record names another destination than the state
  file, the state file wins (with a warning) and the record is pointed at it.
  If the spool cannot be written, a folder shipping under ARM's own name goes
  anyway (exit 1), but one that would ship under a suffix stays.

The dry runs are ordinary rsync pushes, so they need no listing or reading
rights on the media box: a key confined to receiving rsync transfers (see the
Proxmox section) allows them.  `--dry-run` runs them too, so a smoke test
shows the name each folder would get.

## Requirements

* Ripping box (wherever the shipper runs): Python 3.6+ (stdlib only),
  `rsync` 3.0 or newer, `ssh` client.
* Media box: `rsync` 3.0 or newer, an ssh account that can write to the
  destination folders.  rsync is always run with `-s` (`--protect-args`) so
  paths with spaces and parentheses (`Title (Year)`) reach the media box
  intact whatever the rsync versions and remote shell; both ends need 3.0+ for
  that.
* Key-based ssh from the user the shipper runs as (`arm` on a native install,
  `root` on a Proxmox host) to the media box
  (`ssh_options` defaults to `BatchMode=yes` so a missing key fails fast
  instead of hanging on a password prompt).

## Installation (ripping box)

```bash
# 1. Config
sudo cp /opt/arm/scripts/completed_shipper/arm-completed-shipper.conf.example /etc/arm-completed-shipper.conf
sudo editor /etc/arm-completed-shipper.conf

# 2. ssh key for the arm user -> media box
sudo -u arm ssh-keygen -t ed25519 -f /home/arm/.ssh/id_ed25519 -N ''
sudo -u arm ssh-copy-id arm@t620
sudo -u arm ssh -o BatchMode=yes arm@t620 'mkdir -p /srv/plex/Movies /srv/plex/TV /srv/arm-staging/unidentified'

# 3. Try it by hand first
sudo -u arm python3 /opt/arm/scripts/completed_shipper/arm_completed_shipper.py -c /etc/arm-completed-shipper.conf --dry-run -v

# 4. systemd timer
sudo cp /opt/arm/scripts/completed_shipper/systemd/arm-completed-shipper.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now arm-completed-shipper.timer
```

The service unit assumes a native install: ARM checked out at `/opt/arm` and
an `arm` user.  If yours differs, override it with a drop-in instead of editing
the unit (see the Proxmox section below for an example).  Logs go to the
journal: `journalctl -u arm-completed-shipper.service`.

## Running on the Proxmox host (Docker-in-LXC)

When ARM runs in Docker inside an LXC, run the shipper on the Proxmox host, not
in the container: it then works over whichever uplink the host has, and needs
nothing inside ARM's container.  The catch is that ARM writes **container**
paths into `job.path`, while the host sees the same folders elsewhere.  Set
`completed_path` to the host path and `arm_completed_path` to `COMPLETED_PATH`
as it appears in `arm.yaml`; the shipper maps one onto the other.

Example for the ts140 (host `/var/lib/arm-ripper` -> `/srv/arm` in LXC 140 ->
`/home/arm` in the container, and host `/scratch/rips` -> `/srv/arm-media` ->
`/home/arm/media`), shipping to the t620 as root:

```ini
[shipper]
db_path = /var/lib/arm-ripper/home/db/arm.db
completed_path = /scratch/rips/completed
arm_completed_path = /home/arm/media/completed
remote_host = root@t620
remote_movies_path = /tank/media/movies
remote_tv_path = /tank/media/tv
remote_unidentified_path = /tank/rips/unidentified
remote_catalog_path = /tank/rips/catalog
lock_file = /run/lock/arm-completed-shipper.lock
```

There is no `arm` user on the host and the checkout lives in
`/var/lib/arm-ripper/src`, so override the unit with a drop-in
(`sudo systemctl edit arm-completed-shipper.service`):

```ini
[Service]
User=root
Group=root
# An empty ExecStart= clears the one from the unit before setting the new one.
ExecStart=
ExecStart=/usr/bin/python3 /var/lib/arm-ripper/src/scripts/completed_shipper/arm_completed_shipper.py --config /etc/arm-completed-shipper.conf
```

The ssh key is then **root's** on the host:

```bash
sudo ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ''
sudo ssh-copy-id root@t620
sudo ssh -o BatchMode=yes root@t620 'mkdir -p /tank/media/movies /tank/media/tv /tank/rips/unidentified'
sudo python3 /var/lib/arm-ripper/src/scripts/completed_shipper/arm_completed_shipper.py \
    -c /etc/arm-completed-shipper.conf --dry-run -v
```

The "ship immediately" hook below does not apply here: a `BASH_SCRIPT` inside
ARM's container cannot start a unit on the host.  The 15-minute timer is the
mechanism on this setup.

The media box can confine that key to receiving rsync transfers with an
`authorized_keys` forced command (the home-ansible-projects repo installs one,
`arm-shipper-rsync`, that accepts `rsync --server` plus one cluster of short
flags and refuses everything else, including `rsync --sender`): the shipper
only ever pushes and never lists the media box.  The collision check before
each folder is two `rsync --dry-run` pushes with the same ssh, `-s` and extra
arguments as the transfer, so a forced command that accepts the transfer's
command line accepts them too: on the remote side they differ from the
transfer only by two letters in the short-flag cluster, `n` (`--dry-run`) and
`c` (`--checksum`).  With rsync 3.x and `-s` the transfer arrives as
`rsync --server -slogDtpre.iLsfxCIvu` and each probe as
`rsync --server -snlogDtprce.iLsfxCIvu`; `--ignore-existing`, `--timeout` and
the paths travel inside the protocol.  Stock `rrsync -wo` does **not** work here:
it refuses `-s`, which the shipper relies on to keep paths with spaces and
parentheses off the remote command line.

## Configuration

All settings live in one `[shipper]` section.  Defaults match a stock
`arm.yaml`.

| Key | Meaning |
| --- | --- |
| `db_path` | ARM's sqlite file, `DBFILE` in `arm.yaml`.  Read-only. |
| `completed_path` | ARM's `COMPLETED_PATH` as seen by **this** script.  On a native install copy it from `arm.yaml`; when the shipper runs outside ARM's container, use the host path of the same folder. |
| `arm_completed_path` | `COMPLETED_PATH` exactly as in `arm.yaml`, i.e. the prefix ARM writes into `job.path`.  Defaults to `completed_path`; set it when the two differ (container vs host).  DB rows outside this prefix are ignored. |
| `remote_host` | `user@host` of the media box.  Leave empty if the destination paths are local mounts (NFS/SMB) on the ripping box. |
| `remote_movies_path` / `remote_tv_path` | Plex library folders on the media box.  Each rip lands as `<path>/<Title (Year)>/`, or as `<path>/<Title (Year)> - <disc label>/` when that folder already holds a file the rip would replace (see "Never overwriting"). |
| `remote_unidentified_path` | Staging folder on the media box for manual review.  Keep it **outside** any Plex library.  Same rule for a folder name already in use, with the job id as the suffix. |
| `remote_catalog_path` | Optional.  Directory on the media box that receives one catalog record per shipped movie or tv folder.  Must exist.  Empty turns records off. |
| `catalog_spool_path` | Where records wait between being written and shipped, and where each folder's chosen destination name (`job<id>.destination`) is kept while it moves, records on or off.  Default `<completed_path>/catalog`. |
| `ripper_name` | The ripper's name written into each record.  Default: this box's hostname. |
| `bwlimit_recognized` | rsync `--bwlimit` in KiB/s for movies/tv.  `0` = unlimited. |
| `bwlimit_unidentified` | rsync `--bwlimit` for unidentified rips.  `0` = unlimited. |
| `ssh_options` | Extra arguments for the ssh command rsync runs (`-i keyfile`, `-p port`, ...). |
| `rsync_path` / `rsync_extra_args` | rsync binary and extra flags, e.g. `--chown=plex:plex` or `--chmod=D775,F664` so Plex can read the files. |
| `rsync_timeout` | Seconds of I/O silence before rsync gives up; the folder is retried next run. |
| `lock_file` | Prevents overlapping runs. |
| `post_ship_command` | Optional shell command run after each folder ships, with `SHIPPER_TYPE`, `SHIPPER_FOLDER` (ARM's folder name) and `SHIPPER_DESTINATION` (where it landed, suffix included) in the environment. |

Command-line flags: `-c/--config`, `-n/--dry-run` (passes `--dry-run` to
rsync and deletes nothing), `-v/--verbose`, plus `--db-path`,
`--completed-path`, `--arm-completed-path` and `--lock-file` overrides.

Destination parent folders must already exist.  rsync creates the per-title
folder itself.

## Catalog records

A media library kept as records (which discs are owned, what was ripped from
each, which case they live in, what is on the pool) needs to hear about every
rip, and ARM is the one place that knows the disc label, the metadata match,
the sleeve id and the case barcode together.  With `remote_catalog_path` set,
each shipped `movies/` or `tv/` folder leaves one TOML record there, named
`<rip finished>-<ripper>-job<id>.toml`:

```toml
ripper = "ts140"
job = 12
shipped = "2026-10-25T14:03:11"       # when the folder's transfer finished
type = "movie"                       # movie | series, from the folder shipped
title = "Back to the Future"
year = 1985
imdb = "tt0088763"                   # or tmdb = 105; absent when ARM had no match
library = "movies/Back to the Future (1985)"            # the folder that received the files
destination = "/tank/media/movies/Back to the Future (1985)"
source = "movies/Back to the Future (1985)"             # the ripper-side job folder it came from
label = "BACK_TO_THE_FUTURE"         # the disc's volume label
format = "uhd"                       # uhd | bd | dvd
role = "feature"                     # bonus when a movie disc has no main feature
sleeve = "TS-0007"                   # Sleeve Labels id, when the feature is on
box = "Box 02"
barcode = "025192412363"             # case barcode from the Barcode Scanner feature
barcode_title = "Back to the Future: The Ultimate Trilogy (4K UHD)"

[[files]]
path = "movies/Back to the Future (1985)/Back to the Future (1985).mkv"
main = true
size = 74012345678
seconds = 6960
resolution = "3840x2160"
video = "MPEGH/ISO/HEVC"
audio = "eng/TRUEHD,eng/AC3"
subs = 3
chapters = 20

[[files]]
path = "movies/Back to the Future (1985)/extras/Back to the Future (1985)_t03.mkv"
main = false
size = 1234567890
...
```

`format` comes from ARM's disc type and the widest picture in the files
(3840 wide is UHD).  The file rows come from `mkvheader.py`, a standard-library
Matroska header reader shipped next to the script (`python3 mkvheader.py
<path>` prints the same columns as TSV), so a file that is not Matroska is
listed by size alone.  Keys ARM did not fill (no barcode scanned, no sleeve
label, a stock ARM database without those columns) are left out rather than
written empty.

`library`, `destination` and every file `path` name the folder the files
actually landed in: a rip that had to ship as `Movie A (2001) - MOVIE_A_BD`
because `Movie A (2001)` already held a feature is recorded under that name,
while `main` still marks the feature (ARM names it after its own folder).
`source` is the ripper-side job folder (`<movies|tv>/<ARM's folder name>`),
the same as `library` unless the rip shipped under a suffix; the shipper uses
it for its own bookkeeping.

The record is built before the folder's rsync starts, while every file is
still local, and written atomically (via a `.tmp` file and a rename) to
`catalog_spool_path` as `<name>.pending`.  If that transfer is interrupted,
the next run finds the pending record by its `-job<id>.pending` ending and
reuses it, so the record still lists the files the first run already
delivered and removed.  Once rsync has exited 0 and the folder is empty, the
record is renamed to `<name>.toml` (with `shipped` set to that moment), copied
with its own rsync to `remote_catalog_path` (which must exist; the same ssh
key and options, no bandwidth cap) and removed from the spool.  A record whose
copy failed stays in the spool as `.toml`, the run exits 1, and the next run
ships it before anything else.  Only `.toml` files are ever shipped from the
spool; `.pending` and `.tmp` ones never are.

A `.pending` record whose job folder is no longer in `completed_path` (the run
was killed between the folder shipping and the rename, or the folder was
removed by hand) is finished and shipped at the start of the next run, with a
warning naming it: the shipper cannot list the media box to tell the two cases
apart, so it assumes the folder shipped.  It only draws that conclusion while
the folder's category directory (`movies/`, `tv/`) exists: when that is missing
too, `completed_path` is most likely not mounted, and every pending record is
left alone with a warning.  A pending record is matched to its folder by job id
and its `source` key (not `library`, which carries the destination suffix), so a
job id reused after an ARM database reset never picks up another rip's record.
A transfer that was interrupted after stepping aside to `Title (Year) - <label>`
reuses the pending record built with that name, so the finished record lists
every file under it.

The record is optional metadata and never stops a folder from shipping: a file
whose Matroska header is corrupt is listed by size alone (with a warning), a
record that cannot be built is logged and the folder ships without it, and a
spool that cannot be written logs the record's full text at error level, so it
can be recovered from the journal.  Those last two make the run exit 1.

Unidentified rips get no record: the catalog cannot register a disc without a
title, and those folders are renamed by hand anyway.  A folder with no media
file (only sidecars) gets none either.  A dry run logs what it would write
(`-v` prints the record) and touches nothing.

Nothing here reads the records.  On the setup this was written for, the
catalog lives in git on another machine and imports them from the media box
(`catalog import` in the home-ansible-projects repo).

## Pairing with Plex on the media box

Plex can pick new rips up on its own as soon as they land:

1. In Plex, open **Settings → Library** (server settings) and turn on
   **Scan my library automatically** and **Run a partial scan when changes are
   detected**.  Keep **Empty trash automatically after every scan** on if you
   want removed items to disappear too.
2. Point the Movies and TV libraries at `remote_movies_path` and
   `remote_tv_path`.  Do **not** add `remote_unidentified_path` to any library;
   it is a parking lot for folders you still have to rename.  Once you have
   identified a rip, move it into the library folder on the t620 (`mv` on the
   same filesystem is instant) and Plex scans it in.

Change detection on Linux uses inotify, which only works for folders on the
media box's **local** disks.  Because this shipper writes with rsync over ssh
*onto* the media box, the files are local there and the partial scan fires.  It
would not fire if Plex were watching a network mount instead.

rsync writes each file as a hidden temporary name (`.Title.mkv.XXXXXX`) and
renames it when complete.  If a transfer is interrupted, the partial file is
moved into a hidden `.rsync-partial` folder inside the job folder (Plex skips
dot-folders) and the next run resumes from it, so Plex never sees a
half-copied file under its real name.

If you prefer an explicit trigger, or your Plex lives in a container where
inotify is unreliable, use `post_ship_command` to call the Plex API from the
ripping box.  Find your section ids with
`curl "http://t620:32400/library/sections?X-Plex-Token=TOKEN"` and then, for
example:

```ini
post_ship_command = case "$SHIPPER_TYPE" in movies) s=1;; tv) s=2;; *) exit 0;; esac; curl -fsS "http://t620:32400/library/sections/$s/refresh?X-Plex-Token=TOKEN"
```

The hook is skipped for unidentified rips in that example so the staging folder
is never scanned.  (Finding the token: Plex support article "Finding an
authentication token / X-Plex-Token".)

## Optional: ship immediately when ARM finishes

This recipe is for a **native** install, where ARM and the shipper's unit run
on the same system.  It does not work from inside ARM's Docker container.

The timer alone is enough; it fires every 15 minutes and catches up on anything
pending.  If you want rips to leave the box within seconds of ARM finishing,
let ARM's `BASH_SCRIPT` notification hook poke the service.  Add to the
ripping box:

```bash
# /etc/sudoers.d/arm-shipper
arm ALL=(root) NOPASSWD: /bin/systemctl start --no-block arm-completed-shipper.service
```

```bash
# /home/arm/shipper-notify.sh  (set BASH_SCRIPT to this path in arm.yaml)
#!/bin/bash
# ARM calls this with <title> <body> for every notification.  Starting the
# service is cheap: the lock file makes a run that finds nothing a no-op.
sudo /bin/systemctl start --no-block arm-completed-shipper.service
```

ARM's hook fires on job start too, which is harmless: the shipper only ships
jobs the database already marks `success`, so an early call just finds
nothing to do.

## Testing

The test builds a fake `COMPLETED_PATH`, a temporary ARM database and local
destination folders, and runs the shipper end to end through a wrapper around
the real `rsync` that records every call.  It covers priority order, the
bandwidth cap, skipped unfinished/failed/unknown folders, the failure
behaviour, resume after an interrupted copy, dry run, the lock, container to
host path mapping (`arm_completed_path`), the rsync command shape (`-s`,
`--partial-dir`, unquoted remote path), and the catalog records: their
content from a database with and without the fork's columns, the Matroska
header reader on hand-built files (SeekHead following, truncation at every
byte offset, random byte flips, corrupt element sizes and positions), a record
whose transfer fails and ships on the next run, an interrupted folder transfer
whose record still lists every file, pending records (atomic writes, never
shipped early, finished when their folder is gone), an unwritable spool, a
corrupt header or failing record build that still ships the folder, and that
dry runs, unidentified rips and media-less folders leave none.  For the
collision check: a feature already on the media box is kept and the rip
delivered beside it under the disc label, then under the job id, then left on
the ripper with exit 1 when every name is taken; a file of the same size and
time but different content counts as a collision; an unidentified folder
named after its label goes straight to the job id; a second TV disc joins its
season folder; a resumed transfer is not a collision; the record names the
delivered folder; a transfer interrupted after stepping aside finishes in the
same folder on the next run (movies with and without records, unidentified),
with one complete record; a kept name that has since been taken leaves the
rip; the state file wins over a disagreeing pending record; stale state
files are dropped; the probe command shape; and the itemized output of both
rsync and openrsync (the one on macOS), which the test runs under whichever
`rsync` is on `PATH`:

```bash
python3 -m unittest scripts/completed_shipper/test_arm_completed_shipper.py
PATH=/opt/homebrew/bin:$PATH python3 -m unittest scripts/completed_shipper/test_arm_completed_shipper.py
```

## Troubleshooting

* **First smoke test.**  Run it by hand, as the user the unit runs as, on the
  box where it will run:
  `python3 arm_completed_shipper.py -c /etc/arm-completed-shipper.conf --dry-run -v`.
  It lists every folder it would ship and the exact rsync command, and moves
  nothing.
* **Nothing ships, log says "not shippable yet".**  Run with `-v`: either the
  job is not `success` yet, or `arm_completed_path` differs from what ARM wrote
  into `job.path` (typical when running outside ARM's container without
  setting it).  Check with
  `sqlite3 /home/arm/db/arm.db "select job_id,status,path from job order by job_id desc limit 5"`.
* **"unable to open database file".**  The DB is opened read-only
  (`?mode=ro`), but sqlite still needs read access to the db file **and** its
  directory (for the `-journal`/`-wal` file next to it).
* **rsync exits 255.**  ssh could not connect or authenticate as the `arm`
  user non-interactively.  Test with
  `sudo -u arm ssh -o BatchMode=yes arm@t620 true`.
* **Unidentified rips never move.**  A recognized folder keeps failing (the
  log says which) and blocks them by design.  Fix or move that folder, then the
  next run proceeds.
* **A folder arrived as `Title (Year) - LABEL`.**  `Title (Year)` on the media
  box already held a file of the same name that differed; the log names it.
  Nothing was overwritten or deleted.  Decide which copy Plex should keep (or
  keep both as versions) and tidy by hand on the media box.
* **"every destination name for ... already holds a file it would replace".**
  ARM's name, the label name and the job-id name all exist on the media box
  with that file.  Move or remove one there, or rename the folder on the
  ripper; the next run retries.  Until then the run exits 1 and unidentified
  transfers wait.
* **"..., the destination an earlier run chose for ..., now holds ...".**  A
  transfer into `Title (Year) - LABEL` was interrupted, and before the next
  run a different file of the same name appeared in that folder.  The rip
  stays on the ripper (exit 1).  Move the stranger away on the media box, or
  remove `job<id>.destination` from `catalog_spool_path` to let the shipper
  choose a new name, which leaves the part already delivered where it is.
* **Permissions on the media box.**  Use `rsync_extra_args = --chmod=D775,F664`
  (and `--chown=` if the ssh user may chown) so the Plex user can read the files.
