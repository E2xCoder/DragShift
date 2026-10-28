"""No welcome window and no tips; the 'running' toast only confirms a manual launch."""
import inspect
import re
from pathlib import Path

import pytest

from dragshift import app

PACKAGE = Path(app.__file__).parent


@pytest.mark.parametrize("argv,files,shown", [
    (["DragShift.exe"], [], True),                       # double-clicked: confirm it started
    (["DragShift.exe", "--quiet"], [], False),           # start with Windows: stay silent
    (["DragShift.exe", "photo.jpg"], ["photo.jpg"], False),   # Send to / right-click: the menu is the feedback
    (["DragShift.exe", "--quiet", "a.png"], ["a.png"], False),
])
def test_startup_toast_only_after_a_plain_manual_launch(argv, files, shown):
    assert app.startup_notice(argv, files) is shown


def test_main_never_opens_the_welcome_window_by_itself():
    src = inspect.getsource(app.main)
    assert "show_welcome" not in src and "welcomed" not in src
    assert "startup_notice(argv, files)" in src


def test_no_tip_or_donation_code_is_left_in_the_app():
    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in PACKAGE.glob("*.py"))
    assert not re.search(r"cash\.app|TIP_URL|Tip the developer|AtikaKadesha", text, re.I)


def test_tray_menu_keeps_on_demand_help_but_no_tip_item():
    init = inspect.getsource(app.DragShiftApp.__init__)
    assert '"How to use"' in init and "Tip the" not in init and "♥" not in init
    assert "QDesktopServices" not in inspect.getsource(app).split("class _Sig")[0]       # the import went with it


def test_help_window_still_opens_when_asked():
    from PySide6.QtWidgets import QApplication
    from dragshift.settings import Settings, WelcomeWindow
    from dragshift.window import GlassWindow
    qa = QApplication.instance() or QApplication([])
    w = WelcomeWindow(Settings(), "Shift")                # built only; not closed, so nothing is saved
    assert qa is not None and isinstance(w, GlassWindow)
