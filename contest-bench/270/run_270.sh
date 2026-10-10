#!/usr/bin/env bash
# Round 270 (CC-4): run acceptance_270.py against each given checkout's tools/claimcheck/.  run_270.sh NAME=DIR ...
here=$(cd "$(dirname "$0")" && pwd)
for spec in "$@"; do
    name=${spec%%=*}; dir=${spec#*=}
    echo "== $name"
    python3 "$here/acceptance_270.py" --code "$dir" 2>&1 | grep -vE "^  ok" | tail -20
done
