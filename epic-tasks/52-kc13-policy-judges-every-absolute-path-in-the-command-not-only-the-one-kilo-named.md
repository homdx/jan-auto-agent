# KC-13 — `Policy`: every absolute path in the command is judged, not only the one Kilo put in `patterns`

**Status:** open — round 52 of EPIC KC (`docs/kilo-contest/EPIC-KC.md`); found live on 2026-09-19 (`contest-bench/kc6/RUNBOOK.md` §10). Independent of KC-12 (round 51, open in parallel — different file).
**Severity:** HIGH
**File:** `tools/contest/policy.py` (`_extract_paths`)
**Symbol:** `_extract_paths`, `PolicyContext`, `Policy.decide`
**Round:** 52
**Size:** S
**Source:** live round 2 of `contest-bench/kc6/live_smoke.py`. The model ran one bash command: `mkdir -p /tmp/contest/notes && echo … > /tmp/contest/notes/hy3.txt && echo … > /tmp/kc6-outside-hy3.txt`. Kilo's `permission.asked` carried `patterns: ["/tmp/contest/notes/*"]`, `metadata.directories: ["/tmp/contest/notes"]` and the whole command in `metadata.command`. `_extract_paths` reads `patterns` and `metadata.directories`/`.patterns` only; every path it saw was under `tmp_roots`, layer 1 answered `once`, and `/tmp/kc6-outside-hy3.txt` — outside the worktree, outside `tmp_roots`, never shown to the gate — was written. Kilo names the *first* outside directory it detects in a command (the `mkdir` argument); a redirect target, a second argument or a `cd` elsewhere in the same line is not in the event.
**Depends on:** KC-3 (`tools/contest/policy.py`, landed `1499cd8`).
**Also touches:** `tests/test_contest_policy.py`

---

## What happens today

`_extract_paths(props)` returns the paths of `properties.patterns`,
`properties.metadata.directories` and `properties.metadata.patterns`. A bash
permission whose command touches two outside places is judged by the one
Kilo chose to report; the other rides along on the same `once`.

## What must change

1. `_extract_paths` also scans `metadata.command` (a string) for absolute
   path tokens: every maximal run of non-whitespace, non-shell-syntax
   characters that starts with `/`, `~/`, `./` or `../` after a shell
   operator or a space (`>`, `>>`, `<`, `|`, `&&`, `;`, `(`, and the start
   of the command count as separators), quotes stripped. Each becomes one
   more `(resolved, (original,))` pair, de-duplicated by resolved path like
   the others. `_pathlike` stays the gate for what is a path; a bare word
   is still a command, not a path.
2. The verdict is the same as today's, over the *union*: a `forbidden`
   match anywhere → `reject`; every path inside the worktree or `tmp_roots`
   → `once`; otherwise layer 2 (the gate), which already receives the whole
   command in its user message.
3. `metadata.command` is bash's; for a permission with no `command`
   (`external_directory` from a file tool, `doom_loop`) nothing changes.
4. Fail-open on garbage: a non-string command, a token that does not
   resolve, a path with a NUL — skipped, never raised (`decide` never
   raises; keep it so).

## Acceptance

- [ ] `tests/test_contest_policy.py`:
      - the live event verbatim (`patterns: ["/tmp/contest/notes/*"]`,
        `metadata.command` with the second redirect to `/tmp/kc6-outside-x.txt`,
        `tmp_roots = ("/tmp/contest/*",)`) is **not** `once` from layer 1: with
        a stub gate saying `reject` the decision is `reject`/`gate`, and the
        gate's user message names `/tmp/kc6-outside-x.txt`;
      - the same command with both targets under `/tmp/contest/` is still
        `once`/`mechanical`;
      - a command whose second path is under `HARD_DENYLIST` (e.g. `~/.ssh/x`)
        is `reject`/`mechanical` with `forbidden:` in the reason, no gate call;
      - `cat scripts/x.py > /tmp/contest/out.txt` (relative first path inside
        the worktree, absolute second under tmp_roots) is `once`;
      - a bare command word (`reboot`), `2>&1`, `$HOME/x`, a URL
        (`https://…`) and a quoted path with spaces do not become paths or
        do not crash; existing KC-3 tests unmodified and green.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green.

## Out of scope

- Parsing the shell for real (`bash -n`, `shlex` with heredocs): a token
  scan is the point — false positives go to the gate, which reads the
  whole command anyway; false negatives are what this ticket closes.
- Changing what Kilo reports.
- Rate-limiting a model that retries a rejected command (laguna: 5× in one
  turn) — that is `gate_max_calls_per_session` today and KC-9's ground.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

Every line below is a way a KC-5/KC-6 entry lost points on the round bench;
the scorer checks all of them mechanically, so check them yourself first.

- [ ] `python3 --version` on the judge is **3.10.12**. Every new/changed
      module imports there: `python3 -c "import tools.contest.policy"` from
      the repo root. No backslash and no nested same-quote inside an
      f-string expression (a 3.12-only `f"{x.split("\t")}"` is a
      `SyntaxError` here and scores 0).
- [ ] Exactly **one** commit on top of the base: `git log --oneline <base>..HEAD`
      prints one line. Only this ticket's work is in it — no other KC
      ticket, no "while I was here" fixes; amend, do not stack.
- [ ] `git diff --stat <base>..HEAD` names only the files under **File:**
      and **Also touches:** (plus `.smoke_tests/` links). Never `epic-tasks/`.
- [ ] Names and signatures are the ticket's, verbatim — `_extract_paths(props) -> list`
      keeps its shape (`[(resolved, (original, ...)), ...]`), `Policy.decide`
      and `PolicyContext` are unchanged; read `tools/contest/policy.py`
      (`_pathlike`, `_NOT_A_PATH`, `_forbidden_match`, `_inside_worktree_or_tmp`)
      before adding to it.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.
- [ ] The new tests are red without the change: check the test file alone
      out onto the base, run it, see them fail; restore.
- [ ] `python3 -m pytest tests -q --timeout=180` then
      `python3 -m pytest tests_bugfix -q --timeout=180`, **sequentially**,
      both green.
- [ ] `CollectBridge._shrink` byte-identical:
      `git diff <base> HEAD -- tools/auto/collect_bridge.py` is empty.
- [ ] Then, and only then, `scripts/append_task.py` from the worktree;
      open `runs/<you>/PROGRESS.csv` and see your row with the sha of the
      one commit (the script stores `--outcome DONE` as `FIXED`; that is fine).
- [ ] What you hand in is `git format-patch <base>..HEAD` of that one
      commit — not a raw `git diff`, not the whole branch, not an empty file.
- [ ] `grep -n 'shlex\|subprocess' tools/contest/policy.py` finds nothing new:
      the scan is string work, no shell is started to judge a permission.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
