#!/usr/bin/env bash
# scripts/collect_ab.sh — M5: one goal, two runs, one diff.
#
# Thin entry point; every counter, the config patch, the stub and the verdict
# live in scripts/collect_ab.py (see its docstring / --help).
#
#   scripts/collect_ab.sh run --goal "<goal>" --base ../tree \
#       --config agents_128k.ini --workdir /tmp/ab \
#       --out docs/collect-epics/ab-$(date +%F).json
#
#   scripts/collect_ab.sh report --a /tmp/ab/arm_off_0 --b /tmp/ab/arm_on_0
#
# The config is copied to the workdir and patched there — never edited.
# `python` is not on PATH in this repo; python3 is pinned.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/collect_ab.py" "$@"
