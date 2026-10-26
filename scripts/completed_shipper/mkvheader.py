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

A corrupt header (an element size or SeekHead position no real file has) is
refused instead of read: scalar elements are capped at 1 MiB and seeks at the
file's end, so a damaged file can never make this read gigabytes or raise
anything but ``NotMatroska``/``MalformedHeader``/``OSError`` from ``probe``
(``probe_row`` never raises at all).
"""

import logging
import os
import struct
import sys

INFO, TRACKS, CHAPTERS, CLUSTER, SEEKHEAD = 0x1549A966, 0x1654AE6B, 0x1043A770, 0x1F43B675, 0x114D9B74
EBML_HEADER, SEGMENT = 0x1A45DFA3, 0x18538067
TIMECODE_SCALE, DURATION, SEGMENT_TITLE = 0x2AD7B1, 0x4489, 0x7BA9
TRACK_ENTRY, TRACK_TYPE, CODEC_ID, LANGUAGE, VIDEO, PIXEL_WIDTH, PIXEL_HEIGHT = (
    0xAE, 0x83, 0x86, 0x22B59C, 0xE0, 0xB0, 0xBA)
EDITION_ENTRY, CHAPTER_ATOM = 0x45B9, 0xB6
SEEK, SEEK_ID, SEEK_POSITION = 0x4DBB, 0x53AB, 0x53AC
MEDIA_EXTENSIONS = (".mkv", ".mka", ".mks", ".mk3d", ".webm")
"""Containers this module can read; anything else is listed by size alone."""
MAX_SCALAR_SIZE = 1 << 20
"""Largest number, float or string element read (1 MiB); a bigger declared size means a corrupt header."""
MAX_EBML_OFFSET = (1 << 56) - 2
"""Largest size or position EBML can express; a SeekHead position above it is corrupt, not just past the end."""

log = logging.getLogger("mkvheader")


class NotMatroska(Exception):
    """The file does not start with an EBML header."""


class MalformedHeader(ValueError):
    """The header declares an element size or position no sane Matroska file has."""


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


def _read_scalar(handle, size):
    """The payload of a number, float or string element; refuses sizes no such element has."""
    if size < 0 or size > MAX_SCALAR_SIZE:
        raise MalformedHeader(f"scalar element of {size} bytes at offset {handle.tell()}")
    return handle.read(size)


def _uint(handle, size):
    return int.from_bytes(_read_scalar(handle, size), "big")


def _float(handle, size):
    data = _read_scalar(handle, size)
    try:
        return struct.unpack(">f", data)[0] if size == 4 else struct.unpack(">d", data)[0]
    except struct.error:
        raise MalformedHeader(f"float element of {size} bytes ({len(data)} readable)") from None


def _string(handle, size):
    return _read_scalar(handle, size).decode("utf-8", "replace").rstrip("\x00")


def _parse_info(handle, end, result, timing):
    """Segment Info: timecode scale, duration and title."""
    for child_id, child_size, _ in _children(handle, end):
        if child_id == TIMECODE_SCALE:
            timing["scale"] = _uint(handle, child_size)
        elif child_id == DURATION:
            timing["duration"] = _float(handle, child_size)
        elif child_id == SEGMENT_TITLE:
            result["title"] = _string(handle, child_size)


def _parse_track(handle, end, result):
    """One TrackEntry: the first video track's codec and picture size, audio tracks, subtitle count."""
    track = {"type": 0, "codec": "", "lang": "und"}
    for track_id, track_size, track_start in _children(handle, end):
        if track_id == TRACK_TYPE:
            track["type"] = _uint(handle, track_size)
        elif track_id == CODEC_ID:
            track["codec"] = _string(handle, track_size)
        elif track_id == LANGUAGE:
            track["lang"] = _string(handle, track_size)
        elif track_id == VIDEO:
            for video_id, video_size, _ in _children(handle, track_start + track_size):
                if video_id == PIXEL_WIDTH:
                    result["width"] = _uint(handle, video_size)
                elif video_id == PIXEL_HEIGHT:
                    result["height"] = _uint(handle, video_size)
    if track["type"] == 1 and not result["video"]:
        result["video"] = track["codec"].replace("V_", "", 1)
    elif track["type"] == 2:
        result["audio"].append(track["lang"] + "/" + track["codec"].replace("A_", "", 1))
    elif track["type"] == 0x11:
        result["subs"] += 1


