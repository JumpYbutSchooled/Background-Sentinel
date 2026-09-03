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

That starts it without a console window: the process hands itself over to `pythonw.exe` and returns, so the terminal you launched it from can be closed without taking the daemon with it. Look for the tray icon — that's also how you quit it. Pass `--console` (or set `SENTINEL_CONSOLE=1`) to stay attached and watch it print, which is what you want while working on it.

Enable **Start with Windows** from the tray menu or the settings panel to have it come up on boot (writes to the per-user `HKCU\...\Run` key — no admin prompt).

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

Version 1.0.3. The packaged build is gone — no `build.ps1`, no PyInstaller spec, no `dist\`. Running from source is the one supported path again, as it was before. The executable could never run on the laptop this is written on: Defender Exploit Guard blocks any freshly built unsigned binary by administrator policy, and no PyInstaller setting changes that, so a build target that only worked on other people's machines was not worth the four hundred lines and the 45 MB.

The motion has been debugged off a screen recording, without changing how long anything takes. Every baseline is where it was, so the named speeds still mean what they say.

`intro` was being driven through an `InOutSine` curve *and* shaped again by each leg's own smootherstep — eased twice, exactly as the comment above the windows said it should not be. The cost is all at the two ends of the journey and it grows with the duration, so the slower speeds paid the most: at `cinematic` the capsule was under a tenth of the way balled up after nine hundred milliseconds. That is not a gentle opening, it is a dead one, and it read as the card having hung. The driving animation is linear now, as documented; smootherstep still takes both derivatives to zero at either end, so nothing jerks.

Two smaller ones. `OFF_PATTERN` used to go dark, back to *full*, and dark again — a tube striking on may catch and drop, but on the way out it reads as a window that failed to repaint. It still stutters and never brightens. And the card no longer asks Windows to resize and move a translucent, always-on-top window on the many frames where neither number actually changed.

Then the log turned out to be holding 466 of these, in pairs:

```
QPainter::begin: A paint device can only be painted by one painter at a time.
QPainter::translate: Painter not active
```

One pair per hand-over. The card used to fade its contents with a `QGraphicsOpacityEffect`, and Qt renders an effect's subtree through an offscreen pixmap while the window's own painter is active — on a translucent always-on-top window the two collide, the effect's painter never begins, and the frame it was drawing is dropped without telling anyone. The contents are now held as a still and faded by hand, which is not an approximation: `_freeze` has already stopped the clock and pinned the size precisely so nothing in the card can change while it fades. It also costs one render instead of an offscreen pass per repaint, which the old code's own comment was already unhappy about.