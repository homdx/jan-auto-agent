#!/usr/bin/env bash
# Round 197: run acceptance_197.py in each given checkout.  run_197.sh NAME=DIR ...
here=$(cd "$(dirname "$0")" && pwd)
for spec in "$@"; do
    name=${spec%%=*}; dir=${spec#*=}
    mkdir -p "$dir/contest-bench/197"; cp "$here/acceptance_197.py" "$dir/contest-bench/197/"
    echo "== $name"
    (cd "$dir" && python3 -m pytest contest-bench/197/acceptance_197.py -q -p no:cacheprovider -n0 2>&1 | grep -E "passed|failed|FAILED|error" | tail -45)
done
