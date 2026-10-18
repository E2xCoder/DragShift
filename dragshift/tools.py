"""The tool windows: Crop, Add BG, Annotate, Redact, Edit, Metadata, Compress."""
from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Callable

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen,
                           QPixmap, QPolygonF)
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QColorDialog, QComboBox, QGridLayout,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QPushButton,
                               QSlider, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from . import imaging
from .convert import open_image
from .glass import pil_from_qimage, qimage_from_pil
from .util import file_size, human_size, sibling
from .window import GlassWindow, ImageView

Notify = Callable[[str, list], None]


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
    val.setMinimumWidth(52)
    val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    s.valueChanged.connect(lambda v: val.setText(fmt(v)))
    grid.addWidget(val, r, 2)
    return val


def footer(win: GlassWindow, primary_text: str, on_primary, on_reset=None) -> QHBoxLayout:
    row = QHBoxLayout()
    if on_reset:
        reset = QPushButton("Reset")
        reset.clicked.connect(on_reset)
        row.addWidget(reset)
    row.addStretch(1)
    cancel = QPushButton("Cancel")
    cancel.clicked.connect(win.close)
    row.addWidget(cancel)
    ok = QPushButton(primary_text)
    ok.setObjectName("primary")
    ok.setDefault(True)
    ok.clicked.connect(on_primary)
    row.addWidget(ok)
    return row


def preview_qimage(src: str | Path, max_side: int = 1400) -> tuple[Image.Image, QImage]:
    im = open_image(src)
    pv = im.copy()
    pv.thumbnail((max_side, max_side), Image.LANCZOS)
    return im, qimage_from_pil(pv)


class ToolWindow(GlassWindow):
    def __init__(self, title: str, path: str | Path, notify: Notify, size=(820, 680)):
        super().__init__(title, size=size)
        self.path = Path(path)
        self.notify = notify
        sub = QLabel(f"{self.path.name}  ·  {human_size(file_size(self.path))}")
        sub.setObjectName("dim")
        self.body.addWidget(sub)

    def finish(self, outs: list[Path], verb: str):
        self.notify(f"{verb} {self.path.name}", outs)
        self.close()


# ================================================================ CROP
class CropView(ImageView):
    HANDLE = 9

    def __init__(self, image: QImage):
        super().__init__(image)
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
        hs = self.HANDLE
        for pt in (r.topLeft(), r.topRight(), r.bottomLeft(), r.bottomRight()):
            p.drawEllipse(pt, hs / 1.6, hs / 1.6)
        for pt in (QPointF(r.center().x(), r.top()), QPointF(r.center().x(), r.bottom()),
                   QPointF(r.left(), r.center().y()), QPointF(r.right(), r.center().y())):
            p.drawRoundedRect(QRectF(pt.x() - 5, pt.y() - 3, 10, 6) if pt.y() in (r.top(), r.bottom())
                              else QRectF(pt.x() - 3, pt.y() - 5, 6, 10), 3, 3)


