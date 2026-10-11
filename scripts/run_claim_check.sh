#!/usr/bin/env bash
# The claim check end to end, one report in, logs and tables out:
#   extract (Lenz /extract, free, cached)  ->  vote (free models, ini profiles)
#   [-> truth.csv + pending.md with --truth]
# Nothing here spends Lenz credits.  Everything lands in claim-check-out/<UTC>/:
#   claims.json  votes.json  extract.log  vote.log  [truth.csv  pending.md  truth.log]
#
#   scripts/run_claim_check.sh report.md                   # voters from [claim_vote] llm_profiles
#   RUNS=5 scripts/run_claim_check.sh report.md --profiles a b c
#   scripts/run_claim_check.sh report.md --target . --ref origin/kc --truth truth.csv   # CC-6 + CC-8
#   scripts/run_claim_check.sh report.md --target . --ref HEAD --truth t.csv --dry-run  # print, run nothing
#
# --target / --ref / --base / --fetch / --profiles / --models ... go to claim_vote.py as they are.
# --truth FILE (CC-8): claimcheck_truth.py at the end; a bare name lands in the output directory,
# pending.md always does.  --dry-run: print the commands and exit 0 — no network, nothing written.
# Both are taken out of the options before claim_vote.py sees them (it refuses what it does not know).
#
# LENZ_API_KEY is read from the environment, else asked for on the console.
set -uo pipefail
report="${1:?usage: run_claim_check.sh REPORT [--truth OUT.csv] [--dry-run] [claim_vote.py options]}"
shift
truth=""
dry=0
pass=()
while [ $# -gt 0 ]; do
    case "$1" in
        --truth) truth="${2:?--truth needs a file}"; shift 2 ;;
        --truth=*) truth="${1#--truth=}"; shift ;;
        --dry-run) dry=1; shift ;;
        *) pass+=("$1"); shift ;;
    esac
done
out="claim-check-out/$(date -u +%Y%m%dT%H%M%SZ)"
if [ -n "$truth" ] && [ "${truth#*/}" = "$truth" ]; then
    truth="$out/$truth"
fi
if [ "$dry" -eq 1 ]; then
    echo "== dry run: claim check of $report -> $out (nothing is run or written)"
    printf '%q ' python3 scripts/claim_extract.py "$report" --out "$out/claims.json"; echo
    printf '%q ' python3 scripts/claim_vote.py "$out/claims.json" --runs "${RUNS:-3}" --out "$out/votes.json" ${pass[@]+"${pass[@]}"}; echo
    if [ -n "$truth" ]; then
        printf '%q ' python3 scripts/claimcheck_truth.py "$out/votes.json" --out "$truth" --pending "$out/pending.md"; echo
    fi
    exit 0
fi
mkdir -p "$out"
echo "== claim check of $report -> $out"
python3 scripts/claim_extract.py "$report" --out "$out/claims.json" 2>&1 | tee "$out/extract.log"
[ "${PIPESTATUS[0]}" -eq 0 ] || { echo "extract failed, see $out/extract.log"; exit 1; }
python3 scripts/claim_vote.py "$out/claims.json" --runs "${RUNS:-3}" --out "$out/votes.json" ${pass[@]+"${pass[@]}"} 2>&1 | tee "$out/vote.log"
echo "== done: $out/votes.json ($(grep -c . "$out/vote.log") log lines)"
if [ -n "$truth" ]; then
    [ -s "$out/votes.json" ] || { echo "no votes.json, no truth.csv: see $out/vote.log"; exit 1; }
    python3 scripts/claimcheck_truth.py "$out/votes.json" --out "$truth" --pending "$out/pending.md" 2>&1 | tee "$out/truth.log"
    rc="${PIPESTATUS[0]}"
    echo "== truth: $truth, pending: $out/pending.md (exit $rc)"
    exit "$rc"
fi
