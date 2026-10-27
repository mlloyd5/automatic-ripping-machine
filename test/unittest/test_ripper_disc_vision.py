"""
Tests for arm.ripper.disc_vision, the Ollama vision model client, with HTTP and Pillow mocked.

The module imports nothing from the rest of ARM, so these run on any host with ``requests``.
"""
import base64
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]

# arm.ripper.disc_vision is deliberately free of ARM imports. Load it straight from its file so
# these tests do not trigger ``arm/__init__``, which loads the ARM config, the Flask app and
# the database at import time.
_spec = importlib.util.spec_from_file_location("disc_vision", REPO_ROOT / "arm" / "ripper" / "disc_vision.py")
disc_vision = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(disc_vision)

GOOD = {"scene": "disc_label", "title": "Toon Time", "subtitle": "Ultimate Cartoon Collection", "year": "2011",
        "studio": "Echo Bridge Home Entertainment", "disc_number": None, "catalog_number": "73619",
        "format": "DVD", "legible_text": ["TOON", "TIME", "Ultimate", "Cartoon", "Collection", "Echo", "Bridge"],
        "other_text": ["59 cartoons", "Casper"], "confidence": 0.95}

# What qwen2.5vl:7b answered for photos of an empty tray (jobs 86 and 87) and of a blurred hand
# (job 80) when the prompt told it the photo showed a disc: the same film, sure of it, every time
MATRIX = {"title": "The Matrix", "subtitle": "Reloaded", "year": "2003", "studio": "Warner Bros.",
          "disc_number": "Disc 2", "catalog_number": "1902956", "format": "DVD",
          "other_text": ["Special Edition", "10th Anniversary"], "confidence": 0.95}


def _label(title, confidence, **fields):
    """A model answer read off a disc label: the title's words are among the legible ones."""
    return json.dumps(dict({"scene": "disc_label", "legible_text": str(title).split(), "title": title,
                            "confidence": confidence}, **fields))


def _response(status=200, response_text=None, body=None):
    """A fake requests.Response: Ollama wraps the model's text in a JSON envelope."""
    fake = mock.Mock(spec=requests.Response)
    fake.status_code = status
    payload = body if body is not None else {"response": response_text, "total_duration": 6_000_000_000,
                                             "eval_count": 80}
    fake.json.return_value = payload
    fake.text = json.dumps(payload)
    return fake


def _image_of(call):
    """The image bytes a requests.post call sent."""
    return base64.b64decode(call.kwargs["json"]["images"][0])


