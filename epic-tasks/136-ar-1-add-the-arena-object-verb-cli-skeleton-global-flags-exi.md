# AR-1 — Add the `arena <object> <verb>` CLI skeleton, global flags, exit codes, and secret masking

**Status:** open
**Severity:** MEDIUM
**File:** tools/arena/cli.py
**Symbol:** main, OBJECTS
**Round:** 136
**Size:** S
**Also touches:** tools/arena/__init__.py, tools/arena/__main__.py, tools/arena/output.py, arena, tests/test_arena_cli.py, .gitignore

---

## Why

Every later `arena` command needs one shared parser, passthrough behavior, output format, exit-code table, and secret-masking policy. AR-1 establishes that common skeleton without implementing later command actions or changing `tools/contest/`.

## What to build

- Add `tools/arena/__init__.py` and `tools/arena/__main__.py` so `python3 -m tools.arena ...` invokes `tools.arena.cli.main`.
- Add the repository-root `arena` launcher:
  - Keep it executable with mode `100755`.
  - Put the launcher's own directory first on `sys.path`.
  - Invoke `tools.arena.cli.main`.
  - Make `./arena --help` work when launched from another working directory.

### `tools/arena/cli.py`

Implement `main` with two levels of `argparse` subparsers:

- Define `OBJECTS = {"profile": …, "issue": …, "run": …, "entry": …}`.
- `arena --help` lists `profile`, `issue`, `run`, and `entry`.
- Each registered object supports `--help` and exits `0`.
- AR-1 has no implemented object verbs. An otherwise recognized but unimplemented verb prints `arena: <object> <verb> is not implemented yet (AR-N)` and exits `2`.
- Unknown objects, missing required object or verb arguments, and malformed invocations are usage errors.

Support these global flags before the object:

- `-p`/`--profile NAME`.
- `-o`/`--output table|json`, defaulting to `table`.
- `-y`/`--yes`.

AR-1 parses and retains `-p`/`--profile` and `-y`/`--yes`, but no AR-1 action reads either parameter yet. The output format parameter is read to select `table` or `json`.

Before `argparse` sees the arguments, split `argv` at the first standalone `--`:

- Parse only the arguments before that delimiter.
- Store every argument after it verbatim in `args.passthrough`.
- Preserve passthrough values beginning with `-`.
- Use `[]` when no standalone `--` is present.
- Thus `arena run start 5 -- --no-gate --max-parallel 4` retains `["--no-gate", "--max-parallel", "4"]` and parses `5` as the ticket through the fake verb handler used by the test.
- Without `--`, the fake verb handler receives `args.passthrough == []`.
- AR-1 never parses or interprets passthrough arguments.

Override the parser's usage-error behavior:

- Print exactly one stderr line in the form `arena: <message>`.
- Exit `2`.
- Do not print an argparse usage block.

Define one module-level exit-code table:

- `0`: action succeeded.
- `1`: action failed.
- `2`: usage error or refusal before anything was done.
- `3`: nothing to do, such as no rounds to list.
- `4`: a round ran and ended with no READY agent.

AR-1 defines the table only; nothing maps a child's exit code yet. Write the reason next to `4`: the old `run` command exits `2` both for "no READY agent" and for an argparse error (a mistyped flag after `--`), so AR-3 maps a child's `2` to `4` only when the round ran, and to `1` otherwise.

### `tools/arena/output.py`

Implement `emit`, `mask`, `scrub`, and `refuse`.

`emit(rows, columns, fmt)` must:

- For `table`, print a plain left-aligned table using `columns`.
- For `json`, print JSON for `rows`.
- Always recursively apply `mask` before printing.
- Apply `scrub` to every string value before printing.
- Preserve numeric values such as `tokens` as numbers in JSON output.

`mask(mapping)` must:

- Return a copy and not mutate its input.
- Recurse through dictionaries and lists.
- Replace values whose key is `key`, `apikey`, `token`, `secret`, `password`, or `passwd`.
- Also replace values whose key contains one of those terms as a whole word, case-insensitively, with words separated by `_`, `-`, `.`, or camelCase boundaries.
- Mask `api_key`, `API_KEY`, `gate_token`, `apiKey`, and `client-secret`.
- Leave `tokens` usage counts, `max_tokens`, `monkey`, and `base_url` unchanged.
- Leave ordinary keys and values unchanged.

`scrub(text)` must:

- Replace a URL's `user:pass@` portion with `***`.
- Replace every `api_key`, `key`, `token`, `secret`, or `password` value in query/form-style text with `***`.
- Ensure the original credentials and secret values cannot reach printed output.

`refuse(msg)` must:

- Scrub `msg`.
- Print exactly one stderr line in the form `arena: <scrubbed msg>`.
- Return `2`.

### Other files

- Add `.arena/` to `.gitignore`.
- `githooks/pre-commit` rejects only a new root `*.py` file; the launcher is named `arena`, with no extension, so the hook passes it.
- Add the seven tests to `tests/test_arena_cli.py`:
  1. `arena --help` lists the four objects; `arena issue --help` exits `0`.
  2. An unknown object exits `2` and writes exactly one stderr line beginning with `arena:`, with no usage block.
  3. A fake verb handler receives ticket `5` and passthrough `["--no-gate", "--max-parallel", "4"]` for `arena run start 5 -- --no-gate --max-parallel 4`; the same invocation without `--` receives `[]`.
  4. `mask` recursively masks `api_key`, `API_KEY`, `apiKey`, `gate_token`, nested dictionary values, and values in lists, while leaving `model`, `base_url` without a key in it, and numeric `tokens` unchanged.
  5. `emit(..., "json")` produces JSON accepted by `json.loads` and contains `***` where a key was masked.
  6. `scrub` and `refuse` hide credentials and secret values from `https://u:p@host/v1?api_key=abc&x=1` and `token=xyz` in the stderr line.
  7. The root `arena` file is executable, and `./arena --help` exits `0` when launched from another working directory.

## Acceptance

Run each command separately, in this order:

```bash
python3 scripts/sync_test_tiers.py
```

```bash
python3 scripts/sync_test_tiers.py --check
```

```bash
python3 -m pytest tests -n 8 -q
```

```bash
python3 -m pytest tests_bugfix -n 8 -q
```

The implementation must satisfy all seven cases in `tests/test_arena_cli.py`, preserve the existing test suites, keep `arena` executable with mode `100755`, and leave `tools/contest/` unchanged.

## Rules

- Do not change anything under `tools/contest/`.
- Do not implement profile, issue, run, or entry actions from later tickets.
- Do not read `-p`/`--profile` or `-y`/`--yes` in an AR-1 action; only parse and retain them.
- Preserve passthrough arguments verbatim; AR-1 must never parse them.
- Keep usage errors and refusals to one stderr line.
- Refusals must explain how to get past them when a remedy is applicable.
- Never print API keys, tokens, secrets, passwords, credentials, or URL credentials.
- Keep `arena` executable with file mode `100755`.
- Use Python 3.10+, four-space indentation, type hints on public functions, and explanatory comments consistent with the existing CLI code.
- Tier `tests/test_arena_cli.py` with `python3 scripts/sync_test_tiers.py`; do not edit tier symlinks by hand.
- Tests must not start `kilo`, call a model, or touch the real `contest-out/`; anything that needs a repo uses a throw-away git repo in `tmp_path`.
- A test that waits for a child process waits on an event the child creates (its exit, a pipe, a marker file), never on a sleep or a tight timeout: `scripts/check_test_clocks.py` and the parallel runs hold every test to that.
- No unrelated repository changes.
- Commit subject starts with the round number: `136: …`.
