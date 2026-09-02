"""Logging + crash capture.

A silent daemon crash is easy to miss, so everything goes to a rotating log
file under %LOCALAPPDATA%\\BackgroundSentinel\\logs. Unhandled exceptions on the
main thread, on worker threads, and inside Qt itself are all funnelled here.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .store import data_dir

__all__ = ["data_dir", "log_dir", "log_file", "setup_logging"]

_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"

_configured = False


def log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_file() -> Path:
    return log_dir() / "sentinel.log"


def setup_logging(level: int = logging.INFO) -> None:
    # Calling this twice would attach a second set of handlers and duplicate
    # every line in the log file.
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    root.setLevel(level)

    file_handler = RotatingFileHandler(
        log_file(), maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(file_handler)

    # Under pythonw.exe there is no console and sys.stderr is None, so guard it.
    if sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(stream_handler)

    _install_crash_hooks()


def _install_crash_hooks() -> None:
    log = logging.getLogger("crash")

    def excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = excepthook

    def thread_excepthook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        log.critical(
            "Unhandled exception in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = thread_excepthook

    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:  # pragma: no cover
        return

    qt_log = logging.getLogger("qt")
    _levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def qt_handler(mode, context, message):
        qt_log.log(_levels.get(mode, logging.INFO), message)

    qInstallMessageHandler(qt_handler)
