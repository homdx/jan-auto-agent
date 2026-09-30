#!/usr/bin/env bash
# 2legs/prepare_task.sh — make any git repo ready for a multi-leg round.
#
# On a new branch `contest-legs` of the target repo it commits:
#   scripts/next_task.py, scripts/append_task.py   (the agents' prompt runs them)
#   epic-tasks/<NN>-<kind>.md                       (an open ticket from your brief)
# and nothing else. Your own branch is not touched; go back with
# `git -C <repo> checkout -`.
#
# The ticket declares no files (`**File:** —`), so harvest never reports
# off-ticket files; it still requires a test file, one commit and green tests.
#
# usage:
#   2legs/prepare_task.sh [--free] REPO_PATH "brief" [NN]
#   brief  plain text, or @file to read it from a file
#   --free the brief IS the task (coverage, docs, refactor ...); without it
#          the brief is wrapped in the bug-hunt template (find, test, fix)
#   NN     the ticket and round number inside that repo, default 1
set -euo pipefail
KIND=hunt
if [ "${1:-}" = "--free" ]; then KIND=free; shift; fi
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
JAN=$(cd "$HERE/.." && pwd)

REPO=${1:?usage: prepare_task.sh [--free] REPO_PATH "brief" [NN]}
BRIEF=${2:?usage: prepare_task.sh [--free] REPO_PATH "brief" [NN]}
case $BRIEF in @*) BRIEF=$(cat "${BRIEF#@}") ;; esac
NN=$(printf '%02d' "${3:-1}")
REPO=$(cd "$REPO" && pwd)

git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null
if [ -n "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]; then
  echo "!! $REPO has uncommitted changes — commit or stash them first" >&2
  exit 2
fi
if git -C "$REPO" rev-parse --verify -q contest-legs >/dev/null; then
  git -C "$REPO" checkout -q contest-legs
else
  git -C "$REPO" checkout -q -b contest-legs
fi

mkdir -p "$REPO/scripts" "$REPO/epic-tasks"
cp "$JAN/scripts/next_task.py" "$JAN/scripts/append_task.py" "$REPO/scripts/"
TICKET="$REPO/epic-tasks/$NN-$KIND.md"
if [ -e "$TICKET" ]; then
  echo "!! $TICKET exists — pick another NN" >&2
  exit 2
fi
if [ "$KIND" = hunt ]; then
cat > "$TICKET" <<EOF
# HUNT-$NN — Find one real bug, pin it with a test, fix it

**Status:** open
**Severity:** MEDIUM
**File:** —
**Symbol:** —
**Size:** M

## Brief

$BRIEF

## What must change

1. Read the code and find **one** real defect: wrong result, crash, data loss,
   a broken edge case. Not style, not "add error handling", not a refactor.
2. Write a test under \`tests/\` that **fails on the current code** and shows
   the defect in its name and assertion.
3. Fix the code with the smallest change that makes the test pass.
4. Run the whole test suite; it must stay green.
5. Make **one** local commit: subject \`fix: <what was wrong>\`, body with the
   input that broke, the wrong output and the right one.

If the relay hands you a worktree that already holds work, it is yours:
continue it, do not start a second bug.

## Acceptance

- [ ] a new test that fails without the fix and passes with it
- [ ] the full suite is green
- [ ] exactly one commit

## Ground rules
- One bug per entry. A second finding goes into the commit body as a note.
- No test calls a live service or the network.
- One local commit, no push.
EOF
else
cat > "$TICKET" <<EOF
# TASK-$NN — $(printf '%s' "$BRIEF" | head -1 | cut -c1-80)

**Status:** open
**Severity:** MEDIUM
**File:** —
**Symbol:** —
**Size:** M

## What must change

$BRIEF

If the relay hands you a worktree that already holds work, it is yours:
continue it, do not start over.

## Acceptance

- [ ] what the brief asks for, with tests under \`tests/\`
- [ ] the full suite is green
- [ ] exactly one commit

## Ground rules
- No test calls a live service or the network.
- One local commit, no push.
EOF
fi

# intake hands out the lowest open ticket: park every other open one
parked=()
for t in "$REPO"/epic-tasks/*.md; do
  [ "$t" = "$TICKET" ] && continue
  if grep -q '^\*\*Status:\*\* open' "$t"; then
    sed -i 's/^\*\*Status:\*\* open/**Status:** queued/' "$t"
    parked+=("epic-tasks/$(basename "$t")")
  fi
done
[ ${#parked[@]} -eq 0 ] || echo "parked (open -> queued): ${parked[*]}"

git -C "$REPO" add "${parked[@]}" scripts/next_task.py scripts/append_task.py "epic-tasks/$NN-$KIND.md"
git -C "$REPO" commit -q -m "contest: $KIND ticket $NN + contest scripts"
echo "ready: $REPO on branch contest-legs, ticket $NN"
echo "next:  cd $JAN && python3 -m tools.contest run --ticket $((10#$NN)) --target $REPO --legs 2 --models ..."
