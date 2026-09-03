"""Shared motion vocabulary: the speed dial and the CRT flicker.

Nothing in Sentinel should simply appear. A window that pops into existence
reads as a different application; a window that stutters on reads as the same
piece of hardware waking up. Anything that cannot be given a real transition
gets flickered instead.

Both patterns are deliberately uneven. A smooth fade reads as a modern app
easing in; an erratic stutter reads as a tube striking, which is the point.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QElapsedTimer,
    QObject,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QGuiApplication

from ..config import settings

#: One dial for every animation in the app. Lower is faster. The baselines it
#: multiplies are the durations that felt right unscaled. Settings can change
#: it at runtime, so durations are computed per use rather than at import.
MOTION_SCALE = 0.5


def set_motion_scale(value: float) -> None:
    global MOTION_SCALE
    MOTION_SCALE = max(0.05, min(4.0, float(value)))


def ms(baseline: int) -> int:
    """Scale a tuned duration by the global speed dial."""
    return max(1, int(baseline * MOTION_SCALE))


def _follow_settings(key: str, value: object) -> None:
    if key == "motion":
        set_motion_scale(float(value))  # type: ignore[arg-type]


# Same reasoning as the palette: the speed dial belongs to this module, not to
# whichever widget was constructed first.
set_motion_scale(settings.get("motion"))
settings.listeners.append(_follow_settings)


# ------------------------------------------------------------------- chasing
# The other half of the vocabulary. A flicker is for something arriving or
# leaving; this is for a value that has moved and has to be *followed* — a list
# that grew, a selection that stepped down a row, a card that has more in it
# than it did a moment ago. Nothing in a live interface should teleport.

#: How much of the remaining gap an eased value closes per second. Tuned to
#: land in roughly a fifth of a second without overshoot.
CHASE_RATE = 14.0

#: Below this the gap is closed outright, so a chase actually terminates
#: instead of approaching its target forever in ever-smaller steps.
CHASE_SNAP = 0.35


def chase(
    current: float, target: float, delta: float,
    rate: float = CHASE_RATE, snap: float = CHASE_SNAP,
) -> float:
    """Move `current` toward `target`, a fixed fraction of the gap per second.

    Frame-rate independent: `delta` is real elapsed seconds, so the same motion
    plays out over the same wall-clock time at 60fps and at 144. The speed dial
    applies here as it does to every other animation — a lower scale means
    faster, so it divides the rate.
    """
    if abs(target - current) < snap:
        return target
    step = min(1.0, delta * (rate / max(0.05, MOTION_SCALE)))
    return current + (target - current) * step


def settled(current: float, target: float) -> bool:
    return abs(target - current) < CHASE_SNAP


#: Longest step any animation is advanced by in one frame. A stall should make
#: a move skip ahead, not replay itself in slow motion once the panel catches up.
MAX_STEP = 0.1


def step_seconds(nanos: int) -> float:
    """Nanoseconds since the last frame, as a sane number of seconds.

    Clamped at both ends. The ceiling absorbs a stall; the floor matters
    because an un-started QElapsedTimer reports nonsense, and a *negative*
    delta would drive every chase in the app backwards away from its target.
    """
    return max(0.0, min(MAX_STEP, nanos / 1e9))


def frame_interval(screen=None) -> int:
    """Milliseconds per frame, from the setting or the actual display."""
    cap = int(settings.get("frame_cap"))
    if cap <= 0:
        screen = screen or QGuiApplication.primaryScreen()
        rate = screen.refreshRate() if screen is not None else 60.0
        cap = int(round(rate)) if rate and rate > 1.0 else 60
    return max(1, int(round(1000.0 / max(24, min(cap, 360)))))


class Ticker(QObject):
    """A frame clock, reporting real elapsed seconds.

    Shared so both windows animate off one implementation and one frame-rate
    setting. Anything chasing a target needs to be stepped every frame, and a
    resident daemon must not burn a timer while nothing is on screen — so this
    is started and stopped with the window that owns it.
    """

    #: Seconds since the previous frame, clamped by `step_seconds`.
    tick = Signal(float)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._elapsed = QElapsedTimer()
        self._timer = QTimer(self)
        # Precise, not coarse: Qt's default rounds to ~15ms on Windows, which
        # would cap every animation at 60fps however fast the display is.
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._fire)
        settings.listeners.append(self._on_setting_changed)

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def start(self, screen=None) -> None:
        if self._timer.isActive():
            return
        self._elapsed.restart()
        self._timer.setInterval(frame_interval(screen))
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _fire(self) -> None:
        nanos = self._elapsed.nsecsElapsed()
        self._elapsed.restart()
        self.tick.emit(step_seconds(nanos))

    def _on_setting_changed(self, key: str, value: object) -> None:
        if key == "frame_cap" and self._timer.isActive():
            self._timer.setInterval(frame_interval())


#: Striking on: dark, a couple of false starts, then lit. False starts belong
#: here — a tube that catches, drops and catches again is what striking on
#: looks like, and it is read as the thing arriving.
ON_PATTERN = (0.0, 0.9, 0.1, 1.0, 0.25, 0.85, 0.55, 1.0)
#: Dying out: lit, stuttering, then dark — but never *brighter* than the step
#: before. Going out and coming back is not read as a tube dying; on the way
#: out it is read as the window having failed to repaint, which is precisely
#: what it looked like. The steps stay uneven, so the stutter survives.
OFF_PATTERN = (1.0, 0.92, 0.34, 0.30, 0.11, 0.09, 0.02, 0.0)

#: Baseline for a flicker; scaled through `ms()` each time one runs.
FLICKER_BASE = 380


def _sample(pattern: tuple[float, ...], progress: float) -> float:
    if progress <= 0.0:
        return pattern[0]
    if progress >= 1.0:
        return pattern[-1]
    return pattern[min(int(progress * len(pattern)), len(pattern) - 1)]


def flicker_on(progress: float) -> float:
    """0 -> 1, stuttering. Ends fully lit."""
    return _sample(ON_PATTERN, progress)


def flicker_off(progress: float) -> float:
    """1 -> 0, stuttering. Ends fully dark.

    The decay term guarantees it reaches zero however the pattern is edited.
    """
    if progress >= 1.0:
        return 0.0
    return _sample(OFF_PATTERN, progress) * (1.0 - progress)


class Flicker(QObject):
    """Drives an alpha setter through the flicker waveform.

    Deliberately agnostic about *what* it fades — a window's opacity, a graphics
    effect, a painted alpha — so the same stutter can be reused anywhere.
    """

    finished = Signal()

    def __init__(
        self,
        apply_alpha: Callable[[float], None],
        parent: QObject | None = None,
        baseline: int = FLICKER_BASE,
    ) -> None:
        super().__init__(parent)
        self._apply = apply_alpha
        self._progress = 0.0
        self._lighting = True
        self._baseline = baseline

        self._anim = QPropertyAnimation(self, b"progress", self)
        # Linear: the pattern supplies the character, an easing curve on top
        # would only smear the steps into each other.
        self._anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.Linear))
        self._anim.finished.connect(self._settle)

    # The animation drives this; the setter maps it through the waveform.
    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, value: float) -> None:
        self._progress = value
        self._apply(flicker_on(value) if self._lighting else flicker_off(value))

    progress = Property(float, _get_progress, _set_progress)

    @property
    def closing(self) -> bool:
        return not self._lighting and self._anim.state() == QPropertyAnimation.State.Running

    def light(self) -> None:
        """Stutter on, ending fully visible."""
        self._anim.stop()
        self._lighting = True
        self._apply(0.0)
        self._start()

    def douse(self) -> None:
        """Stutter off. `finished` fires once it is fully dark."""
        self._anim.stop()
        self._lighting = False
        self._start()

    def stop(self) -> None:
        """Abandon a flicker mid-way, without settling or reporting it.

        For when whatever was being faded is about to go away: settling would
        write one last alpha to something that is no longer there.
        """
        self._anim.stop()

    def _start(self) -> None:
        # Read the duration now, so a change to the speed setting takes effect
        # on the very next flicker rather than after a restart.
        self._anim.setDuration(ms(self._baseline))
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()

    def _settle(self) -> None:
        self._apply(1.0 if self._lighting else 0.0)
        self.finished.emit()
