"""Pure image/PDF processing used by the tools (no UI here, so it's testable)."""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps
from PIL.ExifTags import GPSTAGS, TAGS

from .convert import fitz, flatten, open_image, save_image
from .util import sibling

EXT_TARGET = {".jpg": "JPG", ".jpeg": "JPG", ".png": "PNG", ".webp": "WEBP", ".tif": "TIFF",
              ".tiff": "TIFF", ".bmp": "BMP", ".heic": "HEIC", ".heif": "HEIC",
              ".avif": "AVIF", ".gif": "GIF", ".ico": "ICO"}


def target_for(path: str | Path) -> str:
    return EXT_TARGET.get(Path(path).suffix.lower(), "PNG")


def save_like(im: Image.Image, src: str | Path, label: str, force_ext: str | None = None) -> Path:
    """Save `im` next to `src` as '<name> <label>.<ext>' in the source's format."""
    ext = force_ext or Path(src).suffix.lower()
    if ext in (".gif", ".ico"):
        ext = ".png"
    out = sibling(src, label, ext)
    save_image(im, out, target_for(out))
    return out


# ---------------------------------------------------------------- compress
def compress_image(src: str | Path, quality: int = 72, max_side: int | None = None,
                   to_webp: bool = False) -> Path:
    im = open_image(src)
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    ext = ".webp" if to_webp else Path(src).suffix.lower()
    out = sibling(src, "Compressed", ext)
    tgt = target_for(out)
    if tgt == "PNG":
        # lossy PNG: quantise to a palette (like pngquant), keeps transparency
        colors = max(16, min(256, int(quality * 2.56)))
        q = im.convert("RGBA").quantize(colors=colors, method=Image.Quantize.FASTOCTREE,
                                        dither=Image.Dither.FLOYDSTEINBERG)
        q.save(out, "PNG", optimize=True)
    elif tgt in ("BMP", "TIFF", "GIF", "ICO"):
        out = sibling(src, "Compressed", ".jpg")
        save_image(im, out, "JPG", quality=quality)
    else:
        save_image(im, out, tgt, quality=quality)
    return out


def compress_pdf(src: str | Path, image_dpi: int = 150, quality: int = 70) -> Path:
    out = sibling(src, "Compressed", ".pdf")
    with fitz.open(src) as doc:
        try:
            doc.rewrite_images(dpi_threshold=image_dpi + 10, dpi_target=image_dpi,
                               quality=quality, lossy=True, lossless=True, bitonal=True,
                               color=True, gray=True, set_to_gray=False)
        except Exception:
            pass  # older PyMuPDF: still get structural compression below
        doc.save(out, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True,
                 clean=True, use_objstms=1)
    return out


# ---------------------------------------------------------------- metadata
def read_image_metadata(src: str | Path) -> dict[str, str]:
    info: dict[str, str] = {}
    with Image.open(src) as im:
        info["Dimensions"] = f"{im.width} × {im.height}"
        info["Format"] = str(im.format)
        info["Mode"] = im.mode
        exif = im.getexif()
        for k, v in exif.items():
            info[TAGS.get(k, str(k))] = _fmt(v)
        try:
            for k, v in exif.get_ifd(0x8769).items():  # Exif sub-IFD (camera settings)
                info[TAGS.get(k, str(k))] = _fmt(v)
            gps = exif.get_ifd(0x8825)
            if gps:
                for k, v in gps.items():
                    info["GPS " + GPSTAGS.get(k, str(k))] = _fmt(v)
        except Exception:
            pass
        for k, v in (im.info or {}).items():
            if k in ("exif", "icc_profile", "xmp"):
                info[k.upper()] = f"{len(v)} bytes" if hasattr(v, "__len__") else "yes"
            elif isinstance(v, (str, int, float)):
                info[k] = str(v)
    return info


def _fmt(v) -> str:
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    s = str(v)
    return s if len(s) < 120 else s[:117] + "…"


def strip_image_metadata(src: str | Path) -> Path:
    im = open_image(src)  # exif_transpose applied, so orientation is baked in
    clean = Image.new(im.mode, im.size)
    clean.paste(im)
    return save_like(clean, src, "Clean")


