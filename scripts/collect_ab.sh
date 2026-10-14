#!/usr/bin/env bash
# One goal, two runs, one diff; the logic is in collect_ab.py (M5). Usage: collect_ab.sh run|report.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/collect_ab.py" "$@"
