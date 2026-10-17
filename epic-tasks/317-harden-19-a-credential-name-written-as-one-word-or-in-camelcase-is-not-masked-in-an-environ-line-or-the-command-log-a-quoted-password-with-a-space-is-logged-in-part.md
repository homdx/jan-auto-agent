# HARDEN-19 — a credential variable named `STRIPEKEY`, `GITHUBTOKEN`, `AUTHTOKEN`, `clientSecret` or `PGPASSWORD` reaches the model and the command log unmasked; `os.environ.setdefault(…)`, `process.env["K"]`, `ENV["K"]` are not looked at; a quoted password with a space is logged after its first word

**Status:** open
**Severity:** MEDIUM (A: a real secret under a very common name is sent to the model; C: a partial password is kept in the logs); LOW-MEDIUM (B: logs, `PGPASSWORD=… psql` in clear)
**File:** `tools/llm_stream.py` (A), `tools/auto/executor.py` (B, C)
**Symbol:** A: the environ-subscript mask of HARDEN-12 B and its name rule (CC review fix 40). B: `_redact_command`, `_SECRET_ENV_ASSIGN_RE`, `_name_is_not_a_credential`. C: `_PASSWORD_OPT_RE`, `_REDIS_CLI_PASSWORD_RE`, `_SECRET_OPT_RE`, `_SECRET_USER_OPT_RE`, `_looks_like_a_password`
**Round:** (set when the round starts)
**Size:** M
**Depends on:** HARDEN-15 (313), CC review fixes 40 and 41 (`a036ae6`, `f7061c4`), all landed
**Also touches:** `tests/test_mask_review_q.py`, the HARDEN-12 B and D tests (pins that must stay green), `tests/`, `tests_bugfix/`

---

## Why

An outside review of `5c403f1` found that CC review fix 40 (a name that only *contains* a credential word is not a credential: `TOKENIZERS_PARALLELISM`, `KEYBOARD_LAYOUT`) went too far: the credential word must now be a whole underscore-separated part of the name, so a name written as one word or in camelCase is no longer a credential. Two places use the rule (the mask and the command log), so one name rule serves both. The same review sent a patch for quoted passwords with a space; its short forms are Part C. A part is done when it has its tests and both suites are green unchanged.

---

## Part A — the environ mask reads a name by its underscores

### What happens today

For `os.environ["<name>"] = "<32 hex characters>"` (a value with no provider prefix), 24 of 28 credential names reach the model unmasked on `5c403f1`; `277d632` masked all 28:

```
STRIPEKEY  GITHUBTOKEN  SYSTEM_ACCESSTOKEN  AUTHTOKEN  SECRETKEY
OpenAIKey  GitHubToken  clientSecret  dbPassword   -> value sent as written
API_KEY  OPENAI_API_KEY  HF_TOKEN  SECRET_KEY        -> masked
```

Prefixed values (`ghp_…`) are still masked by the bare-token pass. Separately, only the subscript-assignment form is covered; the **same name** is not masked in `os.environ.setdefault("API_KEY", "<hex>")`, `process.env["API_KEY"] = "<hex>"`, `ENV["API_KEY"] = "<hex>"`, `$_ENV["API_KEY"] = "<hex>"` (only `process.env.API_KEY = …` is).

### What it does

* One **name rule**, in one helper, used by the mask and by the command log (Part B): split the name into words at `_`, `-`, `.` and at camelCase boundaries; it is a credential name when a word is `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `PWD`, `APIKEY`, `AUTH`+`TOKEN`, … **or** when the name *ends* in one of those words written together (`STRIPEKEY`, `GITHUBTOKEN`, `AUTHTOKEN`, `SECRETKEY`), except for a short stoplist of known false friends (`MONKEY`, `TURKEY`, `HOTKEY`, `DONKEY`, `KEYBOARD`, `TOKENIZERS_PARALLELISM`, `PRIMARY_KEY` columns, `MAX_TOKENS`, `KEYMAP`). The CC fix 40 cases stay green.
* The call form and the bracket forms take the same rule: `os.environ.setdefault("N", v)`, `os.getenv`-set forms where a value is assigned, `process.env["N"] = v`, `ENV["N"] = v`, `$_ENV["N"] = v`.
* Python, Java and Kotlin forms are the priority (`System.setProperty("api.key", "…")`, `System.getenv` is read-only and out of scope); the JS/PHP/Ruby bracket forms above are in because they share the rule.

### Tests (sketch)

The 24 names above masked; `API_KEY`, `HF_TOKEN` as today; `TOKENIZERS_PARALLELISM`, `KEYBOARD_LAYOUT`, `MONKEY_PATCH`, `MAX_TOKENS` untouched; the five access forms of one name masked alike.

---

## Part B — the command log reads a name by its underscores too

### What happens today

18 of 29 credential-named variables leak into the command log on `5c403f1` (1 on `277d632`): `PGPASSWORD=hunter2 psql …`, `NGROK_AUTHTOKEN=… ngrok`, `GITHUBTOKEN=… git push`, `DBPASSWORD=…`, `npm config set //registry.npmjs.org/:_authToken=…`. `PGPASSWORD=… psql` in an acceptance check is logged in clear.

