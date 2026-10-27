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

The model will name a film whether or not there is one to read. Told the photo shows a disc, a
photo of an empty tray, a bare drive or a blurred hand got "The Matrix Reloaded" at 0.95 every
time (jobs 80, 86 and 87), so its confidence says nothing about whether it read anything. The
prompt therefore asks first what the photo shows (``scene``) and which words it can actually
read (``legible_text``): a photo with no label in view ends the read, and a title made of words
the model did not list as legible is not taken.
"""
import base64
import io
import json
import logging
import re
import unicodedata
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
#: most tokens the model may answer with: a full answer is about 200. :data:`SCHEMA` caps the list
#: of legible words, which is where the fine print on a label sent the model round a loop ("ALL",
#: "R", "ALL", "R", ...) until the timeout; this is the backstop
MAX_TOKENS = 400
#: fewest letters and digits a title read off a label must have: a stylised logo read as "M"
#: would otherwise be searched, and Fritz Lang's M is exactly that title ("Up" still counts)
MIN_TITLE_CHARACTERS = 2
#: most words the model may list as legible
MAX_LEGIBLE_WORDS = 20

#: the scene of a photo with a printed disc label in view: the only one a title is taken from
LABEL_SCENE = "disc_label"
#: scenes with no label to read whichever way up the photo is: the read ends without rotating
NO_LABEL_SCENES = ("empty_tray", "obscured", "blurry")
#: title words that need not be among the legible words ("The" is often too small to read)
MINOR_WORDS = frozenset(("the", "a", "an", "of", "and"))

# other_text is what the old prompt asked for in place of legible_text: answers stored on jobs
# before the change still have it
KEYS = ("scene", "title", "subtitle", "year", "studio", "disc_number", "catalog_number", "format",
        "legible_text", "other_text", "confidence")

PROMPT = (
    "This is a webcam photo of an optical drive tray. It may or may not hold a disc, and the photo may be "
    "blurred, dark or show only the drive. Answer with JSON only, no prose, with these keys: "
    "scene (one of: 'disc_label' if a disc with a printed label is clearly visible; 'empty_tray' if the tray "
    "or drive holds no disc; 'obscured' if a hand or object covers the disc; 'blurry' if the photo is too "
    "blurred, dark or overexposed to read; 'other'), "
    f"legible_text (a list of up to {MAX_LEGIBLE_WORDS} distinct words you can actually read in the photo, "
    "title words first, each word once; empty if none), "
    "title (the film or collection title, copied only from legible_text; null if you cannot read one, never guess), "
    "subtitle (secondary title text from legible_text, or null), "
    "year (copyright or release year as printed, or null), studio (publisher/label as printed, or null), "
    "disc_number (e.g. 'Disc 3', or null), catalog_number (any product or catalogue number printed, or null), "
    "format ('DVD', 'Blu-ray' or null), "
    "confidence (0 to 1, how sure you are that the title is printed in this photo)."
)

_TEXT = {"type": ["string", "null"]}
#: Ollama's structured output: the answer must be this JSON object, so the scene is one of the
#: known ones and the legible words stop at MAX_LEGIBLE_WORDS however the model loops
SCHEMA = {
    "type": "object",
    "properties": {
        "scene": {"type": "string", "enum": [LABEL_SCENE, *NO_LABEL_SCENES, "other"]},
        "legible_text": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_LEGIBLE_WORDS},
        "title": _TEXT, "subtitle": _TEXT, "year": _TEXT, "studio": _TEXT, "disc_number": _TEXT,
        "catalog_number": _TEXT, "format": _TEXT,
        "confidence": {"type": "number"},
    },
    "required": ["scene", "legible_text", "title", "subtitle", "year", "studio", "disc_number",
                 "catalog_number", "format", "confidence"],
}


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
    four-digit year or None, ``scene`` to lower case, ``confidence`` to a float between 0 and 1
    (0 when missing). ``rotation`` (degrees the photo was turned, added by :func:`read_disc_photo`)
    is kept, so an answer stored on a job as JSON parses back to the same dict. Whether the title
    was read off a label at all is :func:`read_off_label`'s question, asked of fresh answers only:
    answers stored before the model was asked for ``scene`` still parse.
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
    answer["scene"] = answer["scene"].lower() if answer["scene"] else None
    answer["year"] = _clean_year(raw.get("year"))
    answer["confidence"] = _clean_confidence(raw.get("confidence"))
    rotation = raw.get("rotation")
    answer["rotation"] = rotation if isinstance(rotation, int) and not isinstance(rotation, bool) else None
    if not answer["title"]:
        return None
    return answer


def _words(text) -> List[str]:
    """
    The lower-case words and numbers in a text: "Jimmy Neutron: Boy Genius" -> jimmy neutron boy genius.
    Accents are folded away ("Amélie" -> amelie, "Pokémon" -> pokemon), so a title spelled with one
    in one field and without in the other still matches, and a letter with no ASCII form ("ø") is
    kept as it is rather than dropped.
    """
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    folded = "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()
    return re.findall(r"[^\W_]+", folded)


def _words_legible(text, legible) -> bool:
    """
    Whether every word of ``text`` is among the ``legible`` words, minor words ("the", "of")
    aside, unless the text is nothing but minor words.
    """
    words = _words(text)
    major = [word for word in words if word not in MINOR_WORDS] or words
    seen = set(_words(legible))
    return bool(major) and all(word in seen for word in major)


def read_off_label(answer: Optional[dict]) -> bool:
    """
    Whether a fresh answer's title was read off a disc label in the photo: the model says it
    sees one (``scene`` is :data:`LABEL_SCENE`), the title has at least
    :data:`MIN_TITLE_CHARACTERS` letters and digits, and every word of it is among the words the
    model listed as legible. An answer without a scene or legible words fails, so a model that
    ignores the prompt cannot name a disc after a guess.
    """
    if not answer or answer.get("scene") != LABEL_SCENE:
        return False
    if len("".join(_words(answer.get("title")))) < MIN_TITLE_CHARACTERS:
        return False
    return _words_legible(answer.get("title"), answer.get("legible_text"))


def scene_of(text) -> Optional[str]:
    """The ``scene`` of a raw model answer, lower case, or None; read even when there is no title."""
    try:
        raw = text if isinstance(text, dict) else json.loads(str(text))
    except (TypeError, ValueError):
        return None
    scene = _clean_text(raw.get("scene")) if isinstance(raw, dict) else None
    return scene.lower() if scene else None


class RequestFailed(Exception):
    """The request to the vision model failed: the server never gave the model's answer."""


