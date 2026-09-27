#!/bin/bash
# One tree at a time: copy the suite in, run it, print pass/fail per test.
# usage: run_all_kc10.sh <wt dir> [tree ...]
set -u
WT=$1; shift
HERE=$(cd "$(dirname "$0")" && pwd)
TREES=${@:-$(ls "$WT")}
for t in $TREES; do
  cp "$HERE/acceptance_kc10.py" "$WT/$t/tests/test_kc10_accept.py"
  (cd "$WT/$t" && timeout 900 python3 -m pytest tests/test_kc10_accept.py -q -rA -p no:cacheprovider \
      -n 0 --timeout=120 2>&1 | grep -E "^(PASSED|FAILED|ERROR|XFAIL|XPASS) " | sed "s#^#$t #")
  rm -f "$WT/$t/tests/test_kc10_accept.py"
done
