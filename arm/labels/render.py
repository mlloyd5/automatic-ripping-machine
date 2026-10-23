"""
Draw a sleeve label as a 1-bit bitmap for a Brother P-touch tape printer.

The printers are 180 dpi and only print a band in the middle of the tape, so the image height is
fixed by the tape width (``TAPE_PRINT_DOTS``) and the printer tool scales nothing: what is drawn
here is what comes out. Layout, left to right: a QR code holding only the sleeve id (so a USB
scanner in keyboard mode types just ``S0042``), then the sleeve id in large bold text with the
title, year and disc type underneath, so a sleeve can be found by eye without scanning.

Only Pillow and ``qrcode`` are imported, and both lazily, so the ripper can import this module
when neither is installed and report a clear error at print time instead.
"""
import os
from typing import List, Optional, Tuple

DPI = 180
"""Print resolution of every P-touch model with a USB port."""

TAPE_PRINT_DOTS = {6: 32, 9: 50, 12: 70, 18: 112, 24: 128, 36: 192}
"""Printable height in dots for each TZe tape width in mm (Brother raster command reference)."""

MIN_QR_MODULE_PX = 2
"""Below two dots per QR module the Brother head cannot print a module cleanly."""

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)
"""Bold TrueType fonts tried in order when LABEL_FONT is blank; Pillow's bitmap font is the last resort."""


def mm_to_px(mm: float) -> int:
    """Millimetres to dots at 180 dpi."""
    return int(round(mm / 25.4 * DPI))


def print_height(tape_mm: int) -> int:
    """
    Printable height in dots for a tape width.

    :raises ValueError: for a tape width no P-touch takes
    """
    try:
        return TAPE_PRINT_DOTS[int(tape_mm)]
    except (KeyError, ValueError, TypeError):
        raise ValueError(f"Unsupported tape width {tape_mm!r} mm; one of {sorted(TAPE_PRINT_DOTS)}") from None


def find_font(path: Optional[str] = None) -> Optional[str]:
    """
    The TrueType font file to draw with: ``path`` when it exists, else the first candidate present.

    :return: a path, or ``None`` when only Pillow's built-in bitmap font is available
    """
    if path and os.path.isfile(path):
        return path
    for candidate in FONT_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def _load_font(font_path: Optional[str], size: int):
    from PIL import ImageFont
    if font_path:
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has no sized default font
        return ImageFont.load_default()


def _text_width(draw, text: str, font) -> int:
    left, _top, right, _bottom = draw.textbbox((0, 0), text, font=font)
    return right - left


def fit_text(draw, text: str, font_path: Optional[str], size: int, max_width: int, min_size: int = 10):
    """
    Shrink the font until ``text`` fits ``max_width``; truncate with an ellipsis when it never does.

    :return: (text to draw, font)
    """
    text = text or ""
    font = _load_font(font_path, size)
    while size > min_size and _text_width(draw, text, font) > max_width:
        size -= 2
        font = _load_font(font_path, size)
    if _text_width(draw, text, font) > max_width:
        while len(text) > 1 and _text_width(draw, text + "…", font) > max_width:
            text = text[:-1]
        text = text.rstrip() + "…"
    return text, font


