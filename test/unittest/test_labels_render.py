"""Tests for arm/labels/render.py: label geometry, text lines and the bitmap itself. Needs Pillow and qrcode."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location("arm_labels_render", REPO_ROOT / "arm" / "labels" / "render.py")
render = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render)

try:
    import PIL  # noqa: F401
    import qrcode  # noqa: F401
    HAVE_LIBS = True
except ImportError:  # pragma: no cover - depends on the host
    HAVE_LIBS = False


class TestGeometry(unittest.TestCase):
    def test_print_height_per_tape(self):
        self.assertEqual(render.print_height(18), 112)
        self.assertEqual(render.print_height(12), 70)
        self.assertEqual(render.print_height("24"), 128)

    def test_unknown_tape_is_an_error(self):
        with self.assertRaises(ValueError):
            render.print_height(13)
        with self.assertRaises(ValueError):
            render.print_height("wide")

    def test_mm_to_px_at_180_dpi(self):
        self.assertEqual(render.mm_to_px(25.4), 180)
        self.assertEqual(render.mm_to_px(90), 638)

    def test_find_font_prefers_given_path(self):
        with tempfile.NamedTemporaryFile(suffix=".ttf") as handle:
            self.assertEqual(render.find_font(handle.name), handle.name)
        # A missing path falls through to the candidates (or None), never raises
        result = render.find_font("/nonexistent/font.ttf")
        self.assertTrue(result is None or os.path.isfile(result))


class TestTextLines(unittest.TestCase):
    def test_title_year_and_type(self):
        self.assertEqual(render.label_text_lines("Jaws", "1975", "Blu-ray"), ["Jaws (1975)", "Blu-ray"])

    def test_missing_pieces_are_left_out(self):
        self.assertEqual(render.label_text_lines("Jaws", None, None), ["Jaws"])
        self.assertEqual(render.label_text_lines(None, "", "DVD"), ["DVD"])
        self.assertEqual(render.label_text_lines("", "0000", ""), [])

    def test_disc_numbers(self):
        self.assertEqual(render.label_text_lines("LOTR", "2003", "Blu-ray", 2, 3),
                         ["LOTR (2003)", "Blu-ray · disc 2 of 3"])
        self.assertEqual(render.label_text_lines("LOTR", "2003", None, 1), ["LOTR (2003)", "disc 1"])


@unittest.skipUnless(HAVE_LIBS, "Pillow and qrcode are not installed")
class TestRender(unittest.TestCase):
    def test_label_is_the_print_height_and_one_bit(self):
        image = render.render_label("S0042", ["Jaws (1975)", "Blu-ray"], tape_mm=18)
        self.assertEqual(image.height, 112)
        self.assertEqual(image.mode, "1")
        self.assertGreaterEqual(image.width, render.mm_to_px(36))
        self.assertLessEqual(image.width, render.mm_to_px(90))

    def test_narrow_tape(self):
        image = render.render_label("S0001", ["Jaws (1975)", "DVD"], tape_mm=12)
        self.assertEqual(image.height, 70)

    def test_long_title_never_exceeds_max_length(self):
        title = "An Extremely Long Title That Goes On And On And On Without Any End In Sight At All (1999)"
        image = render.render_label("S9999", [title, "Blu-ray · disc 2 of 2"], tape_mm=18, max_mm=60)
        self.assertLessEqual(image.width, render.mm_to_px(60))

    def test_short_content_is_padded_to_min_length(self):
        image = render.render_label("S1", [], tape_mm=18, min_mm=50)
        self.assertEqual(image.width, render.mm_to_px(50))

    def test_qr_has_ink_and_fills_the_band(self):
        band = render.qr_image("S0042", 112)
        self.assertEqual(band.height, 112)
        self.assertIn(0, band.getdata())  # black modules present
        self.assertEqual(band.getpixel((0, 0)), 255 if band.getpixel((0, 0)) == 255 else 1)  # quiet zone is white

    def test_qr_too_small_is_an_error(self):
        with self.assertRaises(ValueError):
            render.qr_image("S0042", 20)

    def test_save_png_with_dpi(self):
        from PIL import Image
        image = render.render_label("S0007", ["Alien (1979)", "UHD"], tape_mm=18)
        with tempfile.TemporaryDirectory() as folder:
            path = render.save_png(image, os.path.join(folder, "label.png"))
            with Image.open(path) as saved:
                self.assertEqual(tuple(round(v) for v in saved.info["dpi"]), (180, 180))
                self.assertEqual(saved.size, image.size)
        self.assertEqual(render.label_size_mm(image)[1], 15.8)


if __name__ == "__main__":
    unittest.main()