class NoLabel(Exception):
    """The model sees no disc label to read in the photo (an empty tray, a hand, a blur)."""

    def __init__(self, scene):
        super().__init__(scene)
        self.scene = scene


def _ask(image: bytes, url: str, model: str, timeout: int) -> Optional[dict]:
    """
    Send one JPEG to the model.

    :return: the parsed answer (see :func:`parse_answer`), or None when the model answered with
             nothing usable (prose, no title, a title not read off a label: see
             :func:`read_off_label`). A subtitle not among the legible words is dropped.
    :raises RequestFailed: no connection, a timeout, an HTTP error or an envelope that is not
                           Ollama's JSON object: asking again will not help
    :raises NoLabel: the model sees no label in the photo (:data:`NO_LABEL_SCENES`): turning
                     it will not help either
    """
    body = {"model": model, "prompt": PROMPT, "images": [base64.b64encode(image).decode()],
            "stream": False, "format": SCHEMA, "options": {"temperature": 0, "num_predict": MAX_TOKENS}}
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
    text = payload.get("response")
    scene = scene_of(text)
    if scene in NO_LABEL_SCENES:
        raise NoLabel(scene)
    answer = parse_answer(text)
    if answer and not read_off_label(answer):
        logging.info(f"Vision model named {answer['title']!r} but did not read it off a disc label "
                     f"(scene {answer['scene']!r}, legible {answer['legible_text']!r}); ignoring it")
        return None
    if answer and answer["subtitle"] and not _words_legible(answer["subtitle"], answer["legible_text"]):
        logging.debug(f"Dropping subtitle {answer['subtitle']!r}: not among the legible words")
        answer["subtitle"] = None
    return answer


def ask_model(image: bytes, url: str, model: str = DEFAULT_MODEL, timeout: int = DEFAULT_TIMEOUT) -> Optional[dict]:
    """
    Send one JPEG to the model and return its parsed answer (see :func:`parse_answer`), or None
    on any HTTP, timeout or parsing failure, or when it reads no title off a disc label.
    """
    try:
        return _ask(image, url, model, timeout)
    except RequestFailed as error:
        logging.warning(f"Vision model {error}")
        return None
    except NoLabel as no_label:
        logging.info(f"Vision model sees no disc label in the photo ({no_label.scene})")
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
    ripper for up to ``timeout`` seconds more. So does a photo the model sees no label in (see
    :class:`NoLabel`): an empty tray is empty whichever way up it is.

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
        except NoLabel as no_label:
            logging.info(f"Vision model sees no disc label in the photo ({no_label.scene}); "
                         f"not trying the photo rotated")
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
