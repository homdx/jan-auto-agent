"""tools/contest/__main__.py — KC-16: `python3 -m tools.contest …`.

`cli.main` takes subcommands (`run` here; KC-7 adds `status` and `--dry-run`),
so the whole contest lives behind this one entry point.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
