| entrant | total | A transport | B exhausted | C nothink-rung | D nothink-ign | E stream_opts | F wording/ctr | G/H cfg+cap | I meta API | J mine | K/L r2-3 | K4 bonus | S static | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ds41-v3 | 210/211 | 56/56 | 22/22 | 13/13 | 10/10 | 9/9 | 13/13 | 5/5 | 18/18 | 5/5 | 51/52 | 0/1 | 8/8 |  |
| sn68-v1 | 210/211 | 56/56 | 22/22 | 13/13 | 10/10 | 9/9 | 13/13 | 5/5 | 18/18 | 5/5 | 51/52 | 0/1 | 8/8 |  |
| sn68-v2 | 210/211 | 56/56 | 22/22 | 13/13 | 10/10 | 9/9 | 13/13 | 5/5 | 18/18 | 5/5 | 51/52 | 0/1 | 8/8 |  |
| sn68-v3 | 210/211 | 56/56 | 22/22 | 13/13 | 10/10 | 9/9 | 13/13 | 5/5 | 18/18 | 5/5 | 51/52 | 1/1 | 8/8 |  |
| ds41-v1 | 209/211 | 54/56 | 22/22 | 13/13 | 10/10 | 9/9 | 13/13 | 5/5 | 18/18 | 5/5 | 52/52 | 0/1 | 8/8 |  |
| ds41-v2 | 209/211 | 54/56 | 22/22 | 13/13 | 10/10 | 9/9 | 13/13 | 5/5 | 18/18 | 5/5 | 52/52 | 0/1 | 8/8 |  |
| aria | 208/211 | 55/56 | 22/22 | 13/13 | 10/10 | 9/9 | 13/13 | 5/5 | 18/18 | 5/5 | 51/52 | 0/1 | 7/8 |  |
| sn67-v2 | 208/211 | 53/56 | 22/22 | 13/13 | 10/10 | 9/9 | 13/13 | 5/5 | 18/18 | 5/5 | 52/52 | 0/1 | 8/8 |  |
| laguna | 207/211 | 53/56 | 22/22 | 12/13 | 10/10 | 9/9 | 13/13 | 5/5 | 18/18 | 5/5 | 52/52 | 0/1 | 8/8 |  |
| sonet5-v2 | 207/211 | 54/56 | 22/22 | 12/13 | 10/10 | 9/9 | 12/13 | 5/5 | 18/18 | 5/5 | 52/52 | 0/1 | 8/8 |  |
| sonet5-v3 | 202/211 | 49/56 | 21/22 | 13/13 | 10/10 | 9/9 | 13/13 | 5/5 | 18/18 | 5/5 | 51/52 | 0/1 | 8/8 |  |
| hy3 | 199/211 | 51/56 | 21/22 | 13/13 | 10/10 | 9/9 | 13/13 | 5/5 | 16/18 | 5/5 | 48/52 | 0/1 | 8/8 | K8_404_during_transport_retry |
| sonet46-v1 | 196/211 | 52/56 | 21/22 | 12/13 | 10/10 | 9/9 | 13/13 | 5/5 | 18/18 | 5/5 | 46/52 | 0/1 | 5/8 |  |
| stepfun | 193/211 | 48/56 | 22/22 | 13/13 | 10/10 | 7/9 | 13/13 | 5/5 | 18/18 | 5/5 | 45/52 | 0/1 | 7/8 |  |
| ling3 | 176/211 | 48/56 | 18/22 | 5/13 | 8/10 | 9/9 | 12/13 | 4/5 | 15/18 | 4/5 | 45/52 | 0/1 | 8/8 |  |
| agness | 155/211 | 40/56 | 15/22 | 2/13 | 6/10 | 7/9 | 12/13 | 5/5 | 14/18 | 5/5 | 44/52 | 0/1 | 5/8 |  |
| glm47 | 155/211 | 45/56 | 16/22 | 1/13 | 6/10 | 7/9 | 12/13 | 4/5 | 11/18 | 5/5 | 40/52 | 0/1 | 8/8 |  |
| glm47-flash | 154/211 | 45/56 | 16/22 | 1/13 | 6/10 | 7/9 | 12/13 | 4/5 | 11/18 | 5/5 | 40/52 | 0/1 | 7/8 |  |
| base | 73/211 | 15/56 | 10/22 | 1/13 | 3/10 | 7/9 | 6/13 | 1/5 | 4/18 | 3/5 | 19/52 | 0/1 | 4/8 |  |

### ds41-v3 (210/211) — 2 missed
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=stop completion_tokens=12 reasoning_chars=0 elapsed=0.0s']`
- `K8.soft: outer llm_call_retry gives the candidate one more chance → REJECTED` — got: `unknown-kept calls=2`

### sn68-v1 (210/211) — 2 missed
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=stop completion_tokens=12 reasoning_chars=0 content_chunks=0 elapsed=0.0s']`
- `K8.soft: outer llm_call_retry gives the candidate one more chance → REJECTED` — got: `unknown-kept calls=2`

