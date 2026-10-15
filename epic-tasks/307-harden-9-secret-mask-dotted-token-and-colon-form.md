# HARDEN-9 — the secret mask lets a dotted token and an unquoted `api_key: <value>` line reach the model, a Western legacy file is read as cp1251, an arrow function in an object literal is "not found", and two operator-script gaps (`parse_sets`, `build_ini`)

**Status:** landed (round 307: sensenova-var3 as the base — a dotted token and the unquoted `api_key: <value>` line are masked (A, B), the codec of a Western file is decided from the script of each word, with a coding cookie winning and a refusal naming the character (C; the per-word score is sensenova-var2's), a repeated option and a missing ini section are refused by name (D), a property arrow function of an object literal is found in its own pass behind every definition found today (E)). The code is the authority, not this ticket: if a line has moved, the symbol names the place.
**Severity:** HIGH (A: a JWT or an OAuth token in a `.env` or INI line leaves the machine); MEDIUM (B); MEDIUM-LOW (C); LOW (D); MEDIUM (E)
**File:** `tools/llm_stream.py` (A, B), `tools/actions.py` and `tools/auto/coder.py` (C), `scripts/gate1_vote_layer2.py` (D), `tools/block_extractor.py` (E)
**Symbol:** `_value_is_secret_literal`, `_EXPRESSION_CHARS`, `_API_KEY_LINE_RE`, `mask_api_key`, `_mask_payload_secrets`. C: `_edit_write_encodings`, `_file_encodings`, `_file_read_encoding`, `_pick_file_encoding`. D: `parse_sets`, `build_ini`. E: `extract_block` and the candidate patterns of a JS or TS definition head
**Round:** 307
**Size:** S
**Depends on:** HARDEN-1 (298), HARDEN-5 (303), both landed
**Also touches:** `tests/test_mask_api_key_forms.py`, `tests_bugfix/test_harden1_a8_mask_only_literals.py` (pins that must stay green), `tests_bugfix/`

Why not the others: in a black box that drives each tree (180 checks over A to E: dotted tokens, every YAML form and its controls, the codec of Western, Russian and mixed files and the cookie, the script options, the arrow properties in four extensions), sensenova-var2 passed 176, var3 175, step-5-preview 172, var4 171, nemotron 153, var1 142, laguna 128, agnes 94 (the base) and glm 90. The cross matrix put var3 first by a distance (its tests 286/286 on its own code and the most on the others'): its arrow property is a separate pass behind every definition found today, where var2's pattern in the braced pass out-ranked a real definition; its identifier-path test keeps `client.v2Endpoint` as code, where var2 masks it. Its codec guess read a long Russian file with one English word as cp1252, step-5 and var4 did the same; var2's per-word score does not, and is used. Added to it: `api_key: $VAR` is a reference and stays as written, and the coding cookie test no longer pins the order behind the cookie.

Found at judging and fixed in the same commit (present on the base, not a regression of this round): a C++ stream insertion that ends in a qualified call, `std::cout << Widget::label();`, was answered as the body-less declaration of `label`; `_head_prefix_kind` now treats a prefix with `<<` or ending in `<` as a call's position (`tests/test_cpp_stream_insertion_is_not_a_declaration.py`).

---

## Why

`mask_api_key` runs on the wire, on every request, so what it lets through is sent to the provider. HARDEN-1 A8 and HARDEN-5 B made it leave code alone (`self.api_key = token`, `os.environ["X"]`); the price was that a **literal** which merely contains a dot, and a literal in the YAML form, are now taken for code or are not looked at. A part is done when it has its tests and both suites are green unchanged.

---

## Part A — a dotted token is taken for an attribute access

### What happens today

`_value_is_secret_literal` returns `False` for any bare value containing a character of `_EXPRESSION_CHARS`, and `.` is one of them (it is there for `config.api_key`). On `0d28a9b`, through `mask_api_key`:

```
API_KEY=eyJhbGciOiJIUzI1NiIs….eyJzdWIiOiIx….SflKxwRJSMeK…      (a JWT)        sent unchanged
OPENAI_API_KEY=ya29.a0AfH6SMBx9abcdefghij1234567890                             sent unchanged
api_key = eyJhbGci….eyJzdWIi….SflKx…  (INI form)                                sent unchanged
```

The same lines leave unmasked on `f50d9a2`, the commit before HARDEN-5, so this is an old gap and not something HARDEN-5 introduced.

### What it does

* A bare value with dots is a literal when it does **not** read as code: it is not `name.name…` of identifier parts that look like words, a call `name(…)` or an index `name[…]`; it is a long (16 characters or more) token whose parts mix letters and digits, or whose parts are base64url runs (a JWT has three of them). `config.api_key`, `self.settings.api_key`, `os.environ.get`, `settings.OPENAI_KEY` stay as they are.
* A masked value stays masked on a second pass, as now.

### Tests (sketch)

The three lines above are masked, in `.env`, INI, `export` and commented forms; the identifier-path lines of the HARDEN-1 A8 and HARDEN-5 B pins are unchanged.

---

## Part B — `api_key: <value>` without quotes is not looked at

### What happens today

`_API_KEY_LINE_RE` takes `=` only, and `_API_KEY_JSON_RE` takes a quoted value; a YAML line with a colon and no quotes is matched by neither. It is masked only when the value carries a known provider prefix (`sk-…`, which `_SECRET_PREFIX_RE` finds anywhere). On `0d28a9b`:

```
api_key: 0123456789abcdef0123456789abcdef                   sent unchanged   (hex)
api_key: 123e4567-e89b-12d3-a456-426614174000               sent unchanged   (uuid)
api_key: dGhpc2lzYXNlY3JldGtleXZhbHVlMTIzNDU2               sent unchanged   (base64)
api_key: AKIAIOSFODNN7EXAMPLE                               sent unchanged
api_key: ghp_abcdefghijklmnopqrstuvwxyz0123456789           sent unchanged
api_key: sk-abcdefghijklmnopqrstuvwxyz0123                  masked           (the control)
```

### What it does

* A line `<name>api_key: <value>` (YAML, with or without a leading `- ` or indentation) is masked by the same rule as the `=` form, `_value_is_secret_literal` deciding what a literal is, so a placeholder (`<your key>`, `changeme`) and a name (`api_key: api_key`) stay as they are and the quotes of a quoted value are kept.
* A Python dict entry, a type annotation (`api_key: str`, `api_key: Optional[str] = None`) and a `def f(api_key: str)` signature are code and are not touched: a value that is a bare identifier, a type or an expression is not a literal.

### Tests (sketch)

The table above; the type-annotation lines and `def` signatures of this repository's own sources are byte-identical before and after (the test runs the mask over `tools/` and `main.py` and compares).

---

## Part C — a Western cp1252 or latin-1 file is read as cp1251

### What happens today

`_edit_write_encodings` (used by `/edit` and, through `_file_encodings`, by the Coder since HARDEN-5 D) tries `cp1251` first and accepts it when the decoded text encodes back to the same bytes. `cp1251` and `cp1252` define almost every byte, so a Western file always round-trips as `cp1251` and the check never fails. On `0d28a9b`, for files written to a directory and read through `_file_encodings` / `_file_read_encoding`:

| file | codec chosen | what the model is shown |
|---|---|---|
| `Café au lait, naïve façade, déjà vu` saved as cp1252 | `cp1251` | `Cafй au lait, naпve faзade, dйjа vu` |
| `b"caf\xe9\n"` | `cp1251` | `cafй` |
| `b"Gr\xfc\xdfe \xf6l\n"` | `cp1251` | `GrьЯe цl` |
| `Привет мир` saved as cp1251 | `cp1251` | correct |

The model copies the garbage back, and a revision that adds a genuine Western letter is refused: `_pick_file_encoding(('cp1251', 'cp1252', 'latin-1'), "Cafй au lait, naпve faзade\nCrème brûlée\n")` returns `(None, '')`, which the Coder reports as "missing some characters" with nothing named, and `/edit` refuses the same way. Unchanged text round-trips by luck.

### What it does

* The first candidate is the codec whose decoded letters look like the file's script: for each candidate that round-trips, count the letters it gives (Cyrillic against Latin with accents) and prefer the one that gives text of one script; a file whose high bytes decode to Latin letters in `cp1252` and to a mix of Cyrillic and Latin in no codec is read as `cp1252`. A real Russian `cp1251` file is read as `cp1251` as now, and `latin-1` stays last.
* A `# -*- coding: latin-1 -*-` (or `# coding=cp1252`) line in the first two lines of a source file names the codec, and it wins. When the Coder refuses a write because no codec holds the reply, the message names the characters that are the problem, not "some characters".
* A UTF-8 file still returns exactly `("utf-8",)`. The one decision stays in `_edit_write_encodings`, so `/edit` and the Coder cannot disagree.

### Tests (sketch)

The table above through `_file_read_encoding` and `_edit_write_encodings`; an added Western line is written back in `cp1252`; a Russian `cp1251` file, a UTF-8 file, a UTF-8 file with a BOM and a pure-ASCII file are unchanged; the CC-16 Part C and HARDEN-5 D tests green unchanged.

---

## Part D — the operator script `scripts/gate1_vote_layer2.py`: a repeated option is a second variant, a base ini without a section is a `KeyError`

### What happens today

* `parse_sets("3+veto,3+veto+veto")` returns two variants, `(3, 'all', ('veto',))` and `(3, 'all', ('veto', 'veto'))`: the duplicate check compares the sorted option tuples, which differ, so the second is accepted and both run (and pay) as the same variant.
* `build_ini` indexes `cfg["collect"]`, `cfg["gate1"]`, `cfg["gate1_llm"]` and `cfg["loop"]` without looking: a base ini without a `[collect]` section ends with `KeyError: 'collect'` and a traceback instead of a message naming the file and the section.

### What it does

* The options of a set are taken as a set: a repeated option is read once, or refused naming it, and two sets with the same options are the "named twice" refusal that already exists.
* A missing section of the base ini is a `SystemExit` that names the section and the file (or the section is created), not a traceback.

### Tests (sketch)

`parse_sets("3+veto,3+veto+veto")`; `build_ini` with a base ini lacking `[collect]`, `[gate1]`, `[loop]` and `[gate1_llm]` in turn; the existing `parse_sets` tests green unchanged.

---

## Part E — an arrow function that is a property of an object literal is "not found"

### What happens today

HARDEN-4 D2 added `const f = (x) => {…}`, a class field and a method; a property of an object literal, `name: (x) => {…}`, has no `const` and no `function` before it and matches none of the patterns. On `0d28a9b`, for `.js` and `.ts`:

```
const api = { fetchUser: (id) => {…},  save: async (u) => {…},  plain(x) {…},  old: function (y) {…} };  export const top = (z) => {…};

fetchUser   ''   (not found)
save        ''   (not found)
plain       found
old         found
top         found
```

`''` is "not found": Gate 1 drops the task that cites the function and the grounding falls back to the head of the file. Handlers, reducers, route tables and action maps are written this way.

### What it does

* `name: (args) => {…}`, `name: async (args) => {…}`, `name: args => {…}` and the single-expression form, as a property in an object literal, are found; the block is the property with its body, up to the closing brace (and the comma is not part of the body's end).
* No result changes for any name the extractor finds today.

### Tests (sketch)

The rows above for `.js`, `.ts`, `.jsx`, `.tsx`; a property whose value is not a function (`name: 5`, `name: value`) is not a definition; the HARDEN-4 and HARDEN-6 tables green unchanged.

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. The judge runs the mask over this repository's own `.py` files before and after and expects no line of code to change.

## Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** A `.env` with a JWT and a YAML config with a hex key, sent through `/edit`, reach the provider with `here_your_key` in their place, and the edit writes the real values back.

## Not in scope

A bare identifier on the right of `=` that does not name the key (`self.api_key = token`): the mask rewrites it on the wire and `restore_masked_secrets` puts the real line back on write (HARDEN-5), so nothing reaches the disk changed. Secrets in places other than a key line (a bare token on a line by itself), and a codec guess for East Asian or other legacy encodings.
