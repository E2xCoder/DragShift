"""Remembered tool settings: storage, the three windows that use it, and the Forget button."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication

from dragshift import remember, tools, upscale
from dragshift.settings import Settings, SettingsWindow

_APP = None  # module-level so the Qt app outlives the tests


def _qapp():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


DEFAULTS = {"text": "hi", "size": 30, "ratio": 0.5, "on": True}


def photo(tmp_path, name="p.jpg", size=(60, 40)):
    p = tmp_path / name
    Image.new("RGB", size, (60, 90, 140)).save(p)
    return p


def test_tests_never_touch_the_real_settings(_isolated_remembered_settings):
    assert os.path.normpath(remember._store().fileName()) == os.path.normpath(_isolated_remembered_settings)


def test_defaults_roundtrip_and_unknown_keys():
    assert remember.load("t", DEFAULTS) == DEFAULTS
    remember.save("t", {"text": "mine", "size": 55, "ratio": 0.25, "on": False, "extra": 1})
    assert remember.load("t", DEFAULTS) == {"text": "mine", "size": 55, "ratio": 0.25, "on": False}
    assert remember.load("other", DEFAULTS) == DEFAULTS                 # tools don't see each other's settings


def test_wrong_types_fall_back_per_key():
    remember.save("t", {"text": 5, "size": "big", "ratio": "x", "on": 1})
    assert remember.load("t", DEFAULTS) == DEFAULTS
    remember.save("t", {"text": "ok", "size": True, "ratio": 1, "on": True})   # bool is not an int; int is a fine float
    got = remember.load("t", DEFAULTS)
    assert got["text"] == "ok" and got["size"] == 30 and got["ratio"] == 1.0


def test_corrupt_data_never_raises():
    remember._store().setValue("tools/t", "{not json")
    assert remember.load("t", DEFAULTS) == DEFAULTS
    remember._store().setValue("tools/t", "[1, 2]")
    assert remember.load("t", DEFAULTS) == DEFAULTS


def test_unicode_text_survives():
    remember.save("t", {"text": "Çok güzel ©", "size": 1, "ratio": 0.1, "on": True})
    assert remember.load("t", DEFAULTS)["text"] == "Çok güzel ©"


def test_forget_all():
    remember.save("a", {"x": 1})
    remember.save("b", {"x": 2})
    assert remember.forget_all() == 2
    assert remember.load("a", {"x": 0}) == {"x": 0}
    assert remember.forget_all() == 0


def test_watermark_window_restores_what_was_applied(tmp_path):
    _qapp()
    p = photo(tmp_path)
    w = tools.WatermarkWindow([p], lambda *a: None)
    assert w.text.text() == "Confidential" and w.siz.value() == 30            # first time: defaults
    w.text.setText("© Studio")
    w.siz.setValue(45); w.opa.setValue(70); w.mar.setValue(6); w.gap.setValue(150)
    w.shadow.setChecked(False)
    w._set_color("#e11d48")                                                    # a preset swatch
    w.sw_grp.button(2).setChecked(True)
    w.pos_grp.button(2).click()
    w._position_changed(2)
    w.save()
    w2 = tools.WatermarkWindow([p], lambda *a: None)
    m = w2._mark()
    assert (m.text, m.size, m.opacity, m.margin, m.gap, m.shadow, m.color, m.position) == \
        ("© Studio", .45, .7, .06, 1.5, False, "#e11d48", "tr")
    assert w2.sw_grp.checkedId() == 2


def test_custom_colour_leaves_no_preset_lit_and_tile_is_restored(tmp_path):
    _qapp()
    p = photo(tmp_path)
    w = tools.WatermarkWindow([p], lambda *a: None)
    w._set_color("#123456")
    w.pos_grp.button(w.TILE).click()
    w._position_changed(w.TILE)
    w.save()
    w2 = tools.WatermarkWindow([p], lambda *a: None)
    assert w2.color == "#123456" and w2.sw_grp.checkedId() == -1
    assert w2._mark().position == "tile" and w2.ang.value() == -30


def test_logo_mode_is_restored_only_while_the_logo_still_exists(tmp_path):
    _qapp()
    p = photo(tmp_path)
    logo = photo(tmp_path, "logo.png", (20, 20))
    w = tools.WatermarkWindow([p], lambda *a: None)
    w.logo = str(logo)
    w.mode.button(1).click()
    w.save()
    w2 = tools.WatermarkWindow([p], lambda *a: None)
    assert w2.mode.checkedId() == 1 and w2.logo == str(logo) and w2.logo_name.text() == "logo.png"
    logo.unlink()                                                              # moved or deleted since
    w3 = tools.WatermarkWindow([p], lambda *a: None)
    assert w3.mode.checkedId() == 0 and w3.logo == ""


def test_cancel_and_reset_do_not_remember(tmp_path):
    _qapp()
    p = photo(tmp_path)
    w = tools.WatermarkWindow([p], lambda *a: None)
    w.text.setText("never applied")
    w.siz.setValue(80)
    w.close()                                                                  # closed without applying
    w2 = tools.WatermarkWindow([p], lambda *a: None)
    assert w2.text.text() == "Confidential" and w2.siz.value() == 30
    w2.save()
    w3 = tools.WatermarkWindow([p], lambda *a: None)
    w3.siz.setValue(10)
    w3.reset()
    assert w3.siz.value() == 30
    w4 = tools.WatermarkWindow([p], lambda *a: None)
    assert w4.siz.value() == 30


def test_watermark_window_survives_garbage_saved_data(tmp_path):
    _qapp()
    remember.save("watermark", {"mode": "x", "text": 7, "logo": None, "color": "red", "pos": 99, "size": "huge",
                                "opacity": [], "angle": 1000, "margin": -5, "gap": 1e9, "shadow": "yes"})
    w = tools.WatermarkWindow([photo(tmp_path)], lambda *a: None)
    m = w._mark()
    assert m.text == "Confidential" and m.color == "#ffffff" and m.position == "br" and m.size == .3
    assert 0 <= w.ang.value() <= 90 and w.mar.value() >= 0                     # sliders clamp odd numbers
    remember.save("watermark", {"color": "#12"})                               # a malformed colour
    assert tools.WatermarkWindow([photo(tmp_path)], lambda *a: None).color == "#ffffff"


def test_rename_window_remembers_the_pattern_only(tmp_path):
    _qapp()
    ps = [photo(tmp_path, "a.png"), photo(tmp_path, "b.png")]
    w = tools.RenameWindow(ps, lambda *a: None)
    assert w.pattern.text() == "{name}_{n}"
    w.pattern.setText("Trip {n}")
    w.start.setValue(9)
    w.save()
    w2 = tools.RenameWindow([photo(tmp_path, "c.png")], lambda *a: None)
    assert w2.pattern.text() == "Trip {n}" and w2.start.value() == 1            # the start number is per batch


def test_rename_cancel_keeps_the_old_pattern(tmp_path):
    _qapp()
    p = photo(tmp_path, "a.png")
    w = tools.RenameWindow([p], lambda *a: None)
    w.pattern.setText("kept {n}")
    w.save()
    other = tools.RenameWindow([photo(tmp_path, "z.png")], lambda *a: None)
    other.pattern.setText("typed but never applied")
    other.close()
    assert tools.RenameWindow([photo(tmp_path, "y.png")], lambda *a: None).pattern.text() == "kept {n}"


def test_upscale_window_remembers_the_scale_and_falls_back_when_it_does_not_fit(tmp_path, monkeypatch):
    _qapp()
    p = photo(tmp_path)
    w = tools.UpscaleWindow([p], lambda *a: None)
    assert w._scale() == 2                                                     # first time: 2×
    w.choice.button(2).click()
    w._changed(preview=False)
    w.save()
    assert tools.UpscaleWindow([p], lambda *a: None)._scale() == 4
    monkeypatch.setattr(upscale, "MAX_OUTPUT_PIXELS", 60 * 40 * 3)             # now 4× is too big for this file
    w3 = tools.UpscaleWindow([p], lambda *a: None)
    assert w3._scale() == 1 and w3.choice.button(0).isChecked()


def test_forget_button_in_settings(tmp_path):
    _qapp()
    remember.save("watermark", {"text": "x"})
    remember.save("upscale", {"scale": 4})
    w = SettingsWindow(Settings())
    w.forget_btn.click()
    assert "2 tools" in w.forget_note.text()
    assert tools.WatermarkWindow([photo(tmp_path)], lambda *a: None).text.text() == "Confidential"
    w.forget_btn.click()
    assert "Nothing was remembered" in w.forget_note.text()
