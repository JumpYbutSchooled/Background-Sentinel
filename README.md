# Background Sentinel

An always-resident command layer for Windows. Press `Ctrl+Shift+Space` from anywhere and a prompt appears over whatever you were doing — type a command, get an answer, and it stands down.

It sits in the tray and holds one process: the hotkey registration, the popup, and the state that has to survive between summons (todos, reminders, a pomodoro block, the scroll-back). Nothing is launched on demand, so there is no startup cost when you call it.

Typing `open` swaps the prompt for a full-screen radial navigator — the command tree drawn as a ring of branches around a centre dial, with the command line docked at the bottom. Both surfaces share one session, so history typed in either shows up in both.

It comes in two looks, and they are not the same interface recoloured. **phosphor** is the green tube: round plates, a soft glow, a CRT striking on. **mechanical** is built on the ASCTE palette — crimson and navy — and on the 45° diagonal: sheared plates, an octagonal dial, a hazard hatch, a hard shutter sweeping the screen. Switch with `theme mechanical`, or from the settings panel. Every command, animation and easing exists in both.

## Requirements

- Windows 10 or 11
- Python 3.11+ (developed on 3.13)
- `PySide6 >= 6.6`
- `pycaw` — optional, only for per-application mixer control (`mute <app>`). Everything else works without it.
- Roboto Condensed is bundled in `sentinel/ui/fonts/` (Apache-2.0) for the mechanical theme's chrome. Nothing installs it; it is loaded into the process at runtime, and if the files are missing the theme falls back to Bahnschrift Condensed and carries on.

## Running it

```powershell
python -m pip install -r requirements.txt
python main.py
```

That starts it without a console window: the process hands itself over to `pythonw.exe` and returns, so the terminal you launched it from can be closed without taking the daemon with it. Look for the tray icon — that's also how you quit it. Pass `--console` (or set `SENTINEL_CONSOLE=1`) to stay attached and watch it print, which is what you want while working on it.

Enable **Start with Windows** from the tray menu or the settings panel to have it come up on boot (writes to the per-user `HKCU\...\Run` key — no admin prompt).

Only one instance runs at a time; launching a second one tells you so and exits.

## Commands

67 commands across nine groups. `help` lists all of them, `help <command>` explains one.

| Group | What's in it |
|---|---|
| **sentinel** | `open` `close` `clear` `help` `about` `settings` `config` `accent` `motion` `theme` `reload` `log` `echo` `quit` |
| **notes** | `todo` `note` `browse` `done` `due` `tag` `grep` `drop` |
| **timers** | `remind` `pomodoro` `time` |
| **apps** | `launch` `search` `folder` `kill` `procs` |
| **window** | `windows` `focus` `snap` `minimise` `maximise` `shut` `desktop` |
| **system** | `volume` `mute` `audio` `spotify` `stats` `memory` `disk` `battery` `uptime` `display` `clip` `shot` |
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
audio headphones
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
  theme.py           the two looks: colour, shape, motion, type
  store.py           atomic JSON reads and writes
  transcript.py      the scroll-back both command lines share
  notes.py           todos and notes
  timers.py          reminders and pomodoro blocks
  hotkey.py          RegisterHotKey via ctypes
  windows.py         window enumeration, focus, snap zones
  system.py          volume, memory, disk, battery, displays, processes
  endpoints.py       audio devices, and which one Windows plays through
  apps.py            Start Menu index and launching
  desktop.py         clipboard, known folders, screenshots
  web.py             address-vs-query disambiguation
  words.py calc.py   dictionary lookups, expression evaluation
  ui/
    popup.py         the summoned prompt
    nav.py           the radial navigator
    panels.py        leaf panels
    motion.py        animation timing and the flicker waveforms
    paint.py         palette, fonts and the themed plate every surface draws
    fonts/           Roboto Condensed, for the mechanical theme's chrome
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

The theme is live too, and it takes the accent, the shapes and the motion with it:

```
theme                     which one is running, and what the other is
theme mechanical          switch
theme next                step through them
```

Switching moves the accent to whatever the new theme leads with — green for phosphor, crimson for mechanical — and you can cycle it from there as before. `accent` still offers all seven colours in either theme.

The default hotkey is `ctrl+shift+space`, set in `sentinel/__init__.py`. If `RegisterHotKey` fails because something else already owns your combination, Sentinel says so on startup rather than failing silently.

## Status

The navigator was dead to the mouse. It opened, it hovered, and every click on a branch did nothing at all: `nav.py` calls `step_easing()` and `settle_easing()` to build the descent, and neither was ever added to its import from `motion` when the easings moved into the theme. The `NameError` was raised inside `mousePressEvent`, and Qt does not let an exception out of a Python override — it prints it and returns, so the click was swallowed. Nothing was logged either, because a windowless daemon has no stderr to print to, which is why this looked like an interface that had simply stopped responding rather than a crash. Both names are imported now, and clicking through all nine branches to a leaf panel and back out raises nothing. `popup.py` had the same kind of gap — `Callable` used in annotations it never imported — harmless only because `from __future__ import annotations` keeps them unevaluated; it is imported now too.

