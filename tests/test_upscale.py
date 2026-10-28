"""Upscale / enhance with the bundled Real-ESRGAN model."""
import hashlib
import io
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PIL import Image, ImageDraw
from PySide6.QtWidgets import QApplication

from dragshift import app, report, tools, upscale
from dragshift.util import UserError

_APP = None  # module-level so the Qt app outlives the tests


def _qapp():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def drawing(size=(120, 80)):
    im = Image.new("RGB", size, (235, 228, 214))
    d = ImageDraw.Draw(im)
    d.ellipse((size[0] * .1, size[1] * .2, size[0] * .5, size[1] * .9), fill=(200, 60, 50))
    d.rectangle((size[0] * .6, size[1] * .2, size[0] * .9, size[1] * .8), outline=(20, 90, 160), width=3)
    return im


def jpeg_degraded(im, q=30):
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=q)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def edge_strength(im):
    g = np.asarray(im.convert("L"), dtype=float)
    mag = np.hypot(np.diff(g, axis=0)[:, :-1], np.diff(g, axis=1)[:-1, :])
    return np.percentile(mag, 99.5)


def test_the_bundled_model_is_the_expected_file():
    assert hashlib.sha256(upscale.MODEL_FILE.read_bytes()).hexdigest() == upscale.MODEL_SHA256


@pytest.mark.parametrize("scale,size", [(1, (60, 40)), (2, (120, 80)), (4, (240, 160))])
def test_output_sizes(scale, size):
    out = upscale.upscale_image(drawing((60, 40)), scale)
    assert out.size == size and out.mode == "RGB"


def test_result_is_sharper_than_a_plain_enlargement():
    small = jpeg_degraded(drawing((120, 80)).resize((60, 40), Image.LANCZOS))
    plain = small.resize((240, 160), Image.BICUBIC)
    model = upscale.upscale_image(small, 4)
    assert edge_strength(model) > edge_strength(plain) * 1.3


def test_enhance_keeps_the_size_and_cleans_jpeg_noise():
    clean = drawing((120, 80))
    noisy = jpeg_degraded(clean, q=12)
    fixed = upscale.upscale_image(noisy, 1)
    assert fixed.size == noisy.size
    err = lambda a: np.abs(np.asarray(a, float) - np.asarray(clean, float)).mean()
    assert err(fixed) < err(noisy)


def test_tiles_join_without_seams(monkeypatch):
    img = jpeg_degraded(drawing((150, 100)))
    monkeypatch.setattr(upscale, "TILE", 40)
    tiled = np.asarray(upscale.upscale_rgb(img, 4), float)
    monkeypatch.setattr(upscale, "TILE", 10_000)
    whole = np.asarray(upscale.upscale_rgb(img, 4), float)
    d = np.abs(tiled - whole)
    assert d.mean() < 0.3 and np.percentile(d, 99.9) < 12            # same picture, no lines at tile borders


def test_half_scale_tiles_also_match(monkeypatch):
    img = drawing((90, 60))
    monkeypatch.setattr(upscale, "TILE", 25)
    tiled = np.asarray(upscale.upscale_rgb(img, 2), float)
    monkeypatch.setattr(upscale, "TILE", 10_000)
    whole = np.asarray(upscale.upscale_rgb(img, 2), float)
    assert np.abs(tiled - whole).mean() < 0.5


def test_tiny_images_and_progress(monkeypatch):
    seen = []
    out = upscale.upscale_rgb(Image.new("RGB", (8, 8), (10, 200, 30)), 4, seen.append)
    assert out.size == (32, 32) and seen == [1.0]
    monkeypatch.setattr(upscale, "TILE", 50)
    seen.clear()
    upscale.upscale_rgb(drawing((120, 80)), 2, seen.append)
    assert len(seen) == 6 and seen == sorted(seen) and seen[-1] == 1.0


def test_transparency_is_kept_without_a_halo():
    im = Image.new("RGBA", (80, 60), (0, 0, 0, 0))                     # transparent, with black underneath
    ImageDraw.Draw(im).ellipse((20, 10, 60, 50), fill=(230, 40, 40, 255))
    out = upscale.upscale_image(im, 2)
    assert out.mode == "RGBA" and out.size == (160, 120)
    a = np.asarray(out.getchannel("A"))
    assert a[2, 2] == 0 and a[60, 80] == 255
    rgb = np.asarray(out)[..., :3].astype(int)
    edge = (a > 40) & (a < 215)
    assert edge.sum() > 50
    assert rgb[edge][:, 0].mean() > 170                                # edge pixels are the subject's red, not dark fringe


