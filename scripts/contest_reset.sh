#!/usr/bin/env bash
# Stages a contest round: one worktree or clone per agent at the base commit.

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