Sentinel is silent again. The synthesised cues are gone entirely — `sentinel/sound.py`, the `sound` command and its completions, the four settings behind it, and the per-theme cue set on the `Theme` record. Nothing calls out to audio on the summon path any more, and the prompt, the navigator and a firing reminder are all visual only. A settings file written by an earlier version still has the `sound_*` keys in it; the loader ignores keys it does not recognise, so nothing has to be migrated or cleaned up. `audio`, which switches the Windows output device, is unrelated and stays.

Version 1.0.3. The packaged build is gone — no `build.ps1`, no PyInstaller spec, no `dist\`. Running from source is the one supported path again, as it was before. The executable could never run on the laptop this is written on: Defender Exploit Guard blocks any freshly built unsigned binary by administrator policy, and no PyInstaller setting changes that, so a build target that only worked on other people's machines was not worth the four hundred lines and the 45 MB.

There is a second interface. `mechanical` takes its two colours from the ASCTE site — `#bf0a30` and `#002868`, with that site's greys behind them — and everything else about it from the 45° diagonal: plates are chamfered rather than rounded, the dial is an octagon, the reference grid is a diagonal lattice, the radar rings are diamonds, the scanline is a hard-edged shutter leaning at 45°, and the runs out to the branches leave the dial square, take one diagonal, and square on to the button. Chrome is set in Roboto Condensed, tracked wide and upper-cased.

Nothing was forked to do it. One `Theme` record carries the palette, the corner style, the flicker waveform, the easing curves and the type, and the modules that draw and animate read it. Three mechanisms carry almost all of it. The palette is a set of mutable `QColor` objects that get rewritten in place, so a surface holding a reference from a hundred frames ago is already holding the new colour. `plate()` replaced every `drawRoundedRect` call — same rectangle, same radius, and the theme decides whether the corner is an arc or a straight face. And the flicker patterns and easing names moved out of `motion` into the theme, so a mechanism slams and seats where a tube catches and drops.

Two things I would have got wrong without looking at it. The diagonal branch runs landed on a button's *corner* at first — geometrically reasonable, visually wrong, and for a button dead in line with the dial the last leg doubled back through the button. They now end on a face, and there is a check that every leg is either axis-aligned or exactly 45° across three different branch counts. And the shutter was built by shearing a rectangle by its own height, which is only 45° on a square screen; it is drawn in a rotated frame now, so it stays parallel to the hatch on any monitor.

The font is bundled rather than assumed. Roboto Condensed is not installed on this machine and will not be on anyone else's, so the two static instances ship in `sentinel/ui/fonts/` under the Apache licence they came with, and are handed to Qt at runtime — on the first call that needs a font, not at import, because `QFontDatabase` does nothing at all before a `QApplication` exists. If the files are gone the theme falls back to Bahnschrift Condensed and then to Arial Narrow. It looks different and everything still works.

The command line stayed monospace in both themes, deliberately. The scroll-back prints tables padded with spaces, and a condensed proportional face takes the columns apart. The theme's face is for chrome — labels, headings, badges, buttons — where nothing has to line up.

`audio` is about the devices rather than the mixer. pycaw only ever reaches one application's session on whatever endpoint is already default, so this goes to the core audio API through ctypes, and works on a machine that never installed the optional dependency. Listing, per-device levels and switching the default are all there.

Switching is the one part that could stop working, and it says so in the source. Windows has never published a way to change the default device: the Sound control panel does it through `IPolicyConfig`, an interface Microsoft documents nowhere and every tool of this kind therefore uses. Both of its known identities are tried, and all three roles are set together — leaving communications pointed at the old device is exactly how a call ends up coming out of the speakers you just switched away from. If neither identity answers, the command reports that Windows refused rather than claiming a switch that did not happen.

Before this, the motion was debugged off a screen recording, without changing how long anything takes. Every baseline is where it was, so the named speeds still mean what they say.

`intro` was being driven through an `InOutSine` curve *and* shaped again by each leg's own smootherstep — eased twice, exactly as the comment above the windows said it should not be. The cost is all at the two ends of the journey and it grows with the duration, so the slower speeds paid the most: at `cinematic` the capsule was under a tenth of the way balled up after nine hundred milliseconds. That is not a gentle opening, it is a dead one, and it read as the card having hung. The driving animation is linear now, as documented; smootherstep still takes both derivatives to zero at either end, so nothing jerks.

Two smaller ones. `OFF_PATTERN` used to go dark, back to *full*, and dark again — a tube striking on may catch and drop, but on the way out it reads as a window that failed to repaint. It still stutters and never brightens. And the card no longer asks Windows to resize and move a translucent, always-on-top window on the many frames where neither number actually changed.

Then the log turned out to be holding 466 of these, in pairs:

```
QPainter::begin: A paint device can only be painted by one painter at a time.
QPainter::translate: Painter not active
```

One pair per hand-over. The card used to fade its contents with a `QGraphicsOpacityEffect`, and Qt renders an effect's subtree through an offscreen pixmap while the window's own painter is active — on a translucent always-on-top window the two collide, the effect's painter never begins, and the frame it was drawing is dropped without telling anyone. The contents are now held as a still and faded by hand, which is not an approximation: `_freeze` has already stopped the clock and pinned the size precisely so nothing in the card can change while it fades. It also costs one render instead of an offscreen pass per repaint, which the old code's own comment was already unhappy about.