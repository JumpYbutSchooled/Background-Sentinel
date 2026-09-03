

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

#: Set on the copy we start, so it does not hand over again and again. Also
#: honoured if you set it yourself, as a way to force a console run.
RELAUNCHED = "SENTINEL_WINDOWLESS"

#: Ways to ask for the console to be kept — for development, where the whole
#: point is to watch it print. Either the environment or the command line.
CONSOLE_ENV = "SENTINEL_CONSOLE"
CONSOLE_FLAG = "--console"

# Detached *and* in its own process group: a detached process inherits no
# console, and a new group means a Ctrl+C aimed at the shell we were started
# from cannot reach it either.
_DETACHED = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP


def wanted() -> bool:
    """Whether this process should be handing itself over at all."""
    if sys.platform != "win32":
        return False
    # A packaged build is already built for the right subsystem; re-launching
    # it would only fork the daemon in two.
    if getattr(sys, "frozen", False):
        return False
    if os.environ.get(RELAUNCHED) or os.environ.get(CONSOLE_ENV):
        return False
    if CONSOLE_FLAG in sys.argv:
        return False
    # No console to escape. `pythonw` and a detached child both land here, so
    # this is what actually stops the recursion if the env flag is ever lost.
    if not _has_console():
        return False
    return _script() is not None


def _has_console() -> bool:
    try:
        return bool(ctypes.WinDLL("kernel32", use_last_error=True).GetConsoleWindow())
    except Exception:  # pragma: no cover - not Windows, or no kernel32
        return False


def _script() -> Path | None:
    """The file we were started from, absolute. `None` if that is not a file.

    Guards the `python -c ...` and interactive cases, where there is no script
    to hand to a second interpreter and re-launching would start the daemon
    with an argument list that means nothing.
    """
    if not sys.argv or not sys.argv[0]:
        return None
    path = Path(sys.argv[0]).resolve()
    return path if path.is_file() else None


def _windowless_interpreter() -> Path | None:
    """`pythonw.exe` for the interpreter running us, if it ships one.

    Named off the running executable rather than hard-coded, so a virtualenv
    hands over to *its* interpreter and not to whichever Python is first on the
    PATH — that one would not have the packages.
    """
    exe = Path(sys.executable)
    if exe.stem.lower().endswith("w"):
        return None  # already windowless
    # `python3.exe` ships no `python3w.exe`, so fall back to the plain name in
    # the same directory before giving up on it.
    for name in (f"{exe.stem}w{exe.suffix}", f"pythonw{exe.suffix}"):
        candidate = exe.with_name(name)
        if candidate.is_file():
            return candidate
    return None


def hand_off() -> bool:
    """Start a windowless copy and report whether this process should stop.

    Called before anything opens the log or takes the single-instance mutex:
    two processes holding those, even for the moment it takes this one to
    return, is two processes to clean up after.
    """
    if not wanted():
        return False

    script = _script()
    interpreter = _windowless_interpreter()
    if script is None:
        return False
    if interpreter is None:
        # No `pythonw` to hand to — an unusual install, but the console is
        # still a kill switch, so cut it loose where it stands.
        log.warning("No windowless interpreter beside %s", sys.executable)
        return not _detach_console()

    command = [str(interpreter), str(script), *sys.argv[1:]]
    environment = dict(os.environ, **{RELAUNCHED: "1"})
    for flags in (_DETACHED | subprocess.CREATE_BREAKAWAY_FROM_JOB, _DETACHED):
        try:
            subprocess.Popen(
                command,
                env=environment,
                cwd=os.getcwd(),
                close_fds=True,
                # Detached means there is nothing behind these handles. Left
                # inherited they would point at a console that is about to go.
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except OSError as exc:
            # Breaking out of a job object is refused when the job forbids it,
            # which is how some terminals and IDEs keep their children on a
            # leash. Worth asking for — it is what stops the daemon being
            # killed with the terminal — but not worth failing over.
            log.debug("Relaunch with flags %#x failed: %s", flags, exc)
            continue
        log.info("Handed over to %s", interpreter)
        return True

    log.warning("Could not start a windowless copy; carrying on with the console")
    return False


def _detach_console() -> bool:
    """Cut this process loose from its console. True if it worked.

    The fallback, not the plan: the window has already appeared by the time we
    get here. But detaching means closing it no longer sends this process a
    close event, and if the console was opened *for* us — a double-clicked
    script — it has nobody left attached and goes away on its own.
    """
    try:
        if not ctypes.WinDLL("kernel32", use_last_error=True).FreeConsole():
            return False
    except Exception:
        log.debug("FreeConsole failed", exc_info=True)
        return False
    # The standard streams now point at handles that are gone, and writing to
    # one raises rather than being ignored. `None` is what `pythonw` hands a
    # program, and everything here already copes with that.
    sys.stdin = sys.stdout = sys.stderr = None  # type: ignore[assignment]
    return True
