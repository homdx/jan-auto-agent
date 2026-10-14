#!/usr/bin/env bash
# Runs the claim check end to end: extract, vote, then the truth report.
set -uo pipefail
report="${1:?usage: run_claim_check.sh REPORT [--truth OUT.csv [--append|--force]] [--dry-run] [--no-lenz-hint] [claim_vote.py options]}"
shift
truth=""
dry=0
force=0
hint=()
pass=()
while [ $# -gt 0 ]; do
    case "$1" in
        --truth) truth="${2:?--truth needs a file}"; shift 2 ;;
        --truth=*) truth="${1#--truth=}"; shift ;;
        --dry-run) dry=1; shift ;;
        --no-lenz-hint) hint=(--no-lenz-hint); shift ;;
        --append) shift ;;          # the default for an existing --truth file; claim_vote.py would refuse it
        --force) force=1; shift ;;
        *) pass+=("$1"); shift ;;
    esac
done
out="claim-check-out/$(date -u +%Y%m%dT%H%M%SZ)"
if [ -n "$truth" ] && [ "${truth#*/}" = "$truth" ]; then
    truth="$out/$truth"
fi
append=()
if [ -n "$truth" ] && [ -e "$truth" ] && [ "$force" -eq 0 ]; then
    append=(--append)
fi
if [ "$dry" -eq 1 ]; then
    echo "== dry run: claim check of $report -> $out (nothing is run or written)"
    printf '%q ' python3 scripts/claim_extract.py "$report" --out "$out/claims.json"; echo
    printf '%q ' python3 scripts/claim_vote.py "$out/claims.json" --runs "${RUNS:-3}" --out "$out/votes.json" ${pass[@]+"${pass[@]}"}; echo
    if [ -n "$truth" ]; then
        printf '%q ' python3 scripts/claimcheck_truth.py "$out/votes.json" --out "$truth" --pending "$out/pending.md" ${append[@]+"${append[@]}"} ${hint[@]+"${hint[@]}"}; echo
    fi
    exit 0
fi
mkdir -p "$out"
echo "== claim check of $report -> $out"
python3 scripts/claim_extract.py "$report" --out "$out/claims.json" 2>&1 | tee "$out/extract.log"
[ "${PIPESTATUS[0]}" -eq 0 ] || { echo "extract failed, see $out/extract.log"; exit 1; }
# No claims: end here, since claim_vote.py refuses an empty claims file.
if [ "$(tr -d ' \t\r\n' < "$out/claims.json")" = "[]" ]; then      # no python3 of its own: the pipeline runs three
    echo "== no claims in $report — nothing to vote on"
    exit 0
fi
python3 scripts/claim_vote.py "$out/claims.json" --runs "${RUNS:-3}" --out "$out/votes.json" ${pass[@]+"${pass[@]}"} 2>&1 | tee "$out/vote.log"
vote_rc="${PIPESTATUS[0]}"
# the vote's own exit code: a refused --target or a crash used to end in "== done" and exit 0
if [ "$vote_rc" -ne 0 ] || [ ! -s "$out/votes.json" ]; then
    echo "== vote failed (exit $vote_rc), see $out/vote.log"
    [ "$vote_rc" -ne 0 ] && exit "$vote_rc"
    exit 1
fi
echo "== done: $out/votes.json ($(grep -c . "$out/vote.log") log lines)"
if [ -n "$truth" ]; then
    [ "${#append[@]}" -gt 0 ] && echo "== truth: $truth exists, appending (--force replaces it)"
    python3 scripts/claimcheck_truth.py "$out/votes.json" --out "$truth" --pending "$out/pending.md" ${append[@]+"${append[@]}"} ${hint[@]+"${hint[@]}"} 2>&1 | tee "$out/truth.log"
    rc="${PIPESTATUS[0]}"
    echo "== truth: $truth, pending: $out/pending.md (exit $rc)"
    exit "$rc"
fi
