"""User settings, persisted and live.

Every entry here changes something real at runtime — there are no decorative
switches. `Settings.changed` fires on every write so the UI can re-theme, retime
or re-render without a restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .store import read_json, write_json

log = logging.getLogger(__name__)

SETTINGS_FILE = "settings.json"


@dataclass(frozen=True)
class Option:
    key: str
    label: str
    help: str
    kind: str  # "bool" | "choice"
    default: Any
    #: (stored value, shown label) pairs, for kind == "choice".
    choices: tuple[tuple[Any, str], ...] = ()
    #: Settings that only matter while another is on, e.g. the effect toggles.
    depends_on: str = ""


SCHEMA: tuple[Option, ...] = (
    Option(
        "accent", "Accent colour", "Recolours the entire interface.",
        "choice", "#39d353",
        (
            ("#39d353", "phosphor green"),
            ("#4da6ff", "signal blue"),
            ("#ff7b39", "amber"),
            ("#c678dd", "magenta"),
            ("#e6e8ee", "white"),
        ),
    ),
    Option(
        "motion", "Animation speed", "Scales every transition in the app.",
        "choice", 0.5,
        ((0.25, "instant"), (0.5, "quick"), (1.0, "relaxed"), (1.6, "cinematic")),
    ),
    Option(
        "frame_cap", "Frame rate", "Higher costs more CPU while the overlay is up.",
        "choice", 0,
        ((0, "match display"), (60, "60 fps"), (120, "120 fps"), (240, "240 fps")),
    ),
    Option(
        "effects", "Background effects", "The drifting grid, radar and scanline.",
        "bool", True,
    ),
    Option(
        "grid", "  Grid", "Drifting reference grid.", "bool", True, depends_on="effects"
    ),
    Option(
        "rings", "  Radar rings", "Pulses outward from the dial.",
        "bool", True, depends_on="effects",
    ),
    Option(
        "scanline", "  Scanline", "A soft band sweeping down the screen.",
        "bool", True, depends_on="effects",
    ),
    Option(
        "glow", "  Dial glow", "Halo behind the centre dial.",
        "bool", True, depends_on="effects",
    ),
    Option(
        "hide_on_blur", "Close prompt on focus loss",
        "Hide the prompt as soon as you click elsewhere.", "bool", True,
    ),
    Option(
        "autostart", "Start with Windows", "Adds Sentinel to the user Run key.",
        "bool", False,
    ),
)

_BY_KEY = {option.key: option for option in SCHEMA}


class Settings:
    """Loaded once, mutated in place, written on every change."""

    def __init__(self) -> None:
        self._values: dict[str, Any] = {o.key: o.default for o in SCHEMA}
        stored = read_json(SETTINGS_FILE, {})
        if isinstance(stored, dict):
            for key, value in stored.items():
                if key in _BY_KEY and self._valid(_BY_KEY[key], value):
                    self._values[key] = value
        #: Callables run after any change. Plain list rather than a Qt signal so
        #: this module stays importable without a QApplication.
        self.listeners: list[Any] = []

    @staticmethod
    def _valid(option: Option, value: Any) -> bool:
        if option.kind == "bool":
            return isinstance(value, bool)
        return any(value == choice for choice, _ in option.choices)

    def get(self, key: str) -> Any:
        return self._values[key]

    def label_for(self, key: str) -> str:
        option = _BY_KEY[key]
        if option.kind == "bool":
            return "on" if self._values[key] else "off"
        for value, label in option.choices:
            if value == self._values[key]:
                return label
        return str(self._values[key])

    def enabled(self, key: str) -> bool:
        """False when a parent toggle is off, so the row can grey out."""
        parent = _BY_KEY[key].depends_on
        return not parent or bool(self._values[parent])

    def set(self, key: str, value: Any) -> None:
        if self._values.get(key) == value:
            return
        self._values[key] = value
        write_json(SETTINGS_FILE, self._values)
        for listener in list(self.listeners):
            try:
                listener(key, value)
            except Exception:
                log.exception("Settings listener failed for %s", key)

    def cycle(self, key: str, direction: int = 1) -> None:
        """Advance a choice, or flip a bool."""
        option = _BY_KEY[key]
        if option.kind == "bool":
            self.set(key, not self._values[key])
            return
        values = [value for value, _ in option.choices]
        index = values.index(self._values[key]) if self._values[key] in values else 0
        self.set(key, values[(index + direction) % len(values)])


#: One instance for the process; the UI and the daemon share it.
settings = Settings()
