#!/usr/bin/env bash
# Round 255 (CC-1): run acceptance_255.py in each given checkout.  run_255.sh NAME=DIR ...
here=$(cd "$(dirname "$0")" && pwd)
for spec in "$@"; do
    name=${spec%%=*}; dir=${spec#*=}
    mkdir -p "$dir/contest-bench/255"; cp "$here/acceptance_255.py" "$dir/contest-bench/255/"
    echo "== $name"
    (cd "$dir" && python3 -m pytest contest-bench/255/acceptance_255.py -q -p no:cacheprovider -n0 2>&1 | grep -E "passed|failed|error|^FAILED|^ERROR" | sed 's/ - .*//' | tail -40)
done
