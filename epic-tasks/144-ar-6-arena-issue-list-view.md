# AR-6 — `arena issue list [--state S]` / `arena issue view NN`: computed ticket state

**Status:** landed
**Severity:** LOW
**File:** tools/arena/tickets.py
**Symbol:** scan, Ticket, issue_list, issue_view
**Round:** 144
**Size:** S
**Also touches:** tools/arena/cli.py, tests/test_arena_issue_list.py

---

## Why

"Which tickets are open, queued, running, landed" is answered today by reading `epic-tasks/*.md`, `contest-out/` and `git log` by hand. The `**Status:**` field alone lies: a round can be running or finished while the md still says `open`, and a `NN:` commit can be on the integration branch while the md was never set to `landed`. AR-6 computes the state from all three and flags the mismatches.

---

## What AR-1 … AR-5, AR-61 and AR-62 already give you (read them before writing)

- `tools/arena/cli.py`:
  - `OBJECTS["issue"].verbs["list"]` and `["view"]` exist with `ticket="AR-6"` and no handler. AR-6 sets their `handler` and `add_arguments`; no other verb changes (`create`, `land` stay "not implemented").
  - `REPO_ROOT` is the one repo seam; `_run_list` / `_run_view` show how a verb loads the config and hands `REPO_ROOT` on.
  - `-o json` is a global flag; `output.emit(rows, columns, fmt)` prints a table or JSON (masked, scrubbed); `output.refuse(msg)` → exit 2.
  - Exit codes: `EXIT_OK = 0`, `EXIT_FAILED = 1`, `EXIT_USAGE = 2`, `EXIT_NOTHING = 3`, `EXIT_NO_READY = 4`.
- `tools/arena/rounds.py`:
  - `TASKS_DIR`, `_TICKET_RE` (`07-x.md` is 7), `_numbered(names, nn)` (skips `*.rejected.md`), `find_ticket(repo, nn, branch)` — the drafts → checkout → branch order.
  - `integration_branch(repo, flag, prof)` — `--branch`, else the profile's `branch`, else HEAD.
  - `load_config(repo)`, `round_folder(repo, config, nn, leg=None)`, `_has_state(repo, config, nn)`, `round_alive(repo, nn, PROC_ROOT)` (also true through `.arena/locks/NN.pid`).
  - `git(repo, *args)` from `tools/arena/gitref.py`.
- `tools/contest/cli.py`: `_status_of(body)` — the `**Status:**` first word, lower-cased. Import it, do not copy it.
- `tools/contest/draft.py`: `title_of(text)` — the ticket's `# ` line.

---

## What to build

### `tools/arena/tickets.py` (new)

- `Ticket` (dataclass): `number: int`, `title: str`, `path: str` (repo-relative, or `<branch>:epic-tasks/…`), `where: str` (`draft` | `checkout` | `branch`), `status_field: str`, `state: str`, `flags: list[str]`.
- `scan(repo, config, branch, proc_root=PROC_ROOT) -> list[Ticket]`, numeric order, one entry per number, over:
  - `.arena/drafts/*.md` (not `*.rejected.md`);
  - `epic-tasks/*.md` in the checkout;
  - `epic-tasks/` on *branch* (`git ls-tree`, text through `git show`).
  - Precedence: the checkout's file wins over the branch's; an `epic-tasks/` file (either) wins over a draft.
