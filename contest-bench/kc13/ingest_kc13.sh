#!/bin/bash
# ingest_kc13.sh <name> <patch>: worktree at 5ee8417 + git am + the ticket's mechanical checks.
# Worktrees go to ../cb-kc13/<name> (outside the repo tree); logs to ../cb-kc13/logs.
B=$(dirname "$(readlink -f "$0")"); R=$(git -C "$B" rev-parse --show-toplevel); BASE=5ee8417
n=$1; p=$(readlink -f "$2"); W=$R/../cb-kc13/$n; L=$R/../cb-kc13/logs; mkdir -p "$L"
rm -rf "$W"; git -C "$R" worktree prune
git -C "$R" worktree add --detach "$W" $BASE >/dev/null 2>&1 || { echo "$n: worktree failed"; exit 1; }
cd "$W"
echo "=== $n"
if ! git am --3way "$p" >"$L/$n.am" 2>&1; then echo "AM FAILED"; tail -5 "$L/$n.am"; git am --abort; fi
echo "commits: $(git rev-list --count $BASE..HEAD)  author: $(git log -1 --format=%an)"
git log --oneline $BASE..HEAD
echo "files:"; git diff --stat $BASE HEAD | cat
echo "shrink: $(git diff $BASE HEAD -- tools/auto/collect_bridge.py | wc -l) lines"
echo "epic-tasks: $(git diff --name-only $BASE HEAD -- epic-tasks | wc -l)"
echo "policy test removed lines: $(git diff $BASE HEAD -- tests/test_contest_policy.py | grep -c '^-[^-]')"
echo -n "py310 import: "; python3 -c "import tools.contest.policy" && echo ok
echo -n "tiers: "; python3 scripts/sync_test_tiers.py --check >/dev/null 2>&1 && echo clean || echo DIRTY
echo "shlex/subprocess new: $(git diff $BASE HEAD -- tools/contest/policy.py | grep '^+' | grep -c 'shlex\|subprocess')"
echo "progress row: $(grep -c . runs/*/PROGRESS.csv 2>/dev/null | tr '\n' ' ')"
echo -n "own policy tests: "; python3 -m pytest -q -o addopts="" -p no:cacheprovider tests/test_contest_policy.py 2>&1 | tail -1
echo -n "bench: "; KC13_REPO=$W python3 -m pytest -q -o addopts="" -p no:cacheprovider "$B/scenarios_kc13.py" 2>&1 | tail -1
