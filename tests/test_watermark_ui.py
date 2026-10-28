"""Watermark window, ring layout and notification."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication

from dragshift import app, report, tools, watermark

_APP = None  # module-level so the Qt app outlives the tests


def _qapp():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def photo(tmp_path, name="p.jpg", size=(400, 300)):
    p = tmp_path / name
    Image.new("RGB", size, (60, 90, 140)).save(p)
    return p


def pdf(tmp_path, name="d.pdf", pages=2):
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page(width=300, height=400)
    p = tmp_path / name
    doc.save(p)
    doc.close()
    return p


def rings(paths):
    return app.DragShiftApp.build_rings(None, [str(p) for p in paths])


def test_rings_are_split_so_none_gets_crowded(tmp_path):
    img = rings([photo(tmp_path)])
    assert list(img) == ["Convert", "Tools", "Presets", "Files"]
    assert [i.label for i in img["Tools"]] == app.IMAGE_TOOLS and "Watermark" in app.IMAGE_TOOLS
    assert [i.label for i in img["Files"]] == ["Copy", "Rename", "Zip"]
    p = rings([pdf(tmp_path)])
    assert list(p) == ["Convert", "Tools", "Files"] and "Watermark" in [i.label for i in p["Tools"]]
    assert [i.label for i in p["Files"]] == ["Rename", "Zip"]
    assert all(len(r) <= 11 for r in list(img.values()) + list(p.values()))


def test_word_and_text_files_now_get_a_files_ring(tmp_path):
    for name in ("a.docx", "a.txt", "a.md"):
        f = tmp_path / name
        f.write_text("x")
        r = rings([f])
        assert list(r) == ["Convert", "Files"] and [i.label for i in r["Files"]] == ["Rename", "Zip"]


def test_window_previews_and_validates(tmp_path):
    _qapp()
    w = tools.WatermarkWindow([photo(tmp_path)], lambda *a: None)
    w.render()
    assert w.primary_btn.isEnabled() and not w.preview.pixmap().isNull()
    w.text.setText("   ")
    w.render()
    assert not w.primary_btn.isEnabled() and "Type the watermark text" in w.msg.text()
    w.text.setText("Mine")
    w.mode.button(1).click()                                     # logo mode, none chosen yet
    w.render()
    assert not w.primary_btn.isEnabled() and "logo" in w.msg.text().lower()
    w.mode.button(0).click()
    w.render()
    assert w.primary_btn.isEnabled()


def test_tile_sets_a_diagonal_and_leaving_it_resets(tmp_path):
    _qapp()
    w = tools.WatermarkWindow([photo(tmp_path)], lambda *a: None)
    w.pos_grp.button(w.TILE).click()
    w._position_changed(w.TILE)
    assert w.ang.value() == -30 and w._mark().position == "tile"
    w.pos_grp.button(2).click()
    w._position_changed(2)
    assert w.ang.value() == 0 and w._mark().position == "tr"


def test_window_collects_the_chosen_settings(tmp_path):
    _qapp()
    logo = photo(tmp_path, "logo.png", (50, 50))
    w = tools.WatermarkWindow([photo(tmp_path)], lambda *a: None)
    w.text.setText("Line one\\nLine two")
    w.siz.setValue(45); w.opa.setValue(80); w.mar.setValue(5); w.shadow.setChecked(False)
    w._set_color("#e11d48")
    m = w._mark()
    assert (m.text, m.size, m.opacity, m.margin, m.shadow, m.color, m.position) == \
        ("Line one\nLine two", .45, .8, .05, False, "#e11d48", "br")
    w.logo = str(logo)
    w.mode.button(1).click()
    assert w._mark().logo == str(logo) and w._mark().text == ""


def test_saving_several_images_and_a_pdf_in_one_go(tmp_path):
    _qapp()
    calls = []
    imgs = [photo(tmp_path, "a.jpg"), photo(tmp_path, "b.png")]
    w = tools.WatermarkWindow(imgs, lambda *a: calls.append(a))
    assert "2 files" in w._scope_note()
    w.save()                                         # no app attached → runs synchronously
    action, srcs, outs, _ = calls[0]
    assert action == "tool:Watermark" and [o.name for o in outs] == ["a Watermarked.jpg", "b Watermarked.png"]
    d = pdf(tmp_path)
    w2 = tools.WatermarkWindow([d], lambda *a: calls.append(a))
    assert "every page" in w2._scope_note() and not w2.small.size == (0, 0)
    w2.save()
    out_pdf = calls[1][2][0]
    with pymupdf.open(out_pdf) as doc:
        assert doc.page_count == 2 and all(len(pg.get_images()) == 1 for pg in doc)


def test_notification_text(tmp_path):
    d = pdf(tmp_path)
    out = watermark.watermark_pdf(d, watermark.Mark(text="X"))
    r = report.summarize("tool:Watermark", [str(d)], [out])
    assert r.title == "Watermark added" and "2 pages" in r.detail
    p = photo(tmp_path)
    outs = [watermark.watermark_image(p, watermark.Mark(text="X"))]
    assert report.summarize("tool:Watermark", [str(p)], outs).title == "Watermark added"
