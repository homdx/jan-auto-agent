# HARDEN-18 — a key call the model splits over lines is written with the placeholder; the Coder writes a non-string `content` as its Python repr; `/edit "my notes.txt"` keeps the quotes and the hint is not shell-safe

**Status:** open
**Severity:** HIGH (A: silent loss of a real key, success reported); MEDIUM (B: silent loss of the code); LOW (C)
**File:** `tools/llm_stream.py` (A), `tools/auto/coder.py` (B), `tools/actions.py` (C)
**Symbol:** A: `restore_masked_secrets` and its callers `Coder._write_files`, `OrchestratorActions.run_edit`. B: `Coder._parse_response`. C: `_parse_edit_command` / `_parse_payload_and_file`, the `restore with:` hint line of `run_edit`
**Round:** (set when the round starts)
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
