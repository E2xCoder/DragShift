"""Remembers the last settings you used in each tool window (watermark text, rename pattern…).

Stored next to the app's other settings (on Windows: HKEY_CURRENT_USER\\Software\\DragShift), one
JSON text per tool, only the keys the tool asks for. Nothing about the files themselves is stored."""
from __future__ import annotations

import json

from PySide6.QtCore import QSettings

APP = "DragShift"
GROUP = "tools"


def _store() -> QSettings:
    return QSettings(APP, APP)


def _coerce(value, default):
    """`value` if it has the same type as `default`, else `default` (old or hand-edited data)."""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        return value if isinstance(value, int) and not isinstance(value, bool) else default
    if isinstance(default, float):
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    if isinstance(default, str):
        return value if isinstance(value, str) else default
    return default


def load(tool: str, defaults: dict) -> dict:
    """The saved settings of `tool`, with `defaults` for anything missing or invalid."""
    try:
        raw = _store().value(f"{GROUP}/{tool}", "")
        data = json.loads(raw) if isinstance(raw, str) and raw else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:      # noqa: BLE001  (a corrupt entry must never stop a tool window opening)
        data = {}
    return {k: _coerce(data.get(k, d), d) for k, d in defaults.items()}


def save(tool: str, values: dict) -> None:
    try:
        s = _store()
        s.setValue(f"{GROUP}/{tool}", json.dumps(values, ensure_ascii=False))
        s.sync()
    except Exception:      # noqa: BLE001  (not being able to remember is not worth an error)
        pass


def forget_all() -> int:
    """Delete every remembered tool setting. Returns how many tools had something saved."""
    s = _store()
    s.beginGroup(GROUP)
    n = len(s.childKeys())
    s.remove("")
    s.endGroup()
    s.sync()
    return n
