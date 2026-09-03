

from __future__ import annotations

import logging
import textwrap
from dataclasses import dataclass
from typing import Callable, Protocol

from . import APP_NAME, APP_VERSION
from . import calc, desktop, system, web, words
from . import windows as win
from .apps import index as app_index
from .apps import launch as launch_app
from .config import SCHEMA, settings
from .notes import CATEGORIES, NOTE, TODO, Entry, notebook, parse_due
from .timers import human, timers
from .transcript import transcript

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ parsing

QUOTES = "\"'"


@dataclass(frozen=True)
class Token:
    text: str
    start: int
    end: int
    quoted: bool = False


def tokenize(line: str) -> list[Token]:
    """Split on whitespace, keeping quoted runs together.

    Positions are indices into the original string so a highlighter can colour
    the exact characters. An unterminated quote still yields a token — you are
    mid-typing, and the text should colour as you go.
    """
    tokens: list[Token] = []
    i, length = 0, len(line)
    while i < length:
        while i < length and line[i].isspace():
            i += 1
        if i >= length:
            break
        start = i
        if line[i] in QUOTES:
            quote = line[i]
            i += 1
            body_start = i
            while i < length and line[i] != quote:
                i += 1
            text = line[body_start:i]
            if i < length:
                i += 1  # closing quote
            tokens.append(Token(text, start, i, quoted=True))
        else:
            while i < length and not line[i].isspace():
                i += 1
            tokens.append(Token(line[start:i], start, i))
    return tokens


# -------------------------------------------------------------- completions


@dataclass(frozen=True)
class Suggestion:
    #: Text that replaces the token being typed. Empty means reference-only:
    #: shown to explain the syntax, but Tab will not insert it.
    insert: str
    label: str
    detail: str = ""
    kind: str = "command"

    @property
    def insertable(self) -> bool:
        return bool(self.insert)


def quote(text: str) -> str:
    """Wrap a value in quotes when it would otherwise tokenize as two."""
    if not text or any(ch.isspace() for ch in text):
        return f'"{text}"'
    return text


def _position(line: str) -> tuple[list[Token], int, str]:
    """Which token is being typed, and what has been typed of it so far."""
    tokens = tokenize(line)
    fresh = (not line) or line[-1].isspace()
    if fresh:
        return tokens, len(tokens), ""
    return tokens, len(tokens) - 1, tokens[-1].text


def complete(line: str) -> list[Suggestion]:
    """What could come next, given a partly typed line."""
    tokens, index, prefix = _position(line)
    if index <= 0:
        return _complete_name(prefix)
    command = lookup(tokens[0].text)
    if command is None:
        return []
    return _complete_argument(command, [t.text for t in tokens[1:]], index - 1, prefix)


def apply_completion(line: str, suggestion: Suggestion) -> str:
    """Insert a suggestion in place of the token being typed."""
    if not suggestion.insertable:
        return line
    tokens, _index, prefix = _position(line)
    if not prefix:
        return line + suggestion.insert + " "
    return line[: tokens[-1].start] + suggestion.insert + " "


def _complete_name(prefix: str) -> list[Suggestion]:
    needle = prefix.lower()
    exact, alias, loose = [], [], []
    for command in _ORDER:
        entry = Suggestion(
            command.name, command.name,
            command.argument_hint or command.summary, "command",
        )
        if command.name.startswith(needle):
            exact.append(entry)
        elif any(a.startswith(needle) for a in command.aliases):
            hit = next(a for a in command.aliases if a.startswith(needle))
            alias.append(Suggestion(command.name, command.name,
                                    f"= {hit}   {entry.detail}", "command"))
        elif needle and needle in command.name:
            loose.append(entry)
    return exact + alias + loose


def _literals(command: Command, position: int) -> list[Suggestion]:
    """Fixed words the syntax allows at this argument position."""
    out: dict[str, Suggestion] = {}
    for syntax, description in command.forms:
        words = syntax.split()
        # A form starts with the command's own name. Anything else is a legend
        # row explaining a placeholder — `<when> =`, `functions =` — and the
        # words in it are prose, not things you could type here.
        if not words or words[0] != command.name:
            continue
        if len(words) <= position + 1:
            continue
        word = words[position + 1]
        if word.startswith(("<", "[")):
            continue
        for option in word.split("|"):
            out.setdefault(option, Suggestion(option, option, description, "argument"))
    return list(out.values())


def _complete_argument(
    command: Command, args: list[str], position: int, prefix: str
) -> list[Suggestion]:
    provider = _PROVIDERS.get(command.name)
    items = provider(args, position, prefix) if provider else _literals(command, position)

    needle = prefix.lower()
    if needle:
        items = [s for s in items if s.label.lower().startswith(needle)] or [
            s for s in items if needle in s.label.lower()
        ]
    if items:
        return items

    # Nothing to insert — show the syntax itself, so the shape of the argument
    # is still in front of you rather than left to memory.
    return [
        Suggestion("", syntax, description, "syntax") for syntax, description in command.forms
    ]


def _launch_values(args: list[str], position: int, prefix: str) -> list[Suggestion]:
    query = " ".join(args[position:]) or prefix
    return [
        Suggestion(quote(app.name), app.name, app.source, "value")
        for app in app_index.search(query, limit=12)
    ]


def _mute_values(args: list[str], position: int, prefix: str) -> list[Suggestion]:
    if position == 0:
        return [
            Suggestion(quote(name), name, "playing now", "value")
            for name in system.sessions()
        ]
    return [
        Suggestion("on", "on", "force muted", "argument"),
        Suggestion("off", "off", "force unmuted", "argument"),
    ]


def _help_values(args: list[str], position: int, prefix: str) -> list[Suggestion]:
    return [
        Suggestion(c.name, c.name, c.summary, "value") for c in _ORDER
    ] if position == 0 else []


def _open_values(args: list[str], position: int, prefix: str) -> list[Suggestion]:
    if position != 0:
        return []
    from .tree import build_tree

    # Groups and the leaves inside them. `open` drills to a node at any depth,
    # so offering only the groups hid every leaf — including `open settings`,
    # which this command registers as one of its own examples.
    root = build_tree()
    seen: set[str] = set()
    offered: list[Suggestion] = []
    for group in root.children:
        for node in (group, *group.children):
            if node.name in seen:
                continue
            seen.add(node.name)
            offered.append(Suggestion(node.name, node.name, node.summary, "value"))
    return offered


#: Commands whose arguments are real things on this machine rather than a
#: fixed vocabulary.
_PROVIDERS: dict[str, Callable[[list[str], int, str], list[Suggestion]]] = {}


# ----------------------------------------------------------------- registry


@dataclass
class Result:
    ok: bool
    message: str = ""
    #: Output that does not fit on one line — a listing, a table, a help page.
    #: Both command lines grow to show it rather than truncating.
    lines: tuple[str, ...] = ()
    #: Ask the daemon to get out of the way — used after launching something,
    #: so the window that just opened receives the focus instead of us.
    close_ui: bool = False
    #: Keep this out of the scroll-back. Only `clear` wants it: recording the
    #: command that empties the history would leave a row behind.
    quiet: bool = False
    #: The rest of the work, when it is too slow to do here. Commands run on
    #: the thread that draws, so anything that blocks on a socket would freeze
    #: the overlay mid-keystroke. A command that needs the network returns its
    #: "looking it up…" line now and this callable for the answer; the daemon
    #: runs it on a worker and replaces the row with whatever it returns.
    pending: Callable[[], "Result"] | None = None
    #: Text for the clipboard, put there by the daemon rather than the command.
    #: A `pending` callable runs on a worker, and QClipboard is GUI-thread-only
    #: — a command that finishes off-thread must ask for the copy through here
    #: instead of calling `desktop.set_clipboard` itself.
    copy: str = ""

    @property
    def output(self) -> tuple[str, ...]:
        """Everything to show, as display rows: the summary, then the body."""
        if not self.message:
            return self.lines
        return (self.message, *self.lines)


class Host(Protocol):
    """What a command may ask of the running daemon.

    Kept as a protocol so this module never imports the UI: the registry has to
    be importable by the syntax highlighter, and a cycle there would be fatal.
    """

    def open_navigator(self, then: str = "") -> None: ...

    def close_ui(self) -> None: ...

    def shutdown(self) -> None: ...


_host: Host | None = None


def set_host(host: Host) -> None:
    global _host
    _host = host


@dataclass
class Command:
    name: str
    usage: str
    summary: str
    run: Callable[[list[str]], Result]
    #: Every accepted form, as (syntax, what it does). This is the contract the
    #: parser implements — the UI reads it rather than repeating it in prose,
    #: so help cannot drift from behaviour.
    forms: tuple[tuple[str, str], ...] = ()
    #: Lines you could type verbatim.
    examples: tuple[str, ...] = ()
    #: Extra names that dispatch here and highlight as valid.
    aliases: tuple[str, ...] = ()
    #: False when the backend it needs is not installed.
    requires: str = ""
    #: Which branch of the navigator this appears under. The tree is built from
    #: these, so a command shows up in the interface by being registered — there
    #: is no second list to keep in step, and no way for one to drift.
    group: str = "sentinel"
    #: Asks for confirmation before it will do anything, because it cannot be
    #: undone. The word the user has to add is the value.
    confirm: str = ""

    @property
    def available(self) -> bool:
        if self.requires == "mixer":
            return system.mixer_available()
        return True

    @property
    def argument_hint(self) -> str:
        """The usage line without the command name — what goes after it."""
        return self.usage[len(self.name):].strip()


