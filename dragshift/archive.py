"""Zip the dropped files into one archive next to them."""
from __future__ import annotations

import zipfile
from pathlib import Path

from .convert import atomic_save
from .util import UserError, output_dir, sibling, unique_path

# Already compressed: deflating them again costs time and saves nothing.
STORED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif", ".gif", ".zip",
               ".mp4", ".mp3", ".docx"}


def archive_name(srcs: list[Path]) -> Path:
    if len(srcs) == 1:
        return sibling(srcs[0], "", ".zip")
    folder = srcs[0].parent.name or "Files"
    return unique_path(output_dir(srcs[0]) / f"{folder} ({len(srcs)} files).zip")


def entry_names(srcs: list[Path]) -> list[str]:
    """File names inside the zip. Same-named files from different folders get (1), (2)…"""
    used: set[str] = set()
    names = []
    for s in srcs:
        name, i = s.name, 0
        while name.lower() in used:
            i += 1
            name = f"{s.stem} ({i}){s.suffix}"
        used.add(name.lower())
        names.append(name)
    return names


def zip_files(srcs: list[str | Path]) -> Path:
    files = [Path(s) for s in srcs if Path(s).is_file()]
    if not files:
        raise UserError("Those files are no longer there.")
    out = archive_name(files)
    names = entry_names(files)

    def write(dst):
        with zipfile.ZipFile(dst, "w") as z:
            for f, name in zip(files, names):
                kind = zipfile.ZIP_STORED if f.suffix.lower() in STORED_EXTS else zipfile.ZIP_DEFLATED
                z.write(f, name, compress_type=kind)

    atomic_save(write, out)
    return out
