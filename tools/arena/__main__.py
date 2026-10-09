"""tools/arena/__main__.py — AR-1: `python3 -m tools.arena …`.

The same entry point as the repo-root `./arena` launcher: both run `cli.main`.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
