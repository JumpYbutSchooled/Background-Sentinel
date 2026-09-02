"""Background Sentinel — always-resident command layer for Windows."""

APP_NAME = "Background Sentinel"
APP_SLUG = "BackgroundSentinel"
APP_VERSION = "0.1.0"

# Default summon hotkey. Parsed by sentinel.hotkey.parse_hotkey().
# Not ctrl+alt+space: that combination is already claimed on this machine and
# RegisterHotKey fails with 1409 (ERROR_HOTKEY_ALREADY_REGISTERED).
DEFAULT_HOTKEY = "ctrl+shift+space"
