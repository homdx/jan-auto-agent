#!/usr/bin/env bash
# Runs the FL-2 acceptance suite on each bench worktree, one at a time.
set -u
B=$(realpath "$1"); shift
SUITE=$(realpath "$(dirname "$0")/acceptance_fl2.py")
entries=("$@"); [ ${#entries[@]} -eq 0 ] && entries=($(ls "$B"))
for e in "${entries[@]}"; do
  cp "$SUITE" "$B/$e/tests/acceptance_fl2.py"
  out=$(cd "$B/$e" && FL2_REPO="$B/$e" timeout 600 python3 -m pytest tests/acceptance_fl2.py -q -p no:cacheprovider -n0 -rf 2>&1)
  rm -f "$B/$e/tests/acceptance_fl2.py"
  echo "== $e: $(echo "$out" | tail -1)"
  echo "$out" | grep '^FAILED' | sed 's/.*acceptance_fl2.py::/   /; s/ - .*//'
done
