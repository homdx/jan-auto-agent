# HARDEN-15 — the mask rewrites code: a string that ends in `api_key=` gets a quote and a placeholder, a format placeholder and `$KEY` are masked, a query tail is eaten, an `sk-` CSS class and an SSH key type become `here_your_key`; the command log keeps `redis-cli -a` and `aws configure set` and over-redacts `mysql -p mydb`

**Status:** landed (round 313: sensenova-6-8-flash-lite-var3 as the base — a quote that closes a string is not a value, a format or shell reference is not a secret, an unquoted value stops at `&` and `#` (A), an `sk-` slug is not a key (B), the command log redacts `redis-cli -a` / `--pass`, a `redis://:pw@host` URL and `aws configure set`, and keeps `mysql -p mydb` (C))
**Severity:** MEDIUM-HIGH (A: the model is shown broken code and an `/edit` writes a syntax error to disk); LOW-MEDIUM (B); LOW (C: logs only)
**File:** `tools/llm_stream.py` (A, B), `tools/auto/executor.py` (C)
**Symbol:** A: `_API_KEY_LINE_RE`, `_mask_assignment`, `mask_api_key`, `_value_is_secret_literal`. B: `_SECRET_PREFIX_RE`. C: `_redact_command`, `_SECRET_OPT_RE`
**Round:** 313
**Size:** S
**Depends on:** HARDEN-1 (298), HARDEN-9 (307), HARDEN-10 (308), HARDEN-12 (310), HARDEN-13 (311, tests of the restore), all landed
**Also touches:** `tests/test_mask_api_key_forms.py`, `tests_bugfix/test_harden1_a8_mask_only_literals.py` and the HARDEN-9/10/12 mask tests (pins that must stay green), `tests/`, `tests_bugfix/`

---

## Why

HARDEN-1 A8, HARDEN-5 B and HARDEN-10 B taught the mask to leave code alone. These are the shapes of code it still takes for a secret; the model is shown the rewritten line and an edit that touches it writes the rewritten line to disk (the restore only recovers a line the model left identical). A part is done when it has its tests and both suites are green unchanged.

---

## Part A — `api_key=` at the end of a string is not an assignment

### What happens today

Putting a key into a URL by concatenation is a common idiom. On `f2a66db`, through `mask_api_key`:

```
url = "https://a.com/d?api_key=" + key + "&format=json"        ->  url = "https://a.com/d?api_key="here_your_key"&format=json"
r = requests.get("https://a.com/v1/q?api_key=" + os.environ["K"], timeout=5)
                                                               ->  …?api_key="here_your_key"K"], timeout=5)
params = "api_key=" + key + "&q=" + q                           (masked)
log.info("using api_key=%s", key)                               ->  "using api_key=here_your_key", key)
curl "https://x.io/q?api_key=$KEY&lang=en"                      ->  …?api_key=here_your_key"       (the &lang=en is gone)
"https://host/v1?api_key=abc123def456&x=1&y=2"                  ->  …?api_key=here_your_key"       (the &x=1&y=2 is gone)
```

The closing quote of a string is taken for the opening quote of a value, a `%s` / `{}` / `$VAR` reference is taken for a literal, and an unquoted value runs through the `&`. An `/edit` that changed only `json` to `xml` in the first line wrote `…api_key="here_your_key"&format=xml"` to the file, a syntax error with the `+ key +` expression gone.

### What it does

* A quote that directly follows `api_key=` and **closes** a string opened earlier on the same line is not the opening quote of a value: the line is code and is left as written.
* A value that is a format or shell reference (`%s`, `%(name)s`, `{}`, `{name}`, `$VAR`, `${VAR}`) is a reference, not a secret (the colon form already exempts `$`).
* An unquoted value in a query string ends at `&`, `#`, a quote or a space; what follows stays. A real literal value in a URL (`?api_key=abc123def456&x=1`) is still masked, and only the value is replaced.
* Decide the line in two steps (is the `=` inside a string that is still open? then what is the value?), not with one longer pattern.

### Tests (sketch)

The lines above unchanged or masked as stated; `api_key="sk-…"`, `api_key = abc123`, `?api_key=abc123def456&x=1&y=2` (value replaced, tail kept) still masked; a faithful `/edit` that changes `json` → `xml` leaves the rest of the line byte-identical. A reviewer's 41 tests are in `contest-bench/r7/` (not shown to the entries).