REGISTRY: dict[str, Command] = {}
_ORDER: list[Command] = []


def register(command: Command) -> Command:
    REGISTRY[command.name] = command
    for alias in command.aliases:
        REGISTRY[alias] = command
    _ORDER.append(command)
    return command


def commands(group: str = "") -> list[Command]:
    """Registered commands in declaration order, without alias duplicates."""
    if not group:
        return list(_ORDER)
    return [command for command in _ORDER if command.group == group]


def groups() -> list[str]:
    """Every group that has a command in it, in the order they first appear.

    The navigator's branches come from this, so adding a command to a new group
    is all it takes to grow the interface a branch.
    """
    seen: list[str] = []
    for command in _ORDER:
        if command.group not in seen:
            seen.append(command.group)
    return seen


def is_command(word: str) -> bool:
    return word.strip().lower() in REGISTRY


def lookup(word: str) -> Command | None:
    return REGISTRY.get(word.strip().lower())


def run(line: str) -> Result:
    tokens = tokenize(line)
    if not tokens:
        return Result(False, "")
    command = lookup(tokens[0].text)
    if command is None:
        return Result(False, f"unknown command: {tokens[0].text}")
    args = [token.text for token in tokens[1:]]
    try:
        return command.run(args)
    except Exception as exc:  # a bad plugin must not take the daemon down
        log.exception("Command %r failed", line)
        return Result(False, f"{command.name} failed: {exc}")


# ------------------------------------------------------------- the commands

MIXER_HINT = "needs pycaw — pip install -r requirements.txt"


#: Settings by key, for the commands that front them.
_BY_SETTING = {option.key: option for option in SCHEMA}


