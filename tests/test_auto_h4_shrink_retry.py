"""tests/test_auto_h4_shrink_retry.py — AUTO-H4 shrink-retry on truncation.

Covers the feature that follows up a truncated architect JSON array (the
model ran out of output tokens mid-object, salvage kept a prefix, the tail
task was lost) with an automatic re-ask at a smaller `max_tasks` budget,
instead of silently accepting the loss.

  AC-H4-1  Truncated first response → re-asked with a smaller max_tasks.
  AC-H4-2  Second (smaller-budget) response parses clean → its candidates
           are used, NOT merged with the first (truncated) attempt's
           salvaged prefix.
  AC-H4-3  Still truncated after all shrink attempts → keeps the LAST
           attempt's salvaged candidates (fail-open, not fail-closed).
  AC-H4-4  truncation_retry_max=0 → old behaviour: no retry, first
           response's salvaged candidates are kept as-is.
  AC-H4-5  A clean (non-truncated) first response is never retried, even
           if it returned zero or fewer candidates than max_tasks allowed
           — shrink-retry is specifically for JSON truncation, not for
           "the model gave us less than we asked for".
  AC-H4-6  A wholesale-garbage (non-JSON, non-salvageable) response is not
           treated as "truncated" and is not shrink-retried either — that
           failure mode isn't fixed by asking for fewer tasks.
  AC-H4-7  max_tasks actually shrinks between attempts (verified via the
           prompt text sent on each call, since max_tasks is inlined into
           the user message).
  AC-H4-8  truncation_shrink_factor is configurable and respected.
  AC-H4-9  A genuine HTTP failure (5xx) during a shrink-retry attempt still
           goes through the existing transient-error retry loop, and total
           call-failure after that still returns None (not a truncation
           fallback), so the batch is correctly NOT checkpointed.
  AC-H4-10 review_clusters() checkpoints only the FINAL (possibly shrunk)
           result, not the first truncated attempt.

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
    — the exact shape _salvage_json_objects is designed to recover a
    prefix from. All but the last title become complete, salvageable
    objects; the last is deliberately unterminated."""
    complete = ",\n".join(json.dumps(_task(t)) for t in titles[:-1])
    tail_title = titles[-1]
    # Cut the JSON string for the final object's title mid-value, and
    # never close the array — mirrors a real max_tokens cutoff.
    truncated_tail = (
        '{"title": "' + tail_title[: max(1, len(tail_title) // 2)]
    )
    prefix = f"[{complete},\n" if complete else "["
    return prefix + truncated_tail


def _garbage_payload() -> str:
    """Not JSON at all, and nothing salvageable from it — a different
    failure mode than truncation (e.g. the model ignored instructions and
    wrote prose instead of JSON)."""
    return "Sure! Here are some ideas for improving your code, let me think..."


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


# ─────────────────────────────────────────────────────────────────────────────
# AC-H4-1 / AC-H4-2 — truncated → shrink-retry → clean second response wins
# ─────────────────────────────────────────────────────────────────────────────

class TestShrinkRetryOnTruncation:

    def test_truncated_first_response_triggers_retry(
        self, cfg, cluster_and_base
    ) -> None:
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        side_effects = [
            _truncated_payload("Task A", "Task B that got cut off"),
            _good_payload("Task A only"),
        ]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 2
        # AC-H4-2: second attempt's candidates are used, not merged with
        # the first (truncated) attempt's salvaged prefix.
        assert [r.title for r in results] == ["Task A only"]

    def test_second_attempt_uses_smaller_max_tasks(
        self, cfg, cluster_and_base
    ) -> None:
        """AC-H4-7: max_tasks in the prompt actually shrinks."""
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        side_effects = [
            _truncated_payload("Task A", "Task B cut off here"),
            _good_payload("Task A only"),
        ]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ) as mock_llm:
            reviewer.review_clusters([cluster], base_dir, goal="improve code")

        msgs = _user_messages(mock_llm)
        assert len(msgs) == 2
        assert "up to 5 concrete tasks" in msgs[0]
        # default shrink_factor=0.5 on base max_tasks=5 → floor(2.5)=2
        assert "up to 2 concrete tasks" in msgs[1]

    def test_shrink_factor_is_configurable(self, cfg, cluster_and_base) -> None:
        """AC-H4-8."""
        cluster, base_dir = cluster_and_base
        cfg["architect"]["truncation_shrink_factor"] = "0.2"
        reviewer = _reviewer(cfg)
        side_effects = [
            _truncated_payload("Task A", "Task B cut off here"),
            _good_payload("Task A only"),
        ]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ) as mock_llm:
            reviewer.review_clusters([cluster], base_dir, goal="improve code")

        msgs = _user_messages(mock_llm)
        # floor(5 * 0.2) = 1
        assert "up to 1 concrete tasks" in msgs[1]

    def test_invalid_shrink_factor_falls_back_to_default(
        self, cfg, cluster_and_base
    ) -> None:
        cluster, base_dir = cluster_and_base
        cfg["architect"]["truncation_shrink_factor"] = "not-a-number"
        reviewer = _reviewer(cfg)
        side_effects = [
            _truncated_payload("Task A", "Task B cut off here"),
            _good_payload("Task A only"),
        ]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ) as mock_llm:
            reviewer.review_clusters([cluster], base_dir, goal="improve code")

        msgs = _user_messages(mock_llm)
        # falls back to 0.5 → floor(5*0.5)=2
        assert "up to 2 concrete tasks" in msgs[1]

    def test_shrink_factor_out_of_range_falls_back_to_default(
        self, cfg, cluster_and_base
    ) -> None:
        cluster, base_dir = cluster_and_base
        cfg["architect"]["truncation_shrink_factor"] = "1.5"  # not in (0, 1)
        reviewer = _reviewer(cfg)
        side_effects = [
            _truncated_payload("Task A", "Task B cut off here"),
            _good_payload("Task A only"),
        ]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ) as mock_llm:
            reviewer.review_clusters([cluster], base_dir, goal="improve code")

        msgs = _user_messages(mock_llm)
        assert "up to 2 concrete tasks" in msgs[1]