### sn68-v2 (210/211) — 2 missed
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=stop completion_tokens=12 reasoning_chars=0 content_chunks=0 elapsed=0.0s']`
- `K8.soft: outer llm_call_retry gives the candidate one more chance → REJECTED` — got: `unknown-kept calls=2`

### sn68-v3 (210/211) — 1 missed
- `K8.soft: outer llm_call_retry gives the candidate one more chance → REJECTED` — got: `unknown-kept calls=2`

### ds41-v1 (209/211) — 3 missed
- `A1.split line has new counters` — got: `existence=0 presence_confirmed=0 presence_rejected=1 presence_fail_closed=0 presence_unknown=0 presence_reask=0 duplicate=0 non_py=0`
- `A1.split shows presence_empty_transport=1` — got: `existence=0 presence_confirmed=0 presence_rejected=1 presence_fail_closed=0 presence_unknown=0 presence_reask=0 duplicate=0 non_py=0`
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=stop completion_tokens=12 reasoning_chars=0 elapsed=0.0s']`

### ds41-v2 (209/211) — 3 missed
- `A1.split line has new counters` — got: `existence=0 presence_confirmed=0 presence_rejected=1 presence_fail_closed=0 presence_unknown=0 presence_reask=0 duplicate=0 non_py=0`
- `A1.split shows presence_empty_transport=1` — got: `existence=0 presence_confirmed=0 presence_rejected=1 presence_fail_closed=0 presence_unknown=0 presence_reask=0 duplicate=0 non_py=0`
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=stop completion_tokens=12 reasoning_chars=0 elapsed=0.0s']`

### aria (208/211) — 4 missed
- `S.ships tests (new file or new test functions)` — got: `new_files=0 new_test_fns=0`
- `A1.exactly one classification line per empty reply` — got: `1`
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=stop completion_tokens=12 reasoning_chars=0 elapsed=0.0s']`
- `K5.ends UNKNOWN 'garbled'` — got: `("Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 5 re-ask(s", 'Gate1._check_presence [candidate 0]: verdict still unp`

### sn67-v2 (208/211) — 4 missed
- `A1.exactly one classification line per empty reply` — got: `1`
- `A1.split line has new counters` — got: `existence=0 presence_confirmed=0 presence_rejected=1 presence_fail_closed=0 presence_unknown=0 presence_reask=0 duplicate=0 non_py=0`
- `A1.split shows presence_empty_transport=1` — got: `existence=0 presence_confirmed=0 presence_rejected=1 presence_fail_closed=0 presence_unknown=0 presence_reask=0 duplicate=0 non_py=0`
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=stop completion_tokens=12 reasoning_chars=0 elapsed=0.0s']`

