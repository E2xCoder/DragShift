"""Tool windows: Compress, Resize, Crop, Edit, Annotate, Redact, Add BG, Metadata, Protect.

Every window opens on a fast, reduced-size preview. Saving runs on the full-resolution
image in the background (the window closes immediately; a notification reports the result)."""
from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Callable

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QGuiApplication, QImage, QKeySequence,
                           QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QShortcut)
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QColorDialog, QComboBox, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QSlider, QSpinBox, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from . import imaging, presets, remember, rename, upscale, watermark
from .util import UserError, ui_family
from .convert import fitz, open_image, open_pdf, open_preview
from .glass import pil_from_qimage, qimage_from_pil
from .util import file_size, human_size, sibling
from .window import GlassWindow, ImageView

Notify = Callable[..., None]


# ---------------------------------------------------------------- small widgets
def section(text: str) -> QLabel:
    l = QLabel(text.upper())
    l.setObjectName("section")
    return l


def slider(lo: int, hi: int, val: int) -> QSlider:
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(val)
    return s


def toggle_row(labels: list[str], checked: int = 0) -> tuple[QHBoxLayout, QButtonGroup]:
    row = QHBoxLayout()
    row.setSpacing(6)
    grp = QButtonGroup(row)
    grp.setExclusive(True)
    for i, t in enumerate(labels):
        b = QPushButton(t)
        b.setCheckable(True)
        b.setChecked(i == checked)
        grp.addButton(b, i)
        row.addWidget(b)
    row.addStretch(1)
    return row, grp


def slider_row(grid: QGridLayout, r: int, label: str, s: QSlider, fmt: Callable[[int], str]) -> QLabel:
    grid.addWidget(QLabel(label), r, 0)
    grid.addWidget(s, r, 1)
    val = QLabel(fmt(s.value()))
    val.setObjectName("dim")
    val.setMinimumWidth(56)
    val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    s.valueChanged.connect(lambda v: val.setText(fmt(v)))
    grid.addWidget(val, r, 2)
    return val


def dims_text(w: int, h: int) -> str:
    return f"{w} × {h}"


class ToolWindow(GlassWindow):
    tool_name = ""

    def __init__(self, title: str, paths, notify: Notify, size=(820, 680)):
        super().__init__(title, size=size)
        self.paths = [Path(p) for p in (paths if isinstance(paths, (list, tuple)) else [paths])]
        self.path = self.paths[0]
        self.notify = notify
        self.submit = None   # set by the app: run work in the background with a spinner
        if len(self.paths) == 1:
            sub = f"{self.path.name}  ·  {human_size(file_size(self.path))}"
        else:
            total = sum(file_size(p) for p in self.paths)
            sub = f"{len(self.paths)} files  ·  {human_size(total)}"
        self.subtitle = QLabel(sub)
        self.subtitle.setObjectName("dim")
        self.body.addWidget(self.subtitle)
        QShortcut(QKeySequence(Qt.Key_Return), self, activated=self._enter)
        QShortcut(QKeySequence(Qt.Key_Enter), self, activated=self._enter)
        self.primary_btn: QPushButton | None = None

    def _enter(self):
        from PySide6.QtWidgets import QApplication
        fw = QApplication.focusWidget()
        if isinstance(fw, QLineEdit) and isinstance(fw.parent(), ImageView):
            fw.returnPressed.emit()   # Enter in an on-canvas text box finishes the text
            return
        if self.primary_btn is not None and self.primary_btn.isEnabled():
            self.primary_btn.click()

    def footer(self, primary_text: str, on_primary, on_reset=None, extra_buttons=()) -> QHBoxLayout:
        row = QHBoxLayout()
        if on_reset:
            reset = QPushButton("Reset")
            reset.clicked.connect(on_reset)
            row.addWidget(reset)
        for b in extra_buttons:
            row.addWidget(b)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.close)
        row.addWidget(cancel)
        ok = QPushButton(primary_text)
        ok.setObjectName("primary")
        ok.clicked.connect(on_primary)
        row.addWidget(ok)
        self.primary_btn = ok
        return row

    def run(self, fn: Callable[[], list], working: str, extra: str = ""):
        """Save in the background (window closes now) or, without the app, synchronously."""
        action = f"tool:{self.tool_name}"
        if self.submit is not None:
            self.submit(action, [str(p) for p in self.paths], fn, working, extra)
            self.close()
        else:
            outs = fn()
            self.notify(action, [str(p) for p in self.paths], outs, extra)
            self.close()


# ================================================================ CROP
class CropView(ImageView):
    HANDLE = 9

    def __init__(self, image: QImage, k: float):
        super().__init__(image)
        self.k = k
        self.ratio: float | None = None
        self.sel = QRectF(0, 0, image.width(), image.height())
        self._mode: str | None = None
        self._start = QPointF()
        self._orig = QRectF()

    def set_ratio(self, ratio: float | None):
        self.ratio = ratio
        if ratio:
            W, H = self.image.width(), self.image.height()
            w, h = W, W / ratio
            if h > H:
                h, w = H, H * ratio
            self.sel = QRectF((W - w) / 2, (H - h) / 2, w, h)
        self.update()
        self.changed.emit()

    def reset_sel(self):
        self.sel = QRectF(0, 0, self.image.width(), self.image.height())
        if self.ratio:
            self.set_ratio(self.ratio)
        self.update()
        self.changed.emit()

    def _hit(self, pos: QPointF) -> str | None:
        r = self.rect_to_widget(self.sel)
        h = self.HANDLE + 4
        near = lambda a, b: abs(a - b) <= h
        l, t, rt, b = near(pos.x(), r.left()), near(pos.y(), r.top()), near(pos.x(), r.right()), near(pos.y(), r.bottom())
        inside_x = r.left() - h <= pos.x() <= r.right() + h
        inside_y = r.top() - h <= pos.y() <= r.bottom() + h
        if t and l: return "tl"
        if t and rt: return "tr"
        if b and l: return "bl"
        if b and rt: return "br"
        if t and inside_x: return "t"
        if b and inside_x: return "b"
        if l and inside_y: return "l"
        if rt and inside_y: return "r"
        if r.contains(pos): return "move"
        return None

    CURSORS = {"tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor, "tr": Qt.SizeBDiagCursor,
               "bl": Qt.SizeBDiagCursor, "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
               "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor, "move": Qt.SizeAllCursor}

    def mousePressEvent(self, e):
        self._mode = self._hit(e.position())
        if self._mode is None and self.target_rect().contains(e.position()):
            self._mode = "new"
        self._start = self.clamp_image(self.to_image(e.position()))
        self._orig = QRectF(self.sel)

    def mouseDoubleClickEvent(self, e):
        self.reset_sel()

    def mouseMoveEvent(self, e):
        if not self._mode:
            m = self._hit(e.position())
            self.setCursor(self.CURSORS.get(m, Qt.CrossCursor))
            return
        p = self.clamp_image(self.to_image(e.position()))
        W, H = self.image.width(), self.image.height()
        r = QRectF(self._orig)
        if self._mode == "move":
            d = p - self._start
            r.translate(d)
            r.moveLeft(min(max(r.left(), 0), W - r.width()))
            r.moveTop(min(max(r.top(), 0), H - r.height()))
        elif self._mode == "new":
            r = QRectF(self._start, p).normalized()
            if self.ratio and r.height() > 0:
                r.setHeight(r.width() / self.ratio)
        else:
            if "l" in self._mode: r.setLeft(p.x())
            if "r" in self._mode: r.setRight(p.x())
            if "t" in self._mode: r.setTop(p.y())
            if "b" in self._mode: r.setBottom(p.y())
            r = r.normalized()
            if self.ratio:
                if self._mode in ("t", "b"):
                    r.setWidth(r.height() * self.ratio)
                else:
                    nh = r.width() / self.ratio
                    if "t" in self._mode:
                        r.setTop(r.bottom() - nh)
                    else:
                        r.setHeight(nh)
        r = r.intersected(QRectF(0, 0, W, H))
        if r.width() >= 4 and r.height() >= 4:
            self.sel = r
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, e):
        self._mode = None

    def paint_overlay(self, p: QPainter, t: QRectF):
        r = self.rect_to_widget(self.sel)
        shade = QPainterPath()
        shade.addRect(t)
        hole = QPainterPath()
        hole.addRect(r)
        p.fillPath(shade.subtracted(hole), QColor(0, 0, 0, 140))
        p.setPen(QPen(QColor(255, 255, 255, 230), 1.5))
        p.drawRect(r)
        p.setPen(QPen(QColor(255, 255, 255, 70), 1))
        for i in (1, 2):  # rule of thirds
            x = r.left() + r.width() * i / 3
            y = r.top() + r.height() * i / 3
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ff6a4d"))
        for pt in (r.topLeft(), r.topRight(), r.bottomLeft(), r.bottomRight()):
            p.drawEllipse(pt, 5.5, 5.5)
        # live output size badge
        txt = dims_text(round(self.sel.width() * self.k), round(self.sel.height() * self.k))
        f = QFont(ui_family(False))
        f.setPixelSize(12)
        f.setBold(True)
        p.setFont(f)
        w = p.fontMetrics().horizontalAdvance(txt) + 18
        badge = QRectF(r.center().x() - w / 2, r.bottom() - 30, w, 22)
        if badge.top() < r.top() + 4:
            badge.moveTop(r.bottom() + 6)
        p.setBrush(QColor(20, 16, 32, 190))
        p.drawRoundedRect(badge, 11, 11)
        p.setPen(QColor(255, 255, 255))
        p.drawText(badge, Qt.AlignCenter, txt)


