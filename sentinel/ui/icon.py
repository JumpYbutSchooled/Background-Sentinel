"""Tray icon drawn at runtime.

Avoids shipping a .ico for now. Replace with a real asset during the polish
pass; keep the signature so nothing else has to change.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap

BACKGROUND = QColor("#12141a")
ACCENT = QColor("#39d353")


def _pixmap(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    radius = size * 0.18
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(BACKGROUND)
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    painter.setPen(
        QPen(
            ACCENT,
            max(1.0, size * 0.09),
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
    )
    font = QFont("Consolas")
    font.setPixelSize(int(size * 0.55))
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, ">_")

    painter.end()
    return pixmap


def sentinel_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 256):
        icon.addPixmap(_pixmap(size))
    return icon
