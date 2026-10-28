"""Right-click and Send to entries. Everything here runs against a throwaway registry key and folder."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from dragshift import shellmenu
from dragshift.settings import Settings, SettingsWindow
from dragshift.util import UserError

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

REAL_KEY = r"Software\Classes\*\shell\DragShift"
EXE = r"C:\Program Files\Drag Shift\DragShift.exe"
OTHER = r"D:\Elsewhere\DragShift.exe"

_APP = None  # module-level so the Qt app outlives the tests


def _qapp():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def real_verb_exists() -> bool:
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, REAL_KEY))
        return True
    except OSError:
        return False


@pytest.fixture
def frozen(monkeypatch):
    """Behave like the packaged app: the running program is a DragShift.exe."""
    monkeypatch.setattr(shellmenu, "_target", lambda: (EXE, "", EXE))


def values(base, sub=""):
    import winreg
    out = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{base}\\*\\shell\\DragShift{sub}") as k:
        i = 0
        while True:
            try:
                name, val, _ = winreg.EnumValue(k, i)
            except OSError:
                return out
            out[name] = val
            i += 1


def write_foreign(base, exe=OTHER):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{base}\\*\\shell\\DragShift\\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}" "%1"')


def test_the_sandbox_is_active_and_the_real_entries_are_not_touched(shell_sandbox):
    base, sendto = shell_sandbox
    assert base.startswith(r"Software\DragShiftTests") and shellmenu.CLASSES == base
    assert str(sendto) == str(shellmenu.SENDTO_DIR) and "Microsoft" not in str(sendto)
    before = real_verb_exists()
    shellmenu.enable_context()
    assert real_verb_exists() == before                                # the real key is exactly as it was


def test_entry_matches_what_the_installer_registers(frozen, shell_sandbox):
    base, _ = shell_sandbox
    shellmenu.enable_context()
    assert values(base) == {"MUIVerb": "DragShift…", "Icon": EXE, "MultiSelectModel": "Player"}
    assert values(base, r"\command") == {"": f'"{EXE}" "%1"'}          # spaces in the path are quoted


def test_running_from_source_uses_python_m_dragshift(shell_sandbox):
    base, _ = shell_sandbox
    shellmenu.enable_context()
    cmd = values(base, r"\command")[""]
    assert cmd == f'"{sys.executable}" -m dragshift "%1"'
    assert values(base)["Icon"].endswith("dragshift.ico")


def test_status_off_ours_and_other(frozen, shell_sandbox):
    base, _ = shell_sandbox
    assert shellmenu.status() == shellmenu.Status()
    shellmenu.enable_context()
    st = shellmenu.status()
    assert st.context == "ours" and st.context_target == EXE
    shellmenu.disable_context()
    write_foreign(base)
    st = shellmenu.status()
    assert st.context == "other" and st.context_target == OTHER


def test_another_copys_entry_is_only_replaced_when_asked(frozen, shell_sandbox):
    base, _ = shell_sandbox
    write_foreign(base)
    with pytest.raises(UserError, match="another copy"):
        shellmenu.enable_context()
    assert values(base, r"\command")[""] == f'"{OTHER}" "%1"'           # untouched
    shellmenu.enable_context(replace=True)
    assert shellmenu.status().context == "ours"


def test_enable_twice_is_harmless_and_disable_removes_only_our_key(frozen, shell_sandbox):
    import winreg
    base, _ = shell_sandbox
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{base}\\*\\shell\\SomeoneElse\\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "notepad.exe")
    shellmenu.enable_context()
    shellmenu.enable_context()
    assert shellmenu.disable_context() is True
    assert shellmenu.status().context == "off"
    assert shellmenu.disable_context() is False                          # nothing left to remove
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{base}\\*\\shell\\SomeoneElse\\command") as k:
        assert winreg.QueryValueEx(k, "")[0] == "notepad.exe"             # a neighbour's entry survived


def test_exe_parsing():
    assert shellmenu._exe_of(r'"C:\a b\x.exe" "%1"') == r"C:\a b\x.exe"
    assert shellmenu._exe_of(r"C:\ab\x.exe %1") == r"C:\ab\x.exe"
    assert shellmenu._same(r"c:\A\b.EXE", r"C:\a\..\a\b.exe")


def test_send_to_shortcut_roundtrip(frozen, shell_sandbox):
    _, sendto = shell_sandbox
    shellmenu.enable_sendto()
    lnk = sendto / "DragShift.lnk"
    assert lnk.exists() and shellmenu._lnk_target(lnk) == EXE
    assert shellmenu.status().sendto == "ours"
    assert shellmenu.disable_sendto() is True and not lnk.exists()
    assert shellmenu.disable_sendto() is False


def test_send_to_of_another_copy_is_respected(frozen, monkeypatch, shell_sandbox):
    _, sendto = shell_sandbox
    monkeypatch.setattr(shellmenu, "_target", lambda: (OTHER, "", OTHER))
    shellmenu.enable_sendto()                                           # "another copy" makes it
    monkeypatch.setattr(shellmenu, "_target", lambda: (EXE, "", EXE))
    assert shellmenu.status().sendto == "other"
    with pytest.raises(UserError, match="another copy"):
        shellmenu.enable_sendto()
    shellmenu.enable_sendto(replace=True)
    assert shellmenu.status().sendto == "ours"


def test_settings_window_toggles(frozen, shell_sandbox):
    _qapp()
    w = SettingsWindow(Settings())
    assert not w.ctx.isChecked() and not w.send.isChecked()
    w.ctx.click()
    assert shellmenu.status().context == "ours" and w.ctx.isChecked()
    w.send.click()
    assert shellmenu.status().sendto == "ours"
    w.ctx.click()
    w.send.click()
    assert shellmenu.status() == shellmenu.Status() and not w.ctx.isChecked()
    assert "Show more options" in w.shell_note.text()


def test_settings_window_offers_to_take_over_another_copys_entry(frozen, shell_sandbox):
    _qapp()
    base, _ = shell_sandbox
    write_foreign(base)
    w = SettingsWindow(Settings())
    assert not w.ctx.isChecked() and OTHER in w.shell_note.text()        # not silently taken
    assert shellmenu.status().context == "other"
    w.ctx.click()                                                        # the user's explicit choice
    assert shellmenu.status().context == "ours" and w.ctx.isChecked()


def test_settings_window_reports_failures_and_unticks(frozen, monkeypatch, shell_sandbox):
    _qapp()
    w = SettingsWindow(Settings())

    def boom(**kw):
        raise UserError("Access denied")
    monkeypatch.setattr(shellmenu, "enable_context", boom)
    w.ctx.click()
    assert not w.ctx.isChecked() and "Access denied" in w.shell_note.text()
