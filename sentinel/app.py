

from __future__ import annotations

import ctypes
import logging
import signal
import sys
import threading
from ctypes import wintypes

from PySide6.QtCore import QObject, QRectF, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from . import (
    APP_NAME,
    APP_SLUG,
    APP_VERSION,
    DEFAULT_HOTKEY,
    autostart,
    commands,
    desktop,
    windowless,
)
from .config import settings
from .timers import timers
from .hotkey import GlobalHotkey, HotkeyError
from .logging_setup import log_file, setup_logging
from .single_instance import SingleInstance
from .transcript import transcript
from .tray import Tray
from .ui.icon import sentinel_icon
from .ui.nav import NavigatorWindow
from .ui.popup import PopupWindow

log = logging.getLogger(__name__)

#: Kept for callers that ask what raises the navigator; the registry is the
#: single source of truth, so these are read from it rather than duplicated.
OPEN_COMMANDS = frozenset({"open", *commands.REGISTRY["open"].aliases})


class _Deferred(QObject):
    """The way a slow command's answer gets back onto the thread that draws.

    Nothing in the UI may be touched from a worker, and the transcript's
    listeners repaint two windows. A queued signal is the one crossing Qt
    guarantees, so the worker's only contact with the rest of the process is
    `finished.emit(...)` — everything after that runs on the GUI thread.
    """

    #: (transcript.Entry, commands.Result) — untyped because Signal cannot
    #: carry a dataclass without a registered metatype, and `object` can.
    finished = Signal(object, object)


