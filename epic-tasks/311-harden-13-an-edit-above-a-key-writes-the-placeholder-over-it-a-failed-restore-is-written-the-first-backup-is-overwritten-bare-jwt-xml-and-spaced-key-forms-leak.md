# HARDEN-13 — an ordinary edit above a key line makes the restore write `here_your_key` over the real key; a masked line that could not be restored is written anyway; a retry overwrites the only backup; a bare JWT, an XML element and a spaced `apikey <token>` leave the machine, and a digit-free token after a colon is not masked

**Status:** open
**Severity:** HIGH (A: a routine config edit destroys a working key on disk, the write reports success); MEDIUM (B: after A the key survives only in a backup the next write replaces); MEDIUM (C: a secret reaches the provider); LOW (A3)
**File:** `tools/llm_stream.py` (A, C), `tools/auto/coder.py` (A2, B), `tools/actions.py` (A2)
**Symbol:** A: `restore_masked_secrets`. A2: `Coder._write_files` (the restore call), `/edit` (`run_edit`, the restore call). B: `Coder._write_files` (the `.coder.bak` copy). C: `_SECRET_PREFIX_RE`, `_identifier_is_a_credential`, `_API_KEY_LINE_RE`, `mask_api_key`
**Round:** (set when the round starts)
**Size:** M
**Depends on:** HARDEN-5 (303), HARDEN-9 (307), HARDEN-11 (309), HARDEN-12 (310), all landed
**Also touches:** `tests/test_mask_api_key_forms.py`, `tests_bugfix/test_harden11_*`, `tests_bugfix/test_restore_*` (pins that encode today's "left as the placeholder" rule: each one that changes is a decision the commit states), `tests/`, `tests_bugfix/`

---

## Why

`mask_api_key` hides the real key from the model and `restore_masked_secrets` puts it back where the model returned the placeholder; the writer (`Coder._write_files`, `/edit`) stores the result. HARDEN-11 A made the restore safe against a key landing under the wrong provider, and the price is that it now gives up whenever the structure around a key moved by one line. Four independent reports found the same hole on `f2a66db`; the rest of this ticket is the masks that still miss a form. A part is done when it has its tests and both suites are green unchanged (except the pins named above).

---

## Part A — a restored key is filed under the nearest line above it, and one added line above it loses the key

### What happens today

Every masked line is filed under its **place**, the nearest preceding line the mask left alone. Every key masks to the same text, so the match cannot be made by text, and any line inserted or edited between the header and the key moves the place. On `f2a66db`, through `mask_api_key` → edit → `restore_masked_secrets` (two providers, `A` = `sk-live-AAAA…`, `B` = `sk-live-BBBB…`):

```
edit the model made                                   key A on disk    key B on disk
none                                                   real            real
max_tokens = 4000 added under [provider_a]             placeholder     real
# primary comment added under [provider_a]             placeholder     real
base_url edited directly above the key (ini)           placeholder     real
timeout=30, added above one of two api_key="…", calls  placeholder     real
a comment added above each of two keys                 placeholder     placeholder
the whole file re-indented                             placeholder     placeholder
a section renamed ([groq] → [groq_fast])               real            placeholder
the header line edited ([openai]  ; main)              placeholder     real
```

Each lost key prints one `logger.warning` (`has no original line in the same place — left as the placeholder`), and `Coder._write_files` and `/edit` write the file and report success. The base_url above an `api_key` is the common layout of `agents.ini`, and "change the URL of one provider" is the commonest edit of it. HARDEN-5 B says the file must be byte-identical except for the lines the task asked for; here an unrelated line becomes a placeholder.

### What it does

* A masked line is restored by **aligning the reply against the masked original** (a sequence alignment on whole lines), not by the text of one line above it: a masked original line that sits in an unchanged stretch of the reply gets its original text back, and an inserted, edited or re-indented line changes only its own stretch. The reports' prototype is `difflib.SequenceMatcher(a=masked_original_lines, b=reply_lines, autojunk=False).get_opcodes()` with the `equal` blocks restored; any alignment that gives the table below is fine.
* No key is ever restored under another header than the one it was under: the HARDEN-11 A table (delete a section, add a section, reorder the sections, swap two sections) keeps its outcome, and a key outside any unchanged stretch stays the placeholder as today (Part A2 then decides what happens to the write).
* A file re-indented as a whole keeps its keys (compare the lines with their leading whitespace removed, and give the line the reply's own indentation).

### Tests (sketch)

The table above with the expected result "real, real" for every row; the HARDEN-11 A cases unchanged in outcome; two keys in one section; a key line edited by the task itself (`api_key = os.environ["K"]`) stays as written.

---

## Part A2 — a masked line that could not be restored is written to disk

### What happens today

When Part A's restore gives up, `restore_masked_secrets` returns the text with `here_your_key` in it, and both writers store it: `Coder._write_files` (the restore at `content = _llm_stream.restore_masked_secrets(…)`) and `/edit`. The only trace is a warning in the log. A working key is replaced by a dummy and the task is reported done.

### What it does

* After the restore, a reply line that equals the masked text of an original line the mask changed and that was **not** restored is a failed restore. The write is refused: `Coder._write_files` takes it as a refusal (`first_error` naming the file and the line, as `[SAFETY]` does, so the loop retries with that message in its feedback), and `/edit` prints the file and the line and leaves the file unchanged. A reply that **removes** the key line, or replaces it with another expression, is not a failed restore and is written as before.
* A file that holds the placeholder text on purpose (a documented example, an `.env.example` whose line is `api_key = here_your_key` in the original too) is not refused: the original line is the same text, so nothing was masked.

### Tests (sketch)

The unrestorable cases of Part A (a reordered pair of sections) refuse the write through the real `_write_files` and `/edit` and leave the file byte-identical; a reply that deletes the key line is written; an original that already contains the placeholder text is written.

---

## Part A3 — a key line followed by a Unicode line separator keeps its placeholder

### What happens today

`restore_masked_secrets` drops the reply line's own ending only for `\n` and `\r`, while `str.splitlines()` also ends a line at U+2028, U+2029, `\x0b`, `\x0c`, `\x1c`–`\x1e` and `\x85`. `api_key = sk-…<U+2028>` keeps the placeholder after a faithful round trip (the other layouts, CRLF, BOM, lone CR, tabs, are byte-identical).

### What it does

The original's line and the reply's line are cut by the same rule and the line's own ending, whatever `splitlines` took, is put back.

### Tests (sketch)

`api_key = <value>` + each of U+2028, U+2029, `\x0b`, `\x0c`, `\x85` round-trips byte-identical.

---

## Part B — a later write of the same task replaces the only backup

### What happens today

`Coder._write_files` does `backup.unlink(missing_ok=True); shutil.copy2(dest, backup)` before **every** write, so the second attempt of a task copies the file the first attempt wrote over the backup of the original. After a first write that lost a key (Part A) the real key is in `clients.py.coder.bak` only, and after the retry no file in a plain (non-git) folder holds it any more.

### What it does

* Within one task's run the backup of a file is made by the **first** write and kept by the later ones; a new task run (a new `task_id`) starts again. A stale `.coder.bak` from an earlier run is replaced by the first write of this one, as today.
* The delete branch (`delete: true`) follows the same rule.

### Tests (sketch)

Two writes of one file in one task: the `.coder.bak` is the original bytes after both; a second task replaces it; the mode of the backup is the original's.

---

## Part C — secret forms the mask still lets through

### What happens today

On `f2a66db`, `mask_api_key` leaves these unchanged (`V` is a 32-character token with letters and digits):

```
<api_key>V</api_key>        <apiKey>V</apiKey>       <key>V</key>                 XML element
<add key="api_key" value="V"/>                                                    XML attribute pair
apikey V        api_key V       x-api-key V        API key: V                    a space instead of = or :
use eyJhbGciOi….eyJzdWIiOi….SflKxwRJSM… to log in      token: eyJ….eyJ….SflK…      a bare JWT, no key name
api_key: qLKjHGfdSaZXcVbNmPoiUyTzAbCdEfGhIjKlMnOp                                a 40-letter token with no digit, after a colon
```

`apikey=V`, `apikey: V`, `X-Api-Key: V` and `Authorization: Bearer V` are masked (the controls). The last row is a gap HARDEN-12 A opened: a bare identifier after a colon is a credential only with a digit or one of ten provider prefixes (so `api_key: ProviderCredentialsHolder` stays a type), which throws out every digit-free token that is not one of those providers.

### What it does

* **XML:** the text of an element whose tag name is a key name (`api_key`, `apiKey`, `ApiKey`, `apikey`, `secret_key`: the name list the `=` form uses; a bare `<key>` counts only when the line or the parent names an API key) is a value, judged by `_value_is_secret_literal`; and `key="api_key" value="V"` (a pair of attributes) masks the `value`. Do **not** write one regular expression for the whole shape: find the element or the attribute pair first, then ask `_value_is_secret_literal` about the text, in two steps.
* **A space:** `<name> <value>` where the name is a key name and the value is a **token** (at least 20 characters, letters and digits, no spaces) is masked; `api_key is required` and `apikey of the provider` stay prose.
* **A bare JWT:** three base64url runs joined by dots, the first starting `eyJ`, is masked wherever it stands, with a key name or not. Test the runs one by one (split on the dots, check each), not with one pattern.
* **A digit-free token after a colon:** a bare run of letters after `api_key:` is a credential when it does not read as a CamelCase **name**: judge the humps (a name is words of three or more letters, `Provider|Credentials|Holder`, with at most one short acronym, `Async|Open|AI|Client|Factory`; a token has several one- or two-letter humps, `q|LKj|HG|fd|Sa|ZX|c|Vb`) and the length (a name is rarely over 32 letters). Do it in two small checks, not one regex. All 20 type names of the HARDEN-12 A table stay untouched.
* A masked value stays masked on a second pass, and `restore_masked_secrets` restores each of the new forms.

### Tests (sketch)

The table, each form with and without indentation and a trailing comma; the controls above; prose that mentions a key (`set the api_key first`, `apikey of the provider`); the 20 type names of HARDEN-12 A; the mask over this repository's own `.py` files, `.ini` files and `docs/` changes no line of code.

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. The judge runs the mask over this repository's own sources before and after and expects no line of code to change, and drives `Coder._write_files` and `/edit` for Parts A and A2.

## Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged except the HARDEN-11 pins named in the header.
**Layer 2 — the operator.** An `agents.ini` with three providers, edited with "change the base_url of provider two" through `/edit` and through a Coder task, keeps all three keys; a `web.config` with `<api_key>…</api_key>` and a `.env` with a JWT reach the provider with `here_your_key` in their place.

## Not in scope

Rust (the project does not fix it), an unlabelled hex or UUID in prose (not told from data), `token:` and `password:` names whose value has no known prefix (the function masks API keys), and a codec guess. The verdict parser, the block extractor, `/edit`'s codec and line endings and the judge script are HARDEN-14 (ticket 312).
