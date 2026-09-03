

from __future__ import annotations

import logging
import os
import subprocess

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from . import APP_NAME, APP_VERSION, autostart
from .config import settings
from .logging_setup import log_dir
from .ui.icon import sentinel_icon

log = logging.getLogger(__name__)


class Tray(QObject):
    summon_requested = Signal()
    quit_requested = Signal()

    def __init__(self, hotkey_label: str, parent: QObject | None = None) -> None:
        super().__init__(parent)

        self.icon = QSystemTrayIcon(sentinel_icon(), parent)
        self.icon.setToolTip(f"{APP_NAME} {APP_VERSION} — {hotkey_label}")

        menu = QMenu()

        summon = QAction(f"Open Sentinel\t{hotkey_label}", menu)
        summon.triggered.connect(self.summon_requested.emit)
        menu.addAction(summon)

        menu.addSeparator()

        self.autostart_action = QAction("Start with Windows", menu)
        self.autostart_action.setCheckable(True)
        self.autostart_action.setChecked(autostart.is_enabled())
        self.autostart_action.toggled.connect(self._on_autostart_toggled)
        menu.addAction(self.autostart_action)

        open_logs = QAction("Open log folder", menu)
        open_logs.triggered.connect(self._open_logs)
        menu.addAction(open_logs)

        menu.addSeparator()

        quit_action = QAction("Quit Sentinel", menu)
        quit_action.triggered.connect(self.quit_requested.emit)
        menu.addAction(quit_action)

        self._menu = menu  # keep alive
        self.icon.setContextMenu(menu)
        self.icon.activated.connect(self._on_activated)

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def notify(self, title: str, message: str) -> None:
        if self.icon.supportsMessages():
            self.icon.showMessage(title, message, sentinel_icon())

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.summon_requested.emit()

    def set_autostart(self, enabled: bool) -> None:
        """Reflect a change made elsewhere, without re-firing this handler."""
        if self.autostart_action.isChecked() == enabled:
            return
        self.autostart_action.blockSignals(True)
        self.autostart_action.setChecked(enabled)
        self.autostart_action.blockSignals(False)

    def _on_autostart_toggled(self, checked: bool) -> None:
        # Routed through settings so the tray and the settings panel agree;
        # the app's listener is what actually writes the registry.
        settings.set("autostart", checked)
        if autostart.is_enabled() != checked:
            self.set_autostart(not checked)
            self.notify(APP_NAME, "Could not change the autostart setting. See the log.")

    def _open_logs(self) -> None:
        path = str(log_dir())
        try:
            os.startfile(path)  # noqa: S606 - Windows shell open
        except Exception:
            log.exception("Could not open log folder")
            subprocess.Popen(["explorer", path])
