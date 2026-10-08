#!/usr/bin/env bash
# Round 200: run acceptance_200.py in each given checkout.  run_200.sh NAME=DIR ...
here=$(cd "$(dirname "$0")" && pwd)
for spec in "$@"; do
    name=${spec%%=*}; dir=${spec#*=}
    mkdir -p "$dir/contest-bench/200"
    cp "$here/acceptance_200.py" "$dir/contest-bench/200/"
    echo "== $name"
    (cd "$dir" && python3 -m pytest contest-bench/200/acceptance_200.py -q -p no:cacheprovider -n0 2>&1 | grep -E "passed|failed|FAILED|error" | tail -30)
done
