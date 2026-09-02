"""Background Sentinel entry point.

Normal use:   python main.py       — hands itself over to a windowless copy and
                                     returns, so the console it was started
                                     from can be closed without taking the
                                     daemon with it. Look for the tray icon.
Stay attached: python main.py --console   (or set SENTINEL_CONSOLE=1)
Already windowless: pythonw main.py       — no hand-over needed.

Once it is running, the tray icon quits it; there is no console to Ctrl+C
unless you asked for one.
"""

import sys

from sentinel.app import main

if __name__ == "__main__":
    sys.exit(main())
