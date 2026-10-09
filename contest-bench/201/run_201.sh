#!/usr/bin/env bash
# Round 201: run acceptance_201.py and 198's bench in each given checkout.  run_201.sh NAME=DIR ...
here=$(cd "$(dirname "$0")" && pwd)
for spec in "$@"; do
    name=${spec%%=*}; dir=${spec#*=}
    mkdir -p "$dir/contest-bench/201" "$dir/contest-bench/198"
    cp "$here/acceptance_201.py" "$dir/contest-bench/201/"; cp "$here/../198/acceptance_198.py" "$dir/contest-bench/198/"
    echo "== $name"
    (cd "$dir" && python3 -m pytest contest-bench/201/acceptance_201.py contest-bench/198/acceptance_198.py -q -p no:cacheprovider -n0 2>&1 | grep -E "passed|failed|FAILED|error" | tail -30)
done
