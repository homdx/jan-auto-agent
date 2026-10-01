#!/bin/bash
# One tree at a time: copy the suite in, run it, print pass/fail per test.
# usage: run_all_kc40.sh <trees dir> [tree ...]
set -u
CB=$1; shift
HERE=$(cd "$(dirname "$0")" && pwd)
TREES=${@:-$(ls "$CB")}
for t in $TREES; do
  cp "$HERE/acceptance_kc40.py" "$CB/$t/tests/test_kc40_accept.py"
  (cd "$CB/$t" && timeout 900 python3 -m pytest tests/test_kc40_accept.py -q -rA -p no:cacheprovider \
      -n 4 --timeout=180 2>&1 | grep -E "^(PASSED|FAILED|ERROR) " | sed "s#^#$t #")
  rm -f "$CB/$t/tests/test_kc40_accept.py"
done
