#!/usr/bin/env bash
# 2legs/run_2legs.sh — KC-77: run both legs of the two-leg runbook, then check.
#
#   leg 1  round 1, ticket 01 (__repr__) on a freshly built $TARGET
#   land   the first READY agent whose patch applies and keeps tests green
#   leg 2  round 2, ticket 02 (__eq__) on top of leg 1's winner
#   land   the same way
#   check  2legs/check_2legs.py — deterministic, no LLM, no network
#
# The pick is mechanical on purpose: this is the runbook's test, not the
# judging. HOW-WE-RUN-2LEGS.md is the hand version where you read patches.
#
# Models are never stored in git: export MODELS=a,b,c before running.
#
# usage:
#   source 2legs/env.sh && export MODELS=provider/model-a,provider/model-b
#   2legs/run_2legs.sh                 # rebuild target, both legs, check
#   2legs/run_2legs.sh --keep          # keep $TARGET as it is (resume by hand)
#   2legs/run_2legs.sh --check-only    # validate the previous run only
#
# Exit code is 0 only when both legs ran AND every check passed.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$HERE/env.sh"

CHECK_ONLY=0 KEEP=0
for a in "$@"; do
  case $a in
    --check-only) CHECK_ONLY=1 ;;
    --keep) KEEP=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

WINNERS=$TARGET/contest-out/winners.txt

run_round() {  # ticket out
  (cd "$JAN" && python3 -m tools.contest run --ticket "$1" --target "$TARGET" \
      --roster "$JAN/contest.ini" --models "$MODELS" --fresh --out "$2")
}

land() {  # ticket out
  local n=$1 out=$2 agent
  for agent in $(python3 -c "import json,sys
for a in json.load(open(sys.argv[1]))['agents']:
    if a['state'] == 'READY': print(a['agent']['name'])" "$out/state.json"); do
    if ! git -C "$TARGET" am -q "$out/$agent.patch" 2>/dev/null; then
      git -C "$TARGET" am --abort 2>/dev/null || true
      echo "   $agent: patch does not apply"; continue
    fi
    if (cd "$TARGET" && python3 -m pytest -q -p no:xdist -p no:cacheprovider tests >/dev/null 2>&1); then
      echo "$n $agent $(git -C "$TARGET" rev-parse HEAD)" >> "$WINNERS"
      sed -i 's/^\*\*Status:\*\* open/**Status:** landed/' "$TARGET"/epic-tasks/0"$n"-*.md
      git -C "$TARGET" add epic-tasks
      git -C "$TARGET" commit -qm "KC-EXT-0$n landed"
      echo ">> leg $n winner: $agent"
      return 0
    fi
    echo "   $agent: tests red after landing"
    git -C "$TARGET" reset -q --hard HEAD~1
  done
  echo "!! leg $n: no READY agent landed cleanly" >&2
  return 1
}

if [ "$CHECK_ONLY" = 0 ]; then
  [ -n "${MODELS:-}" ] || { echo "!! export MODELS=... first (HOW-WE-RUN-2LEGS.md step 0 #4)" >&2; exit 2; }
  [ -f "$JAN/contest.local.ini" ] || echo "!! no $JAN/contest.local.ini — copy it by hand (keys, gate model)" >&2
  [ "$KEEP" = 1 ] || "$HERE/make_target.sh"
  mkdir -p "$TARGET/contest-out" && : > "$WINNERS"
  echo "== leg 1: ticket 01"; run_round 1 "$TARGET/contest-out/01"
  land 1 "$TARGET/contest-out/01"
  echo "== leg 2: ticket 02"; run_round 2 "$TARGET/contest-out/02.1"
  land 2 "$TARGET/contest-out/02.1"
fi
exec python3 "$HERE/check_2legs.py" "$TARGET"
