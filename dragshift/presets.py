"""Presets: one-click recipes for images, such as 'Web: 1600 px, JPG 82%' or 'Email: under 500 KB'.

Each preset is a name plus: longest side, output format, quality, an optional size cap and whether to
drop camera info and GPS. They are listed in the menu's Presets ring and edited from there."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from PIL import Image

from . import imaging, remember
from .convert import HEIF_OK, open_image, save_image, upright_exif
from .util import UserError, sibling

FORMATS = ["SAME", "JPG", "PNG", "WEBP"]
FORMAT_LABELS = {"SAME": "Same as original", "JPG": "JPG", "PNG": "PNG", "WEBP": "WEBP"}
NAME_MAX = 12                      # longer names get clipped on the menu's petals
KEY = "presets"
LIMITS = {"max_side": (0, 20000), "quality": (30, 100), "max_kb": (0, 100000)}


@dataclass
class Preset:
    name: str
    max_side: int = 0              # pixels on the longer side; 0 keeps the size (never enlarges)
    fmt: str = "SAME"              # one of FORMATS
    quality: int = 85
    max_kb: int = 0                # 0 = no limit; otherwise quality (then size) drops until it fits
    strip: bool = False            # remove camera info and GPS location

    def describe(self) -> str:
        bits = [f"{self.max_side} px" if self.max_side else "full size",
                (self.fmt if self.fmt != "SAME" else "same format")
                + (f" {self.quality}%" if self.fmt in ("SAME", "JPG", "WEBP") else "")]
        if self.max_kb:
            bits.append(f"≤{self.max_kb} KB")
        if self.strip:
            bits.append("no GPS")
        return " · ".join(bits)


DEFAULT_PRESETS = [
    Preset("Web", 1600, "JPG", 82, 0, True),
    Preset("Email", 1200, "JPG", 80, 500, True),
    Preset("Thumb", 320, "JPG", 75, 0, True),
]


def clean(p: Preset) -> Preset:
    """A copy with every field forced into range (hand-edited or old data can't break anything)."""
    d = asdict(p)
    d["name"] = "".join(c for c in re.sub(r'[<>:"/\|?*]', "", str(d["name"])) if c >= " ")   # it becomes part of a file name
    d["name"] = d["name"].strip()[:NAME_MAX].rstrip(". ")
    d["fmt"] = d["fmt"] if d["fmt"] in FORMATS else "SAME"
    d["strip"] = bool(d["strip"])
    for k, (lo, hi) in LIMITS.items():
        v = d[k]
        d[k] = min(hi, max(lo, v)) if isinstance(v, int) and not isinstance(v, bool) else Preset.__dataclass_fields__[k].default
    return Preset(**d)


def load() -> list[Preset]:
    """The saved presets, or the built-in three if nothing was ever saved. An empty saved list stays empty."""
    try:
        raw = remember._store().value(KEY, None)
        if raw is None:
            return [Preset(**asdict(p)) for p in DEFAULT_PRESETS]
        data = json.loads(raw)
        known = {f.name for f in fields(Preset)}
        out, seen = [], set()
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            p = clean(Preset(**{k: v for k, v in item.items() if k in known}))
            if p.name and p.name.lower() not in seen:
                seen.add(p.name.lower())
                out.append(p)
        return out
    except Exception:      # noqa: BLE001  (never let bad saved data stop the menu from opening)
        return [Preset(**asdict(p)) for p in DEFAULT_PRESETS]


def save(presets: list[Preset]) -> None:
    try:
        s = remember._store()
        s.setValue(KEY, json.dumps([asdict(clean(p)) for p in presets], ensure_ascii=False))
        s.sync()
    except Exception:      # noqa: BLE001
        pass


def find(name: str) -> Preset | None:
    return next((p for p in load() if p.name == name), None)


def unique_name(base: str, existing: list[Preset]) -> str:
    taken = {p.name.lower() for p in existing}
    if base.lower() not in taken:
        return base
    i = 2
    while f"{base[:NAME_MAX - 3]} {i}".lower() in taken:
        i += 1
    return f"{base[:NAME_MAX - 3]} {i}"


def _target(src: Path, p: Preset) -> str:
    if p.fmt != "SAME":
        return p.fmt
    t = imaging.target_for(src)
    if t in ("GIF", "ICO") or (t == "HEIC" and not HEIF_OK):
        return "PNG" if t != "HEIC" else "JPG"
    return t


def apply_preset(src: str | Path, preset: Preset) -> Path:
    """Write the recipe's result next to `src` as '<name> <preset>.<ext>'. The original is untouched."""
    p = clean(preset)
    if not p.name:
        raise UserError("This preset has no name.")
    src = Path(src)
    im = open_image(src)
    if p.max_side and max(im.size) > p.max_side:
        im.thumbnail((p.max_side, p.max_side), Image.LANCZOS)
    target = _target(src, p)
    quality = p.quality
    if p.max_kb and target not in ("BMP", "TIFF"):         # lossless containers can't be squeezed by quality
        q, side = imaging.plan_target_size(im, target, p.max_kb * 1024)
        quality = min(quality, q)
        if side and side < max(im.size):
            im.thumbnail((side, side), Image.LANCZOS)
    out = sibling(src, p.name, ".jpg" if target == "JPG" else "." + target.lower())
    save_image(im, out, target, quality=quality, exif=None if p.strip else upright_exif(src))
    return out
