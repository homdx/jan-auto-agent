#!/usr/bin/env bash
# Round 204: run acceptance_204.py in each given checkout.  run_204.sh NAME=DIR ...
for spec in "$@"; do
    name=${spec%%=*}; dir=${spec#*=}
    mkdir -p "$dir/contest-bench/204"; cp "$(dirname "$0")/acceptance_204.py" "$dir/contest-bench/204/"
    echo "== $name"; (cd "$dir" && python3 -m pytest contest-bench/204/acceptance_204.py -q -p no:cacheprovider -n0 2>&1 | grep -E "passed|failed|FAILED|error" | tail -20)
done
