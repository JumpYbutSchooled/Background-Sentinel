"""Tray icon drawn at runtime.

Avoids shipping a .ico. It follows the theme like everything else — the plate
is cut the way the theme cuts plates and takes its colours from the palette —
so the icon in the tray is the same object as the interface it summons.

The tray does not repaint itself when the theme changes, so `sentinel_icon()`
is called again from there; the drawing here is stateless and cheap.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPen, QPixmap

from .paint import ACCENT, CARD_FILL, chrome, plate, stroke


def _pixmap(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    radius = size * 0.18
    rect = QRectF(0, 0, size, size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(CARD_FILL)
    plate(painter, rect, radius)

    painter.setPen(
        QPen(
            ACCENT,
            max(1.0, size * 0.09),
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
    )
    # A hairline of the theme's own edge, so a chamfered icon reads as cut
    # rather than as a rectangle that lost its corners.
    painter.setBrush(Qt.BrushStyle.NoBrush)
    inset = max(0.5, size * 0.03)
    painter.setPen(QPen(ACCENT, stroke(max(1.0, size * 0.045))))
    plate(painter, rect.adjusted(inset, inset, -inset, -inset), radius)

    font = chrome(int(size * 0.55), bold=True)
    painter.setFont(font)
    painter.setPen(ACCENT)
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, ">_")

    painter.end()
    return pixmap


def sentinel_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 256):
        icon.addPixmap(_pixmap(size))
    return icon