### laguna (207/211) — 5 missed
- `A1.exactly one classification line per empty reply` — got: `1`
- `A4.generic 'JSON decode failed (Expecting value' replaced in UNKNOWN line` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): empty (transport): JSON decode failed (Expecting value: l`
- `A6.UNKNOWN or 'ending unknown' line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `C1.logged as think=off re-ask` — got: `[]`
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `["Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason='stop' completion_tokens=12 reasoning_chars=0 content_chunks=0 elapsed=0.0s"]`

### sonet5-v2 (207/211) — 5 missed
- `A1.exactly one classification line per empty reply` — got: `1`
- `A9.sleeps llm_call_retry_wait_sec (7s) between the 2 transport retries` — got: `[]`
- `C1.logged as think=off re-ask` — got: `[]`
- `F1.UNKNOWN or 'ending unknown' line says 'garbled'` — got: `("Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 6 re-ask(s): JSON decode faile", 'Gate1._check_presence [candidate 0`
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=stop completion_tokens=12 reasoning_chars=0 elapsed=0.0s.']`

### sonet5-v3 (202/211) — 10 missed
- `A1.exactly one classification line per empty reply` — got: `1`
- `A1.split line has new counters` — got: `existence=0 presence_confirmed=0 presence_rejected=1 presence_fail_closed=0 presence_unknown=0 presence_reask=0 duplicate=0 non_py=0`
- `A1.counters dict has new keys` — got: `['_uncounted', 'duplicate', 'existence', 'non_py', 'presence_confirmed', 'presence_fail_closed', 'presence_reask', 'presence_rejected', 'presence_unknown']`
- `A1.split shows presence_empty_transport=1` — got: `existence=0 presence_confirmed=0 presence_rejected=1 presence_fail_closed=0 presence_unknown=0 presence_reask=0 duplicate=0 non_py=0`
- `A4.UNKNOWN line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A4.generic 'JSON decode failed (Expecting value' replaced in UNKNOWN line` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A5.UNKNOWN line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `B2.UNKNOWN line says 'empty (exhausted)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `["Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason='stop' completion_tokens=12 reasoning_chars=0 elapsed=0.0s."]`
- `K8.soft: outer llm_call_retry gives the candidate one more chance → REJECTED` — got: `unknown-kept calls=2`

### hy3 (199/211) — 13 missed
- `A1.exactly one classification line per empty reply` — got: `1`
- `A4.'ending unknown' WARNING says 'empty (transport)'` — got: `Gate1._check_presence [candidate 0]: verdict still unparseable after 1 re-ask(s) (mode=fast) — no verdict, ending unknown. raw=''`
- `A4.generic 'JSON decode failed (Expecting value' replaced in UNKNOWN line` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): empty (transport): JSON decode failed (Expecting value: l`
- `A4.presence_empty_exhausted==0` — got: `1`
- `A9.sleeps llm_call_retry_wait_sec (7s) between the 2 transport retries` — got: `[7.0]`
- `B2.'ending unknown' WARNING says 'empty (exhausted)'` — got: `Gate1._check_presence [candidate 0]: verdict still unparseable after 1 re-ask(s) (mode=fast) — no verdict, ending unknown. raw=''`
- `I2.meta: role-only finish_reason=='stop'` — got: `None`
- `I2.meta: reasoning-only finish_reason=='length'` — got: `None`
- `I4.meta: finish_reason on content chunk survives a trailing usage chunk (last non-null)` — got: `None`
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=None completion_tokens=12 reasoning_chars=0 elapsed=0.0s']`
- `K8.404 during a transport retry: filter() does not raise` — got: `ERROR`
- `K8.404 is never turned into a REJECTED verdict (RUN-5 shape kept)` — got: `ERROR`
- `K8.soft: outer llm_call_retry gives the candidate one more chance → REJECTED` — got: `ERROR`

### sonet46-v1 (196/211) — 16 missed
- `S.smoke mirror in sync (sync_test_tiers --check)` — got: `
1 problem(s):
  - .smoke_tests/test_gate1_presence_empty_classification.py: missing
`
- `S.presence_empty_retries documented in agents.ini [gate1]` — got: `missing`
- `S.presence_empty_retries default 2 in agents.ini` — got: `[]`
- `A1.exactly one classification line per empty reply` — got: `1`
- `A4.UNKNOWN line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A4.generic 'JSON decode failed (Expecting value' replaced in UNKNOWN line` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A5.UNKNOWN line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `B2.UNKNOWN line says 'empty (exhausted)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `C1.logged as think=off re-ask` — got: `[]`
- `I4.meta: usage inline on the finish chunk is read` — got: `None`
- `K2.usage inline on finish chunk: completion_tokens read → exhausted` — got: `['Gate1._check_presence [candidate 0]: initial reply is empty — kind=transport finish_reason=stop completion_tokens=None reasoning_chars=0 elapsed=0.0s']`
- `K2.classification line shows completion_tokens=4096` — got: `['Gate1._check_presence [candidate 0]: initial reply is empty — kind=transport finish_reason=stop completion_tokens=None reasoning_chars=0 elapsed=0.0s']`
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `['Gate1._check_presence [candidate 0]: initial reply is empty — kind=transport finish_reason=stop completion_tokens=12 reasoning_chars=0 elapsed=0.0s']`
- `K5.ends UNKNOWN 'garbled'` — got: `("Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 5 re-ask(s", 'Gate1._check_presence [candidate 0]: verdict still unp`
- `K8.404 is never turned into a REJECTED verdict (RUN-5 shape kept)` — got: `rejected calls=2`
- `K8.soft: outer llm_call_retry gives the candidate one more chance → REJECTED` — got: `rejected calls=2`

### stepfun (193/211) — 19 missed
- `S.ships tests (new file or new test functions)` — got: `new_files=0 new_test_fns=0`
- `A1.exactly one classification line per empty reply` — got: `1`
- `A1.split line has new counters` — got: `existence=0 presence_confirmed=0 presence_rejected=1 presence_fail_closed=0 presence_unknown=0 presence_reask=0 presence_empty_transport=1 presence_empty_exhaus`
- `A1.counters dict has new keys` — got: `['_uncounted', 'duplicate', 'existence', 'non_py', 'presence_confirmed', 'presence_empty_exhausted', 'presence_empty_transport', 'presence_fail_closed', 'presen`
- `A4.UNKNOWN line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): empty (exhausted): JSON decode failed (Expecting value: l`
- `A4.'ending unknown' WARNING says 'empty (transport)'` — got: `Gate1._check_presence [candidate 0]: empty (exhausted) — verdict still unparseable after 1 re-ask(s) (mode=fast) — no verdict, ending unknown. raw=''`
- `A4.generic 'JSON decode failed (Expecting value' replaced in UNKNOWN line` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): empty (exhausted): JSON decode failed (Expecting value: l`
- `A5.UNKNOWN line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): empty (exhausted): JSON decode failed (Expecting value: l`
- `A6.UNKNOWN or 'ending unknown' line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): empty (exhausted): JSON decode failed (Expecting value: l`
- `E1.stream_options.include_usage=true in payload` — got: `None`
- `E2.first request carried stream_options` — got: `None`
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=stop completion_tokens=12 reasoning_chars=0 elapsed=0.0s']`
- `K5.ends UNKNOWN 'garbled'` — got: `("Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 5 re-ask(s", 'Gate1._check_presence [candidate 0]: empty (transport)`
- `E3.OpenAI wording 'Unknown parameter: stream_options' → stripped` — got: `[None, None]`
- `E3.candidate 1 never sends it` — got: `[None]`
- `E4.unrelated 400: stream_options NOT stripped for later calls` — got: `[None]`
- `E4.unrelated 400: retry still carried stream_options` — got: `[None, None]`
- `H2.strict+cap: cap line exists (legit cap use)` — got: `0`
- `H2.strict+cap: every cap line is followed by a re-ask at max_tokens=8192` — got: `[]`

### ling3 (176/211) — 36 missed
- `A.presence_empty_transport==1` — got: `0`
- `A1.exactly one classification line per empty reply` — got: `1`
- `A1.split shows presence_empty_transport=1` — got: `existence=0 presence_confirmed=0 presence_rejected=1 presence_fail_closed=0 presence_unknown=0 presence_reask=0 presence_empty_transport=0 presence_empty_exhaus`
- `A2.presence_empty_transport==1` — got: `0`
- `A3.presence_empty_transport==1` — got: `0`
- `A4.UNKNOWN line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A4.generic 'JSON decode failed (Expecting value' replaced in UNKNOWN line` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A5.UNKNOWN line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `J2.presence_empty_transport==1` — got: `0`
- `B1.presence_empty_exhausted==1` — got: `0`
- `B2.UNKNOWN line says 'empty (exhausted)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `B2.presence_empty_exhausted==1` — got: `0`
- `B3.presence_empty_exhausted==1` — got: `0`
- `C1.think=true: a think=off re-ask was made` — got: `off=[] on=[0, 1, 2] n=3`
- `C1.think=off call is the LAST call` — got: `last reasoning=None thinking={'type': 'enabled'}`
- `C1.exactly one think=off call` — got: `[]`
- `C1.think=off payload: no think/reasoning_effort, reasoning.exclude=true` — got: `['max_tokens', 'messages', 'model', 'stream', 'stream_options', 'temperature', 'thinking']`
- `C1.think=off payload: thinking.type=disabled (AUTO-ZAITHINK-1 parity)` — got: `{'type': 'enabled'}`
- `C1.verdict from think=off call used → REJECTED` — got: `{'candidate 0': 'unknown-kept'}`
- `C1.presence_reask==1 (counts as a re-ask)` — got: `0`
- `C2.think=true exhausted forever: exactly one think=off call` — got: `off=[] n=3`
- `D1.presence_empty_exhausted==5` — got: `0`
- `D2.workers=4: presence_empty_exhausted==8` — got: `0`
- `F4.workers=4: presence_empty_transport==12` — got: `0`
- `H1.no cap line followed by 'already tried' quoting another max_tokens` — got: `[('[candidate 0]: re-ask max_tokens=8192 capped at unparseable_max_tokens_cap=8192.', 'ate 0]: fast mode — skipping re-ask 5/6 (max_tokens=4096, temperature=0.0`
- `I2.meta: role-only finish_reason=='stop'` — got: `None`
- `I2.meta: reasoning-only finish_reason=='length'` — got: `None`
- `I4.meta: finish_reason on content chunk survives a trailing usage chunk (last non-null)` — got: `None`
- `I3.meta: ollama eval_count→completion_tokens` — got: `None`
- `K1.ollama presence_empty_transport==1` — got: `0`
- `K1b.ollama presence_empty_exhausted==1` — got: `0`
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=None completion_tokens=12 reasoning_chars=0 elapsed=0.0s']`
- `K5.presence_empty_transport==1` — got: `0`
- `C3a.profile think=true (gate1 think=false): think=off rung fires` — got: `off=[] n=3`
- `C3a.REJECTED from the think=off call` — got: `{'candidate 0': 'unknown-kept'}`
- `L1.learned budget (8192) reused by every transport retry` — got: `[8192, 4096, 4096]`

### agness (155/211) — 57 missed
- `S.ships tests (new file or new test functions)` — got: `new_files=0 new_test_fns=0`
- `S.presence_empty_retries documented in agents.ini [gate1]` — got: `missing`
- `S.presence_empty_retries default 2 in agents.ini` — got: `[]`
- `A1.split line has new counters` — got: `existence=0 presence_confirmed=0 presence_rejected=1 presence_fail_closed=0 presence_unknown=0 presence_reask=0 duplicate=0 non_py=0`
- `A1.counters dict has new keys` — got: `['_uncounted', 'duplicate', 'existence', 'non_py', 'presence_confirmed', 'presence_fail_closed', 'presence_reask', 'presence_rejected', 'presence_unknown']`
- `A1.split shows presence_empty_transport=1` — got: `existence=0 presence_confirmed=0 presence_rejected=1 presence_fail_closed=0 presence_unknown=0 presence_reask=0 duplicate=0 non_py=0`
- `A3.verdict REJECTED` — got: `{'candidate 0': 'unknown-kept'}`
- `A3.3 calls` — got: `calls=2`
- `A3.same (max_tokens,temperature)` — got: `[(4096, 0.0), (4096, 0.1)]`
- `A3.no nudge in 2nd/3rd` — got: `[False, True]`
- `A3.retry request byte-identical (messages) to the first` — got: `n/a`
- `A3.presence_empty_transport==1` — got: `0`
- `A3.presence_unknown==0` — got: `1`
- `A4.UNKNOWN line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A4.'ending unknown' WARNING says 'empty (transport)'` — got: `Gate1._check_presence [candidate 0]: verdict still unparseable after 1 re-ask(s) (mode=fast) — no verdict, ending unknown empty (exhausted). raw=''`
- `A4.generic 'JSON decode failed (Expecting value' replaced in UNKNOWN line` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A4.presence_empty_exhausted==0` — got: `1`
- `A5.UNKNOWN line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A9.sleeps llm_call_retry_wait_sec (7s) between the 2 transport retries` — got: `[]`
- `B1.classification kind=exhausted` — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=length completion_tokens=4096 reasoning_chars=0 elapsed=0.0s']`
- `B2.UNKNOWN line says 'empty (exhausted)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `B3.finish=stop but completion_tokens=0.95*max → exhausted` — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=stop completion_tokens=3891 reasoning_chars=0 elapsed=0.0s']`
- `B4.reasoning_content + finish=stop → exhausted` — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=stop completion_tokens=12 reasoning_chars=230 elapsed=0.0s']`
- `B4.nothink-ignored WARNING emitted` — got: `[]`
- `B4.presence_nothink_ignored>=1` — got: `0`
- `B5.classified exhausted` — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=length completion_tokens=4096 reasoning_chars=0 elapsed=0.0s']`
- `C1.think=true: a think=off re-ask was made` — got: `off=[] on=[0, 1] n=2`
- `C1.think=off call is the LAST call` — got: `last reasoning=None thinking={'type': 'enabled'}`
- `C1.exactly one think=off call` — got: `[]`
- `C1.think=off payload: no think/reasoning_effort, reasoning.exclude=true` — got: `['max_tokens', 'messages', 'model', 'stream', 'stream_options', 'temperature', 'thinking']`
- `C1.think=off payload: thinking.type=disabled (AUTO-ZAITHINK-1 parity)` — got: `{'type': 'enabled'}`
- `C1.think=off at pinned budget + initial temperature` — got: `(4096, 0.1)`
- `C1.verdict from think=off call used → REJECTED` — got: `{'candidate 0': 'unknown-kept'}`
- `C1.logged as think=off re-ask` — got: `[]`
- `C1.presence_reask==1 (counts as a re-ask)` — got: `0`
- `C2.think=true exhausted forever: exactly one think=off call` — got: `off=[] n=2`
- `C2.3 calls total (initial + pinned re-ask + think=off)` — got: `[(4096, 0.0, False), (4096, 0.1, False)]`
- `D1.WARNING emitted exactly once for 5 candidates` — got: `n=0 []`
- `D1.WARNING names url/model` — got: `[]`
- `D1.presence_nothink_ignored counted (>=1)` — got: `0`
- `D2.workers=4: nothink WARNING still exactly once` — got: `n=0`
- `E2.after HTTP 400 the retry omits stream_options` — got: `[True, True]`
- `E2.candidate 1 (same url/model) never sends it again` — got: `[True]`
- `F1.UNKNOWN or 'ending unknown' line says 'garbled'` — got: `("Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 6 re-ask(s): JSON decode faile", 'Gate1._check_presence [candidate 0`
- `I1.plain request_completion: normal text unchanged` — got: `('{"verdict": "rejected", "reason": "already handled"}', CompletionMeta(finish_reason='stop', completion_tokens=24, reasoning_chars=0, content_chunks=4, elapsed`
- `I1.plain: role-only+[DONE] → ''` — got: `('', CompletionMeta(finish_reason='stop', completion_tokens=0, reasoning_chars=0, content_chunks=0, elapsed=6.80780003676773e-05))`
- `I1.plain: reasoning-only → ''` — got: `('', CompletionMeta(finish_reason='length', completion_tokens=4096, reasoning_chars=230, content_chunks=0, elapsed=0.00011594399984460324))`
- `I1.plain: ollama text unchanged` — got: `('hello', CompletionMeta(finish_reason='length', completion_tokens=77, reasoning_chars=0, content_chunks=0, elapsed=7.554199964943109e-05))`
- `K1b.ollama done_reason=length/eval_count=max → exhausted (2 calls)` — got: `calls=2 ['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=length completion_tokens=0 reasoning_chars=0 elapsed=0.0s']`
- `K2.usage inline on finish chunk: completion_tokens read → exhausted` — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=stop completion_tokens=4096 reasoning_chars=0 elapsed=0.0s']`
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=stop completion_tokens=12 reasoning_chars=0 elapsed=0.0s']`
- `K5.ends UNKNOWN 'garbled'` — got: `("Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 5 re-ask(s", 'Gate1._check_presence [candidate 0]: verdict still unp`
- `E3.OpenAI wording 'Unknown parameter: stream_options' → stripped` — got: `[True, True]`
- `E3.candidate 1 never sends it` — got: `[True]`
- `C3a.profile think=true (gate1 think=false): think=off rung fires` — got: `off=[] n=2`
- `C3a.REJECTED from the think=off call` — got: `{'candidate 0': 'unknown-kept'}`
- `I6.on_token still receives every content piece` — got: `(4, ('{"verdict": "rejected", "reason": "already handled"}', CompletionMeta(finish_reason='stop', completion_tokens=24, reasoning_chars=0, content_chunks=4, ela`

### glm47 (155/211) — 57 missed
- `A1.exactly one classification line per empty reply` — got: `1`
- `A1.split line has new counters` — got: `existence=0 presence_confirmed=0 presence_rejected=1 presence_fail_closed=0 presence_unknown=0 presence_reask=0 duplicate=0 non_py=0`
- `A1.counters dict has new keys` — got: `['_uncounted', 'duplicate', 'existence', 'non_py', 'presence_confirmed', 'presence_fail_closed', 'presence_reask', 'presence_rejected', 'presence_unknown']`
- `A1.split shows presence_empty_transport=1` — got: `existence=0 presence_confirmed=0 presence_rejected=1 presence_fail_closed=0 presence_unknown=0 presence_reask=0 duplicate=0 non_py=0`
- `A4.UNKNOWN line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A4.'ending unknown' WARNING says 'empty (transport)'` — got: `Gate1._check_presence [candidate 0]: verdict still unparseable after 1 re-ask(s) (mode=fast) — no verdict, ending unknown. raw=''`
- `A4.generic 'JSON decode failed (Expecting value' replaced in UNKNOWN line` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A4.presence_empty_exhausted==0` — got: `1`
- `A5.UNKNOWN line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A6.UNKNOWN or 'ending unknown' line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A9.sleeps llm_call_retry_wait_sec (7s) between the 2 transport retries` — got: `[]`
- `B2.UNKNOWN line says 'empty (exhausted)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `B2.'ending unknown' WARNING says 'empty (exhausted)'` — got: ``
- `B4.reasoning_content + finish=stop → exhausted` — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=None completion_tokens=12 reasoning_chars=0 elapsed=0.0s']`
- `B4.nothink-ignored WARNING emitted` — got: `[]`
- `B4.presence_nothink_ignored>=1` — got: `0`
- `B5.classified exhausted` — got: `[]`
- `C1.think=true: a think=off re-ask was made` — got: `off=[] on=[0, 1] n=2`
- `C1.think=off call is the LAST call` — got: `last reasoning=None thinking={'type': 'enabled'}`
- `C1.exactly one think=off call` — got: `[]`
- `C1.think=off payload: no think/reasoning_effort, reasoning.exclude=true` — got: `['max_tokens', 'messages', 'model', 'temperature', 'thinking']`
- `C1.think=off payload: thinking.type=disabled (AUTO-ZAITHINK-1 parity)` — got: `{'type': 'enabled'}`
- `C1.think=off at pinned budget + initial temperature` — got: `(4096, 0.1)`
- `C1.verdict from think=off call used → REJECTED` — got: `{'candidate 0': 'unknown-kept'}`
- `C1.logged as think=off re-ask` — got: `[]`
- `C1.presence_reask==1 (counts as a re-ask)` — got: `0`
- `C2.think=true exhausted forever: exactly one think=off call` — got: `off=[] n=2`
- `C2.3 calls total (initial + pinned re-ask + think=off)` — got: `[(4096, 0.0, False), (4096, 0.1, False)]`
- `C2.ends UNKNOWN 'empty (exhausted)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `D1.WARNING emitted exactly once for 5 candidates` — got: `n=0 []`
- `D1.WARNING names url/model` — got: `[]`
- `D1.presence_nothink_ignored counted (>=1)` — got: `0`
- `D2.workers=4: nothink WARNING still exactly once` — got: `n=0`
- `E2.after HTTP 400 the retry omits stream_options` — got: `[True, True]`
- `E2.candidate 1 (same url/model) never sends it again` — got: `[True]`
- `F1.UNKNOWN or 'ending unknown' line says 'garbled'` — got: `("Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 6 re-ask(s): JSON decode faile", 'Gate1._check_presence [candidate 0`
- `H1.ends UNKNOWN exhausted` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `I1.plain request_completion: normal text unchanged` — got: `('{"verdict": "rejected", "reason": "already handled"}', CompletionMeta(finish_reason=None, completion_tokens=24, reasoning_chars=0, content_chunks=4, elapsed=0`
- `I1.plain: role-only+[DONE] → ''` — got: `('', CompletionMeta(finish_reason=None, completion_tokens=0, reasoning_chars=0, content_chunks=0, elapsed=0.00010395050048828125, raw=''))`
- `I1.plain: reasoning-only → ''` — got: `('', CompletionMeta(finish_reason=None, completion_tokens=4096, reasoning_chars=0, content_chunks=0, elapsed=0.00016927719116210938, raw=''))`
- `I1.plain: ollama text unchanged` — got: `('hello', CompletionMeta(finish_reason='length', completion_tokens=77, reasoning_chars=0, content_chunks=5, elapsed=0.0001049041748046875, raw=''))`
- `I2.meta: role-only finish_reason=='stop'` — got: `None`
- `I2.meta: reasoning-only → text '' and reasoning_chars>0` — got: `0`
- `I2.meta: reasoning-only finish_reason=='length'` — got: `None`
- `I4.meta: finish_reason on content chunk survives a trailing usage chunk (last non-null)` — got: `None`
- `K1b.ollama done_reason=length/eval_count=max → exhausted (2 calls)` — got: `calls=62 ['Gate1._check_presence [candidate 0]: empty reply — kind=exhausted finish_reason=length completion_tokens=0 reasoning_chars=0 elapsed=0.0s']`
- `K1b.ollama REJECTED via pinned re-ask` — got: `{'candidate 0': 'unknown-kept'}`
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=None completion_tokens=12 reasoning_chars=0 elapsed=0.0s']`
- `K5.ends UNKNOWN 'garbled'` — got: `("Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 5 re-ask(s", 'Gate1._check_presence [candidate 0]: verdict still unp`
- `E3.OpenAI wording 'Unknown parameter: stream_options' → stripped` — got: `[True, True]`
- `E3.candidate 1 never sends it` — got: `[True]`
- `H2.strict+cap: cap line exists (legit cap use)` — got: `0`
- `H2.strict+cap: every cap line is followed by a re-ask at max_tokens=8192` — got: `[]`
- `C3a.profile think=true (gate1 think=false): think=off rung fires` — got: `off=[] n=2`
- `C3a.REJECTED from the think=off call` — got: `{'candidate 0': 'unknown-kept'}`
- `C3b.ends UNKNOWN exhausted` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode faile`
- `I6.on_token still receives every content piece` — got: `(4, ('{"verdict": "rejected", "reason": "already handled"}', CompletionMeta(finish_reason=None, completion_tokens=24, reasoning_chars=0, content_chunks=4, elaps`

### glm47-flash (154/211) — 58 missed
- `S.one commit` — got: `3`
- `A1.exactly one classification line per empty reply` — got: `1`
- `A1.split line has new counters` — got: `existence=0 presence_confirmed=0 presence_rejected=1 presence_fail_closed=0 presence_unknown=0 presence_reask=0 duplicate=0 non_py=0`
- `A1.counters dict has new keys` — got: `['_uncounted', 'duplicate', 'existence', 'non_py', 'presence_confirmed', 'presence_fail_closed', 'presence_reask', 'presence_rejected', 'presence_unknown']`
- `A1.split shows presence_empty_transport=1` — got: `existence=0 presence_confirmed=0 presence_rejected=1 presence_fail_closed=0 presence_unknown=0 presence_reask=0 duplicate=0 non_py=0`
- `A4.UNKNOWN line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A4.'ending unknown' WARNING says 'empty (transport)'` — got: `Gate1._check_presence [candidate 0]: verdict still unparseable after 1 re-ask(s) (mode=fast) — no verdict, ending unknown. raw=''`
- `A4.generic 'JSON decode failed (Expecting value' replaced in UNKNOWN line` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A4.presence_empty_exhausted==0` — got: `1`
- `A5.UNKNOWN line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A6.UNKNOWN or 'ending unknown' line says 'empty (transport)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `A9.sleeps llm_call_retry_wait_sec (7s) between the 2 transport retries` — got: `[]`
- `B2.UNKNOWN line says 'empty (exhausted)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `B2.'ending unknown' WARNING says 'empty (exhausted)'` — got: ``
- `B4.reasoning_content + finish=stop → exhausted` — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=None completion_tokens=12 reasoning_chars=0 elapsed=0.0s']`
- `B4.nothink-ignored WARNING emitted` — got: `[]`
- `B4.presence_nothink_ignored>=1` — got: `0`
- `B5.classified exhausted` — got: `[]`
- `C1.think=true: a think=off re-ask was made` — got: `off=[] on=[0, 1] n=2`
- `C1.think=off call is the LAST call` — got: `last reasoning=None thinking={'type': 'enabled'}`
- `C1.exactly one think=off call` — got: `[]`
- `C1.think=off payload: no think/reasoning_effort, reasoning.exclude=true` — got: `['max_tokens', 'messages', 'model', 'temperature', 'thinking']`
- `C1.think=off payload: thinking.type=disabled (AUTO-ZAITHINK-1 parity)` — got: `{'type': 'enabled'}`
- `C1.think=off at pinned budget + initial temperature` — got: `(4096, 0.1)`
- `C1.verdict from think=off call used → REJECTED` — got: `{'candidate 0': 'unknown-kept'}`
- `C1.logged as think=off re-ask` — got: `[]`
- `C1.presence_reask==1 (counts as a re-ask)` — got: `0`
- `C2.think=true exhausted forever: exactly one think=off call` — got: `off=[] n=2`
- `C2.3 calls total (initial + pinned re-ask + think=off)` — got: `[(4096, 0.0, False), (4096, 0.1, False)]`
- `C2.ends UNKNOWN 'empty (exhausted)'` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `D1.WARNING emitted exactly once for 5 candidates` — got: `n=0 []`
- `D1.WARNING names url/model` — got: `[]`
- `D1.presence_nothink_ignored counted (>=1)` — got: `0`
- `D2.workers=4: nothink WARNING still exactly once` — got: `n=0`
- `E2.after HTTP 400 the retry omits stream_options` — got: `[True, True]`
- `E2.candidate 1 (same url/model) never sends it again` — got: `[True]`
- `F1.UNKNOWN or 'ending unknown' line says 'garbled'` — got: `("Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 6 re-ask(s): JSON decode faile", 'Gate1._check_presence [candidate 0`
- `H1.ends UNKNOWN exhausted` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode failed (Expecting value: line 1 column 1 (cha`
- `I1.plain request_completion: normal text unchanged` — got: `('{"verdict": "rejected", "reason": "already handled"}', CompletionMeta(finish_reason=None, completion_tokens=24, reasoning_chars=0, content_chunks=4, elapsed=0`
- `I1.plain: role-only+[DONE] → ''` — got: `('', CompletionMeta(finish_reason=None, completion_tokens=0, reasoning_chars=0, content_chunks=0, elapsed=0.00011539459228515625, raw=''))`
- `I1.plain: reasoning-only → ''` — got: `('', CompletionMeta(finish_reason=None, completion_tokens=4096, reasoning_chars=0, content_chunks=0, elapsed=0.0001704692840576172, raw=''))`
- `I1.plain: ollama text unchanged` — got: `('hello', CompletionMeta(finish_reason='length', completion_tokens=77, reasoning_chars=0, content_chunks=5, elapsed=7.62939453125e-05, raw=''))`
- `I2.meta: role-only finish_reason=='stop'` — got: `None`
- `I2.meta: reasoning-only → text '' and reasoning_chars>0` — got: `0`
- `I2.meta: reasoning-only finish_reason=='length'` — got: `None`
- `I4.meta: finish_reason on content chunk survives a trailing usage chunk (last non-null)` — got: `None`
- `K1b.ollama done_reason=length/eval_count=max → exhausted (2 calls)` — got: `calls=62 ['Gate1._check_presence [candidate 0]: empty reply — kind=exhausted finish_reason=length completion_tokens=0 reasoning_chars=0 elapsed=0.0s']`
- `K1b.ollama REJECTED via pinned re-ask` — got: `{'candidate 0': 'unknown-kept'}`
- `K4.bonus: OpenRouter `reasoning` delta key counted as reasoning (→ exhausted)` (bonus, not counted) — got: `['Gate1._check_presence [candidate 0]: empty reply — kind=transport finish_reason=None completion_tokens=12 reasoning_chars=0 elapsed=0.0s']`
- `K5.ends UNKNOWN 'garbled'` — got: `("Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 5 re-ask(s", 'Gate1._check_presence [candidate 0]: verdict still unp`
- `E3.OpenAI wording 'Unknown parameter: stream_options' → stripped` — got: `[True, True]`
- `E3.candidate 1 never sends it` — got: `[True]`
- `H2.strict+cap: cap line exists (legit cap use)` — got: `0`
- `H2.strict+cap: every cap line is followed by a re-ask at max_tokens=8192` — got: `[]`
- `C3a.profile think=true (gate1 think=false): think=off rung fires` — got: `off=[] n=2`
- `C3a.REJECTED from the think=off call` — got: `{'candidate 0': 'unknown-kept'}`
- `C3b.ends UNKNOWN exhausted` — got: `Gate1[presence] UNKNOWN 'candidate 0' — presence unknown — provider gave no verdict after 1 re-ask(s): JSON decode faile`
- `I6.on_token still receives every content piece` — got: `(4, ('{"verdict": "rejected", "reason": "already handled"}', CompletionMeta(finish_reason=None, completion_tokens=24, reasoning_chars=0, content_chunks=4, elaps`

