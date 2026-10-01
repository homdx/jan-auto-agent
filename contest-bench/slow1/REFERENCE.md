# SLOW-1 — the reference answer (for the judge, never for the target)

`make_target.sh` copies only the ticket into the target. This file stays here:
it names the fixes that landed on kc after BASE (`b5257cf`), so an entry is
judged against what the slowdown really was.

## The causes, and where kc fixed them

| # | cause | brought in by | kc fix |
|---|---|---|---|
| 1 | `make_config` in `tests/test_contest_runner.py` does not set `error_retry_backoff_sec`, so the silence tests sleep the roster's 15 s for real (`_wait_backoff`) — about 65 s, all on the one `port_bound_http_servers` worker | `4b5c8ee` (KC-9, the SILENT continue) | `b6464a1` — `error_retry_backoff_sec=0` in the harness; the roster default stays 15 |
| 2 | `tests/test_contest_runner.py` carries a file-wide `pytestmark = xdist_group("port_bound_http_servers")`; with `--dist=loadgroup` its ~250 tests queue on one worker behind the port suites — that queue *is* the wall clock | `e8c6ad3` (KC-6) | `3144826` — the file-wide mark removed; the bench binds OS-assigned ports only |
| 3 | `test_collect_already_safe_query.py` rebuilds the real repo index (~38 s) per test, `test_collect_gates.py` the gates map; spread over workers each pays again | — (not a regression, a standing cost) | the commit that brings this file ("SLOW-1: the collect index and gates-map tests share one session build each") — one session build per file, an `xdist_group` per file; the two files ~100 s → ~63 s |

Causes 1 and 2 are the regression the ticket asks for. Cause 3 is a bonus.

## Judging an entry

1. `python3 contest-bench/slow1/check_slow1.py <TARGET> <COMMIT>` — the
   mechanical part: which causes the commit removes, no assertion dropped from
   a test file it touches, the message names the base sha and seconds.
2. Time it: `python3 -m pytest tests -n 8` on the ticket commit and on
   `<COMMIT>`, twice each, on the same machine with nothing else running. Load
   moves the absolute numbers by a minute; compare within one machine only.
3. Read the diff. A fix that is not one of the three is not wrong for that —
   a lock around a cache that xdist's *processes* never share, or a cache that
   speeds one test while the suite gets slower, is.
