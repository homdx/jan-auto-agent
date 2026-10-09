#!/usr/bin/env bash
# Round 184 acceptance: test_run_tests_detail_ends_the_suite_at_the_harvest_budget
# must not depend on how fast the nested pytest starts, and must still catch
# what it pins.  Usage:  acceptance_184.sh OUT_DIR NAME=WORKTREE ...
#
# Cases (one result line per case and tree in OUT_DIR/results.tsv):
#   quiet        the test alone, no load, 3 runs                    -> must pass
#   load         16 busy loops, the test at nice 19, 20 runs         -> must pass
#   m-nokill     _end_process_group is a no-op (ticket's test 2)     -> must fail
#   m-noname     the tail no longer says "the budget hit in:", quiet -> must fail
#   m-noname-ld  the same mutation under the load, 5 runs            -> fails = the
#                name is still checked when the box is starved
#   m-nobudget   run_tests_detail ignores budget_sec                 -> must fail
#   file         tests/test_contest_harvest.py -n 4                  -> must pass
set -uo pipefail
OUT=$1; shift
mkdir -p "$OUT"
RES=$OUT/results.tsv
: > "$RES"
NODE=tests/test_contest_harvest.py::test_run_tests_detail_ends_the_suite_at_the_harvest_budget
G=tools/contest/gates.py

one() {  # one NAME TREE CASE N NICE -> appends "name case i outcome secs"
    local name=$1 tree=$2 case=$3 i=$4 nice=$5 t0 rc out
    t0=$(date +%s.%N)
    out=$(cd "$tree" && [ "$(pwd)" = "$tree" ] && \
          nice -n "$nice" python3 -m pytest "$NODE" -n 0 -q -p no:cacheprovider 2>&1)
    rc=$?
    local secs; secs=$(python3 -c "import time;print(round(time.time()-$t0,1))")
    local o=fail
    [ $rc -eq 0 ] && o=pass
    grep -qE '[0-9]+ skipped' <<<"$out" && ! grep -qE '[0-9]+ (passed|failed)' <<<"$out" && o=skip
    printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$case" "$i" "$o" "$secs" >> "$RES"
    printf '%s\n' "$out" > "$OUT/$name.$case.$i.log"
}

reap() {  # kill any nested pytest still working inside $1 (m-nokill leaves one)
    for p in /proc/[0-9]*; do
        [ "$(readlink "$p/cwd" 2>/dev/null)" = "$1" ] || continue
        grep -qa pytest "$p/cmdline" 2>/dev/null && kill -9 "${p#/proc/}" 2>/dev/null
    done
}

mutate() {  # mutate TREE KIND
    local f=$1/$G
    case $2 in
        nokill)   python3 - "$f" <<'PY'
import sys; p=sys.argv[1]; s=open(p).read()
a='    pgid = proc.pid\n    for sig in'
assert a in s; open(p,'w').write(s.replace(a,'    return\n'+a,1))
PY
        ;;
        noname)   sed -i 's/"the budget hit in: "/"the budget ran in: "/' "$f"; grep -q 'budget ran in' "$f" ;;
        nobudget) sed -i 's/budget=remaining)/budget=0)/' "$f"; grep -q 'budget=0)' "$f" ;;
    esac
}
restore() { git -C "$1" checkout -q -- "$G"; }

load_on()  { LOOPS=(); for _ in $(seq 1 16); do
                 python3 -c "while True: pass" & LOOPS+=($!); done; sleep 2; }
load_off() { kill "${LOOPS[@]}" 2>/dev/null; wait "${LOOPS[@]}" 2>/dev/null; LOOPS=(); }
trap 'load_off' EXIT

declare -A T
NAMES=()
for a in "$@"; do T[${a%%=*}]=${a#*=}; NAMES+=("${a%%=*}"); done

echo "== quiet"; for i in 1 2 3; do for n in "${NAMES[@]}"; do one "$n" "${T[$n]}" quiet $i 0; done; done
echo "== m-nokill / m-noname / m-nobudget (quiet)"
for n in "${NAMES[@]}"; do
    t=${T[$n]}
    mutate "$t" nokill;   one "$n" "$t" m-nokill 1 0;   restore "$t"; reap "$t"
    mutate "$t" noname;   one "$n" "$t" m-noname 1 0;   one "$n" "$t" m-noname 2 0; restore "$t"
done
for n in "${NAMES[@]}"; do (t=${T[$n]}; mutate "$t" nobudget; one "$n" "$t" m-nobudget 1 0; restore "$t") & done; wait
echo "== load"; load_on
for i in $(seq 1 20); do for n in "${NAMES[@]}"; do one "$n" "${T[$n]}" load $i 19; done; done
for n in "${NAMES[@]}"; do mutate "${T[$n]}" noname; done
for i in $(seq 1 5); do for n in "${NAMES[@]}"; do one "$n" "${T[$n]}" m-noname-ld $i 19; done; done
for n in "${NAMES[@]}"; do restore "${T[$n]}"; done
load_off
echo "== file"
for n in "${NAMES[@]}"; do
    t=${T[$n]}; out=$(cd "$t" && [ "$(pwd)" = "$t" ] && python3 -m pytest tests/test_contest_harvest.py -n 4 -q -p no:cacheprovider 2>&1)
    rc=$?; o=fail; [ $rc -eq 0 ] && grep -q ' passed' <<<"$out" && ! grep -q failed <<<"$out" && o=pass
    printf '%s\tfile\t1\t%s\t%s\n' "$n" "$o" "$(tail -1 <<<"$out")" >> "$RES"
done
python3 "$(dirname "$0")/table_184.py" "$RES" | tee "$OUT/table.md"
