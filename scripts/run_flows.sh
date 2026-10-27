#!/usr/bin/env bash
# Runs the skill flows, each in its OWN sandbox, then validates them with check_runbook.py (usage: [N] [--check-only] [--keep] [--config FILE]).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

PYTHON="${PYTHON:-python3}"
CONFIG="${CONFIG:-agents_32k.ini}"
RUNNER="${RUNNER:-}"          # e.g. RUNNER=proxychains4
KEEP=0
CHECK_ONLY=0
TASKS=()

GOAL_1="Harden main.py: docstrings, type hints, a pytest test"
GOAL_2="Write user documentation for this project"
# RUN-11: not "for the greeting" — the seed entry "### The first greeting" already tells that story, so
# every honest new entry retold it, Gate 2 called the two a duplicate and the coder dropped the seed.
GOAL_3="Write a narrative changelog entry for the runbook"
GOAL_4="Write a narrative changelog entry for the runbook"

SKILL_1="hello-code"
SKILL_2="hello-docs"
SKILL_3="hello-creative"
SKILL_4="hello-creative-split"

BASELINE_FILES=(main.py README.md CHANGELOG.md RUNBOOK.md)

while [ $# -gt 0 ]; do
    case "$1" in
        1|2|3|4)       TASKS+=("$1") ;;
        --config)      CONFIG="$2"; shift ;;
        --keep)        KEEP=1 ;;
        --check-only)  CHECK_ONLY=1 ;;
        --runner)      RUNNER="$2"; shift ;;
        -h|--help)     sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done
[ ${#TASKS[@]} -eq 0 ] && TASKS=(1 2 3 4)

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*"; }
ok()   { printf '\033[32m%s\033[0m\n' "$*"; }

# ── preflight ────────────────────────────────────────────────────────────────
if [ ! -f "$CONFIG" ]; then
    fail "config not found: $CONFIG"
    exit 2
fi

# Skills declare min_num_ctx = 16384; checking here costs a second, checking inside a run costs the whole run.
if ! "$PYTHON" - "$CONFIG" <<'PY'
import configparser, sys
cfg = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
cfg.read(sys.argv[1], encoding="utf-8")
active = cfg.get("api", "active", fallback="local")
section = f"api_{active}" if cfg.has_section(f"api_{active}") else "api_local"
try:
    num_ctx = cfg.getint(section, "num_ctx", fallback=0)
except ValueError:
    num_ctx = 0
if num_ctx < 16384:
    print(f"{sys.argv[1]}: num_ctx={num_ctx} — the skills require >= 16384.")
    print("Use agents_32k.ini or larger, or pass --config <profile>.")
    sys.exit(1)
PY
then
    exit 2
fi

reset_sandbox() {
    local task="$1"
    local dir="examples/task${task}/hello-world"
    if [ "$KEEP" -eq 1 ] && [ -d "$dir" ]; then
        warn "  keeping existing $dir"
        return 0
    fi
    rm -rf "$dir"
    mkdir -p "$dir"
    local f
    for f in "${BASELINE_FILES[@]}"; do
        if git show "HEAD:examples/hello-world/$f" > "$dir/$f" 2>/dev/null; then
            :
        else
            rm -f "$dir/$f"
        fi
    done
    if [ ! -f "$dir/main.py" ]; then
        fail "  could not restore baseline into $dir (is HEAD missing examples/hello-world?)"
        return 1
    fi
    return 0
}

run_flow() {
    local task="$1"
    local goal skill dir
    eval "goal=\$GOAL_$task"
    eval "skill=\$SKILL_$task"
    dir="examples/task${task}/hello-world"

    say "Task $task — $skill"
    reset_sandbox "$task" || return 1

    local start elapsed
    start=$(date +%s)
    # shellcheck disable=SC2086
    $RUNNER "$PYTHON" main.py --auto "$goal" \
        --base "$dir" --config "$CONFIG" --skill "$skill" \
        > "examples/task${task}/console-log.txt" 2>&1
    local rc=$?
    elapsed=$(( $(date +%s) - start ))

    if [ $rc -ne 0 ]; then
        fail "  run exited $rc after ${elapsed}s — see examples/task${task}/console-log.txt"
    else
        ok "  run finished in ${elapsed}s"
    fi
    return $rc
}

# ── run ──────────────────────────────────────────────────────────────────────
RUN_FAILED=()
if [ "$CHECK_ONLY" -eq 0 ]; then
    for t in "${TASKS[@]}"; do
        run_flow "$t" || RUN_FAILED+=("$t")
    done
else
    warn "--check-only: skipping the runs"
fi

# ── validate ─────────────────────────────────────────────────────────────────
say "Validation (deterministic — no LLM)"
CHECK_FAILED=()
for t in "${TASKS[@]}"; do
    if ! "$PYTHON" scripts/check_runbook.py --task "$t"; then
        CHECK_FAILED+=("$t")
    fi
done

# ── summary ──────────────────────────────────────────────────────────────────
say "Summary"
for t in "${TASKS[@]}"; do
    ran="ok"; checked="ok"
    # --check-only runs nothing; reporting "ok" there would read as a successful run that never happened.
    [ "$CHECK_ONLY" -eq 1 ] && ran="skipped"
    [[ " ${RUN_FAILED[*]-} " == *" $t "* ]] && ran="FAILED"
    [[ " ${CHECK_FAILED[*]-} " == *" $t "* ]] && checked="FAILED"
    printf '  task %s: run=%-7s validate=%s\n' "$t" "$ran" "$checked"
done

if [ ${#RUN_FAILED[@]} -eq 0 ] && [ ${#CHECK_FAILED[@]} -eq 0 ]; then
    ok "All ${#TASKS[@]} flow(s) ran and validated."
    exit 0
fi
fail "Failures: run=${RUN_FAILED[*]-none} validate=${CHECK_FAILED[*]-none}"
exit 1
