"""Finding and launching installed applications.

Scans the Start Menu rather than the registry: the Start Menu is what the user
actually sees, it already excludes uninstallers and update helpers, and the
shortcut name is the name they know the program by.

The walk touches a few thousand files, so it runs on a worker thread and caches
its result. The UI never blocks on it — it renders whatever the index has and
refreshes when the scan lands.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .store import read_json, write_json

log = logging.getLogger(__name__)

CACHE_FILE = "apps.json"
#: Rescan if the cache is older than this; installs are not that frequent.
CACHE_TTL = 6 * 60 * 60

_SUFFIXES = frozenset({".lnk", ".url", ".appref-ms"})
#: Shortcuts that are noise in a launcher.
_NOISE = (
    "uninstall", "readme", "release notes", "documentation", "help",
    "website", "changelog", "license", "eula", "repair", "modify",
)


@dataclass(frozen=True)
class App:
    name: str
    path: str
    source: str

    @property
    def sort_key(self) -> str:
        return self.name.lower()


def _roots() -> list[tuple[Path, str, frozenset[str]]]:
    """(directory, source label, accepted suffixes), in precedence order."""
    candidates = [
        (os.environ.get("APPDATA"), "Microsoft/Windows/Start Menu/Programs",
         "user", _SUFFIXES),
        (os.environ.get("PROGRAMDATA"), "Microsoft/Windows/Start Menu/Programs",
         "system", _SUFFIXES),
        # Store and inbox apps — notepad, terminal, winget — never appear in
        # the Start Menu folders. They are execution-alias stubs here.
        (os.environ.get("LOCALAPPDATA"), "Microsoft/WindowsApps",
         "store", frozenset({".exe"})),
    ]
    roots = []
    for base, tail, source, suffixes in candidates:
        if base:
            path = Path(base) / tail
            if path.is_dir():
                roots.append((path, source, suffixes))
    return roots


def scan() -> list[App]:
    """Walk the app directories. Blocking — call this off the UI thread."""
    found: dict[str, App] = {}
    for root, source, suffixes in _roots():
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                for filename in filenames:
                    if Path(filename).suffix.lower() not in suffixes:
                        continue
                    name = Path(filename).stem
                    lowered = name.lower()
                    if any(token in lowered for token in _NOISE):
                        continue
                    # First writer wins, and the roots are walked in precedence
                    # order, so a user-installed copy shadows the others.
                    found.setdefault(
                        lowered, App(name, str(Path(dirpath) / filename), source)
                    )
        except OSError:
            log.exception("Could not walk %s", root)
    return sorted(found.values(), key=lambda a: a.sort_key)


def score(app: App, query: str) -> int:
    """Rank a match. Higher is better; 0 means no match at all.

    Prefix beats word-start beats substring beats subsequence, so typing "ex"
    puts Excel above Adobe Experience.
    """
    if not query:
        return 1
    name = app.name.lower()
    needle = query.lower().strip()
    if not needle:
        return 1
    if name.startswith(needle):
        return 1000 - len(name)
    if any(word.startswith(needle) for word in name.replace("-", " ").split()):
        return 800 - len(name)
    if needle in name:
        return 600 - len(name)
    # Subsequence: every character in order, anywhere.
    position = 0
    for char in needle:
        position = name.find(char, position) + 1
        if position == 0:
            return 0
    return 300 - len(name)


def launch(app: App) -> bool:
    try:
        os.startfile(app.path)  # noqa: S606 - Windows shell open, by design
        log.info("Launched %s", app.name)
        return True
    except OSError:
        log.exception("Could not launch %s (%s)", app.name, app.path)
        return False


class AppIndex:
    """The app list, plus the background scan that fills it."""

    def __init__(self) -> None:
        self._apps: list[App] = []
        self._lock = threading.Lock()
        self._scanning = False
        self.scanned_at = 0.0
        #: Called on the worker thread when a scan finishes.
        self.on_loaded: list = []
        self._load_cache()

    # ------------------------------------------------------------- accessors

    @property
    def apps(self) -> list[App]:
        with self._lock:
            return self._apps

    @property
    def scanning(self) -> bool:
        return self._scanning

    @property
    def stale(self) -> bool:
        return time.time() - self.scanned_at > CACHE_TTL

    def search(self, query: str, limit: int = 200) -> list[App]:
        apps = self.apps
        if not query.strip():
            return apps[:limit]
        ranked = [(score(app, query), app) for app in apps]
        hits = [(s, a) for s, a in ranked if s > 0]
        hits.sort(key=lambda pair: (-pair[0], pair[1].sort_key))
        return [app for _, app in hits[:limit]]

    # ---------------------------------------------------------------- scanning

    def _load_cache(self) -> None:
        cached = read_json(CACHE_FILE, None)
        if not isinstance(cached, dict):
            return
        rows = cached.get("apps", [])
        if not isinstance(rows, list):
            return
        apps = [
            App(r["name"], r["path"], r.get("source", "system"))
            for r in rows
            if isinstance(r, dict) and "name" in r and "path" in r
        ]
        with self._lock:
            self._apps = apps
        self.scanned_at = float(cached.get("scanned_at", 0.0))

    def _save_cache(self, apps: list[App]) -> None:
        write_json(
            CACHE_FILE,
            {
                "scanned_at": self.scanned_at,
                "apps": [
                    {"name": a.name, "path": a.path, "source": a.source} for a in apps
                ],
            },
        )

    def refresh(self, force: bool = False) -> None:
        """Kick off a scan unless one is already running."""
        if self._scanning or (not force and not self.stale and self._apps):
            return
        self._scanning = True
        threading.Thread(target=self._work, name="app-scan", daemon=True).start()

    def _work(self) -> None:
        try:
            apps = scan()
            with self._lock:
                self._apps = apps
            self.scanned_at = time.time()
            self._save_cache(apps)
            log.info("Indexed %d applications", len(apps))
        except Exception:
            log.exception("Application scan failed")
        finally:
            self._scanning = False
            for callback in list(self.on_loaded):
                try:
                    callback()
                except Exception:
                    log.exception("App index callback failed")


index = AppIndex()
