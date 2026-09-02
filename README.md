# Background Sentinel

An always-resident command layer for Windows. Press `Ctrl+Shift+Space` from anywhere and a prompt appears over whatever you were doing — type a command, get an answer, and it stands down.

It sits in the tray and holds one process: the hotkey registration, the popup, and the state that has to survive between summons (todos, reminders, a pomodoro block, the scroll-back). Nothing is launched on demand, so there is no startup cost when you call it.

Typing `open` swaps the prompt for a full-screen radial navigator — the command tree drawn as a ring of branches around a centre dial, with the command line docked at the bottom. Both surfaces share one session, so history typed in either shows up in both.

## Requirements

- Windows 10 or 11
- Python 3.11+ (developed on 3.13)
- `PySide6 >= 6.6`
- `pycaw` — optional, only for per-application mixer control (`mute <app>`). Everything else works without it.

## Running it

```powershell
python -m pip install -r requirements.txt
python main.py
```

Use `pythonw main.py` to start it without a console window, which is how you'd actually want it running day to day. Enable **Start with Windows** from the tray menu or the settings panel to have it come up on boot (writes to the per-user `HKCU\...\Run` key — no admin prompt).

Only one instance runs at a time; launching a second one tells you so and exits.

## Commands

65 commands across nine groups. `help` lists all of them, `help <command>` explains one.

| Group | What's in it |
|---|---|
| **sentinel** | `open` `close` `clear` `help` `about` `settings` `config` `accent` `motion` `reload` `log` `echo` `quit` |
| **notes** | `todo` `note` `browse` `done` `due` `tag` `grep` `drop` |
| **timers** | `remind` `pomodoro` `time` |
| **apps** | `launch` `search` `folder` `kill` `procs` |
| **window** | `windows` `focus` `snap` `minimise` `maximise` `shut` `desktop` |
| **system** | `volume` `mute` `spotify` `stats` `memory` `disk` `battery` `uptime` `display` `clip` `shot` |
| **tools** | `calc` `roll` `uuid` `hash` |
| **lookup** | `define` `synonyms` `antonyms` `rhyme` `spell` `word` `wiki` |
| **power** | `lock` `sleep` `hibernate` `restart` `shutdown` `signout` |

A few examples:

```
launch "visual studio code"
snap left firefox
remind me in 20m to check the oven
calc round(pi, 4)
word fear of heights
volume up 3
```

Anything destructive takes a confirmation word — `quit now`, `shutdown now`, `restart now`. The prompt colours green as you type a real command and red when it isn't one, and Tab completes from live values where that makes sense: open windows for `focus`, running processes for `kill`, indexed applications for `launch`.

Network lookups (`define`, `wiki`, `rhyme`) print a holding line, run on a worker thread, and fill the row in when the answer lands — the prompt never blocks.

## How it fits together

```
main.py              entry point
sentinel/
  app.py             daemon wiring: tray, hotkey, popup, navigator
  commands.py        the registry — tokenizing, validity, completions, dispatch
  tree.py            command tree for the navigator, derived from the registry
  config.py          settings schema, live and persisted
  store.py           atomic JSON reads and writes
  transcript.py      the scroll-back both command lines share
  notes.py           todos and notes
  timers.py          reminders and pomodoro blocks
  hotkey.py          RegisterHotKey via ctypes
  windows.py         window enumeration, focus, snap zones
  system.py          volume, memory, disk, battery, displays, processes
  apps.py            Start Menu index and launching
  desktop.py         clipboard, known folders, screenshots
  web.py             address-vs-query disambiguation
  words.py calc.py   dictionary lookups, expression evaluation
  ui/
    popup.py         the summoned prompt
    nav.py           the radial navigator
    panels.py        leaf panels
    motion.py        animation timing
    ...
```

Two design points worth knowing if you're reading the source:

**The registry is the single source of truth.** The navigator's tree, the completions, and the syntax highlighting are all derived from it. Registering a command makes it appear everywhere; there's no second list to keep in step.

**The UI thread draws, workers don't.** Slow commands return a callable that runs on a daemon thread and reports back through a queued Qt signal. Clipboard writes happen on the GUI thread for the same reason.

Settings and notes live in `%LOCALAPPDATA%\BackgroundSentinel\`, written through a temp file and an atomic replace — a daemon that runs all day will eventually get killed mid-write, and a half-written notes file would lose everything. Logs rotate in `logs\` under the same directory; `log` opens the current one.

## Configuration

Accent colour, animation speed, frame cap, background effects, and focus-loss behaviour are all live — change them and the interface re-themes without a restart. Either open the settings panel or use `config`:

```
config                    every setting and its value
config accent blue
config motion next
```

The default hotkey is `ctrl+shift+space`, set in `sentinel/__init__.py`. If `RegisterHotKey` fails because something else already owns your combination, Sentinel says so on startup rather than failing silently.

## Status

Version 1.0.1, im THINKING about packaging it as an `exe` seperately, but this was designed for use on a school laptop which sometimes dosent support `exe`.
