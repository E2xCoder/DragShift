"""Watermarks for images and PDFs: text or a logo, in a corner, centred, or tiled across the page.

Everything is sized relative to the page, so a preview on a small copy looks the same as the
saved result. The mark is drawn once as a transparent layer and composited."""
from __future__ import annotations

import io
import math
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .convert import open_image, open_pdf, save_like_source
from .util import UserError, sibling

POSITIONS = ["tl", "tc", "tr", "ml", "c", "mr", "bl", "bc", "br"]      # 3×3 grid, row by row
PDF_DPI = 150                                                           # resolution of the PDF overlay
FONT_FILES = ["segoeuib.ttf", "arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf", "Helvetica.ttc"]


@dataclass
class Mark:
    text: str = ""
    logo: str = ""               # path to an image; used instead of the text when set
    position: str = "br"         # one of POSITIONS, or "tile"
    opacity: float = 0.5         # 0..1
    size: float = 0.30           # width of one mark as a share of the page width
    angle: int = 0               # degrees, counter-clockwise (the whole grid when tiled)
    margin: float = 0.03         # distance from the edges, share of the shorter side
    color: str = "#ffffff"       # text colour
    shadow: bool = True          # soft dark halo so white text stays readable on light areas
    gap: float = 1.0             # tile spacing, in mark sizes


@lru_cache(maxsize=64)
def _font(px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in FONT_FILES:
        for folder in (Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts", Path("/Library/Fonts"),
                       Path("/System/Library/Fonts"), Path("/usr/share/fonts/truetype/dejavu")):
            p = folder / name
            if p.exists():
                return ImageFont.truetype(str(p), px)
        try:
            return ImageFont.truetype(name, px)            # found by name on the system font path
        except OSError:
            continue
    return ImageFont.load_default(size=px)


def check(mark: Mark) -> None:
    if mark.logo:
        if not Path(mark.logo).is_file():
            raise UserError("The logo file is missing.")
    elif not mark.text.strip():
        raise UserError("Type the watermark text or choose a logo.")


def _text_layer(mark: Mark, target_w: int) -> Image.Image:
    text = mark.text.strip("\n")
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    ref = 200
    box = probe.multiline_textbbox((0, 0), text, font=_font(ref), spacing=ref // 5, align="center")
    px = max(8, round(ref * target_w / max(1, box[2] - box[0])))
    font, spacing = _font(px), px // 5
    box = probe.multiline_textbbox((0, 0), text, font=font, spacing=spacing, align="center")
    pad = round(px * 0.2) if mark.shadow else 2
    w, h = math.ceil(box[2] - box[0]) + 2 * pad, math.ceil(box[3] - box[1]) + 2 * pad     # multi-line boxes are fractional
    at = (pad - box[0], pad - box[1])
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if mark.shadow:
        halo = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(halo).multiline_text((at[0] + px * 0.03, at[1] + px * 0.05), text, font=font,
                                            fill=(0, 0, 0, 190), spacing=spacing, align="center")
        layer.alpha_composite(halo.filter(ImageFilter.GaussianBlur(max(1, px * 0.06))))
    ImageDraw.Draw(layer).multiline_text(at, text, font=font, fill=mark.color, spacing=spacing, align="center")
    return layer


def _logo_layer(mark: Mark, target_w: int, max_h: int) -> Image.Image:
    try:
        logo = open_image(mark.logo).convert("RGBA")
    except Exception as e:      # noqa: BLE001
        raise UserError(f"Couldn't read the logo: {e}")
    w = max(4, target_w)
    h = max(4, round(logo.height * w / logo.width))
    if h > max_h:
        w, h = max(4, round(w * max_h / h)), max_h
    return logo.resize((w, h), Image.LANCZOS)


def _mark_layer(mark: Mark, size: tuple[int, int]) -> Image.Image:
    W, H = size
    target_w = max(8, round(mark.size * W))
    layer = _logo_layer(mark, target_w, round(H * 0.9)) if mark.logo else _text_layer(mark, target_w)
    if mark.opacity < 1:
        a = layer.getchannel("A").point(lambda v: round(v * mark.opacity))
        layer.putalpha(a)
    return layer


def overlay(size: tuple[int, int], mark: Mark) -> Image.Image:
    """A transparent RGBA layer of `size` carrying only the watermark."""
    check(mark)
    W, H = size
    layer = _mark_layer(mark, size)
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    if mark.position == "tile":
        w, h = layer.size
        sx, sy = max(1, round(w * (1 + mark.gap))), max(1, round(h * (1 + mark.gap)))
        side = int(math.hypot(W, H)) + sx + sy                 # big enough to survive rotating the grid
        grid = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        for row, y in enumerate(range(0, side - h, sy)):
            shift = sx // 2 if row % 2 else 0                  # every other row is offset by half a step
            for x in range(0, side - w - shift, sx):
                grid.alpha_composite(layer, (x + shift, y))
        if mark.angle:
            grid = grid.rotate(mark.angle, resample=Image.BICUBIC)
        left, top = (side - W) // 2, (side - H) // 2
        return grid.crop((left, top, left + W, top + H))
    if mark.angle:
        layer = layer.rotate(mark.angle, expand=True, resample=Image.BICUBIC)
    w, h = layer.size
    m = round(mark.margin * min(W, H))
    row, col = divmod(POSITIONS.index(mark.position) if mark.position in POSITIONS else 8, 3)
    x = (m, (W - w) // 2, W - w - m)[col]
    y = (m, (H - h) // 2, H - h - m)[row]
    x, y = max(0, min(x, W - 1)), max(0, min(y, H - 1))
    clip = layer.crop((0, 0, min(w, W - x), min(h, H - y)))     # a mark larger than the page is cut, not an error
    out.alpha_composite(clip, (x, y))
    return out


def apply(im: Image.Image, mark: Mark) -> Image.Image:
    """`im` with the watermark on it (RGBA stays RGBA, everything else comes back RGB)."""
    keep_alpha = im.mode == "RGBA"
    base = im.convert("RGBA")
    base.alpha_composite(overlay(base.size, mark))
    return base if keep_alpha else base.convert("RGB")


# ---------------------------------------------------------------- files
def watermark_image(src: str | Path, mark: Mark) -> Path:
    check(mark)
    return save_like_source(apply(open_image(src), mark), src, "Watermarked")


def watermark_pdf(src: str | Path, mark: Mark) -> Path:
    check(mark)
    src = Path(src)
    layers: dict[tuple[int, int], bytes] = {}          # pages of one size share one overlay
    with open_pdf(src) as doc:
        if doc.needs_pass:
            raise UserError("This PDF is password-protected.")
        for page in doc:
            rect = page.rect                                   # as displayed (rotation applied)
            size = (max(1, round(rect.width * PDF_DPI / 72)), max(1, round(rect.height * PDF_DPI / 72)))
            if size not in layers:
                buf = io.BytesIO()
                overlay(size, mark).save(buf, "PNG", optimize=True)
                layers[size] = buf.getvalue()
            raw = (rect * page.derotation_matrix).normalize()  # where the page really lives, unrotated
            page.insert_image(raw, stream=layers[size], rotate=page.rotation, overlay=True)
        out = sibling(src, "Watermarked", ".pdf")
        doc.save(out, garbage=3, deflate=True)
    return out