def test_too_big_results_are_refused_before_any_work():
    with pytest.raises(UserError, match="megapixels"):
        upscale.check_size((5000, 4000), 4)
    upscale.check_size((5000, 4000), 2)                                # 80 MP: exactly at the limit is fine
    with pytest.raises(ValueError):
        upscale.upscale_rgb(Image.new("RGB", (4, 4)), 3)


def test_files_keep_their_format_and_camera_info(tmp_path):
    ex = Image.Exif()
    ex[0x010F] = "Canon"
    p = tmp_path / "shot.jpg"
    drawing((60, 40)).save(p, exif=ex)
    (out,) = upscale.upscale_file(p, 2)
    assert out == tmp_path / "shot 2x.jpg"
    with Image.open(out) as im:
        assert im.format == "JPEG" and im.size == (120, 80) and im.getexif().get(0x010F) == "Canon"
    g = tmp_path / "anim.gif"
    drawing((40, 30)).save(g)
    assert upscale.upscale_file(g, 1)[0] == tmp_path / "anim Enhanced.png"
    assert p.exists() and Image.open(p).size == (60, 40)               # original untouched


def test_missing_and_damaged_model(tmp_path, monkeypatch):
    monkeypatch.setattr(upscale, "_session", None)
    monkeypatch.setattr(upscale, "MODEL_FILE", tmp_path / "nope.onnx")
    with pytest.raises(UserError, match="missing"):
        upscale.upscale_rgb(Image.new("RGB", (8, 8)), 2)
    bad = tmp_path / "bad.onnx"
    bad.write_bytes(b"junk")
    monkeypatch.setattr(upscale, "MODEL_FILE", bad)
    with pytest.raises(UserError, match="damaged"):
        upscale.upscale_rgb(Image.new("RGB", (8, 8)), 2)


def test_menu_and_notification(tmp_path):
    assert "Upscale" in app.IMAGE_TOOLS and "Upscale" in app.GLYPHS and "Upscale" in app.HINTS
    p = tmp_path / "a.png"
    drawing((60, 40)).save(p)
    (out,) = upscale.upscale_file(p, 2)
    r = report.summarize("tool:Upscale", [str(p)], [out])
    assert r.title == "Upscaled 2×" and "60×40 → 120×80" in r.detail
    (e,) = upscale.upscale_file(p, 1)
    assert report.summarize("tool:Upscale", [str(p)], [e]).title == "Enhanced"
    four = upscale.upscale_file(p, 4)
    assert report.summarize("tool:Upscale", [str(p), str(p)], four * 2).title == "Upscaled 2 images"


def test_window_offers_only_scales_that_fit_and_saves(tmp_path, monkeypatch):
    _qapp()
    p = tmp_path / "pic.png"
    drawing((60, 40)).save(p)
    calls = []
    w = tools.UpscaleWindow([p], lambda *a: calls.append(a))
    assert w._scale() == 2 and w.primary_btn.text() == "Upscale 2×" and "60×40 → 120×80" in w.info.text()
    w.choice.button(2).click()
    w._changed()
    assert w._scale() == 4 and "240×160" in w.info.text() and w.primary_btn.text() == "Upscale 4×"
    w.choice.button(0).click()
    w._changed()
    assert w.primary_btn.text() == "Enhance" and "cleaned up" in w.info.text()
    assert not w.before.pixmap().isNull() and not w.after.pixmap().isNull()
    w.save()                                                            # no app attached → synchronous
    action, srcs, outs, _ = calls[0]
    assert action == "tool:Upscale" and outs[0].name == "pic Enhanced.png"
    monkeypatch.setattr(upscale, "MAX_OUTPUT_PIXELS", 60 * 40 * 3)      # 4× no longer fits, 2× still does
    w2 = tools.UpscaleWindow([p], lambda *a: None)
    assert w2.choice.button(2).isEnabled() is False and w2.choice.button(1).isEnabled() is False
    assert w2.choice.button(0).isChecked()                              # falls back to Enhance
