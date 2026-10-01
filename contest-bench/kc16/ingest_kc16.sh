#!/bin/bash
# ingest_kc16.sh <name> <patch>: worktree at ae124ea (under $KC16_ENTRIES, outside the repo) + git am, or git apply for a raw diff, + the mechanical checks
B=$(dirname "$(readlink -f "$0")"); R=$(git -C "$B" rev-parse --show-toplevel); ENT=${KC16_ENTRIES:-/tmp/kc16-bench/entries}
n=$1; p=$2; W=$ENT/$n
rm -rf "$W"; git -C $R worktree prune
git -C $R worktree add --detach "$W" ae124ea >/dev/null 2>&1 || { echo "$n: worktree failed"; exit 1; }
cd "$W"
echo "=== $n"
if grep -q '^From [0-9a-f]\{40\}' "$p"; then
  if ! git am --3way "$p" >/tmp/$n.am 2>&1; then echo "AM FAILED"; tail -5 /tmp/$n.am; git am --abort; fi
else
  echo "RAW DIFF (not format-patch): applying with epic-tasks/ and contest-bench/ excluded"
  git apply --exclude='epic-tasks/*' --exclude='contest-bench/*' "$p" >/tmp/$n.am 2>&1 || { echo "APPLY FAILED"; tail -5 /tmp/$n.am; }
  git add -A && git -c user.name=raw -c user.email=raw@x commit -qm "raw diff $n" 
fi
echo "commits: $(git rev-list --count ae124ea..HEAD)  author: $(git log -1 --format=%an)"
git log --oneline ae124ea..HEAD
echo "files:"; git diff --stat ae124ea HEAD | cat
echo "shrink: $(git diff ae124ea HEAD -- tools/auto/collect_bridge.py | wc -l) lines"
echo "runner tests removed lines: $(git diff ae124ea HEAD -- tests/test_contest_runner.py | grep -c '^-[^-]')"
echo "epic-tasks: $(git diff --name-only ae124ea HEAD -- epic-tasks | wc -l)  contest-bench: $(git diff --name-only ae124ea HEAD -- contest-bench | wc -l)"
echo -n "py310 import: "; python3 -c "import tools.contest.runner; import tools.contest.cli" 2>&1 | tail -1; python3 -c "import tools.contest.cli" 2>/dev/null && echo ok
echo -n "--help: "; python3 -m tools.contest run --help >/dev/null 2>&1 && echo ok || echo FAIL
echo -n "no-subcommand rc: "; python3 -m tools.contest >/dev/null 2>&1; echo $?
echo -n "tiers: "; python3 scripts/sync_test_tiers.py --check >/dev/null 2>&1 && echo clean || echo DIRTY
echo "AGENTS.md line: $(grep -c 'tools.contest run' AGENTS.md)"
grep -n '^def run_round' -A 4 tools/contest/runner.py | head -6
grep -n 'threading.Lock' tools/contest/runner.py | head -3
grep -n '^def \(main\|cmd_run\|intake\|export_patches\|agents_from_models\)' tools/contest/cli.py 2>/dev/null
