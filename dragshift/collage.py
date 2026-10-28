"""Combine several images into one: an animated GIF or a contact sheet."""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .convert import atomic_save, flatten, open_image, open_preview, save_image
from .rename import natural_key
from .util import UserError, sibling

GIF_MAX_SIDE = 720       # longest side of the animation
GIF_FRAME_MS = 500
SHEET_CELL = 280         # thumbnail box
SHEET_GAP = 18
SHEET_LABEL = 26
PAPER = (255, 255, 255)
INK = (90, 90, 96)


def _ordered(srcs) -> list[Path]:
    return sorted((Path(s) for s in srcs), key=natural_key)


def animated_gif(srcs: list[str | Path], frame_ms: int = GIF_FRAME_MS) -> list[Path]:
    """One looping GIF, frames in name order. Every frame is fitted onto the first image's size."""
    files = _ordered(srcs)
    if len(files) < 2:
        raise UserError("Drop at least two images to make an animation.")
    first = flatten(open_image(files[0]))
    scale = min(1.0, GIF_MAX_SIDE / max(first.size))
    size = (max(1, round(first.width * scale)), max(1, round(first.height * scale)))
    frames = [ImageOps.fit(first, size, Image.LANCZOS) if first.size != size else first]
    for f in files[1:]:
        frames.append(ImageOps.pad(flatten(open_image(f)), size, Image.LANCZOS, color=PAPER))
    out = sibling(files[0], "Animated", ".gif")
    atomic_save(lambda dst: frames[0].save(dst, "GIF", save_all=True, append_images=frames[1:],
                                           duration=frame_ms, loop=0, disposal=2), out)
    return [out]


def _label(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while len(text) > 1 and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def contact_sheet(srcs: list[str | Path]) -> list[Path]:
    """A grid of labelled thumbnails in one JPG."""
    files = _ordered(srcs)
    if len(files) < 2:
        raise UserError("Drop at least two images to make a contact sheet.")
    cols = math.ceil(math.sqrt(len(files)))
    rows = math.ceil(len(files) / cols)
    cw, ch = SHEET_CELL, SHEET_CELL + SHEET_LABEL
    sheet = Image.new("RGB", (SHEET_GAP + cols * (cw + SHEET_GAP), SHEET_GAP + rows * (ch + SHEET_GAP)), PAPER)
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=14)
    for i, f in enumerate(files):
        x = SHEET_GAP + (i % cols) * (cw + SHEET_GAP)
        y = SHEET_GAP + (i // cols) * (ch + SHEET_GAP)
        try:
            thumb = flatten(open_preview(f, SHEET_CELL * 2))
        except Exception:      # unreadable image: leave a grey box instead of failing the whole sheet
            thumb = Image.new("RGB", (cw, cw), (225, 225, 228))
        thumb = ImageOps.contain(thumb, (cw, cw), Image.LANCZOS)
        sheet.paste(thumb, (x + (cw - thumb.width) // 2, y + (cw - thumb.height) // 2))
        draw.text((x + cw // 2, y + cw + 6), _label(draw, f.name, font, cw), fill=INK, font=font, anchor="ma")
    out = sibling(files[0], "Contact sheet", ".jpg")
    save_image(sheet, out, "JPG")
    return [out]
