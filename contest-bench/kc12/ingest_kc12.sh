#!/bin/bash
# ingest.sh <name> <patch>: worktree at ae124ea + git am + mechanical checks
B=$(dirname "$(readlink -f "$0")"); R=/home/renat/Project/opensource/github/agent-offline/qwen25
n=$1; p=$2; W=$B/entries/$n
rm -rf "$W"; git -C $R worktree prune
git -C $R worktree add --detach "$W" ae124ea >/dev/null 2>&1 || { echo "$n: worktree failed"; exit 1; }
cd "$W"
echo "=== $n"
if ! git am --3way "$p" >"$B/logs/$n.am" 2>&1; then echo "AM FAILED"; tail -5 "$B/logs/$n.am"; git am --abort; fi
echo "commits: $(git rev-list --count ae124ea..HEAD)  author: $(git log -1 --format=%an)"
git log --oneline ae124ea..HEAD
echo "files:"; git diff --stat ae124ea HEAD | cat
echo "shrink: $(git diff ae124ea HEAD -- tools/auto/collect_bridge.py | wc -l) lines"
echo "runner tests diff: $(git diff ae124ea HEAD -- tests/test_contest_runner.py | wc -l) lines"
echo "kc1 tests removed lines: $(git diff ae124ea HEAD -- tests/test_contest_kilo_client.py | grep -c '^-[^-]')"
echo "epic-tasks: $(git diff --name-only ae124ea HEAD -- epic-tasks | wc -l)"
echo -n "py310 compile: "; python3 -c "import tools.contest.kilo_client, tools.contest.runner" && echo ok
echo -n "tiers: "; python3 scripts/sync_test_tiers.py --check >/dev/null 2>&1 && echo clean || echo DIRTY
echo "silence_watch: $(grep -c '_silence_watch' tools/contest/runner.py)  inspect: $(grep -c 'inspect' tools/contest/runner.py)"
echo "client 300/contest.ini new: $(git diff ae124ea HEAD -- tools/contest/kilo_client.py | grep '^+' | grep -c 'contest.ini\|300')"
echo "pause_before_idle_sec in fake: $(grep -c pause_before_idle_sec tests/_kilo_fake.py)"
grep -n 'def wait_idle' -A 3 tools/contest/kilo_client.py | head -4
