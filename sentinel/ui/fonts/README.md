# Bundled fonts

`RobotoCondensed-Regular.ttf` and `RobotoCondensed-Bold.ttf` — Roboto Condensed,
by Christian Robertson, released under the Apache License 2.0 (`LICENSE.txt`).
Static instances taken from Google Fonts.

The mechanical theme draws its chrome in this face, because it is the one the
palette came from. Nothing else in Sentinel depends on it: the files are loaded
with `QFontDatabase.addApplicationFont` at startup, and if they are missing —
someone's checkout without LFS, a stripped copy, a packaging step that dropped
them — the theme falls back to Bahnschrift Condensed, then Arial Narrow, then
whatever the platform calls a sans. It looks different and it still works.

The command line, the scroll-back and the suggestion list are never drawn in
this: they are laid out in columns padded with spaces, and only a monospace
face keeps those columns lined up.
