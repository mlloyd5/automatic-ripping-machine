# Sleeve Labels

If you move your discs out of their cases into sleeves and boxes, you need a way to find a disc again. With a Brother P-touch tape printer plugged into the ARM box, ARM prints a label for every disc as its rip finishes, and keeps the index of which box each sleeve went into. Nothing is tracked outside ARM's database; the Boxes page searches it and exports it as CSV.

The label, on 18 mm tape (about 16 x 40 to 90 mm):

```
┌────┐  T6-0042
│ QR │  The Return of the King (2003)
└────┘  Blu-ray
```

The QR code holds only the sleeve id, `T6-0042`, so a cheap 2D USB scanner in keyboard mode (the same kind the [Barcode Scanner](Barcode-Scanner) feature uses) types it into the Boxes page followed by Enter. The text is there for flipping through a box without scanning.

The letters before the dash are the **machine prefix**: each ARM box hands out ids from its own sequence (`T6-0001`, `T6-0002` on a box called `t620`; `TS-0001` on `ts140`), so two boxes ripping at the same time never print the same code, and their CSV exports merge without clashes. The prefix comes from the hostname unless `LABEL_ID_PREFIX` says otherwise.

## Workflow

1. A DVD or Blu-ray rip finishes. ARM assigns the next sleeve id and sends the usual notification with it. With `LABEL_AUTO_PRINT` and the printer on this box, the label prints right away. Stick the label on the sleeve, drop the disc in.
2. When a stack of sleeves goes into a box, open **Boxes** in the web UI, type the box name once, then scan each sleeve. Each scan files that disc in the box.
3. To find a disc, type or scan its id into **Find a disc** (a title fragment works too; a bare number means this machine's prefix). The result names the box.

### The print queue

One printer, several ripping boxes: every rip gets its sleeve id at once, and a label that could not print (printer on the other box, unplugged, out of tape, `LABEL_AUTO_PRINT` off) waits in the queue. The Boxes page lists the queue at the top with a **Print pending** button; `python3 -m arm.labels print --pending` does the same from a shell. Plug the printer into the box, press the button, and the backlog comes out oldest first, in the order the discs were ripped. Printing stops at the first failure so the queue keeps its order (`--keep-going` on the command line tries them all).

Pending jobs show a *label pending* badge on their detail page; the id is already on the job, so a disc can go into a sleeve before its label exists and the label catches up.

Discs ripped before the feature was turned on get ids (and join the queue) with the backfill command below. Reprint a label from the job's detail page, or print one for a job that never got one (a failed rip, say) with the same button.

Sleeve ids are separate from job ids: they are assigned once, counting up from the highest one with this machine's prefix in the database, so a rebuilt database or a second rip of the same disc never prints a code that is already on a sleeve.

## Hardware

Any Brother P-touch with a USB port that [ptouch-rs](https://github.com/vowstar/ptouch-rs) supports: PT-E310BT, PT-E560BT, PT-P710BT, PT-D610BT, PT-D460BT, PT-P700, PT-P750W, PT-E550W and others (the full list is in the ptouch-rs README). Written for and set up with the PT-E310BT; **no other model has been tried**. Bluetooth is not used: the printer is cabled to the ARM box.

Tape: 18 mm TZe laminated tape is the one to use. Narrower tape works (`LABEL_TAPE_MM`), but the QR code shrinks with it: on 12 mm it is about 9 mm square and a phone camera will struggle, a desk scanner is fine. 24 mm tape only fits the larger models.

### Linux USB access

The printer is a USB printer-class device, so the kernel's `usblp` driver claims it when it is plugged in and libusb (ptouch-rs) cannot open it. The udev rule `setup/99-arm-ptouch.rules` unbinds `usblp` from any Brother printer and gives the device to the `plugdev` group, which the `arm` user is added to. The installer and the Docker image put the rule in place; on a manual install:

```bash
sudo cp /opt/arm/setup/99-arm-ptouch.rules /etc/udev/rules.d/
sudo usermod -aG plugdev arm
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Check it from the `arm` user: `ptouch doctor` lists the printer and the tape it has loaded, `ptouch print "hello"` prints a test label.

### Docker

Pass the USB bus into the container (the device path changes when the printer is re-plugged, so pass the whole bus rather than one device) in `start_arm_container.sh`:

```bash
    --device="/dev/bus/usb:/dev/bus/usb" \
```

The image installs ptouch-rs and the udev rule, but udev does not run inside the container: install the rule on the host as above so `usblp` is unbound there. The container runs `--privileged` already, which covers the device permissions.

### Proxmox LXC

For an unprivileged container, allow USB character devices and bind the bus in the container's config (`/etc/pve/lxc/<id>.conf`):

```
lxc.cgroup2.devices.allow: c 189:* rwm
lxc.mount.entry: /dev/bus/usb dev/bus/usb none bind,optional,create=dir
```

and install the udev rule on the Proxmox host, not in the container, since the host's kernel owns `usblp`. Inside the container the device must be readable by the `arm` user's group; the host rule's `GROUP="plugdev"` maps to a host gid, so either give the container's `arm` user that mapped gid or use `MODE="0666"` in the rule on the host.

## Configuration

All settings live in `arm.yaml` (Settings > Ripper Settings in the UI) under the **Sleeve Labels** heading.

| Key | Default | Meaning |
|---|---|---|
| `LABEL_ENABLED` | `false` | Turn the feature on. Boxes can be assigned without it; nothing prints. |
| `LABEL_AUTO_PRINT` | `true` | Print as soon as a DVD or Blu-ray rip finishes. Off: the label waits in the queue (the id is assigned either way). |
| `LABEL_TAPE_MM` | `18` | Tape width loaded: 6, 9, 12, 18 or 24. Sets the label height. |
| `LABEL_PRINTER_CMD` | `ptouch print -i {image}` | Command that prints a PNG; `{image}` is the file. Any tool works, e.g. `ptouch-print --image {image}`. |
| `LABEL_USB` | blank | ptouch-rs `--usb BUS:ADDRESS` (from `ptouch doctor`) when more than one printer is attached. |
| `LABEL_TIMEOUT` | `60` | Seconds to wait for the printer command. |
| `LABEL_ID_PREFIX` | blank | Machine prefix of the ids, 1-4 letters or digits. Blank: the first two characters of the hostname. Give each box its own; changing it later starts a second sequence. |
| `LABEL_MAX_MM` | `90` | Longest label. Long titles are shrunk, then shortened with an ellipsis. |
| `LABEL_FONT` | blank | TrueType font for the text. Blank finds DejaVu Sans Bold (installed by the installer and the image) or another bold font. |

A printer that is off, unplugged or out of tape never fails a rip: the rip completes, the sleeve id is still assigned and saved, and the label waits in the queue.

## Command line

Run from the ARM install as the `arm` user, with ARM's virtualenv (`/opt/arm/venv/bin/python3` on a script install, plain `python3` in Docker):

```bash
python3 -m arm.labels pending               # list the labels waiting to be printed
python3 -m arm.labels print --pending       # print the queue, oldest first (stops at the first failure)
python3 -m arm.labels print 42 43           # print or reprint the labels of jobs 42 and 43
python3 -m arm.labels backfill              # give every finished video rip without an id one, then print the queue
python3 -m arm.labels backfill --no-print   # ids only; they wait in the queue
python3 -m arm.labels backfill --dry-run    # show what would be assigned
python3 -m arm.labels preview 42 out.png    # render job 42's label to a PNG without printing
python3 -m arm.labels test out.png          # render a sample label, no database needed
```

The backfill goes through jobs oldest first, so the ids follow the order the discs were ripped.

## JSON API

`json?mode=set_box&job=42&box=Box%2003` files job 42's sleeve in "Box 03" (blank `box` clears it); `json?mode=print_label&job=42` prints the label and returns the sleeve id and the printer's message; `json?mode=pending_labels` lists the queue and `json?mode=print_pending` prints it. All need a logged-in session, like the rest of the mini API.

## What was tested

The renderer, printer wrapper, id assignment, Boxes page and API have unit tests (`test/unittest/test_labels_*.py`, `test_ui_boxes.py`). Nothing has been run against a real printer yet: the ptouch-rs support for the PT-E310BT comes from its model list, and the udev rule, the Docker and LXC device passthrough and the tape sizing were written from Brother's raster documentation. The first print on real hardware is the real check; `python3 -m arm.labels test out.png` followed by `ptouch print -i out.png` as the `arm` user is the way to do it before enabling auto-print.
