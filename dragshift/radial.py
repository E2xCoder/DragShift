"""The radial drop menu that pops up under the cursor while you Shift+drag a file."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import (QEasingCurve, QPoint, QPointF, QRectF, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QCursor, QFont, QGuiApplication, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import QWidget

from . import glass


@dataclass
class Item:
    label: str
    glyph: str = ""
    run: Callable[[list[str]], None] | None = None
    key: str = ""


SIZE = 380          # window size
R_OUT = 158         # outer radius of petals
R_IN = 58           # inner radius of petals
R_HUB = 50          # hub radius
GAP_DEG = 3.0


def sector_path(c: QPointF, r0: float, r1: float, a0: float, a1: float) -> QPainterPath:
    """Annular sector between angles a0..a1 (degrees, Qt convention: 0 = 3 o'clock, CCW)."""
    path = QPainterPath()
    outer = QRectF(c.x() - r1, c.y() - r1, 2 * r1, 2 * r1)
    inner = QRectF(c.x() - r0, c.y() - r0, 2 * r0, 2 * r0)
    path.arcMoveTo(outer, a0)
    path.arcTo(outer, a0, a1 - a0)
    path.arcTo(inner, a1, -(a1 - a0))
    path.closeSubpath()
    return path


class RadialMenu(QWidget):
    activated = Signal(object, list)     # Item, paths
    dismissed = Signal()

    def __init__(self, build_rings: Callable[[list[str]], dict[str, list[Item]]],
                 describe: Callable[[list[str]], str]):
        super().__init__(None)
        self.build_rings = build_rings
        self.describe = describe
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint |
                            Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.resize(SIZE, SIZE)

        self.paths: list[str] = []
        self.rings: dict[str, list[Item]] = {}
        self.ring_names: list[str] = []
        self.ring_idx = 0
        self.hover = -1          # petal index, -2 = hub, -1 = none
        self.hub_text = ""
        self.backdrop: QPixmap | None = None
        self.got_drag = False
        self.hub_armed = False
        self.click_mode = False  # opened from tray/menu instead of a drag
        self._t = 1.0            # pop-in animation 0..1
        self._swap = 1.0         # ring-swap animation 0..1

        self.anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=170,
                                      easingCurve=QEasingCurve.OutBack)
        self.anim.valueChanged.connect(self._set_t)
        self.swap_anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=180,
                                           easingCurve=QEasingCurve.OutCubic)
        self.swap_anim.valueChanged.connect(self._set_swap)
        self.hub_timer = QTimer(self, singleShot=True, interval=420, timeout=self.next_ring)

    # ------------------------------------------------------------ show / hide
    def popup(self, pos: QPoint | None = None, paths: list[str] | None = None):
        pos = pos or QCursor.pos()
        tl = pos - QPoint(SIZE // 2, SIZE // 2)
        scr = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        g = scr.geometry()
        tl.setX(min(max(tl.x(), g.left()), g.right() - SIZE))
        tl.setY(min(max(tl.y(), g.top()), g.bottom() - SIZE))
        # snapshot what's underneath *before* we appear, for the frosted glass
        try:
            shot = scr.grabWindow(0, tl.x() - g.left(), tl.y() - g.top(), SIZE, SIZE)
            self.backdrop = glass.blur_pixmap(shot, 24)
        except Exception:
            self.backdrop = None
        self.move(tl)
        self.hover = -1
        self.got_drag = False
        self.hub_armed = False  # drags start on the hub; don't flip until they leave it
        self.click_mode = paths is not None
        if paths is not None:
            self._load(paths)
        else:
            self.paths, self.rings, self.ring_names = [], {}, []
            self.hub_text = "Drop here"
        self._t = 0.0
        self.show()
        self.raise_()
        self.anim.start()

    def close_menu(self):
        if self.isVisible():
            self.hide()
            self.hub_timer.stop()
            self.dismissed.emit()

    def _load(self, paths: list[str]):
        self.paths = paths
        self.rings = {k: v for k, v in self.build_rings(paths).items() if v}
        self.ring_names = list(self.rings)
        self.ring_idx = 0
        self.hub_text = self.describe(paths)
        self.update()

    def next_ring(self):
        if len(self.ring_names) > 1:
            self.ring_idx = (self.ring_idx + 1) % len(self.ring_names)
            self.hover = -1
            self._swap = 0.0
            self.swap_anim.start()
            self.update()

    def _set_t(self, v):
        self._t = float(v)
        self.update()

    def _set_swap(self, v):
        self._swap = float(v)
        self.update()

    # ------------------------------------------------------------ geometry
    @property
    def items(self) -> list[Item]:
        if not self.ring_names:
            return []
        return self.rings[self.ring_names[self.ring_idx]]

    def _angles(self, n: int, i: int) -> tuple[float, float]:
        span = 360.0 / n
        start = 90 + span / 2 - (i + 1) * span   # first petal centred at 12 o'clock, clockwise
        return start + GAP_DEG / 2, start + span - GAP_DEG / 2

    def hit(self, pos: QPointF) -> int:
        c = QPointF(SIZE / 2, SIZE / 2)
        dx, dy = pos.x() - c.x(), c.y() - pos.y()
        d = math.hypot(dx, dy)
        if d <= R_HUB:
            return -2
        if d > R_OUT + 24 or not self.items:
            return -1
        ang = math.degrees(math.atan2(dy, dx)) % 360
        n = len(self.items)
        rel = (90 + 180.0 / n - ang) % 360
        return int(rel // (360.0 / n)) % n

    def _set_hover(self, h: int):
        if h != self.hover:
            self.hover = h
            if h >= 0 or h == -1:
                self.hub_armed = True  # left the hub at least once
            if h == -2 and self.hub_armed and len(self.ring_names) > 1:
                self.hub_timer.start()
            else:
                self.hub_timer.stop()
            self.update()

    # ------------------------------------------------------------ drag & drop
    @staticmethod
    def _local_paths(md) -> list[str]:
        if not md.hasUrls():
            return []
        return [u.toLocalFile() for u in md.urls() if u.isLocalFile() and u.toLocalFile()]

    def dragEnterEvent(self, e):
        paths = self._local_paths(e.mimeData())
        if not paths:
            e.ignore()
            return
        self.got_drag = True
        if paths != self.paths:
            self._load(paths)
        e.setDropAction(Qt.CopyAction)  # never let a Shift(=move) drop delete the source
        e.accept()

    def dragMoveEvent(self, e):
        self._set_hover(self.hit(e.position()))
        e.setDropAction(Qt.CopyAction)
        e.accept()

    def dragLeaveEvent(self, e):
        self._set_hover(-1)

    def dropEvent(self, e):
        idx = self.hit(e.position())
        items = self.items
        if 0 <= idx < len(items):
            e.setDropAction(Qt.CopyAction)
            e.accept()
            item, paths = items[idx], list(self.paths)
            self.hide()
            # return to Explorer's drag loop immediately; do the work after
            QTimer.singleShot(0, lambda: self.activated.emit(item, paths))
        else:
            e.ignore()
            self.close_menu()

    # ------------------------------------------------------------ click mode
    def mouseMoveEvent(self, e):
        if self.click_mode:
            self._set_hover(self.hit(e.position()))

    def mouseReleaseEvent(self, e):
        if not self.click_mode:
            return
        if e.button() == Qt.RightButton:
            self.close_menu()
            return
        idx = self.hit(e.position())
        if idx == -2:
            self.next_ring()
        elif 0 <= idx < len(self.items):
            item, paths = self.items[idx], list(self.paths)
            self.hide()
            QTimer.singleShot(0, lambda: self.activated.emit(item, paths))
        else:
            self.close_menu()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close_menu()
        elif e.key() in (Qt.Key_Tab, Qt.Key_Shift):
            self.next_ring()

    def leaveEvent(self, e):
        if self.click_mode:
            self._set_hover(-1)

    # ------------------------------------------------------------ painting
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        c = QPointF(SIZE / 2, SIZE / 2)
        t = self._t
        p.setOpacity(min(1.0, max(0.0, t * 1.4)))
        p.translate(c)
        s = 0.82 + 0.18 * t
        p.scale(s, s)
        p.translate(-c)

        # ambient glow so the glass reads on dark and light wallpapers
        halo = QRadialGradient(c, R_OUT + 26)
        halo.setColorAt(0.80, QColor(0, 0, 0, 55))
        halo.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(halo)
        p.drawEllipse(c, R_OUT + 26, R_OUT + 26)

        items = self.items
        n = len(items)
        swap = self._swap
        rot = (1 - swap) * -25  # petals swirl in when switching rings
        for i, it in enumerate(items):
            a0, a1 = self._angles(n, i)
            a0 += rot; a1 += rot
            hov = i == self.hover
            r1 = R_OUT + (8 if hov else 0)
            path = sector_path(c, R_IN, r1, a0, a1)
            tint = QColor(255, 140, 110, 70) if hov else QColor(22, 18, 34, 62)
            glass.paint_glass(p, path, self.backdrop, QPointF(0, 0), tint=tint,
                              rainbow_alpha=230 if hov else 140, highlight=1.3 if hov else 1.0)
            if hov:
                warm = QLinearGradient(path.boundingRect().topLeft(), path.boundingRect().bottomRight())
                warm.setColorAt(0, QColor(255, 180, 90, 175))
                warm.setColorAt(0.55, QColor(255, 70, 130, 165))
                warm.setColorAt(1, QColor(130, 100, 255, 165))
                p.fillPath(path, warm)
                p.setPen(glass.iridescent_pen(c, 2.4, 255, angle=(a0 + a1) / 2))
                p.drawPath(path)
            self._petal_label(p, c, it, (a0 + a1) / 2, (R_IN + r1) / 2, hov, swap)

        # hub
        hub = QPainterPath()
        hub.addEllipse(c, R_HUB, R_HUB)
        hub_hov = self.hover == -2
        glass.paint_glass(p, hub, self.backdrop, QPointF(0, 0),
                          tint=QColor(255, 255, 255, 70) if hub_hov else QColor(22, 18, 34, 90),
                          rainbow_alpha=220, highlight=1.4)
        p.setPen(QColor(255, 255, 255))
        f = QFont("Segoe UI", 1)
        f.setPixelSize(13)
        f.setBold(True)
        p.setFont(f)
        ring_name = self.ring_names[self.ring_idx] if self.ring_names else ""
        top = QRectF(c.x() - R_HUB, c.y() - 22, 2 * R_HUB, 20)
        p.drawText(top, Qt.AlignCenter, self.hub_text)
        f.setPixelSize(10)
        f.setBold(False)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255, 190))
        if len(self.ring_names) > 1:
            other = self.ring_names[(self.ring_idx + 1) % len(self.ring_names)]
            sub = f"⇄ {other.upper()}"
        else:
            sub = ring_name.upper()
        p.drawText(QRectF(c.x() - R_HUB + 2, c.y() + 2, 2 * R_HUB - 4, 18), Qt.AlignCenter, sub)

    def _petal_label(self, p: QPainter, c: QPointF, it: Item, ang: float, r: float,
                     hov: bool, swap: float):
        rad = math.radians(ang)
        pt = QPointF(c.x() + r * math.cos(rad), c.y() - r * math.sin(rad))
        p.save()
        p.setOpacity(p.opacity() * (0.3 + 0.7 * swap))
        p.setPen(QColor(255, 255, 255))
        f = QFont("Segoe UI", 1)
        if it.glyph:
            f.setPixelSize(19 if hov else 17)
            p.setFont(f)
            p.drawText(QRectF(pt.x() - 30, pt.y() - 22, 60, 22), Qt.AlignHCenter | Qt.AlignBottom, it.glyph)
            f.setPixelSize(10)
            f.setBold(True)
            f.setLetterSpacing(QFont.AbsoluteSpacing, 0.6)
            p.setFont(f)
            p.drawText(QRectF(pt.x() - 40, pt.y() + 1, 80, 16), Qt.AlignCenter, it.label.upper())
        else:
            f.setPixelSize(15 if hov else 13)
            f.setBold(True)
            f.setLetterSpacing(QFont.AbsoluteSpacing, 0.8)
            p.setFont(f)
            p.drawText(QRectF(pt.x() - 40, pt.y() - 11, 80, 22), Qt.AlignCenter, it.label)
        p.restore()
