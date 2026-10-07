#!/usr/bin/env bash
# The claim check end to end, one report in, logs and tables out:
#   extract (Lenz /extract, free, cached)  ->  vote (free models, ini profiles)
# Nothing here spends Lenz credits.  Everything lands in claim-check-out/<UTC>/:
#   claims.json  votes.json  extract.log  vote.log
#
#   scripts/run_claim_check.sh report.md                   # voters from [claim_vote] llm_profiles
#   RUNS=5 scripts/run_claim_check.sh report.md --profiles a b c
#
# LENZ_API_KEY is read from the environment, else asked for on the console.
set -uo pipefail
report="${1:?usage: run_claim_check.sh REPORT [claim_vote.py options]}"
shift
out="claim-check-out/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$out"
echo "== claim check of $report -> $out"
python3 scripts/claim_extract.py "$report" --out "$out/claims.json" 2>&1 | tee "$out/extract.log"
[ "${PIPESTATUS[0]}" -eq 0 ] || { echo "extract failed, see $out/extract.log"; exit 1; }
python3 scripts/claim_vote.py "$out/claims.json" --runs "${RUNS:-3}" --out "$out/votes.json" "$@" 2>&1 | tee "$out/vote.log"
echo "== done: $out/votes.json ($(grep -c . "$out/vote.log") log lines)"
