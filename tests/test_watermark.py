"""Watermark engine: placement, opacity, tiling, logos, image and PDF output."""
import numpy as np
import pymupdf
import pytest
from PIL import Image, ImageDraw

from dragshift import watermark
from dragshift.util import UserError
from dragshift.watermark import Mark


def bbox_of_alpha(layer, thresh=8):
    a = np.asarray(layer.getchannel("A"))
    ys, xs = np.where(a > thresh)
    return (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1) if len(xs) else None


def centre_of(box):
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


@pytest.mark.parametrize("pos,expect", [("tl", (0, 0)), ("tr", (1, 0)), ("bl", (0, 1)), ("br", (1, 1)),
                                        ("c", (.5, .5)), ("tc", (.5, 0)), ("mr", (1, .5))])
def test_each_position_puts_the_mark_in_that_part_of_the_page(pos, expect):
    W, H = 1000, 700
    box = bbox_of_alpha(watermark.overlay((W, H), Mark(text="MARK", position=pos, size=.2, shadow=False)))
    cx, cy = centre_of(box)
    assert abs(cx / W - (.1 if expect[0] == 0 else .9 if expect[0] == 1 else .5)) < .06, (pos, cx)
    assert abs(cy / H - (.08 if expect[1] == 0 else .92 if expect[1] == 1 else .5)) < .06, (pos, cy)


def test_size_is_a_share_of_the_page_width_so_previews_match_the_result():
    small = bbox_of_alpha(watermark.overlay((500, 300), Mark(text="Sample", size=.4, shadow=False)))
    big = bbox_of_alpha(watermark.overlay((2000, 1200), Mark(text="Sample", size=.4, shadow=False)))
    assert abs((small[2] - small[0]) / 500 - 0.4) < .02
    assert abs((big[2] - big[0]) / 2000 - 0.4) < .02


def test_opacity_scales_the_alpha():
    for op in (.25, .5, 1.0):
        a = np.asarray(watermark.overlay((600, 300), Mark(text="XXXX", opacity=op, shadow=False)).getchannel("A"))
        assert abs(a.max() - round(255 * op)) <= 1


def test_text_colour_and_shadow():
    plain = watermark.overlay((600, 300), Mark(text="XXXX", color="#ff0000", shadow=False))
    px = np.asarray(plain)[np.asarray(plain.getchannel("A")) > 250]
    assert (px[:, 0] > 250).all() and (px[:, 1] < 5).all()
    shadowed = watermark.overlay((600, 300), Mark(text="XXXX", color="#ffffff", shadow=True))
    arr = np.asarray(shadowed)
    assert (arr[..., 3][(arr[..., :3].sum(-1) < 60)] > 0).any()      # dark halo pixels exist


def test_angle_rotates_the_mark():
    flat = bbox_of_alpha(watermark.overlay((1000, 1000), Mark(text="LONG TEXT HERE", position="c", size=.5, shadow=False)))
    turned = bbox_of_alpha(watermark.overlay((1000, 1000), Mark(text="LONG TEXT HERE", position="c", size=.5,
                                                                angle=90, shadow=False)))
    assert (flat[2] - flat[0]) > 3 * (flat[3] - flat[1])
    assert (turned[3] - turned[1]) > 3 * (turned[2] - turned[0])


def test_multiline_and_unicode_text_work():
    layer = watermark.overlay((800, 600), Mark(text="Gizli\nÇok güzel ğış", position="c", size=.4))
    box = bbox_of_alpha(layer)
    assert box and (box[3] - box[1]) > 40


