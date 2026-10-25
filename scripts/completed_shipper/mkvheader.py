#!/usr/bin/env python3
"""
Read what a Matroska file's header says about it, without ffprobe or mkvinfo.

Only the standard library: EBML is walked by hand, reading the Info, Tracks and
Chapters elements at the top of the Segment (or, when a muxer put them after the
first Cluster, where the SeekHead points).  The result is what a media catalog
wants to know about a file: duration, picture size, video codec, audio and
subtitle tracks, chapter count and the segment title.

``probe(path)`` returns a dict; ``probe_row(path)`` the same as the TSV columns
the catalog's inventory uses.  Run as a script it prints one TSV line per file
under the given paths.
"""

import os
import struct
import sys

INFO, TRACKS, CHAPTERS, CLUSTER, SEEKHEAD = 0x1549A966, 0x1654AE6B, 0x1043A770, 0x1F43B675, 0x114D9B74
EBML_HEADER, SEGMENT = 0x1A45DFA3, 0x18538067
MEDIA_EXTENSIONS = (".mkv", ".mka", ".mks", ".mk3d", ".webm")
"""Containers this module can read; anything else is listed by size alone."""


class NotMatroska(Exception):
    """The file does not start with an EBML header."""


def _read_id(handle):
    first = handle.read(1)
    if not first:
        return None
    lead = first[0]
    for length in range(1, 5):
        if lead & (0x80 >> (length - 1)):
            return int.from_bytes(first + handle.read(length - 1), "big")
    return None


def _read_size(handle):
    """An EBML data size; -1 means "unknown" (a live-muxed Segment or Cluster)."""
    first = handle.read(1)
    if not first:
        return None
    lead = first[0]
    for length in range(1, 9):
        mask = 0x80 >> (length - 1)
        if lead & mask:
            rest = handle.read(length - 1)
            value = ((lead & (mask - 1)) << (8 * (length - 1))) | (int.from_bytes(rest, "big") if rest else 0)
            if value == (1 << (7 * length)) - 1:
                return -1
            return value
    return None


def _children(handle, end):
    """Yield (element id, size, data start) for each child element up to ``end``."""
    while handle.tell() < end:
        element_id = _read_id(handle)
        if element_id is None:
            return
        size = _read_size(handle)
        if size is None:
            return
        start = handle.tell()
        yield element_id, size, start
        if size < 0:
            return
        handle.seek(start + size)


def _uint(handle, size):
    return int.from_bytes(handle.read(size), "big")


def _float(handle, size):
    data = handle.read(size)
    return struct.unpack(">f", data)[0] if size == 4 else struct.unpack(">d", data)[0]


def _string(handle, size):
    return handle.read(size).decode("utf-8", "replace").rstrip("\x00")


