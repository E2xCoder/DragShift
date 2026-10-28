"""Rename window + result summary/undo + menu wiring."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from dragshift import app, report, tools

_APP = None  # module-level so the Qt app outlives the tests


def _qapp():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def make(tmp_path, *names):
    out = []
    for n in names:
        p = tmp_path / n
        p.write_text(n)
        out.append(str(p))
    return out


def test_rename_is_in_both_tool_rings():
    assert "Rename" in app.FILE_TOOLS["image"] and "Rename" in app.OTHER_FILE_TOOLS
    assert "Rename" in app.GLYPHS and "Rename" in app.HINTS


def test_window_previews_and_renames_in_order_with_undo(tmp_path):
    _qapp()
    ps = make(tmp_path, "b.png", "a.png", "c.png")
    calls = []
    w = tools.RenameWindow(ps, lambda *a: calls.append(a))
    assert w.pattern.text() == "{name}_{n}"
    assert w.primary_btn.isEnabled()
    assert "a.png  →  a_1.png" in w.preview.text()

    w.pattern.setText("Trip {n}")
    w.start.setValue(5)
    w.digits.setValue(2)
    assert "a.png  →  Trip 05.png" in w.preview.text()

    w.save()                                   # no app attached → runs synchronously
    action, srcs, outs, _extra = calls[0]
    assert action == "tool:Rename"
    assert srcs == ps and [os.path.basename(o) for o in outs] == ["Trip 06.png", "Trip 05.png", "Trip 07.png"]
    assert sorted(os.listdir(tmp_path)) == ["Trip 05.png", "Trip 06.png", "Trip 07.png"]

    result = report.summarize(action, srcs, outs, _extra)
    assert result.title == "Renamed 3 files" and result.undo is not None
    assert result.undo() == 3
    assert sorted(os.listdir(tmp_path)) == ["a.png", "b.png", "c.png"]
    assert (tmp_path / "b.png").read_text() == "b.png"


def test_window_blocks_bad_patterns_and_no_op(tmp_path):
    _qapp()
    ps = make(tmp_path, "a.png")
    w = tools.RenameWindow(ps, lambda *a: None)
    assert not w.primary_btn.isEnabled()          # {name} alone changes nothing
    assert "Nothing would change" in w.preview.text()
    w.pattern.setText("{bogus}")
    assert not w.primary_btn.isEnabled() and "Unknown" in w.preview.text()
    w.pattern.setText("x")
    assert w.primary_btn.isEnabled()
