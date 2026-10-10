#!/usr/bin/env bash
# Round 265 (CC-3): run acceptance_265.py against each given checkout's tools/claimcheck/.  run_265.sh NAME=DIR ...
here=$(cd "$(dirname "$0")" && pwd)
for spec in "$@"; do
    name=${spec%%=*}; dir=${spec#*=}
    echo "== $name"
    python3 "$here/acceptance_265.py" --code "$dir" 2>&1 | grep -vE "^  ok" | tail -20
done
