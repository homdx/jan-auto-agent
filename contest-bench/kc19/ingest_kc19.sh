#!/bin/bash
# ingest_kc19.sh <name> <patch-or-diff>: worktree at c27bc32 + the entry + the ticket's mechanical checks.
# A `.patch` (git format-patch) goes in with `git am`; a `.diff` (a STALLED worktree's `git diff`) with
# `git apply` + one commit, so every entry is "one commit over the base" for the checks below.
# Worktrees go to ../cb-kc19/<name> (outside the repo tree); logs to ../cb-kc19/logs.
# The pytest lines run one at a time — never two entries at once (the roots collide on ports).
B=$(dirname "$(readlink -f "$0")"); R=$(git -C "$B" rev-parse --show-toplevel); BASE=c27bc32
n=$1; p=$(readlink -f "$2"); W=$R/../cb-kc19/$n; L=$R/../cb-kc19/logs; mkdir -p "$L"
rm -rf "$W"; git -C "$R" worktree prune
git -C "$R" worktree add --detach "$W" $BASE >/dev/null 2>&1 || { echo "$n: worktree failed"; exit 1; }
cd "$W"
echo "=== $n"
case "$p" in
  *.patch) git am --3way "$p" >"$L/$n.am" 2>&1 || { echo "AM FAILED"; tail -5 "$L/$n.am"; git am --abort; } ;;
  *)       git apply "$p" >"$L/$n.am" 2>&1 && git add -A && git commit -q -m "KC-19 (STALLED worktree diff, committed by the judge)" \
             || { echo "APPLY FAILED"; tail -5 "$L/$n.am"; } ;;
esac
echo "commits: $(git rev-list --count $BASE..HEAD)  author: $(git log -1 --format=%an)"
git log --oneline $BASE..HEAD | cat
echo "files:"; git diff --stat $BASE HEAD | cat
echo "shrink: $(git diff $BASE HEAD -- tools/auto/collect_bridge.py | wc -l) lines"
echo "epic-tasks: $(git diff --name-only $BASE HEAD -- epic-tasks | wc -l)"
echo "fake diff: $(git diff $BASE HEAD -- tests/_kilo_fake.py | wc -l) lines"
echo "runner test removed lines: $(git diff $BASE HEAD -- tests/test_contest_runner.py | grep -c '^-[^-]')"
echo "judge script diff: $(git diff $BASE HEAD -- scripts/judge_epic_round.py | wc -l) lines"
echo -n "py310 import: "; python3 -c "import tools.contest.runner, tools.contest.roster" && echo ok
echo -n "tiers: "; python3 scripts/sync_test_tiers.py --check >/dev/null 2>&1 && echo clean || echo DIRTY
echo -n "symbols: "; python3 - <<'PY'
import inspect, tools.contest.runner as r, tools.contest.roster as ro
print("RETRY_PROMPT" if isinstance(getattr(r, "RETRY_PROMPT", None), str) else "no RETRY_PROMPT",
      "|", "_retryable" + str(inspect.signature(r._retryable)) if hasattr(r, "_retryable") else "no _retryable",
      "|", "run_agent" + str(inspect.signature(r.run_agent)),
      "|", [k for k in ro.CONTEST_KEYS if "retr" in k])
PY
echo "time.sleep in runner.py: $(grep -c 'time\.sleep' tools/contest/runner.py)  (base: 0)"
echo -n "own roster tests: "; python3 -m pytest -q -o addopts="" -p no:cacheprovider tests/test_contest_roster.py 2>&1 | tail -1
echo -n "own runner tests: "; python3 -m pytest -q -o addopts="" -p no:cacheprovider tests/test_contest_runner.py --timeout=180 2>&1 | tail -1
echo -n "cli tests: "; python3 -m pytest -q -o addopts="" -p no:cacheprovider tests/test_contest_cli.py --timeout=180 2>&1 | tail -1
echo -n "bench: "; KC19_REPO=$W python3 -m pytest -q -o addopts="" -p no:cacheprovider "$B/scenarios_kc19.py" --timeout=120 2>&1 | tail -1
