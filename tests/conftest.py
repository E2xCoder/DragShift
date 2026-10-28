"""Shared test setup."""
import sys
import uuid

import pytest
from PySide6.QtCore import QSettings

from dragshift import remember, shellmenu

TEST_ROOT = r"Software\DragShiftTests"          # the only registry area tests may write to


def delete_tree(path: str) -> None:
    """Recursively delete a test registry key. Refuses anything outside TEST_ROOT."""
    if sys.platform != "win32":
        return
    import winreg
    assert path.startswith(TEST_ROOT + "\\"), f"refusing to delete {path!r}"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as k:
            while True:
                try:
                    sub = winreg.EnumKey(k, 0)
                except OSError:
                    break
                delete_tree(path + "\\" + sub)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass


@pytest.fixture(autouse=True)
def _isolated_remembered_settings(tmp_path_factory, monkeypatch):
    """Tool windows remember their last settings; tests must neither read nor write the real ones."""
    ini = str(tmp_path_factory.mktemp("settings") / "dragshift.ini")
    monkeypatch.setattr(remember, "_store", lambda: QSettings(ini, QSettings.IniFormat))
    return ini


@pytest.fixture(scope="session", autouse=True)
def _remove_test_registry_root():
    """After the whole run, drop the (now empty) parent key the sandboxes lived under."""
    yield
    if sys.platform == "win32":
        import winreg
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_ROOT)     # only succeeds if it has no subkeys left
        except OSError:
            pass


@pytest.fixture(autouse=True)
def shell_sandbox(tmp_path_factory, monkeypatch):
    """The right-click and Send to entries go to a throwaway registry key and folder, never the real ones."""
    base = rf"{TEST_ROOT}\{uuid.uuid4().hex}\Classes"
    sendto = tmp_path_factory.mktemp("sendto")
    monkeypatch.setattr(shellmenu, "CLASSES", base)
    monkeypatch.setattr(shellmenu, "SENDTO_DIR", sendto)
    yield base, sendto
    delete_tree(base.rsplit("\\Classes", 1)[0])
