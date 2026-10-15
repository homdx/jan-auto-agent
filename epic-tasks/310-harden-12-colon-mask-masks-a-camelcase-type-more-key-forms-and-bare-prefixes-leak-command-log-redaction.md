# HARDEN-12 — the colon-form mask rewrites a CamelCase type name into the placeholder; a single-quoted dict key, `os.environ["X_API_KEY"] = "…"`, `api_key => '…'` and an XML tag leave the machine; a bare Groq, GitHub, Hugging Face, xAI or AWS key in prose is not masked; the executor's command log keeps inline secrets

**Status:** open — FINAL, written 2026-10-15 against `5dfd4d5`. Four defects from outside reviews, each **reproduced by running the code** on that commit (the output is below). Part A is a regression of HARDEN-9 B (the ticket's own landed code), B and C are gaps of the same mask (`mask_api_key`), D is the executor's command redaction. The code is the authority, not this ticket: if a line has moved, the symbol names the place.
**Severity:** MEDIUM (A: a file edit that writes the masked line back turns a type into an undefined name, silently); MEDIUM (B, C: a secret reaches the provider); LOW-MEDIUM (D: a secret is kept in traces and progress logs)
**File:** `tools/llm_stream.py` (A, B, C), `tools/auto/executor.py` (D)
**Symbol:** A: `_looks_like_a_token`, `_value_is_secret_literal` (the `colon=True` branch), `_API_KEY_COLON_RE`. B: `_API_KEY_LINE_RE`, `_API_KEY_JSON_RE`, `_API_KEY_COLON_RE`, `mask_api_key`. C: `_SECRET_PREFIX_RE`. D: `_redact_command`, `_SECRET_OPT_RE`, `_SECRET_AUTH_RE`
**Round:** —
**Size:** M
**Depends on:** HARDEN-1 (298), HARDEN-5 (303), HARDEN-9 (307), all landed
**Also touches:** `tests/test_mask_api_key_forms.py`, `tests_bugfix/test_harden1_a8_mask_only_literals.py` and the HARDEN-9 mask tests (pins that must stay green), `tests/`, `tests_bugfix/`

---

## Why

`mask_api_key` runs on the wire, on every request, and `restore_masked_secrets` puts the real value back where the model returned the placeholder. A mask that rewrites **code** turns the edit into a broken file (Part A); a mask that misses a **form** sends the secret to the provider (Parts B, C); the command log is the other place a secret persists (Part D). A part is done when it has its tests and both suites are green unchanged.

---

## Part A — `api_key: AsyncOpenAIClientFactory` is a type, and the mask rewrites it

### What happens today

HARDEN-9 B masks the unquoted `api_key: <value>` line; for a bare identifier on the right it asks `_looks_like_a_token`, which is true when the value has a digit **or** has both upper-case and lower-case letters. A CamelCase class name has both. On `5dfd4d5`, through `mask_api_key`:

```
    api_key: AsyncOpenAIClientFactory,                        ->   api_key: here_your_key,
api_key: ProviderCredentialsHolder                            ->   api_key: here_your_key
api_key: SecretStr                                            unchanged
api_key: Optional[ProviderCredentialsHolder] = None           unchanged
def __init__(self, api_key: APIKeyProviderFactory, timeout: int = 30):    unchanged
api_key: sk-live-abcdef1234567890                             ->   api_key: here_your_key      (right)
```

An indented dataclass or Pydantic field is rewritten, the same name in a `def` line is not. The model sees the masked file, and the edit writes `api_key: here_your_key` back (restore only runs when the model returns the placeholder where a real secret stood): the annotation names a class that does not exist and the module fails on import. Nothing reports it.

### What it does

* A bare identifier on the right of the colon is a literal only when it has the shape of a credential and not of a name: a CamelCase or snake_case word of letters is a type, a class or a variable and stays as written, whatever its length and case; a value is a token when it carries digits among its letters (`AKIAIOSFODNN7EXAMPLE`, `ghp_…0123`), or a known prefix. The rule for the `=` form is unchanged.
* The quoted forms, a URL and the `sk-…` family are as they are.

### Tests (sketch)

The table above; the type-annotation lines and `def` signatures of this repository's own sources (and a dataclass field per CamelCase name in `tools/`) are byte-identical before and after; the credential shapes in the HARDEN-9 B table are still masked.

---

## Part B — more forms of a key line reach the provider

### What happens today

Through `_mask_payload_secrets` / `mask_api_key` on `5dfd4d5` (a key of 36 characters after its prefix):

```
{'api_key': 'xai-…'}                      (a Python dict, single quotes)      sent unchanged
os.environ["GROQ_API_KEY"] = "gsk_…"      (an assignment through the environ) sent unchanged
api_key => 'gsk_…'                        (a Ruby or PHP hash)                sent unchanged
<api_key>gsk_…</api_key>                  (XML)                               sent unchanged
api_key: gsk_…        apiKey: 'hf_…'      X-Api-Key: gsk_…      export GROQ_API_KEY=gsk_…       masked (controls)
```

`_API_KEY_JSON_RE` takes a double-quoted key name only, and nothing takes `os.environ[...] =` with a subscript on the left. A key with an `sk-` prefix is caught by the prefix rule wherever it stands; Groq, Hugging Face, xAI and Mistral keys are not.

### What it does

* A key name in single quotes, followed by `:` or `=>`, with a quoted value, is a key line; so is `os.environ["NAME"] = "value"` / `os.environ['NAME'] = 'value'` where NAME names a key (contains `KEY`, `TOKEN`, `SECRET` or `PASSWORD`, any case), and an element `<api_key>value</api_key>` (the tag name is a key name). The same literal test decides what a value is, so `os.environ["X_API_KEY"] = os.getenv("Y")`, `{'api_key': None}`, `{'api_key': value}` and a type annotation stay as they are, and the quotes are kept.

### Tests (sketch)

The four lines above masked in a file, in a request payload, and restored by `restore_masked_secrets` when the model returns the masked line; the controls unchanged; the HARDEN-1 A8, HARDEN-5 B and HARDEN-9 tests green.

---

## Part C — a bare key of a common provider, with no `api_key =` label, is not masked

### What happens today

`_SECRET_PREFIX_RE` knows `sk-or-v1-`, `sk-proj-`, `sk-`, `AIza` and `xoxb-`. On `5dfd4d5` these are sent as they stand when they appear in prose, a comment, a log line or a Markdown file:

```
use gsk_<36 chars> to log in                          sent unchanged   (Groq)
token ghp_<36 chars>   /   github_pat_11AB…_<59>     sent unchanged   (GitHub)
hf_<36 chars> here                                    sent unchanged   (Hugging Face)
xai-<36 chars>                                        sent unchanged   (xAI)
key AKIAIOSFODNN7EXAMPLE                              sent unchanged   (AWS)
```

### What it does

* The prefix rule also knows `gsk_`, `ghp_` / `gho_` / `ghs_` / `ghu_` / `github_pat_`, `hf_`, `xai-`, `glpat-`, `AKIA` / `ASIA`, with the long tail each of them has, so a shorter word that merely starts the same way (`hf_model`, `gsk_`, `AKIA` in a sentence) is untouched; the existing `sk-real-1` pin stays.
* A value masked this way is masked on a second pass as it is now.

### Tests (sketch)

The table above and its short look-alikes; the repository's own `.py`, `.md` and `.ini` sources are byte-identical before and after (the run compares).

---

## Part D — the command log keeps a secret passed as an environment variable, a header or a URL

### What happens today

`_redact_command` replaces the value of `--api-key`/`--token`/`--password` options and of an `Authorization:` header only. The command is stored on every `ExecutionResult` and reaches the trace JSONL and the progress log. On `5dfd4d5`:

```
OPENAI_API_KEY=sk123abc4567890 pytest                   kept
API_KEY=abcd1234efgh python run.py                      kept
HF_TOKEN=hf_abcdefgh12345678 python x.py                kept
curl -H 'x-api-key: abcd1234efgh5678' http://x          kept
curl -u user:secretpass1234 http://x                    kept
docker login -p secretpass1234                          kept
mysql -psecretpass1234                                  kept
git clone https://user:tokenabc12345@host/r.git         kept
pytest --api-key=abc12345                               redacted   (control)
```

A key given to a test command as an inline `NAME=value` is the usual way to hand it over.

### What it does

* `NAME=value` at the start of a command (or after `;`, `&&`, `|`) where NAME contains `KEY`, `TOKEN`, `SECRET`, `PASSWORD` or `PASSWD` has its value replaced; so do the `x-api-key:` header, the password of `user:pass@` in a URL, and the argument of `-u user:pass`, `-p value` / `-pvalue` for the commands that take them. The name and the rest of the command stay; a command without a secret is returned byte for byte.

### Tests (sketch)

The table above, each form with and without quotes; `ls -p`, `grep -p pat`, `pytest -p no:cacheprovider`, `NAME=value` with a name that holds none of the words, and a URL without a password are unchanged.

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. The judge runs the mask over this repository's own sources before and after and expects no line of code to change.

## Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** A dataclass with `api_key: ProviderCredentialsHolder` sent through `/edit` comes back with the annotation untouched; a Python config with `{'api_key': 'xai-…'}` and `os.environ["GROQ_API_KEY"] = "gsk_…"` reaches the provider with `here_your_key` in their place; a run with `HF_TOKEN=… pytest` leaves `[REDACTED]` in the trace.

## Not in scope

A key with no label and no known prefix (opaque hex or a UUID alone in prose), a codec guess, and the restore by position (HARDEN-11, ticket 309).
