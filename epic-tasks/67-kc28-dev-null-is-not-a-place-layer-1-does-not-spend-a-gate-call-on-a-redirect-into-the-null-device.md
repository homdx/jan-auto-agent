# KC-28 — `/dev/null` is not a place: layer 1 does not spend a gate call (and the budget) on a redirect into the null device

**Status:** queued — after KC-27, last in the queue; found 2026-09-20 landing KC-15 (round 54): with `*>*` asked, a `> /dev/null 2>&1` goes to the gate. Same file as KC-15 (landed) and KC-13 (landed); disjoint from every open ticket.
**Severity:** HIGH, raised from LOW on 2026-09-23 after round 86 (see *Seen live* below). It costs a gate call per `> /dev/null`, and `reject`/`budget` once the session's twenty are spent (fail-closed by design, KC-3). With a free gate that answers empty, every such call is a `reject`, and a reject can end the agent's turn.
**File:** `tools/contest/policy.py` (`_extract_paths` or `_inside_worktree_or_tmp`)
**Symbol:** `_extract_paths`, `NULL_DEVICES`
**Round:** 67
**Size:** S
**Source:** the KC-15 ideal on `kc`, probed by hand before landing: `python3 -m pytest tests -q > /dev/null 2>&1` → `once`/`gate`, one gate call, reason `gate: scratch path under tmp_roots`; `sort x | tee out.txt` and `cp a.py b.py` → `once`/`mechanical`, no call. `/dev/null` is a path token of the command (KC-13's scan), it resolves outside the worktree and outside `tmp_roots`, so layer 1 cannot settle it and layer 2 answers a question nobody asked. `gate_max_calls_per_session = 20` in `contest.ini`; a model that habitually silences commands with `> /dev/null` burns the budget on nothing and then meets `reject`/`budget` on the first command that matters. `/dev/stdout`, `/dev/stderr` and `/dev/tty` are the same kind of token.
**Depends on:** KC-13 (`_extract_paths` scans `metadata.command`, landed `3e4b41b`), KC-15 (`*>*` asked, landed).
**Also touches:** `tests/test_contest_policy.py`


**Seen live, round 86 (run 3, 2026-09-23, base `4634507`, 12 agents, gate `hy3:free`):**
in the first five minutes, **all 13** gate calls were for a command whose only
outside path was `/dev/null` (`ls tests/ | head; ls .smoke_tests/ 2>/dev/null`,
`pip list 2>/dev/null | grep …`, `git diff --stat HEAD~1..HEAD 2>/dev/null`,
`cat tools/auto/collect_bridge.py 2>/dev/null | head -50`). 12 of the 13 came
back `gate-failed` (11 `empty reply`, 1 `RuntimeError`, 3–5 s each; KC-37),
so each became a `reject` on a harmless read inside the worktree. The losses:

- `agnes-2-5-flash`: 3 of 4 reads refused, then its turn ended `idle` at +92 s with a clean tree → `HARVESTING` on nothing (KC-50).
- `mimo-v2-5` 3, `sensenova-6-7-flash-lite-var1` 2, `agnes-3-0-flash` 2 and `sensenova-6-8-flash-lite-var1` 2 refused reads.

`2>/dev/null` is the most common suffix models add to a probing command. Until
this ticket lands, every one of them goes through the gate, and the gate is
the weakest link in the round.

---

## What happens today

`_extract_paths` keeps every path-shaped token of `metadata.command`;
`/dev/null` is one. `_inside_worktree_or_tmp` sees a path outside both
lists, returns `False`, and `_mechanical` returns `None` — the gate is
asked whether writing nothing to nowhere is safe. It says `allow`, at the
price of one call out of the session's twenty.

## What must change

1. `NULL_DEVICES: frozenset[str] = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})`
   next to `HARD_DENYLIST`.
2. `_extract_paths` drops a token whose *resolved* path is in
   `NULL_DEVICES` (resolve first: `/dev/stdout` is a symlink on Linux; the
   set is checked against both the original and the resolved string). The
   pairs list never contains them, so `_inside_worktree_or_tmp` and the
   forbidden check never see them, and a command whose only path is
   `/dev/null` is a **no-path** bash ask — KC-15's rule gives `once`/`mechanical`,
   `bash: no path outside worktree/tmp_roots`.
3. Nothing else: a command with `/dev/null` **and** an outside path still
   goes to the gate for the outside path; `external_directory` events are
   unchanged (Kilo never reports `/dev` as a directory argument).

## Acceptance

- [ ] `tests/test_contest_policy.py`:
      - `bash` with `command: "python3 -m pytest tests -q > /dev/null 2>&1"`,
        `tmp_roots = ("/tmp/contest/*",)`, a stub gate → `once`/`mechanical`,
        reason contains `no path outside`, the gate is **not** called;
      - `command: "cat x > /dev/null; cp y /tmp/elsewhere/z"` → the gate is
        called once and its message names `/tmp/elsewhere/z`, not `/dev/null`;
      - `_extract_paths` on `patterns: ["/dev/null"]` (an `external_directory`
        shape) → empty list;
      - `command: "echo x > /dev/shm/leak"` → still the gate (`/dev/shm` is
        a place; only the four devices are dropped).
- [ ] Every existing test in `tests/test_contest_policy.py` unmodified and green.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green.

## Out of scope

- Dropping `/dev/*` wholesale (`/dev/shm`, `/dev/sd*` are places a gate
  should see).
- Any change to `ask_commands` defaults, `session_rules()`, or the gate prompt.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version` on the judge is **3.10.12**;
      `python3 -c "import tools.contest.policy"` from the repo root. No
      backslash and no nested same-quote inside an f-string expression.
- [ ] Exactly **one** commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/policy.py`
      and `tests/test_contest_policy.py` (plus `.smoke_tests/` links). Never `epic-tasks/`.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.
- [ ] The new tests are red without the change.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` then
      `python3 -m pytest tests_bugfix -n 4 -q --timeout=180`, **sequentially**,
      both green.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the **sha** of the
      one commit — not `HEAD`; hand in `git format-patch <base>..HEAD`.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
