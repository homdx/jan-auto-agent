#!/usr/bin/env bash
# Round 195 (AR-8): run acceptance_195.py on each entry's committed code.
#   run_195.sh OUT_DIR NAME=CHECKOUT[@REF] ...
# Each entry is a `git archive` of REF (default HEAD) in OUT_DIR/NAME — the
# checkout itself is only read — with the bench copied in and run there.
# One line per entry in OUT_DIR/results.tsv: name, passed, failed, the FAILED ids.
set -euo pipefail
OUT=$1; shift
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT"
: > "$OUT/results.tsv"
for spec in "$@"; do
    name=${spec%%=*}; src=${spec#*=}; ref=HEAD
    case $src in *@*) ref=${src##*@}; src=${src%@*};; esac
    tree=$OUT/$name
    rm -rf "$tree"; mkdir -p "$tree/contest-bench/195"
    git -C "$src" archive "$ref" | tar -x -C "$tree"
    cp "$HERE/acceptance_195.py" "$tree/contest-bench/195/"
    log=$OUT/$name.log
    (cd "$tree" && [ "$(pwd)" = "$tree" ] && \
        python3 -m pytest contest-bench/195/acceptance_195.py -n 0 -q -p no:cacheprovider) \
        > "$log" 2>&1 || true
    passed=$(grep -oE '[0-9]+ passed' "$log" | grep -oE '[0-9]+' || echo 0)
    failed=$(grep -oE '[0-9]+ failed' "$log" | grep -oE '[0-9]+' || echo 0)
    fails=$(grep '^FAILED' "$log" | sed 's/ - .*//; s/.*:://' | paste -sd' ' || true)
    printf '%s\t%s\t%s\t%s\n' "$name" "$passed" "$failed" "$fails" >> "$OUT/results.tsv"
    printf '%-32s %3s passed %3s failed\n' "$name" "$passed" "$failed"
done