def qr_image(data: str, height: int):
    """
    A QR code of ``data`` as a 1-bit Pillow image exactly ``height`` dots tall (quiet zone included).

    Error correction M, the version picked by the library for the data length, and as many dots per
    module as fit in ``height``; ``height`` must leave at least two dots per module.
    """
    import qrcode
    from PIL import Image

    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
    code.add_data(data)
    code.make(fit=True)
    modules = code.modules_count + 2 * code.border
    box = max(height // modules, 1)
    if box < MIN_QR_MODULE_PX:
        raise ValueError(f"{height} dots is too small for a {modules}-module QR code at {MIN_QR_MODULE_PX} dots/module")
    code.box_size = box
    image = code.make_image(fill_color="black", back_color="white").get_image().convert("1")
    # Centre the code vertically in a white band of the full print height
    band = Image.new("1", (image.width, height), 1)
    band.paste(image, (0, (height - image.height) // 2))
    return band


def render_label(sleeve_id: str, lines: List[str], tape_mm: int = 18, font_path: Optional[str] = None,
                 max_mm: float = 90.0, min_mm: float = 36.0):
    """
    Draw the sleeve label.

    :param sleeve_id: the code to encode in the QR and print in large text, e.g. ``S0042``
    :param lines: up to three further text lines (title, year and disc type, ...); blanks are skipped
    :param tape_mm: tape width in mm; sets the image height
    :param font_path: TrueType font, or ``None`` to use the first of :data:`FONT_CANDIDATES` present
    :param max_mm: longest label to print, text is shrunk or truncated to fit
    :param min_mm: shortest label; narrower content is padded so the cut is not too close to the QR
    :return: a 1-bit Pillow image (white background, black ink) of the exact print height
    """
    from PIL import Image, ImageDraw

    height = print_height(tape_mm)
    font_path = find_font(font_path)
    lines = [line for line in (lines or []) if line and str(line).strip()][:3]
    gap = max(height // 14, 3)

    qr = qr_image(sleeve_id, height)
    # The QR code starts at the very edge: the printer adds its own blank lead (the head-to-cutter distance,
    # about 24 mm on a P-touch), so any margin drawn here only adds to it. The QR's quiet zone is inside qr.
    text_x = qr.width + gap
    max_width = mm_to_px(max_mm)
    text_width_limit = max_width - text_x - gap

    # Headline (the sleeve id) takes about 40% of the height, the rest is shared by the lines under it
    scratch = ImageDraw.Draw(Image.new("1", (1, 1), 1))
    headline_size = max(int(height * 0.40), 8)
    headline, headline_font = fit_text(scratch, sleeve_id, font_path, headline_size, text_width_limit)
    remaining = height - headline_size - gap
    body_size = max(int(remaining / max(len(lines), 1) * 0.82), 6) if lines else 0
    body = [fit_text(scratch, str(line), font_path, body_size, text_width_limit) for line in lines]

    widest = max([_text_width(scratch, headline, headline_font)] + [_text_width(scratch, t, f) for t, f in body])
    width = max(min(text_x + widest + gap * 2, max_width), mm_to_px(min_mm))

    image = Image.new("1", (width, height), 1)
    image.paste(qr, (0, 0))
    draw = ImageDraw.Draw(image)
    y = 0
    draw.text((text_x, y), headline, font=headline_font, fill=0)
    y += headline_size + gap
    for text, font in body:
        draw.text((text_x, y), text, font=font, fill=0)
        y += body_size + gap // 2
    return image


def label_text_lines(title: Optional[str], year: Optional[str], disc_type: Optional[str],
                     disc_number: Optional[int] = None, disc_total: Optional[int] = None) -> List[str]:
    """
    The text lines under the sleeve id: ``Title (Year)`` and ``Blu-ray · disc 2 of 3``.

    Everything is optional; missing pieces are left out rather than printed as ``None``.
    """
    first = (title or "").strip()
    if year and str(year).strip() and str(year).strip() != "0000":
        first = f"{first} ({str(year).strip()})" if first else str(year).strip()
    second = (disc_type or "").strip()
    if disc_number:
        disc = f"disc {disc_number}" + (f" of {disc_total}" if disc_total else "")
        second = f"{second} · {disc}" if second else disc
    return [line for line in (first, second) if line]


def save_png(image, path: str) -> str:
    """Write the label as a PNG with the 180 dpi tag, so tools that read DPI print it at true size."""
    image.save(path, format="PNG", dpi=(DPI, DPI))
    return path


def label_size_mm(image) -> Tuple[float, float]:
    """(width, height) of a rendered label in millimetres, for log lines and the UI."""
    return (round(image.width / DPI * 25.4, 1), round(image.height / DPI * 25.4, 1))