class CropWindow(ToolWindow):
    RATIOS = [("Free", None), ("1:1", 1.0), ("4:3", 4 / 3), ("3:2", 1.5), ("16:9", 16 / 9),
              ("9:16", 9 / 16), ("4:5", 0.8)]

    def __init__(self, path, notify):
        super().__init__("Crop Image", path, notify)
        self.full, qi = preview_qimage(path)
        self.k = self.full.width / qi.width()  # preview → full-res
        self.view = CropView(qi)
        self.body.addWidget(self.view, 1)

        self.body.addWidget(section("Aspect ratio"))
        row, self.grp = toggle_row([n for n, _ in self.RATIOS])
        self.grp.idClicked.connect(lambda i: self.view.set_ratio(self.RATIOS[i][1]))
        self.body.addLayout(row)

        dims = QHBoxLayout()
        self.w = QSpinBox(); self.h = QSpinBox()
        for s in (self.w, self.h):
            s.setRange(1, 100000)
            s.setFixedWidth(90)
        self.w.setMaximum(self.full.width); self.h.setMaximum(self.full.height)
        dims.addWidget(QLabel("W")); dims.addWidget(self.w)
        dims.addWidget(QLabel("H")); dims.addWidget(self.h)
        dims.addWidget(QLabel("px"))
        dims.addStretch(1)
        self.body.addLayout(dims)
        self.w.editingFinished.connect(self._from_spin)
        self.h.editingFinished.connect(self._from_spin)
        self.view.changed.connect(self._to_spin)
        self._to_spin()

        self.body.addLayout(footer(self, "Apply", self.apply, self.reset))

    def _to_spin(self):
        s = self.view.sel
        for box, v in ((self.w, s.width()), (self.h, s.height())):
            box.blockSignals(True)
            box.setValue(round(v * self.k))
            box.blockSignals(False)

    def _from_spin(self):
        s = QRectF(self.view.sel)
        s.setWidth(self.w.value() / self.k)
        s.setHeight(self.h.value() / self.k)
        self.view.sel = s.intersected(QRectF(0, 0, self.view.image.width(), self.view.image.height()))
        self.view.update()

    def reset(self):
        self.grp.button(0).setChecked(True)
        self.view.ratio = None
        self.view.sel = QRectF(0, 0, self.view.image.width(), self.view.image.height())
        self.view.update()
        self._to_spin()

    def apply(self):
        s = self.view.sel
        box = tuple(round(v * self.k) for v in (s.left(), s.top(), s.right(), s.bottom()))
        out = imaging.save_like(self.full.crop(box), self.path, "Cropped")
        self.finish([out], "Cropped")


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
    ASPECTS = ["Auto", "1:1", "4:3", "16:9", "9:16", "4:5"]

    def __init__(self, path, notify):
        super().__init__("Add BG", path, notify, size=(860, 760))
        self.full = open_image(path)
        self.small = self.full.copy()
        self.small.thumbnail((700, 700), Image.LANCZOS)
        self.bg = imaging.Background()
        self.colors = PRESETS[0][1]

        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(360, 300)
        self.body.addWidget(self.preview, 1)

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

        self.body.addLayout(footer(self, "Save with Background", self.save, self.reset))

        self._timer = QTimer(self, singleShot=True, interval=30, timeout=self.render)
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

    def render(self):
        k = self.small.width / self.full.width
        out = imaging.add_background(self.small, self._params(k * max(1, self.full.width / 1000)))
        pm = QPixmap.fromImage(qimage_from_pil(out))
        box = self.preview.size()
        self.preview.setPixmap(pm.scaled(box - QSize(10, 10), Qt.KeepAspectRatio, Qt.SmoothTransformation))

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

    def save(self):
        out_im = imaging.add_background(self.full, self._params(max(1, self.full.width / 1000)))
        out = sibling(self.path, "With Background", ".png")
        out_im.save(out, "PNG", optimize=True)
        self.finish([out], "Added background to")


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
        p.drawLine(a, b)
        ang = math.atan2(b.y() - a.y(), b.x() - a.x())
        head = max(10.0, w * 4)
        left = QPointF(b.x() - head * math.cos(ang - 0.45), b.y() - head * math.sin(ang - 0.45))
        right = QPointF(b.x() - head * math.cos(ang + 0.45), b.y() - head * math.sin(ang + 0.45))
        p.setBrush(col)
        p.drawPolygon(QPolygonF([b, left, right]))
    elif s.kind == "Rect" and len(pts) == 2:
        p.drawRoundedRect(QRectF(pts[0], pts[1]).normalized(), w, w)
    elif s.kind == "Ellipse" and len(pts) == 2:
        p.drawEllipse(QRectF(pts[0], pts[1]).normalized())
    elif s.kind == "Text" and pts:
        f = QFont("Segoe UI", 1)
        f.setPixelSize(max(8, int(s.width * 5 * scale)))
        f.setBold(True)
        p.setFont(f)
        p.drawText(pts[0], s.text)


class AnnotateView(ImageView):
    def __init__(self, image: QImage, owner: "AnnotateWindow"):
        super().__init__(image)
        self.owner = owner
        self.shapes: list[Shape] = []
        self.cur: Shape | None = None
        self.setCursor(Qt.CrossCursor)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or not self.target_rect().contains(e.position()):
            return
        o = self.owner
        p = self.to_image(e.position())
        width = o.width.value() / self.scale()  # keep on-screen thickness consistent
        if o.tool == "Text":
            text, ok = QInputDialog.getText(self, "Text", "Label:")
            if ok and text:
                self.shapes.append(Shape("Text", o.color, width, [p], text))
                self.update()
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
            self.shapes.append(self.cur)
            self.cur = None
            self.update()

    def paint_overlay(self, p, t):
        s = self.scale()
        for sh in self.shapes + ([self.cur] if self.cur else []):
            draw_shape(p, sh, self.to_widget, s)