- `state`, first match wins:
  1. `landed` — `**Status:** landed`;
  2. `closed` — `**Status:** closed` (arena only; `PARKED` / `SKIP_STATUS` are AR-14's, do not touch them);
  3. `running` — the round folder has a `state.json` and `round_alive` is true;
  4. `done` — the round folder has a `state.json`, no live runner;
  5. `queued` — `**Status:** queued`;
  6. `draft` — only in `.arena/drafts/`;
  7. `open` — anything else.
- `flags` (each a short text):
  - `commit NN: on <branch> but status <S>` — a commit on *branch* whose subject starts with `NN:` (or `NN ` / `NN —`), status not `landed`/`closed`;
  - `status landed but no NN: commit on <branch>`;
  - `draft and epic-tasks both hold NN`;
  - `two files for NN: a.md, b.md` — two files with the same number in one place.
  - The commit list is read once per `scan` (`git log --format=%s <branch>`), not once per ticket.

### `arena issue list [--state S] [--branch B]`

- Table columns `NN STATE TITLE !`; `!` is `!` when the ticket has flags, else empty. `-o json`: one object per ticket with `number`, `state`, `title`, `path`, `where`, `status_field`, `flags` (a list).
- `--state S` keeps only that state; a state not in the list above → refusal.
- No tickets at all (after the filter) → exit 3, one line.

### `arena issue view NN [--branch B]`

- Prints the ticket's md text as it is, then one line per flag (`! <flag>`), then nothing else. `-o json`: the `Ticket` fields plus `text`.
- `NN` not a number → refusal; no ticket NN anywhere → exit 1, one line.

### Both

- The branch: `rounds.integration_branch(REPO_ROOT, args.branch, prof)`; its `RoundError` is a refusal.
- Read-only: no file written, no process started besides `git` reads, no ref created.

---

## Tests (`tests/test_arena_issue_list.py`)

Throw-away git repo under `tmp_path` (`git init`, one commit on a branch `arena`), `contest.ini` with `out_dir = out` and one `[contest.agent.a]`; patch `cli.REPO_ROOT` and `rounds.PROC_ROOT` (a fake `/proc`). No kilo, no model, no real round.

1. One ticket per state: a draft only in `.arena/drafts/` → `draft`; `open`; `queued`; `landed`; `closed`; `out/NN/state.json` with no runner → `done`; the same with a fake live runner (lock file + fake `/proc/<pid>/cmdline`) → `running`; a leg folder `out/NN.2/state.json` also counts.
2. A ticket only on the branch (not in the checkout) is listed with `where = branch`.
3. Each flag: a `NN:` commit with status `open`; status `landed` with no such commit; draft + epic-tasks for one number; two files for one number.
4. `--state open` filters; `--state nosuch` → exit 2; a filter matching nothing → exit 3.
5. `-o json` parses; `flags` is a list.
6. `issue view NN` prints the md text and the flag lines; a missing NN → exit 1, one line; `issue view abc` → exit 2.
7. `issue create` and `issue land` still say "not implemented".
8. Nothing in the repo changes (file list and `git for-each-ref` before = after).

Tier the file with `python3 scripts/sync_test_tiers.py`; never edit tier symlinks by hand.

---

## Acceptance

Run each command separately and in this order:

```bash
python3 scripts/sync_test_tiers.py
```

```bash
python3 scripts/sync_test_tiers.py --check
```

```bash
python3 -m pytest tests/test_arena_issue_list.py tests/test_arena_run_rerun.py tests/test_arena_run_view.py tests/test_arena_run_start.py tests/test_arena_cli.py -q
```

```bash
python3 -m pytest tests -n 8 -q
```

```bash
python3 -m pytest tests_bugfix -n 8 -q
```

All cases above pass. `contest-bench/139`, `142` and `143` still pass.

---

## Rules

- No change under `tools/contest/` and `scripts/`.
- `REPO_ROOT` is the only repo seam; never read the current working directory.
- **Never run a real round while working on this ticket** — no `python3 -m tools.contest run …`, no `arena run start|rerun …`, no `scripts/revive_round.py` on a real `contest-out/`.
- Every refusal is one line through `output.refuse`, exit 2, and writes nothing.
- Never read or print `agents_128k.ini`. No real provider or model names in committed files. Never print an api key.
- Do not change existing tests except to add cases; `tests/test_arena_cli.py` may move a "not implemented" example off `issue list`/`issue view` to another unimplemented verb.
- Python 3.10+, four-space indent, type hints on public functions, explanatory comments in the style of `tools/arena/rounds.py`.
- Tests wait on events, never on sleeps or tight timeouts.
- No unrelated repository changes.
- Commit subject starts with the round number: `144: …`.
