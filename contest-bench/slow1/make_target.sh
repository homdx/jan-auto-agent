#!/usr/bin/env bash
# contest-bench/slow1/make_target.sh — build the SLOW-1 target: our own repo
# cut at BASE, with NO commit after it anywhere (no branches, remotes, tags,
# reflog) so an agent cannot find the reference fix in `git log --all`.
# Ancestors stay, so bisecting backwards works. Then the ticket is committed.
#
# usage: contest-bench/slow1/make_target.sh [TARGET]   (default ../slow1-target)
set -euo pipefail
JAN=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
BASE=${BASE:-b5257cf}
TARGET=${1:-$JAN/../slow1-target}
SUBJECT="SLOW-1: ticket"

if [ -e "$TARGET" ]; then
  [ "$(git -C "$TARGET" log -1 --format=%s 2>/dev/null)" = "$SUBJECT" ] \
    || { echo "!! $TARGET exists and is not a slow1 target — refusing" >&2; exit 2; }
  rm -rf "$TARGET"
fi

git -C "$JAN" branch -f slow1-base-tmp "$BASE"
git clone -q --no-local --single-branch --branch slow1-base-tmp "$JAN" "$TARGET"
git -C "$JAN" branch -D -q slow1-base-tmp
cd "$TARGET"
git checkout -q -b main && git branch -D -q slow1-base-tmp
git remote remove origin
git tag -l | xargs -r git tag -d >/dev/null
git reflog expire --expire=now --all && git gc -q --prune=now

cp "$JAN/contest-bench/slow1/119-slow-1-tests-got-slower-find-the-commit-and-fix.md" epic-tasks/
git add epic-tasks && git commit -qm "$SUBJECT"

# proof: every commit is BASE's ancestor, except the ticket
n_all=$(git rev-list --all | wc -l); n_base=$(git rev-list "$BASE" | wc -l)
[ "$n_all" = $((n_base + 1)) ] || { echo "!! foreign commits in $TARGET" >&2; exit 1; }
echo "target ready: $TARGET @ $(git rev-parse --short HEAD) — $n_base commits up to $BASE, nothing after"
