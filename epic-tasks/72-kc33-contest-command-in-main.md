# KC-33 — `/contest` command in `main.py` REPL and `--contest` one-shot flag

**Status:** landed `6b7978c` — round 72, ideal commit chosen black-box over all 10 candidate patches (3 round-72 agents + 7 SenSenova variants): all 10 agreed on the core dispatch, the one discriminating test was `test_main_skips_the_orchestrator` (`--contest` must short-circuit before `Orchestrator(config_path=...)` is built) — 7/10 got it right; SenSenova-6-8-var10 won on the fullest green suite (20 tests) and clean hygiene. Note added 2026-09-22: this ticket's own Status line and the INDEX row were still `open` after the commit landed — a bookkeeping lag, not a code gap; fixed now.  
**Severity:** MEDIUM  
**File:** `main.py` (`HELP_TEXT`, `_parse_args`, `main`)  
**Symbol:** `main`, `_parse_args`, `HELP_TEXT`  
**Round:** 72  
**Size:** S  
**Source:** `main.py`'s REPL already dispatches `/auto`, `/collect`, `/faq` into their
respective `tools.*` entry points with the same pattern — `user_input.startswith("/contest")`
→ `tools.contest.cli.cmd_run`. The contest CLI is already complete
(`python3 -m tools.contest run --ticket NN`) but is invisible inside `main.py`'s
interactive shell. Operators running the pipeline from `main.py` have no path to the
contest without switching to a second terminal.  
**Depends on:** KC-6 (`tools/contest/runner.py`, landed `e8c6ad3`),
KC-16 (`tools/contest/cli.py` `cmd_run`, landed `1304950`).  
**Also touches:** `tests/test_main_contest_dispatch.py` (new)

---

## What happens today

`main.py`'s REPL has no `/contest` case. The only entry point for the contest
is:

```
python3 -m tools.contest run --ticket NN --models a:free,b:free --max-parallel 4
```

An operator inside the interactive shell must `Ctrl-C`, re-invoke from the
command line, then restart `main.py`. The `--once` / `--auto` / `--faq`
one-shot flags exist in `_parse_args` but no `--contest` equivalent does.

## What must change

### 1. REPL dispatch — `/contest`

In `main()`, after the `/faq` block and before the unrecognized-slash guard,
add:

```python
if user_input.startswith("/contest"):
    body = user_input[len("/contest"):].strip()
    argv = body.split() if body else ["--help"]
    from tools.contest.cli import main as _contest_main
    try:
        _rc = _contest_main(argv)
    except SystemExit as exc:
        # CORRECTION (review pass): argparse's OWN --help action and its
        # own usage-error path (e.g. a bare `/contest run` missing the
        # required --ticket) call sys.exit() directly — they do not
        # return an int. That raises SystemExit, which is NOT an
        # Exception subclass and is NOT caught by the REPL's own
        # `except (KeyboardInterrupt, EOFError):` further down. Left
        # unguarded, a bare `/contest` (or any malformed one) would
        # propagate out of `while True:` and kill the whole main.py
        # process — exactly the failure mode `/collect`'s own dispatch
        # was built to avoid (see parse_collect_args's docstring: "Kept
        # separate from stdlib argparse so main.py can add ... and just
        # forward here"). Catch it here and normalize to the same int
        # contract every other dispatch uses.
        _rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    if _rc:
        print(f"[{_ts()}] ⚠️  contest finished with exit code {_rc}.")
    continue
```

`tools.contest.cli.main(argv)` accepts a list, parses it with its own
`argparse._parser()`, and returns an int on the normal path — the same
contract `/auto` uses with `run_auto`. Passing `["--help"]` on a bare
`/contest` prints the contest's own help text to stdout; argparse's
`--help` action raises `SystemExit(0)` rather than returning, which is why
the `try/except SystemExit` above is required, not optional — without it
this exit code never becomes a printed "exit code 0" and never reaches
`continue`, it terminates the interpreter.

