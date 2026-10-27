#!/usr/bin/env bash
# CC-0: claim-check bench, live, over the fixture and the real claims.
set -uo pipefail
# CC-6: --target is handled here, not by claim_vote: each set is judged at its own pinned tree (fixture at base, this repo at real_sha).
target=0; diff=0; args=()
for a in "$@"; do
    if [ "$a" = --target ]; then target=1; elif [ "$a" = --diff ]; then diff=1; else args+=("$a"); fi
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

if [ "$diff" = 1 ]; then
    # CC-7: the fixture's claims at its base and head shas; ~650 requests with 3 voters and RUNS=3
    head=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['head_sha'])" "$here/claims_fixture.json")
    python3 scripts/claim_diff.py "$here/claims_fixture.json" --target "$out/fixture-repo" --base "$base" \
        --head "$head" --runs "${RUNS:-3}" --scratch "$out/scratch" --out "$out/diff" "$@" 2> "$out/diff.log"
    code=$?
    tail -3 "$out/diff.log"
    [ -s "$out/diff/delta.json" ] || { echo "run_cc: no delta for the fixture, see $out/diff.log"; exit 1; }
    # thresholds.json says "enforced from CC-6 on", and nothing passed it: a miss is exit 1 now
    python3 "$here/score_cc.py" "$out/diff/delta.json" "$here/claims_fixture.json" \
        --thresholds "$here/thresholds.json" | tee "$out/score_diff.txt"
    [ "${PIPESTATUS[0]}" -ne 0 ] && [ "$code" -eq 0 ] && code=1
    echo "== done: $out (diff $base -> $head)"
    exit $code
fi

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
    # the targets are for votes cast on the evidence (CC-6 on); the no-evidence baseline is 0 by design
    th=(); [ "$target" = 1 ] && th=(--thresholds "$here/thresholds.json")
    python3 "$here/score_cc.py" "$out/votes_$set.json" "$src" "${th[@]+"${th[@]}"}" | tee -a "$out/score.txt"
    [ "${PIPESTATUS[0]}" -ne 0 ] && status=1
    echo | tee -a "$out/score.txt"
done
echo "== done: $out (fixture base $base, real $real, sha of this checkout $(git rev-parse --short HEAD))"
exit $status
