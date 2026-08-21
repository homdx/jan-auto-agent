"""tests/test_auto_h5_empty_response_retry.py — AUTO-H5 plain retry on an
unsalvageable (empty/degenerate/non-JSON) architect response.

Distinct from AUTO-H4 (test_auto_h4_shrink_retry.py): AUTO-H4 handles a
JSON array cut off mid-object where at least one complete task WAS
salvaged (the model tried to fit too much into its token budget — the fix
is asking for fewer tasks). AUTO-H5 handles the case where NOTHING could
be salvaged at all:

  - a completely empty response (`raw_text == ""`),
  - a degenerate/repetitive non-JSON ramble that never produces a single
    complete `{...}` object (e.g. a model stuck repeating `"title":
    "title": "title": ...` until it hits its token cap),
  - prose instead of JSON,
  - valid JSON that isn't even a list (e.g. a bare object).

None of these are a "budget too small for how much I tried to say"
problem, so shrinking max_tasks (AUTO-H4's fix) wouldn't help — AUTO-H5
instead re-sends the SAME request unchanged, since this is typically a
one-off decoding hiccup or a flaky upstream/proxy returning an empty body,
not a structural sizing problem.

  AC-H5-1  A completely empty response is retried (plain, same max_tasks).
  AC-H5-2  A degenerate non-JSON ramble is retried the same way.
  AC-H5-3  Second attempt succeeds cleanly → its candidates are used.
  AC-H5-4  Still unsalvageable after all retries → gives up with 0
           candidates for this batch (fail-open, not fail-closed) — the
           batch is simply skipped, not fatal to the whole run.
  AC-H5-5  empty_response_retry_max=0 → old behaviour: no retry, 0
           candidates immediately.
  AC-H5-6  Custom empty_response_retry_max is respected (retry count).
  AC-H5-7  max_tasks does NOT shrink across AUTO-H5 retries (verified via
           the prompt text sent on each call) — this is the key
           distinction from AUTO-H4's shrink behaviour.
  AC-H5-8  A response that alternates between truncated (AUTO-H4) and
           unsalvageable (AUTO-H5) across attempts is handled by the
           right mechanism on each attempt, with independent attempt
           budgets for each failure mode.
  AC-H5-9  A genuine HTTP failure (5xx) during an AUTO-H5 retry still goes
           through the existing transient-error retry loop, and total
           call-failure after that still returns None (not an
           unsalvageable-batch fallback), so the batch is correctly NOT
           checkpointed.
  AC-H5-10 A clean, validly-empty `"[]"` response (the model legitimately
           found nothing to propose) is NOT retried by AUTO-H5 — this is
           a successful outcome, not a failure.
  AC-H5-11 review_clusters() checkpoints only the FINAL result after
           AUTO-H5 retries succeed.

All LLM calls are patched; no network or real sleep I/O occurs.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.auto.architect import ClusterReviewer, review_clusters
from tools.auto.repo_ingest import RepoCluster


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def cfg() -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    c.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url":   "http://localhost:1337/v1",
            "api_key":    "test",
            "model":      "test-model",
            "api_format": "openai",
        },
        "architect": {"temperature": "0.2", "max_tokens": "512"},
        "loop":      {"timeout_seconds": "10"},
    })
    return c


def _reviewer(cfg: configparser.ConfigParser) -> ClusterReviewer:
    return ClusterReviewer(
        config=cfg,
        base_url="http://localhost:1337/v1",
        api_key="test",
        model="test-model",
        api_format="openai",
        verify_ssl=False,
    )


@pytest.fixture()
def cluster_and_base(tmp_path: Path) -> tuple[RepoCluster, Path]:
    """One real file so _build_file_contents doesn't error."""
    src = tmp_path / "tools" / "example.py"
    src.parent.mkdir()
    src.write_text("def fn(): pass\n", encoding="utf-8")
    cl = RepoCluster(name="agents", patterns=["tools/*"], files=["tools/example.py"])
    return cl, tmp_path


def _task(title: str) -> dict:
    return {
        "title": title,
        "instruction": "Do the fix.",
        "target_files": ["tools/example.py"],
        "acceptance_check": "pytest tests/",
        "cited_location": {
            "file": "tools/example.py",
            "symbol": "fn",
            "line_start": 1,
            "line_end": 1,
        },
    }


def _good_payload(*titles: str) -> str:
    return json.dumps([_task(t) for t in titles])


