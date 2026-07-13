"""tests/test_architect_resilience.py — Retry + checkpoint resilience.

Covers the two features added to fix HTTP-500 cluster loss:

  RETRY (fix 1 — _review_one_cluster)
  ────────────────────────────────────
  AC-R1  Transient 500 → success on 2nd attempt; sleep called once.
  AC-R2  Non-transient 4xx → NOT retried; returns [] immediately.
  AC-R3  All 4 attempts (1 + 3 retries) fail on 500 → returns [].
  AC-R4  time.sleep receives the exact configured delays (5, 15, 30 s).
  AC-R5  502 / 503 / 504 / "Connection refused" / "timed out" are retried.
  AC-R6  Clean first attempt → no sleep at all; result returned normally.

  CHECKPOINT (fix 2 — review_clusters + pipeline wiring)
  ───────────────────────────────────────────────────────
  AC-C1  _serialise/_deserialise round-trip preserves every field.
  AC-C2  Checkpoint file is written after each successful batch.
  AC-C3  Checkpoint hit → LLM NOT called again; saved candidates returned.
  AC-C4  Checkpoint miss (new cluster name) → LLM is called normally.
  AC-C5  Corrupt checkpoint JSON → graceful fallback to fresh LLM call.
  AC-C6  Batch key includes goal; different goals do NOT share cache.
  AC-C7  checkpoint_path=None → no file created (original behaviour).
  AC-C8  Partial run (some batches cached, rest new) → only new batches call LLM.
  AC-C9  review_clusters convenience function forwards checkpoint_path.

All LLM calls are patched; no network or real sleep I/O occurs.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from unittest.mock import call, patch

import pytest

from tools.auto.architect import (
    CandidateTask,
    CitedLocation,
    ClusterReviewer,
    _deserialise_candidates,
    _serialise_candidates,
    review_clusters,
)
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


@pytest.fixture()
def reviewer(cfg: configparser.ConfigParser) -> ClusterReviewer:
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


def _good_payload(title: str = "Fix something") -> str:
    return json.dumps([{
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
    }])


def _make_candidate(title: str = "Fix something") -> CandidateTask:
    return CandidateTask(
        title=title,
        instruction="Do the fix.",
        target_files=["tools/example.py"],
        acceptance_check="pytest tests/",
        cluster="agents",
        cited_location=CitedLocation(
            file="tools/example.py",
            symbol="fn",
            line_start=1,
            line_end=1,
        ),
        raw={},
    )


# ─────────────────────────────────────────────────────────────────────────────
# RETRY — AC-R1 through AC-R6
# ─────────────────────────────────────────────────────────────────────────────

class TestRetryOnTransientErrors:

    # AC-R1: 500 on first call, success on second
    def test_succeeds_on_second_attempt_after_500(
        self, reviewer: ClusterReviewer, cluster_and_base
    ) -> None:
        cluster, base_dir = cluster_and_base
        side_effects = [
            Exception("HTTP 500 from http://localhost:1337/v1/chat/completions"),
            _good_payload("Recovered task"),
        ]
        with (
            patch("tools.llm_stream.request_completion", side_effect=side_effects),
            patch("time.sleep") as mock_sleep,
        ):
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert len(results) == 1
        assert results[0].title == "Recovered task"
        # Must have slept once before the retry
        assert mock_sleep.call_count == 1

    # AC-R2: 4xx is not retried
    def test_non_transient_4xx_not_retried(
        self, reviewer: ClusterReviewer, cluster_and_base
    ) -> None:
        cluster, base_dir = cluster_and_base
        with (
            patch(
                "tools.llm_stream.request_completion",
                side_effect=Exception("HTTP 401 Unauthorized"),
            ) as mock_llm,
            patch("time.sleep") as mock_sleep,
        ):
            results = reviewer.review_clusters([cluster], base_dir)

        assert results == []
        assert mock_llm.call_count == 1   # no retry
        mock_sleep.assert_not_called()

    # AC-R3: All 4 attempts (initial + 3 retries) fail → []
    def test_all_retries_exhausted_returns_empty(
        self, reviewer: ClusterReviewer, cluster_and_base
    ) -> None:
        cluster, base_dir = cluster_and_base
        err = Exception("HTTP 500 server blew up")
        with (
            patch("tools.llm_stream.request_completion", side_effect=[err, err, err, err]),
            patch("time.sleep"),
        ):
            results = reviewer.review_clusters([cluster], base_dir)

        assert results == []

    # AC-R4: sleep delays are 5 s, 15 s, 30 s in that order
    def test_sleep_delays_are_correct(
        self, reviewer: ClusterReviewer, cluster_and_base
    ) -> None:
        cluster, base_dir = cluster_and_base
        err = Exception("HTTP 500 transient")
        with (
            patch("tools.llm_stream.request_completion", side_effect=[err, err, err, err]),
            patch("time.sleep") as mock_sleep,
        ):
            reviewer.review_clusters([cluster], base_dir)

        assert mock_sleep.call_args_list == [call(5), call(15), call(30)]

    # AC-R5: other transient patterns are retried
    @pytest.mark.parametrize("error_text", [
        "HTTP 502 Bad Gateway",
        "HTTP 503 Service Unavailable",
        "HTTP 504 Gateway Timeout",
        "Connection refused by server",
        "ConnectionRefused: [Errno 111]",
        "request timed out after 300s",
        "Read timeout occurred",
    ])
    def test_other_transient_errors_are_retried(
        self, reviewer: ClusterReviewer, cluster_and_base, error_text: str
    ) -> None:
        cluster, base_dir = cluster_and_base
        with (
            patch(
                "tools.llm_stream.request_completion",
                side_effect=[Exception(error_text), _good_payload()],
            ),
            patch("time.sleep"),
        ):
            results = reviewer.review_clusters([cluster], base_dir)

        assert len(results) == 1

    # AC-R6: clean first attempt → no sleep
    def test_clean_first_attempt_no_sleep(
        self, reviewer: ClusterReviewer, cluster_and_base
    ) -> None:
        cluster, base_dir = cluster_and_base
        with (
            patch("tools.llm_stream.request_completion", return_value=_good_payload()),
            patch("time.sleep") as mock_sleep,
        ):
            results = reviewer.review_clusters([cluster], base_dir)

        assert len(results) == 1
        mock_sleep.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# BATCH CLUSTER-NAME NORMALISATION (AUTO-T1 regression)
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchClusterNameNormalisation:
    """A cluster large enough to need more than one review batch must still
    return candidates whose ``.cluster`` is the plain, un-suffixed cluster
    name — not "<name> (batch N/M)".

    tools/auto/pipeline.py builds the ``cluster_files`` dict Gate 1 uses to
    catch hallucinated file citations as ``{c.name: set(c.files) for c in
    clusters}``, keyed by the plain name repo_ingest assigned. If a
    batch-suffixed name ever leaks onto ``CandidateTask.cluster``,
    ``cluster_files.get(candidate.cluster, set())`` silently returns an
    empty set and gate1_filter.py's hallucinated-path guard (``if known and
    loc.file not in known:``) never runs, since an empty set is falsy.
    """

    def test_batched_review_normalises_cluster_name(self, tmp_path: Path) -> None:
        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "a.py").write_text("def a(): pass\n", encoding="utf-8")
        (tmp_path / "tools" / "b.py").write_text("def b(): pass\n", encoding="utf-8")
        cluster = RepoCluster(
            name="agents",
            patterns=["tools/*"],
            files=["tools/a.py", "tools/b.py"],
        )

        cfg = configparser.ConfigParser()
        cfg.read_dict({
            "api":       {"active": "local", "verify_ssl": "false"},
            "api_local": {
                "base_url":   "http://localhost:1337/v1",
                "api_key":    "test",
                "model":      "test-model",
                "api_format": "openai",
            },
            "architect": {
                "temperature": "0.2", "max_tokens": "512",
                # Force 2 batches (one file each) for this 2-file cluster.
                "max_files_per_review": "1",
            },
            "loop": {"timeout_seconds": "10"},
        })
        reviewer = ClusterReviewer(
            config=cfg,
            base_url="http://localhost:1337/v1",
            api_key="test",
            model="test-model",
            api_format="openai",
            verify_ssl=False,
        )

        # Batch 1/2 is only shown tools/a.py, but the LLM cites tools/b.py —
        # a real file belonging to the SAME cluster, just not to the batch
        # actually under review. Batch 2/2 (tools/b.py) reports nothing.
        cross_batch_payload = json.dumps([{
            "title": "Cross-batch citation",
            "instruction": "Do the fix.",
            "target_files": ["tools/b.py"],
            "acceptance_check": "pytest tests/",
            "cited_location": {
                "file": "tools/b.py",
                "symbol": "b",
                "line_start": 1,
                "line_end": 1,
            },
        }])

        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[cross_batch_payload, "[]"],
        ):
            results = reviewer.review_clusters([cluster], tmp_path, goal="improve code")

        assert len(results) == 1
        assert results[0].cluster == "agents"
        assert "(batch" not in results[0].cluster

    # Guards against a regression in the other direction: a cluster small
    # enough to fit in a single batch never had a suffix to strip, so the
    # fix must be a true no-op for the common (non-batched) case.
    def test_single_batch_cluster_name_unaffected(
        self, reviewer: ClusterReviewer, cluster_and_base
    ) -> None:
        cluster, base_dir = cluster_and_base
        with patch("tools.llm_stream.request_completion", return_value=_good_payload()):
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert len(results) == 1
        assert results[0].cluster == "agents"


# ─────────────────────────────────────────────────────────────────────────────
# CHECKPOINT — AC-C1 through AC-C9
# ─────────────────────────────────────────────────────────────────────────────

class TestCandidateSerialisation:

    # AC-C1: round-trip preserves every field
    def test_serialise_deserialise_round_trip(self) -> None:
        original = _make_candidate("Round-trip task")
        serialised = _serialise_candidates([original])
        restored = _deserialise_candidates(serialised)

        assert len(restored) == 1
        r = restored[0]
        assert r.title            == original.title
        assert r.instruction      == original.instruction
        assert r.target_files     == original.target_files
        assert r.acceptance_check == original.acceptance_check
        assert r.cluster          == original.cluster
        assert r.cited_location.file       == original.cited_location.file
        assert r.cited_location.symbol     == original.cited_location.symbol
        assert r.cited_location.line_start == original.cited_location.line_start
        assert r.cited_location.line_end   == original.cited_location.line_end

    def test_serialise_empty_list(self) -> None:
        assert _serialise_candidates([]) == []

    def test_deserialise_empty_list(self) -> None:
        assert _deserialise_candidates([]) == []

    def test_round_trip_null_symbol_and_lines(self) -> None:
        c = CandidateTask(
            title="Nullable fields",
            instruction=".",
            target_files=["f.py"],
            acceptance_check="true",
            cluster="x",
            cited_location=CitedLocation(file="f.py", symbol=None,
                                         line_start=None, line_end=None),
            raw={},
        )
        restored = _deserialise_candidates(_serialise_candidates([c]))[0]
        assert restored.cited_location.symbol is None
        assert restored.cited_location.line_start is None
        assert restored.cited_location.line_end is None


class TestCheckpointWriteAndRead:

    # AC-C2: checkpoint file written after successful batch
    def test_checkpoint_written_after_successful_batch(
        self, reviewer: ClusterReviewer, cluster_and_base, tmp_path: Path
    ) -> None:
        cluster, base_dir = cluster_and_base
        ckpt = tmp_path / "arch_ckpt.json"

        with patch("tools.llm_stream.request_completion", return_value=_good_payload()):
            reviewer.review_clusters(
                [cluster], base_dir,
                goal="improve code",
                checkpoint_path=ckpt,
            )

        assert ckpt.exists()
        data = json.loads(ckpt.read_text())
        assert len(data) == 1
        key = next(iter(data))
        assert "agents" in key
        assert "improve code" in key

    # AC-C3: checkpoint hit → LLM NOT called a second time
    def test_checkpoint_hit_skips_llm(
        self, reviewer: ClusterReviewer, cluster_and_base, tmp_path: Path
    ) -> None:
        cluster, base_dir = cluster_and_base
        ckpt = tmp_path / "arch_ckpt.json"

        # First run: populate checkpoint
        with patch("tools.llm_stream.request_completion", return_value=_good_payload("Cached task")):
            reviewer.review_clusters([cluster], base_dir, goal="improve code",
                                     checkpoint_path=ckpt)

        # Second run: LLM must NOT be called
        with patch("tools.llm_stream.request_completion") as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code",
                                               checkpoint_path=ckpt)

        mock_llm.assert_not_called()
        assert len(results) == 1
        assert results[0].title == "Cached task"

    # AC-C4: new cluster name → LLM is called (cache miss)
    def test_checkpoint_miss_calls_llm(
        self, reviewer: ClusterReviewer, tmp_path: Path
    ) -> None:
        # Pre-populate checkpoint for "agents" cluster
        ckpt = tmp_path / "arch_ckpt.json"
        ckpt.write_text(
            json.dumps({"agents||improve code": _serialise_candidates([_make_candidate()])}),
            encoding="utf-8",
        )

        # Build a *different* cluster (name = "support")
        src = tmp_path / "support.py"
        src.write_text("# support\n", encoding="utf-8")
        new_cluster = RepoCluster(name="support", patterns=["*.py"], files=["support.py"])

        with patch(
            "tools.llm_stream.request_completion",
            return_value=_good_payload("New cluster result"),
        ) as mock_llm:
            results = reviewer.review_clusters(
                [new_cluster], tmp_path, goal="improve code", checkpoint_path=ckpt
            )

        mock_llm.assert_called_once()
        assert results[0].title == "New cluster result"

    # AC-C5: corrupt checkpoint JSON → fresh LLM call, no crash
    def test_corrupt_checkpoint_falls_back_gracefully(
        self, reviewer: ClusterReviewer, cluster_and_base, tmp_path: Path
    ) -> None:
        cluster, base_dir = cluster_and_base
        ckpt = tmp_path / "arch_ckpt.json"
        ckpt.write_text("THIS IS NOT JSON {{{", encoding="utf-8")

        with patch(
            "tools.llm_stream.request_completion",
            return_value=_good_payload("After corrupt ckpt"),
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code",
                                               checkpoint_path=ckpt)

        mock_llm.assert_called_once()
        assert results[0].title == "After corrupt ckpt"

    # AC-C6: different goals do NOT share cache entries
    def test_different_goals_dont_share_cache(
        self, reviewer: ClusterReviewer, cluster_and_base, tmp_path: Path
    ) -> None:
        cluster, base_dir = cluster_and_base
        ckpt = tmp_path / "arch_ckpt.json"

        # Populate checkpoint with goal A
        with patch("tools.llm_stream.request_completion",
                   return_value=_good_payload("Goal A result")):
            reviewer.review_clusters([cluster], base_dir, goal="goal A",
                                     checkpoint_path=ckpt)

        # Run with goal B — must NOT use the goal-A cache
        with patch("tools.llm_stream.request_completion",
                   return_value=_good_payload("Goal B result")) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="goal B",
                                               checkpoint_path=ckpt)

        mock_llm.assert_called_once()
        assert results[0].title == "Goal B result"

    # AC-C7: checkpoint_path=None → no file created
    def test_no_checkpoint_path_creates_no_file(
        self, reviewer: ClusterReviewer, cluster_and_base, tmp_path: Path
    ) -> None:
        cluster, base_dir = cluster_and_base
        with patch("tools.llm_stream.request_completion", return_value=_good_payload()):
            reviewer.review_clusters([cluster], base_dir, checkpoint_path=None)

        # Nothing written to tmp_path
        written = list(tmp_path.rglob("*.json"))
        assert written == []

    # AC-C8: partial run — one cluster cached, one new — only new calls LLM
    def test_partial_run_only_uncached_clusters_call_llm(
        self, reviewer: ClusterReviewer, tmp_path: Path
    ) -> None:
        # Cluster A: source file
        (tmp_path / "a.py").write_text("# a\n", encoding="utf-8")
        cluster_a = RepoCluster(name="alpha", patterns=["a.py"], files=["a.py"])

        # Cluster B: source file
        (tmp_path / "b.py").write_text("# b\n", encoding="utf-8")
        cluster_b = RepoCluster(name="beta", patterns=["b.py"], files=["b.py"])

        ckpt = tmp_path / "ckpt.json"

        # First run: cache cluster_a only (cluster_b call fails)
        responses = [_good_payload("Alpha result"), Exception("HTTP 500")]
        with (
            patch("tools.llm_stream.request_completion", side_effect=responses),
            patch("time.sleep"),
        ):
            reviewer.review_clusters([cluster_a, cluster_b], tmp_path,
                                     goal="g", checkpoint_path=ckpt)

        data = json.loads(ckpt.read_text())
        assert any("alpha" in k for k in data), "alpha should be checkpointed"
        assert not any("beta" in k for k in data), "beta failed — must NOT be checkpointed"

        # Second run: alpha must be served from cache; beta must call LLM
        with patch(
            "tools.llm_stream.request_completion",
            return_value=_good_payload("Beta result"),
        ) as mock_llm:
            results = reviewer.review_clusters([cluster_a, cluster_b], tmp_path,
                                               goal="g", checkpoint_path=ckpt)

        mock_llm.assert_called_once()   # only beta triggers a real call
        titles = {r.title for r in results}
        assert "Alpha result" in titles
        assert "Beta result" in titles


class TestCheckpointConvenienceFunction:

    # AC-C9: review_clusters convenience function forwards checkpoint_path
    def test_convenience_function_forwards_checkpoint_path(
        self, cfg: configparser.ConfigParser, tmp_path: Path
    ) -> None:
        src = tmp_path / "tools" / "example.py"
        src.parent.mkdir()
        src.write_text("def fn(): pass\n", encoding="utf-8")
        cluster = RepoCluster(name="agents", patterns=["tools/*"],
                              files=["tools/example.py"])
        ckpt = tmp_path / "ckpt.json"

        with patch("tools.llm_stream.request_completion",
                   return_value=_good_payload("Via factory")):
            results = review_clusters(
                [cluster], tmp_path, cfg,
                goal="improve code",
                checkpoint_path=ckpt,
            )

        assert len(results) == 1
        assert ckpt.exists(), "Checkpoint must be written by the convenience function"

        # Second call: must NOT invoke LLM
        with patch("tools.llm_stream.request_completion") as mock_llm:
            results2 = review_clusters(
                [cluster], tmp_path, cfg,
                goal="improve code",
                checkpoint_path=ckpt,
            )

        mock_llm.assert_not_called()
        assert results2[0].title == "Via factory"
