# hello-world runbook

Three skill flows, one per mechanical base. Every command below has been run
against this repository; the "what to expect" sections describe what actually
happened, not what should happen in theory.

The runbook test in the parent repository (tests/test_hello_world_runbook.py) parses this file and checks each command
is still valid — the skill exists, the profile exists and clears the skill's
context budget, the flags are real, and the documented gate sets match the
adapters. If you edit a command here, run that test.

## Running everything at once

```bash
scripts/run_flows.sh                      # all three flows, then validate
scripts/run_flows.sh 2                    # just task 2
scripts/run_flows.sh --config agents_128k.ini
scripts/run_flows.sh --runner proxychains4
scripts/run_flows.sh --check-only         # validate previous runs, run nothing
```

Each flow gets its **own** sandbox, rebuilt from the committed baseline
before it starts:

```
examples/task1/hello-world/     hello-code
examples/task2/hello-world/     hello-docs
examples/task3/hello-world/     hello-creative
```

The isolation matters. The flows commit into their base directory, so running
all three against the shared `examples/hello-world/` means task 2 inspects
task 1's output — the results stop being independent, and a passing task 2
might only be passing because task 1 created the file it wanted.

These directories are gitignored: they are run output.

The script refuses a profile below the skills' 16384 floor before starting,
which costs a second instead of a whole run.

## Validating a run

`pytest` proves the machinery is wired. It cannot prove a *run* produced the
right artefact, because the artefact is written by a model at runtime.

```bash
python3 scripts/check_runbook.py --task 2
python3 scripts/check_runbook.py --all
python3 scripts/check_runbook.py --all --json
```

Every check is **deterministic — no LLM, no network.** "Does `test_main.py`
exist", "did `README.md` change", "is the seed changelog entry still there"
all have exact answers on disk. That matters twice: the checks cost nothing,
and they cannot themselves hallucinate a pass. Judging prose *quality* is
deliberately out of scope — the Gate-3 gates do that during the run.

Each task is checked against its own sandbox if one exists, otherwise against
the shared `examples/hello-world/`.

| Task | Checks |
|---|---|
| 1 | main.py still prints `Hello world`; module and `main()` docstrings; return annotation; a test file exists and passes |
| 2 | README.md actually changed; has a Usage section; no `.py` file targeted; no references to missing files (via the shipped `existence` gate) |
| 3 | a new entry exists; the seed entry survived; canon fact intact; prose only; main.py untouched |

Exit code is 0 only when everything passes.

**This is not hypothetical.** A docs run was once judged working because it
exited 0 and committed. It had written a Python test file and never touched
`README.md`. The checker reports that as four separate failures.

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

**What the `existence` gate catches here.** Both earlier runs documented a
test suite that does not exist:

```
Run the tests by executing `python -m unittest test_main.py`
Run `python -m unittest discover` to execute the test suite.
```

There is no test file in the baseline repository. Gate 1 passed both and
could not have caught them — it judges the planned task, not the emitted
prose. Gate-3 `fact` was tried and also passed them, because its prompt
compares the text against facts stated in the *task*, so it has no file list.

`existence` (GATES-3) closes that gap. It reads the filesystem, makes **no
LLM call**, and reports two kinds of finding: a referenced path that is not
in the repository, and a document that tells the reader to run tests when
there are no test files at all. The second is what catches
`unittest discover`, which names no file.

Expect a Gate-3 rejection with feedback naming the reference, and the coder
removing the testing section on the next attempt. If the run instead ends
with an accepted README still claiming a test suite, check that the gate was
built:

```
ExistenceValidator: enabled (max_existence_revisions=2, no LLM).
InnerLoop: Gate-3 order for docs mode — existence
```

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
`continuity` running. Flow 2 made two calls per attempt back when it ran the
`fact` gate.

This counting trick no longer applies to flow 2: `existence` is deterministic
and makes no API call at all, so a docs attempt shows **one** call (Gate 2)
whether or not the gate ran. Read the startup line instead:

```
ExistenceValidator: enabled (max_existence_revisions=2, no LLM).
```

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
| 2 | `hello-docs` | `docs` | `existence` |
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
the skill regression test in the parent repository asserts none ever does.

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
