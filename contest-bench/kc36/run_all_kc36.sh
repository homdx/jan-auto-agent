#!/bin/bash
# Runs each tree one at a time, copying the suite in, and prints pass/fail per test.
set -u
CB=$1; shift
HERE=$(cd "$(dirname "$0")" && pwd)
TREES=${@:-$(ls "$CB")}
for t in $TREES; do
  cp "$HERE/acceptance_kc36.py" "$CB/$t/tests/test_kc36_accept.py"
  (cd "$CB/$t" && timeout 900 python3 -m pytest tests/test_kc36_accept.py -q -rA -p no:cacheprovider \
      -n 0 --timeout=180 2>&1 | grep -E "^(PASSED|FAILED|ERROR) " | sed "s#^#$t #")
done
