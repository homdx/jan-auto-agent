#!/usr/bin/env bash
# Round 207: run acceptance_207.py in each given checkout.  run_207.sh NAME=DIR ...
here=$(cd "$(dirname "$0")" && pwd)
for spec in "$@"; do
    name=${spec%%=*}; dir=${spec#*=}
    mkdir -p "$dir/contest-bench/207"
    cp "$here/acceptance_207.py" "$dir/contest-bench/207/"
    echo "== $name"
    (cd "$dir" && python3 -m pytest contest-bench/207/acceptance_207.py -q -p no:cacheprovider -n0 2>&1 | grep -E "passed|failed|FAILED|error" | tail -30)
done
