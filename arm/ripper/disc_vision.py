#!/usr/bin/env python3
"""
Read a disc label from the webcam photo with a vision model served by Ollama.

Tesseract reads nothing useful off a 640x480 webcam frame of a printed disc, but a small
multimodal model (``qwen2.5vl:7b`` on a single GPU) reads the title, subtitle, year and studio
from the same photo in a few seconds. This module is the client: it sends the JPEG to Ollama's
``/api/generate`` endpoint and parses the JSON the model answers with.

Everything here is defensive. The model may answer with half the keys, with prose, or not at
all; the server may be down or slow. :func:`read_disc_photo` returns None on any failure and
never raises, so the ripper can call it without guarding every step. A request that fails (no
connection, a timeout, an HTTP error such as 404 for a model that is not pulled) ends the read
at once: only an answer the model gave but was unsure of is worth asking again rotated.
"""
import base64
import io
import json
import logging
from typing import List, Optional, Tuple

import requests

DEFAULT_MODEL = "qwen2.5vl:7b"
DEFAULT_TIMEOUT = 120
#: seconds to wait for the connection to the server: an unreachable host should fail fast, not
#: after DISC_PHOTO_VISION_TIMEOUT, which is how long the model may take to answer
CONNECT_TIMEOUT = 5
#: a lower confidence than this, or a one-word title, makes us try the photo rotated
RETRY_CONFIDENCE = 0.9
#: labels are often printed sideways in the tray: try these rotations (degrees, counter-clockwise)
ROTATIONS = (90, 270)
#: a title read with less confidence than this does not name an unidentified disc
NAME_CONFIDENCE = 0.5

KEYS = ("title", "subtitle", "year", "studio", "disc_number", "catalog_number", "format",
        "other_text", "confidence")

PROMPT = (
    "This is a photo of a DVD or Blu-ray disc sitting in an open drive tray, taken by a webcam. "
    "Read the printed label. Answer with JSON only, no prose, with these keys: "
    "title (the film or collection title as printed), subtitle (secondary title text or null), "
    "year (copyright or release year as printed, or null), studio (publisher/label, or null), "
    "disc_number (e.g. 'Disc 3', or null), catalog_number (any product or catalogue number printed, or null), "
    "format ('DVD', 'Blu-ray' or null), other_text (a short list of other legible words), "
    "confidence (0 to 1, how sure you are of the title)."
)


def generate_url(url: str) -> str:
    """
    The ``/api/generate`` endpoint for a configured Ollama URL.

    ``DISC_PHOTO_VISION_URL`` may be the server (``http://host:11434``) or the endpoint itself.
    """
    url = str(url or "").strip().rstrip("/")
    if url.endswith("/api/generate"):
        return url
    return url + "/api/generate"


def rotate_jpeg(data: bytes, degrees: int) -> Optional[bytes]:
    """
    The JPEG rotated counter-clockwise by ``degrees``, or None when Pillow is missing or the
    image cannot be decoded. Pillow comes with the sleeve label extras (requirements-labels.txt),
    so it is imported here rather than at module level.
    """
    try:
        from PIL import Image  # pylint: disable=import-outside-toplevel
    except ImportError:
        logging.debug("Pillow is not installed, cannot rotate the disc photo")
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            rotated = image.rotate(degrees, expand=True)
            buffer = io.BytesIO()
            rotated.convert("RGB").save(buffer, format="JPEG", quality=90)
            return buffer.getvalue()
    except Exception as error:  # a truncated or non-JPEG file
        logging.debug(f"Could not rotate the disc photo: {error}")
        return None


def _clean_text(value) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (list, tuple)):
        value = " ".join(str(part) for part in value if part is not None)
    text = " ".join(str(value).split())
    if not text or text.lower() in ("null", "none", "n/a", "unknown"):
        return None
    return text


def _clean_year(value) -> Optional[str]:
    text = _clean_text(value)
    if not text:
        return None
    digits = "".join(character for character in text if character.isdigit())
    for start in range(0, len(digits) - 3):
        year = digits[start:start + 4]
        if year.startswith(("19", "20")):
            return year
    return None


def _clean_confidence(value) -> float:
    percent = isinstance(value, str) and value.strip().endswith("%")
    if percent:  # "95%"
        value = value.strip()[:-1]
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    if confidence != confidence:  # NaN
        return 0.0
    if percent:
        confidence = confidence / 100
    elif confidence > 1:  # "95" rather than 0.95
        confidence = confidence / 100 if confidence <= 100 else 1.0
    return max(0.0, min(1.0, confidence))


def parse_answer(text) -> Optional[dict]:
    """
    The model's answer as a dict with every key in :data:`KEYS`, or None when it is not JSON,
    not an object, or has no title.

    Text fields are collapsed to single-line strings (None when blank or "null"), ``year`` to a
    four-digit year or None, ``confidence`` to a float between 0 and 1 (0 when missing).
    ``rotation`` (degrees the photo was turned, added by :func:`read_disc_photo`) is kept, so an
    answer stored on a job as JSON parses back to the same dict.
    """
    if isinstance(text, dict):
        raw = text
    else:
        try:
            raw = json.loads(str(text))
        except (TypeError, ValueError):
            logging.debug(f"Vision model did not answer with JSON: {str(text)[:200]!r}")
            return None
    if not isinstance(raw, dict):
        return None
    answer = {key: _clean_text(raw.get(key)) for key in KEYS}
    answer["year"] = _clean_year(raw.get("year"))
    answer["confidence"] = _clean_confidence(raw.get("confidence"))
    rotation = raw.get("rotation")
    answer["rotation"] = rotation if isinstance(rotation, int) and not isinstance(rotation, bool) else None
    if not answer["title"]:
        return None
    return answer