# ─────────────────────────────────────────────────────────────────────────────
# AC-H4-3 / AC-H4-4 — retries exhausted, or disabled
# ─────────────────────────────────────────────────────────────────────────────

class TestRetryExhaustionAndDisable:

    def test_still_truncated_after_all_retries_keeps_last_salvage(
        self, cfg, cluster_and_base
    ) -> None:
        """AC-H4-3: default truncation_retry_max=2 → 3 total attempts
        (1 + 2 retries). If ALL are truncated, keep the LAST attempt's
        salvaged candidates rather than discarding everything."""
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        side_effects = [
            _truncated_payload("Task A1", "Task A2 cut off"),
            _truncated_payload("Task B1", "Task B2 cut off"),
            _truncated_payload("Task C1", "Task C2 cut off"),
        ]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 3  # 1 initial + 2 shrink-retries (default max)
        # Last attempt's salvaged prefix ("Task C1") is what's kept.
        assert [r.title for r in results] == ["Task C1"]

    def test_truncation_retry_max_zero_disables_shrink_retry(
        self, cfg, cluster_and_base
    ) -> None:
        """AC-H4-4: old behaviour preserved when explicitly disabled."""
        cluster, base_dir = cluster_and_base
        cfg["architect"]["truncation_retry_max"] = "0"
        reviewer = _reviewer(cfg)
        with patch(
            "tools.llm_stream.request_completion",
            return_value=_truncated_payload("Task A1", "Task A2 cut off"),
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 1  # no retry at all
        assert [r.title for r in results] == ["Task A1"]

    def test_custom_retry_max_is_respected(self, cfg, cluster_and_base) -> None:
        cluster, base_dir = cluster_and_base
        cfg["architect"]["truncation_retry_max"] = "1"
        reviewer = _reviewer(cfg)
        side_effects = [
            _truncated_payload("Task A1", "Task A2 cut off"),
            _truncated_payload("Task B1", "Task B2 cut off"),
        ]
        with patch(
            "tools.llm_stream.request_completion", side_effect=side_effects
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 2  # 1 initial + 1 retry (capped)
        assert [r.title for r in results] == ["Task B1"]


# ─────────────────────────────────────────────────────────────────────────────
# AC-H4-5 / AC-H4-6 — shrink-retry does NOT fire for other failure modes
# ─────────────────────────────────────────────────────────────────────────────

class TestShrinkRetryDoesNotOverfire:

    def test_clean_response_with_fewer_tasks_than_asked_not_retried(
        self, cfg, cluster_and_base
    ) -> None:
        """AC-H4-5: the model legitimately returning fewer candidates than
        max_tasks permits is not truncation and must not trigger a retry."""
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        with patch(
            "tools.llm_stream.request_completion",
            return_value=_good_payload("Only one task"),
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 1
        assert [r.title for r in results] == ["Only one task"]

    def test_empty_valid_array_not_retried(self, cfg, cluster_and_base) -> None:
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        with patch(
            "tools.llm_stream.request_completion", return_value="[]"
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 1
        assert results == []

    def test_wholesale_garbage_not_treated_as_truncation(
        self, cfg, cluster_and_base
    ) -> None:
        """AC-H4-6: non-JSON, non-salvageable text is a different failure
        mode from AUTO-H4's truncation — max_tasks must NOT shrink for it
        (this is what this test actually guards; see the shrink-retry
        tests above for how a shrink would look). It IS still eligible
        for AUTO-H5's retry (same request, escalating max_tokens/
        temperature since AUTO-H5-ESCALATE-1 — see architect.py's class
        docstring) — a model ignoring the JSON-only instruction is
        exactly the "unsalvageable" bucket AUTO-H5 exists to retry, since
        it's often the same thinking-budget-exhaustion cause Gate 1
        already handles rather than a structural max_tasks-sizing
        problem. With the default empty_response_retry_max=5, a garbage
        response that never changes is retried five times (6 calls
        total) before the batch gives up with 0 candidates."""
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        with patch(
            "tools.llm_stream.request_completion", return_value=_garbage_payload()
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        # AUTO-H5's default retry budget: 1 initial + 5 retries.
        assert mock_llm.call_count == 6
        assert results == []
        # The actual AC-H4-6 claim: max_tasks must stay fixed across all
        # attempts — this is what distinguishes "unsalvageable, plain
        # retry" from AUTO-H4's shrink-retry, not merely the call count.
        msgs = _user_messages(mock_llm)
        assert all("up to 5 concrete tasks" in m for m in msgs)


# ─────────────────────────────────────────────────────────────────────────────
# AC-H4-9 — interaction with the existing transient-error retry loop
# ─────────────────────────────────────────────────────────────────────────────

class TestInteractionWithTransientErrorRetry:

    def test_hard_failure_during_shrink_retry_returns_none_not_salvage(
        self, cfg, cluster_and_base
    ) -> None:
        """A truncation triggers a shrink-retry; if THAT retry call fails
        outright after exhausting its own transient-error retries, the
        batch must come back as a call-failure (None via review_clusters
        not checkpointing it) rather than silently falling back to the
        first attempt's salvaged candidates — a network failure and a
        content-truncation are different failure classes and must not be
        conflated."""
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)
        err = Exception("HTTP 500 server blew up")
        side_effects = [
            _truncated_payload("Task A1", "Task A2 cut off"),
            err, err, err, err,  # 1 initial + 3 retries, all fail
        ]
        with (
            patch("tools.llm_stream.request_completion", side_effect=side_effects) as mock_llm,
            patch("time.sleep"),
        ):
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        # review_clusters treats a None batch result as "skip it" (not
        # checkpointed, not merged into results) — see review_clusters'
        # `if batch_results is None: continue`.
        assert results == []
        assert mock_llm.call_count == 5


# ─────────────────────────────────────────────────────────────────────────────
# AC-H4-10 — checkpointing only records the final result
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckpointRecordsFinalResultOnly:

    def test_checkpoint_stores_post_shrink_candidates(
        self, cfg, cluster_and_base, tmp_path: Path
    ) -> None:
        cluster, base_dir = cluster_and_base
        checkpoint_path = tmp_path / "architect_checkpoint.json"
        side_effects = [
            _truncated_payload("Task A", "Task B cut off here"),
            _good_payload("Task A only"),
        ]
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

        assert [r.title for r in results] == ["Task A only"]
        saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        assert len(saved) == 1
        (batch_key, batch_val), = saved.items()
        assert [t["title"] for t in batch_val] == ["Task A only"]