class CropWindow(ToolWindow):
    tool_name = "Crop"
    RATIOS = [("Free", None), ("Original", "orig"), ("1:1", 1.0), ("4:3", 4 / 3), ("3:2", 1.5),
              ("16:9", 16 / 9), ("9:16", 9 / 16), ("4:5", 0.8)]

    def __init__(self, path, notify):
        super().__init__("Crop Image", path, notify)
        pv = open_preview(path, 1400)
        with Image.open(path) as im:
            fw, fh = im.size
        from PIL import ImageOps
        with Image.open(path) as im:
            o = im.getexif().get(0x0112, 1)
        if o in (5, 6, 7, 8):
            fw, fh = fh, fw
        self.full_size = (fw, fh)
        self.rot = 0
        self.base = pv
        self.view = CropView(qimage_from_pil(pv), fw / pv.width)
        self.body.addWidget(self.view, 1)

        self.body.addWidget(section("Aspect ratio"))
        row, self.grp = toggle_row([n for n, _ in self.RATIOS])
        self.grp.idClicked.connect(self._ratio)
        for txt, d in (("⟲", -90), ("⟳", 90)):
            b = QPushButton(txt)
            b.setToolTip("Rotate 90°")
            b.clicked.connect(lambda _=False, d=d: self._rotate(d))
            row.addWidget(b)
        self.body.addLayout(row)

        dims = QHBoxLayout()
        self.w = QSpinBox()
        self.h = QSpinBox()
        for s in (self.w, self.h):
            s.setRange(1, 100000)
            s.setFixedWidth(96)
        dims.addWidget(QLabel("W"))
        dims.addWidget(self.w)
        dims.addWidget(QLabel("H"))
        dims.addWidget(self.h)
        dims.addWidget(QLabel("px"))
        dims.addStretch(1)
        tip = QLabel("Double-click the image to reset")
        tip.setObjectName("dim")
        dims.addWidget(tip)
        self.body.addLayout(dims)
        self.w.editingFinished.connect(self._from_spin)
        self.h.editingFinished.connect(self._from_spin)
        self.view.changed.connect(self._to_spin)
        self._to_spin()
        self.body.addLayout(self.footer("Apply", self.apply, self.reset))

    def _ratio(self, i):
        r = self.RATIOS[i][1]
        if r == "orig":
            r = self.view.image.width() / self.view.image.height()
        self.view.set_ratio(r)

    def _rotate(self, d):
        self.rot = (self.rot + d) % 360
        img = self.base.rotate(-self.rot, expand=True)
        k = self.view.k
        self.view.set_image(qimage_from_pil(img))
        self.view.k = k
        self.view.reset_sel()

    def _to_spin(self):
        s = self.view.sel
        k = self.view.k
        for box, v in ((self.w, s.width()), (self.h, s.height())):
            box.blockSignals(True)
            box.setValue(round(v * k))
            box.blockSignals(False)

    def _from_spin(self):
        k = self.view.k
        s = QRectF(self.view.sel)
        s.setWidth(self.w.value() / k)
        s.setHeight(self.h.value() / k)
        self.view.sel = s.intersected(QRectF(0, 0, self.view.image.width(), self.view.image.height()))
        self.view.update()
        self._to_spin()

    def reset(self):
        self.grp.button(0).setChecked(True)
        self.view.ratio = None
        if self.rot:
            self.rot = 0
            self.view.set_image(qimage_from_pil(self.base))
        self.view.reset_sel()

    def apply(self):
        s, k, rot, path = self.view.sel, self.view.k, self.rot, self.path
        fw, fh = (self.full_size[1], self.full_size[0]) if rot % 180 else self.full_size
        x0, y0 = round(s.left() * k), round(s.top() * k)
        w, h = round(s.width() * k), round(s.height() * k)
        # preview → full-size rounding: snap to the image edges when within 3 px
        if x0 <= 3:
            w, x0 = w + x0, 0
        if y0 <= 3:
            h, y0 = h + y0, 0
        snap_w = abs(x0 + w - fw) <= 3
        snap_h = abs(y0 + h - fh) <= 3
        if snap_w:
            w = fw - x0
        if snap_h:
            h = fh - y0
        if self.view.ratio:  # exact ratio (no 1600×1599)
            r = self.view.ratio
            if snap_h and not snap_w:
                w = round(h * r)
            else:
                h = round(w / r)
            if y0 + h > fh:
                h = fh - y0
                w = round(h * r)
            if x0 + w > fw:
                w = fw - x0
                h = round(w / r)
        box = (x0, y0, x0 + w, y0 + h)

        def work():
            im = open_image(path)
            if rot:
                im = im.rotate(-rot, expand=True)
            return [imaging.save_like(im.crop(box), path, "Cropped")]
        extra = f"rotated {rot}°" if rot else ""
        self.run(work, "Cropping…", extra)


# ================================================================ ADD BG
PRESETS = [
    ("Sunset", ["#ffb36b", "#ff5f6d", "#a24bff"]),
    ("Prism", ["#ff6b9d", "#ffb36b", "#ffe66b", "#7dffb0", "#6bd5ff", "#8a7bff"]),
    ("Peach", ["#ffd6a5", "#ff9a8b"]),
    ("Aurora", ["#00d2ff", "#7a5cff", "#ff5fd2"]),
    ("Mint", ["#a8ffce", "#3ad6c6"]),
    ("Ocean", ["#43cbff", "#2058d6"]),
    ("Dusk", ["#2b1055", "#d53a9d"]),
    ("Lemon", ["#fff59d", "#ffb74d"]),
    ("Night", ["#0f2027", "#2c5364"]),
    ("Rose", ["#ffdde1", "#ee9ca7"]),
]
SOLIDS = ["#ffffff", "#f2f2f7", "#d1d1d6", "#8e8e93", "#1c1c1e", "#000000", "#ff3b30",
          "#ff9500", "#ffcc00", "#34c759", "#00c7be", "#007aff", "#5856d6", "#af52de"]


class AddBgWindow(ToolWindow):
    tool_name = "Add BG"
    ASPECTS = ["Auto", "1:1", "4:3", "16:9", "9:16", "4:5"]

    def __init__(self, path, notify):
        super().__init__("Add BG", path, notify, size=(880, 780))
        self.small = open_preview(path, 760)
        with Image.open(path) as im:
            self.full_w = max(im.size) if im.getexif().get(0x0112, 1) in (5, 6, 7, 8) else im.width
            self.full_size = im.size
        self.colors = PRESETS[0][1]

        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(360, 300)
        self.body.addWidget(self.preview, 1)
        self.out_label = QLabel()
        self.out_label.setObjectName("dim")
        self.out_label.setAlignment(Qt.AlignCenter)
        self.body.addWidget(self.out_label)

        self.body.addWidget(section("Background"))
        sw = QGridLayout()
        sw.setSpacing(6)
        self.sw_grp = QButtonGroup(self)
        items = [("g", c) for _, c in PRESETS] + [("s", [c]) for c in SOLIDS]
        for i, (_, cols) in enumerate(items):
            b = QPushButton()
            b.setObjectName("swatch")
            b.setCheckable(True)
            b.setStyleSheet(self._swatch_css(cols))
            b.setChecked(i == 0)
            b.setToolTip(PRESETS[i][0] if i < len(PRESETS) else cols[0])
            b.clicked.connect(lambda _=False, c=cols: self._pick(c))
            self.sw_grp.addButton(b, i)
            sw.addWidget(b, i // 12, i % 12)
        custom = QPushButton("＋")
        custom.setObjectName("swatch")
        custom.setToolTip("Custom colour")
        custom.clicked.connect(self._custom)
        sw.addWidget(custom, len(items) // 12, len(items) % 12)
        self.body.addLayout(sw)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.pad = slider(0, 40, 12)
        self.rad = slider(0, 120, 24)
        self.sha = slider(0, 100, 50)
        self.ang = slider(0, 360, 135)
        slider_row(grid, 0, "Padding", self.pad, lambda v: f"{v}%")
        slider_row(grid, 1, "Corners", self.rad, lambda v: f"{v} px")
        slider_row(grid, 2, "Shadow", self.sha, lambda v: f"{v}%")
        slider_row(grid, 3, "Angle", self.ang, lambda v: f"{v}°")
        self.body.addLayout(grid)

        self.body.addWidget(section("Size"))
        row, self.asp = toggle_row(self.ASPECTS)
        self.body.addLayout(row)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setToolTip("Copy the result to the clipboard")
        self.copy_btn.clicked.connect(self.copy)
        self.body.addLayout(self.footer("Save with Background", self.save, self.reset, [self.copy_btn]))

        self._timer = QTimer(self, singleShot=True, interval=16, timeout=self.render)
        for s in (self.pad, self.rad, self.sha, self.ang):
            s.valueChanged.connect(lambda _: self._timer.start())
        self.asp.idClicked.connect(lambda _: self._timer.start())
        self.render()

    @staticmethod
    def _swatch_css(cols: list[str]) -> str:
        if len(cols) == 1:
            return f"background: {cols[0]};"
        stops = ", ".join(f"stop:{i / (len(cols) - 1):.2f} {c}" for i, c in enumerate(cols))
        return f"background: qlineargradient(x1:0,y1:0,x2:1,y2:1, {stops});"

    def _pick(self, cols):
        self.colors = cols
        self.render()

    def _custom(self):
        c = QColorDialog.getColor(QColor(self.colors[0]), self, "Background colour")
        if c.isValid():
            self.colors = [c.name()]
            self.render()

    def _params(self, scale: float) -> imaging.Background:
        return imaging.Background(colors=self.colors, angle=self.ang.value(),
                                  padding=self.pad.value() / 100,
                                  radius=int(self.rad.value() * scale),
                                  shadow=self.sha.value() / 100,
                                  aspect=self.ASPECTS[self.asp.checkedId()])

    def _full_scale(self) -> float:
        return max(1.0, max(self.full_size) / 1000)

    def render(self):
        k = max(self.small.size) / max(self.full_size)
        out = imaging.add_background(self.small, self._params(k * self._full_scale()))
        pm = QPixmap.fromImage(qimage_from_pil(out))
        self.preview.setPixmap(pm.scaled(self.preview.size() - QSize(10, 10), Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation))
        fw = round(out.width / k)
        fh = round(out.height / k)
        self.out_label.setText(f"Output  {dims_text(fw, fh)} px  ·  PNG")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "_timer"):
            self._timer.start()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self.render)

    def reset(self):
        self.pad.setValue(12); self.rad.setValue(24); self.sha.setValue(50); self.ang.setValue(135)
        self.asp.button(0).setChecked(True)
        self.sw_grp.button(0).setChecked(True)
        self._pick(PRESETS[0][1])

    def _compose_full(self) -> Image.Image:
        return imaging.add_background(open_image(self.path), self._params(self._full_scale()))

    def copy(self):
        QGuiApplication.clipboard().setImage(qimage_from_pil(self._compose_full()))
        self.copy_btn.setText("Copied ✓")
        QTimer.singleShot(1400, lambda: self.copy_btn.setText("Copy"))

    def save(self):
        params, path = self._params(self._full_scale()), self.path

        def work():
            out_im = imaging.add_background(open_image(path), params)
            out = sibling(path, "With Background", ".png")
            from .convert import save_image
            save_image(out_im, out, "PNG")
            return [out]
        self.run(work, "Adding background…")


