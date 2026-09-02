"""The command tree the navigator renders.

Built by walking the registry, which is what the first pass promised and never
delivered. It matters for more than tidiness: the hand-written version had
drifted, and was still offering `display` and `obs` branches for commands that
were never implemented. A tree derived from the registry cannot lie about what
exists, and a new command appears in the interface by being registered.

A leaf names the `panel` that draws it. Most get `COMMAND`, which is a working
view of the command — its forms, its completions, an argument line and a way to
run it. A handful have richer views of their own: the launcher indexes the Start
Menu, the notes panels read and write the notebook, settings writes settings.json.

`eq=False` keeps dataclass identity semantics, so `children.index(node)` finds
*that* node rather than the first one with matching fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .commands import commands, groups, lookup

READOUT = "readout"
COMMAND = "command"
LAUNCHER = "launcher"
TODO_LIST = "todo"
NEW_ENTRY = "new"
NOTE_LIST = "notes"
SETTINGS = "settings"
ABOUT = "about"

#: Commands whose own panel is richer than the generic one.
PANELS: dict[str, str] = {
    "launch": LAUNCHER,
    "todo": TODO_LIST,
    "browse": NOTE_LIST,
    "settings": SETTINGS,
    "about": ABOUT,
}

#: Commands that drive the navigator itself. Offering "open the navigator" as a
#: branch *of* the navigator is a joke the interface only gets to make once.
HIDDEN = frozenset({"open", "close", "notes"})

#: What each group is, in one line, for the branch that holds it.
BLURBS: dict[str, str] = {
    "sentinel": "the daemon, its settings and its own state",
    "notes": "quick capture, kept by the daemon",
    "timers": "time kept while nothing is on screen",
    "apps": "starting things, and the web",
    "window": "arranging what is already open",
    "system": "audio, hardware and the machine's own counters",
    "tools": "small answers without leaving the prompt",
    "lookup": "words and facts, fetched from the network",
    "power": "session and power state",
}


@dataclass(eq=False)
class Node:
    name: str
    summary: str = ""
    #: Terminal-style readout shown when a leaf is opened, for READOUT panels.
    detail: tuple[str, ...] = ()
    children: tuple[Node, ...] = field(default_factory=tuple)
    #: Which panel draws this leaf.
    panel: str = READOUT
    #: The command this leaf runs, when it has one.
    command: str = ""

    @property
    def is_leaf(self) -> bool:
        return not self.children


def _leaf(name: str) -> Node:
    command = lookup(name)
    if command is None:  # only reachable for the synthetic nodes below
        return Node(name)
    return Node(
        command.name,
        command.summary,
        panel=PANELS.get(command.name, COMMAND),
        command=command.name,
    )


def _group(name: str) -> Node:
    children = [_leaf(c.name) for c in commands(name) if c.name not in HIDDEN]
    if name == "notes":
        # The capture form is a view, not a command — `todo` and `note` both
        # write through it — so it has no registry entry to be derived from.
        children.insert(1, Node("new", "capture a todo or a note", panel=NEW_ENTRY))
    return Node(name, BLURBS.get(name, ""), children=tuple(children))


def build_tree() -> Node:
    return Node(
        "main",
        "command surface",
        children=tuple(_group(name) for name in groups()),
    )