class SentinelApp:
    def __init__(self, hotkey_spec: str = DEFAULT_HOTKEY) -> None:
        self.hotkey_spec = hotkey_spec

        self.qapp = QApplication(sys.argv)
        self.qapp.setApplicationName(APP_NAME)
        self.qapp.setApplicationVersion(APP_VERSION)
        self.qapp.setWindowIcon(sentinel_icon())
        # Critical for a daemon: hiding the popup must not end the process.
        self.qapp.setQuitOnLastWindowClosed(False)

        self.popup = PopupWindow()
        self.popup.submitted.connect(self.handle_command)

        self.navigator = NavigatorWindow()
        self.navigator.invoked.connect(self.handle_navigator_choice)
        # The docked command line routes back through the same handler, so a
        # command typed there behaves exactly like one typed at the popup.
        self.navigator.submitted.connect(self.handle_command)
        # Escaping the navigator hands the prompt back rather than dropping the
        # user on the desktop — the band folds into the popup's own capsule.
        # `handover`, not `summon`: the navigator's last frame is already drawn
        # as this card, so flickering on here would spoil a seamless swap.
        self.navigator.closed.connect(self.popup.handover)

        self.tray = Tray(self._hotkey_label())
        self.tray.summon_requested.connect(self.popup.summon)
        self.tray.quit_requested.connect(self.shutdown)

        # The settings panel and the tray menu are two views of one registry
        # key. Seed from what the registry actually says, then keep them in
        # step — otherwise the checkbox and the setting can disagree. Seeded
        # after the tray exists, because the listener touches it.
        settings.set("autostart", autostart.is_enabled())
        settings.listeners.append(self._on_setting_changed)

        # Reminders and pomodoro blocks fire whether or not anything is on
        # screen — that is the point of a resident daemon.
        timers.fired.connect(self.tray.notify)

        self.hotkey = GlobalHotkey(hotkey_spec)
        self.hotkey.pressed.connect(self.popup.toggle)

        # Commands that drive Sentinel's own windows call back through here.
        commands.set_host(self)

        self._deferred = _Deferred()
        self._deferred.finished.connect(self._settle)

        self._sigint_timer: QTimer | None = None

    # ------------------------------------------------------------- commands

    def handle_command(self, text: str) -> None:
        """Run a typed command through the registry.

        Everything goes through it, including the ones that drive Sentinel's
        own windows — special-casing those here is what made them show up red
        at the prompt, since the highlighter only knows the registry.

        This is also the single point where the scroll-back is written, which
        is why both command lines show the same history: they are two views of
        one session, not two independent boxes.
        """
        log.info("command: %r", text)
        result = commands.run(text)
        entry = None
        if not result.quiet:
            entry = transcript.record(text, result.output, result.ok)
        self._report(result)
        if result.pending is not None and entry is not None:
            self._defer(entry, result.pending)

    def _defer(self, entry, work) -> None:
        """Finish a command off the UI thread, then amend the row it wrote.

        Daemon threads by design: a lookup still in flight must never be the
        reason `quit` hangs. The worst case is an answer nobody sees, which is
        exactly what shutting down means.
        """
        def worker() -> None:
            try:
                outcome = work()
            except Exception as exc:  # a failed lookup must not kill the thread
                log.exception("Deferred work for %r failed", entry.command)
                outcome = commands.Result(False, f"{entry.command}: {exc}")
            self._deferred.finished.emit(entry, outcome)

        threading.Thread(target=worker, name="sentinel-defer", daemon=True).start()

    def _settle(self, entry, result: commands.Result) -> None:
        """A deferred answer, arrived. Runs on the GUI thread."""
        transcript.amend(entry, result.output, result.ok)
        self._report(result)

    # ------------------------------------------------- commands.Host

    def open_navigator(self, then: str = "") -> None:
        # Two beats, not one. First the card empties itself — its contents
        # flicker out inside a card that does not move, so the running timer
        # and the history are seen to leave rather than simply being gone.
        # Only then does the overlay take the shape over, and the shape it
        # takes over is the one that was on screen.
        self.popup.hand_off(lambda: self._raise_navigator(then))

    def _raise_navigator(self, then: str) -> None:
        # `prompt_rise` tells the overlay where the prompt sits inside that
        # shape, since with history above it, it is not the middle. Then yield
        # the foreground deliberately: the popup hides on focus loss anyway,
        # and doing it first keeps the two from fighting.
        origin = QRectF(self.popup.geometry())
        rise = self.popup.prompt_rise()
        self.popup.hide()
        self.navigator.open(origin, then=then, prompt_rise=rise)

    def close_ui(self) -> None:
        """Stand down entirely, so whatever we started takes the foreground."""
        if self.navigator.isVisible():
            self.navigator.dismiss(hand_back=False)
        self.popup.dismiss()

    def _report(self, result: commands.Result) -> None:
        """Log a command's outcome and act on what it asked for.

        Showing it is the scroll-back's job now — both command lines read the
        transcript directly, so pushing the message at whichever window happens
        to be up would only duplicate the row they have both already drawn.
        """
        for line in result.output:
            log.info("-> %s", line)
            print(f"[sentinel] {line}", flush=True)
        # Here rather than in the command, because a command that finished on a
        # worker cannot touch the clipboard itself — QClipboard belongs to the
        # thread that draws, and this method only ever runs on it.
        if result.copy:
            desktop.set_clipboard(result.copy)
        if result.close_ui:
            self.close_ui()

    def _on_setting_changed(self, key: str, value: object) -> None:
        if key != "autostart":
            return
        wanted = bool(value)
        if autostart.is_enabled() != wanted and not autostart.set_enabled(wanted):
            log.warning("Could not apply autostart=%s", wanted)
            return
        self.tray.set_autostart(wanted)

    def handle_navigator_choice(self, path: str) -> None:
        """A leaf was opened in the navigator.

        Every leaf now carries a working panel of its own, so there is nothing
        to dispatch — this only puts the command's usage on the status line, as
        a reminder of what the panel above it is for.
        """
        log.info("navigator: %s", path)
        leaf = path.split()[-1] if path else ""
        command = commands.lookup(leaf)
        if command is not None:
            self.navigator.set_status(f"{command.usage} — {command.summary}", ok=True)

    # ------------------------------------------------------------ lifecycle

    def run(self) -> int:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            QMessageBox.critical(
                None, APP_NAME, "No system tray is available, so Sentinel cannot run."
            )
            return 1

        try:
            self.hotkey.register(self.qapp)
        except HotkeyError as exc:
            log.error("%s", exc)
            QMessageBox.critical(
                None,
                APP_NAME,
                f"{exc}\n\nChange DEFAULT_HOTKEY in sentinel/__init__.py and start again.",
            )
            return 1

        self.tray.show()
        self._install_sigint_handler()

        log.info("%s %s started (hotkey %s)", APP_NAME, APP_VERSION, self.hotkey_spec)
        log.info("Logging to %s", log_file())
        self.tray.notify(APP_NAME, f"Running in the background. Press {self._hotkey_label()}.")

        return self.qapp.exec()

    def shutdown(self) -> None:
        log.info("Shutting down")
        self.hotkey.unregister(self.qapp)
        self.tray.hide()
        self.navigator.close()
        self.popup.close()
        self.qapp.quit()

    # --------------------------------------------------------------- detail

    def _hotkey_label(self) -> str:
        return "+".join(part.strip().capitalize() for part in self.hotkey_spec.split("+"))

    def _install_sigint_handler(self) -> None:
        """Let Ctrl+C work when launched from a console.

        Qt's event loop blocks in native code, so Python never gets a chance to
        run its signal handler unless the loop is woken periodically. Only done
        when a console is attached, to keep the packaged daemon fully idle.
        """
        if sys.stderr is None:
            return
        signal.signal(signal.SIGINT, lambda *_: self.shutdown())
        self._sigint_timer = QTimer()
        self._sigint_timer.timeout.connect(lambda: None)
        self._sigint_timer.start(250)


def _set_app_user_model_id() -> None:
    """Give the process its own taskbar/notification identity.

    Without this, Windows attributes the tray balloon to python.exe.
    """
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        set_id = shell32.SetCurrentProcessExplicitAppUserModelID
        set_id.argtypes = [wintypes.LPCWSTR]
        set_id.restype = ctypes.HRESULT
        set_id(APP_SLUG)
    except Exception:
        log.debug("Could not set AppUserModelID", exc_info=True)


def main() -> int:
    if sys.platform != "win32":
        print("Background Sentinel targets Windows.", file=sys.stderr)
        return 1

    # First, before the log file is opened or the mutex taken: this process may
    # be about to be replaced by a windowless one, and two of them holding
    # either would be two of them to clean up after. Run with `--console` (or
    # `SENTINEL_CONSOLE=1`) to stay attached and watch it print.
    if windowless.hand_off():
        return 0

    setup_logging()

    guard = SingleInstance(f"{APP_SLUG}.daemon")
    if guard.already_running:
        log.warning("Another instance is already running; exiting")
        # A QApplication must exist before any widget, including a message box.
        # Qt owns the instance once constructed, so no local reference is needed.
        QApplication(sys.argv)
        QMessageBox.information(None, APP_NAME, f"{APP_NAME} is already running.")
        guard.release()
        return 0

    _set_app_user_model_id()

    try:
        return SentinelApp().run()
    finally:
        guard.release()
