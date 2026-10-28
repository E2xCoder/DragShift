"""Read text from images and scanned PDFs with the OCR engine built into Windows 10/11.

Offline, nothing to install: languages come from Windows (Settings → Time & language → Language).
  image → .txt          scanned PDF → searchable PDF (invisible text over each page)
Other platforms: available() is False and the menu doesn't offer OCR."""
from __future__ import annotations

import asyncio
import io
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .convert import flatten, open_image, open_pdf
from .util import CONFIG, UserError, sibling

PDF_DPI = 250
EMBEDDED_TEXT_MIN = 25        # a page with more characters than this already has a text layer


@dataclass
class Word:
    text: str
    x: float
    y: float
    w: float
    h: float


@dataclass
class Line:
    text: str
    words: list[Word] = field(default_factory=list)


_AVAILABLE: bool | None = None


def available() -> bool:
    global _AVAILABLE
    if _AVAILABLE is None:
        _AVAILABLE = False
        if sys.platform == "win32":
            try:
                _AVAILABLE = _engine() is not None
            except Exception:
                _AVAILABLE = False
    return _AVAILABLE


def languages() -> list[tuple[str, str]]:
    """(tag, name) of every language Windows can recognise text in."""
    from winrt.windows.media.ocr import OcrEngine
    return [(lang.language_tag, lang.display_name) for lang in OcrEngine.available_recognizer_languages]


def _engine():
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine
    eng = None
    tag = CONFIG.get("ocr_lang") or ""
    if tag:                                    # the language chosen in Settings, if it is still installed
        eng = OcrEngine.try_create_from_language(Language(tag))
    if eng is None:
        eng = OcrEngine.try_create_from_user_profile_languages()
    if eng is None:
        eng = OcrEngine.try_create_from_language(Language("en-US"))
    return eng


async def _recognize(im: Image.Image) -> list[Line]:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    eng = _engine()
    if eng is None:
        raise UserError("Windows has no text-recognition language installed. Add one in "
                        "Settings → Time & language → Language & region.")
    im = flatten(im.convert("RGBA") if im.mode not in ("RGB", "L") else im)
    scale = 1.0
    limit = OcrEngine.max_image_dimension
    if max(im.size) > limit:
        scale = limit / max(im.size)
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG")

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(buf.getvalue())
    await writer.store_async()
    writer.detach_stream()
    stream.seek(0)
    bitmap = await (await BitmapDecoder.create_async(stream)).get_software_bitmap_async()
    result = await eng.recognize_async(bitmap)
    lines = []
    for ln in result.lines:
        words = []
        for w in ln.words:
            r = w.bounding_rect
            words.append(Word(w.text, r.x / scale, r.y / scale, r.width / scale, r.height / scale))
        lines.append(Line(ln.text, words))
    return lines


def recognize(im: Image.Image) -> list[Line]:
    """OCR one image. Blocking; call from a worker thread."""
    if not available():
        raise UserError("Text recognition needs Windows 10 or 11 with a recognition language installed.")
    return asyncio.run(_recognize(im))


def text_of(lines: list[Line]) -> str:
    return "\n".join(ln.text for ln in lines)


# ---------------------------------------------------------------- image → text
def image_to_txt(src: str | Path) -> list[Path]:
    text = text_of(recognize(open_image(src))).strip()
    if not text:
        raise UserError("No text found in this image.")
    out = sibling(src, ext=".txt")
    out.write_text(text + "\n", encoding="utf-8")
    return [out]


# ---------------------------------------------------------------- PDF
def _page_image(page, dpi: int = PDF_DPI) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _has_text(page) -> bool:
    return len(page.get_text("text").strip()) > EMBEDDED_TEXT_MIN


def pdf_ocr_text(src: str | Path) -> str:
    """Text of a PDF: the embedded text where a page has it, OCR for scanned pages."""
    parts = []
    with open_pdf(src) as doc:
        for page in doc:
            parts.append(page.get_text("text") if _has_text(page) else text_of(recognize(_page_image(page))))
    return "\n\f\n".join(parts)


def _font():
    import pymupdf as fitz
    for name in ("arial.ttf", "segoeui.ttf"):         # covers Latin, Turkish, Cyrillic, Greek, Arabic
        p = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name
        if p.exists():
            return fitz.Font(fontfile=str(p))
    return fitz.Font("helv")


def _write_invisible_text(page, lines: list[Line], dpi: int, font):
    """Lay each recognised word over the page, invisible but searchable and selectable."""
    import pymupdf as fitz
    k = 72.0 / dpi
    derot = page.derotation_matrix            # the picture is of the rotated page; text goes in raw space
    rot = page.rotation
    tw = fitz.TextWriter(page.rect)
    for ln in lines:
        for w in ln.words:
            if not w.text.strip():
                continue
            r = fitz.Rect(w.x * k, w.y * k, (w.x + w.w) * k, (w.y + w.h) * k)
            width1 = font.text_length(w.text, fontsize=1)
            if width1 <= 0 or r.width <= 0:
                continue
            size = min(r.width / width1, r.height * 1.6)       # stretch to the word's width, but sanely
            if rot == 0:
                tw.append((r.x0, r.y1 - r.height * 0.2), w.text, font=font, fontsize=size)
            else:
                origin = fitz.Point(r.x0, r.y1 - r.height * 0.2) * derot    # baseline start, in raw page space
                sub = fitz.TextWriter(page.rect)
                sub.append(origin, w.text, font=font, fontsize=size)
                sub.write_text(page, render_mode=3, morph=(origin, fitz.Matrix(rot)))
    if rot == 0:
        tw.write_text(page, render_mode=3)


def pdf_to_searchable(src: str | Path) -> list[Path]:
    """A copy of the PDF where scanned pages also carry their text. Pages that already have a
    text layer are left alone."""
    import pymupdf as fitz
    src = Path(src)
    font = _font()
    done = 0
    with open_pdf(src) as doc:
        if doc.needs_pass:
            raise UserError("This PDF is password-protected.")
        for page in doc:
            if _has_text(page):
                continue
            lines = recognize(_page_image(page))
            if lines:
                _write_invisible_text(page, lines, PDF_DPI, font)
                done += 1
        if not done:
            raise UserError("Nothing to do: every page already has text, or no text was found.")
        out = sibling(src, "OCR", ".pdf")
        doc.save(out, garbage=3, deflate=True)
    return [out]