---

## Part B — `sk-` followed by a lower-case slug is not a key

### What happens today

`_SECRET_PREFIX_RE` masks `sk-` plus 12 or more slug characters **anywhere**. `<div class="sk-fading-circle">`, scikit-learn's `sk-toggleable__label` and `b"sk-ecdsa-sha2-nistp256@openssh.com"` become `here_your_key`; an edit of another attribute of the line writes `class="here_your_key"` to disk. A reviewer counted about 40 of the 45 lines the mask changed across ~40,000 library files as this kind.

### What it does

An `sk-` token is a key when its tail has a digit or an upper-case letter; a hyphenated or underscored run of lower-case letters alone is a slug. `sk-live-AAAA…`, `sk-or-v1-…`, `sk-proj-…` and the HARDEN-12 C table stay masked.

### Tests (sketch)

The three lines above unchanged; the provider keys of the HARDEN-12 C table masked.

---

## Part C — the executor's command log

### What happens today

`_redact_command` leaves `redis-cli -a hunter2secret ping` and `aws configure set aws_secret_access_key AbCdEf123456` as written and turns `mysql -p mydb` (where `mydb` is the database; a MySQL password is attached, `-psecret`) into `mysql -p [REDACTED]`. Logs only.

### What it does

`redis-cli -a <pw>` and the value of `aws configure set <name containing secret|key|token> <value>` are redacted; a `-p` followed by a space for `mysql` and `mysqldump` is the database, not a password (the attached form stays redacted).

Found again by an outside review of `264440c` (the `redis-cli -a` case; already here). Two more spellings of the same hole, same function, checked on `0815666`: `docker login -u me --password hunter2` is logged as written (only `-p` is looked at for `docker login`), and `redis-cli --pass pw` and `redis-cli -u redis://:pw@host` are logged as written.

* `docker login --password <pw>` (and `--password=<pw>`; `--password-stdin` takes no value and stays) is redacted; `redis-cli --pass <pw>` and the password of a `redis://:pw@host` / `rediss://user:pw@host` URL given to `-u` are redacted. A command with none of these is logged as it was.

### Tests (sketch)

The three commands and the three spellings above; the HARDEN-12 D table unchanged.

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. The judge runs the mask over this repository's own sources and a corpus of installed library files before and after and expects no line of code to change.

## Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** `/edit` on a file with `requests.get("…?api_key=" + KEY + "&format=json")` that only changes `json` to `xml` leaves every other byte alone; an HTML file with `class="sk-fading-circle"` comes back unchanged.

## Not in scope

Rust, an unlabelled secret in prose, the restore (HARDEN-13, ticket 311) and the verdict parser (HARDEN-14, ticket 312).

Why not the others: in a black box that drives each tree (79 checks over A to C through the real `mask_api_key`, `restore_masked_secrets` and `_redact_command`, written after the entries), sensenova-var3, sensenova-var1 and laguna passed 79, var4 77, var2 76, step-5-preview 75, agnes 73, nemotron 60 (it leaves `sk-live-…` unmasked and has the `body` bug it names itself), glm 52 (ERROR, `provider_unavailable`, never delivered). var3 is the base because it is the only one of the three that keeps `mysql -p mydb` by a rule that does not guess from the length (a spaced `-p` word of lower-case letters is the database), masks a lower-case `sk-live-…1` and puts the `redis-cli` rules in the per-program segment. The mask over 28,000 files of this repository and the installed libraries changed 2,359 lines before and 2,282 after, none new, 77 fewer (the `sk-` classes and the URL concatenations).

Found at judging and fixed in the same commit: var3 stopped an unquoted value at `=` as well, so a padded base64 key kept its `==` (`api_key=here_your_key==`); the stop set is `& #` and whitespace and quotes only. Tests of the judge's cases: `tests/test_harden15_judge_cases_313.py`. Other entries' own tests ran on this tree: 382 passed; the 3 that failed pin a wrong expectation (agnes expects a literal value in a URL to stay, var4 expects `mysql -psecretpass1234` to be logged as written), 3 files import private names of their own tree.