class RequestFailed(Exception):
    """The request to the vision model failed: the server never gave the model's answer."""


def _ask(image: bytes, url: str, model: str, timeout: int) -> Optional[dict]:
    """
    Send one JPEG to the model.

    :return: the parsed answer (see :func:`parse_answer`), or None when the model answered with
             nothing usable (prose, no title)
    :raises RequestFailed: no connection, a timeout, an HTTP error or an envelope that is not
                           Ollama's JSON object: asking again will not help
    """
    body = {"model": model, "prompt": PROMPT, "images": [base64.b64encode(image).decode()],
            "stream": False, "format": "json", "options": {"temperature": 0}}
    try:
        response = requests.post(generate_url(url), json=body, timeout=(CONNECT_TIMEOUT, timeout))
    except requests.RequestException as error:
        raise RequestFailed(f"request to {url} failed: {error}") from error
    if response.status_code != 200:
        raise RequestFailed(f"{url} returned HTTP {response.status_code}: {response.text[:200]!r}")
    try:
        payload = response.json()
    except ValueError as error:
        raise RequestFailed(f"{url} returned something that is not JSON") from error
    if not isinstance(payload, dict):
        raise RequestFailed(f"{url} returned JSON that is not an object: {str(payload)[:200]!r}")
    duration = payload.get("total_duration")
    took = f"{duration / 1e9:.1f}s" if isinstance(duration, (int, float)) and not isinstance(duration, bool) else "?"
    logging.debug(f"Vision model took {took} ({payload.get('eval_count')} tokens)")
    return parse_answer(payload.get("response"))


def ask_model(image: bytes, url: str, model: str = DEFAULT_MODEL, timeout: int = DEFAULT_TIMEOUT) -> Optional[dict]:
    """
    Send one JPEG to the model and return its parsed answer (see :func:`parse_answer`), or None
    on any HTTP, timeout or parsing failure.
    """
    try:
        return _ask(image, url, model, timeout)
    except RequestFailed as error:
        logging.warning(f"Vision model {error}")
        return None


def needs_retry(answer: Optional[dict]) -> bool:
    """Whether an answer is doubtful enough to try the photo rotated: no answer, unsure, or one word."""
    if not answer:
        return True
    return answer["confidence"] < RETRY_CONFIDENCE or len(str(answer["title"]).split()) < 2


def read_disc_photo(photo_path, url: str, model: str = DEFAULT_MODEL,
                    timeout: int = DEFAULT_TIMEOUT) -> Optional[dict]:
    """
    Ask the vision model what the disc in ``photo_path`` is.

    The photo is sent as it is first. Labels are often printed sideways in the tray, so when the
    answer is doubtful (see :func:`needs_retry`) it is sent again rotated 90 and 270 degrees and
    the answer with the highest confidence wins; a confident answer stops the retries early. A
    failed request (see :class:`RequestFailed`) stops them too: a server that is down, too slow or
    without the model will not do better with the photo turned, and each retry would hold up the
    ripper for up to ``timeout`` seconds more.

    :return: the best answer (see :func:`parse_answer`) with a ``rotation`` key (degrees), or
             None when the photo cannot be read or the model never answered usefully
    """
    try:
        with open(photo_path, "rb") as handle:
            image = handle.read()
    except OSError as error:
        logging.warning(f"Could not read disc photo {photo_path}: {error}")
        return None
    try:
        return _read_rotations(image, url, model, timeout)
    except Exception as error:  # belt and braces: identification must never stop on this step
        logging.error(f"Vision model lookup failed: {error}", exc_info=True)
        return None


def _read_rotations(image: bytes, url: str, model: str, timeout: int) -> Optional[dict]:
    answers = []
    for degrees in (0,) + ROTATIONS:
        rotated = image if degrees == 0 else rotate_jpeg(image, degrees)
        if rotated is None:
            continue
        try:
            answer = _ask(rotated, url, model, timeout)
        except RequestFailed as error:
            logging.warning(f"Vision model {error}; not trying the photo rotated")
            break
        if answer:
            answer["rotation"] = degrees
            answers.append(answer)
            logging.debug(f"Vision model read {answer['title']!r} (confidence {answer['confidence']:.2f}) "
                          f"with the photo rotated {degrees} degrees")
        if not needs_retry(answer):
            break
    if not answers:
        return None
    return max(answers, key=lambda candidate: candidate["confidence"])


def search_titles(answer: Optional[dict]) -> List[str]:
    """
    The titles worth searching online for an answer: the title alone and, when there is a
    subtitle, the title with it ("Toon Time: Ultimate Cartoon Collection").
    """
    if not answer or not answer.get("title"):
        return []
    titles = [answer["title"]]
    if answer.get("subtitle") and answer["subtitle"].lower() != answer["title"].lower():
        titles.append(f"{answer['title']}: {answer['subtitle']}")
    return titles


def label_title(answer: Optional[dict]) -> Tuple[Optional[str], Optional[str]]:
    """
    The name a disc should get when nothing online matches what its label says: the full
    title with the subtitle, and the year, as read. Nothing when the model was not at least
    :data:`NAME_CONFIDENCE` sure of the title.
    """
    if not answer or not answer.get("title") or answer.get("confidence", 0) < NAME_CONFIDENCE:
        return None, None
    titles = search_titles(answer)
    return titles[-1], answer.get("year")