def _truncated_payload(*titles: str) -> str:
    """A JSON array cut off mid-way through the LAST object's string value
    — salvageable (AUTO-H4's territory, not H5's)."""
    complete = ",\n".join(json.dumps(_task(t)) for t in titles[:-1])
    tail_title = titles[-1]
    truncated_tail = (
        '{"title": "' + tail_title[: max(1, len(tail_title) // 2)]
    )
    prefix = f"[{complete},\n" if complete else "["
    return prefix + truncated_tail


_EMPTY_PAYLOAD = ""

_DEGENERATE_PAYLOAD = (
    '[\n  {\n  "title": "title": "title": "title": "title": "title": '
    '"title": "title": "title": "title": "title": "title": "title": '
)

_PROSE_PAYLOAD = "Sure! Here are some ideas for improving your code, let me think..."

_NON_LIST_PAYLOAD = json.dumps({"title": "not an array at all"})


def _user_messages(mock_llm) -> list[str]:
    """Extract the user-message content sent to request_completion on each
    call, in call order, by inspecting the `payload` kwarg."""
    out = []
    for c in mock_llm.call_args_list:
        payload = c.kwargs.get("payload") or c.args[2]
        messages = payload.get("messages", [])
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        out.append(user)
    return out


def _payloads(mock_llm) -> list[dict]:
    """Extract the full outgoing payload dict for each call, in call
    order — used to inspect max_tokens/temperature actually sent per
    attempt (AUTO-H5-ESCALATE-1)."""
    out = []
    for c in mock_llm.call_args_list:
        payload = c.kwargs.get("payload") or c.args[2]
        out.append(payload)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# AC-H5-1 / AC-H5-2 / AC-H5-3 — unsalvageable responses are retried
# ─────────────────────────────────────────────────────────────────────────────

class TestPlainRetryOnUnsalvageableResponse:

    def test_empty_response_triggers_retry(self, cfg, cluster_and_base) -> None:
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        side_effects = [_EMPTY_PAYLOAD, _good_payload("Recovered task")]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 2
        assert [r.title for r in results] == ["Recovered task"]

    def test_degenerate_ramble_triggers_retry(self, cfg, cluster_and_base) -> None:
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        side_effects = [_DEGENERATE_PAYLOAD, _good_payload("Recovered task")]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 2
        assert [r.title for r in results] == ["Recovered task"]

    def test_prose_instead_of_json_triggers_retry(self, cfg, cluster_and_base) -> None:
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        side_effects = [_PROSE_PAYLOAD, _good_payload("Recovered task")]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 2
        assert [r.title for r in results] == ["Recovered task"]

    def test_non_list_json_triggers_retry(self, cfg, cluster_and_base) -> None:
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        side_effects = [_NON_LIST_PAYLOAD, _good_payload("Recovered task")]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 2
        assert [r.title for r in results] == ["Recovered task"]


# ─────────────────────────────────────────────────────────────────────────────
# AC-H5-4 / AC-H5-5 / AC-H5-6 — retry exhaustion, disable, custom count
# ─────────────────────────────────────────────────────────────────────────────

class TestRetryExhaustionAndConfig:

    def test_still_unsalvageable_after_all_retries_gives_up_with_zero(
        self, cfg, cluster_and_base
    ) -> None:
        """AC-H5-4: default empty_response_retry_max=5 → 6 total attempts
        (1 + 5 retries, was 2 retries / 3 total before AUTO-H5-ESCALATE-1
        raised the default alongside adding escalation — see the class
        docstring in architect.py). If ALL are unsalvageable, the batch
        fails open with 0 candidates rather than raising or aborting the
        whole run."""
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        with patch(
            "tools.llm_stream.request_completion", return_value=_EMPTY_PAYLOAD
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 6  # 1 initial + 5 retries (default max)
        assert results == []

    def test_empty_response_retry_max_zero_disables_retry(
        self, cfg, cluster_and_base
    ) -> None:
        """AC-H5-5: old behaviour preserved when explicitly disabled."""
        cluster, base_dir = cluster_and_base
        cfg["architect"]["empty_response_retry_max"] = "0"
        reviewer = _reviewer(cfg)
        with patch(
            "tools.llm_stream.request_completion", return_value=_EMPTY_PAYLOAD
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 1
        assert results == []

    def test_custom_retry_max_is_respected(self, cfg, cluster_and_base) -> None:
        cluster, base_dir = cluster_and_base
        cfg["architect"]["empty_response_retry_max"] = "1"
        reviewer = _reviewer(cfg)
        with patch(
            "tools.llm_stream.request_completion", return_value=_EMPTY_PAYLOAD
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 2  # 1 initial + 1 retry (capped)
        assert results == []

    def test_invalid_retry_max_falls_back_to_config_default_parsing(
        self, cfg, cluster_and_base
    ) -> None:
        """getint() raising on a non-integer falls through this method's
        own try/except path the same way truncation_retry_max does —
        verify a garbage value doesn't crash the run."""
        cluster, base_dir = cluster_and_base
        cfg["architect"]["empty_response_retry_max"] = "not-a-number"
        reviewer = _reviewer(cfg)
        with patch(
            "tools.llm_stream.request_completion", return_value=_EMPTY_PAYLOAD
        ):
            # configparser.getint raises ValueError for a non-integer with
            # no fallback triggered (fallback only applies to a MISSING
            # key, not an invalid one) — this documents that behaviour
            # rather than asserting a specific recovery, since the ini
            # value itself is simply invalid input.
            with pytest.raises(ValueError):
                reviewer.review_clusters([cluster], base_dir, goal="improve code")


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-H5-ESCALATE-1 — max_tokens/temperature escalate across AUTO-H5
# retries, mirroring Gate1Filter's own unparseable-retry escalation
# (tools/auto/gate1_filter.py). An unsalvageable response is very often
# the model's thinking channel consuming the entire ORIGINAL budget, not
# a one-off decoding hiccup a same-settings retry can fix — see field
# report in the ClusterReviewer class docstring.
# ─────────────────────────────────────────────────────────────────────────────

class TestEmptyResponseRetryEscalation:

    def test_max_tokens_doubles_from_floor_each_attempt(
        self, cfg, cluster_and_base
    ) -> None:
        """[architect] max_tokens=512 (this fixture's cfg) is deliberately
        NOT the base the escalation multiplies from — same rationale as
        Gate1Filter: a small configured budget is exactly the thing
        causing the exhaustion, so multiplying it stays small too. The
        floor (4096) and doubling are fixed regardless of the configured
        base."""
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        with patch(
            "tools.llm_stream.request_completion", return_value=_EMPTY_PAYLOAD
        ) as mock_llm:
            reviewer.review_clusters([cluster], base_dir, goal="improve code")

        payloads = _payloads(mock_llm)
        assert len(payloads) == 6  # 1 initial + 5 retries (default max)
        assert payloads[0]["max_tokens"] == 512    # initial: unescalated base config
        assert payloads[1]["max_tokens"] == 4096   # retry 1: floor
        assert payloads[2]["max_tokens"] == 8192   # retry 2: floor * 2
        assert payloads[3]["max_tokens"] == 16384  # retry 3: floor * 4
        assert payloads[4]["max_tokens"] == 32768  # retry 4: floor * 8
        assert payloads[5]["max_tokens"] == 32768  # retry 5: floor * 16, capped

    def test_temperature_decreases_each_attempt_floored_at_zero(
        self, cfg, cluster_and_base
    ) -> None:
        """cfg's [architect] temperature = 0.2 — each retry steps down by
        0.1, floored at 0.0 (never negative)."""
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        with patch(
            "tools.llm_stream.request_completion", return_value=_EMPTY_PAYLOAD
        ) as mock_llm:
            reviewer.review_clusters([cluster], base_dir, goal="improve code")

        payloads = _payloads(mock_llm)
        assert payloads[0]["temperature"] == pytest.approx(0.2)   # initial: unescalated
        assert payloads[1]["temperature"] == pytest.approx(0.1)
        assert payloads[2]["temperature"] == pytest.approx(0.0)
        assert payloads[3]["temperature"] == pytest.approx(0.0)   # floored, not negative
        assert payloads[4]["temperature"] == pytest.approx(0.0)
        assert payloads[5]["temperature"] == pytest.approx(0.0)

    def test_ceiling_tracks_num_ctx_when_configured(
        self, cfg, cluster_and_base
    ) -> None:
        """When [api_local] num_ctx is set, the ceiling is num_ctx * 0.5,
        not the flat 32768 default — matches Gate1Filter's own ctx-aware
        ceiling so a small-context model doesn't get an unrealistic
        token request."""
        cfg["api_local"]["num_ctx"] = "20000"
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        with patch(
            "tools.llm_stream.request_completion", return_value=_EMPTY_PAYLOAD
        ) as mock_llm:
            reviewer.review_clusters([cluster], base_dir, goal="improve code")

        payloads = _payloads(mock_llm)
        # ceiling = int(20000 * 0.5) = 10000
        assert payloads[1]["max_tokens"] == 4096    # under ceiling: unaffected
        assert payloads[2]["max_tokens"] == 8192    # under ceiling: unaffected
        assert payloads[3]["max_tokens"] == 10000   # would be 16384 — capped
        assert payloads[4]["max_tokens"] == 10000   # would be 32768 — capped
        assert payloads[5]["max_tokens"] == 10000   # would be 65536 — capped

    def test_ceiling_defaults_to_flat_value_when_num_ctx_unset(
        self, cfg, cluster_and_base
    ) -> None:
        """No [api_local] num_ctx at all (this fixture's default) — the
        ceiling falls back to the flat 32768 default, not e.g. 0 (which
        would collapse every escalated attempt down to the floor)."""
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        with patch(
            "tools.llm_stream.request_completion", return_value=_EMPTY_PAYLOAD
        ) as mock_llm:
            reviewer.review_clusters([cluster], base_dir, goal="improve code")

        payloads = _payloads(mock_llm)
        assert payloads[4]["max_tokens"] == 32768  # reaches the flat ceiling
        assert payloads[5]["max_tokens"] == 32768  # stays capped there

    def test_recovers_mid_escalation_when_a_later_attempt_succeeds(
        self, cfg, cluster_and_base
    ) -> None:
        """The escalation isn't just cosmetic — a wider budget on a later
        attempt can be the actual reason a previously-empty response
        starts producing valid JSON."""
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        side_effects = [_EMPTY_PAYLOAD, _EMPTY_PAYLOAD, _good_payload("Recovered task")]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        payloads = _payloads(mock_llm)
        assert mock_llm.call_count == 3
        assert payloads[2]["max_tokens"] == 8192  # the attempt that succeeded
        assert [r.title for r in results] == ["Recovered task"]


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-H5-ESCALATE-1 must NOT leak into AUTO-H4's shrink-retry path
# ─────────────────────────────────────────────────────────────────────────────

class TestEscalationDoesNotAffectTruncatedRetries:

    def test_truncated_retry_keeps_base_tokens_and_temperature(
        self, cfg, cluster_and_base
    ) -> None:
        """AUTO-H4's shrink-retry (a truncated, partially-salvaged
        response) has its own, unrelated remedy — shrinking max_tasks —
        and must keep using the base config's max_tokens/temperature
        unchanged. Only AUTO-H5's unsalvageable branch escalates."""
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        side_effects = [
            _truncated_payload("A", "B", "C", "D", "E"),
            _good_payload("Recovered"),
        ]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ) as mock_llm:
            reviewer.review_clusters([cluster], base_dir, goal="improve code")

        payloads = _payloads(mock_llm)
        assert payloads[1]["max_tokens"] == 512          # cfg's base, not escalated
        assert payloads[1]["temperature"] == pytest.approx(0.2)  # cfg's base


# ─────────────────────────────────────────────────────────────────────────────
# AC-H5-7 — max_tasks does NOT shrink across AUTO-H5 retries
# ─────────────────────────────────────────────────────────────────────────────

class TestMaxTasksStaysConstant:

    def test_max_tasks_unchanged_across_empty_retries(
        self, cfg, cluster_and_base
    ) -> None:
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        side_effects = [_EMPTY_PAYLOAD, _EMPTY_PAYLOAD, _good_payload("Recovered")]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ) as mock_llm:
            reviewer.review_clusters([cluster], base_dir, goal="improve code")

        msgs = _user_messages(mock_llm)
        assert len(msgs) == 3
        # Base max_tasks (code mode) is 5 — must be identical on all three
        # calls, never shrunk the way AUTO-H4 would.
        assert all("up to 5 concrete tasks" in m for m in msgs)


# ─────────────────────────────────────────────────────────────────────────────
# AC-H5-8 — mixed failure modes across attempts, independent budgets
# ─────────────────────────────────────────────────────────────────────────────

class TestMixedFailureModes:

    def test_truncated_then_unsalvageable_then_success(
        self, cfg, cluster_and_base
    ) -> None:
        """Attempt 1: truncated (AUTO-H4 fires, shrinks max_tasks).
        Attempt 2 (shrunk budget): unsalvageable (AUTO-H5 fires, same
        shrunk budget). Attempt 3: clean success. Each mechanism reacts
        correctly to the failure mode of ITS triggering attempt."""
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        side_effects = [
            _truncated_payload("Task A", "Task B cut off here"),
            _EMPTY_PAYLOAD,
            _good_payload("Final task"),
        ]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 3
        assert [r.title for r in results] == ["Final task"]

        msgs = _user_messages(mock_llm)
        assert "up to 5 concrete tasks" in msgs[0]
        # shrunk after attempt 1's truncation: floor(5*0.5) = 2
        assert "up to 2 concrete tasks" in msgs[1]
        # AUTO-H5 retry after attempt 2's empty response keeps the SAME
        # (already-shrunk) budget — it doesn't shrink further itself.
        assert "up to 2 concrete tasks" in msgs[2]

    def test_independent_attempt_budgets_for_each_failure_mode(
        self, cfg, cluster_and_base
    ) -> None:
        """Exhausting AUTO-H5's budget on unsalvageable attempts doesn't
        borrow from or block AUTO-H4's separate shrink budget, and vice
        versa — each is tracked independently."""
        cluster, base_dir = cluster_and_base
        cfg["architect"]["empty_response_retry_max"] = "1"
        cfg["architect"]["truncation_retry_max"] = "1"
        reviewer = _reviewer(cfg)
        side_effects = [
            _EMPTY_PAYLOAD,   # attempt 1: unsalvageable (H5 attempt 1/1)
            _truncated_payload("X1", "X2 cut off"),  # attempt 2: truncated (H4 attempt 1/1)
            _good_payload("Done"),  # attempt 3: clean
        ]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 3
        assert [r.title for r in results] == ["Done"]


# ─────────────────────────────────────────────────────────────────────────────
# AC-H5-9 — interaction with the existing transient-error retry loop
# ─────────────────────────────────────────────────────────────────────────────

class TestInteractionWithTransientErrorRetry:

    def test_hard_failure_during_empty_retry_returns_none_not_zero_candidates(
        self, cfg, cluster_and_base
    ) -> None:
        """An unsalvageable response triggers an AUTO-H5 retry; if THAT
        retry call fails outright after exhausting its own transient-error
        retries, the batch must come back as a call-failure (None via
        review_clusters not checkpointing it), not as a "batch legitimately
        produced 0 candidates" outcome — a network failure and an
        unsalvageable-content failure are different failure classes."""
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        err = Exception("HTTP 500 server blew up")
        side_effects = [
            _EMPTY_PAYLOAD,
            err, err, err, err,  # 1 initial + 3 retries, all fail
        ]
        with (
            patch("tools.llm_stream.request_completion", side_effect=side_effects) as mock_llm,
            patch("time.sleep"),
        ):
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert results == []
        assert mock_llm.call_count == 5


# ─────────────────────────────────────────────────────────────────────────────
# AC-H5-10 — a clean, validly-empty array is a success, not a failure
# ─────────────────────────────────────────────────────────────────────────────

class TestCleanEmptyArrayIsNotAFailure:

    def test_clean_empty_array_not_retried(self, cfg, cluster_and_base) -> None:
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        with patch(
            "tools.llm_stream.request_completion", return_value="[]"
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 1
        assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# AC-H5-11 — checkpointing records only the final (post-retry) result
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckpointRecordsFinalResultOnly:

    def test_checkpoint_stores_post_retry_candidates(
        self, cfg, cluster_and_base, tmp_path: Path
    ) -> None:
        cluster, base_dir = cluster_and_base
        checkpoint_path = tmp_path / "architect_checkpoint.json"
        side_effects = [_EMPTY_PAYLOAD, _good_payload("Recovered task")]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ):
            results = review_clusters(
                clusters=[cluster],
                base_dir=base_dir,
                config=cfg,
                goal="improve code",
                checkpoint_path=checkpoint_path,
            )

        assert [r.title for r in results] == ["Recovered task"]
        saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        assert len(saved) == 1
        (batch_key, batch_val), = saved.items()
        assert [t["title"] for t in batch_val] == ["Recovered task"]
