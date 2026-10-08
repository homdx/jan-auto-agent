#!/usr/bin/env bash
# Round 213: run acceptance_213.py (+ 198's and 201's benches when present) in each given checkout.  run_213.sh NAME=DIR ...
here=$(cd "$(dirname "$0")" && pwd)
for spec in "$@"; do
    name=${spec%%=*}; dir=${spec#*=}
    files=""
    for n in 213 201 198; do
        [ -f "$here/../$n/acceptance_$n.py" ] || continue
        mkdir -p "$dir/contest-bench/$n"; cp "$here/../$n/acceptance_$n.py" "$dir/contest-bench/$n/"; files="$files contest-bench/$n/acceptance_$n.py"
    done
    echo "== $name"
    (cd "$dir" && python3 -m pytest $files -q -p no:cacheprovider -n0 2>&1 | grep -E "passed|failed|^FAILED" | sed 's/ - .*//' | tail -40)
done
