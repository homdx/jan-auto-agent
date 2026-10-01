#!/usr/bin/env bash
# contest_reset.sh — the operator's one-liner for staging a contest round.
#
# Usage:
#   scripts/contest_reset.sh <round> [base_ref] [--clone name=path …] [--force-clone]
#
# Delegates to tools/contest/workspace.py (python3 -m tools.contest.workspace
# prepare), which prepares one worktree (or clone) per agent at the base commit
# and prints one line per agent:
#   laguna  ../rounds/40-laguna  contest/40/laguna  @ <base_ref>  (worktree, fresh)
# The last word is fresh | reset | clone.
#
# Never `git push`; never modifies the repo's own checkout.

set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "usage: scripts/contest_reset.sh <round> [base_ref] [--clone name=path …] [--force-clone]" >&2
    exit 2
fi

ROUND="$1"
shift

BASE_REF="HEAD"
CLONES=()
FORCE=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --clone)
            CLONES+=("$2")
            shift 2
            ;;
        --force-clone)
            FORCE="--force-clone"
            shift
            ;;
        --*)
            echo "unknown option: $1" >&2
            exit 2
            ;;
        *)
            BASE_REF="$1"
            shift
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_DIR"

ARGS=(prepare --repo . --config contest.ini --round "$ROUND" "$BASE_REF" $FORCE)
for c in "${CLONES[@]:-}"; do
    [ -n "$c" ] && ARGS+=(--clone "$c")
done

exec python3 -m tools.contest.workspace "${ARGS[@]}"