def _parse_tracks(handle, end, result):
    for child_id, child_size, child_start in _children(handle, end):
        if child_id == TRACK_ENTRY:
            _parse_track(handle, child_start + child_size, result)


def _parse_chapters(handle, end, result):
    """Chapters: the ChapterAtom count of the largest EditionEntry."""
    for child_id, child_size, child_start in _children(handle, end):
        if child_id == EDITION_ENTRY:
            atoms = _children(handle, child_start + child_size)
            count = sum(1 for atom_id, _, _ in atoms if atom_id == CHAPTER_ATOM)
            result["chapters"] = max(result["chapters"], count)


def _parse_seekhead(handle, end, seeks):
    """SeekHead: ``seeks[element id] = position relative to the Segment data``."""
    for child_id, child_size, child_start in _children(handle, end):
        if child_id != SEEK:
            continue
        seek_id = seek_position = None
        for key_id, key_size, _ in _children(handle, child_start + child_size):
            if key_id == SEEK_ID:
                seek_id = _uint(handle, key_size)
            elif key_id == SEEK_POSITION:
                seek_position = _uint(handle, key_size)
        if seek_id is not None and seek_position is not None:
            seeks[seek_id] = seek_position


def _parse_element(handle, element_id, end, result, timing, seeks):
    """Read one top-level Segment child that a catalog cares about; ignore the rest."""
    if element_id == INFO:
        _parse_info(handle, end, result, timing)
    elif element_id == TRACKS:
        _parse_tracks(handle, end, result)
    elif element_id == CHAPTERS:
        _parse_chapters(handle, end, result)
    elif element_id == SEEKHEAD:
        _parse_seekhead(handle, end, seeks)


def probe(path):
    """
    Read the header of the Matroska file at ``path``.

    :return: dict with ``size`` (bytes), ``seconds`` (float, -1 when the header
             has no duration), ``width``, ``height``, ``video`` (codec id without
             the ``V_`` prefix), ``audio`` (list of ``lang/CODEC``), ``subs``
             (count), ``chapters`` (count) and ``title``
    :raises NotMatroska: when the file has no EBML header
    :raises MalformedHeader: when the header declares an absurd element size or position
    :raises OSError: when it cannot be read
    """
    result = {"size": os.path.getsize(path), "seconds": -1.0, "width": 0, "height": 0, "video": "",
              "audio": [], "subs": 0, "chapters": 0, "title": ""}
    timing = {"duration": None, "scale": 1000000}
    seeks = {}

    with open(path, "rb") as handle:
        file_size = os.fstat(handle.fileno()).st_size
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

        for element_id, size, start in _children(handle, segment_end):
            if element_id == CLUSTER:
                break
            seen.add(element_id)
            _parse_element(handle, element_id, start + size, result, timing, seeks)
        for wanted in (INFO, TRACKS, CHAPTERS):
            if wanted in seen or wanted not in seeks:
                continue
            if seeks[wanted] > MAX_EBML_OFFSET:
                raise MalformedHeader(f"SeekHead position {seeks[wanted]} for element {wanted:#x}")
            if segment + seeks[wanted] >= file_size:
                continue  # past the end of a truncated file: nothing there to read
            handle.seek(segment + seeks[wanted])
            element_id = _read_id(handle)
            size = _read_size(handle)
            if element_id == wanted and size is not None:
                _parse_element(handle, element_id, handle.tell() + size, result, timing, seeks)

    if timing["duration"] is not None:
        result["seconds"] = timing["duration"] * timing["scale"] / 1e9
    return result


def _size_only_row(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return {"size": str(size), "seconds": "", "resolution": "", "video": "", "audio": "", "subs": "",
            "chapters": "", "title": "", "probed": False}


def probe_row(path):
    """
    The catalog's probe columns for one file, as strings.  Never raises.

    A file that is not Matroska, cannot be read or has a corrupt header gets
    its size and blank probe fields, so it still appears in an inventory.  A
    corrupt header is logged as a warning.

    :return: dict with ``size``, ``seconds``, ``resolution``, ``video``,
             ``audio``, ``subs``, ``chapters``, ``title`` and ``probed`` (bool)
    """
    try:
        info = probe(path)
    except (NotMatroska, OSError):
        return _size_only_row(path)
    except Exception as err:  # optional metadata: a corrupt header must never stop the caller
        log.warning("%s: unreadable Matroska header (%s: %s); listed by size only", path, type(err).__name__, err)
        return _size_only_row(path)
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