def read_pdf_metadata(src: str | Path) -> dict[str, str]:
    with fitz.open(src) as doc:
        info = {k: str(v) for k, v in (doc.metadata or {}).items() if v}
        info["Pages"] = str(doc.page_count)
        if doc.get_xml_metadata():
            info["XMP"] = "present"
    return info


def strip_pdf_metadata(src: str | Path) -> Path:
    out = sibling(src, "Clean", ".pdf")
    with fitz.open(src) as doc:
        doc.set_metadata({})
        try:
            doc.del_xml_metadata()
        except Exception:
            pass
        doc.save(out, garbage=3, deflate=True)
    return out


# ---------------------------------------------------------------- pdf extras
def split_pdf(src: str | Path) -> list[Path]:
    outs = []
    with fitz.open(src) as doc:
        for i in range(doc.page_count):
            nd = fitz.open()
            nd.insert_pdf(doc, from_page=i, to_page=i)
            out = sibling(src, f"page {i + 1}", ".pdf")
            nd.save(out)
            nd.close()
            outs.append(out)
    return outs


def merge_pdfs(srcs: list[str | Path]) -> Path:
    out = sibling(srcs[0], "Merged", ".pdf")
    nd = fitz.open()
    for s in srcs:
        with fitz.open(s) as d:
            nd.insert_pdf(d)
    nd.save(out, garbage=3, deflate=True)
    nd.close()
    return out


def rotate_pdf(src: str | Path, degrees: int = 90) -> Path:
    out = sibling(src, "Rotated", ".pdf")
    with fitz.open(src) as doc:
        for p in doc:
            p.set_rotation((p.rotation + degrees) % 360)
        doc.save(out)
    return out