class AnnotateWindow(ToolWindow):
    TOOLS = ["Pen", "Highlight", "Arrow", "Rect", "Ellipse", "Text"]
    COLORS = ["#ff3b30", "#ff9500", "#ffcc00", "#34c759", "#007aff", "#af52de", "#ffffff", "#000000"]

    def __init__(self, path, notify):
        super().__init__("Annotate", path, notify)
        self.full = open_image(path)
        self.tool, self.color = "Arrow", self.COLORS[0]
        qi = qimage_from_pil(self.full)
        self.view = AnnotateView(qi, self)
        self.body.addWidget(self.view, 1)

        row, grp = toggle_row(self.TOOLS, self.TOOLS.index("Arrow"))
        grp.idClicked.connect(lambda i: setattr(self, "tool", self.TOOLS[i]))
        undo = QPushButton("Undo")
        undo.setShortcut("Ctrl+Z")
        undo.clicked.connect(self.undo)
        row.addWidget(undo)
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
        self.width = slider(1, 20, 4)
        self.width.setFixedWidth(160)
        crow.addWidget(self.width)
        crow.addStretch(1)
        self.body.addLayout(crow)
        self.body.addLayout(footer(self, "Save", self.save))

    def undo(self):
        if self.view.shapes:
            self.view.shapes.pop()
            self.view.update()

    def save(self):
        img = qimage_from_pil(self.full.convert("RGBA"))
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        for sh in self.view.shapes:
            draw_shape(p, sh, lambda q: q, 1.0)
        p.end()
        out = imaging.save_like(pil_from_qimage(img), self.path, "Annotated")
        self.finish([out], "Annotated")


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
    MODES = ["Black box", "Pixelate", "Blur"]

    def __init__(self, path, notify):
        super().__init__("Redact", path, notify)
        self.full, qi = preview_qimage(path)
        self.base = pil_from_qimage(qi)
        self.k = self.full.width / qi.width()
        self.view = RedactView(qi, self)
        self.body.addWidget(self.view, 1)
        hint = QLabel("Drag over anything you want hidden. Redaction is burned into the pixels.")
        hint.setObjectName("dim")
        self.body.addWidget(hint)
        row, self.mode = toggle_row(self.MODES)
        self.mode.idClicked.connect(lambda _: self.refresh())
        undo = QPushButton("Undo")
        undo.setShortcut("Ctrl+Z")
        undo.clicked.connect(self.undo)
        row.addWidget(undo)
        self.body.addLayout(row)
        self.body.addLayout(footer(self, "Save", self.save))

    def _mode(self):
        return ["black", "pixelate", "blur"][self.mode.checkedId()]

    def _boxes(self, k: float):
        return [tuple(round(v * k) for v in (r.left(), r.top(), r.right(), r.bottom())) for r in self.view.rects]

    def refresh(self):
        im = imaging.redact(self.base, self._boxes(1.0), self._mode())
        self.view.set_image(qimage_from_pil(im))

    def undo(self):
        if self.view.rects:
            self.view.rects.pop()
            self.refresh()

    def save(self):
        im = imaging.redact(self.full, self._boxes(self.k), self._mode())
        out = imaging.save_like(im, self.path, "Redacted")
        self.finish([out], "Redacted")


