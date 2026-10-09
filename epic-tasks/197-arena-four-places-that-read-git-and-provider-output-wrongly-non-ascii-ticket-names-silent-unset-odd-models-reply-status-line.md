# 197 — arena: four places that read git and provider output wrongly (quoted ticket names, silent unset, odd `/models` reply, status line)

**Status:** landed
**Origin:** operator review of the arena, bugs 206–209
**Severity:** LOW (all four; 207 and 206 are silent, 208 is a traceback, 209 is cosmetic)
**File:** tools/arena/rounds.py
**Symbol:** find_ticket, _dirty_tasks (+ tickets._branch_names, contest/cli.py; models.write_profile_keys; py_model_test.free_from_direct_api, models._direct_for)
**Round:** 197
**Size:** M
**Also touches:** tools/arena/tickets.py, tools/arena/models.py, tools/arena/gitref.py, tools/contest/cli.py, scripts/py_model_test.py, tests_bugfix/

## Bugs

**206 — a ticket name with a non-ASCII letter, `"` or `\` is invisible on a branch or a base commit.**
`git ls-tree --name-only` without `-z` quotes such names (`"epic-tasks/\320\260.md"`), and no pattern knows them. Silent: `issue list` says there is no ticket, `find_ticket` says "no ticket 2", the runner does not see it on the base. Only hand-named files hit it (`slug_for` writes ASCII). Places: `rounds.py::find_ticket`, `tickets.py::_branch_names` and the two `ls-tree` calls at `tickets.py` ~798 and ~1427, `contest/cli.py` ~297, ~339, ~878.

**207 — `profile set key=` and `model unset-role` do nothing for a key set in the committed `contest.ini`** (`models.write_profile_keys`).
Only `contest.local.ini` is written, there is nothing to remove there, and the preview says the key is gone. Exit 0, not a word, the profile is unchanged; `issue create` goes on writing the ticket with the model that was meant to be removed.

**208 — `arena model list / available / use` on an odd provider reply** (`py_model_test.free_from_direct_api`, `models._direct_for`).
The shape of each model's fields is never checked: `pricing` as a string, `currency: null`, `capabilities` a list of objects, `min_plan` a number, `id` an object — all raise. A cut body raises `IncompleteRead`/`BadStatusLine`, which are `http.client.HTTPException`, neither `OSError` nor `ValueError`, so `_direct_for` does not turn them into its one-line hint. In the operator's trial, 9 of 12 malformed replies gave a traceback.

**209 — `run start` names a half-eaten file for an unstaged change** (`rounds._dirty_tasks`).
`git()` strips all the output, so the first status line loses its leading space (` M epic-tasks/01-a.md` → `M epic-tasks/…`) and `line[3:]` cuts a letter: the refusal fires but names `pic-tasks/01-a.md`. Non-ASCII names print with escape codes, a rename prints as `old -> new`.

## Fix

- 206: list names with `-z` and split on `\0` (or `-c core.quotepath=off`) in every one of the seven places; one helper in `gitref.py` (`ls_tree_names(repo, ref, folder)`) so the places stop each carrying a copy.
- 207: after the write, re-read the profile; if a key asked to go is still there (it comes from `contest.ini`), refuse with one line naming the file, writing nothing — or write an empty override if the ini format allows it, decide and say which in the commit.
- 208: parse each model entry inside a `try` and skip an entry whose fields are of the wrong type (`isinstance` checks on `pricing`, `capabilities`, `min_plan`, `id`); `_direct_for` catches `http.client.HTTPException` too, so it is the same one-line "direct list failed — trying Kilo".
- 209: `git(..., strip=False)` and `status --porcelain -z`; take the path from the record, the new name for a rename.

## Tests

One file per bug in `tests_bugfix/` (`test_arena_<what>_197.py`), each failing on the old code. Cover: a ticket `07-а.md` and `07-"q".md` on a branch and as a base; a key in `contest.ini` with `profile set key=` and `unset-role` (exit code, message, nothing written); the twelve malformed replies of the operator's trial, a cut body, a 200 with an error object (nothing but a one-line hint, no traceback); ` M` with an unstaged file, a non-ASCII name, a rename.

## Second review (cross-check on `arena` @ `00355fd`)

An independent review (`bugs-to-review/`, bugs 206–209, tests `test_arena_ticket_name_non_ascii_206`, `…unset_inherited_key_207`, `…direct_models_field_shape_208`, `…dirty_tasks_names_209`) reports the same four defects, and its regression tests still fail on this tree (6, 5, 15 and 4 failing cases). The same two readers (quoted ticket names, a stripped `git status` line) are repeated in `tickets.py` — that is ticket 209, bug 44; fix the shared reader once, here or there, and say which in the commit.

## Review material

Review tests (held by the operator, outside the repo so a round does not copy them): `test_arena_ticket_name_non_ascii_206`, `test_arena_unset_inherited_key_207`, `test_arena_direct_models_field_shape_208`, `test_arena_dirty_tasks_names_209` — 6, 5, 15 and 4 failing cases on this tree.
The operator keeps these outside the repo on purpose: a round that can read the reviewer's fix would copy it, and the competition would measure nothing.

## Acceptance

```bash
python3 -m pytest tests_bugfix -k "197" -q
python3 -m pytest .smoke_tests/ -q
```
