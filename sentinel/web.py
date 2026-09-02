"""Turning what you typed into somewhere to go.

One prompt, two intents. `search claude api pricing` is a question; `claude.ai`
is an address. Telling them apart is the whole job of this module, and it has to
be *predictable* — a rule that guesses differently depending on the day is worse
than one that occasionally makes you type `https://`.

The rule: a scheme, a `www.` prefix, a bare host with a recognised suffix, or
localhost/an IP is an address. Everything else is a query. That is deliberately
conservative about bare hosts — `search config.py` and `search 3.5 mm jack`
should look things up, not try to open a website.
"""

from __future__ import annotations

import logging
import re
import webbrowser
from urllib.parse import quote_plus

log = logging.getLogger(__name__)

#: Where a query goes when it is not an address.
SEARCH_URL = "https://www.google.com/search?q={}"

#: Handed to the browser exactly as typed.
SCHEMES = ("http://", "https://", "ftp://", "ftps://", "file://", "about:")

#: A bare host only reads as an address when its last label is one of these.
#: An allowlist rather than "any letters" precisely so a filename with an
#: extension stays a search — the failure mode of the looser rule is silently
#: opening a browser when you asked a question.
SUFFIXES = frozenset("""
com org net int edu gov mil io dev app ai co uk us ca de fr jp ru cn au nz
nl se no dk fi it es pt pl cz gr ie il kr mx tr za br in ch be at eu
me tv gg xyz info biz site online store tech cloud page news blog wiki
live life world space fun club shop art design studio dog cat sh so cc im
is fm re to ly st ms id ee lt lv si sk hr rs ua by kz hu ro bg
""".split())

_HOST = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+([a-z]{2,24})(?::\d{1,5})?(?:[/?#].*)?$",
    re.I,
)
_LOCAL = re.compile(
    r"^(?:localhost|\d{1,3}(?:\.\d{1,3}){3})(?::\d{1,5})?(?:[/?#].*)?$", re.I
)


def looks_like_url(text: str) -> bool:
    """Whether `text` names somewhere to go rather than something to look up."""
    raw = text.strip()
    if not raw:
        return False
    if raw.lower().startswith(SCHEMES):
        return True
    # Past this point an address is a single word: `foo.com bar` is a question
    # about two things, not a page.
    if any(ch.isspace() for ch in raw):
        return False
    if raw.lower().startswith("www."):
        return True
    if _LOCAL.match(raw):
        return True
    match = _HOST.match(raw)
    return bool(match) and match.group(1).lower() in SUFFIXES


def normalise(url: str) -> str:
    """An address as typed, given the scheme it left out.

    https everywhere except a machine-local address, where it is nearly always
    a dev server that has never been given a certificate — defaulting those to
    https means a browser warning instead of the page.
    """
    raw = url.strip()
    if raw.lower().startswith(SCHEMES):
        return raw
    scheme = "http" if _LOCAL.match(raw) else "https"
    return f"{scheme}://{raw}"


def search_url(query: str) -> str:
    return SEARCH_URL.format(quote_plus(query.strip()))


def resolve(text: str) -> tuple[str, bool]:
    """The URL to open, and whether it came from an address rather than a query."""
    if looks_like_url(text):
        return normalise(text), True
    return search_url(text), False


def open_url(url: str) -> bool:
    """Hand a URL to the default browser."""
    try:
        if webbrowser.open(url, new=2):
            log.info("Opened %s", url)
            return True
    except Exception:
        log.exception("webbrowser could not open %s", url)
    # webbrowser returns False when it cannot find a browser to drive; the
    # shell knows how to open an http:// URL even when Python does not.
    try:
        import os

        os.startfile(url)  # noqa: S606 - Windows shell open, by design
        log.info("Opened %s via the shell", url)
        return True
    except OSError:
        log.exception("Could not open %s", url)
        return False
