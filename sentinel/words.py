

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import APP_NAME, APP_VERSION

log = logging.getLogger(__name__)

#: How long to wait before giving up. Short on purpose: the answer is a
#: convenience, and a prompt that sits there for half a minute has already
#: failed you whatever it eventually prints.
TIMEOUT = 8.0

#: Wikipedia rejects the stock urllib agent outright, and the others are within
#: their rights to. Identifying the caller is the polite minimum.
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (personal command launcher)"

DATAMUSE_URL = "https://api.datamuse.com/words"
WIKI_SEARCH_URL = "https://en.wikipedia.org/w/api.php"
WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"

#: Answers already paid for. Typing `define quiet` and then `syn quiet` is the
#: normal way to use this, and the second one should not cost another round
#: trip. Bounded because the daemon is resident for weeks at a time.
_CACHE: dict[str, object] = {}
_CACHE_LIMIT = 128


class WordError(Exception):
    """A lookup that could not be completed — no network, or a bad answer.

    Distinct from "nothing is filed under that word", which is not an error but
    a result, and comes back empty.
    """


# ------------------------------------------------------------------ fetching


def _get(url: str, params: dict[str, str] | None = None) -> object:
    """A JSON document, or `WordError` explaining which way it went wrong.

    A 404 is not an exception here. Wikipedia says "no such page" with a status
    code, and a caller that has to catch an exception to learn something so
    ordinary reads worse than one that gets `None`.
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    if url in _CACHE:
        return _CACHE[url]

    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            payload = None
        else:
            raise WordError(f"the source answered {exc.code}") from exc
    except urllib.error.URLError as exc:
        # The overwhelmingly common cause is being offline, and saying so is
        # more use than relaying a socket message nobody reads.
        log.info("Lookup failed for %s: %s", url, exc.reason)
        raise WordError("no answer — check the network") from exc
    except TimeoutError as exc:
        raise WordError(f"timed out after {TIMEOUT:.0f}s") from exc
    except (ValueError, OSError) as exc:
        raise WordError(f"unreadable answer: {exc}") from exc

    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()  # cheap and rare; an LRU here would be ceremony
    _CACHE[url] = payload
    return payload


def forget() -> int:
    """Drop the cache, so the next lookup really asks. Returns what was held."""
    count = len(_CACHE)
    _CACHE.clear()
    return count


def _rows(params: dict[str, str]) -> list[dict]:
    """A Datamuse result list, with anything unexpected filtered out."""
    payload = _get(DATAMUSE_URL, params)
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict) and row.get("word")]


def _cap(limit: int) -> str:
    """Datamuse takes `max` between 1 and 1000; keep well inside it."""
    return str(max(1, min(100, limit)))


# --------------------------------------------------------------- definitions

#: Datamuse labels a sense with the abbreviation; a reader wants the word.
PARTS = {
    "n": "noun",
    "v": "verb",
    "adj": "adjective",
    "adv": "adverb",
    "prop": "proper noun",
    "u": "",  # the source's own "unknown", which is not worth printing
}

#: How a frequency figure — occurrences per million words — reads in English.
_BANDS = ((10.0, "very common"), (1.0, "common"), (0.1, "uncommon"), (0.0, "rare"))

#: Wiktionary files misspellings as words in their own right, defined as
#: "Misspelling of X". That is exactly what `spell` needs to know and exactly
#: what would otherwise make `spell recieve` answer "yes, that is a word".
_MISSPELLING = re.compile(
    r"\b(?:common |alternative |obsolete |eye )?misspelling of ([\w'-]+)", re.I
)


@dataclass(frozen=True)
class Sense:
    #: "noun", "verb", "adjective" — expanded from the source's abbreviation.
    part: str
    text: str


@dataclass(frozen=True)
class Entry:
    word: str
    senses: tuple[Sense, ...]
    #: Every part of speech the word can be, whether or not a sense uses it.
    parts: tuple[str, ...] = ()
    syllables: int = 0
    #: Occurrences per million words of running text. 0.0 when not reported.
    frequency: float = 0.0

    @property
    def misspelling(self) -> str:
        """The word this is a misspelling of, or "" if it is a word itself."""
        for sense in self.senses:
            found = _MISSPELLING.search(sense.text)
            if found:
                return found.group(1)
        return ""

    @property
    def rarity(self) -> str:
        for floor, label in _BANDS:
            if self.frequency >= floor:
                return label
        return "rare"


def _entry(row: dict) -> Entry:
    """One Datamuse row with `md=dpsf` asked for, as an Entry."""
    parts: list[str] = []
    frequency = 0.0
    for tag in row.get("tags") or []:
        if not isinstance(tag, str):
            continue
        if tag.startswith("f:"):
            try:
                frequency = float(tag[2:])
            except ValueError:
                pass
        elif tag in PARTS and PARTS[tag] and PARTS[tag] not in parts:
            parts.append(PARTS[tag])

    senses: list[Sense] = []
    for definition in row.get("defs") or []:
        if not isinstance(definition, str):
            continue
        # "adj\tWith little or no sound." — the tab is the whole format.
        abbreviation, _, text = definition.partition("\t")
        if not text:
            abbreviation, text = "", abbreviation
        text = text.strip()
        if text:
            senses.append(Sense(PARTS.get(abbreviation, abbreviation), text))

    return Entry(
        str(row["word"]),
        tuple(senses),
        tuple(parts),
        int(row.get("numSyllables") or 0),
        frequency,
    )


def define(word: str, near: int = 8) -> tuple[Entry | None, tuple[str, ...]]:
    """What `word` means, and the real words nearest to it.

    Both in one call, because they answer the same question from either side:
    if the exact word is there you want its senses, and if it is not you want
    to know what you should have typed. Asking twice would double the latency
    of the only case that is already a miss.

    The entry is `None` unless the source came back with that *exact* word —
    Datamuse's spelling index answers `wasapi` with `wasabi`, and quietly
    printing the definition of a word nobody asked about is worse than a miss.
    """
    term = " ".join(word.split())
    if not term:
        return None, ()
    rows = _rows({"sp": term, "md": "dpsf", "max": _cap(near)})
    if not rows:
        return None, ()
    exact = None
    if str(rows[0]["word"]).lower() == term.lower():
        exact = _entry(rows[0])
    others = tuple(str(row["word"]) for row in rows if str(row["word"]).lower() != term.lower())
    return exact, others


# -------------------------------------------------------------- associations

#: Datamuse relation codes, by the name the commands use them under.
SYNONYM = "rel_syn"
ANTONYM = "rel_ant"
RHYME = "rel_rhy"
NEAR_RHYME = "rel_nry"
MEANS_LIKE = "ml"
TRIGGERS = "rel_trg"


def related(word: str, relation: str, limit: int = 24) -> tuple[str, ...]:
    """Words standing in `relation` to `word`, strongest association first."""
    term = " ".join(word.split())
    if not term:
        return ()
    found = [str(row["word"]) for row in _rows({relation: term, "max": _cap(limit)})]
    # Datamuse will happily return the word you asked about, which is never
    # what you wanted to read back.
    return tuple(w for w in found if w.lower() != term.lower())


def synonyms(word: str, limit: int = 24) -> tuple[str, ...]:
    """The thesaurus entry.

    Falls back to "means like" when nothing is filed as a strict synonym.
    That is looser — it will return phrases and near misses — but an answer you
    have to weigh beats a blank, and the strict list is empty more often than
    you would expect.
    """
    found = related(word, SYNONYM, limit)
    return found or related(word, MEANS_LIKE, limit)


def antonyms(word: str, limit: int = 24) -> tuple[str, ...]:
    """The opposites. Genuinely sparse — plenty of words simply have none."""
    return related(word, ANTONYM, limit)


def rhymes(word: str, limit: int = 30) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Perfect rhymes and near rhymes, kept apart because the difference matters."""
    perfect = related(word, RHYME, limit)
    seen = {w.lower() for w in perfect}
    near = tuple(w for w in related(word, NEAR_RHYME, limit) if w.lower() not in seen)
    return perfect, near


