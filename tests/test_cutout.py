"""Background removal (local ONNX model)."""
import hashlib
import threading

import numpy as np
import pytest
from PIL import Image, ImageDraw

from dragshift import app, cutout, report
from dragshift.util import UserError


def apple(size=(480, 320), mode="RGB"):
    """A red ball with stem on a textured blue-grey background."""
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w]
    bg = np.stack([150 + 40 * np.sin(xx / 40.0), 170 + 30 * np.cos(yy / 30.0), 190 + 0 * xx], -1)
    im = Image.fromarray(np.clip(bg + np.random.default_rng(1).normal(0, 6, bg.shape), 0, 255).astype("uint8"))
    d = ImageDraw.Draw(im)
    d.ellipse((w * .25, h * .22, w * .75, h * .92), fill=(200, 30, 35))
    d.ellipse((w * .33, h * .32, w * .43, h * .42), fill=(255, 220, 220))
    d.rectangle((w * .49, h * .08, w * .51, h * .25), fill=(80, 50, 20))
    return im.convert(mode)


def test_the_bundled_model_is_the_expected_file():
    assert hashlib.sha256(cutout.MODEL_FILE.read_bytes()).hexdigest() == cutout.MODEL_SHA256


def test_cut_out_makes_a_transparent_png_of_the_same_size(tmp_path):
    p = tmp_path / "fruit.jpg"
    apple().save(p, quality=92)
    (out,) = cutout.cut_out(p)
    assert out == tmp_path / "fruit Cutout.png"
    with Image.open(out) as im:
        assert im.mode == "RGBA" and im.size == (480, 320)
        a = np.asarray(im.getchannel("A"))
        assert a[5, 5] == 0 and a[-5, -5] == 0 and a[-5, 5] == 0          # background gone
        assert a[200, 240] == 255                                          # subject kept
        assert 0.15 < (a > 128).mean() < 0.6                               # roughly the ball's share
        assert tuple(np.asarray(im)[200, 240][:3]) != (0, 0, 0)            # colours are the photo's, not black
    assert p.exists()                                                      # original untouched


def test_existing_transparency_is_kept(tmp_path):
    im = apple().convert("RGBA")
    ImageDraw.Draw(im).rectangle((0, 0, 100, 320), fill=(0, 0, 0, 0))      # a hole already in the picture
    out = cutout.remove_background(im)
    a = np.asarray(out.getchannel("A"))
    assert (a[:, :100] == 0).all() and a[200, 240] == 255


def test_blank_images_have_no_subject(tmp_path):
    p = tmp_path / "wall.png"
    Image.new("RGB", (400, 300), (128, 128, 128)).save(p)
    with pytest.raises(UserError, match="Couldn't find a subject"):
        cutout.cut_out(p)
    assert not list(tmp_path.glob("*Cutout*"))


def test_palette_png_and_grayscale_inputs_work(tmp_path):
    for mode in ("P", "L", "LA"):
        im = apple().convert(mode if mode != "LA" else "L").convert(mode)
        out = cutout.remove_background(im)
        assert out.mode == "RGBA" and out.size == im.size


def test_parallel_jobs_share_one_session(tmp_path):
    im = apple((240, 160))
    results, errors = [], []

    def work():
        try:
            results.append(np.asarray(cutout.remove_background(im).getchannel("A")).mean())
        except Exception as e:      # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and len(results) == 4 and max(results) - min(results) < 1e-6


def test_missing_or_damaged_model_gives_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cutout, "_session", None)
    monkeypatch.setattr(cutout, "MODEL_FILE", tmp_path / "nope.onnx")
    with pytest.raises(UserError, match="missing"):
        cutout.remove_background(apple((64, 64)))
    bad = tmp_path / "bad.onnx"
    bad.write_bytes(b"not a model")
    monkeypatch.setattr(cutout, "MODEL_FILE", bad)
    with pytest.raises(UserError, match="damaged"):
        cutout.remove_background(apple((64, 64)))


def test_menu_and_summary(tmp_path):
    assert "Cutout" in app.IMAGE_TOOLS and "Cutout" in app.GLYPHS and "Cutout" in app.HINTS
    p = tmp_path / "fruit.jpg"
    apple().save(p)
    (out,) = cutout.cut_out(p)
    r = report.summarize("tool:Cutout", [str(p)], [out])
    assert r.title == "Background removed" and out.name in r.detail
