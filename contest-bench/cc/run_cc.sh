#!/usr/bin/env bash
# CC-0: the claim-check bench, live. Runs today's scripts/claim_vote.py over the
# fixture's 80 claims and the 30 real ones and prints the score_cc.py table.
# The only network is the voters' own calls; nothing spends Lenz credits.
#
#   contest-bench/cc/run_cc.sh                         # voters: [claim_vote] llm_profiles
#   contest-bench/cc/run_cc.sh --profiles a b c        # three voters, three families
#   RUNS=1 contest-bench/cc/run_cc.sh --profiles a b c
#   contest-bench/cc/run_cc.sh --target --profiles a b c   # CC-6: each set judged on its tree's evidence
#
# Everything lands in claim-check-out/cc-<UTC>/: the claim lists, the built
# fixture, votes_<set>.json, vote_<set>.log and score.txt.
set -uo pipefail
# CC-6: --target (first or anywhere) is ours, not claim_vote's: each set is judged at its own
# pinned tree (the fixture at base, this repository at real_sha), so it cannot be passed on as is
target=0; args=()
for a in "$@"; do
    if [ "$a" = --target ]; then target=1; else args+=("$a"); fi
done
set -- "${args[@]+"${args[@]}"}"
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
out="$repo/claim-check-out/cc-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$out"
cd "$repo" || exit 1

# the fixture, built fresh; the voters' needs_code rule reads symbols from its base tree
python3 "$here/make_fixture.py" --build "$out/fixture-repo" | tee "$out/fixture.log" || exit 1
base=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['base_sha'])" "$here/claims_fixture.json")
real=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['real_sha'])" "$here/claims_real.json")
# git checkouts, not exports: CC-1's classifier resolves a commit claim through git
git clone -q --shared "$out/fixture-repo" "$out/fixture-base" && git -C "$out/fixture-base" checkout -q "$base"
git clone -q --shared "$repo" "$out/real-tree" && git -C "$out/real-tree" checkout -q "$real" \
    || { echo "run_cc: $real is not in this repository"; exit 2; }

status=0
for set in fixture real; do
    src="$here/claims_$set.json"
    roots=("$out/fixture-base"); [ "$set" = real ] && roots=("$out/real-tree")
    # claim_vote.py reads a bare list of {claim, truth}; the key keeps its own file
    python3 -c "import json,sys; json.dump(json.load(open(sys.argv[1]))['claims'], open(sys.argv[2],'w'), ensure_ascii=False, indent=1)" \
        "$src" "$out/claims_$set.json"
    echo "== vote: $set ($(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$out/claims_$set.json") claims)"
    on=(); [ "$target" = 1 ] && on=(--target "${roots[0]}" --ref HEAD --scratch "$out/scratch")
    python3 scripts/claim_vote.py "$out/claims_$set.json" --runs "${RUNS:-3}" \
        --symbols-root "${roots[0]}" "${on[@]+"${on[@]}"}" --out "$out/votes_$set.json" "$@" > "$out/vote_$set.log" 2>&1
    grep -E "^verdicts:|ERROR|quorum|warning" "$out/vote_$set.log" | head -5
    [ -s "$out/votes_$set.json" ] || { echo "run_cc: no votes for $set, see $out/vote_$set.log"; status=1; continue; }
    python3 "$here/score_cc.py" "$out/votes_$set.json" "$src" | tee -a "$out/score.txt"
    echo | tee -a "$out/score.txt"
done
echo "== done: $out (fixture base $base, real $real, sha of this checkout $(git rev-parse --short HEAD))"
exit $status
