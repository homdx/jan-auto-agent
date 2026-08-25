# hello-world runbook

Three skill flows, one per mechanical base. Every command below has been run
against this repository; the "what to expect" sections describe what actually
happened, not what should happen in theory.

`tests/test_hello_world_runbook.py` parses this file and checks each command
is still valid — the skill exists, the profile exists and clears the skill's
context budget, the flags are real, and the documented gate sets match the
adapters. If you edit a command here, run that test.

## Before you start

Reset the sandbox. These flows commit to a git repo inside
`examples/hello-world/`, so a second run starts from the first run's output
and you will be validating the wrong thing:

```bash
git checkout -- examples/hello-world/
rm -rf examples/hello-world/.agent examples/hello-world/.git
rm -f examples/hello-world/*.coder.bak examples/hello-world/test_main.py
```

`main.py` must print `Hello world` and contain no `sys.exit`, and
`CHANGELOG.md` must still hold the `### The first greeting` seed entry.
Both are the baseline the gates measure against.

## Choosing a profile

Skills need room. The adapters set `min_num_ctx = 16384`, so `agents.ini`
(8192) is refused by design:

```
Error: --skill 'hello-code': skill 'hello-code' requires num_ctx >= 16384,
but the active profile has 8192.
```

Use `agents_32k.ini` or larger. The runs below were done on
`agents_128k.ini`.

## Flow 1 — code hardening

`base = code`. Full code mechanics: fenced-block parsing, acceptance criteria
required, Gate 1 Stage B active. Gate-3 is empty.

```bash
python3 main.py --auto "Harden main.py: docstrings, type hints, a pytest test" \
  --base examples/hello-world --config agents_32k.ini --skill hello-code
```

**What to expect.** Startup names the skill:

```
skill 'hello-code' loaded — base=code, ~322 tokens injected into architect, coder
task_mode : code
```

Up to four tasks against `main.py` and `test_main.py`. The last observed run
produced a working result: `main() -> int` returning 0, `sys.exit(main())` at
the entry point, and a pytest file using `capsys`.

**Verify by hand:**

```bash
cd examples/hello-world && python3 -m pytest test_main.py -q   # 1 passed
python3 main.py                                                # Hello world
```

**Known rough edges.** The Architect has twice produced two tasks for the
same test file under different titles, despite the skill forbidding it. And
`import sys` has been inserted *above* the module docstring, which silently
demotes the docstring to a plain string — `main.__doc__` still works,
`module.__doc__` does not.

## Flow 2 — documentation

`base = docs`. Prose output, still grounded: Gate 1 runs, so a task claiming
a file that already exists is rejected before the coder starts.

```bash
python3 main.py --auto "Write user documentation for this project" \
  --base examples/hello-world --config agents_32k.ini --skill hello-docs
```

**What to expect.** One task against `README.md`. Gate 1 typically rejects a
`Create documentation file` candidate because `README.md` already exists —
that rejection is correct behaviour, not a failure.

**Known defect — read this before judging the output.** Both observed runs
documented a test suite that does not exist:

```
Run `python -m unittest discover` to execute the test suite.
```

There is no test file in the baseline repository. The `fact` gate is enabled
for this flow and it approved the text anyway, because `fact` compares the
text against facts stated in the *task*, not against the contents of the
repository — it has no file list to check against. It is the right gate for
"this chapter contradicts the story bible" and the wrong one for "this
document describes a file that isn't there". Closing that gap needs either a
repo-aware extension to `fact` or a separate existence gate for docs mode;
neither exists yet.

So: **expect an invented test-suite line, and do not read it as a skill
failure.** It is a known limitation with a known cause.

## Flow 3 — narrative changelog

`base = creative`. Prose parsing, acceptance criteria optional, Gate 1
Stage B skipped, and Gate-3 trimmed to two gates.

```bash
python3 main.py --auto "Write a narrative changelog entry for the greeting" \
  --base examples/hello-world --config agents_32k.ini --skill hello-creative
```

**What to expect.** Startup must show both lines:

```
ContinuityValidator: enabled (max_continuity_revisions=2).
InnerLoop: Gate-3 order for creative mode — canon, continuity
```

The task must target `CHANGELOG.md`. If it targets `main.py`, stop — that is
the failure mode that once burned 10 feedback rounds and 84 Gate-2 decisions
without converging, because the coder writes code, Gate 2 correctly refuses
it, and the loop cannot escape.

**Known defect.** The coder has rewritten `CHANGELOG.md` wholesale rather
than prepending, dropping the `### The first greeting` seed entry. When that
happens `continuity` has no predecessor left to compare against, so its
approval means less than it appears to. Check the seed entry survived.

## Reading whether Gate-3 actually ran

Gate-3 gates log **only** when they reject or hit their revision cap. Silence
means "checked and approved", which is indistinguishable from "never ran" in
the log text. Count API calls in the window between the executor line and the
verdict instead:

```bash
grep -nE "executor run:|attempt [0-9]+ (APPROVED|rejected)|Strict chain" \
  console-log.txt
```

- **1 call** before the verdict → Gate 2 only. Gate-3 did not run.
- **2 calls** → Gate 2 plus one Gate-3 gate.

Observed: flow 3 attempt 1 made one call and was rejected; attempt 2 made two
and was approved. That is Gate 2 rejecting, then Gate 2 passing and
`continuity` running. Flow 2 attempt 2 likewise made two calls — Gate 2 plus
`fact`.

**Gate-3 sits after Gate 2 in the attempt loop.** While Gate 2 rejects,
Gate-3 is unreachable by construction. A silent Gate-3 nearly always means
Gate 2 never passed.

`canon` is on the list for flow 3 but will not fire on a first entry: it runs
on a cadence (`[auto] canon_check_every`, default 3) and its `should_check`
filter skips the file until the cadence comes round. That is expected.

## Expected gate sets

| Flow | Skill | `base` | Gate-3 |
|---|---|---|---|
| 1 | `hello-code` | `code` | *(none)* |
| 2 | `hello-docs` | `docs` | `fact` |
| 3 | `hello-creative` | `creative` | `canon`, `continuity` |

Confirm from the startup log rather than from this table — the adapters are
the source of truth and this table is only a copy.

## The startup warning is expected

```
[validator_agent] system is code-specific but task_mode=docs;
set system_docs or rely on the builtin (AUTO-CR-19-1)
```

Relying on the builtin is correct here. `[validator_agent] system_{mode}`
**replaces** the Gate-2 critique prompt outright, so injecting an authoring
skill there would turn the validator into an author: it would stop emitting
the `{"approved": ..., "feedback": ...}` verdict and Gate 2 would fail-closed
on every attempt. No shipped adapter injects into `validator_agent`, and
`tests/test_skills_hello_world_regressions.py` asserts none ever does.

## Collecting evidence

```bash
python3 view_trace.py                                  # per-stage decisions
python3 analyze_logs.py                                # aggregate
cat examples/hello-world/.agent/plan.json              # target_files per task
cat examples/hello-world/.agent/progress.json          # status, stop_reason
```

`progress.json`'s `stop_reason` is worth checking whenever a run looks
truncated: `task_cap` means `auto.max_tasks_per_run` stopped it, which reads
like a crash but is the cap doing its job.
