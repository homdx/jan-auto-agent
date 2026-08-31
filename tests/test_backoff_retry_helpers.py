"""tests/test_backoff_retry_helpers.py

Dedicated coverage for the two shared retry helpers extracted into
tools/backoff.py to remove the retry logic that used to be copy-pasted
at every call site:

  retry_with_backoff  — the bounded "call / on exception sleep
                         backoff_seconds(n) / retry" loop previously
                         duplicated in run_search (single-file + chunk),
                         FaqAgent._answer_legacy and summarize_repo.
  api_error_pause     — the Issue-7 "consecutive API error" block
                         (milestone table once, backoff_seconds(n-1),
                         sleep_with_interrupt_save checkpoint) previously
                         duplicated 5x across main.py / actions.py.

Behaviour locked in here mirrors the pre-refactor call sites exactly:

  * first success returns immediately, no sleep, no callbacks;
  * a failure sleeps backoff_seconds(failure_index) BEFORE the next
    attempt (never after the last one);
  * the final failure re-raises the LAST exception;
  * on_error fires for every failure (1-indexed), on_retry only between
    attempts;
  * attempts<=1 means one try, no retry, no sleep;
  * api_error_pause prints MILESTONE_TABLE exactly once (first error)
    and delegates the sleep to sleep_with_interrupt_save.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.actions as actions_mod  # noqa: E402
from tools import backoff  # noqa: E402


class TestRetryWithBackoffSuccess:
    def test_first_try_success_returns_without_sleeping(self):
        sleeps, retries, errors = [], [], []
        result = backoff.retry_with_backoff(
            lambda: "ok", attempts=3,
            sleep_fn=sleeps.append,
            on_retry=lambda exc, n, wait: retries.append((exc, n, wait)),
            on_error=lambda exc, n: errors.append((exc, n)),
        )
        assert result == "ok"
        assert sleeps == []
        assert retries == []
        assert errors == []

    def test_later_try_success_stops_retrying(self):
        calls = {"n": 0}
        sleeps = []

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("boom")
            return "recovered"

        result = backoff.retry_with_backoff(
            flaky, attempts=5, sleep_fn=sleeps.append,
        )
        assert result == "recovered"
        assert calls["n"] == 3
        # one sleep per FAILED attempt before success: 1s then 2s
        assert sleeps == [1, 2]

    def test_falsy_return_value_is_still_a_success(self):
        # None is a legitimate return (run_search's _ask_over_text contract):
        # retry_with_backoff must return it immediately, not treat it as a
        # failure — only EXCEPTIONS are retry-worthy.
        calls = []

        def returns_none():
            calls.append(1)
            return None

        sleeps = []
        result = backoff.retry_with_backoff(
            returns_none, attempts=3, sleep_fn=sleeps.append,
        )
        assert result is None
        assert calls == [1]
        assert sleeps == []


class TestRetryWithBackoffFailure:
    def test_raises_last_exception_after_all_attempts(self):
        calls, sleeps = [], []

        def always_fails():
            calls.append(1)
            raise ConnectionError(f"refused #{len(calls)}")

        try:
            backoff.retry_with_backoff(
                always_fails, attempts=3, sleep_fn=sleeps.append,
            )
            raise AssertionError("must raise")
        except ConnectionError as exc:
            assert "refused #3" in str(exc)
        assert len(calls) == 3
        # sleeps only BETWEEN attempts — never after the final failure
        assert sleeps == [1, 2]

    def test_single_attempt_never_sleeps(self):
        calls, sleeps = [], []

        def always_fails():
            calls.append(1)
            raise RuntimeError("boom")

        try:
            backoff.retry_with_backoff(
                always_fails, attempts=1, sleep_fn=sleeps.append,
            )
            raise AssertionError("must raise")
        except RuntimeError:
            pass
        assert calls == [1]
        assert sleeps == []

    def test_zero_attempts_means_one_try(self):
        # A falsy/zero attempts must degrade to a single call, never to
        # zero calls (a silent no-op success) — same defensive spirit as
        # backoff_seconds' clamping.
        calls = []

        def always_fails():
            calls.append(1)
            raise RuntimeError("boom")

        try:
            backoff.retry_with_backoff(always_fails, attempts=0)
            raise AssertionError("must raise")
        except RuntimeError:
            pass
        assert calls == [1]

    def test_backoff_series_escalates(self):
        sleeps = []

        def always_fails():
            raise RuntimeError("boom")

        try:
            backoff.retry_with_backoff(
                always_fails, attempts=5, sleep_fn=sleeps.append,
            )
        except RuntimeError:
            pass
        assert sleeps == [1, 2, 4, 8]

    def test_on_error_fires_for_every_failure_including_last(self):
        errors, retries = [], []

        def always_fails():
            raise RuntimeError("boom")

        try:
            backoff.retry_with_backoff(
                always_fails, attempts=3,
                sleep_fn=lambda s: None,
                on_retry=lambda exc, n, wait: retries.append((n, wait)),
                on_error=lambda exc, n: errors.append(n),
            )
        except RuntimeError:
            pass
        # 1-indexed error counts, ALL failures reported (summarize_repo's
        # public on_error contract: 1 initial + max_retries retries)
        assert errors == [1, 2, 3]
        # on_retry only between attempts, with the matching wait
        assert retries == [(1, 1), (2, 2)]


class TestApiErrorPause:
    def test_prints_milestone_table_exactly_once(self, capsys, monkeypatch):
        # Never actually sleep in these tests.
        monkeypatch.setattr(backoff.time, "sleep", lambda s: None)
        for count in (1, 2, 3):
            backoff.api_error_pause(count, {"loop": "run_edit", "iteration": 1})
        out = capsys.readouterr().out
        assert out.count("Backoff schedule") == 1

    def test_first_error_waits_one_second(self, monkeypatch):
        slept = []
        monkeypatch.setattr(backoff.time, "sleep", lambda s: slept.append(s))
        backoff.api_error_pause(1, {"loop": "run_edit"})
        assert slept == [1]

    def test_later_errors_escalate_on_the_standard_series(self, monkeypatch):
        slept = []
        monkeypatch.setattr(backoff.time, "sleep", lambda s: slept.append(s))
        for count in (1, 2, 3, 4):
            backoff.api_error_pause(count, {"loop": "run_edit"})
        assert slept == [1, 2, 4, 8]

    def test_no_milestone_table_when_first_error_is_not_first(self, capsys, monkeypatch):
        # A loop resuming from a checkpoint whose count starts above 1 must
        # not re-print the schedule.
        monkeypatch.setattr(backoff.time, "sleep", lambda s: None)
        backoff.api_error_pause(2, {"loop": "run_edit"})
        assert "Backoff schedule" not in capsys.readouterr().out


# ── run_search integration: the refactored retry loop keeps its contract ──────
#
# run_search signals "the /search LLM call itself failed" via None from
# _ask_over_text, and retries that failure a bounded number of times
# before giving up on the file/chunk. The refactor routes this through
# retry_with_backoff, which retries on EXCEPTIONS — so the None sentinel
# must be translated (_SearchChunkFailed) and the pre-refactor counts
# preserved exactly.

class _FakeOrchestrator:
    def __init__(self):
        self.model = "test-model"
        self.base_url = "http://fake-host"
        self.api_key = "x"
        self.timeout_seconds = 5
        self.stream_agents = False
        self.ssl_context = None
        self.api_format = "openai"
        self.config = None
        self.search_full_file_max_chars = 10_000


def _make_actions():
    class _Orch(actions_mod.OrchestratorActions, _FakeOrchestrator):
        def __init__(self):
            _FakeOrchestrator.__init__(self)

    return _Orch()


class TestRunSearchSingleFileRetry:
    def test_recovers_from_transient_none_then_validates(self, tmp_path, monkeypatch):
        """A file within the budget: _ask_over_text returns None twice (the
        transient-failure sentinel) then succeeds — exactly the retry the
        BUGFIX comment above that path promises."""
        monkeypatch.setattr(backoff.time, "sleep", lambda s: None)
        orch = _make_actions()
        target = tmp_path / "doc.md"
        target.write_text("some content", encoding="utf-8")

        with patch(
            "tools.actions.request_completion",
            side_effect=[
                ConnectionError("net"),   # attempt 1 — transient failure
                ConnectionError("net"),   # attempt 2 — transient failure
                "the answer",             # attempt 3 — recovered
                '{"status": "approved", "grounded": true, "feedback": ""}',  # validator
            ],
        ) as mock_llm:
            orch.run_search("what is this in doc.md", str(tmp_path))

        assert mock_llm.call_count == 4  # 3 search attempts (1 + 2 retries) + 1 validation

    def test_gives_up_after_three_attempts_on_single_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backoff.time, "sleep", lambda s: None)
        orch = _make_actions()
        target = tmp_path / "doc.md"
        target.write_text("some content", encoding="utf-8")

        with patch(
            "tools.actions.request_completion",
            side_effect=ConnectionError("net"),
        ) as mock_llm:
            orch.run_search("what is this in doc.md", str(tmp_path))

        assert mock_llm.call_count == 3  # the bounded _RETRY budget


class TestRunSearchChunkRetry:
    def _big_doc(self, tmp_path):
        # > search_full_file_max_chars (10_000) forces the chunked path.
        target = tmp_path / "doc.md"
        target.write_text("x" * 30_000, encoding="utf-8")
        return target

    def test_transient_chunk_failure_is_retried_not_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(backoff.time, "sleep", lambda s: None)
        orch = _make_actions()
        target = self._big_doc(tmp_path)

        with patch(
            "tools.actions.request_completion",
            side_effect=[
                ConnectionError("net"),   # chunk 1, attempt 1 — transient
                "the answer",             # chunk 1, attempt 2 — recovered
                '{"status": "approved", "grounded": true, "feedback": ""}',  # validator
            ],
        ) as mock_llm:
            orch.run_search("what is this in doc.md", str(tmp_path))

        assert mock_llm.call_count == 3

    def test_unreachable_chunk_is_skipped_search_continues(self, tmp_path, monkeypatch):
        """A chunk whose 3 attempts ALL fail is skipped — the search does
        not abort, it moves on (and here reports no validated answer)."""
        monkeypatch.setattr(backoff.time, "sleep", lambda s: None)
        orch = _make_actions()
        target = self._big_doc(tmp_path)

        with patch(
            "tools.actions.request_completion",
            side_effect=ConnectionError("net"),
        ) as mock_llm:
            orch.run_search("what is this in doc.md", str(tmp_path))

        # 1 chunk (30000 chars packs into one ~10000-char chunk + overlap
        # head-room) × 3 attempts, no validator call (never got an answer)
        assert mock_llm.call_count == 3