def _size(count: float) -> str:
    """Bytes as a person reads them, to one decimal place."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(count) < 1024.0 or unit == "TB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024.0
    return f"{count:.1f} PB"


def _bar(fraction: float, width: int = 18) -> str:
    """A meter drawn in text, so a readout is scannable at a glance."""
    filled = max(0, min(width, int(round(fraction * width))))
    return "█" * filled + "·" * (width - filled)


def _table(rows: list[tuple[str, str]]) -> tuple[str, ...]:
    """Label/value pairs with the labels padded to a common width."""
    if not rows:
        return ()
    width = max(len(label) for label, _ in rows)
    return tuple(f"{label:<{width}}   {value}" for label, value in rows)


#: Where prose gets broken. Both command lines *elide* rather than wrap, so a
#: sentence longer than this does not spill onto a second row — it vanishes off
#: the right edge. Anything printing English has to break it up itself.
WRAP = 76


def _wrap(text: str, first: str = "", rest: str = "") -> tuple[str, ...]:
    """A paragraph as display rows, `first` on the opening line and `rest` on
    the others — which is what makes a numbered definition hang correctly."""
    body = " ".join(text.split())
    if not body:
        return ()
    return tuple(textwrap.wrap(
        body, width=WRAP, initial_indent=first, subsequent_indent=rest or " " * len(first)
    ))


def _listing(items: tuple[str, ...], label: str = "  ") -> tuple[str, ...]:
    """A comma-separated run of words, broken across as many rows as it needs.

    `label` heads the first row only — the continuations line up underneath it,
    so `did you mean:` is said once rather than once per row.
    """
    if not items:
        return ()
    return _wrap(", ".join(items), label)


def _volume(args: list[str]) -> Result:
    if not args:
        current = system.master_volume()
        if current is None:
            return Result(False, f"volume: reading the level {MIXER_HINT}")
        return Result(True, f"volume is {current}%")

    word = args[0].lower()
    if word in ("up", "+"):
        steps = int(args[1]) if len(args) > 1 and args[1].isdigit() else 5
        system.tap(system.VK_VOLUME_UP, steps)
        return Result(True, f"volume up {steps * system.VOLUME_STEP_PERCENT}%")
    if word in ("down", "-"):
        steps = int(args[1]) if len(args) > 1 and args[1].isdigit() else 5
        system.tap(system.VK_VOLUME_DOWN, steps)
        return Result(True, f"volume down {steps * system.VOLUME_STEP_PERCENT}%")
    if word in ("mute", "toggle"):
        system.tap(system.VK_VOLUME_MUTE)
        return Result(True, "toggled system mute")
    if word.rstrip("%").isdigit():
        target = int(word.rstrip("%"))
        if system.set_master_volume(target):
            return Result(True, f"volume set to {target}%")
        return Result(False, f"volume {target}: {MIXER_HINT}")
    return Result(False, "usage: volume [up|down|mute|<0-100>]")


def _mute(args: list[str]) -> Result:
    if not args:
        system.tap(system.VK_VOLUME_MUTE)
        return Result(True, "toggled system mute")
    if not system.mixer_available():
        return Result(False, f"mute {args[0]}: {MIXER_HINT}")
    state = None
    target = args[0]
    if len(args) > 1:
        state = args[1].lower() in ("on", "yes", "true", "1")
    result = system.mute_app(target, state)
    if result is None:
        open_apps = ", ".join(system.sessions()) or "none"
        return Result(False, f"no audio session for {target!r}. playing: {open_apps}")
    return Result(True, f"{'muted' if result else 'unmuted'} {target}")


def _launch(args: list[str]) -> Result:
    if not args:
        # Bare `launch` opens the browser rather than erroring at you.
        return _open(["launch"])
    query = " ".join(args)
    hits = app_index.search(query, limit=1)
    if not hits:
        if app_index.scanning:
            return Result(False, "still indexing applications — try again shortly")
        return Result(False, f"no application matching {query!r}")
    if launch_app(hits[0]):
        # Stand down so the window that is about to appear gets the focus.
        return Result(True, f"launched {hits[0].name}", close_ui=True)
    return Result(False, f"could not launch {hits[0].name}")


def _search(args: list[str]) -> Result:
    """One box for both intents: an address goes there, anything else is asked.

    Which one you meant is decided by `web.looks_like_url`, not by a flag —
    typing `github.com` and typing `how do i rebase` should both just work.
    """
    query = " ".join(args).strip()
    if not query:
        return Result(False, "usage: search <query|url>")
    url, direct = web.resolve(query)
    if not web.open_url(url):
        return Result(False, f"could not open {url}")
    # Stand down: the browser window is about to want the foreground.
    verb = "opening" if direct else "searching for"
    return Result(True, f"{verb} {query if direct else query!r}", close_ui=True)


# ------------------------------------------------------------ the interface
# These drive Sentinel's own windows. They live in the registry like anything
# else, so the prompt colours them green — handling them off to one side is
# exactly why they used to show up red.


def _open(args: list[str]) -> Result:
    if _host is None:
        return Result(False, "no window to open")
    target = args[0].lower() if args else ""
    _host.open_navigator(target)
    return Result(True, f"opening {target}" if target else "")


def _close(args: list[str]) -> Result:
    if _host is None:
        return Result(False, "nothing to close")
    _host.close_ui()
    return Result(True, "")


def _media(vk: int, label: str) -> Callable[[list[str]], Result]:
    def action(args: list[str]) -> Result:
        system.tap(vk)
        return Result(True, label)

    return action


def _spotify(args: list[str]) -> Result:
    word = args[0].lower() if args else "play"
    table = {
        "next": (system.VK_MEDIA_NEXT, "next track"),
        "skip": (system.VK_MEDIA_NEXT, "next track"),
        "prev": (system.VK_MEDIA_PREV, "previous track"),
        "previous": (system.VK_MEDIA_PREV, "previous track"),
        "back": (system.VK_MEDIA_PREV, "previous track"),
        "play": (system.VK_MEDIA_PLAY_PAUSE, "play/pause"),
        "pause": (system.VK_MEDIA_PLAY_PAUSE, "play/pause"),
        "stop": (system.VK_MEDIA_STOP, "stop"),
    }
    if word not in table:
        return Result(False, "usage: spotify [play|pause|next|prev|stop]")
    vk, label = table[word]
    system.tap(vk)
    return Result(True, label)


# ------------------------------------------------------------------- tools


def _calc(args: list[str]) -> Result:
    expression = " ".join(args)
    if not expression.strip():
        return Result(False, "usage: calc <expression>")
    try:
        value = calc.evaluate(expression)
    except calc.CalcError as exc:
        return Result(False, f"calc: {exc}")
    answer = calc.render(value)
    desktop.set_clipboard(answer)
    return Result(True, f"{expression.strip()} = {answer}   (copied)")


def _roll(args: list[str]) -> Result:
    """Dice in the usual notation: 2d6, d20, 3d8+2."""
    import random
    import re

    spec = ("".join(args) or "d6").lower().replace(" ", "")
    match = re.fullmatch(r"(\d*)d(\d+)([+-]\d+)?", spec)
    if not match:
        return Result(False, "usage: roll [<n>]d<sides>[+/-<modifier>]  e.g. 2d6+1")
    count = int(match.group(1) or 1)
    sides = int(match.group(2))
    modifier = int(match.group(3) or 0)
    if not 1 <= count <= 100 or not 2 <= sides <= 1000:
        return Result(False, "roll at most 100 dice of at most 1000 sides")
    throws = [random.randint(1, sides) for _ in range(count)]
    total = sum(throws) + modifier
    if count == 1 and not modifier:
        return Result(True, f"{spec}: {total}")
    detail = " + ".join(str(t) for t in throws)
    if modifier:
        detail += f" {'+' if modifier > 0 else '-'} {abs(modifier)}"
    return Result(True, f"{spec}: {total}", (detail,))


def _uuid(args: list[str]) -> Result:
    import uuid as _uuid_module

    value = str(_uuid_module.uuid4())
    desktop.set_clipboard(value)
    return Result(True, f"{value}   (copied)")


def _hash(args: list[str]) -> Result:
    import hashlib

    words = list(args)
    algorithms = ("md5", "sha1", "sha256", "sha512")
    algorithm = "sha256"
    if words and words[0].lower() in algorithms:
        algorithm = words.pop(0).lower()
    text = " ".join(words)
    if not text:
        return Result(False, f"usage: hash [{'|'.join(algorithms)}] <text>")
    digest = hashlib.new(algorithm, text.encode("utf-8")).hexdigest()
    desktop.set_clipboard(digest)
    return Result(True, f"{algorithm} of {len(text)} chars   (copied)", (digest,))


def _time(args: list[str]) -> Result:
    from datetime import datetime

    now = datetime.now()
    return Result(True, now.strftime("%H:%M:%S"), _table([
        ("date", now.strftime("%A %d %B %Y")),
        ("week", f"{now.isocalendar().week:02d} of the year, day {now.timetuple().tm_yday}"),
        ("uptime", human(system.uptime())),
    ]))


def _clip(args: list[str]) -> Result:
    if not args:
        held = desktop.clipboard_text()
        if not held:
            return Result(True, "the clipboard is empty")
        lines = held.splitlines()
        head = tuple(line for line in lines[:8])
        summary = f"{len(held)} chars, {len(lines)} line(s)"
        return Result(True, summary, head + (("…",) if len(lines) > 8 else ()))
    text = " ".join(args)
    if not desktop.set_clipboard(text):
        return Result(False, "could not reach the clipboard")
    return Result(True, f"copied {len(text)} chars")


def _shot(args: list[str]) -> Result:
    path = desktop.screenshot()
    if path is None:
        return Result(False, "could not take a screenshot")
    desktop.set_clipboard(str(path))
    return Result(True, f"saved {path.name}", (str(path.parent),))


def _folder(args: list[str]) -> Result:
    name = (args[0] if args else "home").lower()
    path = desktop.folder(name)
    if path is None:
        known = ", ".join([*desktop.FOLDERS, "appdata", "temp"])
        return Result(False, f"no folder {name!r}. try: {known}")
    if not desktop.reveal(path):
        return Result(False, f"could not open {path}")
    return Result(True, f"opening {path}", close_ui=True)


def _lock(args: list[str]) -> Result:
    if system.lock():
        return Result(True, "locking", close_ui=True)
    return Result(False, "could not lock the workstation")


def _guarded(
    action: Callable[[], bool], word: str, doing: str, name: str
) -> Callable[[list[str]], Result]:
    """A command you have to mean.

    Every one of these ends the session one way or another, and they sit one
    keystroke from a prompt that is always listening — so none of them fires on
    a bare word. The confirmation is part of the command rather than a dialog:
    a dialog you would learn to dismiss without reading.
    """

    def run_it(args: list[str]) -> Result:
        if not args or args[0].lower() != word:
            return Result(False, f"say '{name} {word}' to confirm — this {doing}")
        if not action():
            return Result(False, f"could not {name}")
        return Result(True, f"{doing}…", close_ui=True)

    return run_it


_sleep = _guarded(system.sleep, "now", "suspends the machine", "sleep")
_hibernate = _guarded(system.hibernate, "now", "suspends to disk", "hibernate")
_restart = _guarded(system.restart, "now", "reboots the machine", "restart")
_shutdown = _guarded(system.shutdown, "now", "turns the machine off", "shutdown")
_signout = _guarded(system.sign_out, "now", "ends your session", "signout")


# ------------------------------------------------------------------- lookup
# The only commands that leave this machine to answer you. Every one of them is
# in two halves: the part that runs at the prompt, which validates the argument
# and returns immediately, and the part that talks to the network, which the
# daemon runs on a worker and swaps into the same row when it comes back. See
# `Result.pending`. Nothing below the `_slow` line may be called on the UI
# thread — it will block for as long as the socket takes.


def _slow(word: str, doing: str, work: Callable[[], Result]) -> Result:
    """The holding line, and the real work to do behind it.

    The placeholder says what it is doing rather than just "…", because the
    common failure is being offline and a row that reads `looking up "quiet"`
    for six seconds is already telling you where the time is going.
    """
    return Result(True, f"{doing} {word!r}…", pending=work)


#: How many senses a definition prints before it stops. The scroll-back is
#: fourteen rows; a word with thirty senses would push everything else you were
#: doing off the top of it to tell you what `set` means.
SENSE_LIMIT = 6


def _define(args: list[str]) -> Result:
    word = " ".join(args).strip()
    if not word:
        return Result(False, "usage: define <word>")
    return _slow(word, "looking up", lambda: _defined(word))


def _defined(word: str) -> Result:
    try:
        entry, near = words.define(word)
    except words.WordError as exc:
        return Result(False, f"define {word}: {exc}")
    if entry is None:
        return Result(False, f"no entry for {word!r}", _suggest(near))
    if not entry.senses:
        # In the index but undefined — a proper noun, usually. Say which,
        # rather than printing a headword with nothing under it.
        return Result(True, f"{_headword(entry)} — no definition filed", _suggest(near))

    # Gathered by part of speech rather than printed in source order, which
    # interleaves them: the eye should be able to find "the verb sense" without
    # reading the prose, and it cannot if `adjective` heads two separate blocks.
    grouped: dict[str, list[str]] = {}
    for sense in entry.senses[:SENSE_LIMIT]:
        grouped.setdefault(sense.part, []).append(sense.text)

    lines: list[str] = []
    number = 0
    for part, texts in grouped.items():
        if part:
            lines.append(part)
        for text in texts:
            number += 1
            lines.extend(_wrap(text, f"{number:>2}. ", "    "))
    held = len(entry.senses)
    if held > SENSE_LIMIT:
        lines.append(f"    … and {held - SENSE_LIMIT} more sense(s)")
    return Result(True, _headword(entry), tuple(lines))


def _headword(entry: words.Entry) -> str:
    """The word itself and what the source knows about it, on one line."""
    facts = list(entry.parts)
    if entry.syllables:
        facts.append(f"{entry.syllables} syllable{'s' if entry.syllables != 1 else ''}")
    facts.append(entry.rarity)
    return f"{entry.word}  ({' · '.join(facts)})"


def _suggest(near: tuple[str, ...]) -> tuple[str, ...]:
    """Spelling suggestions, when there are any worth offering."""
    return _listing(near[:8], "did you mean: ") if near else ()


def _near(word: str) -> tuple[str, ...]:
    """Suggestions for a word nothing was filed under.

    Best-effort by design: this is consolation for an answer that already came
    back empty, so a second failure just means no consolation.
    """
    try:
        _entry, near = words.define(word)
    except words.WordError:
        return ()
    return _suggest(near)


def _thesaurus(
    name: str, doing: str, fetch: Callable[[str], tuple[str, ...]], empty: str
) -> Callable[[list[str]], Result]:
    """`synonyms` and `antonyms` are one command with two relations."""

    def start(args: list[str]) -> Result:
        word = " ".join(args).strip()
        if not word:
            return Result(False, f"usage: {name} <word>")
        return _slow(word, doing, lambda: finish(word))

    def finish(word: str) -> Result:
        try:
            found = fetch(word)
        except words.WordError as exc:
            return Result(False, f"{name} {word}: {exc}")
        if not found:
            return Result(True, f"{empty} for {word!r}", _near(word))
        # Copied, because the reason you asked is almost always to use one.
        return Result(True, f"{len(found)} for {word!r}   (copied)", _listing(found),
                      copy=", ".join(found))

    return start


_synonyms = _thesaurus("synonyms", "finding synonyms for", words.synonyms, "no synonyms")
_antonyms = _thesaurus("antonyms", "finding antonyms for", words.antonyms, "no antonyms")


def _rhyme(args: list[str]) -> Result:
    word = " ".join(args).strip()
    if not word:
        return Result(False, "usage: rhyme <word>")
    return _slow(word, "rhyming", lambda: _rhymed(word))


def _rhymed(word: str) -> Result:
    try:
        perfect, near = words.rhymes(word)
    except words.WordError as exc:
        return Result(False, f"rhyme {word}: {exc}")
    if not perfect and not near:
        return Result(True, f"nothing rhymes with {word!r}", _near(word))
    lines: list[str] = []
    if perfect:
        lines.append("perfect")
        lines.extend(_listing(perfect))
    if near:
        lines.append("near")
        lines.extend(_listing(near))
    return Result(True, f"{len(perfect)} perfect, {len(near)} near", tuple(lines))


def _spell(args: list[str]) -> Result:
    pattern = " ".join(args).strip()
    if not pattern:
        return Result(False, "usage: spell <word>   (? = one letter, * = any run)")
    return _slow(pattern, "checking", lambda: _spelled(pattern))


def _spelled(pattern: str) -> Result:
    # A wildcard is a pattern to fill, not a spelling to check. Different
    # question, different answer — and no sense in either case is a real one.
    if "?" in pattern or "*" in pattern:
        try:
            found = words.spellings(pattern, limit=16)
        except words.WordError as exc:
            return Result(False, f"spell {pattern}: {exc}")
        if not found:
            return Result(True, f"nothing matches {pattern!r}")
        return Result(True, f"{len(found)} matching {pattern!r}", _listing(found))

    try:
        entry, near = words.define(pattern)
    except words.WordError as exc:
        return Result(False, f"spell {pattern}: {exc}")
    if entry is None or not entry.senses:
        # Being in the spelling index proves nothing: it holds names, initials
        # and typos. Having a definition is what makes it a word.
        return Result(False, f"{pattern!r} is not a word", _suggest(near))
    # Misspellings *are* defined, as "misspelling of X" — which is the source
    # telling you the answer, so long as it is read rather than counted.
    wrong = entry.misspelling
    if wrong:
        return Result(False, f"{pattern} is a misspelling of {wrong}", _listing(near, "near: "))
    return Result(True, f"{pattern} is spelled correctly ({entry.rarity})",
                  _listing(near, "near: "))


def _word(args: list[str]) -> Result:
    description = " ".join(args).strip()
    if not description:
        return Result(False, "usage: word <description>   e.g. word fear of heights")
    return _slow(description, "finding a word for", lambda: _worded(description))


def _worded(description: str) -> Result:
    try:
        found = words.describes(description)
    except words.WordError as exc:
        return Result(False, f"word: {exc}")
    if not found:
        return Result(True, f"no word for {description!r}")
    # The best match leads on its own line: this command is asked in the hope
    # of exactly one answer, and burying it in a list would be a worse one.
    return Result(True, f"{found[0]}   (copied)", _listing(found[1:], "also: "),
                  copy=found[0])


def _wiki(args: list[str]) -> Result:
    topic = " ".join(args).strip()
    if not topic:
        return Result(False, "usage: wiki <topic>")
    return _slow(topic, "reading up on", lambda: _wikied(topic))


def _wikied(topic: str) -> Result:
    try:
        found = words.article(topic)
    except words.WordError as exc:
        return Result(False, f"wiki {topic}: {exc}")
    if found is None:
        return Result(False, f"no article for {topic!r}")
    if found.ambiguous:
        # A disambiguation page's extract is a sentence about the *name*, which
        # answers nothing. The list of what the name could mean does.
        lines = list(_listing(found.alternatives, "  "))
        if found.url:
            lines.append(found.url)
        return Result(True, f"{found.title} — several articles share the name", tuple(lines))
    head = found.title
    if found.description:
        head += f" — {found.description}"
    lines = list(_wrap(found.extract))
    if found.url:
        lines.append(found.url)
    return Result(True, head, tuple(lines))


#: Titles are trimmed to this in listings so the date column stays lined up.
TITLE_W = 44


def _row(index: int, entry: Entry) -> str:
    """One notebook entry as a fixed-width line.

    Notes have no state to show, so they get a bullet where a todo gets its box
    — the shape of the line tells you which kind you are looking at before you
    have read a word of it.
    """
    mark = "·" if entry.kind == NOTE else ("[x]" if entry.done else "[ ]")
    title = entry.title if len(entry.title) <= TITLE_W else entry.title[: TITLE_W - 1] + "…"
    when = f"due {entry.due_label}" if entry.due else entry.created_label
    return f"{index:>2}. {mark:<3} {title:<{TITLE_W}}  {when}"


def _browse(args: list[str]) -> Result:
    """Everything captured, printed at the prompt rather than in a panel."""
    kinds = {"todo": TODO, "todos": TODO, "note": NOTE, "notes": NOTE}
    kind = None
    if args:
        kind = kinds.get(args[0].lower())
        if kind is None:
            return Result(False, "usage: browse [todos|notes]")
    entries = notebook.all(kind)
    if not entries:
        return Result(True, "nothing captured yet")
    open_count = sum(1 for e in entries if e.kind == TODO and not e.done)
    return Result(
        True,
        f"{len(entries)} captured, {open_count} open",
        tuple(_row(i + 1, e) for i, e in enumerate(entries)),
    )


def _clear(args: list[str]) -> Result:
    """Empty the scroll-back — never the notebook."""
    log.debug("Cleared %d transcript entries", transcript.clear())
    # Quiet, and with nothing to say: the point is a blank prompt, so a
    # "cleared" line sitting where the history was would defeat it.
    return Result(True, "", quiet=True)


def _todo(args: list[str]) -> Result:
    if not args:
        items = [e for e in notebook.all(TODO) if not e.done]
        if not items:
            return Result(True, "nothing open")
        return Result(
            True,
            f"{len(items)} open",
            tuple(_row(i + 1, e) for i, e in enumerate(items)),
        )
    if args[0].lower() == "done" and len(args) > 1 and args[1].isdigit():
        items = [e for e in notebook.all(TODO) if not e.done]
        position = int(args[1]) - 1
        if not 0 <= position < len(items):
            return Result(False, f"no open item {args[1]}")
        notebook.toggle(items[position])
        return Result(True, f"done: {items[position].title}")
    entry = notebook.add(Entry(title=" ".join(args), kind=TODO))
    return Result(True, f"captured: {entry.title}")


def _note(args: list[str]) -> Result:
    if not args:
        entries = notebook.all(NOTE)
        return Result(True, f"{len(entries)} note(s) — see /notes/browse")
    entry = notebook.add(Entry(title=" ".join(args), kind=NOTE))
    return Result(True, f"noted: {entry.title}")


def _remind(args: list[str]) -> Result:
    if not args:
        return Result(True, timers.status())
    words = list(args)
    if words and words[0].lower() == "me":
        words.pop(0)
    if words and words[0].lower() == "in":
        words.pop(0)
    if not words:
        return Result(False, "usage: remind [me] in <20m|2h|17:30> to <text>")

    when = parse_due(words[0])
    if when is None:
        return Result(False, f"'{words[0]}' is not a time I understand")
    rest = words[1:]
    if rest and rest[0].lower() == "to":
        rest.pop(0)
    text = " ".join(rest) or "reminder"

    import time as _time

    seconds = max(1.0, when - _time.time())
    timers.remind(seconds, text)
    return Result(True, f"reminding you in {human(seconds)}: {text}")


def _pomodoro(args: list[str]) -> Result:
    word = args[0].lower() if args else "status"
    if word in ("start", "go"):
        minutes = int(args[1]) if len(args) > 1 and args[1].isdigit() else 25
        timers.start_pomodoro(minutes)
        return Result(True, f"pomodoro started — {minutes} minutes")
    if word == "stop":
        return Result(timers.stop_pomodoro(), "pomodoro stopped")
    return Result(True, timers.pomodoro_state)


def _open_todos() -> list[Entry]:
    return [entry for entry in notebook.all(TODO) if not entry.done]


def _at(items: list[Entry], token: str) -> tuple[Entry | None, str]:
    """The entry a numbered argument refers to, or why it does not exist."""
    if not token.isdigit():
        return None, f"{token!r} is not an item number"
    position = int(token) - 1
    if not 0 <= position < len(items):
        return None, f"no item {token} — there are {len(items)}"
    return items[position], ""


def _done(args: list[str]) -> Result:
    """Close a todo — or reopen one, which `todo done` could never do."""
    items = _open_todos() if not (args and args[0].lower() == "undo") else [
        entry for entry in notebook.all(TODO) if entry.done
    ]
    reopening = bool(args) and args[0].lower() == "undo"
    rest = args[1:] if reopening else args
    if not rest:
        return Result(False, "usage: done <n>   ·   done undo <n>")
    entry, problem = _at(items, rest[0])
    if entry is None:
        return Result(False, problem)
    notebook.toggle(entry)
    return Result(True, f"{'reopened' if reopening else 'done'}: {entry.title}")


def _drop(args: list[str]) -> Result:
    entries = notebook.all()
    if not args:
        return Result(False, "usage: drop <n>   (numbers come from `browse`)")
    entry, problem = _at(entries, args[0])
    if entry is None:
        return Result(False, problem)
    notebook.remove(entry)
    return Result(True, f"dropped: {entry.title}")


def _due(args: list[str]) -> Result:
    items = _open_todos()
    if len(args) < 2:
        return Result(False, "usage: due <n> <when|none>")
    entry, problem = _at(items, args[0])
    if entry is None:
        return Result(False, problem)
    when = " ".join(args[1:])
    if when.lower() in ("none", "clear", "never"):
        entry.due = None
        notebook.save()
        return Result(True, f"cleared the due date on {entry.title}")
    stamp = parse_due(when)
    if stamp is None:
        return Result(False, f"'{when}' is not a time I understand")
    entry.due = stamp
    notebook.save()
    return Result(True, f"{entry.title} is due {entry.due_label}")


def _grep(args: list[str]) -> Result:
    """Search what you have captured — the counterpart to `search` for the web."""
    needle = " ".join(args).lower().strip()
    if not needle:
        return Result(False, "usage: grep <text>")
    hits = [
        (index, entry)
        for index, entry in enumerate(notebook.all(), 1)
        if needle in entry.title.lower() or needle in entry.body.lower()
    ]
    if not hits:
        return Result(True, f"nothing captured mentions {needle!r}")
    return Result(
        True,
        f"{len(hits)} match(es) for {needle!r}",
        tuple(_row(index, entry) for index, entry in hits),
    )


def _tag(args: list[str]) -> Result:
    items = notebook.all()
    if len(args) < 2:
        return Result(False, f"usage: tag <n> <{'|'.join(CATEGORIES)}>")
    entry, problem = _at(items, args[0])
    if entry is None:
        return Result(False, problem)
    category = args[1].lower()
    if category not in CATEGORIES:
        return Result(False, f"tag it one of: {', '.join(CATEGORIES)}")
    entry.category = category
    notebook.save()
    return Result(True, f"{entry.title} is now {category}")


# ------------------------------------------------------------ sentinel itself


def _echo(args: list[str]) -> Result:
    return Result(True, " ".join(args) or "")


def _reload(args: list[str]) -> Result:
    app_index.refresh(force=True)
    # The word cache goes with it: `reload` is the one command that means
    # "forget what you think you know", and it is the only way to make a
    # lookup ask the network again rather than repeat itself.
    held = words.forget()
    detail = f", {held} cached lookup(s) dropped" if held else ""
    return Result(True, f"re-indexing applications…{detail}")


def _log(args: list[str]) -> Result:
    from .logging_setup import log_file

    path = log_file()
    if not path.exists():
        return Result(False, f"no log at {path}")
    if not desktop.reveal(path):
        return Result(False, f"could not open {path}")
    return Result(True, f"opening {path.name}", close_ui=True)


def _apply_setting(key: str, word: str) -> Result:
    """Take one word and make it mean something for one setting.

    Shared by the friendly front-ends (`accent`, `motion`) and by the generic
    `config`, so all of them accept the same vocabulary: a value, a label, a
    prefix of a label, or next/prev to step through.
    """
    option = _BY_SETTING.get(key)
    if option is None:
        return Result(False, f"no setting called {key!r}")
    if not word:
        return Result(True, f"{option.label.lower()} is {settings.label_for(key)}")
    if word in ("next", "cycle", "+", "prev", "back", "-", "toggle"):
        settings.cycle(key, -1 if word in ("prev", "back", "-") else 1)
        return Result(True, f"{option.label.lower()} is {settings.label_for(key)}")
    if option.kind == "bool":
        if word in ("on", "yes", "true", "1", "enable"):
            settings.set(key, True)
        elif word in ("off", "no", "false", "0", "disable"):
            settings.set(key, False)
        else:
            return Result(False, f"{option.label.lower()}: on or off")
        return Result(True, f"{option.label.lower()} is {settings.label_for(key)}")
    for value, name in option.choices:
        if word in (str(value).lower(), name.lower()) or name.lower().startswith(word):
            settings.set(key, value)
            return Result(True, f"{option.label.lower()} is {settings.label_for(key)}")
    offered = ", ".join(name for _, name in option.choices)
    return Result(False, f"{option.label.lower()}: pick one of {offered}")


def _setting(key: str) -> Callable[[list[str]], Result]:
    def action(args: list[str]) -> Result:
        return _apply_setting(key, " ".join(args).lower().strip())

    return action


def _config(args: list[str]) -> Result:
    """Every setting, and a way to change any of them without leaving the prompt."""
    if not args:
        return Result(
            True,
            f"{len(SCHEMA)} settings",
            _table([(option.key, settings.label_for(option.key)) for option in SCHEMA]),
        )
    key = args[0].lower()
    if key not in _BY_SETTING:
        return Result(False, f"no setting {key!r} — try `config` for the list")
    return _apply_setting(key, " ".join(args[1:]).lower().strip())


def _quit(args: list[str]) -> Result:
    if _host is None:
        return Result(False, "no daemon to stop")
    if not args or args[0].lower() != "now":
        return Result(False, "say 'quit now' — this stops Sentinel entirely")
    _host.shutdown()
    return Result(True, "shutting down")


def _help(args: list[str]) -> Result:
    """The registry, printed. Every line here is read off a Command, so help
    cannot drift from what the parser actually accepts."""
    if args:
        command = lookup(args[0])
        if command is None:
            return Result(False, f"unknown command: {args[0]}")
        width = max((len(syntax) for syntax, _ in command.forms), default=0)
        lines = [f"{syntax:<{width}}   {what}" for syntax, what in command.forms]
        if not lines:
            lines = [command.usage]
        if command.aliases:
            lines.append("also: " + ", ".join(command.aliases))
        if command.examples:
            lines.append("e.g.  " + "   ".join(command.examples))
        if command.requires and not command.available:
            lines.append(f"unavailable: {command.requires} backend is not installed")
        return Result(True, f"{command.name} — {command.summary}", tuple(lines))

    width = max(len(c.name) for c in commands())
    lines: list[str] = []
    for group in groups():
        lines.append(f"/{group}")
        lines.extend(
            f"  {c.name:<{width}}  {c.summary}" for c in commands(group)
        )
    return Result(True, f"{len(commands())} commands in {len(groups())} groups",
                  tuple(lines))


def _about(args: list[str]) -> Result:
    open_count, total = notebook.counts()
    return Result(
        True,
        f"{APP_NAME} {APP_VERSION}",
        _table([
            ("commands", str(len(commands()))),
            ("applications", f"{len(app_index.apps)} indexed"),
            ("todos", f"{open_count} open of {total}"),
            ("pomodoro", timers.pomodoro_state),
            ("uptime", human(system.uptime())),
        ]),
    )


# ------------------------------------------------------------ the machine
# Straight readouts. Each is one call to `system`, so they stay instant enough
# to type without thinking about it.


def _uptime(args: list[str]) -> Result:
    return Result(True, f"up {human(system.uptime())}")


def _memory(args: list[str]) -> Result:
    reading = system.memory()
    if reading is None:
        return Result(False, "could not read the memory counters")
    used, total, percent = reading
    return Result(True, f"memory {percent}%  {_bar(percent / 100.0)}", _table([
        ("used", _size(used)),
        ("free", _size(total - used)),
        ("total", _size(total)),
    ]))


def _disk(args: list[str]) -> Result:
    drive = (args[0] if args else "C").rstrip(":\\/").upper() + ":\\"
    reading = system.disk(drive)
    if reading is None:
        return Result(False, f"no drive {drive}")
    free, total = reading
    if total <= 0:
        return Result(False, f"{drive} reports no size")
    used = total - free
    return Result(
        True,
        f"{drive[:2]} {used / total:.0%} full  {_bar(used / total)}",
        _table([
            ("used", _size(used)),
            ("free", _size(free)),
            ("total", _size(total)),
        ]),
    )


def _battery(args: list[str]) -> Result:
    reading = system.battery()
    if reading is None:
        return Result(True, "no battery — this machine runs on mains")
    percent, mains, remaining = reading
    state = "charging" if mains else "on battery"
    lines = [("state", state), ("charge", f"{percent}%  {_bar(percent / 100.0)}")]
    if remaining >= 0 and not mains:
        lines.append(("remaining", human(remaining)))
    return Result(True, f"battery {percent}% — {state}", _table(lines))


def _display(args: list[str]) -> Result:
    found = desktop.screens()
    if not found:
        return Result(False, "no displays reported")
    lines = []
    for screen in found:
        mark = "*" if screen.primary else " "
        scale = f"  @{screen.scale:g}x" if screen.scale != 1.0 else ""
        lines.append(
            f"{mark} {screen.name:<24} {screen.width}x{screen.height}"
            f"  {screen.refresh:g}Hz{scale}"
        )
    return Result(True, f"{len(found)} display(s), * is primary", tuple(lines))


def _procs(args: list[str]) -> Result:
    limit = int(args[0]) if args and args[0].isdigit() else 12
    ranked = system.processes(max(1, min(limit, 40)))
    if not ranked:
        return Result(False, "could not read the process list")
    width = max(len(name) for name, _ in ranked)
    return Result(
        True,
        f"{len(ranked)} heaviest by memory",
        tuple(f"{name:<{width}}  {_size(kb * 1024):>9}" for name, kb in ranked),
    )


def _kill(args: list[str]) -> Result:
    if not args:
        return Result(False, "usage: kill <process>")
    ok, message = system.kill(" ".join(args))
    return Result(ok, message)


def _stats(args: list[str]) -> Result:
    """One screen for the lot, for when you just want to know."""
    rows: list[tuple[str, str]] = [("uptime", human(system.uptime()))]
    ram = system.memory()
    if ram is not None:
        used, total, percent = ram
        rows.append(("memory", f"{_size(used)} of {_size(total)}  ({percent}%)"))
    space = system.disk("C:\\")
    if space is not None:
        free, total = space
        rows.append(("disk C:", f"{_size(free)} free of {_size(total)}"))
    power = system.battery()
    if power is not None:
        percent, mains, _ = power
        rows.append(("battery", f"{percent}%  {'charging' if mains else 'discharging'}"))
    screens = desktop.screens()
    if screens:
        rows.append((
            "display",
            "  ·  ".join(f"{s.width}x{s.height}@{s.refresh:g}" for s in screens),
        ))
    rows.append(("windows", f"{len(win.listing())} open"))
    return Result(True, "this machine", _table(rows))


# ------------------------------------------------------------ other windows


def _windows(args: list[str]) -> Result:
    found = win.listing()
    if not found:
        return Result(True, "no ordinary windows are open")
    lines = []
    for index, window in enumerate(found, 1):
        title = window.title if len(window.title) <= 58 else window.title[:57] + "…"
        lines.append(f"{index:>2}. {title:<58}  {window.state}")
    return Result(True, f"{len(found)} window(s)", tuple(lines))


def _pick(args: list[str]) -> tuple[win.Window | None, str]:
    """The window a command is talking about: named, numbered, or the active one."""
    if not args:
        window = win.active()
        return window, "" if window else "nothing is focused"
    query = " ".join(args)
    if query.isdigit():
        found = win.listing()
        position = int(query) - 1
        if not 0 <= position < len(found):
            return None, f"no window {query} — try `windows`"
        return found[position], ""
    window = win.find(query)
    return window, "" if window else f"no window matching {query!r}"


def _focus(args: list[str]) -> Result:
    if not args:
        return Result(False, "usage: focus <title|number>")
    window, problem = _pick(args)
    if window is None:
        return Result(False, problem)
    if not win.focus(window):
        return Result(False, f"could not focus {window.title!r}")
    return Result(True, f"focused {window.title}", close_ui=True)


def _window_state(command: int, verb: str) -> Callable[[list[str]], Result]:
    def action(args: list[str]) -> Result:
        window, problem = _pick(args)
        if window is None:
            return Result(False, problem)
        if not win.show(window, command):
            return Result(False, f"could not {verb} {window.title!r}")
        return Result(True, f"{verb}d {window.title}", close_ui=True)

    return action


def _snap(args: list[str]) -> Result:
    if not args:
        return Result(False, f"usage: snap <{'|'.join(win.SNAP_ZONES)}> [window]")
    zone = args[0].lower()
    zone = {"l": "left", "r": "right", "t": "top", "b": "bottom",
            "center": "centre", "max": "full"}.get(zone, zone)
    if zone not in win.SNAP_ZONES:
        return Result(False, f"snap where? one of {', '.join(win.SNAP_ZONES)}")
    window, problem = _pick(args[1:])
    if window is None:
        return Result(False, problem)
    if not win.snap(window, zone):
        return Result(False, f"could not snap {window.title!r}")
    return Result(True, f"snapped {window.title} {zone}", close_ui=True)


def _desktop(args: list[str]) -> Result:
    if not win.minimise_all():
        return Result(True, "nothing was open to clear away")
    return Result(True, "cleared the desktop", close_ui=True)


def _close_window(args: list[str]) -> Result:
    window, problem = _pick(args)
    if window is None:
        return Result(False, problem)
    if not win.close(window):
        return Result(False, f"could not close {window.title!r}")
    return Result(True, f"asked {window.title} to close", close_ui=True)


# ------------------------------------------------------------ registration
# Order here is the order everything else shows them in: the help listing, the
# completions, and the navigator's branches. Grouped, because a flat list of
# fifty commands is a wall.

# -- sentinel itself ---------------------------------------------------------

register(Command(
    "open", "open [branch]", "raise the navigator", _open,
    forms=(
        ("open", "raise the navigator at the root"),
        ("open <branch>", "raise it and drill straight into that branch"),
    ),
    examples=("open", "open settings", "open notes"),
    aliases=("nav", "menu"),
))
register(Command(
    "close", "close", "put the navigator away", _close,
    forms=(("close", "dismiss the overlay and hand the prompt back"),),
    aliases=("exit", "hide"),
))
register(Command(
    "clear", "clear", "empty the scroll-back", _clear,
    forms=(("clear", "drop the command history above the prompt"),),
    aliases=("cls",),
))
register(Command(
    "help", "help [command]", "list commands, or explain one", _help,
    forms=(
        ("help", "every registered command, by group"),
        ("help <command>", "the forms, aliases and examples for one"),
    ),
    examples=("help", "help volume", "help snap"),
    aliases=("?", "commands"),
))
register(Command(
    "about", "about", "what this is, and what it is holding", _about,
    forms=(("about", "version, index size, open todos, timer state"),),
    aliases=("version",),
))
register(Command(
    "settings", "settings", "appearance, motion and startup",
    lambda a: _open(["settings"]),
    forms=(("settings", "open the settings panel"),),
))
register(Command(
    "config", "config [key] [value]", "read or change any setting", _config,
    forms=(
        ("config", "every setting and its current value"),
        ("config <key>", "just that one"),
        ("config <key> <value>", "change it; next/prev step through"),
    ),
    examples=("config", "config accent blue", "config motion next"),
))
register(Command(
    "accent", "accent [colour]", "recolour the interface", _setting("accent"),
    forms=(
        ("accent", "which colour is on"),
        ("accent <name>", "green, blue, amber — or next/prev"),
    ),
    examples=("accent", "accent amber", "accent next"),
    aliases=("colour", "color", "theme"),
))
register(Command(
    "motion", "motion [speed]", "how fast everything animates", _setting("motion"),
    forms=(
        ("motion", "the current speed"),
        ("motion <speed>", "instant, brisk, cinematic — or next/prev"),
    ),
    examples=("motion", "motion brisk"),
    aliases=("speed",),
))
register(Command(
    "reload", "reload", "re-index the Start Menu", _reload,
    forms=(
        ("reload", "rescan for applications, in the background"),
        ("also =", "drops the cached word and article lookups"),
    ),
    aliases=("reindex",),
))
register(Command(
    "log", "log", "open today's log file", _log,
    forms=(("log", "reveal the log Sentinel is writing"),),
))
register(Command(
    "echo", "echo <text>", "say something back", _echo,
    forms=(("echo <text>", "print it to the scroll-back, and nothing else"),),
    examples=("echo hello",),
))
register(Command(
    "quit", "quit now", "stop Sentinel entirely", _quit,
    forms=(("quit now", "shut the daemon down; 'now' is required"),),
    examples=("quit now",),
    confirm="now",
))

# -- notes -------------------------------------------------------------------

register(Command(
    "todo", "todo [<text>|done <n>]", "capture or close a todo", _todo,
    forms=(
        ("todo", "list the open items, numbered"),
        ("todo <text>", "capture a new one"),
        ("todo done <n>", "close item <n> from that list"),
    ),
    examples=("todo", "todo buy milk", "todo done 2"),
    aliases=("td",),
    group="notes",
))
register(Command(
    "note", "note <text>", "append a timestamped note", _note,
    forms=(
        ("note", "how many notes are held"),
        ("note <text>", "capture one, stamped with the time"),
    ),
    examples=('note "check WASAPI loopback"',),
    group="notes",
))
register(Command(
    "browse", "browse [todos|notes]", "list everything captured", _browse,
    forms=(
        ("browse", "every todo and note, open items first"),
        ("browse todos", "just the todos"),
        ("browse notes", "just the notes"),
    ),
    examples=("browse", "browse todos"),
    aliases=("list", "ls"),
    group="notes",
))
register(Command(
    "done", "done <n>", "close a todo, or reopen one", _done,
    forms=(
        ("done <n>", "close item <n> of the open list"),
        ("done undo <n>", "reopen item <n> of the closed list"),
    ),
    examples=("done 1", "done undo 2"),
    aliases=("tick",),
    group="notes",
))
register(Command(
    "due", "due <n> <when>", "set or clear a due date", _due,
    forms=(
        ("due <n> <when>", "put a deadline on an open todo"),
        ("due <n> none", "take the deadline off again"),
        ("<when> =", "20m · 2h · 3d · tomorrow · 17:30 · 25/12"),
    ),
    examples=("due 1 tomorrow", "due 2 17:30", "due 1 none"),
    group="notes",
))
register(Command(
    "tag", "tag <n> <category>", "file an entry under a category", _tag,
    forms=(
        ("tag <n> <category>", f"one of {', '.join(CATEGORIES)}"),
        ("<n> =", "the number shown by `browse`"),
    ),
    examples=("tag 1 work", "tag 3 later"),
    group="notes",
))
register(Command(
    "grep", "grep <text>", "search what you have captured", _grep,
    forms=(("grep <text>", "every todo or note mentioning it"),),
    examples=("grep milk", "grep wasapi"),
    aliases=("findnote",),
    group="notes",
))
register(Command(
    "drop", "drop <n>", "delete an entry for good", _drop,
    forms=(("drop <n>", "remove item <n> as numbered by `browse`"),),
    examples=("drop 3",),
    aliases=("rm",),
    group="notes",
))
register(Command(
    "notes", "notes", "open the notes browser", lambda a: _open(["notes"]),
    forms=(("notes", "open the notes branch of the navigator"),),
    group="notes",
))

# -- timers ------------------------------------------------------------------

register(Command(
    "remind", "remind [me] in <when> to <text>", "one-shot reminder", _remind,
    forms=(
        ("remind", "what is currently pending"),
        ("remind in <when> to <text>", "schedule one; 'me' and 'to' are optional"),
        ("<when> =", "20m · 2h · 3d · tomorrow · 17:30 · 25/12"),
    ),
    examples=("remind me in 20m to check the oven", "remind 17:30 standup"),
    group="timers",
))
register(Command(
    "pomodoro", "pomodoro [start|stop|status]", "focus timer", _pomodoro,
    forms=(
        ("pomodoro", "how the current block is going"),
        ("pomodoro start [minutes]", "begin a block, 25 minutes by default"),
        ("pomodoro stop", "abandon the current block"),
    ),
    examples=("pom start", "pom start 45", "pom stop"),
    aliases=("pom",),
    group="timers",
))
register(Command(
    "time", "time", "the clock, and where the year is up to", _time,
    forms=(("time", "time, date, week number and uptime"),),
    aliases=("date", "now"),
    group="timers",
))

# -- applications and the web ------------------------------------------------

register(Command(
    "launch", "launch <app>", "start an application by name", _launch,
    forms=(
        ("launch", "open the application browser"),
        ("launch <name>", "start the best match and stand down"),
        ('launch "<name>"', "quote a name containing spaces"),
    ),
    examples=("launch notepad", 'launch "visual studio code"'),
    aliases=("run", "start"),
    group="apps",
))
register(Command(
    "search", "search <query|url>", "go there, or look it up", _search,
    forms=(
        ("search <url>", "open an address in the default browser"),
        ("search <query>", "anything else is a Google search"),
        ("<url> =", "a scheme, www., a known suffix, localhost or an IP"),
    ),
    examples=("search github.com", "search claude api pricing"),
    aliases=("google", "web", "find"),
    group="apps",
))
register(Command(
    "folder", "folder <name>", "open a folder in Explorer", _folder,
    forms=(
        ("folder <name>", "downloads, documents, desktop, pictures…"),
        ("folder appdata|temp", "the ones with no fixed path"),
    ),
    examples=("folder downloads", "folder appdata"),
    aliases=("dir", "explorer"),
    group="apps",
))
register(Command(
    "kill", "kill <process>", "end every process of that name", _kill,
    forms=(("kill <name>", "force-close it; .exe is optional"),),
    examples=("kill notepad",),
    aliases=("end",),
    group="apps",
    confirm="",
))
register(Command(
    "procs", "procs [count]", "what is using the memory", _procs,
    forms=(
        ("procs", "the twelve heaviest processes"),
        ("procs <count>", "as many as you ask for, up to 40"),
    ),
    examples=("procs", "procs 5"),
    aliases=("ps", "tasks"),
    group="apps",
))

# -- other windows -----------------------------------------------------------

register(Command(
    "windows", "windows", "every open window, numbered", _windows,
    forms=(("windows", "the list the other window commands take numbers from"),),
    aliases=("wins",),
    group="window",
))
register(Command(
    "focus", "focus <title|number>", "bring a window to the front", _focus,
    forms=(
        ("focus <title>", "match by title; a prefix is enough"),
        ("focus <number>", "the number shown by `windows`"),
    ),
    examples=("focus firefox", "focus 3"),
    aliases=("raise",),
    group="window",
))
register(Command(
    "snap", "snap <zone> [window]", "put a window in half the screen", _snap,
    forms=(
        ("snap <zone>", f"the active window: {', '.join(win.SNAP_ZONES)}"),
        ("snap <zone> <title|number>", "some other window"),
    ),
    examples=("snap left", "snap full", "snap right firefox"),
    group="window",
))
register(Command(
    "minimise", "minimise [window]", "send a window down", _window_state(
        win.SW_MINIMIZE, "minimise"
    ),
    forms=(
        ("minimise", "the active window"),
        ("minimise <title|number>", "some other one"),
    ),
    aliases=("minimize", "min"),
    group="window",
))
register(Command(
    "maximise", "maximise [window]", "fill the screen with a window",
    _window_state(win.SW_MAXIMIZE, "maximise"),
    forms=(
        ("maximise", "the active window"),
        ("maximise <title|number>", "some other one"),
    ),
    aliases=("maximize", "max"),
    group="window",
))
register(Command(
    "shut", "shut [window]", "ask a window to close", _close_window,
    forms=(
        ("shut", "close the active window"),
        ("shut <title|number>", "close some other one"),
    ),
    examples=("shut", "shut notepad"),
    group="window",
))
register(Command(
    "desktop", "desktop", "minimise everything", _desktop,
    forms=(("desktop", "clear every window out of the way"),),
    aliases=("clean",),
    group="window",
))

# -- the machine -------------------------------------------------------------

register(Command(
    "volume", "volume [up|down|mute|<0-100>]", "system output level", _volume,
    forms=(
        ("volume", "report the current level"),
        ("volume <0-100>", "set the level exactly"),
        ("volume up [steps]", f"raise by {system.VOLUME_STEP_PERCENT}% a step, 5 by default"),
        ("volume down [steps]", "lower it the same way"),
        ("volume mute", "toggle the system mute"),
    ),
    examples=("volume 40", "volume up 3", "volume mute"),
    aliases=("vol",),
    group="system",
))
register(Command(
    "mute", "mute [app] [on|off]", "mute the system, or one app", _mute,
    forms=(
        ("mute", "toggle the system mute"),
        ("mute <app>", "toggle that application's mixer session"),
        ("mute <app> on|off", "force it to a state"),
    ),
    examples=("mute", "mute discord", "mute spotify off"),
    requires="mixer",
    group="system",
))
register(Command(
    "spotify", "spotify [play|next|prev|stop]",
    "media transport, wherever it is playing", _spotify,
    forms=(
        ("spotify", "play or pause"),
        ("spotify next|prev", "skip a track either way"),
        ("spotify stop", "stop playback"),
    ),
    examples=("spotify", "spotify next"),
    aliases=("media", "music"),
    group="system",
))
register(Command(
    "stats", "stats", "everything about this machine at once", _stats,
    forms=(("stats", "uptime, memory, disk, battery, displays, windows"),),
    aliases=("sys", "sysinfo"),
    group="system",
))
register(Command(
    "memory", "memory", "how much RAM is left", _memory,
    forms=(("memory", "used, free and total physical memory"),),
    aliases=("mem", "ram"),
    group="system",
))
register(Command(
    "disk", "disk [drive]", "how much space is left", _disk,
    forms=(
        ("disk", "the C: drive"),
        ("disk <letter>", "any other volume"),
    ),
    examples=("disk", "disk D"),
    aliases=("space",),
    group="system",
))
register(Command(
    "battery", "battery", "charge and what it is running on", _battery,
    forms=(("battery", "percentage, mains state and time left"),),
    aliases=("power",),
    group="system",
))
register(Command(
    "uptime", "uptime", "how long since the last boot", _uptime,
    forms=(("uptime", "time since Windows started"),),
    group="system",
))
register(Command(
    "display", "display", "the monitors and how they are set", _display,
    forms=(("display", "resolution, refresh rate and scaling, per screen"),),
    aliases=("monitors", "screens"),
    group="system",
))
register(Command(
    "clip", "clip [text]", "read or set the clipboard", _clip,
    forms=(
        ("clip", "show what is on the clipboard"),
        ("clip <text>", "put something on it"),
    ),
    examples=("clip", "clip hello world"),
    aliases=("clipboard", "copy"),
    group="system",
))
register(Command(
    "shot", "shot", "screenshot every monitor", _shot,
    forms=(("shot", "save a PNG to Pictures and copy its path"),),
    aliases=("screenshot", "grab"),
    group="system",
))

# -- tools -------------------------------------------------------------------

register(Command(
    "calc", "calc <expression>", "work something out", _calc,
    forms=(
        ("calc <expression>", "arithmetic, with the answer copied"),
        ("functions =", "sqrt log sin cos round min max abs floor ceil …"),
        ("constants =", "pi · e · tau"),
    ),
    examples=("calc 1920*0.26", "calc sqrt(2)", "calc round(pi,4)"),
    aliases=("=", "math"),
    group="tools",
))
register(Command(
    "roll", "roll <dice>", "throw dice", _roll,
    forms=(
        ("roll", "one six-sided die"),
        ("roll <n>d<sides>[+/-<mod>]", "the usual notation"),
    ),
    examples=("roll", "roll 2d6", "roll d20+3"),
    aliases=("dice",),
    group="tools",
))
register(Command(
    "uuid", "uuid", "a fresh identifier, copied", _uuid,
    forms=(("uuid", "a random version-4 UUID, put on the clipboard"),),
    aliases=("guid",),
    group="tools",
))
register(Command(
    "hash", "hash [algorithm] <text>", "digest some text", _hash,
    forms=(
        ("hash <text>", "sha256 of it, copied"),
        ("hash md5|sha1|sha256|sha512 <text>", "pick the algorithm"),
    ),
    examples=("hash hello", "hash md5 hello"),
    group="tools",
))

# -- lookup ------------------------------------------------------------------
# These reach the network, so each one prints a holding line first and fills the
# row in when the answer lands. They stay registered and green with no
# connection at all — running one then says so, which is the useful failure.

register(Command(
    "define", "define <word>", "what a word means", _define,
    forms=(
        ("define <word>", "its senses, grouped by part of speech"),
        ("no entry =", "spelling suggestions instead"),
    ),
    examples=("define quiet", "define ephemeral"),
    aliases=("def", "dict", "meaning"),
    group="lookup",
))
register(Command(
    "synonyms", "synonyms <word>", "other words for it", _synonyms,
    forms=(
        ("synonyms <word>", "closest first, and copied to the clipboard"),
        ("nothing filed =", "loosely related words rather than a blank"),
    ),
    examples=("synonyms quiet", "syn build"),
    aliases=("syn", "thesaurus"),
    group="lookup",
))
register(Command(
    "antonyms", "antonyms <word>", "words meaning the opposite", _antonyms,
    forms=(("antonyms <word>", "the opposites, copied; plenty of words have none"),),
    examples=("antonyms quiet", "ant increase"),
    aliases=("ant", "opposite"),
    group="lookup",
))
register(Command(
    "rhyme", "rhyme <word>", "what it rhymes with", _rhyme,
    forms=(("rhyme <word>", "perfect rhymes first, then near ones"),),
    examples=("rhyme orange", "rhyme silver"),
    aliases=("rhymes",),
    group="lookup",
))
register(Command(
    "spell", "spell <pattern>", "check a spelling, or fit a pattern", _spell,
    forms=(
        ("spell <word>", "confirm it, or offer the closest real words"),
        ("spell <pattern>", "? is one letter, * is any run — a crossword solver"),
    ),
    examples=("spell recieve", "spell c?t", "spell wr*ing"),
    aliases=("spelling",),
    group="lookup",
))
register(Command(
    "word", "word <description>", "the word for a thing you can describe", _word,
    forms=(
        ("word <description>", "a reverse dictionary; the best match is copied"),
        ("a phrase works =", "it matches on meaning, not on spelling"),
    ),
    examples=("word fear of heights", "word smell after rain"),
    aliases=("reverse", "tot"),
    group="lookup",
))
register(Command(
    "wiki", "wiki <topic>", "the opening paragraph on something", _wiki,
    forms=(
        ("wiki <topic>", "searched, not guessed — near misses still land"),
        ("<link> =", "printed after it, for when you want the whole article"),
    ),
    examples=("wiki wasapi", "wiki heisenberg uncertainty"),
    aliases=("wikipedia", "encyclopedia"),
    group="lookup",
))

# -- power -------------------------------------------------------------------

register(Command(
    "lock", "lock", "lock the workstation", _lock,
    forms=(("lock", "lock immediately, no confirmation"),),
    group="power",
))
register(Command(
    "sleep", "sleep now", "suspend the machine", _sleep,
    forms=(("sleep now", "suspend to RAM; 'now' is required"),),
    examples=("sleep now",),
    group="power",
    confirm="now",
))
register(Command(
    "hibernate", "hibernate now", "suspend to disk", _hibernate,
    forms=(("hibernate now", "suspend to disk; 'now' is required"),),
    examples=("hibernate now",),
    group="power",
    confirm="now",
))
register(Command(
    "restart", "restart now", "reboot the machine", _restart,
    forms=(("restart now", "reboot; 'now' is required"),),
    examples=("restart now",),
    aliases=("reboot",),
    group="power",
    confirm="now",
))
register(Command(
    "shutdown", "shutdown now", "turn the machine off", _shutdown,
    forms=(("shutdown now", "power off; 'now' is required"),),
    examples=("shutdown now",),
    aliases=("poweroff",),
    group="power",
    confirm="now",
))
register(Command(
    "signout", "signout now", "end the Windows session", _signout,
    forms=(("signout now", "log off; 'now' is required"),),
    examples=("signout now",),
    aliases=("logoff", "logout"),
    group="power",
    confirm="now",
))


# ------------------------------------------------------------ argument values
# Commands whose arguments are real things on this machine rather than a fixed
# vocabulary. Everything else completes from its `forms`.


def _window_values(args: list[str], position: int, prefix: str) -> list[Suggestion]:
    return [
        Suggestion(quote(w.title), w.title, w.state, "value") for w in win.listing()
    ]


def _snap_values(args: list[str], position: int, prefix: str) -> list[Suggestion]:
    if position == 0:
        return [
            Suggestion(zone, zone, "half the work area", "argument")
            for zone in win.SNAP_ZONES
        ]
    return _window_values(args, position, prefix)


def _process_values(args: list[str], position: int, prefix: str) -> list[Suggestion]:
    return [
        Suggestion(name, name, _size(kb * 1024), "value")
        for name, kb in system.processes(20)
    ]


def _folder_values(args: list[str], position: int, prefix: str) -> list[Suggestion]:
    names = [*desktop.FOLDERS, "appdata", "temp"]
    return [
        Suggestion(name, name, str(desktop.folder(name) or "not on this machine"), "value")
        for name in names
    ]


def _config_values(args: list[str], position: int, prefix: str) -> list[Suggestion]:
    if position == 0:
        return [
            Suggestion(o.key, o.key, settings.label_for(o.key), "value") for o in SCHEMA
        ]
    option = _BY_SETTING.get(args[0].lower() if args else "")
    return _setting_values(option, "")


def _setting_values(option, _prefix: str) -> list[Suggestion]:
    if option is None:
        return []
    if option.kind == "bool":
        return [
            Suggestion("on", "on", option.help, "argument"),
            Suggestion("off", "off", option.help, "argument"),
        ]
    return [
        Suggestion(name.split()[0].lower(), name, str(value), "value")
        for value, name in option.choices
    ]


def _choice_values(key: str) -> Callable[[list[str], int, str], list[Suggestion]]:
    def provider(args: list[str], position: int, prefix: str) -> list[Suggestion]:
        return _setting_values(_BY_SETTING.get(key), prefix) if position == 0 else []

    return provider


def _entry_values(kind: str | None) -> Callable[[list[str], int, str], list[Suggestion]]:
    """Numbered entries, so `done`/`due`/`drop` can be completed rather than counted."""

    def provider(args: list[str], position: int, prefix: str) -> list[Suggestion]:
        if position != 0:
            return []
        items = _open_todos() if kind == TODO else notebook.all(kind)
        return [
            Suggestion(str(i), str(i), entry.title, "value")
            for i, entry in enumerate(items[:20], 1)
        ]

    return provider


def _category_values(args: list[str], position: int, prefix: str) -> list[Suggestion]:
    if position == 0:
        return _entry_values(None)(args, position, prefix)
    return [Suggestion(name, name, "category", "argument") for name in CATEGORIES]


_PROVIDERS.update({
    "launch": _launch_values,
    "mute": _mute_values,
    "help": _help_values,
    "open": _open_values,
    "focus": _window_values,
    "minimise": _window_values,
    "maximise": _window_values,
    "shut": _window_values,
    "snap": _snap_values,
    "kill": _process_values,
    "folder": _folder_values,
    "config": _config_values,
    "accent": _choice_values("accent"),
    "motion": _choice_values("motion"),
    "done": _entry_values(TODO),
    "due": _entry_values(TODO),
    "drop": _entry_values(None),
    "tag": _category_values,
})