def test_tile_covers_the_page_evenly_and_respects_angle():
    W, H = 1200, 800
    t = watermark.overlay((W, H), Mark(text="DRAFT", position="tile", size=.15, gap=1.0, angle=-30, shadow=False))
    a = np.asarray(t.getchannel("A")) > 8
    assert t.size == (W, H)
    for qy in (0, 1):
        for qx in (0, 1):
            assert a[qy * H // 2:(qy + 1) * H // 2, qx * W // 2:(qx + 1) * W // 2].mean() > 0.02    # every quarter has marks
    assert a.mean() < 0.5                                                                           # but it isn't a wall of text


def test_logo_is_scaled_and_keeps_its_transparency(tmp_path):
    logo = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    ImageDraw.Draw(logo).ellipse((20, 10, 180, 90), fill=(255, 0, 0, 255))
    p = tmp_path / "logo.png"
    logo.save(p)
    layer = watermark.overlay((1000, 800), Mark(logo=str(p), position="c", size=.3, opacity=1))
    box = bbox_of_alpha(layer)
    assert abs((box[2] - box[0]) - 240) < 20                       # 30% of 1000, minus the logo's own transparent margin
    arr = np.asarray(layer)
    assert tuple(arr[400, 500][:3]) == (255, 0, 0) and arr[0, 0, 3] == 0


def test_a_mark_larger_than_the_page_is_cut_not_an_error():
    out = watermark.overlay((100, 60), Mark(text="HUGE HUGE HUGE", size=3.0, position="br"))
    assert out.size == (100, 60)


def test_missing_text_logo_or_unreadable_logo(tmp_path):
    with pytest.raises(UserError, match="text or choose a logo"):
        watermark.overlay((100, 100), Mark())
    with pytest.raises(UserError, match="logo file is missing"):
        watermark.overlay((100, 100), Mark(logo=str(tmp_path / "x.png")))
    bad = tmp_path / "bad.png"
    bad.write_text("nope")
    with pytest.raises(UserError, match="read the logo"):
        watermark.overlay((100, 100), Mark(logo=str(bad)))


def test_apply_keeps_modes_and_changes_only_where_the_mark_is():
    base = Image.new("RGB", (400, 300), (30, 60, 90))
    out = watermark.apply(base, Mark(text="WM", position="br", size=.2, opacity=1, shadow=False))
    assert out.mode == "RGB" and out.size == base.size
    d = np.abs(np.asarray(out, dtype=int) - np.asarray(base, dtype=int)).sum(-1) > 0
    ys, xs = np.where(d)
    assert xs.min() > 200 and ys.min() > 200 and d[:150, :150].sum() == 0
    rgba = watermark.apply(Image.new("RGBA", (50, 50), (0, 0, 0, 0)), Mark(text="W", position="c", size=.8))
    assert rgba.mode == "RGBA"


def test_watermark_image_keeps_format_exif_and_original(tmp_path):
    src = tmp_path / "Holiday.jpg"
    ex = Image.Exif()
    ex[0x010F] = "Canon"
    ex[0x0112] = 6                       # camera says: rotate on display
    Image.new("RGB", (300, 200), (50, 120, 50)).save(src, exif=ex)
    before = src.read_bytes()
    out = watermark.watermark_image(src, Mark(text="Mine", size=.3))
    assert out == tmp_path / "Holiday Watermarked.jpg"
    with Image.open(out) as im:
        assert im.format == "JPEG"
        assert im.size == (200, 300)                                  # upright, as open_image shows it
        assert im.getexif().get(0x010F) == "Canon" and im.getexif().get(0x0112) == 1   # no double rotation
    assert src.read_bytes() == before


@pytest.mark.parametrize("name,fmt", [("a.png", "PNG"), ("a.webp", "WEBP"), ("a.bmp", "BMP"), ("a.tif", "TIFF"),
                                      ("a.gif", "PNG"), ("a.jpeg", "JPEG")])
def test_other_formats(tmp_path, name, fmt):
    src = tmp_path / name
    Image.new("RGB", (120, 90), (10, 20, 30)).save(src)
    out = watermark.watermark_image(src, Mark(text="x"))
    with Image.open(out) as im:
        assert im.format == fmt


def make_pdf(path, rotations=(0,), size=(300, 200)):
    doc = pymupdf.open()
    for rot in rotations:
        page = doc.new_page(width=size[0], height=size[1])
        page.insert_text((20, 40), "Page text stays", fontsize=14)
        if rot:
            page.set_rotation(rot)
    doc.save(path)
    doc.close()
    return path


def shown(page, dpi=150):
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_pdf_watermark_matches_the_overlay_on_displayed_pages(tmp_path, rotation):
    src = make_pdf(tmp_path / "doc.pdf", (rotation,))
    mark = Mark(text="CONFIDENTIAL", position="br", size=.45, opacity=.8, shadow=False, color="#ff0000")
    out = watermark.watermark_pdf(src, mark)
    assert out == tmp_path / "doc Watermarked.pdf"
    with pymupdf.open(src) as a, pymupdf.open(out) as b:
        before, after = shown(a[0]), shown(b[0])
        changed = np.abs(np.asarray(after, dtype=int) - np.asarray(before, dtype=int)).sum(-1) > 30
        wanted = np.asarray(watermark.overlay(before.size, mark).getchannel("A")) > 128
        iou = (changed & wanted).sum() / (changed | wanted).sum()
        assert iou > 0.7, (rotation, iou)                      # what you see is the upright overlay, same place
        ys, xs = np.where(changed)
        assert xs.mean() > before.width * .5 and ys.mean() > before.height * .6    # bottom-right of the *displayed* page
        assert "Page text stays" in b[0].get_text()            # original text still real text


def test_pdf_all_pages_and_mixed_sizes(tmp_path):
    doc = pymupdf.open()
    for w, h in [(300, 200), (300, 200), (200, 300)]:
        doc.new_page(width=w, height=h)
    p = tmp_path / "m.pdf"
    doc.save(p)
    doc.close()
    out = watermark.watermark_pdf(p, Mark(text="X", position="c", size=.5))
    with pymupdf.open(out) as d:
        assert d.page_count == 3 and all(len(pg.get_images()) == 1 for pg in d)


def test_protected_pdf_is_refused(tmp_path):
    doc = pymupdf.open()
    doc.new_page()
    p = tmp_path / "locked.pdf"
    doc.save(p, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    doc.close()
    with pytest.raises(UserError, match="password"):
        watermark.watermark_pdf(p, Mark(text="x"))