class TestParseAnswer(unittest.TestCase):
    def test_good_json_is_normalised(self):
        answer = disc_vision.parse_answer(json.dumps(GOOD))
        self.assertEqual(answer["title"], "Toon Time")
        self.assertEqual(answer["subtitle"], "Ultimate Cartoon Collection")
        self.assertEqual(answer["year"], "2011")
        self.assertEqual(answer["other_text"], "59 cartoons Casper")
        self.assertEqual(answer["confidence"], 0.95)
        self.assertIsNone(answer["disc_number"])
        self.assertIsNone(answer["rotation"])
        self.assertEqual(set(answer), set(disc_vision.KEYS) | {"rotation"})

    def test_missing_keys_and_odd_values(self):
        answer = disc_vision.parse_answer('{"title": "  WALL-E  ", "year": "© 2008 Disney", "confidence": "87"}')
        self.assertEqual(answer["title"], "WALL-E")
        self.assertEqual(answer["year"], "2008")
        self.assertEqual(answer["confidence"], 0.87)
        self.assertIsNone(answer["subtitle"])
        self.assertIsNone(answer["studio"])
        self.assertEqual(disc_vision.parse_answer('{"title": "X", "confidence": "high"}')["confidence"], 0.0)
        self.assertEqual(disc_vision.parse_answer('{"title": "X", "confidence": 95}')["confidence"], 0.95)
        self.assertEqual(disc_vision.parse_answer('{"title": "X", "confidence": 700}')["confidence"], 1.0)
        self.assertEqual(disc_vision.parse_answer('{"title": "X", "confidence": -2}')["confidence"], 0.0)
        self.assertEqual(disc_vision.parse_answer('{"title": "X", "confidence": "95%"}')["confidence"], 0.95)
        self.assertEqual(disc_vision.parse_answer('{"title": "X", "confidence": " 80 % "}')["confidence"], 0.8)
        self.assertEqual(disc_vision.parse_answer('{"title": "X", "confidence": "0.5%"}')["confidence"], 0.005)
        self.assertEqual(disc_vision.parse_answer('{"title": "X", "confidence": "%"}')["confidence"], 0.0)
        self.assertIsNone(disc_vision.parse_answer('{"title": "X", "year": "the 90s"}')["year"])
        self.assertIsNone(disc_vision.parse_answer('{"title": "X", "subtitle": "null"}')["subtitle"])

    def test_not_json_not_an_object_or_no_title(self):
        self.assertIsNone(disc_vision.parse_answer("The disc is WALL-E (2008)."))
        self.assertIsNone(disc_vision.parse_answer("[1, 2]"))
        self.assertIsNone(disc_vision.parse_answer('{"subtitle": "only"}'))
        self.assertIsNone(disc_vision.parse_answer('{"title": null}'))
        self.assertIsNone(disc_vision.parse_answer(None))
        self.assertIsNone(disc_vision.parse_answer(""))

    def test_stored_answer_round_trips(self):
        stored = dict(GOOD, rotation=90)
        again = disc_vision.parse_answer(json.dumps(disc_vision.parse_answer(stored)))
        self.assertEqual(again["rotation"], 90)
        self.assertEqual(again["title"], "Toon Time")

    def test_search_titles_and_label_title(self):
        answer = disc_vision.parse_answer(GOOD)
        self.assertEqual(disc_vision.search_titles(answer), ["Toon Time", "Toon Time: Ultimate Cartoon Collection"])
        self.assertEqual(disc_vision.label_title(answer), ("Toon Time: Ultimate Cartoon Collection", "2011"))
        plain = disc_vision.parse_answer({"title": "WALL-E", "confidence": 0.6})
        self.assertEqual(disc_vision.search_titles(plain), ["WALL-E"])
        self.assertEqual(disc_vision.label_title(plain), ("WALL-E", None))
        unsure = disc_vision.parse_answer({"title": "Something", "confidence": 0.3})
        self.assertEqual(disc_vision.label_title(unsure), (None, None))
        self.assertEqual(disc_vision.search_titles(None), [])
        self.assertEqual(disc_vision.label_title(None), (None, None))

    def test_generate_url(self):
        self.assertEqual(disc_vision.generate_url("http://192.168.68.105:11434"),
                         "http://192.168.68.105:11434/api/generate")
        self.assertEqual(disc_vision.generate_url("http://host:11434/"), "http://host:11434/api/generate")
        self.assertEqual(disc_vision.generate_url("http://host:11434/api/generate"), "http://host:11434/api/generate")


