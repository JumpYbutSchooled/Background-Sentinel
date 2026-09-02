"""Background Sentinel entry point.

Development:   python main.py
Windowless:    pythonw main.py
"""

import sys

from sentinel.app import main

if __name__ == "__main__":
    sys.exit(main())
 