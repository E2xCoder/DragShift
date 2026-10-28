"""Presets: storage, applying a recipe, the Presets ring and the editor window."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication

from dragshift import app, presets, remember, report, tools
from dragshift.presets import Preset
from dragshift.util import UserError

_APP = None  # module-level so the Qt app outlives the tests


def _qapp():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def noisy(path, size=(1500, 1000), exif=True, mode="RGB"):
    rng = np.random.default_rng(0)
    arr = rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)
    im = Image.fromarray(arr).convert(mode)
    kw = {}
    if exif:
        ex = Image.Exif()
        ex[0x010F] = "Canon"
        ex[0x0112] = 6
        ex[0x8825] = {1: "N", 2: (24.0, 33.0, 18.0), 3: "W", 4: (81.0, 47.0, 0.0)}
        kw["exif"] = ex
    im.save(path, **kw)
    return path


def names():
    return [p.name for p in presets.load()]


# ---------------------------------------------------------------- storage
def test_first_run_has_the_example_presets():
    assert names() == ["Web", "Email", "Thumb"]
    assert presets.find("Email").max_kb == 500 and presets.find("nope") is None


def test_save_load_roundtrip_and_an_empty_list_stays_empty():
    presets.save([Preset("Mine", 800, "WEBP", 70, 200, True)])
    assert presets.load() == [Preset("Mine", 800, "WEBP", 70, 200, True)]
    presets.save([])
    assert presets.load() == []                                     # deleting everything is respected


def test_bad_saved_data_is_repaired_or_ignored():
    remember._store().setValue("presets", "{not json")
    assert names() == ["Web", "Email", "Thumb"]
    remember._store().setValue("presets", '"a string"')
    assert presets.load() == []
    remember._store().setValue("presets", '[{"name": "Ok", "max_side": "x", "fmt": "BOGUS", "quality": 1000,'
                                          ' "max_kb": -5, "strip": 3, "future_field": 1}, 7, {"nope": 1},'
                                          ' {"name": "ok"}, {"name": "  "}]')
    (p,) = presets.load()                                           # duplicates, junk and nameless entries dropped
    assert p == Preset("Ok", 0, "SAME", 100, 0, True)


def test_names_are_cleaned_for_use_in_file_names():
    p = presets.clean(Preset('A/B:C*?"<>|x'))
    assert p.name == "ABCx"
    assert presets.clean(Preset("A very long preset name")).name == "A very long "[:12].rstrip(". ")
    assert presets.clean(Preset("dots...")).name == "dots"
    assert presets.unique_name("Web", presets.load()) == "Web 2"
    assert presets.unique_name("Fresh", presets.load()) == "Fresh"


def test_describe():
    assert Preset("x", 1200, "JPG", 80, 500, True).describe() == "1200 px · JPG 80% · ≤500 KB · no GPS"
    assert Preset("x").describe() == "full size · same format 85%"
    assert Preset("x", 0, "PNG").describe() == "full size · PNG"


# ---------------------------------------------------------------- applying
def test_web_preset_shrinks_converts_and_strips(tmp_path):
    src = noisy(tmp_path / "Holiday.png", exif=False)               # a PNG in, a JPG out
    out = presets.apply_preset(src, presets.find("Web"))
    assert out == tmp_path / "Holiday Web.jpg"
    with Image.open(out) as im:
        assert im.format == "JPEG" and im.size == (1500, 1000)      # 1500 < 1600: never enlarged
    big = noisy(tmp_path / "Big.jpg", (3200, 2000))
    out = presets.apply_preset(big, presets.find("Web"))
    with Image.open(out) as im:
        assert im.size == (1000, 1600)                              # the EXIF flag turns it upright first, then 1600 px tall
        assert not im.getexif().get(0x010F) and not im.getexif().get(0x8825)   # camera info and GPS removed
    assert Image.open(big).getexif().get(0x8825)                    # the original still has its GPS


def test_keeping_camera_info_keeps_it_upright_without_double_rotation(tmp_path):
    src = noisy(tmp_path / "shot.jpg", (400, 300))
    out = presets.apply_preset(src, Preset("Keep", 0, "SAME", 90, 0, False))
    with Image.open(out) as im:
        assert im.size == (300, 400)                                # EXIF said rotate: the picture is now upright
        assert im.getexif().get(0x010F) == "Canon" and im.getexif().get(0x0112) == 1


def test_same_format_follows_the_source(tmp_path):
    for name, fmt in [("a.png", "PNG"), ("a.webp", "WEBP"), ("a.bmp", "BMP"), ("a.gif", "PNG"), ("a.jpeg", "JPEG")]:
        src = tmp_path / name
        noisy(src, (64, 64), exif=False)
        with Image.open(presets.apply_preset(src, Preset("Same"))) as im:
            assert im.format == fmt, name


def test_a_size_cap_is_met_by_lowering_quality_then_size(tmp_path):
    src = noisy(tmp_path / "n.jpg", (1600, 1200), exif=False)
    out = presets.apply_preset(src, Preset("Cap", 0, "JPG", 95, 120, True))
    assert out.stat().st_size <= 120 * 1024
    assert out.stat().st_size > 30 * 1024                           # it did not just collapse the picture
    with Image.open(out) as im:
        assert im.size[0] <= 1600
    tiny = presets.apply_preset(src, Preset("Tiny", 0, "JPG", 95, 8, True))
    assert tiny.stat().st_size < src.stat().st_size / 10            # an impossible cap still shrinks as far as it can


def test_lossless_containers_ignore_the_size_cap(tmp_path):
    src = noisy(tmp_path / "x.bmp", (300, 200), exif=False)
    out = presets.apply_preset(src, Preset("Cap", 0, "SAME", 90, 1, False))
    with Image.open(out) as im:
        assert im.format == "BMP" and im.size == (300, 200)


def test_transparent_png_to_jpg_is_flattened(tmp_path):
    src = tmp_path / "t.png"
    Image.new("RGBA", (50, 50), (255, 0, 0, 0)).save(src)
    with Image.open(presets.apply_preset(src, Preset("J", 0, "JPG", 90))) as im:
        assert im.mode == "RGB" and im.getpixel((5, 5)) == (255, 255, 255)


def test_never_overwrites_and_leaves_the_original_alone(tmp_path):
    src = noisy(tmp_path / "p.jpg", (200, 100))
    before = src.read_bytes()
    (tmp_path / "p Web.jpg").write_text("precious")
    out = presets.apply_preset(src, presets.find("Web"))
    assert out.name == "p Web (1).jpg" and (tmp_path / "p Web.jpg").read_text() == "precious"
    assert src.read_bytes() == before


def test_a_preset_without_a_name_is_refused(tmp_path):
    with pytest.raises(UserError, match="no name"):
        presets.apply_preset(noisy(tmp_path / "p.jpg", (20, 20)), Preset("///"))


# ---------------------------------------------------------------- menu + notification
def test_image_menu_has_a_presets_ring_and_other_types_do_not(tmp_path):
    img = noisy(tmp_path / "a.jpg", (20, 20))
    r = app.DragShiftApp.build_rings(None, [str(img)])
    assert list(r) == ["Convert", "Tools", "Presets", "Files"]
    assert [i.label for i in r["Presets"]] == ["Web", "Email", "Thumb", "Edit"]
    assert r["Presets"][0].key == "preset:Web" and r["Presets"][-1].key == "presets:edit"
    assert r["Presets"][1].hint == "1200 px · JPG 80% · ≤500 KB · no GPS"
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert "Presets" not in app.DragShiftApp.build_rings(None, [str(pdf)])
    presets.save([])
    assert [i.label for i in app.DragShiftApp.build_rings(None, [str(img)])["Presets"]] == ["Edit"]


def test_notification_text(tmp_path):
    src = noisy(tmp_path / "Big.jpg", (3200, 2000), exif=False)
    out = presets.apply_preset(src, presets.find("Web"))
    r = report.summarize("preset:Web", [str(src)], [out])
    assert r.title == "Web preset" and "3200×2000 → 1600×1000" in r.detail and out.name in r.detail
    assert r.good.endswith("smaller")
    many = report.summarize("preset:Web", [str(src)] * 2, [out, out])
    assert many.title == "Web: 2 images"


# ---------------------------------------------------------------- editor
def test_editor_saves_every_change_as_you_make_it():
    _qapp()
    w = tools.PresetsWindow()
    assert [w.pick.itemText(i) for i in range(w.pick.count())] == ["Web", "Email", "Thumb"]
    w.pick.setCurrentIndex(1)
    assert (w.side.value(), w.kb.value(), w.strip.isChecked(), w.fmt.currentData()) == (1200, 500, True, "JPG")
    w.side.setValue(1000)
    w.kb.setValue(0)
    w.strip.setChecked(False)
    w.fmt.setCurrentIndex(presets.FORMATS.index("WEBP"))
    w.q.setValue(60)
    saved = presets.find("Email")
    assert (saved.max_side, saved.max_kb, saved.strip, saved.fmt, saved.quality) == (1000, 0, False, "WEBP", 60)
    assert "1000 px · WEBP 60%" in w.summary.text()


def test_editor_new_rename_clash_delete_and_restore():
    _qapp()
    w = tools.PresetsWindow()
    w.new_btn.click()
    assert names() == ["Web", "Email", "Thumb", "My preset"] and w.pick.currentText() == "My preset"
    w.name.setText("Social")
    w.name.textEdited.emit("Social")
    assert names()[-1] == "Social" and w.pick.currentText() == "Social"
    w.name.setText("web")                                           # clashes with Web (case-insensitive)
    w.name.textEdited.emit("web")
    assert names()[-1] == "Social" and "already has that name" in w.note.text()
    w.name.setText("")
    w.name.textEdited.emit("")
    assert names()[-1] == "Social" and "needs a name" in w.note.text()
    w.del_btn.click()
    assert names() == ["Web", "Email", "Thumb"]
    for _ in range(3):
        w.del_btn.click()
    assert names() == [] and not w.name.isEnabled() and "No presets yet" in w.note.text()
    w.defaults_btn.click()
    assert names() == ["Web", "Email", "Thumb"] and "Added 3" in w.note.text()
    w.defaults_btn.click()
    assert "already there" in w.note.text()


def test_png_disables_the_quality_slider():
    _qapp()
    w = tools.PresetsWindow()
    w.fmt.setCurrentIndex(presets.FORMATS.index("PNG"))
    assert not w.q.isEnabled()
    w.fmt.setCurrentIndex(presets.FORMATS.index("JPG"))
    assert w.q.isEnabled()


def test_editor_survives_clash_names_with_odd_characters():
    _qapp()
    w = tools.PresetsWindow()
    w.name.setText("A/B:C")
    w.name.textEdited.emit("A/B:C")
    assert names()[0] == "ABC" and w.pick.currentText() == "ABC"
