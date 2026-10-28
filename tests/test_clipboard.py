"""Copy-to-clipboard tool."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication

from dragshift import app, clipboard


_APP = None  # module-level so the Qt app outlives every test (a freed app crashes at exit)


def _qapp():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


@pytest.fixture(autouse=True)
def _clear_clipboard():
    """The headless 'offscreen' platform crashes at interpreter exit if our QMimeData is still
    on the clipboard (the real Windows platform does not)."""
    yield
    _qapp().clipboard().clear()


def test_copy_tool_is_in_the_image_menu():
    assert "Copy" in app.FILE_TOOLS["image"]
    assert "Copy" in app.GLYPHS and "Copy" in app.HINTS


def test_single_image_goes_on_clipboard_as_pixels(tmp_path):
    qa = _qapp()
    p = tmp_path / "a.png"
    Image.new("RGB", (40, 30), (255, 0, 0)).save(p)
    msg = clipboard.copy_images([str(p)])
    img = qa.clipboard().image()
    assert (img.width(), img.height()) == (40, 30)
    assert img.pixelColor(5, 5).red() == 255
    assert "40×30" in msg


def test_several_images_go_on_clipboard_as_files(tmp_path):
    qa = _qapp()
    ps = []
    for n in ("a", "b"):
        p = tmp_path / f"{n}.png"
        Image.new("RGB", (8, 8)).save(p)
        ps.append(str(p))
    msg = clipboard.copy_images(ps)
    urls = [u.toLocalFile() for u in qa.clipboard().mimeData().urls()]
    assert len(urls) == 2 and urls[0].endswith("a.png")
    assert "2 files" in msg
