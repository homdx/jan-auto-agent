#!/bin/bash
# judge_round.sh — judge a round's candidate patches locally, one after another.
#
#   scripts/judge_round.sh 88                        every contest-out/88/*.patch
#   scripts/judge_round.sh 88 fl5-88-ideal.patch 56/ ...plus extra patches / folders of patches
#   QUICK=1 scripts/judge_round.sh 88                apply + scope checks only, no pytest
#   JOBS=8 scripts/judge_round.sh 88                 pytest -n 8 instead of 4
#
# scripts/judge_epic_round.py scores agents' worktrees as they stand; this one
# takes the handed-in .patch files and checks that they land on today's kc.
#
# Each patch is applied with `git am` on a fresh detached worktree of the local
# `kc` HEAD (what the patch must land on), then:
#   commits above base · files touched · CollectBridge._shrink byte-identical ·
#   pytest tests -n 4 · pytest tests_bugfix -n 4 (sequential, never in parallel) ·
#   sync_test_tiers.py --check
# The main checkout is never touched, nothing is committed or pushed, and the
# worktrees are removed at the end. Results: $OUT/summary.txt, logs per patch.
set -u
# The main checkout (where contest-out/ lives), even when run from a worktree.
REPO=$(dirname "$(git -C "$(dirname "$(realpath "$0")")" rev-parse --path-format=absolute --git-common-dir)")
BASE=${BASE:-kc}
JOBS=${JOBS:-4}   # pytest -n; JOBS=8 when the box is free
round=${1:?usage: judge_round.sh ROUND [extra.patch|dir/ ...]}; shift
OUT=${OUT:-/tmp/judge-$round}
mkdir -p "$OUT/wt"

patches=("$REPO"/contest-out/"$round"/*.patch)
for x in "$@"; do
  x=$(realpath "$x"); [ -d "$x" ] && patches+=("$x"/*.patch) || patches+=("$x")
done

base_sha=$(git -C "$REPO" rev-parse "$BASE")
shrink() {  # source of CollectBridge._shrink in collect_bridge.py file $1
  python3 - "$1" <<'EOF'
import ast, sys
s = open(sys.argv[1]).read()
for n in ast.walk(ast.parse(s)):
    if isinstance(n, ast.ClassDef) and n.name == "CollectBridge":
        for f in n.body:
            if getattr(f, "name", "") == "_shrink":
                print(ast.get_source_segment(s, f))
EOF
}
# From the base commit, not the main checkout's tree: that may sit on a round branch.
git -C "$REPO" show "$base_sha":tools/auto/collect_bridge.py >"$OUT/base_collect_bridge.py"
base_shrink=$(shrink "$OUT/base_collect_bridge.py" | md5sum)

echo "base $BASE = ${base_sha:0:7}   out: $OUT" | tee "$OUT/summary.txt"
for p in "${patches[@]}"; do
  [ -f "$p" ] || continue
  name=$(basename "$p" .patch); wt="$OUT/wt/$name"; log="$OUT/$name.log"
  git -C "$REPO" worktree remove --force "$wt" 2>/dev/null
  git -C "$REPO" worktree add -q --detach "$wt" "$base_sha"
  if ! git -C "$wt" am -q --whitespace=nowarn "$p" >"$log" 2>&1; then
    git -C "$wt" am --abort 2>/dev/null
    echo "$name | DOES NOT APPLY on ${base_sha:0:7} (see $log)" | tee -a "$OUT/summary.txt"
    git -C "$REPO" worktree remove --force "$wt"; continue
  fi
  n=$(git -C "$wt" rev-list --count "$base_sha"..HEAD)
  files=$(git -C "$wt" diff --name-only "$base_sha"..HEAD | grep -v '^\.smoke_tests/\|^\.regression_tests/' | tr '\n' ' ')
  bad=$(git -C "$wt" diff --name-only "$base_sha"..HEAD | grep -E '^(epic-tasks|fl2|contest-out|ground|kc[0-9]+)/|\.patch$' | tr '\n' ' ')
  [ "$(shrink "$wt/tools/auto/collect_bridge.py" | md5sum)" = "$base_shrink" ] && sh=ok || sh=CHANGED
  row="$name | commits:$n | _shrink:$sh${bad:+ | FORBIDDEN: $bad}"
  if [ -z "${QUICK:-}" ]; then
    t=$(cd "$wt" && python3 -m pytest tests -n "$JOBS" -q -p no:cacheprovider 2>&1 | tee -a "$log" | tail -1)
    b=$(cd "$wt" && python3 -m pytest tests_bugfix -n "$JOBS" -q -p no:cacheprovider 2>&1 | tee -a "$log" | tail -1)
    c=$(cd "$wt" && python3 scripts/sync_test_tiers.py --check 2>&1 | tail -1)
    row="$row | tests: $t | bugfix: $b | $c"
  fi
  echo "$row" | tee -a "$OUT/summary.txt"
  echo "    files: $files" | tee -a "$OUT/summary.txt"
  git -C "$REPO" worktree remove --force "$wt"
done
git -C "$REPO" worktree prune