class TestReadDiscPhoto(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arm_vision_test_")
        self.photo = os.path.join(self.tmp, "sr1_20261026-120000.jpg")
        with open(self.photo, "wb") as handle:
            handle.write(b"\xff\xd8 not really a jpeg")
        # Pillow is not needed: the fake rotation tags the bytes so the fake server can tell them apart
        self.rotate = mock.patch.object(disc_vision, "rotate_jpeg",
                                        side_effect=lambda data, degrees: f"rot{degrees}:".encode() + data)
        self.rotate.start()

    def tearDown(self):
        self.rotate.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_confident_answer_is_taken_as_is(self):
        with mock.patch.object(disc_vision.requests, "post",
                               return_value=_response(response_text=json.dumps(GOOD))) as post:
            answer = disc_vision.read_disc_photo(self.photo, "http://host:11434", "qwen2.5vl:7b", 60)
        self.assertEqual(answer["title"], "Toon Time")
        self.assertEqual(answer["rotation"], 0)
        self.assertEqual(post.call_count, 1)
        body = post.call_args.kwargs["json"]
        self.assertEqual(post.call_args.args[0], "http://host:11434/api/generate")
        self.assertEqual(post.call_args.kwargs["timeout"], (disc_vision.CONNECT_TIMEOUT, 60))
        self.assertEqual((body["model"], body["stream"], body["format"], body["options"]),
                         ("qwen2.5vl:7b", False, disc_vision.SCHEMA,
                          {"temperature": 0, "num_predict": disc_vision.MAX_TOKENS}))
        self.assertEqual(body["format"]["properties"]["legible_text"]["maxItems"], disc_vision.MAX_LEGIBLE_WORDS)
        self.assertIn("empty_tray", body["format"]["properties"]["scene"]["enum"])
        self.assertEqual(_image_of(post.call_args), b"\xff\xd8 not really a jpeg")
        self.assertIn("JSON only", body["prompt"])

    def test_rotation_retry_keeps_the_best_answer(self):
        """The Peanuts Movie: sideways label read as one unsure word, right way up at 270 degrees."""
        def answer_for(url, json=None, timeout=None):
            image = base64.b64decode(json["images"][0])
            if image.startswith(b"rot90:"):
                return _response(response_text=_label("Peanuts", 0.5))
            if image.startswith(b"rot270:"):
                return _response(response_text=_label("The Peanuts Movie", 0.92, year="2015"))
            return _response(response_text=_label("Snoopy", 0.4))

        with mock.patch.object(disc_vision.requests, "post", side_effect=answer_for) as post:
            answer = disc_vision.read_disc_photo(self.photo, "http://host:11434")
        self.assertEqual(post.call_count, 3)
        self.assertEqual((answer["title"], answer["year"], answer["rotation"]), ("The Peanuts Movie", "2015", 270))

    def test_retry_stops_at_the_first_confident_answer(self):
        def answer_for(url, json=None, timeout=None):
            image = base64.b64decode(json["images"][0])
            if image.startswith(b"rot90:"):
                return _response(response_text=_label("Room on the Broom", 0.95))
            return _response(response_text=_label("Broom", 0.3))

        with mock.patch.object(disc_vision.requests, "post", side_effect=answer_for) as post:
            answer = disc_vision.read_disc_photo(self.photo, "http://host:11434")
        self.assertEqual(post.call_count, 2)
        self.assertEqual((answer["title"], answer["rotation"]), ("Room on the Broom", 90))

    def test_one_word_title_triggers_a_retry_but_still_wins_when_best(self):
        def answer_for(url, json=None, timeout=None):
            image = base64.b64decode(json["images"][0])
            if image.startswith(b"rot"):
                return _response(response_text=_label("Tarzan", 0.7))
            return _response(response_text=_label("Tarzan", 0.98))

        with mock.patch.object(disc_vision.requests, "post", side_effect=answer_for) as post:
            answer = disc_vision.read_disc_photo(self.photo, "http://host:11434")
        self.assertEqual(post.call_count, 3)
        self.assertEqual((answer["title"], answer["confidence"], answer["rotation"]), ("Tarzan", 0.98, 0))

    def test_non_json_answers_give_none(self):
        with mock.patch.object(disc_vision.requests, "post",
                               return_value=_response(response_text="I think this is WALL-E.")) as post:
            self.assertIsNone(disc_vision.read_disc_photo(self.photo, "http://host:11434"))
        self.assertEqual(post.call_count, 3)  # the model answered, just not usefully: every rotation was tried
        # an envelope that is not Ollama's JSON object is the server's failure, not the model's
        with mock.patch.object(disc_vision.requests, "post", return_value=_response(body="not an object")) as post:
            self.assertIsNone(disc_vision.read_disc_photo(self.photo, "http://host:11434"))
        self.assertEqual(post.call_count, 1)
        broken = _response()
        broken.json.side_effect = ValueError("no json")
        with mock.patch.object(disc_vision.requests, "post", return_value=broken) as post:
            self.assertIsNone(disc_vision.read_disc_photo(self.photo, "http://host:11434"))
        self.assertEqual(post.call_count, 1)

    def test_timeout_and_http_errors_give_none_without_retrying(self):
        """A failed request is not an unsure read: the rotations would only multiply the wait."""
        for failure in (dict(side_effect=requests.Timeout("too slow")),
                        dict(side_effect=requests.ConnectionError("down")),
                        dict(return_value=_response(status=404, body={"error": "model not found"})),
                        dict(return_value=_response(status=500, body={"error": "out of memory"}))):
            with mock.patch.object(disc_vision.requests, "post", **failure) as post:
                self.assertIsNone(disc_vision.read_disc_photo(self.photo, "http://host:11434", timeout=5))
            self.assertEqual(post.call_count, 1, failure)
            self.assertEqual(post.call_args.kwargs["timeout"], (disc_vision.CONNECT_TIMEOUT, 5))

    def test_a_failure_after_an_unsure_answer_keeps_that_answer(self):
        """The server went away while the rotated photo was being read: what it said first still counts."""
        def answer_for(url, json=None, timeout=None):
            image = base64.b64decode(json["images"][0])
            if image.startswith(b"rot"):
                raise requests.Timeout("too slow")
            return _response(response_text=_label("WALL-E", 0.8, year=2008))

        with mock.patch.object(disc_vision.requests, "post", side_effect=answer_for) as post:
            answer = disc_vision.read_disc_photo(self.photo, "http://host:11434")
        self.assertEqual(post.call_count, 2)  # as it is, then 90 degrees, which timed out: no 270
        self.assertEqual((answer["title"], answer["year"], answer["rotation"]), ("WALL-E", "2008", 0))

    def test_one_good_answer_among_unusable_ones(self):
        """Sideways the label reads as nothing the model can name; the right way up it is clear."""
        def answer_for(url, json=None, timeout=None):
            image = base64.b64decode(json["images"][0])
            if image.startswith(b"rot270:"):
                return _response(response_text=_label("WALL-E", 0.8, year=2008))
            return _response(response_text='{"title": null, "other_text": ["DISNEY", "PIXAR"]}')

        with mock.patch.object(disc_vision.requests, "post", side_effect=answer_for) as post:
            answer = disc_vision.read_disc_photo(self.photo, "http://host:11434")
        self.assertEqual(post.call_count, 3)
        self.assertEqual((answer["title"], answer["year"], answer["rotation"]), ("WALL-E", "2008", 270))

    def test_envelope_without_a_duration_is_still_read(self):
        """Ollama may send "total_duration": null (or leave it out): only the log line uses it."""
        for envelope in ({"response": json.dumps(GOOD), "total_duration": None, "eval_count": None},
                         {"response": json.dumps(GOOD)},
                         {"response": json.dumps(GOOD), "total_duration": "6s"}):
            with mock.patch.object(disc_vision.requests, "post", return_value=_response(body=envelope)):
                answer = disc_vision.read_disc_photo(self.photo, "http://host:11434")
            self.assertEqual((answer["title"], answer["confidence"]), ("Toon Time", 0.95), envelope)

    def test_ask_model_gives_none_on_a_failed_request(self):
        with mock.patch.object(disc_vision.requests, "post", side_effect=requests.ConnectionError("down")):
            self.assertIsNone(disc_vision.ask_model(b"jpeg", "http://host:11434"))
        with mock.patch.object(disc_vision.requests, "post", return_value=_response(response_text=json.dumps(GOOD))):
            self.assertEqual(disc_vision.ask_model(b"jpeg", "http://host:11434")["title"], "Toon Time")

    def test_missing_photo_or_rotation_gives_none_without_raising(self):
        with mock.patch.object(disc_vision.requests, "post") as post:
            self.assertIsNone(disc_vision.read_disc_photo(os.path.join(self.tmp, "missing.jpg"), "http://host:11434"))
        post.assert_not_called()
        # no Pillow: only the photo as it is can be asked
        self.rotate.stop()
        with mock.patch.object(disc_vision, "rotate_jpeg", return_value=None), \
                mock.patch.object(disc_vision.requests, "post",
                                  return_value=_response(
                                      response_text=_label("Tarzan", 0.5))) as post:
            answer = disc_vision.read_disc_photo(self.photo, "http://host:11434")
        self.rotate.start()
        self.assertEqual(post.call_count, 1)
        self.assertEqual((answer["title"], answer["rotation"]), ("Tarzan", 0))

    def test_unexpected_error_gives_none(self):
        with mock.patch.object(disc_vision.requests, "post", side_effect=RuntimeError("boom")):
            self.assertIsNone(disc_vision.read_disc_photo(self.photo, "http://host:11434"))

    def test_no_label_in_view_ends_the_read_without_rotating(self):
        """An empty tray, a hand or a blur: the answers qwen2.5vl gave for jobs 86, 87 and 80 with this prompt."""
        for scene in ("empty_tray", "obscured", "blurry", "Empty_Tray"):
            text = json.dumps({"scene": scene, "legible_text": [], "title": None, "confidence": 0.0})
            with mock.patch.object(disc_vision.requests, "post", return_value=_response(response_text=text)) as post:
                self.assertIsNone(disc_vision.read_disc_photo(self.photo, "http://host:11434"))
            self.assertEqual(post.call_count, 1, scene)
        # a title alongside a no-label scene is the guess the scene gives away
        text = json.dumps(dict(MATRIX, scene="empty_tray", legible_text=[]))
        with mock.patch.object(disc_vision.requests, "post", return_value=_response(response_text=text)) as post:
            self.assertIsNone(disc_vision.read_disc_photo(self.photo, "http://host:11434"))
        self.assertEqual(post.call_count, 1)

    def test_title_not_among_the_legible_words_is_not_taken(self):
        """The Matrix with nothing legible, or with words that are not its title, is a guess."""
        for text in (json.dumps(MATRIX),  # the old answer: no scene, no legible words
                     json.dumps(dict(MATRIX, scene="disc_label")),
                     json.dumps(dict(MATRIX, scene="disc_label", legible_text=[])),
                     json.dumps(dict(MATRIX, scene="disc_label", legible_text=["ANNE", "OF", "AVONLEA", "BBC"])),
                     json.dumps(dict(MATRIX, scene="other", legible_text=["THE", "MATRIX"]))):
            with mock.patch.object(disc_vision.requests, "post", return_value=_response(response_text=text)) as post:
                self.assertIsNone(disc_vision.read_disc_photo(self.photo, "http://host:11434"), text)
            self.assertEqual(post.call_count, 3, text)  # a scene with a label in it is worth turning

    def test_title_read_off_the_label_is_taken(self):
        """Almost Famous and Jimmy Neutron, as qwen2.5vl read them from real photos with this prompt."""
        almost = json.dumps({"scene": "disc_label",
                             "legible_text": ["Almost", "Famous", "DVD", "HOME", "ENTERTAINMENT"],
                             "title": "Almost Famous", "year": None, "confidence": 0.9})
        neutron = json.dumps({"scene": "disc_label", "legible_text": ["JIMMY", "NEUTRON", "BOY", "GENIUS", "DVD"],
                              "title": "Jimmy Neutron: Boy Genius", "confidence": 0.95})
        for text, title in ((almost, "Almost Famous"), (neutron, "Jimmy Neutron: Boy Genius")):
            with mock.patch.object(disc_vision.requests, "post", return_value=_response(response_text=text)):
                answer = disc_vision.read_disc_photo(self.photo, "http://host:11434")
            self.assertEqual((answer["title"], answer["scene"]), (title, "disc_label"))

    def test_minor_words_need_not_be_legible_but_a_subtitle_must_be(self):
        text = json.dumps({"scene": "disc_label", "legible_text": ["PEANUTS", "MOVIE", "2015"],
                           "title": "The Peanuts Movie", "subtitle": "Snoopy's Grand Adventure",
                           "year": "2015", "confidence": 0.93})
        with mock.patch.object(disc_vision.requests, "post", return_value=_response(response_text=text)):
            answer = disc_vision.read_disc_photo(self.photo, "http://host:11434")
        self.assertEqual((answer["title"], answer["subtitle"]), ("The Peanuts Movie", None))
        kept = _label("Toon Time", 0.95, subtitle="Ultimate Cartoon Collection",
                      legible_text=["TOON", "TIME", "ULTIMATE", "CARTOON", "COLLECTION"])
        with mock.patch.object(disc_vision.requests, "post", return_value=_response(response_text=kept)):
            answer = disc_vision.read_disc_photo(self.photo, "http://host:11434")
        self.assertEqual(answer["subtitle"], "Ultimate Cartoon Collection")

    def test_no_label_on_a_turned_photo_keeps_the_earlier_answer(self):
        def answer_for(url, json=None, timeout=None):
            image = base64.b64decode(json["images"][0])
            if image.startswith(b"rot"):
                return _response(response_text='{"scene": "blurry", "legible_text": [], "title": null}')
            return _response(response_text=_label("WALL-E", 0.8))

        with mock.patch.object(disc_vision.requests, "post", side_effect=answer_for) as post:
            answer = disc_vision.read_disc_photo(self.photo, "http://host:11434")
        self.assertEqual(post.call_count, 2)
        self.assertEqual((answer["title"], answer["rotation"]), ("WALL-E", 0))

    def test_ask_model_gives_none_for_an_empty_tray(self):
        text = '{"scene": "empty_tray", "legible_text": [], "title": null}'
        with mock.patch.object(disc_vision.requests, "post", return_value=_response(response_text=text)):
            self.assertIsNone(disc_vision.ask_model(b"jpeg", "http://host:11434"))


class TestReadOffLabel(unittest.TestCase):
    def test_scene_and_legible_words(self):
        self.assertTrue(disc_vision.read_off_label(disc_vision.parse_answer(GOOD)))
        self.assertFalse(disc_vision.read_off_label(disc_vision.parse_answer(MATRIX)))
        self.assertFalse(disc_vision.read_off_label(None))
        wall_e = disc_vision.parse_answer(_label("WALL-E", 0.8, legible_text=["WALL", "E", "Disney"]))
        self.assertTrue(disc_vision.read_off_label(wall_e))
        # a title of nothing but minor words still has to be legible
        self.assertFalse(disc_vision.read_off_label(disc_vision.parse_answer(_label("The", 0.9, legible_text=["X"]))))
        self.assertTrue(disc_vision.read_off_label(disc_vision.parse_answer(_label("The", 0.9))))
        # Men in Black's logo read as "M" (job 79's photo): too short to be a title worth searching
        men_in_black = _label("M", 0.8, subtitle="DVD", legible_text=["M", "DVD", "R", "R"])
        self.assertFalse(disc_vision.read_off_label(disc_vision.parse_answer(men_in_black)))
        self.assertTrue(disc_vision.read_off_label(disc_vision.parse_answer(_label("Up", 0.9))))

    def test_accents_fold_before_the_words_are_compared(self):
        amelie = _label("Amélie", 0.9, legible_text=["AMELIE", "Miramax"])
        self.assertTrue(disc_vision.read_off_label(disc_vision.parse_answer(amelie)))
        pokemon = _label("Pokemon: The First Movie", 0.9, legible_text=["POKéMON", "THE", "FIRST", "MOVIE"])
        self.assertTrue(disc_vision.read_off_label(disc_vision.parse_answer(pokemon)))
        self.assertEqual(disc_vision._words("Léon: The Professional"), ["leon", "the", "professional"])
        # a title of accented letters only still counts its letters (not 0) against MIN_TITLE_CHARACTERS
        self.assertTrue(disc_vision.read_off_label(disc_vision.parse_answer(_label("Ôé", 0.9, legible_text=["OE"]))))
        self.assertEqual(disc_vision._words("Ø_snake"), ["ø", "snake"])

    def test_scene_of(self):
        self.assertEqual(disc_vision.scene_of('{"scene": " Empty_Tray "}'), "empty_tray")
        self.assertEqual(disc_vision.scene_of({"scene": "blurry"}), "blurry")
        self.assertIsNone(disc_vision.scene_of(json.dumps(MATRIX)))
        self.assertIsNone(disc_vision.scene_of("not json"))
        self.assertIsNone(disc_vision.scene_of("[1]"))
        self.assertIsNone(disc_vision.scene_of(None))

    def test_answers_stored_before_the_scene_still_parse(self):
        """Jobs keep the old prompt's answers: the title fix pages and naming still read them."""
        stored = disc_vision.parse_answer(json.dumps(dict(MATRIX, rotation=0)))
        self.assertEqual((stored["title"], stored["scene"], stored["legible_text"]), ("The Matrix", None, None))
        self.assertEqual(disc_vision.parse_answer(GOOD)["scene"], "disc_label")
        self.assertEqual(disc_vision.parse_answer(GOOD)["legible_text"],
                         "TOON TIME Ultimate Cartoon Collection Echo Bridge")

    def test_prompt_asks_what_the_photo_shows(self):
        for words in ("scene", "empty_tray", "obscured", "blurry", "legible_text", "never guess", "JSON only"):
            self.assertIn(words, disc_vision.PROMPT)
        self.assertNotIn("This is a photo of a DVD", disc_vision.PROMPT)


class TestRotateJpeg(unittest.TestCase):
    def test_without_pillow_or_with_bad_data(self):
        with mock.patch.dict("sys.modules", {"PIL": None, "PIL.Image": None}):
            self.assertIsNone(disc_vision.rotate_jpeg(b"\xff\xd8", 90))
        try:
            import PIL  # noqa: F401
        except ImportError:
            return
        self.assertIsNone(disc_vision.rotate_jpeg(b"not an image", 90))

    def test_rotates_when_pillow_is_there(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        import io
        buffer = io.BytesIO()
        Image.new("RGB", (40, 20), "white").save(buffer, format="JPEG")
        rotated = disc_vision.rotate_jpeg(buffer.getvalue(), 90)
        with Image.open(io.BytesIO(rotated)) as image:
            self.assertEqual(image.size, (20, 40))


if __name__ == "__main__":
    unittest.main()
