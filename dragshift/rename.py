"""Batch rename: pattern with tokens, safe against overwrites, reversible.

Tokens: {name} original name · {n} counter · {date} today (YYYY-MM-DD) · {mdate} the file's
modified date · {parent} the folder name. The extension is always kept."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .util import UserError

TOKENS = {"name", "n", "date", "mdate", "parent"}
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def natural_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def clean(name: str) -> str:
    """Make a file stem safe on Windows (and everywhere else)."""
    name = _ILLEGAL.sub("_", name).strip().rstrip(".")
    if name.split(".")[0].upper() in _RESERVED:
        name = f"_{name}"
    return name


def check_pattern(pattern: str) -> None:
    if not pattern.strip():
        raise UserError("Type a name pattern.")
    for tok in re.findall(r"\{([^{}]*)\}", pattern):
        if tok not in TOKENS:
            raise UserError(f"Unknown {{{tok}}}. Use {{name}}, {{n}}, {{date}}, {{mdate}} or {{parent}}.")
    stripped = re.sub(r"\{[^{}]*\}", "", pattern)
    if "{" in stripped or "}" in stripped:
        raise UserError("Unbalanced { } in the pattern.")


def new_stem(pattern: str, path: Path, index: int, start: int = 1, digits: int = 1) -> str:
    n = str(start + index).zfill(max(1, digits))
    mdate = time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime)) if path.exists() else ""
    values = {"name": path.stem, "n": n, "date": time.strftime("%Y-%m-%d"),
              "mdate": mdate, "parent": path.parent.name}
    stem = re.sub(r"\{([^{}]*)\}", lambda m: values[m.group(1)], pattern)
    stem = clean(stem)
    if not stem:
        raise UserError("That pattern gives an empty file name.")
    return stem


def plan(paths: list[str | Path], pattern: str, start: int = 1, digits: int = 1) -> list[tuple[Path, Path]]:
    """(old, new) for every file, in natural name order. Raises UserError if any would collide
    with another file or with each other. Never plans an overwrite."""
    check_pattern(pattern)
    srcs = sorted((Path(p) for p in paths), key=natural_key)
    pairs = []
    for i, src in enumerate(srcs):
        pairs.append((src, src.with_name(new_stem(pattern, src, i, start, digits) + src.suffix)))

    def key(p: Path) -> str:
        return os.path.normcase(str(p))

    seen: dict[str, Path] = {}
    moving = {key(s) for s, _ in pairs}
    for src, dst in pairs:
        k = key(dst)
        if k in seen:
            raise UserError(f"Two files would both be called “{dst.name}”. Add {{n}} to the pattern.")
        seen[k] = src
        if k != key(src) and k not in moving and dst.exists():
            raise UserError(f"“{dst.name}” already exists. Nothing was renamed.")
    return pairs


def apply(pairs: list[tuple[Path, Path]]) -> list[Path]:
    """Rename old→new. Two steps through temporary names so swaps and chains (a→b, b→c) work;
    rolls back if anything fails. Returns the new paths in the same order."""
    todo = [(s, d) for s, d in pairs if str(s) != str(d)]
    temps: list[tuple[Path, Path, Path]] = []
    done: list[tuple[Path, Path, Path]] = []
    try:
        for i, (s, d) in enumerate(todo):
            t = s.with_name(f".dragshift-rename-{os.getpid()}-{i}{s.suffix}")
            os.rename(s, t)
            temps.append((s, d, t))
        for s, d, t in temps:
            os.rename(t, d)
            done.append((s, d, t))
    except OSError as e:
        for s, d, t in reversed(done):      # undo second step
            os.rename(d, t)
        for s, d, t in reversed(temps):     # undo first step
            os.rename(t, s)
        raise UserError(f"Couldn't rename (is a file open in another program?). Nothing was changed. {e}")
    return [d for _, d in pairs]


def undo(olds: list[str | Path], news: list[str | Path]) -> int:
    """Put the original names back. Returns how many files were restored."""
    pairs = [(Path(n), Path(o)) for o, n in zip(olds, news) if Path(n).exists()]
    moving = {os.path.normcase(str(n)) for n, _ in pairs}
    for n, o in pairs:
        k = os.path.normcase(str(o))
        if k != os.path.normcase(str(n)) and k not in moving and o.exists():
            raise UserError(f"“{o.name}” exists again, so the rename can't be undone safely.")
    apply(pairs)
    return len(pairs)