### What it does

`_SECRET_ENV_ASSIGN_RE` and `_name_is_not_a_credential` use the **same helper** as Part A; `_authToken=` (npm) is a credential name; `MAX_TOKENS=100`, `TOKENIZER=bert`, `KEYBOARD=us`, `--tokens=100` stay as written (the HARDEN-12 D table).

### Tests (sketch)

The 18 leaking names redacted; the HARDEN-12 D table and the CC fix 40 command-log cases unchanged.

---

## Part C — a shell-quoted password with a space

### What happens today

`_PASSWORD_OPT_RE` (`-p`) and `_REDIS_CLI_PASSWORD_RE` (`-a`) take the value as `\S*`. For `'my secret'` the value is cut at the space; `'my` is under the length rule and the whole word goes out:

```
mysql -p'my secret' mydb                    -> unchanged
redis-cli -a "my secret" get key            -> unchanged
docker login -p 'my secret' registry.example -> unchanged
mysql --password='my secret' mydb           -> mysql --password=[REDACTED] secret' mydb   (the word `secret` leaks)
curl -u 'admin:my secret' https://x.example -> curl -u 'admin:[REDACTED] secret' https://x.example
```

The unquoted forms are right. The reviewer's patch (`v12-quoted-password-with-space.patch`) fixes `-p` and `-a` with `'[^'\n]*'|"[^"\n]*"|\S*`, but puts code before the docstring of `_looks_like_a_password` and leaves the long form and `-u` open.

### What it does

* One **value pattern** (a single-quoted word, a double-quoted word, or a run of non-space characters) shared by `-p`, `-a`, `--password=`/`--pass=`/`--passwd=`, `--token=`, `-u user:pass`, and the `_SECRET_OPT_RE` value; the whole token is redacted, quotes included. An unterminated quote redacts to the end of the line rather than leaving half a password.
* The database-name exception of `mysql -p mydb` is unchanged and does not apply to a quoted word.

The reviewer's second patch (`v13-cmd1-quoted-passwords-full.patch`: the long option and `-u 'user:pass'` as well as `-p` and `-a`) is applied as a separate CC commit right after round 315; this part is what stays for the round: the one shared value pattern instead of five copies, the docstring left in place, an unterminated quote, `--token=`, and `-u "a b:c d"` (a space in the user part).

### Tests (sketch)

The seven commands above redacted whole; the reviewer's 8 cases (`tests_bugfix/test_v12_quoted_password_with_space.py` in the patch); the unquoted forms and the HARDEN-15 C tables unchanged; `--password="a b" --other x` keeps `--other x`.

---

## Contest

One commit. The judge's checks are written after the round, from the entries; the entries' tests are cross-tested on every other entry's code. The judge runs the mask and the command log over the repository's sources and a corpus of installed library files, before and after, and expects no new line changed except for credential names.

## Acceptance

**Layer 1 — offline.** The tests above; both suites green unchanged.
**Layer 2 — the operator.** A task whose acceptance command is `PGPASSWORD=… psql -c …` leaves no password in the command log; a file with `os.environ["STRIPEKEY"] = "<hex>"` reaches the model masked.

## Not in scope

Rust, Windows `set NAME=…`, and the decision whether `mask_api_key` should mask every labelled secret (`password`, `client_secret`, `private_key` in a config file, `let secretKey = "…"`): see the note in the report, it is a scope decision for the operator and not part of this ticket.
