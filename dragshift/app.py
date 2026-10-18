"""DragShift: Shift+drag any file to convert or edit it from a radial glass menu."""
from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import (QLockFile, QObject, QPoint, QPointF, QRectF, QRunnable, QSize, Qt,
                            QThreadPool, QTimer, Signal, QDir)
from PySide6.QtGui import QAction, QColor, QCursor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QLabel, QMenu,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

from . import convert, glass, imaging, tools
from .radial import Item, RadialMenu
from .trigger import DragShiftWatcher
from .util import category, file_size, human_size

APP_NAME = "DragShift"
VERSION = "1.0.0"
IS_WIN = sys.platform == "win32"

GLYPHS = {"Compress": "⇲", "Metadata": "ⓘ", "Redact": "▮", "Crop": "⌗", "Edit": "☼",
          "Annotate": "✎", "Add BG": "▣", "Split": "✂", "Rotate": "⟳", "Merge": "⧉"}


# ---------------------------------------------------------------- background jobs
class _Sig(QObject):
    done = Signal(str, list)
    failed = Signal(str, str)


class Job(QRunnable):
    def __init__(self, title: str, fn):
        super().__init__()
        self.title, self.fn, self.sig = title, fn, _Sig()

    def run(self):
        try:
            outs = self.fn() or []
            self.sig.done.emit(self.title, [str(o) for o in outs])
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.sig.failed.emit(self.title, str(e) or e.__class__.__name__)


# ---------------------------------------------------------------- toast
class Toast(QWidget):
    """Small glass notification bottom-right. Click to show the result in Explorer."""

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint |
                            Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet(glass.QSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(2)
        self.title = QLabel()
        self.title.setObjectName("title")
        self.sub = QLabel()
        self.sub.setObjectName("dim")
        lay.addWidget(self.title)
        lay.addWidget(self.sub)
        self.setFixedWidth(340)
        self.hide_timer = QTimer(self, singleShot=True, timeout=self.hide)
        self.target: str | None = None
        self.error = False

    def show_msg(self, title: str, sub: str, target: str | None = None, error=False):
        self.title.setText(("⚠  " if error else "✓  ") + title)
        self.sub.setText(sub)
        self.target, self.error = target, error
        self.adjustSize()
        g = (QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()).availableGeometry()
        self.move(g.right() - self.width() - 18, g.bottom() - self.height() - 18)
        self.show()
        self.raise_()
        self.hide_timer.start(6000 if error else 3500)

    def mouseReleaseEvent(self, e):
        if self.target:
            reveal(self.target)
        self.hide()

    def paintEvent(self, e):
        p = QPainter(self)
        path = glass.rounded_rect_path(QRectF(self.rect()).adjusted(1, 1, -1, -1), 14)
        tint = QColor(120, 30, 40, 225) if self.error else QColor(38, 30, 56, 230)
        glass.paint_glass(p, path, tint=tint, rainbow_alpha=190)


def reveal(path: str):
    if IS_WIN:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])


# ---------------------------------------------------------------- autostart
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" -m dragshift'


def autostart_enabled() -> bool:
    if not IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on: bool):
    if not IS_WIN:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


# ---------------------------------------------------------------- icon
def make_icon() -> QIcon:
    ico = Path(__file__).with_name("assets") / "dragshift.ico"
    if ico.exists():
        return QIcon(str(ico))
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(glass.iridescent_pen(QPointF(32, 32), 7, 255))
    p.drawEllipse(QPointF(32, 32), 24, 24)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255))
    p.drawEllipse(QPointF(32, 32), 9, 9)
    p.end()
    return QIcon(pm)