def probe(path):
    """
    Read the header of the Matroska file at ``path``.

    :return: dict with ``size`` (bytes), ``seconds`` (float, -1 when the header
             has no duration), ``width``, ``height``, ``video`` (codec id without
             the ``V_`` prefix), ``audio`` (list of ``lang/CODEC``), ``subs``
             (count), ``chapters`` (count) and ``title``
    :raises NotMatroska: when the file has no EBML header
    :raises OSError: when it cannot be read
    """
    result = {"size": os.path.getsize(path), "seconds": -1.0, "width": 0, "height": 0, "video": "",
              "audio": [], "subs": 0, "chapters": 0, "title": ""}
    duration = None
    timecode_scale = 1000000

    with open(path, "rb") as handle:
        if _read_id(handle) != EBML_HEADER:
            raise NotMatroska(path)
        size = _read_size(handle)
        handle.seek(handle.tell() + max(size or 0, 0))
        if _read_id(handle) != SEGMENT:
            raise NotMatroska(path)
        size = _read_size(handle)
        segment = handle.tell()
        segment_end = segment + size if size and size > 0 else result["size"]
        seen = set()
        seeks = {}

        def parse(element_id, size, start):
            nonlocal duration, timecode_scale
            if element_id == INFO:
                for child_id, child_size, _ in _children(handle, start + size):
                    if child_id == 0x2AD7B1:
                        timecode_scale = _uint(handle, child_size)
                    elif child_id == 0x4489:
                        duration = _float(handle, child_size)
                    elif child_id == 0x7BA9:
                        result["title"] = _string(handle, child_size)
            elif element_id == TRACKS:
                for child_id, child_size, child_start in _children(handle, start + size):
                    if child_id != 0xAE:
                        continue
                    track = {"type": 0, "codec": "", "lang": "und"}
                    for track_id, track_size, track_start in _children(handle, child_start + child_size):
                        if track_id == 0x83:
                            track["type"] = _uint(handle, track_size)
                        elif track_id == 0x86:
                            track["codec"] = _string(handle, track_size)
                        elif track_id == 0x22B59C:
                            track["lang"] = _string(handle, track_size)
                        elif track_id == 0xE0:
                            for video_id, video_size, _ in _children(handle, track_start + track_size):
                                if video_id == 0xB0:
                                    result["width"] = _uint(handle, video_size)
                                elif video_id == 0xBA:
                                    result["height"] = _uint(handle, video_size)
                    if track["type"] == 1 and not result["video"]:
                        result["video"] = track["codec"].replace("V_", "", 1)
                    elif track["type"] == 2:
                        result["audio"].append(track["lang"] + "/" + track["codec"].replace("A_", "", 1))
                    elif track["type"] == 0x11:
                        result["subs"] += 1
            elif element_id == CHAPTERS:
                for child_id, child_size, child_start in _children(handle, start + size):
                    if child_id == 0x45B9:  # EditionEntry: count its ChapterAtoms
                        count = sum(1 for atom_id, _, _ in _children(handle, child_start + child_size) if atom_id == 0xB6)
                        result["chapters"] = max(result["chapters"], count)
            elif element_id == SEEKHEAD:
                for child_id, child_size, child_start in _children(handle, start + size):
                    if child_id != 0x4DBB:
                        continue
                    seek_id = seek_position = None
                    for key_id, key_size, _ in _children(handle, child_start + child_size):
                        if key_id == 0x53AB:
                            seek_id = int.from_bytes(handle.read(key_size), "big")
                        elif key_id == 0x53AC:
                            seek_position = _uint(handle, key_size)
                    if seek_id is not None and seek_position is not None:
                        seeks[seek_id] = seek_position

        for element_id, size, start in _children(handle, segment_end):
            if element_id == CLUSTER:
                break
            seen.add(element_id)
            parse(element_id, size, start)
        for wanted in (INFO, TRACKS, CHAPTERS):
            if wanted not in seen and wanted in seeks:
                handle.seek(segment + seeks[wanted])
                element_id = _read_id(handle)
                size = _read_size(handle)
                if element_id == wanted and size is not None:
                    parse(element_id, size, handle.tell())

    if duration is not None:
        result["seconds"] = duration * timecode_scale / 1e9
    return result


def probe_row(path):
    """
    The catalog's probe columns for one file, as strings.

    A file that is not Matroska (or cannot be read) gets its size and blank
    probe fields, so it still appears in an inventory.

    :return: dict with ``size``, ``seconds``, ``resolution``, ``video``,
             ``audio``, ``subs``, ``chapters``, ``title`` and ``probed`` (bool)
    """
    try:
        info = probe(path)
    except (NotMatroska, OSError, struct.error):
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        return {"size": str(size), "seconds": "", "resolution": "", "video": "", "audio": "", "subs": "",
                "chapters": "", "title": "", "probed": False}
    return {"size": str(info["size"]),
            "seconds": "%.0f" % info["seconds"] if info["seconds"] >= 0 else "",
            "resolution": "%dx%d" % (info["width"], info["height"]),
            "video": info["video"],
            "audio": ",".join(info["audio"]),
            "subs": str(info["subs"]),
            "chapters": str(info["chapters"]),
            "title": info["title"],
            "probed": True}


def main(argv):
    columns = ("size", "seconds", "resolution", "video", "audio", "subs", "chapters", "title")
    for root in argv:
        paths = [root] if os.path.isfile(root) else [
            os.path.join(directory, name) for directory, _, files in os.walk(root) for name in sorted(files)]
        for path in paths:
            row = probe_row(path)
            print("\t".join([path] + [row[column] for column in columns]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