# ================================================================ ANNOTATE
class Shape:
    def __init__(self, kind, color, width, pts=None, text=""):
        self.kind, self.color, self.width = kind, QColor(color), width
        self.pts: list[QPointF] = pts or []
        self.text = text


def draw_shape(p: QPainter, s: Shape, map_pt, scale: float):
    pts = [map_pt(q) for q in s.pts]
    w = max(1.0, s.width * scale)
    col = QColor(s.color)
    if s.kind == "Highlight":
        col.setAlpha(110)
        w *= 4
    pen = QPen(col, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if s.kind in ("Pen", "Highlight") and len(pts) > 1:
        path = QPainterPath(pts[0])
        for q in pts[1:]:
            path.lineTo(q)
        p.drawPath(path)
    elif s.kind == "Arrow" and len(pts) == 2:
        a, b = pts
        ang = math.atan2(b.y() - a.y(), b.x() - a.x())
        head = max(12.0, w * 4.2)
        base = QPointF(b.x() - head * 0.8 * math.cos(ang), b.y() - head * 0.8 * math.sin(ang))
        p.drawLine(a, base)
        left = QPointF(b.x() - head * math.cos(ang - 0.42), b.y() - head * math.sin(ang - 0.42))
        right = QPointF(b.x() - head * math.cos(ang + 0.42), b.y() - head * math.sin(ang + 0.42))
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawPolygon(QPolygonF([b, left, right]))
    elif s.kind == "Rect" and len(pts) == 2:
        p.drawRoundedRect(QRectF(pts[0], pts[1]).normalized(), w, w)
    elif s.kind == "Ellipse" and len(pts) == 2:
        p.drawEllipse(QRectF(pts[0], pts[1]).normalized())
    elif s.kind == "Text" and pts:
        f = QFont(ui_family(False))
        f.setPixelSize(max(8, int(s.width * 5 * scale)))
        f.setBold(True)
        p.setFont(f)
        # outline for legibility on any background
        outline = QPainterPath()
        outline.addText(pts[0], f, s.text)
        lum = col.red() * 0.3 + col.green() * 0.59 + col.blue() * 0.11
        p.setPen(QPen(QColor(0, 0, 0, 160) if lum > 128 else QColor(255, 255, 255, 190), max(2.0, w * 0.6)))
        p.drawPath(outline)
        p.fillPath(outline, col)
    elif s.kind == "Step" and pts:
        r = max(10.0, s.width * 3.2 * scale)
        c = pts[0]
        p.setPen(QPen(QColor(255, 255, 255), max(1.5, r * 0.12)))
        p.setBrush(col)
        p.drawEllipse(c, r, r)
        f = QFont(ui_family(False))
        f.setPixelSize(int(r * 1.15))
        f.setBold(True)
        p.setFont(f)
        lum = col.red() * 0.3 + col.green() * 0.59 + col.blue() * 0.11
        p.setPen(QColor(20, 20, 20) if lum > 170 else QColor(255, 255, 255))
        p.drawText(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), Qt.AlignCenter, s.text)


class AnnotateView(ImageView):
    def __init__(self, image: QImage, owner: "AnnotateWindow"):
        super().__init__(image)
        self.owner = owner
        self.shapes: list[Shape] = []
        self.redo_stack: list[Shape] = []
        self.cur: Shape | None = None
        self.editor: QLineEdit | None = None
        self.setCursor(Qt.CrossCursor)

    def next_step(self) -> int:
        return sum(1 for s in self.shapes if s.kind == "Step") + 1

    def _commit_text(self):
        ed = self.editor
        if ed is None:
            return
        self.editor = None
        text = ed.text().strip()
        if text:
            self.add(Shape("Text", ed.property("color"), ed.property("w"), [ed.property("pt")], text))
        ed.deleteLater()

    def add(self, s: Shape):
        self.shapes.append(s)
        self.redo_stack.clear()
        self.update()

    def mousePressEvent(self, e):
        self._commit_text()
        if e.button() != Qt.LeftButton or not self.target_rect().contains(e.position()):
            return
        o = self.owner
        p = self.to_image(e.position())
        width = o.stroke.value() / self.scale()  # keep on-screen thickness consistent
        if o.tool == "Text":
            ed = QLineEdit(self)
            ed.setPlaceholderText("Type, then Enter")
            ed.setProperty("pt", p)
            ed.setProperty("color", o.color)
            ed.setProperty("w", width)
            ed.move(int(e.position().x()), int(e.position().y()) - 16)
            ed.resize(220, 30)
            ed.show()
            ed.setFocus()
            ed.returnPressed.connect(self._commit_text)
            self.editor = ed
            return
        if o.tool == "Step":
            self.add(Shape("Step", o.color, width, [p], str(self.next_step())))
            return
        self.cur = Shape(o.tool, o.color, width, [p, p] if o.tool not in ("Pen", "Highlight") else [p])

    def mouseMoveEvent(self, e):
        if not self.cur:
            return
        p = self.clamp_image(self.to_image(e.position()))
        if self.cur.kind in ("Pen", "Highlight"):
            self.cur.pts.append(p)
        else:
            self.cur.pts[1] = p
        self.update()

    def mouseReleaseEvent(self, e):
        if self.cur:
            self.add(self.cur)
            self.cur = None

    def paint_overlay(self, p, t):
        s = self.scale()
        for sh in self.shapes + ([self.cur] if self.cur else []):
            draw_shape(p, sh, self.to_widget, s)


class AnnotateWindow(ToolWindow):
    tool_name = "Annotate"
    TOOLS = ["Arrow", "Rect", "Ellipse", "Pen", "Highlight", "Text", "Step"]
    COLORS = ["#ff3b30", "#ff9500", "#ffcc00", "#34c759", "#007aff", "#af52de", "#ffffff", "#000000"]

    def __init__(self, path, notify):
        super().__init__("Annotate", path, notify, size=(900, 720))
        pv = open_preview(path, 1600)
        with Image.open(path) as im:
            full = im.size if im.getexif().get(0x0112, 1) not in (5, 6, 7, 8) else im.size[::-1]
        self.k = full[0] / pv.width
        self.tool, self.color = "Arrow", self.COLORS[0]
        self.view = AnnotateView(qimage_from_pil(pv), self)
        self.body.addWidget(self.view, 1)

        row, grp = toggle_row(["➜ Arrow", "▢ Box", "◯ Circle", "✎ Pen", "▬ Marker", "T Text", "① Step"])
        grp.idClicked.connect(lambda i: setattr(self, "tool", self.TOOLS[i]))
        undo = QPushButton("Undo")
        undo.clicked.connect(self.undo)
        redo = QPushButton("Redo")
        redo.clicked.connect(self.redo)
        QShortcut(QKeySequence.Undo, self, activated=self.undo)
        QShortcut(QKeySequence.Redo, self, activated=self.redo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self.redo)
        row.addWidget(undo)
        row.addWidget(redo)
        self.body.addLayout(row)

        crow = QHBoxLayout()
        cgrp = QButtonGroup(self)
        for i, c in enumerate(self.COLORS):
            b = QPushButton()
            b.setObjectName("swatch")
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setStyleSheet(f"background: {c};")
            b.clicked.connect(lambda _=False, c=c: setattr(self, "color", c))
            cgrp.addButton(b)
            crow.addWidget(b)
        crow.addSpacing(16)
        crow.addWidget(QLabel("Size"))
        self.stroke = slider(1, 20, 4)
        self.stroke.setFixedWidth(160)
        crow.addWidget(self.stroke)
        crow.addStretch(1)
        self.body.addLayout(crow)
        self.body.addLayout(self.footer("Save", self.save))

    def undo(self):
        if self.view.shapes:
            self.view.redo_stack.append(self.view.shapes.pop())
            self.view.update()

    def redo(self):
        if self.view.redo_stack:
            self.view.shapes.append(self.view.redo_stack.pop())
            self.view.update()

    def save(self):
        self.view._commit_text()
        shapes = list(self.view.shapes)
        if not shapes:
            self.close()
            return
        k, path = self.k, self.path
        counts: dict[str, int] = {}
        for s in shapes:
            counts[s.kind] = counts.get(s.kind, 0) + 1
        names = {"Arrow": "arrow", "Rect": "box", "Ellipse": "circle", "Pen": "stroke", "Highlight": "highlight",
                 "Text": "label", "Step": "step"}
        extra = ", ".join(f"{n} {names[t]}{'s' if n > 1 else ''}" for t, n in counts.items())

        def work():
            img = qimage_from_pil(open_image(path).convert("RGBA"))
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.TextAntialiasing)
            for sh in shapes:
                draw_shape(p, sh, lambda q: QPointF(q.x() * k, q.y() * k), k)
            p.end()
            return [imaging.save_like(pil_from_qimage(img), path, "Annotated")]
        self.run(work, "Saving annotations…", extra)


