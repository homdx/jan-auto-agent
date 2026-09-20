#!/bin/bash
# ingest_kc14.sh <name> <patch>: worktree at 0f4eb02 + git am + the ticket's mechanical checks.
# Worktrees go to ../cb-kc14/<name> (outside the repo tree); logs to ../cb-kc14/logs.
# The pytest lines run one at a time — never two entries at once (the roots collide on ports).
B=$(dirname "$(readlink -f "$0")"); R=$(git -C "$B" rev-parse --show-toplevel); BASE=0f4eb02
n=$1; p=$(readlink -f "$2"); W=$R/../cb-kc14/$n; L=$R/../cb-kc14/logs; mkdir -p "$L"
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
echo "harvest test removed lines: $(git diff $BASE HEAD -- tests/test_contest_harvest.py | grep -c '^-[^-]')"
echo "runner tests diff: $(git diff $BASE HEAD -- tests/test_contest_runner.py | wc -l) lines"
echo "judge script diff: $(git diff $BASE HEAD -- scripts/judge_epic_round.py | wc -l) lines"
echo -n "py310 import: "; python3 -c "import tools.contest.harvest" && echo ok
echo -n "tiers: "; python3 scripts/sync_test_tiers.py --check >/dev/null 2>&1 && echo clean || echo DIRTY
echo "subprocess.run in harvest.py: $(grep -c 'subprocess.run' tools/contest/harvest.py)  _is_ancestor kept: $(grep -c 'def _is_ancestor' tools/contest/harvest.py)"
echo -n "REASON_CODES: "; python3 -c "from tools.contest.harvest import REASON_CODES as R; print(len(R), R == ('no_progress_row', 'progress_not_done', 'no_commit', 'commit_not_on_branch', 'commits_ne_1', 'pushed', 'no_test_file', 'shrink_changed', 'off_ticket_files', 'tests_failed'))"
echo -n "own harvest tests: "; python3 -m pytest -q -o addopts="" -p no:cacheprovider tests/test_contest_harvest.py 2>&1 | tail -1
echo -n "base runner tests: "; python3 -m pytest -q -o addopts="" -p no:cacheprovider tests/test_contest_runner.py 2>&1 | tail -1
echo -n "bench: "; KC14_REPO=$W python3 -m pytest -q -o addopts="" -p no:cacheprovider "$B/scenarios_kc14.py" 2>&1 | tail -1
