"""Copy images to the system clipboard (one image → pixels, several → a file list)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image
from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QGuiApplication, QImage

from .convert import open_image


def pil_to_qimage(im: Image.Image) -> QImage:
    im = im.convert("RGBA")
    w, h = im.size
    # .copy() detaches the QImage from the Python bytes buffer
    return QImage(im.tobytes("raw", "RGBA"), w, h, 4 * w, QImage.Format.Format_RGBA8888).copy()


def copy_images(paths: list[str]) -> str:
    """Put the images on the clipboard. Must run on the GUI thread. Returns a short summary."""
    clip = QGuiApplication.clipboard()
    if len(paths) == 1:
        im = open_image(paths[0])
        clip.setImage(pil_to_qimage(im))
        return f"{im.width}×{im.height} image copied. Paste it anywhere."
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(Path(p).resolve())) for p in paths])
    clip.setMimeData(mime)
    return f"{len(paths)} files copied. Paste them in a folder."