# ================================================================ EDIT
class EditWindow(ToolWindow):
    def __init__(self, path, notify):
        super().__init__("Edit", path, notify, size=(860, 760))
        self.full = open_image(path)
        self.small = self.full.copy()
        self.small.thumbnail((900, 900), Image.LANCZOS)
        self.adj = imaging.Adjust()

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
        self.size = slider(5, 100, 100)
        slider_row(grid, len(self.sl), "Size", self.size, lambda v: f"{v}%")
        self.size.valueChanged.connect(self._size_label)
        self.dim_label = QLabel()
        self.dim_label.setObjectName("dim")
        grid.addWidget(self.dim_label, len(self.sl) + 1, 1)
        self.body.addLayout(grid)

        row = QHBoxLayout()
        for text, fn in (("⟲ Rotate", lambda: self._rot(-90)), ("⟳ Rotate", lambda: self._rot(90)),
                         ("⇋ Flip H", lambda: self._flip("h")), ("⇵ Flip V", lambda: self._flip("v"))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        self.gray = QPushButton("B&&W")
        self.gray.setCheckable(True)
        self.gray.toggled.connect(lambda _: self._timer.start())
        row.addWidget(self.gray)
        row.addStretch(1)
        self.body.addLayout(row)
        self.body.addLayout(footer(self, "Save", self.save, self.reset))

        self._timer = QTimer(self, singleShot=True, interval=40, timeout=self.render)
        self._size_label()
        self.render()

    def _size_label(self):
        p = self.size.value() / 100
        w, h = self.full.size
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
        a.resize_pct = self.size.value() if resize else 100
        return a

    def render(self):
        out = imaging.apply_adjust(self.small, self._collect(False))
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
        self.size.setValue(100)
        self.gray.setChecked(False)
        self.adj = imaging.Adjust()
        self._size_label()
        self.render()

    def save(self):
        out = imaging.save_like(imaging.apply_adjust(self.full, self._collect(True)), self.path, "Edited")
        self.finish([out], "Edited")


# ================================================================ METADATA
class MetadataWindow(ToolWindow):
    def __init__(self, path, notify, is_pdf: bool = False):
        super().__init__("Metadata", path, notify, size=(620, 560))
        self.is_pdf = is_pdf
        info = imaging.read_pdf_metadata(path) if is_pdf else imaging.read_image_metadata(path)
        has_gps = any(k.startswith("GPS") for k in info)
        if has_gps:
            warn = QLabel("⚠  This file contains GPS location data.")
            warn.setStyleSheet("color: #ffd36b; font-weight: 600;")
            self.body.addWidget(warn)
        t = QTableWidget(len(info), 2)
        t.setHorizontalHeaderLabels(["Field", "Value"])
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        t.horizontalHeader().setStretchLastSection(True)
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        for r, (k, v) in enumerate(info.items()):
            t.setItem(r, 0, QTableWidgetItem(k))
            t.setItem(r, 1, QTableWidgetItem(v))
        self.body.addWidget(t, 1)
        self.body.addLayout(footer(self, "Strip && Save Copy", self.strip))

    def strip(self):
        out = imaging.strip_pdf_metadata(self.path) if self.is_pdf else imaging.strip_image_metadata(self.path)
        self.finish([out], "Removed metadata from")


# ================================================================ COMPRESS
class CompressWindow(ToolWindow):
    SIDES = [("Original size", None), ("Max 3840 px", 3840), ("Max 2560 px", 2560),
             ("Max 1920 px", 1920), ("Max 1280 px", 1280), ("Max 800 px", 800)]

    def __init__(self, path, notify):
        super().__init__("Compress", path, notify, size=(520, 330))
        self.orig = file_size(path)
        self.full = open_image(path)

        grid = QGridLayout()
        self.q = slider(10, 95, 72)
        slider_row(grid, 0, "Quality", self.q, lambda v: f"{v}")
        grid.addWidget(QLabel("Size"), 1, 0)
        self.side = QComboBox()
        for name, _ in self.SIDES:
            self.side.addItem(name)
        grid.addWidget(self.side, 1, 1)
        self.webp = QCheckBox("Convert to WebP (usually smallest)")
        grid.addWidget(self.webp, 2, 1)
        self.body.addLayout(grid)

        self.est = QLabel()
        self.est.setStyleSheet("font-size: 15pt; font-weight: 600;")
        self.body.addWidget(self.est)
        self.body.addStretch(1)
        self.body.addLayout(footer(self, "Compress", self.save))

        self._timer = QTimer(self, singleShot=True, interval=150, timeout=self.estimate)
        self.q.valueChanged.connect(lambda _: self._timer.start())
        self.side.currentIndexChanged.connect(lambda _: self._timer.start())
        self.webp.toggled.connect(lambda _: self._timer.start())
        self.estimate()

    def estimate(self):
        im = self.full.copy()
        side = self.SIDES[self.side.currentIndex()][1]
        if side and max(im.size) > side:
            im.thumbnail((side, side), Image.LANCZOS)
        ext = ".webp" if self.webp.isChecked() else self.path.suffix.lower()
        tgt = imaging.target_for("x" + ext)
        buf = io.BytesIO()
        q = self.q.value()
        if tgt == "PNG":
            im.convert("RGBA").quantize(colors=max(16, min(256, int(q * 2.56))),
                                        method=Image.Quantize.FASTOCTREE).save(buf, "PNG", optimize=True)
        else:
            from .convert import save_image
            save_image(im, buf, tgt if tgt not in ("BMP", "TIFF", "GIF", "ICO") else "JPG", quality=q)
        n = buf.tell()
        pct = (1 - n / max(1, self.orig)) * 100
        self.est.setText(f"{human_size(self.orig)}  →  {human_size(n)}   ({pct:+.0f}%)".replace("+", "−", 1)
                         if pct > 0 else f"{human_size(self.orig)}  →  {human_size(n)}")

    def save(self):
        out = imaging.compress_image(self.path, self.q.value(), self.SIDES[self.side.currentIndex()][1],
                                     self.webp.isChecked())
        self.finish([out], f"Compressed ({human_size(self.orig)} → {human_size(file_size(out))})")
