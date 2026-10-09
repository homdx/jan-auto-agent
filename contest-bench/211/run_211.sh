#!/usr/bin/env bash
# Round 211: run acceptance_211.py in each given checkout.  run_211.sh NAME=DIR ...
here=$(cd "$(dirname "$0")" && pwd)
for spec in "$@"; do
    name=${spec%%=*}; dir=${spec#*=}
    mkdir -p "$dir/contest-bench/211"
    cp "$here/acceptance_211.py" "$dir/contest-bench/211/"
    echo "== $name"
    (cd "$dir" && python3 -m pytest contest-bench/211/acceptance_211.py -q -p no:cacheprovider -n0 2>&1 | grep -E "passed|failed|FAILED|error" | tail -30)
done
