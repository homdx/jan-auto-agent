# HARDEN-18 — two sections that share a line trade their keys on a reorder; a key call the model splits over lines is written with the placeholder; the Coder writes a non-string `content` as its Python repr; `/edit "my notes.txt"` keeps the quotes and the hint is not shell-safe

**Status:** landed (round 316: sensenova-6-8-flash-lite-var2 as the base — the restore pairs the masked lines of a replaced region with the placeholder lines of the reply by order and refuses a count that differs (A), a masked line is paired only on an anchor the file holds once and the writer checks the placeholders of the reply the model sent (D), `_content_to_text` joins a list of strings and rejects every other non-string (B), `_unquote_path` and a `shlex.quote`d hint (C), a backup keyed on the resolved path with "not recorded" as the test (E); judging added a line-identity rule for one-line sections (`"work": {"api_key": …}` reordered with a comma moved crossed two keys in the base and in every entry), an error that names the item and the type, the inner-quote rule of agnes/var4, a resolved backup key from step-5 and step-5's best-of-three `test_check_test_clocks`)
**Severity:** HIGH (D: a key written under another account's header, silently; A: silent loss of a real key, success reported); MEDIUM (B: silent loss of the code); LOW (C, E: id-less tasks only)
**File:** `tools/llm_stream.py` (A, D), `tools/auto/coder.py` (B, E), `tools/actions.py` (C)
**Symbol:** D: `restore_masked_secrets`, `_same_place`, the head/tail pairing. E: `Coder._backup_is_new`. A: `restore_masked_secrets` and its callers `Coder._write_files`, `OrchestratorActions.run_edit`. B: `Coder._parse_response`. C: `_parse_edit_command` / `_parse_payload_and_file`, the `restore with:` hint line of `run_edit`
**Round:** 316
**Size:** S
**Depends on:** HARDEN-13 (311, the restore this extends; landed in `0815666`)
**Also touches:** `tests_bugfix/` pins of HARDEN-9 A, HARDEN-12, HARDEN-13 A (must stay green), `tests/`

---

## Why

An outside review of `264440c` reported bugs #34–#36. They were re-run on `277d632`. #34 is **mostly** closed by HARDEN-13 (the change-around-the-key forms keep the key); one form is not. #35 and #36 are real and are in no ticket.

---

## Part A — the model rewrites the key call over several lines: the placeholder is written and the edit is approved

### What happens today

On `277d632`, the file holds `client = OpenAI(api_key="sk-proj-…", timeout=30)`; the model sees it masked and answers

```
client = OpenAI(
    api_key="here_your_key",
    timeout=30,
)
```

`restore_masked_secrets(original, reply)` returns a text with `here_your_key` and without the key; `run_edit` prints `status=approved` and writes it, and the real key exists only in the `.bak`. The one-line forms (`api_key=…, timeout=30` added, wrapped in `try:`, `'` for `"`, variable renamed) keep the key since HARDEN-13.

### What it does

* A placeholder in the reply that the original did not hold, and that the restore could not pair with a masked original line, is **never written**: the write is refused, the file is left as it was, the user sees the reason and the key is not printed. This is the HARDEN-13 "a failed restore refuses the write" rule; today it does not fire for this shape — find why (the mask lines of the original and the reply are not the same lines) and fix it there, not with a special case for `OpenAI(`.
* Better, where it is safe: pair the masked lines of the original with the placeholder lines of the reply **by order inside the changed region** (k masked lines in a replaced block, k placeholders in the reply) and restore; refuse when the counts differ.
* The same through `Coder._write_files` (the check runs where the file is written, for both writers).

### Tests (sketch)

The split-over-lines reply above, through `restore_masked_secrets`, `run_edit` (file unchanged, no `approved`) and `Coder._write_files` (file unchanged, error set); the four one-line forms still keep the key; two keys in one file, one call split, the other untouched → both keys kept or the write refused, never one lost.

---

## Part B — `"content": [...]`, `0`, `false`, `{...}` is written as the repr

### What happens today

`Coder._parse_response` takes `str(content)`. `["import os", "print(os.getcwd())"]` becomes the file text `['import os', 'print(os.getcwd())']`; `0` → `0`, `false` → `False`, `{"a":1}` → `{'a': 1}`, with no error. For a `.py` file the repr is a valid expression statement, so `import app` still passes and the code is gone. The Architect already treats a non-string field as absent (`_to_str_or_empty`).

### What it does

* A list whose items are all strings is joined with `"\n"` (not through a helper that strips each item: indentation must survive) and a final newline is added once.
* Any other non-string `content` (number, bool, dict, mixed list) is a parse error naming the field, as a missing `content` is; no file is written for that item.
* `content: ""` (an empty string) stays what it is today.

### Tests

The four cases above plus a list with an indented line (indentation kept) and a list with a non-string item (error).

---

## Part C — a quoted path in `/edit`, and the unquoted `mv` hint

### What happens today

`/edit "my notes.txt" :: append a line` → `Not a file: …/"my notes.txt"`: the quotes stay in the name. After an edit of a file with a space the hint line prints `mv <dir>/my notes.txt.bak.105124 <dir>/my notes.txt`, four arguments, which fails and helps no one (`92dcdf7` quoted the contest run hint; this one was missed).

### What it does

* A matching pair of single or double quotes around the file part is stripped; an unquoted path works as before; a name with an inner quote is left alone.
* The `restore with:` hint is built with `shlex.quote` for both paths.

### Tests

`/edit "my notes.txt" :: …` and `'my notes.txt'` edit the file; the hint for a spaced name, run through `shlex.split`, has exactly three words; plain names print as before.

---

## Part D — two sections that share a line trade their keys when the model reorders them

### What happens today

`agents.ini` holds `[openai_personal]` and `[openai_work]`, each with the same `base_url = https://api.openai.com/v1` line above its key (and the same `model = gpt-4o` below it). The model sees both keys masked and is asked to put the work section first. On `5c403f1`, `restore_masked_secrets` returns

```
[openai_work]
api_key = sk-personal-…      <- the personal key under the work header
[openai_personal]
api_key = sk-work-…          <- the work key under the personal header
```

with `written=['agents.ini'] error=''` through `Coder._write_files`; `/edit` does the same. An identical line above the key, or an identical line after it (`max_tokens = 4096`), is enough. The restore decides "same place" from one neighbouring line's text, and the head/tail pairing of HARDEN-13 pairs trailing lines as they stand, so two sections that share a line look unchanged and a reorder reads as no change. Ticket 311 promised that no key is restored under another header; this breaks it. A credential that moves to the wrong account is worse than a lost one.

### What it does

* Pair a masked line with its original only on an **anchor that is unique** in the file (the section header, or the nearest line that occurs once). Matched entries must keep their order and their header; otherwise the placeholder stays in the reply and Part A's write refusal fires.
* A reorder of whole sections is restored by the header, never by a shared neighbour.
* Same through `Coder._write_files` and `run_edit`; no special case for `[openai_*]` or INI.

### Tests (sketch)

The two-section file above with the sections swapped: each key under its own header or the write refused, never crossed; an identical line above, one below, both; three sections rotated; a faithful reply (nothing moved) still restores every key; the HARDEN-13 pins stay green.

---

## Part E — the first write of an id-less task makes no backup

### What happens today

`Coder._backup_is_new` compares `self._backups.get(rel, "") != task_id`. With an empty `task_id` (`generate()` allows one) that is `"" != ""`, so the first write makes no `.coder.bak`. And `./app.py` and `app.py` are different keys, so the second write of the same file overwrites the backup with attempt 1, the only copy of the original key.

### What it does

"Not recorded for this file" is the test, not an empty-string default, and the key is the resolved path.

### Tests (sketch)

An empty task id: the first write makes the backup, the second keeps it; `./app.py` then `app.py`: one backup, the original's.

---

## Contest

One commit. The judge's checks are written after the round, from the entries; the entries' own tests are cross-tested on every other entry's code. The judge fuzzes the mask-and-restore round trip over random config files (sections that share lines, reorders, insertions, split calls) and expects a faithful reply to restore byte for byte and any other reply to restore every key under its own header or to be refused.

## Acceptance

**Layer 1 — offline.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** `/edit "put the work section first"` on a two-provider `agents.ini` with shared lines keeps each key under its own header.

## Not in scope

Rust, a secret that is not an `api_key` assignment (HARDEN-19), the verdict parser (HARDEN-17).

Why not the others: eight entries (nemotron produced no commit on the branch and was left with a test file and a `.bak`; glm's `_content_to_str` is not defined, so every `content` reply crashes). A black box over 40 checks (the five parts through `restore_masked_secrets`, `_write_files`, `_parse_response`, `run_edit` and a 120-config fuzz of the mask-reorder-restore round trip) scored the base 16, var2, var1 and step-5 39, var4 39, var3 38, laguna 37, agnes 34, glm 23, nemotron 16. The fuzz found a written placeholder with success reported in agnes, laguna and step-5 (a reorder of three sections lost a key), and a lost key of two split calls in var4 and laguna. var1 and var3 stalled and mixed in the work of HARDEN-19 (317), so var2, which is clean, was the base. The entries' tests ran against every implementation (90 cells): the failures that remain on the ideal are the other entries' private helper names (`_unquote_file_name`, `_BACKUP_NOT_RECORDED`) and the choice to refuse a split call that the ideal restores.

Open for the next tickets: a secret that is not an `api_key` assignment (HARDEN-19, 317) and the parser tickets that follow (320, 321, 322).