# ================================================================ REDACT
class RedactView(ImageView):
    def __init__(self, image: QImage, owner: "RedactWindow"):
        super().__init__(image)
        self.owner = owner
        self.rects: list[QRectF] = []
        self.cur: QRectF | None = None
        self._a = QPointF()
        self.setCursor(Qt.CrossCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.target_rect().contains(e.position()):
            self._a = self.clamp_image(self.to_image(e.position()))
            self.cur = QRectF(self._a, self._a)

    def mouseMoveEvent(self, e):
        if self.cur is not None:
            self.cur = QRectF(self._a, self.clamp_image(self.to_image(e.position()))).normalized()
            self.update()

    def mouseReleaseEvent(self, e):
        if self.cur is not None and self.cur.width() > 2 and self.cur.height() > 2:
            self.rects.append(self.cur)
            self.owner.refresh()
        self.cur = None
        self.update()

    def paint_overlay(self, p, t):
        if self.cur is not None:
            p.setPen(QPen(QColor("#ff5f6d"), 1.5, Qt.DashLine))
            p.setBrush(QColor(0, 0, 0, 90))
            p.drawRect(self.rect_to_widget(self.cur))


class RedactWindow(ToolWindow):
    tool_name = "Redact"
    MODES = ["Black box", "Pixelate", "Blur"]

    def __init__(self, path, notify):
        super().__init__("Redact", path, notify)
        self.base = open_preview(path, 1400)
        with Image.open(path) as im:
            full = im.size if im.getexif().get(0x0112, 1) not in (5, 6, 7, 8) else im.size[::-1]
        self.k = full[0] / self.base.width
        self.view = RedactView(qimage_from_pil(self.base), self)
        self.body.addWidget(self.view, 1)
        self.hint = QLabel("Drag over anything you want hidden. It's burned into the pixels — it can't be undone in the copy.")
        self.hint.setObjectName("dim")
        self.body.addWidget(self.hint)
        row, self.mode = toggle_row(self.MODES)
        self.mode.idClicked.connect(lambda _: self.refresh())
        undo = QPushButton("Undo")
        undo.clicked.connect(self.undo)
        QShortcut(QKeySequence.Undo, self, activated=self.undo)
        row.addWidget(undo)
        self.body.addLayout(row)
        self.body.addLayout(self.footer("Save", self.save))

    def _mode(self):
        return ["black", "pixelate", "blur"][self.mode.checkedId()]

    def _boxes(self, k: float):
        return [tuple(round(v * k) for v in (r.left(), r.top(), r.right(), r.bottom())) for r in self.view.rects]

    def refresh(self):
        im = imaging.redact(self.base, self._boxes(1.0), self._mode())
        self.view.set_image(qimage_from_pil(im))
        n = len(self.view.rects)
        self.subtitle.setText(f"{self.path.name}  ·  {n} area{'s' if n != 1 else ''} hidden" if n else self.path.name)

    def undo(self):
        if self.view.rects:
            self.view.rects.pop()
            self.refresh()

    def save(self):
        boxes, mode, path = self._boxes(self.k), self._mode(), self.path
        if not boxes:
            self.close()
            return
        label = {"black": "black boxes", "pixelate": "pixelated", "blur": "blurred"}[mode]

        def work():
            return [imaging.save_like(imaging.redact(open_image(path), boxes, mode), path, "Redacted")]
        self.run(work, "Redacting…", f"{len(boxes)} area{'s' if len(boxes) != 1 else ''} {label}")


# ================================================================ EDIT
class EditWindow(ToolWindow):
    tool_name = "Edit"

    def __init__(self, path, notify):
        super().__init__("Edit", path, notify, size=(880, 780))
        self.small = open_preview(path, 900)
        with Image.open(path) as im:
            self.full_size = im.size if im.getexif().get(0x0112, 1) not in (5, 6, 7, 8) else im.size[::-1]
        self.adj = imaging.Adjust()
        self.comparing = False

        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(360, 300)
        self.body.addWidget(self.preview, 1)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.sl = {
            "brightness": slider(-100, 100, 0), "contrast": slider(-100, 100, 0),
            "saturation": slider(-100, 100, 0), "warmth": slider(-100, 100, 0),
            "sharpness": slider(-100, 100, 0),
        }
        for i, (k, s) in enumerate(self.sl.items()):
            slider_row(grid, i, k.capitalize(), s, lambda v: f"{v:+d}")
            s.valueChanged.connect(lambda _: self._timer.start())
        self.size_sl = slider(5, 100, 100)
        slider_row(grid, len(self.sl), "Size", self.size_sl, lambda v: f"{v}%")
        self.size_sl.valueChanged.connect(self._size_label)
        self.dim_label = QLabel()
        self.dim_label.setObjectName("dim")
        grid.addWidget(self.dim_label, len(self.sl) + 1, 1)
        self.body.addLayout(grid)

        row = QHBoxLayout()
        auto = QPushButton("✦ Auto")
        auto.setToolTip("Automatic light and colour")
        auto.clicked.connect(self.auto)
        row.addWidget(auto)
        for text, fn in (("⟲", lambda: self._rot(-90)), ("⟳", lambda: self._rot(90)),
                         ("⇋ Flip", lambda: self._flip("h")), ("⇵ Flip", lambda: self._flip("v"))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        self.gray = QPushButton("B&&W")
        self.gray.setCheckable(True)
        self.gray.toggled.connect(lambda _: self._timer.start())
        row.addWidget(self.gray)
        row.addStretch(1)
        cmp_btn = QPushButton("Hold to compare")
        cmp_btn.pressed.connect(lambda: self._compare(True))
        cmp_btn.released.connect(lambda: self._compare(False))
        row.addWidget(cmp_btn)
        self.body.addLayout(row)
        self.body.addLayout(self.footer("Save", self.save, self.reset))

        self._timer = QTimer(self, singleShot=True, interval=16, timeout=self.render)
        self._size_label()
        self.render()

    def _compare(self, on: bool):
        self.comparing = on
        self.render()

    def auto(self):
        a = imaging.auto_enhance(self.small)
        vals = {"brightness": a.brightness - 1, "contrast": a.contrast - 1, "saturation": a.saturation - 1,
                "sharpness": (a.sharpness - 1) / 2}
        for k, v in vals.items():
            self.sl[k].setValue(int(round(v * 100)))

    def _size_label(self):
        p = self.size_sl.value() / 100
        w, h = self.full_size
        if self.adj.rotate % 180:
            w, h = h, w
        self.dim_label.setText(f"{round(w * p)} × {round(h * p)} px")

    def _rot(self, d):
        self.adj.rotate = (self.adj.rotate + d) % 360
        self._size_label()
        self.render()

    def _flip(self, axis):
        if axis == "h":
            self.adj.flip_h = not self.adj.flip_h
        else:
            self.adj.flip_v = not self.adj.flip_v
        self.render()

    def _collect(self, resize: bool) -> imaging.Adjust:
        f = lambda k: 1 + self.sl[k].value() / 100
        a = self.adj
        a.brightness, a.contrast, a.saturation = f("brightness"), f("contrast"), f("saturation")
        a.sharpness = 1 + self.sl["sharpness"].value() / 50
        a.warmth = self.sl["warmth"].value() / 100
        a.grayscale = self.gray.isChecked()
        a.resize_pct = self.size_sl.value() if resize else 100
        return a

    def render(self):
        out = self.small if self.comparing else imaging.apply_adjust(self.small, self._collect(False))
        pm = QPixmap.fromImage(qimage_from_pil(out))
        self.preview.setPixmap(pm.scaled(self.preview.size() - QSize(10, 10), Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "_timer"):
            self._timer.start()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self.render)

    def reset(self):
        for s in self.sl.values():
            s.setValue(0)
        self.size_sl.setValue(100)
        self.gray.setChecked(False)
        self.adj = imaging.Adjust()
        self._size_label()
        self.render()

    def _summary(self) -> str:
        bits = [f"{k} {s.value():+d}" for k, s in self.sl.items() if s.value()]
        if self.adj.rotate:
            bits.append(f"rotated {self.adj.rotate}°")
        if self.adj.flip_h or self.adj.flip_v:
            bits.append("flipped")
        if self.gray.isChecked():
            bits.append("B&W")
        return ", ".join(bits[:4])

    def save(self):
        adj = imaging.Adjust(**vars(self._collect(True)))
        path = self.path

        def work():
            return [imaging.save_like(imaging.apply_adjust(open_image(path), adj), path, "Edited")]
        self.run(work, "Applying edits…", self._summary())


# ================================================================ METADATA
class MetadataWindow(ToolWindow):
    tool_name = "Metadata"

    def __init__(self, path, notify, is_pdf: bool = False):
        super().__init__("Metadata", path, notify, size=(640, 380))
        self.is_pdf = is_pdf
        info = imaging.read_pdf_metadata(path) if is_pdf else imaging.read_image_metadata(path)
        summary = {} if is_pdf else imaging.metadata_summary(path)
        self.n_fields, self.has_gps = (len(info), False) if is_pdf else imaging.count_metadata(path)

        card = QFrame()
        card.setObjectName("card")
        cl = QGridLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setHorizontalSpacing(16)
        rows = summary or {k: v for k, v in info.items() if k in ("title", "author", "creator", "producer", "Pages")}
        if not rows:
            rows = {"": "No personal metadata found."}
        for i, (k, v) in enumerate(rows.items()):
            kl = QLabel(k)
            kl.setObjectName("dim")
            cl.addWidget(kl, i, 0)
            vl = QLabel(v)
            vl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            cl.addWidget(vl, i, 1)
            if k == "Location":
                vl.setObjectName("warn")
                mb = QPushButton("Open map")
                lat, lon = v.split(", ")
                mb.clicked.connect(lambda _=False, a=lat, b=lon: QDesktopServices.openUrl(
                    QUrl(f"https://www.openstreetmap.org/?mlat={a}&mlon={b}#map=15/{a}/{b}")))
                cl.addWidget(mb, i, 2)
        self.body.addWidget(card)
        if self.has_gps:
            warn = QLabel("⚠  This photo reveals where it was taken.")
            warn.setObjectName("warn")
            self.body.addWidget(warn)

        self.toggle = QPushButton(f"Show all {self.n_fields} fields")
        self.toggle.setCheckable(True)
        self.body.addWidget(self.toggle, 0, Qt.AlignLeft)
        t = QTableWidget(len(info), 2)
        t.setHorizontalHeaderLabels(["Field", "Value"])
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        t.horizontalHeader().setStretchLastSection(True)
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        for r, (k, v) in enumerate(info.items()):
            t.setItem(r, 0, QTableWidgetItem(k))
            t.setItem(r, 1, QTableWidgetItem(v))
        t.setVisible(False)

        def show_all(on):
            t.setVisible(on)
            self.resize(self.width(), 640 if on else 380)
        self.toggle.toggled.connect(show_all)
        self.body.addWidget(t, 1)
        self.body.addStretch(0)

        extra = []
        if self.has_gps:
            loc = QPushButton("Remove location only")
            loc.clicked.connect(self.strip_location)
            extra.append(loc)
        self.body.addLayout(self.footer("Remove all metadata", self.strip, None, extra))

    def strip(self):
        path, is_pdf = self.path, self.is_pdf
        detail = f"removed {self.n_fields} field{'s' if self.n_fields != 1 else ''}" + (" incl. GPS location" if self.has_gps else "")

        def work():
            return [imaging.strip_pdf_metadata(path) if is_pdf else imaging.strip_image_metadata(path)]
        self.run(work, "Removing metadata…", detail)

    def strip_location(self):
        path = self.path
        self.run(lambda: [imaging.strip_location(path)], "Removing location…", "GPS removed, camera info kept")


# ================================================================ COMPRESS
class CompressWindow(ToolWindow):
    tool_name = "Compress"
    SIDES = [("Original size", None), ("Max 3840 px", 3840), ("Max 2560 px", 2560),
             ("Max 1920 px", 1920), ("Max 1280 px", 1280), ("Max 800 px", 800)]
    TARGETS = [("2 MB", 2_000_000), ("1 MB", 1_000_000), ("500 KB", 500_000), ("200 KB", 200_000), ("100 KB", 100_000)]

    def __init__(self, paths, notify):
        super().__init__("Compress", paths, notify, size=(640, 580))
        self.orig_total = sum(file_size(p) for p in self.paths)
        self.pv = open_preview(self.path, 1600)
        with Image.open(self.path) as im:
            self.full_px = im.width * im.height
            self.full_long = max(im.size)

        row, self.mode = toggle_row(["Quality", "Target size"])
        self.body.addLayout(row)

        self.q_box = QWidget()
        g = QGridLayout(self.q_box)
        g.setContentsMargins(0, 0, 0, 0)
        self.q = slider(10, 95, 72)
        slider_row(g, 0, "Quality", self.q, lambda v: f"{v}")
        self.body.addWidget(self.q_box)

        self.t_box = QWidget()
        tl = QHBoxLayout(self.t_box)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(QLabel("Make each file under"))
        self.tgrp = QButtonGroup(self)
        for i, (lbl, _) in enumerate(self.TARGETS):
            b = QPushButton(lbl)
            b.setCheckable(True)
            b.setChecked(i == 2)
            self.tgrp.addButton(b, i)
            tl.addWidget(b)
        tl.addStretch(1)
        self.t_box.setVisible(False)
        self.body.addWidget(self.t_box)

        g2 = QGridLayout()
        g2.addWidget(QLabel("Size"), 0, 0)
        self.side = QComboBox()
        for name, _ in self.SIDES:
            self.side.addItem(name)
        g2.addWidget(self.side, 0, 1)
        self.webp = QCheckBox("Convert to WebP (usually the smallest)")
        g2.addWidget(self.webp, 1, 1)
        self.body.addLayout(g2)

        # before / after at 100%
        pr = QHBoxLayout()
        self.before = QLabel()
        self.after = QLabel()
        for lab, cap in ((self.before, "Original"), (self.after, "Compressed")):
            col = QVBoxLayout()
            lab.setFixedSize(270, 170)
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet("border-radius: 12px; background: rgba(0,0,0,0.25);")
            c = QLabel(cap)
            c.setObjectName("dim")
            col.addWidget(lab)
            col.addWidget(c, 0, Qt.AlignHCenter)
            pr.addLayout(col)
        self.body.addLayout(pr)

        self.est = QLabel()
        self.est.setObjectName("big")
        self.saving = QLabel()
        self.saving.setObjectName("good")
        self.body.addWidget(self.est)
        self.body.addWidget(self.saving)
        self.body.addStretch(1)
        label = "Compress" if len(self.paths) == 1 else f"Compress {len(self.paths)} files"
        self.body.addLayout(self.footer(label, self.save))

        self._timer = QTimer(self, singleShot=True, interval=60, timeout=self.estimate)
        self.q.valueChanged.connect(lambda _: self._timer.start())
        self.side.currentIndexChanged.connect(lambda _: self._timer.start())
        self.webp.toggled.connect(lambda _: self._timer.start())
        self.tgrp.idClicked.connect(lambda _: self._timer.start())
        self.mode.idClicked.connect(self._mode)
        self._set_before()
        self.estimate()

    def _mode(self, i):
        self.q_box.setVisible(i == 0)
        self.t_box.setVisible(i == 1)
        self._timer.start()

    def _detail_box(self, im: Image.Image) -> tuple[int, int, int, int]:
        """The 270×170 window with the most detail (where compression artefacts show)."""
        from PIL import ImageFilter, ImageStat
        w, h = 270, 170
        if im.width <= w or im.height <= h:
            return (0, 0, min(w, im.width), min(h, im.height))
        edges = im.convert("L").filter(ImageFilter.FIND_EDGES)
        best, box = -1.0, (0, 0, w, h)
        for gx in range(5):
            for gy in range(5):
                x = int((im.width - w) * gx / 4)
                y = int((im.height - h) * gy / 4)
                v = ImageStat.Stat(edges.crop((x, y, x + w, y + h))).mean[0]
                if v > best:
                    best, box = v, (x, y, x + w, y + h)
        return box

    def _crop_100(self, im: Image.Image) -> QPixmap:
        if not hasattr(self, "_box"):
            self._box = self._detail_box(self.pv)
        return QPixmap.fromImage(qimage_from_pil(im.crop(self._box)))

    def _set_before(self):
        self.before.setPixmap(self._crop_100(self.pv))

    def _settings(self):
        side = self.SIDES[self.side.currentIndex()][1]
        target = imaging.lossy_target(self.path, self.webp.isChecked())
        return side, target

    def estimate(self):
        side, target = self._settings()
        im = self.pv
        # scale factor from preview pixels to output pixels
        out_long = min(self.full_long, side) if side else self.full_long
        px_ratio = (out_long / max(im.size)) ** 2
        if self.mode.checkedId() == 1:
            limit = self.TARGETS[self.tgrp.checkedId()][1]
            q = self._quality_for(im, target, limit, px_ratio)
            note = f"quality ≈ {q}"
        else:
            q = self.q.value()
            note = ""
        n = int(imaging.encode_size(im, target, q) * px_ratio)
        buf = io.BytesIO()
        if target == "PNG":
            im.convert("RGBA").quantize(colors=max(16, min(256, int(q * 2.56)))).save(buf, "PNG")
        else:
            from .convert import save_image
            save_image(im, buf, target, quality=q)
        buf.seek(0)
        try:
            self.after.setPixmap(self._crop_100(Image.open(buf).convert("RGB")))
        except Exception:
            pass
        orig1 = file_size(self.path)
        if len(self.paths) > 1:
            total = int(n * self.orig_total / max(1, orig1))
            self.est.setText(f"{human_size(self.orig_total)}  →  ≈ {human_size(total)}")
            n_cmp, o_cmp = total, self.orig_total
        else:
            self.est.setText(f"{human_size(orig1)}  →  ≈ {human_size(n)}")
            n_cmp, o_cmp = n, orig1
        pct = (1 - n_cmp / max(1, o_cmp)) * 100
        fmt = target if target != "JPG" else "JPG"
        if pct >= 1:
            self.saving.setObjectName("good")
            self.saving.setText(f"≈ {pct:.0f}% smaller · {fmt}" + (f" · {note}" if note else ""))
        else:
            self.saving.setObjectName("warn")
            self.saving.setText("Already well compressed — try a smaller size or WebP")
        self.saving.style().unpolish(self.saving)
        self.saving.style().polish(self.saving)

    @staticmethod
    def _quality_for(im, target, limit, px_ratio) -> int:
        lo, hi, best = 30, 92, 30
        while lo <= hi:
            q = (lo + hi) // 2
            if imaging.encode_size(im, target, q) * px_ratio <= limit:
                best, lo = q, q + 1
            else:
                hi = q - 1
        return best

    def save(self):
        side, target = self._settings()
        paths = list(self.paths)
        if self.mode.checkedId() == 1:
            limit = self.TARGETS[self.tgrp.checkedId()][1]
            label = self.TARGETS[self.tgrp.checkedId()][0]

            def work():
                outs = []
                for p in paths:
                    im = open_image(p)
                    if side and max(im.size) > side:
                        im.thumbnail((side, side), Image.LANCZOS)
                    q, s2 = imaging.plan_target_size(im, target, limit)
                    outs.append(imaging.compress_to(p, q, min(x for x in (side, s2) if x) if (side or s2) else None, target))
                return outs
            self.run(work, "Compressing…", f"target {label}")
        else:
            q = self.q.value()
            self.run(lambda: [imaging.compress_to(p, q, side, target) for p in paths], "Compressing…", f"quality {q}")


# ================================================================ RESIZE
class ResizeWindow(ToolWindow):
    tool_name = "Resize"
    PRESETS = [("25%", "pct", 25), ("50%", "pct", 50), ("75%", "pct", 75), ("3840", "long", 3840),
               ("2560", "long", 2560), ("1920", "long", 1920), ("1280", "long", 1280), ("800", "long", 800)]

    def __init__(self, paths, notify):
        super().__init__("Resize", paths, notify, size=(600, 380))
        with Image.open(self.path) as im:
            w, h = im.size
            if im.getexif().get(0x0112, 1) in (5, 6, 7, 8):
                w, h = h, w
        self.orig = (w, h)
        self.body.addWidget(section("Presets — percent or longest side in px"))
        row, self.grp = toggle_row([p[0] for p in self.PRESETS], 1)
        self.grp.idClicked.connect(self._preset)
        self.body.addLayout(row)
        self.body.addWidget(section("Exact size"))
        er = QHBoxLayout()
        self.w = QSpinBox()
        self.h = QSpinBox()
        for s in (self.w, self.h):
            s.setRange(1, 30000)
            s.setFixedWidth(100)
        self.lock = QCheckBox("Keep proportions")
        self.lock.setChecked(True)
        er.addWidget(QLabel("W"))
        er.addWidget(self.w)
        er.addWidget(QLabel("H"))
        er.addWidget(self.h)
        er.addWidget(QLabel("px"))
        er.addSpacing(10)
        er.addWidget(self.lock)
        er.addStretch(1)
        self.body.addLayout(er)
        self.out = QLabel()
        self.out.setObjectName("big")
        self.body.addWidget(self.out)
        self.body.addStretch(1)
        label = "Resize" if len(self.paths) == 1 else f"Resize {len(self.paths)} files"
        self.body.addLayout(self.footer(label, self.save))
        self.w.valueChanged.connect(lambda v: self._typed("w", v))
        self.h.valueChanged.connect(lambda v: self._typed("h", v))
        self._preset(1)

    def _set(self, w, h):
        for box, v in ((self.w, w), (self.h, h)):
            box.blockSignals(True)
            box.setValue(v)
            box.blockSignals(False)
        self.out.setText(f"{dims_text(*self.orig)}  →  {dims_text(w, h)}")

    def _preset(self, i):
        _, mode, val = self.PRESETS[i]
        self.mode = (mode, val)
        self._set(*imaging.resize_dims(self.orig, mode, val))

    def _typed(self, which, v):
        b = self.grp.checkedButton()
        if b:
            self.grp.setExclusive(False)
            b.setChecked(False)
            self.grp.setExclusive(True)
        if self.lock.isChecked():
            w, h = imaging.resize_dims(self.orig, which, v)
        else:
            w, h = self.w.value(), self.h.value()
        self.mode = (which, v) if self.lock.isChecked() else ("exact", (w, h))
        self._set(w, h)

    def save(self):
        mode, val = self.mode
        paths = list(self.paths)

        def dims_for(p):
            with Image.open(p) as im:
                size = im.size if im.getexif().get(0x0112, 1) not in (5, 6, 7, 8) else im.size[::-1]
            if mode == "exact":
                return val
            return imaging.resize_dims(size, mode, val)
        self.run(lambda: [imaging.resize_image(p, dims_for(p)) for p in paths], "Resizing…")


# ================================================================ PROTECT (PDF)
class ProtectWindow(ToolWindow):
    tool_name = "Protect"

    def __init__(self, path, notify):
        super().__init__("Password-protect PDF", path, notify, size=(520, 330))
        g = QGridLayout()
        self.pw = QLineEdit()
        self.pw2 = QLineEdit()
        for e in (self.pw, self.pw2):
            e.setEchoMode(QLineEdit.Password)
        g.addWidget(QLabel("Password"), 0, 0)
        g.addWidget(self.pw, 0, 1)
        g.addWidget(QLabel("Confirm"), 1, 0)
        g.addWidget(self.pw2, 1, 1)
        show = QCheckBox("Show")
        show.toggled.connect(lambda on: [e.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
                                         for e in (self.pw, self.pw2)])
        g.addWidget(show, 0, 2)
        self.body.addLayout(g)
        self.msg = QLabel("AES-256 encryption. Anyone opening the PDF will need this password.")
        self.msg.setObjectName("dim")
        self.msg.setWordWrap(True)
        self.body.addWidget(self.msg)
        self.body.addStretch(1)
        self.body.addLayout(self.footer("Protect", self.save))
        self.pw.textChanged.connect(self._check)
        self.pw2.textChanged.connect(self._check)
        self._check()
        QTimer.singleShot(0, self.pw.setFocus)

    def _check(self):
        a, b = self.pw.text(), self.pw2.text()
        ok = len(a) >= 4 and a == b
        self.primary_btn.setEnabled(ok)
        if a and len(a) < 4:
            self.msg.setText("Use at least 4 characters.")
        elif b and a != b:
            self.msg.setText("Passwords don't match yet.")
        elif ok:
            strength = "strong" if len(a) >= 12 else "okay" if len(a) >= 8 else "weak"
            self.msg.setText(f"Looks good · {strength} password")
        else:
            self.msg.setText("AES-256 encryption. Anyone opening the PDF will need this password.")

    def save(self):
        pw, path = self.pw.text(), self.path
        self.run(lambda: [imaging.protect_pdf(path, pw)], "Encrypting…")


# ================================================================ RENAME
class RenameWindow(ToolWindow):
    tool_name = "Rename"

    def __init__(self, paths, notify):
        super().__init__("Rename", paths, notify, size=(580, 560))
        many = len(self.paths) > 1
        self.body.addWidget(section("Name pattern"))
        saved = remember.load("rename", {"pattern": ""})["pattern"]
        self.pattern = QLineEdit(saved or ("{name}_{n}" if many else "{name}"))
        self.body.addWidget(self.pattern)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        for tok in ("name", "n", "date", "mdate", "parent"):
            b = QPushButton("{" + tok + "}")
            b.clicked.connect(lambda _=False, t=tok: self._insert(t))
            chips.addWidget(b)
        chips.addStretch(1)
        self.body.addLayout(chips)
        hint = QLabel("{name} original name · {n} counter · {date} today · {mdate} file's modified date "
                      "· {parent} folder. The extension is kept.")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        self.body.addWidget(hint)

        g = QGridLayout()
        self.start = QSpinBox()
        self.start.setRange(0, 99999)
        self.start.setValue(1)
        self.digits = QSpinBox()
        self.digits.setRange(1, 8)
        self.digits.setValue(max(1, len(str(len(self.paths)))))
        g.addWidget(QLabel("Start counting at"), 0, 0)
        g.addWidget(self.start, 0, 1)
        g.addWidget(QLabel("Digits (zero-padded)"), 1, 0)
        g.addWidget(self.digits, 1, 1)
        g.setColumnStretch(2, 1)
        self.body.addLayout(g)

        self.body.addWidget(section("Preview"))
        self.preview = QLabel()
        self.preview.setTextFormat(Qt.PlainText)
        self.preview.setWordWrap(True)
        self.preview.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.body.addWidget(self.preview, 1)
        note = QLabel("Renames the files in place. Undo in the notification puts the old names back.")
        note.setObjectName("dim")
        note.setWordWrap(True)
        self.body.addWidget(note)
        self.body.addLayout(self.footer("Rename", self.save))

        for w in (self.pattern,):
            w.textChanged.connect(self._refresh)
        self.start.valueChanged.connect(self._refresh)
        self.digits.valueChanged.connect(self._refresh)
        self._pairs = []
        self._refresh()
        QTimer.singleShot(0, self.pattern.setFocus)

    def _insert(self, tok: str):
        self.pattern.insert("{" + tok + "}")
        self.pattern.setFocus()

    def _refresh(self):
        try:
            self._pairs = rename.plan(self.paths, self.pattern.text(), self.start.value(), self.digits.value())
        except UserError as e:
            self._pairs = []
            self.preview.setText(str(e))
            self.primary_btn.setEnabled(False)
            return
        except OSError as e:     # a file vanished while the window was open
            self._pairs = []
            self.preview.setText(f"Can't read a file: {e}")
            self.primary_btn.setEnabled(False)
            return
        shown = [f"{s.name}  →  {d.name}" for s, d in self._pairs[:10]]
        if len(self._pairs) > 10:
            shown.append(f"… and {len(self._pairs) - 10} more")
        changed = sum(1 for s, d in self._pairs if s.name != d.name)
        if not changed:
            shown.append("\nNothing would change yet.")
        self.preview.setText("\n".join(shown))
        self.primary_btn.setEnabled(bool(changed))

    def save(self):
        pairs, order = self._pairs, list(self.paths)
        if not pairs:
            return
        remember.save("rename", {"pattern": self.pattern.text()})

        def job():
            new = dict(zip([s for s, _ in pairs], rename.apply(pairs)))
            return [new[p] for p in order]     # same order as the sources, so Undo lines up

        self.run(job, "Renaming…")


# ================================================================ WATERMARK
class WatermarkWindow(ToolWindow):
    tool_name = "Watermark"
    SWATCHES = ["#ffffff", "#111111", "#e11d48", "#2563eb", "#f59e0b"]
    POS_GLYPHS = ["↖", "↑", "↗", "←", "●", "→", "↙", "↓", "↘"]
    TILE = 9
    DEFAULTS = {"mode": 0, "text": "Confidential", "logo": "", "color": "#ffffff", "pos": 8, "size": 30,
                "opacity": 50, "angle": 0, "margin": 3, "gap": 100, "shadow": True}

    def __init__(self, paths, notify):
        super().__init__("Watermark", paths, notify, size=(900, 860))
        self.small = self._preview_source(self.paths[0])
        self.color, self.logo = "#ffffff", ""

        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(360, 260)
        self.body.addWidget(self.preview, 1)
        self.msg = QLabel()
        self.msg.setObjectName("dim")
        self.msg.setAlignment(Qt.AlignCenter)
        self.body.addWidget(self.msg)

        row, self.mode = toggle_row(["Text", "Logo"])
        self.body.addLayout(row)

        self.text_box = QWidget()
        tl = QVBoxLayout(self.text_box)
        tl.setContentsMargins(0, 0, 0, 0)
        self.text = QLineEdit("Confidential")
        self.text.setPlaceholderText("Watermark text  (type \\n for a second line)")
        tl.addWidget(self.text)
        sw = QHBoxLayout()
        sw.setSpacing(6)
        self.sw_grp = QButtonGroup(self)
        for i, c in enumerate(self.SWATCHES):
            b = QPushButton()
            b.setObjectName("swatch")
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setStyleSheet(f"background: {c};")
            b.clicked.connect(lambda _=False, col=c: self._set_color(col))
            self.sw_grp.addButton(b, i)
            sw.addWidget(b)
        custom = QPushButton("＋")
        custom.setObjectName("swatch")
        custom.setToolTip("Custom colour")
        custom.clicked.connect(self._custom_color)
        sw.addWidget(custom)
        self.shadow = QCheckBox("Soft shadow")
        self.shadow.setChecked(True)
        sw.addSpacing(12)
        sw.addWidget(self.shadow)
        sw.addStretch(1)
        tl.addLayout(sw)
        self.body.addWidget(self.text_box)

        self.logo_box = QWidget()
        ll = QHBoxLayout(self.logo_box)
        ll.setContentsMargins(0, 0, 0, 0)
        pick = QPushButton("Choose logo…")
        pick.clicked.connect(self._pick_logo)
        self.logo_name = QLabel("No logo chosen (a PNG with a transparent background works best)")
        self.logo_name.setObjectName("dim")
        ll.addWidget(pick)
        ll.addWidget(self.logo_name, 1)
        self.body.addWidget(self.logo_box)

        mid = QHBoxLayout()
        mid.setSpacing(24)
        col = QVBoxLayout()
        col.addWidget(section("Position"))
        pg = QGridLayout()
        pg.setSpacing(4)
        self.pos_grp = QButtonGroup(self)
        for i, g in enumerate(self.POS_GLYPHS):
            b = QPushButton(g)
            b.setCheckable(True)
            b.setChecked(i == 8)
            b.setFixedSize(40, 34)
            self.pos_grp.addButton(b, i)
            pg.addWidget(b, i // 3, i % 3)
        tile = QPushButton("Tile across page")
        tile.setCheckable(True)
        self.pos_grp.addButton(tile, self.TILE)
        pg.addWidget(tile, 3, 0, 1, 3)
        col.addLayout(pg)
        col.addStretch(1)
        mid.addLayout(col)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.siz = slider(5, 100, 30)
        self.opa = slider(5, 100, 50)
        self.ang = slider(-90, 90, 0)
        self.mar = slider(0, 15, 3)
        self.gap = slider(0, 300, 100)
        slider_row(grid, 0, "Size", self.siz, lambda v: f"{v}%")
        slider_row(grid, 1, "Opacity", self.opa, lambda v: f"{v}%")
        slider_row(grid, 2, "Angle", self.ang, lambda v: f"{v}°")
        slider_row(grid, 3, "Margin", self.mar, lambda v: f"{v}%")
        slider_row(grid, 4, "Tile spacing", self.gap, lambda v: f"{v}%")
        mid.addLayout(grid, 1)
        self.body.addLayout(mid)

        self.body.addLayout(self.footer("Add Watermark", self.save, self.reset))
        self._restore(remember.load("watermark", self.DEFAULTS))

        self._timer = QTimer(self, singleShot=True, interval=16, timeout=self.render)
        for s in (self.siz, self.opa, self.ang, self.mar, self.gap):
            s.valueChanged.connect(lambda _: self._timer.start())
        self.text.textChanged.connect(lambda _: self._timer.start())
        self.shadow.toggled.connect(lambda _: self._timer.start())
        self.mode.idClicked.connect(self._mode_changed)
        self.pos_grp.idClicked.connect(self._position_changed)
        self._mode_changed()
        QTimer.singleShot(0, self.text.setFocus)

    # ---------------------------------------------------------- inputs
    @staticmethod
    def _preview_source(path: Path) -> Image.Image:
        if path.suffix.lower() != ".pdf":
            return open_preview(path, 760).convert("RGB")
        with open_pdf(path) as doc:
            page = doc[0]
            z = 760 / max(page.rect.width, page.rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    def _restore(self, v: dict):
        """Put back what was used last time (before any signal is connected, so nothing re-renders)."""
        self.text.setText(v["text"])
        self.siz.setValue(v["size"])
        self.opa.setValue(v["opacity"])
        self.ang.setValue(v["angle"])
        self.mar.setValue(v["margin"])
        self.gap.setValue(v["gap"])
        self.shadow.setChecked(v["shadow"])
        self.pos_grp.button(v["pos"] if 0 <= v["pos"] <= self.TILE else 8).setChecked(True)
        color = v["color"] if len(v["color"]) == 7 and v["color"].startswith("#") and QColor(v["color"]).isValid()             else self.DEFAULTS["color"]
        self.color = color
        if color in self.SWATCHES:
            self.sw_grp.button(self.SWATCHES.index(color)).setChecked(True)
        else:                                        # a custom colour: no preset swatch is lit
            self.sw_grp.setExclusive(False)
            for b in self.sw_grp.buttons():
                b.setChecked(False)
            self.sw_grp.setExclusive(True)
        if v["logo"] and Path(v["logo"]).is_file():  # the logo may have been moved or deleted since
            self.logo = v["logo"]
            self.logo_name.setText(Path(v["logo"]).name)
        self.mode.button(1 if (v["mode"] == 1 and self.logo) else 0).setChecked(True)

    def _collect(self) -> dict:
        return {"mode": self.mode.checkedId(), "text": self.text.text(), "logo": self.logo, "color": self.color,
                "pos": self.pos_grp.checkedId(), "size": self.siz.value(), "opacity": self.opa.value(),
                "angle": self.ang.value(), "margin": self.mar.value(), "gap": self.gap.value(),
                "shadow": self.shadow.isChecked()}

    def _scope_note(self) -> str:
        what = "every page" if self.paths[0].suffix.lower() == ".pdf" else "each image"
        if len(self.paths) > 1:
            return f"Applies to {len(self.paths)} files ({what}) · the preview shows the first"
        return f"Applies to {what}"

    def _set_color(self, c: str):
        self.color = c
        self._timer.start()

    def _custom_color(self):
        c = QColorDialog.getColor(QColor(self.color), self, "Text colour")
        if c.isValid():
            self.sw_grp.setExclusive(False)
            for b in self.sw_grp.buttons():
                b.setChecked(False)
            self.sw_grp.setExclusive(True)
            self._set_color(c.name())

    def _pick_logo(self):
        f, _ = QFileDialog.getOpenFileName(self, "Choose a logo", "",
                                           "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff)")
        if f:
            self.logo = f
            self.logo_name.setText(Path(f).name)
            self._timer.start()

    def _mode_changed(self, *_):
        logo = self.mode.checkedId() == 1
        self.text_box.setVisible(not logo)
        self.logo_box.setVisible(logo)
        self._timer.start()

    def _position_changed(self, i: int):
        if i == self.TILE and self.ang.value() == 0:
            self.ang.setValue(-30)                 # diagonal tiles are the usual look
        elif i != self.TILE and self.ang.value() == -30:
            self.ang.setValue(0)
        self._timer.start()

    def _mark(self) -> watermark.Mark:
        logo = self.mode.checkedId() == 1
        pos = self.pos_grp.checkedId()
        return watermark.Mark(
            text="" if logo else self.text.text().replace("\\n", "\n"), logo=self.logo if logo else "",
            position="tile" if pos == self.TILE else watermark.POSITIONS[pos if pos >= 0 else 8],
            opacity=self.opa.value() / 100, size=self.siz.value() / 100, angle=self.ang.value(),
            margin=self.mar.value() / 100, color=self.color, shadow=self.shadow.isChecked(),
            gap=self.gap.value() / 100)

    # ---------------------------------------------------------- preview
    def render(self):
        try:
            out = watermark.apply(self.small, self._mark())
            ok, note = True, self._scope_note()
        except UserError as e:
            out, ok, note = self.small, False, str(e)
        self.msg.setText(note)
        self.primary_btn.setEnabled(ok)
        pm = QPixmap.fromImage(qimage_from_pil(out))
        self.preview.setPixmap(pm.scaled(self.preview.size() - QSize(10, 10), Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "_timer"):
            self._timer.start()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self.render)

    def reset(self):
        self.siz.setValue(30); self.opa.setValue(50); self.ang.setValue(0); self.mar.setValue(3); self.gap.setValue(100)
        self.pos_grp.button(8).setChecked(True)
        self.sw_grp.button(0).setChecked(True)
        self.shadow.setChecked(True)
        self._set_color(self.SWATCHES[0])

    def save(self):
        mark, paths = self._mark(), [str(p) for p in self.paths]
        remember.save("watermark", self._collect())

        def work():
            return [watermark.watermark_pdf(p, mark) if p.lower().endswith(".pdf") else watermark.watermark_image(p, mark)
                    for p in paths]
        self.run(work, "Adding watermark…")


# ================================================================ UPSCALE
class UpscaleWindow(ToolWindow):
    tool_name = "Upscale"
    CHOICES = [(1, "Enhance · same size"), (2, "2×"), (4, "4×")]
    CROP = 128                 # the before/after preview works on this much of the picture's centre
    SHOW = 300                 # …shown this big

    def __init__(self, paths, notify):
        super().__init__("Upscale", paths, notify, size=(780, 640))
        self.src = open_image(self.paths[0]).convert("RGB")
        self.sizes = [self._size(p) for p in self.paths]

        self.body.addWidget(section("Result"))
        last = remember.load("upscale", {"scale": 2})["scale"]
        row, self.choice = toggle_row([label for _, label in self.CHOICES],
                                      checked=next((i for i, (s, _) in enumerate(self.CHOICES) if s == last), 1))
        self.body.addLayout(row)
        self.info = QLabel()
        self.info.setObjectName("dim")
        self.info.setWordWrap(True)
        self.body.addWidget(self.info)

        self.body.addWidget(section("Preview · centre of the picture"))
        pv = QHBoxLayout()
        pv.setSpacing(16)
        self.before, self.after = QLabel(), QLabel()
        for lab, title in ((self.before, "Plain enlargement"), (self.after, "DragShift")):
            col = QVBoxLayout()
            lab.setFixedSize(self.SHOW, self.SHOW)
            lab.setAlignment(Qt.AlignCenter)
            cap = QLabel(title)
            cap.setObjectName("dim")
            cap.setAlignment(Qt.AlignCenter)
            col.addWidget(lab)
            col.addWidget(cap)
            pv.addLayout(col)
        pv.addStretch(1)
        self.body.addLayout(pv)
        warn = QLabel("An AI model redraws the picture: it removes noise and blur and sharpens edges, but it "
                      "invents detail. Very small text or faces can come out altered, so compare with the original.")
        warn.setObjectName("dim")
        warn.setWordWrap(True)
        self.body.addWidget(warn)
        self.body.addStretch(1)
        self.body.addLayout(self.footer("Upscale", self.save))

        # scales whose result would be too big for any of the files can't be chosen
        for i, (scale, _) in enumerate(self.CHOICES):
            ok = all(self._fits(s, scale) for s in self.sizes)
            self.choice.button(i).setEnabled(ok)
            if not ok:
                self.choice.button(i).setToolTip("The result would be too large")
        if not self.choice.button(self.choice.checkedId()).isEnabled():      # a remembered scale may not fit this file
            fallback = next((i for i in (1, 0, 2) if self.choice.button(i).isEnabled()), None)
            if fallback is not None:
                self.choice.button(fallback).setChecked(True)
        self.choice.idClicked.connect(lambda _: self._changed())
        self._changed(preview=False)                                  # text now, the (slower) preview once shown
        QTimer.singleShot(0, lambda: self._preview(self._scale()))

    @staticmethod
    def _size(path: Path) -> tuple[int, int]:
        with Image.open(path) as im:
            w, h = im.size
            return (h, w) if im.getexif().get(0x0112, 1) in (5, 6, 7, 8) else (w, h)

    @staticmethod
    def _fits(size, scale) -> bool:
        try:
            upscale.check_size(size, scale)
            return True
        except UserError:
            return False

    def _scale(self) -> int:
        return self.CHOICES[max(0, self.choice.checkedId())][0]

    def _changed(self, preview: bool = True):
        scale = self._scale()
        w, h = self.sizes[0]
        W, H = upscale.output_size((w, h), scale)
        total = sum(a * b for a, b in self.sizes)
        eta = max(1, round(total / 150_000))                      # about 150 thousand input pixels a second
        many = f" · {len(self.paths)} files" if len(self.paths) > 1 else ""
        self.info.setText((f"{w}×{h} → {W}×{H} px" if scale != 1 else f"{w}×{h} px, cleaned up")
                          + f"  ·  roughly {eta} s{many}")
        self.primary_btn.setText("Enhance" if scale == 1 else f"Upscale {scale}×")
        if preview:
            self._preview(scale)

    def _preview(self, scale: int):
        side = min(self.CROP, *self.src.size)
        x, y = (self.src.width - side) // 2, (self.src.height - side) // 2
        crop = self.src.crop((x, y, x + side, y + side))
        try:
            better = upscale.upscale_rgb(crop, 4)
        except UserError as e:
            self.after.setText(str(e))
            return
        plain = crop.resize(better.size, Image.BICUBIC)
        for lab, im in ((self.before, plain), (self.after, better)):
            pm = QPixmap.fromImage(qimage_from_pil(im.resize((self.SHOW, self.SHOW), Image.LANCZOS)))
            lab.setPixmap(pm)

    def save(self):
        scale, paths = self._scale(), [str(p) for p in self.paths]
        remember.save("upscale", {"scale": scale})

        def work():
            return [o for p in paths for o in upscale.upscale_file(p, scale)]
        self.run(work, "Upscaling… (this can take a while)" if scale != 1 else "Enhancing…")


# ================================================================ PRESETS EDITOR
class PresetsWindow(GlassWindow):
    """Add, change and delete presets. Every change is saved as you make it."""

    def __init__(self, notify=None):
        super().__init__("Presets", size=(620, 620))
        self.presets = presets.load()
        self._loading = False

        top = QHBoxLayout()
        self.pick = QComboBox()
        top.addWidget(self.pick, 1)
        self.new_btn = QPushButton("New")
        self.del_btn = QPushButton("Delete")
        self.defaults_btn = QPushButton("Restore examples")
        self.defaults_btn.setToolTip("Add the built-in Web, Email and Thumb presets back")
        for b in (self.new_btn, self.del_btn, self.defaults_btn):
            top.addWidget(b)
        self.body.addLayout(top)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        self.name = QLineEdit()
        self.name.setMaxLength(presets.NAME_MAX)
        self.name.setPlaceholderText(f"Up to {presets.NAME_MAX} characters, shown on the menu")
        grid.addWidget(QLabel("Name"), 0, 0)
        grid.addWidget(self.name, 0, 1, 1, 2)

        self.side = QSpinBox()
        self.side.setRange(*presets.LIMITS["max_side"])
        self.side.setSingleStep(100)
        self.side.setSuffix(" px")
        self.side.setSpecialValueText("Keep size")
        grid.addWidget(QLabel("Longest side"), 1, 0)
        grid.addWidget(self.side, 1, 1)
        hint = QLabel("Only shrinks, never enlarges")
        hint.setObjectName("dim")
        grid.addWidget(hint, 1, 2)

        self.fmt = QComboBox()
        for f in presets.FORMATS:
            self.fmt.addItem(presets.FORMAT_LABELS[f], f)
        grid.addWidget(QLabel("Format"), 2, 0)
        grid.addWidget(self.fmt, 2, 1)

        self.q = slider(*presets.LIMITS["quality"], 85)
        self.q_val = slider_row(grid, 3, "Quality", self.q, lambda v: f"{v}%")

        self.kb = QSpinBox()
        self.kb.setRange(*presets.LIMITS["max_kb"])
        self.kb.setSingleStep(50)
        self.kb.setSuffix(" KB")
        self.kb.setSpecialValueText("No limit")
        grid.addWidget(QLabel("Keep under"), 4, 0)
        grid.addWidget(self.kb, 4, 1)
        hint2 = QLabel("Lowers quality, then size, until it fits")
        hint2.setObjectName("dim")
        grid.addWidget(hint2, 4, 2)

        self.strip = QCheckBox("Remove camera info and GPS location")
        grid.addWidget(self.strip, 5, 1, 1, 2)
        grid.setColumnStretch(2, 1)
        self.body.addLayout(grid)

        self.summary = QLabel()
        self.summary.setObjectName("dim")
        self.summary.setWordWrap(True)
        self.body.addWidget(self.summary)
        self.note = QLabel()
        self.note.setObjectName("dim")
        self.note.setWordWrap(True)
        self.body.addWidget(self.note)
        self.body.addStretch(1)
        foot = QHBoxLayout()
        foot.addStretch(1)
        done = QPushButton("Done")
        done.setObjectName("primary")
        done.clicked.connect(self.close)
        foot.addWidget(done)
        self.body.addLayout(foot)

        self.new_btn.clicked.connect(self._new)
        self.del_btn.clicked.connect(self._delete)
        self.defaults_btn.clicked.connect(self._restore_examples)
        self.pick.currentIndexChanged.connect(lambda _: self._show())
        self.name.textEdited.connect(lambda _: self._edited())
        self.side.valueChanged.connect(lambda _: self._edited())
        self.fmt.currentIndexChanged.connect(lambda _: self._edited())
        self.q.valueChanged.connect(lambda _: self._edited())
        self.kb.valueChanged.connect(lambda _: self._edited())
        self.strip.toggled.connect(lambda _: self._edited())
        self._fill_picker(0)

    # ---------------------------------------------------------- list
    def _fill_picker(self, index: int):
        self._loading = True
        self.pick.clear()
        for p in self.presets:
            self.pick.addItem(p.name)
        self.pick.setCurrentIndex(min(index, len(self.presets) - 1) if self.presets else -1)
        self._loading = False
        self._show()

    def _current(self):
        i = self.pick.currentIndex()
        return self.presets[i] if 0 <= i < len(self.presets) else None

    def _show(self):
        p = self._current()
        have = p is not None
        for w in (self.name, self.side, self.fmt, self.q, self.kb, self.strip, self.del_btn):
            w.setEnabled(have)
        self.note.setText("" if have else "No presets yet. Click New to make one.")
        if not have:
            self.summary.setText("")
            return
        self._loading = True
        self.name.setText(p.name)
        self.side.setValue(p.max_side)
        self.fmt.setCurrentIndex(max(0, presets.FORMATS.index(p.fmt)))
        self.q.setValue(p.quality)
        self.kb.setValue(p.max_kb)
        self.strip.setChecked(p.strip)
        self._loading = False
        self._sync_enabled()
        self.summary.setText("On the menu: " + p.describe())

    def _sync_enabled(self):
        self.q.setEnabled(self.fmt.currentData() != "PNG" and self._current() is not None)

    # ---------------------------------------------------------- editing
    def _edited(self):
        if self._loading:
            return
        i = self.pick.currentIndex()
        if not (0 <= i < len(self.presets)):
            return
        name = self.name.text().strip()
        clash = any(j != i and q.name.lower() == name.lower() for j, q in enumerate(self.presets))
        self.note.setText("Another preset already has that name." if clash else
                          "" if name else "A preset needs a name.")
        p = self.presets[i]
        if name and not clash:
            p.name = name
        p.max_side, p.fmt, p.quality = self.side.value(), self.fmt.currentData(), self.q.value()
        p.max_kb, p.strip = self.kb.value(), self.strip.isChecked()
        self.presets[i] = presets.clean(p)
        presets.save(self.presets)
        p = self.presets[i]
        self.pick.setItemText(i, p.name)
        self._sync_enabled()
        self.summary.setText("On the menu: " + p.describe())

    def _new(self):
        p = presets.Preset(presets.unique_name("My preset", self.presets), 1600, "JPG", 85, 0, False)
        self.presets.append(p)
        presets.save(self.presets)
        self._fill_picker(len(self.presets) - 1)
        self.name.setFocus()
        self.name.selectAll()

    def _delete(self):
        i = self.pick.currentIndex()
        if 0 <= i < len(self.presets):
            gone = self.presets.pop(i).name
            presets.save(self.presets)
            self._fill_picker(max(0, i - 1))
            self.note.setText(f"Deleted “{gone}”." if self.presets else self.note.text())

    def _restore_examples(self):
        have = {p.name.lower() for p in self.presets}
        added = [presets.Preset(**vars(p)) for p in presets.DEFAULT_PRESETS if p.name.lower() not in have]
        self.presets += added
        presets.save(self.presets)
        self._fill_picker(self.pick.currentIndex() if self.presets else 0)
        self.note.setText(f"Added {len(added)} example preset{'s' if len(added) != 1 else ''}." if added
                          else "The examples are already there.")