Accepted syntax (passed verbatim as `argv` after `/contest`):

```
/contest run --ticket 63 --models agnes-2-5-flash:free,hy3:free --max-parallel 4
/contest run --ticket 63 --no-tests --fresh
/contest run --ticket 63 --resume
```

### 2. `HELP_TEXT` addition

Add under the existing `/auto` entry:

```
  /contest run --ticket NN   Run one contest round — prompt, wait, harvest,
                              rework — for the agents on the roster (or
                              --models a:free,b:free to override it).
                              Passes every flag straight to
                              `python3 -m tools.contest run`.
                              /contest --help for the full flag list.
```

### 3. `_parse_args` — `--contest` one-shot flag

Add a `--contest` flag that mirrors `--auto`:

```python
parser.add_argument(
    "--contest", metavar="ARGS", default=None,
    help="One-shot contest round: pass the rest of the argument string to "
         "`tools.contest.cli.main`, then exit.  "
         "e.g. --contest \"run --ticket 63 --models hy3:free\"",
)
```

In `main()`, before the checkpoint block:

```python
if args.contest is not None:
    import shlex
    from tools.contest.cli import main as _contest_main
    sys.exit(_contest_main(shlex.split(args.contest)))
```

This lets the operator call:

```
python main.py --contest "run --ticket 63 --models agnes-2-5-flash:free,hy3:free --max-parallel 8"
```

and is identical in shape to how `time python3 -m tools.contest run --ticket 63 …`
is used today, while keeping `main.py` as the single entry point.

### 4. Unrecognized-slash guard

`/contest` must appear in the exclusion list next to `/edit` and `/search`:

```python
if user_input.startswith("/") and not user_input.startswith(
    ("/edit", "/search", "/contest")
):
```

## Acceptance

- [ ] `tests/test_main_contest_dispatch.py` (new), against a monkeypatched
      `tools.contest.cli.main`:
      - `/contest run --ticket 63 --no-tests` in the REPL calls
        `tools.contest.cli.main(["run", "--ticket", "63", "--no-tests"])` and
        does not fall through to `run_pipeline`;
      - a non-zero return from `_contest_main` prints the `⚠️` line;
      - bare `/contest` calls `_contest_main(["--help"])` — does not crash
        (this must go through the real `SystemExit(0)` argparse raises for
        `--help`, not a monkeypatched `_contest_main` that returns 0
        directly, or the test cannot catch a regression here);
      - `/contest run` with no `--ticket` (argparse's own required-arg
        error, `SystemExit(2)`) also does not crash the REPL and prints
        the exit-code-2 warning line;
      - `--contest "run --ticket 63"` in `_parse_args` calls
        `_contest_main(["run", "--ticket", "63"])` and `sys.exit`s with its
        return code;
      - `/contest` does **not** match the unrecognized-slash guard.
- [ ] `HELP_TEXT` contains the string `/contest run --ticket`.
- [ ] Every existing test unmodified and green.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green.

## Out of scope

- A `/contest status` command (reads `state.json`) — that is KC-7's `cmd_status`.
- Passing `--roster` or `--base` from `main.py`'s own `--config` — the operator
  supplies them in the argument string directly.
- Any change to `tools/contest/`.

## Self-check before `append_task.py`

- [ ] `python3 --version` on the judge is **3.10.12**; `python3 -c "import main"` clean.
- [ ] Exactly **one** commit; only `main.py` and the new test file touched.
- [ ] `git diff --stat <base>..HEAD` names only `main.py`,
      `tests/test_main_contest_dispatch.py` (plus `.smoke_tests/` links).
      Never `epic-tasks/`.
- [ ] `python3 scripts/sync_test_tiers.py --check` clean.
- [ ] New tests red without the change.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` then
      `python3 -m pytest tests_bugfix -n 4 -q --timeout=180`, sequentially, both green.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` with the sha of the one commit.

## Ground rules

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