# ---------------------------------------------------------------- redact
def redact(im: Image.Image, rects: list[tuple[int, int, int, int]], mode: str = "black") -> Image.Image:
    im = im.copy()
    for (x0, y0, x1, y1) in rects:
        box = (max(0, min(x0, x1)), max(0, min(y0, y1)),
               min(im.width, max(x0, x1)), min(im.height, max(y0, y1)))
        if box[2] - box[0] < 1 or box[3] - box[1] < 1:
            continue
        if mode == "black":
            ImageDraw.Draw(im).rectangle(box, fill=(0, 0, 0, 255) if im.mode == "RGBA" else 0)
        else:
            region = im.crop(box)
            if mode == "pixelate":
                w, h = region.size
                f = max(8, min(w, h) // 6)
                region = region.resize((max(1, w // f), max(1, h // f)), Image.BILINEAR)
                region = region.resize((w, h), Image.NEAREST)
            else:  # blur – strong enough that text can't be recovered
                region = region.filter(ImageFilter.GaussianBlur(max(12, min(region.size) / 3)))
            im.paste(region, box[:2])
    return im


# ---------------------------------------------------------------- edit
@dataclass
class Adjust:
    brightness: float = 1.0
    contrast: float = 1.0
    saturation: float = 1.0
    sharpness: float = 1.0
    warmth: float = 0.0       # -1 … 1
    rotate: int = 0           # multiples of 90
    flip_h: bool = False
    flip_v: bool = False
    grayscale: bool = False
    resize_pct: int = 100


def apply_adjust(im: Image.Image, a: Adjust) -> Image.Image:
    alpha = im.split()[3] if im.mode == "RGBA" else None
    rgb = im.convert("RGB")
    if a.brightness != 1.0:
        rgb = ImageEnhance.Brightness(rgb).enhance(a.brightness)
    if a.contrast != 1.0:
        rgb = ImageEnhance.Contrast(rgb).enhance(a.contrast)
    if a.saturation != 1.0:
        rgb = ImageEnhance.Color(rgb).enhance(a.saturation)
    if a.sharpness != 1.0:
        rgb = ImageEnhance.Sharpness(rgb).enhance(a.sharpness)
    if a.warmth:
        r, g, b = rgb.split()
        k = a.warmth * 25
        r = r.point(lambda v: max(0, min(255, v + k)))
        b = b.point(lambda v: max(0, min(255, v - k)))
        rgb = Image.merge("RGB", (r, g, b))
    if a.grayscale:
        rgb = ImageOps.grayscale(rgb).convert("RGB")
    out = rgb
    if alpha is not None:
        out = rgb.convert("RGBA")
        out.putalpha(alpha)
    if a.flip_h:
        out = ImageOps.mirror(out)
    if a.flip_v:
        out = ImageOps.flip(out)
    if a.rotate % 360:
        out = out.rotate(-a.rotate, expand=True)
    if a.resize_pct != 100:
        w = max(1, round(out.width * a.resize_pct / 100))
        h = max(1, round(out.height * a.resize_pct / 100))
        out = out.resize((w, h), Image.LANCZOS)
    return out


# ---------------------------------------------------------------- add background
@dataclass
class Background:
    colors: list[str] = field(default_factory=lambda: ["#ff9a5a", "#ff3d6e", "#7b5cff"])
    angle: float = 135.0       # degrees, CSS-style
    padding: float = 0.12      # fraction of the longer image side
    radius: int = 24           # px on the source image scale
    shadow: float = 0.5        # 0 … 1
    aspect: str = "Auto"       # Auto, 1:1, 4:3, 16:9, 9:16, 4:5


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore


def gradient(size: tuple[int, int], colors: list[str], angle: float) -> Image.Image:
    import math
    w, h = size
    if len(colors) == 1:
        return Image.new("RGB", size, _hex(colors[0]))
    # Build a 1-px ramp, then stretch + rotate a big square and crop the centre.
    stops = [_hex(c) for c in colors]
    n = 512
    ramp = Image.new("RGB", (n, 1))
    px = ramp.load()
    for i in range(n):
        t = i / (n - 1) * (len(stops) - 1)
        j = min(int(t), len(stops) - 2)
        f = t - j
        a, b = stops[j], stops[j + 1]
        px[i, 0] = tuple(round(a[k] + (b[k] - a[k]) * f) for k in range(3))
    diag = int(math.hypot(w, h)) + 2
    big = ramp.resize((diag, diag), Image.BILINEAR)
    big = big.rotate(-(angle - 90), resample=Image.BICUBIC)
    left, top = (diag - w) // 2, (diag - h) // 2
    return big.crop((left, top, left + w, top + h))


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    scale = 4  # supersample for smooth corners
    m = Image.new("L", (size[0] * scale, size[1] * scale), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, m.width - 1, m.height - 1),
                                        radius=radius * scale, fill=255)
    return m.resize(size, Image.LANCZOS)


ASPECTS = {"1:1": 1.0, "4:3": 4 / 3, "16:9": 16 / 9, "9:16": 9 / 16, "4:5": 4 / 5, "3:2": 1.5}


def add_background(im: Image.Image, bg: Background) -> Image.Image:
    im = im.convert("RGBA")
    w, h = im.size
    pad = int(max(w, h) * bg.padding)
    cw, ch = w + 2 * pad, h + 2 * pad
    if bg.aspect in ASPECTS:
        r = ASPECTS[bg.aspect]
        if cw / ch < r:
            cw = int(round(ch * r))
        else:
            ch = int(round(cw / r))
    canvas = gradient((cw, ch), bg.colors, bg.angle).convert("RGBA")
    x, y = (cw - w) // 2, (ch - h) // 2
    radius = min(bg.radius, min(w, h) // 2)
    mask = rounded_mask((w, h), radius) if radius > 0 else Image.new("L", (w, h), 255)
    if im.mode == "RGBA":
        mask = ImageChops.multiply(mask, im.split()[3])
    if bg.shadow > 0:
        blur = max(4, int(max(w, h) * 0.035))
        sh = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        smask = Image.new("L", (cw, ch), 0)
        smask.paste(mask, (x, y + int(blur * 0.6)))
        smask = smask.filter(ImageFilter.GaussianBlur(blur))
        smask = smask.point(lambda v: int(v * 0.65 * bg.shadow))
        sh.putalpha(smask)
        canvas = Image.alpha_composite(canvas, sh)
    layer = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    layer.paste(im, (x, y), mask)
    return Image.alpha_composite(canvas, layer)


# ---------------------------------------------------------------- Qt bridge
def pil_to_png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()
