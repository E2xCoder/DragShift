"""OCR with the Windows engine (skipped where it isn't available)."""
import io
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from dragshift import convert, ocr, report
from dragshift.util import CONFIG, UserError

FONT = Path(r"C:\Windows\Fonts\arial.ttf")
pytestmark = pytest.mark.skipif(not (ocr.available() and FONT.exists()), reason="Windows OCR not available")

LINES = ["Invoice number 48213", "Total due: 1,250 dollars"]


def text_image(lines=LINES, size=44, w=1100):
    f = ImageFont.truetype(str(FONT), size)
    im = Image.new("RGB", (w, 90 * len(lines) + 40), "white")
    d = ImageDraw.Draw(im)
    for i, t in enumerate(lines):
        d.text((40, 30 + 90 * i), t, fill="black", font=f)
    return im


def scanned_pdf(path, im=None, rotation=0, extra_text_page=False):
    """A PDF whose page is only a picture of text (a scan). rotation: how the page is turned."""
    im = im or text_image()
    img = im.rotate(rotation, expand=True) if rotation else im
    buf = io.BytesIO()
    img.save(buf, "PNG")
    doc = pymupdf.open()
    page = doc.new_page(width=img.width * 72 / 200, height=img.height * 72 / 200)
    page.insert_image(page.rect, stream=buf.getvalue())
    if rotation:
        page.set_rotation(rotation)
    if extra_text_page:
        doc.new_page().insert_text((72, 100), "This page already has real text on it, plenty of it.")
    doc.save(path)
    doc.close()
    return path


def test_ocr_is_offered_for_images_and_pdfs_only():
    assert convert.targets_for("image", ".png")[-1] == "OCR"
    assert convert.targets_for("pdf", ".pdf")[-1] == "OCR"
    assert "OCR" not in convert.targets_for("docx", ".docx")
    assert "OCR" not in convert.targets_for("text", ".txt")


def test_image_to_text_file(tmp_path):
    p = tmp_path / "scan.png"
    text_image().save(p)
    (out,) = convert.convert(p, "image", "OCR")
    assert out == tmp_path / "scan.txt"
    body = out.read_text(encoding="utf-8")
    assert "Invoice number 48213" in body and "1,250" in body


def test_image_without_text_is_a_clear_error(tmp_path):
    p = tmp_path / "blank.png"
    Image.new("RGB", (400, 300), "white").save(p)
    with pytest.raises(UserError, match="No text found"):
        ocr.image_to_txt(p)


def test_transparent_png_is_read_on_white(tmp_path):
    im = Image.new("RGBA", (1100, 130), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((40, 30), "Hello transparent", fill=(0, 0, 0, 255), font=ImageFont.truetype(str(FONT), 44))
    p = tmp_path / "t.png"
    im.save(p)
    assert "Hello transparent" in ocr.image_to_txt(p)[0].read_text(encoding="utf-8")


@pytest.mark.parametrize("rotation", [0, 90, 270])
def test_scanned_pdf_becomes_searchable_in_the_right_place(tmp_path, rotation):
    src = scanned_pdf(tmp_path / "scan.pdf", rotation=rotation)
    (out,) = convert.convert(src, "pdf", "OCR")
    assert out.name == "scan OCR.pdf"
    with pymupdf.open(out) as doc:
        page = doc[0]
        assert "Invoice number 48213" in page.get_text()
        (hit,) = page.search_for("Invoice")[:1]
        # where the picture has the word, mapped into the page's own (unrotated) coordinates
        expected = pymupdf.Rect(44 * 72 / 200, 38 * 72 / 200, 190 * 72 / 200, 80 * 72 / 200)
        exp = (expected * page.derotation_matrix).normalize()
        assert abs(hit.x0 - exp.x0) < 14 and abs(hit.y0 - exp.y0) < 14, (rotation, hit, exp)
        assert (hit.width < hit.height) == (rotation in (90, 270))     # sideways pages: raw box is tall
    with pymupdf.open(src) as orig:
        assert orig[0].get_text().strip() == ""                          # the original is untouched


def test_pages_with_text_are_left_alone_and_all_text_is_nothing_to_do(tmp_path):
    src = scanned_pdf(tmp_path / "mixed.pdf", extra_text_page=True)
    (out,) = ocr.pdf_to_searchable(src)
    with pymupdf.open(out) as doc:
        assert "Invoice" in doc[0].get_text() and "already has real text" in doc[1].get_text()
        assert doc[1].get_text().count("already has real text") == 1       # not doubled by OCR
    with pytest.raises(UserError, match="Nothing to do"):
        ocr.pdf_to_searchable(out)


def test_scanned_pdf_to_txt_falls_back_to_ocr_but_real_text_is_untouched(tmp_path):
    src = scanned_pdf(tmp_path / "scan.pdf")
    (txt,) = convert.convert(src, "pdf", "TXT")
    assert "Invoice number 48213" in txt.read_text(encoding="utf-8")
    real = tmp_path / "real.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "Plain embedded text here")
    doc.save(real)
    doc.close()
    (txt2,) = convert.convert(real, "pdf", "TXT")
    assert "Plain embedded text here" in txt2.read_text(encoding="utf-8")


def test_language_setting_changes_the_engine(tmp_path):
    tags = [t for t, _ in ocr.languages()]
    if "tr" not in tags:
        pytest.skip("Turkish recognition pack not installed")
    im = text_image(["Merhaba dünya, çok güzel"])
    try:
        CONFIG["ocr_lang"] = "tr"
        assert "çok güzel" in ocr.text_of(ocr.recognize(im))
    finally:
        CONFIG["ocr_lang"] = ""
    CONFIG["ocr_lang"] = "not-a-language"       # an uninstalled choice falls back instead of failing
    try:
        assert "Merhaba" in ocr.text_of(ocr.recognize(im))
    finally:
        CONFIG["ocr_lang"] = ""


def test_summaries(tmp_path):
    p = tmp_path / "scan.png"
    text_image().save(p)
    (txt,) = convert.convert(p, "image", "OCR")
    r = report.summarize("convert:OCR", [str(p)], [txt])
    assert r.title == "Text recognised" and "words" in r.detail
    src = scanned_pdf(tmp_path / "s.pdf")
    (pdf,) = convert.convert(src, "pdf", "OCR")
    r = report.summarize("convert:OCR", [str(src)], [pdf])
    assert r.title == "Searchable PDF" and r.good == "text layer added"


def test_large_images_are_scaled_for_the_engine_and_coordinates_come_back_in_full_size():
    big = Image.new("RGB", (12000, 400), "white")
    ImageDraw.Draw(big).text((6000, 100), "Far right", fill="black", font=ImageFont.truetype(str(FONT), 120))
    lines = ocr.recognize(big)
    words = [w for ln in lines for w in ln.words]
    assert any("Far" in w.text and 5800 < w.x < 6400 for w in words), [(w.text, w.x) for w in words]
