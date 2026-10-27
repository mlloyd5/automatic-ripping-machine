# Disc Camera

The disc camera feature points a webcam at your drive trays and photographs each disc as the tray closes. The photo is attached to the ARM job, shown in the web UI (home page cards, job detail, history and the title fix pages), copied into the finished rip folder as `disc.jpg`, and, when ARM cannot identify a disc, read by a [vision model](#vision-model) (or with OCR) to search for the title.

It gives you a ground-truth record of what was actually inserted, which is handy when the online lookup gets a title wrong or a disc has no usable label.

## How it works

ARM itself only wakes up on the udev `change` event, which fires after the tray has closed and the disc has been read. By then the label is inside the drive. So a small daemon (`arm/camera/daemon.py`) runs all the time:

1. It polls each optical drive's tray state a few times a second (the same `CDROM_DRIVE_STATUS` ioctl the UI uses).
2. It keeps a rolling buffer of the last few seconds of webcam frames.
3. When a tray goes from **open** to anything else, it saves the frame from `CAMERA_LEAD_SECONDS` earlier, i.e. the disc sitting in the open tray just before it retracted, as `DISC_PHOTO_PATH/<srN>_<YYYYmmdd-HHMMSS>.jpg`.
4. A few seconds later udev starts the ripper for that drive. The ripper looks for the newest photo of that drive taken within `DISC_PHOTO_MAX_AGE` seconds and stores its filename on the job.
5. If neither the case barcode nor the disc's own metadata (a DVD's CRC64 record, a Blu-ray's `bdmt_eng.xml` title) identified the disc and `DISC_PHOTO_VISION_URL` is set, the ripper sends the photo to a vision model and searches for the title it reads, before searching the disc's volume label (see [Vision model](#vision-model)). What the model read is kept on the job and offered on the Title Search / Custom Title pages, and names the disc when nothing online matches it.
6. If identification still fails and `DISC_PHOTO_OCR` is on, the ripper runs `tesseract` over the photo and tries the largest lines of text as title searches, unless the vision model already read the label well enough to name the disc (at least 50% sure): then OCR is skipped, so its noisier reading cannot retitle the rip. The raw OCR text is kept on the job and offered on the same pages. When a [case barcode](Barcode-Scanner) named the product, a line only counts if a provider has a title that is exactly that line; otherwise the disc keeps the product title.
7. After the rip, the photo is copied to the completed folder as `disc.jpg`.

Photos that no job claims (empty-tray closes, ejects) are deleted after `DISC_PHOTO_RETENTION_DAYS`.

The same daemon also drives the [Barcode Scanner](Barcode-Scanner): a scan of the disc case is paired with the next tray close in exactly the same way. Either feature works on its own.

## Hardware

- Any UVC USB webcam works (`/dev/video0` on most systems). 720p is plenty; 1080p helps OCR.
- Mount it above the drives looking down at the open trays. One camera can cover two or three drives stacked in a tower; use `CAMERA_DRIVE_REGIONS` to crop each drive's tray out of the frame so each photo only shows its own disc.
- Make sure the trays are lit. Disc labels are reflective, so avoid a light source directly behind the camera.

## Configuration

All settings live in `arm.yaml` under the **Disc Camera** heading. Use the **Disc Camera** page in the web UI (see below) to change them; it only rewrites these keys and the daemon picks the new values up without a restart.

| Key | Default | Meaning |
|---|---|---|
| `CAMERA_ENABLED` | `false` | Turn the daemon on. When false the service stays idle. |
| `CAMERA_DEVICE` | `/dev/video0` | Video device to read. |
| `CAMERA_DRIVES` | blank | Drives to watch, comma separated (`sr0,sr1`). Blank watches every `/dev/sr*`. |
| `CAMERA_DRIVE_REGIONS` | blank | Optional crop per drive: `sr0:x,y,w,h;sr1:x,y,w,h` in frame pixels. |
| `CAMERA_FPS` | `5` | Frames per second kept in the rolling buffer. |
| `CAMERA_BUFFER_SECONDS` | `4` | Seconds of frames kept. Must be larger than the lead time. |
| `CAMERA_LEAD_SECONDS` | `1.5` | How far back from the tray-closed moment to take the photo. |
| `CAMERA_JPEG_QUALITY` | `85` | JPEG quality of saved photos. |
| `DISC_PHOTO_PATH` | `/home/arm/media/disc_photos/` | Where photos are stored. In Docker this is inside the media volume so photos persist. |
| `DISC_PHOTO_MAX_AGE` | `180` | Seconds. A photo older than this when the job starts is not attached. |
| `DISC_PHOTO_RETENTION_DAYS` | `14` | Unattached photos older than this are deleted. |
| `DISC_PHOTO_OCR` | `false` | Run tesseract on the photo when identification fails. Off by default because misread label text can retitle a rip; only lines of two or more words, or six or more letters, are searched. |
| `DISC_PHOTO_VISION_URL` | blank | Ollama server to send the photo to (`http://host:11434`) when identification fails. Blank turns the vision model off. See [Vision model](#vision-model). |
| `DISC_PHOTO_VISION_MODEL` | `qwen2.5vl:7b` | Vision model to ask; must be pulled on the Ollama host. |
| `DISC_PHOTO_VISION_TIMEOUT` | `120` | Seconds to wait for one answer. The first request after an idle period also loads the model into the GPU. Connecting gives up after 5 seconds. |
| `DISC_PHOTO_COPY` | `true` | Copy the photo into the completed rip folder as `disc.jpg` (and a scanned [barcode](Barcode-Scanner) as `disc.barcode`). |

## Vision model

Tesseract reads next to nothing off a webcam frame of a printed disc: the text is curved, reflective and often sideways. A small multimodal model does far better. With `qwen2.5vl:7b` on one 16 GB GPU, every one of a batch of 640x480 tray photos (WALL-E, Room on the Broom, Tarzan, Thomas & Friends, Toon Time, The Peanuts Movie) was read correctly, in about six seconds each after a fifteen second first load.

**What it needs.** An [Ollama](https://ollama.com) server ARM can reach over the network with a vision model pulled on it:

```bash
ollama pull qwen2.5vl:7b
```

It needs Ollama 0.5.0 or later, the first release to accept a JSON schema as the answer format. Ollama only listens on localhost by default; set `OLLAMA_HOST=0.0.0.0` on that machine so the ARM box can reach port 11434. Nothing is installed on the ARM side: the ripper talks to Ollama's `/api/generate` endpoint over HTTP. Rotating the photo needs Pillow, which comes with the sleeve label extras (`requirements-labels.txt`, installed by the Dockerfile and the Debian installer); without it only the photo as taken is sent.

**Settings.** Set `DISC_PHOTO_VISION_URL` to the server (`http://192.168.1.20:11434`) in `arm.yaml`; `DISC_PHOTO_VISION_MODEL` and `DISC_PHOTO_VISION_TIMEOUT` have working defaults. A blank URL turns the step off entirely, and then tesseract OCR (`DISC_PHOTO_OCR`) is the only fallback that reads the photo.

**What happens.** The model is asked only about a video disc that nothing cheaper identified: the case [barcode](Barcode-Scanner) missed, the volume label does not name a collection of films, and the disc's own metadata has no title (a DVD the CRC64 database does not know, a Blu-ray whose `bdmt_eng.xml` is missing or unreadable and so only gives the volume label). The order is: case barcode, collection label, CRC64 record or `bdmt_eng.xml`, vision model, label search. The ripper sends the photo to the model and asks for JSON: what the photo shows (a disc label, an empty tray, a disc covered by a hand, a photo too blurred to read, or something else), up to 20 words it can actually read, then title, subtitle, year, studio, disc number, catalogue number, format and how sure it is of the title. The model names a film whether or not it can read one (told the photo showed a disc, it answered "The Matrix Reloaded", 95% sure, for every photo of an empty tray), so its answer is only used when it says it sees a disc label and every word of the title (other than "the", "a", "of" and the like) is among the words it read; a title of one letter is not used either. A photo of an empty tray, a hand or a blur ends the step at once, without trying the photo rotated. Labels are often printed sideways in the tray, so when the model is less than about 90% sure, or reads a one-word title, the photo is sent again rotated 90° and 270° and the most confident answer wins. A request that fails (Ollama down, a timeout, the model not pulled) is not repeated rotated: the step gives up at once rather than holding up the rip. The title (and the title with its subtitle) is then searched online with the year read, exact matches only, like a barcode's product title: a near miss is refused and the disc goes on to the label search. Once the model has read a title it is at least half sure of, the label search is held to the same rule as after a case barcode: only a result titled exactly like the volume label is taken, so "CARTOONS_D3" cannot take a provider's first near miss over what the disc says. When nothing online matches, the title read off the disc names the unidentified rip, with the year read (or none), ahead of the barcode's product title, because the disc names itself more precisely than its case does (one case said "59 Cartoon Classics", the disc "Toon Time: Ultimate Cartoon Collection"). A read the model was less than half sure of is not used as a name, and leaves tesseract OCR (if on) to try; a read it was surer of turns OCR off for that disc.

What the model read is stored on the job and shown on the Title Search and Custom Title pages as **Disc label**, with the year and how sure the model was, next to the barcode and OCR suggestions; click it to fill the title field. The title field starts out filled with the same full title (title and subtitle) the rip was named after.

**Database upgrade.** This step adds a column to the job table. The migration runs when the web UI starts, so start the web UI once after upgrading before inserting a disc.

## The Disc Camera page

Open **Disc Camera** in the web UI's navigation bar. Everything needed to set the camera up is on that one page:

- **Status card.** Whether the `armcamera` daemon is running, whether it has the camera open, the frame size and rate, how many frames it has read, and the tray state and last photo of each drive. A red *Daemon not running* badge means the service is not up (or cannot write to `DISC_PHOTO_PATH`); a yellow *idle* badge means it is up but `CAMERA_ENABLED` is off.
- **Live preview.** The camera image refreshes once a second while the page is open, so you can aim the camera and check the lighting. Regions already saved in `arm.yaml` are drawn in green.
- **Crop regions.** Pick a drive, drag a box around its tray on the preview and the pixel coordinates are filled in for you (scaled to the real camera resolution). Clear a drive to go back to the full frame.
- **Camera and timing / Disc photos.** Every `CAMERA_*` and `DISC_PHOTO_*` key with its help text. Save writes only those keys back to `arm.yaml`; the daemon reloads them within a second or two.
- **Test capture.** Takes a photo of one drive exactly as a real tray close would, using the current lead time, and shows it in the gallery underneath.

**Tuning the lead time.** Trays take one to two seconds to retract. Open a tray, press **Test capture** for that drive and look at the photo in the gallery: a half-closed tray means raise `CAMERA_LEAD_SECONDS`, an empty tray means lower it. Save and repeat until the disc is clearly visible. Keep `CAMERA_BUFFER_SECONDS` comfortably above the lead time.

**Finding crop regions.** Draw them on the live preview as described above; there is no need to measure pixels by hand. Leave `CAMERA_DRIVE_REGIONS` blank to save the full frame.

The daemon and the web UI never talk to each other directly. The page works through a few hidden files in `DISC_PHOTO_PATH`: `.camera_status.json` (written by the daemon every second, or about once a minute while the camera is disabled), `.preview_requested` and `.preview.jpg` (preview frames while the page is open) and `.capture_srN` (a test capture request). If the page says the daemon is not running but `sv status armcamera` says it is, check that both processes can read and write that folder.

## Docker

1. Add the camera to `start_arm_container.sh` (it is not in the default script, since `docker run` fails when the host has no `/dev/video0`):

   ```bash
   --device="/dev/video0:/dev/video0" \
   ```

2. Set `CAMERA_ENABLED: true` in `arm.yaml` and restart the container. The daemon runs as the `armcamera` runit service inside the container, next to the web UI.

3. **Database upgrade.** This feature adds two columns to the job table. The migration runs when the web UI starts, so after upgrading start the container once and let the UI come up before inserting a disc.

Check it is running (the Disc Camera page in the UI shows the same thing):

```bash
docker exec -it arm-rippers sv status armcamera
docker exec -it arm-rippers tail -f /home/arm/logs/camera.log
```

## Bare metal install

The Debian installer installs `tesseract-ocr`, adds `opencv-python-headless` (from `requirements-camera.txt`, kept separate because it has no 32-bit ARM wheel) to the venv and installs the `armcamera` systemd unit. On an existing install:

```bash
sudo apt install tesseract-ocr tesseract-ocr-eng
sudo -u arm /opt/arm/venv/bin/pip install -r /opt/arm/requirements-camera.txt
sudo cp /opt/arm/setup/armcamera.service /lib/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now armcamera
```

The `arm` user must be in the `video` group to read the webcam (`sudo usermod -aG video arm`).

## Testing without hardware

The daemon can fake both the camera and the tray event, which is a quick way to check paths and permissions:

```bash
python3 /opt/arm/arm/camera/daemon.py --frames-from /path/to/any.jpg --simulate-close sr0
```

A file named `sr0_<timestamp>.jpg` should appear in `DISC_PHOTO_PATH`.

## Troubleshooting

- **No photos appear.** Look at the status card on the Disc Camera page first, then `camera.log`. "Could not open camera" means the device is missing (Docker: check the `--device` line) or the `arm` user cannot read it (group `video`). "tray closed but the frame buffer is empty" means the camera opened but is not delivering frames; try another `CAMERA_DEVICE` or a lower resolution camera.
- **Photos exist but jobs do not show them.** The photo must be of the same drive (`sr0_…` for `/dev/sr0`) and taken less than `DISC_PHOTO_MAX_AGE` seconds before the job started. Check the job log for "Attaching disc photo" or "No disc photo found".
- **Photo is of a closed tray.** Increase `CAMERA_LEAD_SECONDS` on the Disc Camera page and use **Test capture** to check.
- **OCR never finds anything.** Run `tesseract /home/arm/media/disc_photos/<file>.jpg stdout` by hand to see what it reads. Better lighting and a crop region help far more than a higher resolution camera.
- **The vision model never answers.** Look in the job log for "asking vision model": a request failure names the URL and the error. Check the ARM box can reach the server (`curl http://host:11434/api/tags` lists the models pulled there; the model in `DISC_PHOTO_VISION_MODEL` must be among them) and that the first request is not timing out while the model loads (raise `DISC_PHOTO_VISION_TIMEOUT`). The ripper asks for its answer in a fixed JSON schema, which needs Ollama 0.5.0 or later (`ollama --version`): an older server answers every request with HTTP 400, so a request failure naming HTTP 400 on every disc means Ollama needs upgrading. "could not read a title" means the model answered but not with a usable title: "sees no disc label in the photo (empty_tray)" means the photo shows no disc (look at it on the job page), and "did not read it off a disc label" means the title it gave is not among the words it could read; try the photo by hand with `ollama run qwen2.5vl:7b "What disc is this?" /path/to/photo.jpg` on the server.
- **Job fails right after upgrading.** The database has not been migrated yet. Restart the web UI (or the container) once.