# ---------------------------------------------------------------- the app
class DragShiftApp(QObject):
    def __init__(self, qapp: QApplication, transient: bool = False):
        super().__init__()
        self.qapp = qapp
        self.transient = transient  # launched with files while another copy owns the tray
        self.pool = QThreadPool.globalInstance()
        self.windows: list[QWidget] = []
        self.toast = Toast()
        self.menu = RadialMenu(self.build_rings, self.describe)
        self.menu.activated.connect(self.run_item)

        self.watcher = DragShiftWatcher(self.menu.isVisible, lambda: self.menu.got_drag, self)
        self.watcher.show_menu.connect(lambda pos: self.menu.popup(pos))
        self.watcher.toggle_ring.connect(self.menu.next_ring)
        self.watcher.hide_menu.connect(self.menu.close_menu)
        if transient:
            self.menu.dismissed.connect(self._maybe_quit)
            QTimer(self, interval=1000, timeout=self._maybe_quit).start()
            return
        self.watcher.start()

        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip(f"{APP_NAME} — Shift while dragging a file")
        m = QMenu()
        m.setStyleSheet(glass.QSS)
        self.act_enabled = QAction("Enabled", m, checkable=True, checked=True)
        self.act_enabled.toggled.connect(lambda on: setattr(self.watcher, "enabled", on))
        m.addAction(self.act_enabled)
        m.addAction("Open a file…", self.open_dialog)
        m.addSeparator()
        if IS_WIN:
            self.act_auto = QAction("Start with Windows", m, checkable=True, checked=autostart_enabled())
            self.act_auto.toggled.connect(set_autostart)
            m.addAction(self.act_auto)
        m.addAction("How to use", self.show_help)
        m.addSeparator()
        m.addAction("Quit", self.qapp.quit)
        self.tray_menu = m
        self.tray.setContextMenu(m)
        self.tray.activated.connect(lambda r: self.open_dialog() if r == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()

    # ------------------------------------------------------------ menu contents
    def describe(self, paths: list[str]) -> str:
        if len(paths) > 1:
            return f"{len(paths)} files"
        return human_size(file_size(paths[0]))

    def build_rings(self, paths: list[str]) -> dict[str, list[Item]]:
        cat = category(paths[0])
        if cat is None:
            return {"Unsupported": []}
        ext = Path(paths[0]).suffix
        same = [p for p in paths if category(p) == cat]
        convert_items = [Item(t, key=f"convert:{t}") for t in convert.targets_for(cat, ext)]
        rings: dict[str, list[Item]] = {"Convert": convert_items}
        if cat == "image":
            names = ["Compress", "Metadata", "Redact", "Crop", "Edit", "Annotate", "Add BG"]
            rings["Tools"] = [Item(n, GLYPHS[n], key=f"tool:{n}") for n in names]
        elif cat == "pdf":
            names = ["Compress", "Metadata", "Split", "Rotate"] + (["Merge"] if len(same) > 1 else [])
            rings["Tools"] = [Item(n, GLYPHS[n], key=f"tool:{n}") for n in names]
        return rings

    # ------------------------------------------------------------ actions
    def run_item(self, item: Item, paths: list[str]):
        cat = category(paths[0])
        paths = [p for p in paths if category(p) == cat]
        kind, name = item.key.split(":", 1)
        if kind == "convert":
            if cat == "image" and name == "PDF" and len(paths) > 1:
                self.submit(f"Combined {len(paths)} images into PDF",
                            lambda: convert.images_to_single_pdf(paths))
                return
            for p in paths:
                self.submit(f"{Path(p).name} → {name}", lambda p=p: convert.convert(p, cat, name))
            return
        # tools
        if cat == "pdf":
            fns = {
                "Compress": lambda p: [imaging.compress_pdf(p)],
                "Split": imaging.split_pdf,
                "Rotate": lambda p: [imaging.rotate_pdf(p)],
            }
            if name == "Merge":
                self.submit(f"Merged {len(paths)} PDFs", lambda: [imaging.merge_pdfs(paths)])
            elif name == "Metadata":
                self.open_window(tools.MetadataWindow(paths[0], self.notify, is_pdf=True))
            else:
                for p in paths:
                    self.submit(f"{name}: {Path(p).name}", lambda p=p, f=fns[name]: f(p))
            return
        windows = {"Crop": tools.CropWindow, "Add BG": tools.AddBgWindow, "Annotate": tools.AnnotateWindow,
                   "Redact": tools.RedactWindow, "Edit": tools.EditWindow,
                   "Metadata": tools.MetadataWindow, "Compress": tools.CompressWindow}
        try:
            self.open_window(windows[name](paths[0], self.notify))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.toast.show_msg(f"Couldn't open {name}", str(e), error=True)

    def open_window(self, w: QWidget):
        self.windows.append(w)
        w.destroyed.connect(lambda *_: self.windows.remove(w) if w in self.windows else None)
        w.show()
        w.raise_()
        w.activateWindow()

    def submit(self, title: str, fn):
        job = Job(title, fn)
        job.sig.done.connect(self.notify)
        job.sig.failed.connect(lambda t, err: self.toast.show_msg(f"Failed: {t}", err, error=True))
        self.pool.start(job)

    def notify(self, title: str, outs: list):
        outs = [str(o) for o in outs]
        if not outs:
            self.toast.show_msg(title, "Nothing was written")
            return
        sub = Path(outs[0]).name if len(outs) == 1 else f"{len(outs)} files · {Path(outs[0]).name} …"
        self.toast.show_msg(title, sub + "   ·  click to show", outs[0])

    def _maybe_quit(self):
        if (self.transient and not self.menu.isVisible() and not self.windows
                and self.pool.activeThreadCount() == 0 and not self.toast.isVisible()):
            self.qapp.quit()

    # ------------------------------------------------------------ misc
    def open_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(None, "Choose file(s) for DragShift", QDir.homePath())
        if files:
            self.show_click_menu(files)

    def show_click_menu(self, files: list[str]):
        g = (QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()).availableGeometry()
        pos = QCursor.pos() if g.contains(QCursor.pos()) else g.center()
        self.menu.popup(pos, files)
        self.menu.activateWindow()

    def show_help(self):
        self.toast.show_msg("Drag a file, then press Shift",
                            "Drop it on a slice. Tap Shift again (or hover the centre) to switch Convert ⇄ Tools.")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    if IS_WIN:
        try:  # crisp taskbar/tray icon grouping
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DragShift.App")
        except Exception:
            pass
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    qapp = QApplication(argv)
    qapp.setApplicationName(APP_NAME)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setWindowIcon(make_icon())

    if "--selftest" in argv:
        return selftest()
    files = [a for a in argv[1:] if not a.startswith("-") and os.path.exists(a)]
    lock = QLockFile(os.path.join(QDir.tempPath(), "dragshift.lock"))
    owner = lock.tryLock(100)
    if not files and not owner:
        return 0  # already running in the tray
    app = DragShiftApp(qapp, transient=not owner)
    if files:  # e.g. "Send to → DragShift" or drag onto the .exe
        QTimer.singleShot(150, lambda: app.show_click_menu(files))
    elif "--quiet" not in argv:
        QTimer.singleShot(400, lambda: app.toast.show_msg(
            "DragShift is running", "Drag any file, then press Shift. Find me in the tray."))
    return qapp.exec()


def selftest() -> int:
    """Used by CI on the frozen .exe: exercise every library and build the UI, then exit."""
    import tempfile
    from PIL import Image
    try:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "t.png"
            Image.new("RGBA", (64, 48), (255, 100, 80, 255)).save(src)
            for t in convert.targets_for("image", ".png"):
                convert.convert(src, "image", t)
            pdf = convert.convert(src, "image", "PDF")[0]
            for t in convert.PDF_TARGETS:
                convert.convert(pdf, "pdf", t)
            (Path(d) / "n.txt").write_text("hello")
            docx = convert.convert(Path(d) / "n.txt", "text", "DOCX")[0]
            convert.convert(docx, "docx", "TXT")
            imaging.add_background(convert.open_image(src), imaging.Background())
            imaging.compress_pdf(pdf)
            menu = RadialMenu(lambda p: {"Convert": [Item("PNG")]}, lambda p: "1 KB")
            menu._load([str(src)])
            for cls in (tools.CropWindow, tools.AddBgWindow, tools.EditWindow, tools.AnnotateWindow,
                        tools.RedactWindow, tools.MetadataWindow, tools.CompressWindow):
                cls(str(src), lambda *a: None).close()
        print("SELFTEST OK")
        return 0
    except Exception:
        traceback.print_exc()
        print("SELFTEST FAILED")
        return 1