def describes(description: str, limit: int = 16) -> tuple[str, ...]:
    """A reverse dictionary: the word you meant, from what it means.

    `ml` is a semantic match rather than a lookup, so this works on a phrase —
    "fear of heights", "the smell after rain" — which is the case no ordinary
    dictionary can help with at all.
    """
    phrase = " ".join(description.split())
    if not phrase:
        return ()
    return tuple(str(row["word"]) for row in _rows({"ml": phrase, "max": _cap(limit)}))


def spellings(pattern: str, limit: int = 16) -> tuple[str, ...]:
    """Words matching a spelling pattern — `?` for one letter, `*` for any run."""
    term = pattern.strip()
    if not term:
        return ()
    return tuple(str(row["word"]) for row in _rows({"sp": term, "max": _cap(limit)}))


# ------------------------------------------------------------------ articles


@dataclass(frozen=True)
class Article:
    title: str
    #: The one-line "American physicist" descriptor, when there is one.
    description: str
    extract: str
    url: str
    #: The other titles the search offered. Only worth reading when the page
    #: turned out to be a disambiguation — then they are the actual answer.
    alternatives: tuple[str, ...] = ()
    #: Wikipedia's own "did you mean one of these?" page. Its extract is a
    #: sentence about the *name*, which is never what was being asked.
    ambiguous: bool = False


#: How many titles the search is asked for. One would do for the common case,
#: but a disambiguation hit is only useful if the alternatives came back with
#: it — and they cost nothing, since it is the same request either way.
_WIKI_CANDIDATES = 6


def article(topic: str) -> Article | None:
    """Wikipedia's opening paragraph on `topic`, or `None` if it has no page.

    Searched rather than guessed at. The summary endpoint wants an exact title,
    and `wiki heisenberg uncertainty` is not one — asking the search index first
    costs a round trip and turns near misses into hits.
    """
    query = " ".join(topic.split())
    if not query:
        return None

    found = _get(WIKI_SEARCH_URL, {
        "action": "opensearch",
        "search": query,
        "limit": str(_WIKI_CANDIDATES),
        "namespace": "0",
        "format": "json",
    })
    titles: list[str] = []
    if isinstance(found, list) and len(found) > 1 and isinstance(found[1], list):
        titles = [str(t) for t in found[1]]
    title = titles[0] if titles else query

    payload = _get(WIKI_SUMMARY_URL.format(urllib.parse.quote(title.replace(" ", "_"))))
    if not isinstance(payload, dict) or not payload.get("extract"):
        return None
    urls = payload.get("content_urls")
    page = ""
    if isinstance(urls, dict) and isinstance(urls.get("desktop"), dict):
        page = str(urls["desktop"].get("page") or "")
    return Article(
        str(payload.get("title") or title),
        str(payload.get("description") or ""),
        str(payload["extract"]).strip(),
        page,
        tuple(titles[1:]),
        str(payload.get("type") or "") == "disambiguation",
    )
