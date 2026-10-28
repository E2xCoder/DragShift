"""Add DragShift to File Explorer's right-click menu and the Send to menu (Windows, current user only).

Registry (all under HKEY_CURRENT_USER, no admin rights, nothing machine-wide):
  Software\\Classes\\*\\shell\\DragShift            MUIVerb = "DragShift…", Icon = <exe>, MultiSelectModel = "Player"
  Software\\Classes\\*\\shell\\DragShift\\command   (Default) = "<exe>" "%1"
Send to: a shortcut  %APPDATA%\\Microsoft\\Windows\\SendTo\\DragShift.lnk  pointing at the exe.

On Windows 11 the classic right-click entry appears under "Show more options" (or Shift+F10); Explorer's
new compact menu only accepts entries from packaged shell extensions, which a portable app cannot add.
This matches what the project's own installer registers, so the two never conflict."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .util import UserError

CLASSES = r"Software\Classes"                          # under HKEY_CURRENT_USER
SENDTO_DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "SendTo"
VERB_NAME = "DragShift"
VERB_TEXT = "DragShift…"
LNK_NAME = "DragShift.lnk"


def available() -> bool:
    return sys.platform == "win32"


def _verb_key(base: str | None) -> str:
    return f"{base or CLASSES}\\*\\shell\\{VERB_NAME}"


def _target() -> tuple[str, str, str]:
    """(program, arguments that go before the file name, icon file) for the running copy of DragShift."""
    if getattr(sys, "frozen", False):
        return sys.executable, "", sys.executable
    return sys.executable, "-m dragshift", str(Path(__file__).with_name("assets") / "dragshift.ico")


def app_paths() -> tuple[str, str]:
    """(command line for the verb, icon file) for the running copy of DragShift."""
    exe, args, icon = _target()
    return f'"{exe}" {args + " " if args else ""}"%1"', icon


def _exe_of(command: str) -> str:
    """The program in a registry command line: '"C:\\x y\\a.exe" "%1"' -> 'C:\\x y\\a.exe'."""
    command = command.strip()
    if command.startswith('"'):
        return command[1:].split('"', 1)[0]
    return command.split(" ", 1)[0]


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


@dataclass
class Status:
    context: str = "off"           # "off" | "ours" | "other" (an entry pointing at a different copy)
    context_target: str = ""
    sendto: str = "off"
    sendto_target: str = ""


def _read_verb(base: str | None) -> str | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _verb_key(base) + r"\command") as k:
            return winreg.QueryValueEx(k, "")[0]
    except OSError:
        return None


def _lnk_target(path: Path) -> str:
    try:
        import win32com.client  # type: ignore
        return win32com.client.Dispatch("WScript.Shell").CreateShortCut(str(path)).TargetPath
    except Exception:      # noqa: BLE001
        return ""


def status(base: str | None = None, sendto_dir: Path | None = None) -> Status:
    st = Status()
    if not available():
        return st
    cmd, _ = app_paths()
    ours = _exe_of(cmd)
    found = _read_verb(base)
    if found is not None:
        st.context_target = _exe_of(found)
        st.context = "ours" if _same(st.context_target, ours) else "other"
    lnk = (sendto_dir or SENDTO_DIR) / LNK_NAME
    if lnk.exists():
        st.sendto_target = _lnk_target(lnk)
        st.sendto = "ours" if _same(st.sendto_target, ours) else "other"
    return st


# ---------------------------------------------------------------- right-click entry
def enable_context(replace: bool = False, base: str | None = None) -> None:
    """Create the right-click entry. An entry that points at a different copy of DragShift (for
    example the installed one) is only replaced when `replace` is true."""
    import winreg
    cmd, icon = app_paths()
    existing = _read_verb(base)
    if existing is not None and not _same(_exe_of(existing), _exe_of(cmd)) and not replace:
        raise UserError(f"The right-click entry already belongs to another copy of DragShift ({_exe_of(existing)}).")
    try:
        key = _verb_key(base)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
            winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, VERB_TEXT)
            winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon)
            winreg.SetValueEx(k, "MultiSelectModel", 0, winreg.REG_SZ, "Player")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key + r"\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, cmd)
    except OSError as e:
        raise UserError(f"Couldn't add the right-click entry: {e}")


def disable_context(base: str | None = None) -> bool:
    """Remove our entry (only the DragShift key and its command). True if there was one."""
    import winreg
    key = _verb_key(base)
    try:
        for sub in (key + r"\command", key):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        raise UserError(f"Couldn't remove the right-click entry: {e}")


# ---------------------------------------------------------------- Send to
def enable_sendto(replace: bool = False, sendto_dir: Path | None = None) -> None:
    folder = sendto_dir or SENDTO_DIR
    lnk = folder / LNK_NAME
    exe, args, icon = _target()
    if lnk.exists() and not replace and not _same(_lnk_target(lnk), exe):
        raise UserError("The Send to shortcut already belongs to another copy of DragShift.")
    try:
        import win32com.client  # type: ignore
        folder.mkdir(parents=True, exist_ok=True)
        sc = win32com.client.Dispatch("WScript.Shell").CreateShortCut(str(lnk))
        sc.TargetPath = exe
        sc.Arguments = args
        sc.IconLocation = icon
        sc.Save()
    except Exception as e:      # noqa: BLE001
        raise UserError(f"Couldn't add the Send to shortcut: {e}")


def disable_sendto(sendto_dir: Path | None = None) -> bool:
    lnk = (sendto_dir or SENDTO_DIR) / LNK_NAME
    try:
        lnk.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        raise UserError(f"Couldn't remove the Send to shortcut: {e}")
