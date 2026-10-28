"""Animated GIF + contact sheet."""
import pytest
from PIL import Image

from dragshift import app, collage, report
from dragshift.util import UserError


def imgs(folder, specs):
    folder.mkdir(exist_ok=True)
    out = []
    for name, size, color in specs:
        p = folder / name
        Image.new("RGB", size, color).save(p)
        out.append(p)
    return out


def test_animated_gif_has_every_frame_in_name_order_and_loops(tmp_path):
    ps = imgs(tmp_path, [("f10.png", (60, 40), (0, 0, 255)), ("f2.png", (60, 40), (255, 0, 0)),
                         ("f1.png", (60, 40), (0, 255, 0))])
    (out,) = collage.animated_gif(ps)
    assert out.name == "f1 Animated.gif"
    with Image.open(out) as g:
        assert g.n_frames == 3 and g.info["loop"] == 0 and g.info["duration"] == 500
        colors = []
        for i in range(3):
            g.seek(i)
            colors.append(g.convert("RGB").getpixel((5, 5)))
    assert colors == [(0, 255, 0), (255, 0, 0), (0, 0, 255)]          # f1, f2, f10


def test_gif_frames_are_fitted_to_the_first_image_and_big_ones_are_capped(tmp_path):
    ps = imgs(tmp_path, [("a.png", (1600, 800), (10, 10, 10)), ("b.png", (200, 900), (250, 250, 0))])
    (out,) = collage.animated_gif(ps)
    with Image.open(out) as g:
        assert g.size == (720, 360)
        g.seek(1)
        assert g.size == (720, 360)


def test_contact_sheet_is_one_jpg_with_a_cell_per_image(tmp_path):
    ps = imgs(tmp_path, [(f"p{i}.png", (300, 200), (i * 40, 90, 200)) for i in range(5)])
    (out,) = collage.contact_sheet(ps)
    assert out.suffix == ".jpg" and out.name == "p0 Contact sheet.jpg"
    with Image.open(out) as sh:
        cols, rows = 3, 2                                             # ceil(sqrt(5)) columns
        assert sh.size == (collage.SHEET_GAP + cols * (collage.SHEET_CELL + collage.SHEET_GAP),
                           collage.SHEET_GAP + rows * (collage.SHEET_CELL + collage.SHEET_LABEL + collage.SHEET_GAP))
        # centre of the first thumbnail carries the first image's colour (0, 90, 200)
        x = collage.SHEET_GAP + collage.SHEET_CELL // 2
        y = collage.SHEET_GAP + collage.SHEET_CELL // 2
        r, g_, b = sh.getpixel((x, y))
        assert abs(r - 0) < 12 and abs(g_ - 90) < 12 and abs(b - 200) < 12


def test_unreadable_image_does_not_break_the_sheet(tmp_path):
    ps = imgs(tmp_path, [("a.png", (50, 50), (1, 2, 3))])
    bad = tmp_path / "b.png"
    bad.write_text("not an image")
    (out,) = collage.contact_sheet(ps + [bad])
    assert out.exists()


@pytest.mark.parametrize("fn", [collage.animated_gif, collage.contact_sheet])
def test_one_image_is_rejected(tmp_path, fn):
    ps = imgs(tmp_path, [("a.png", (10, 10), (1, 1, 1))])
    with pytest.raises(UserError, match="at least two"):
        fn(ps)


def test_never_overwrites_and_leaves_no_temp_files(tmp_path):
    ps = imgs(tmp_path, [("a.png", (20, 20), (1, 1, 1)), ("b.png", (20, 20), (9, 9, 9))])
    (tmp_path / "a Animated.gif").write_text("precious")
    (out,) = collage.animated_gif(ps)
    assert out.name == "a Animated (1).gif" and (tmp_path / "a Animated.gif").read_text() == "precious"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_menu_offers_both_only_for_several_images(tmp_path):
    ps = [str(p) for p in imgs(tmp_path, [("a.png", (9, 9), (1, 1, 1)), ("b.png", (9, 9), (1, 1, 1))])]
    labels = lambda paths: [i.key.split(":", 1)[1] for i in app.DragShiftApp.build_rings(None, paths)["Convert"]]
    assert "Animated GIF" in labels(ps) and "Contact sheet" in labels(ps)
    assert "Animated GIF" not in labels(ps[:1]) and "Contact sheet" not in labels(ps[:1])


def test_summary_text(tmp_path):
    ps = imgs(tmp_path, [("a.png", (40, 40), (1, 1, 1)), ("b.png", (40, 40), (9, 9, 9))])
    (out,) = collage.animated_gif(ps)
    r = report.summarize("convert:Animated GIF", [str(p) for p in ps], [out])
    assert r.title == "2 files → 1 Animated GIF"
