"""tests/test_architect_concurrency.py — AUTO-335: Architect parallel request concurrency.

Covers the acceptance criteria from ticket 335:

  AC-1  Setting absent / malformed / 1 → serial, order preserved.
  AC-2  Value > 1 → at least two requests overlap, peak never exceeds limit.
  AC-3  Out-of-order completion → results in cluster/batch order, dedup works.
  AC-4  One batch failure → siblings kept, failed not checkpointed, retried later.
  AC-5  Concurrent successful batches → all survive checkpoint, restored next run.
  AC-6  on_cluster_done fires once per cluster (empty, checkpointed, partly failed).
  AC-7  Multi-cluster with probing → concurrency limited to 1, no shared probe state.

All LLM calls are patched; no network I/O.
"""

from __future__ import annotations

import configparser
import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.auto.architect import (
    CandidateTask,
    CitedLocation,
    ClusterReviewer,
)
from tools.auto.repo_ingest import RepoCluster


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture()
def base_cfg() -> configparser.ConfigParser:
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


def _reviewer(cfg: configparser.ConfigParser, task_mode: str = "code") -> ClusterReviewer:
    return ClusterReviewer(
        config=cfg,
        base_url="http://localhost:1337/v1",
        api_key="test",
        model="test-model",
        api_format="openai",
        verify_ssl=False,
        task_mode=task_mode,
    )


def _make_cluster(name: str, files: list[str], tmp_path: Path) -> RepoCluster:
    """Create real files on disk so the batch fingerprint can stat them."""
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {f}\n", encoding="utf-8")
    return RepoCluster(name=name, patterns=["*"], files=files)


def _make_candidate(title: str, cluster: str, file: str = "a.py") -> CandidateTask:
    return CandidateTask(
        title=title,
        instruction="Do the thing.",
        target_files=[file],
        acceptance_check="pytest",
        cluster=cluster,
        cited_location=CitedLocation(file=file, symbol="fn", line_start=1, line_end=1),
        raw={},
    )


# ── Fake _review_one_cluster with concurrency tracking ────────────────────

class _FakeReview:
    """Stand-in for ``ClusterReviewer._review_one_cluster``.

    Tracks active/peak concurrency, supports per-(sub)cluster results /
    failures / delays, and a go-event for synchronising the start of calls.
    The sub-cluster name (e.g. ``"alpha (batch 1/2)"``) is used as the key,
    so individual batches can be controlled independently.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.call_order: list[str] = []
        self.results: dict[str, list[CandidateTask]] = {}
        self.failures: set[str] = set()
        self.delays: dict[str, float] = {}
        self.go_event = threading.Event()
        self.started_count = 0

    def __call__(self, cluster: RepoCluster, base_dir: Path, goal: str,
                 all_files: "list[str] | None" = None) -> "list[CandidateTask] | None":
        name = cluster.name
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.call_order.append(name)
            self.started_count += 1

        delay = self.delays.get(name, 0.0)
        if delay:
            time.sleep(delay)

        self.go_event.wait(timeout=10)  # a regression fails the test, it does not hang the suite

        with self.lock:
            self.active -= 1

        if name in self.failures:
            return None
        return list(self.results.get(name, []))

    def release(self) -> None:
        self.go_event.set()


# ── AC-1: absent / malformed / 1 → serial ─────────────────────────────────

class TestSerialByDefault:
    """With max_parallel_requests absent, malformed, or 1, requests run one
    at a time and candidate output matches input order."""

    @pytest.mark.parametrize("config_value", [None, "abc", "0", "-3", "1"])
    def test_serial_when_setting_absent_malformed_or_one(
        self, base_cfg: configparser.ConfigParser, tmp_path: Path,
        config_value: "str | None",
    ) -> None:
        cluster = _make_cluster("alpha", ["a.py", "b.py"], tmp_path)
        fake = _FakeReview()
        fake.results["alpha"] = [_make_candidate("Task A", "alpha")]

        if config_value is None:
            base_cfg.remove_option("architect", "max_parallel_requests")
        else:
            base_cfg.set("architect", "max_parallel_requests", config_value)

        reviewer = _reviewer(base_cfg)
        assert reviewer._max_parallel_requests == 1

        with patch.object(ClusterReviewer, "_review_one_cluster", new=fake):
            fake.release()
            results = reviewer.review_clusters([cluster], tmp_path, goal="improve")

        assert fake.peak == 1, f"expected serial (peak=1), got peak={fake.peak}"
        assert [r.title for r in results] == ["Task A"]
        assert results[0].cluster == "alpha"

    def test_malformed_setting_defaults_to_one(
        self, base_cfg: configparser.ConfigParser, tmp_path: Path,
    ) -> None:
        base_cfg.set("architect", "max_parallel_requests", "abc")
        reviewer = _reviewer(base_cfg)
        assert reviewer._max_parallel_requests == 1


# ── AC-2: value > 1 → overlap, peak never exceeds limit ───────────────────

class TestParallelOverlap:
    """With max_parallel_requests > 1, requests overlap but peak is bounded."""

    def _run_overlap(self, base_cfg, tmp_path, max_parallel, n_clusters,
                     expect_min_peak, expect_max_peak):
        base_cfg.set("architect", "max_parallel_requests", str(max_parallel))
        clusters = [
            _make_cluster(f"c{i}", [f"c{i}.py"], tmp_path) for i in range(n_clusters)
        ]
        fake = _FakeReview()
        for c in clusters:
            fake.results[c.name] = [_make_candidate(f"Task {c.name}", c.name)]

        reviewer = _reviewer(base_cfg)
        result_holder: dict = {}

        with patch.object(ClusterReviewer, "_review_one_cluster", new=fake):
            fake.go_event.clear()
            t = threading.Thread(
                target=lambda: result_holder.__setitem__(
                    "results",
                    reviewer.review_clusters(clusters, tmp_path, goal="improve"),
                )
            )
            t.start()

            # Wait for at least expect_min_peak tasks to pile up in the pool.
            deadline = time.time() + 10
            while fake.started_count < expect_min_peak and time.time() < deadline:
                time.sleep(0.005)

            assert fake.started_count >= expect_min_peak, (
                f"expected >= {expect_min_peak} concurrent starts, got {fake.started_count}"
            )

            fake.release()
            t.join(timeout=10)
            assert not t.is_alive(), "review_clusters did not finish after release"

        results = result_holder["results"]
        assert fake.peak >= expect_min_peak, f"expected peak >= {expect_min_peak}, got {fake.peak}"
        assert fake.peak <= expect_max_peak, f"expected peak <= {expect_max_peak}, got {fake.peak}"
        assert len(results) == n_clusters
        return results

    def test_overlap_and_peak_bounded(
        self, base_cfg: configparser.ConfigParser, tmp_path: Path,
    ) -> None:
        """4 clusters, limit 3 → at least 2 overlap, peak never above 3."""
        self._run_overlap(base_cfg, tmp_path, max_parallel=3, n_clusters=4,
                          expect_min_peak=2, expect_max_peak=3)

    def test_peak_equals_configured_limit(
        self, base_cfg: configparser.ConfigParser, tmp_path: Path,
    ) -> None:
        """5 clusters, limit 2 → peak is exactly 2."""
        self._run_overlap(base_cfg, tmp_path, max_parallel=2, n_clusters=5,
                          expect_min_peak=2, expect_max_peak=2)


# ── AC-3: out-of-order completion → order preserved ───────────────────────

class TestOutOfOrder:
    """Requests completing out of order still yield input-order results."""

    def test_results_in_cluster_and_batch_order(
        self, base_cfg: configparser.ConfigParser, tmp_path: Path,
    ) -> None:
        base_cfg.set("architect", "max_parallel_requests", "4")
        base_cfg.set("architect", "max_files_per_review", "2")
        # Cluster "alpha": 4 files → 2 batches (batch 1: a1,a2 ; batch 2: a3,a4).
        # Cluster "beta": 1 file → 1 batch.
        alpha = _make_cluster("alpha", ["a1.py", "a2.py", "a3.py", "a4.py"], tmp_path)
        beta = _make_cluster("beta", ["b.py"], tmp_path)

        fake = _FakeReview()
        fake.results["alpha (batch 1/2)"] = [_make_candidate("Alpha batch 1", "alpha", "a1.py")]
        fake.results["alpha (batch 2/2)"] = [_make_candidate("Alpha batch 2", "alpha", "a3.py")]
        fake.results["beta"] = [_make_candidate("Beta", "beta", "b.py")]
        # Make batch 1 slower so batch 2 completes first — out of order.
        fake.delays["alpha (batch 1/2)"] = 0.05

        reviewer = _reviewer(base_cfg)
        with patch.object(ClusterReviewer, "_review_one_cluster", new=fake):
            fake.release()
            results = reviewer.review_clusters([alpha, beta], tmp_path, goal="improve")

        titles = [r.title for r in results]
        assert titles == ["Alpha batch 1", "Alpha batch 2", "Beta"], titles
        assert all(
            r.cluster == ("alpha" if "Alpha" in r.title else "beta")
            for r in results
        )


# ── AC-4: one batch failure → siblings kept, failed not checkpointed ──────

class TestFailureIsolation:
    """A failed batch does not discard successful siblings and stays
    retryable on the next invocation."""

    def test_failed_batch_isolated_siblings_kept(
        self, base_cfg: configparser.ConfigParser, tmp_path: Path,
    ) -> None:
        base_cfg.set("architect", "max_parallel_requests", "3")
        base_cfg.set("architect", "max_files_per_review", "1")
        alpha = _make_cluster("alpha", ["a1.py", "a2.py"], tmp_path)
        beta = _make_cluster("beta", ["b.py"], tmp_path)
        ckpt = tmp_path / "arch_ckpt.json"

        fake = _FakeReview()
        # alpha batch 1 succeeds; alpha batch 2 fails; beta succeeds.
        fake.results["alpha (batch 1/2)"] = [_make_candidate("Alpha ok", "alpha", "a1.py")]
        fake.failures.add("alpha (batch 2/2)")
        fake.results["beta"] = [_make_candidate("Beta ok", "beta", "b.py")]

        reviewer = _reviewer(base_cfg)
        with patch.object(ClusterReviewer, "_review_one_cluster", new=fake):
            fake.release()
            results = reviewer.review_clusters([alpha, beta], tmp_path, goal="g",
                                               checkpoint_path=ckpt)

        # beta's result survives; alpha batch 1 survives; alpha batch 2 lost.
        titles = [r.title for r in results]
        assert titles == ["Alpha ok", "Beta ok"], titles

        # Failed alpha batch 2 is NOT in the checkpoint (retryable next run).
        data = json.loads(ckpt.read_text())
        assert not any("batch 2/2" in k for k in data), "failed batch must not be checkpointed"
        assert any("batch 1/2" in k for k in data), "successful batch must be checkpointed"
        assert any("beta" in k for k in data), "beta success must be checkpointed"

        # Second run: alpha batch 1 from checkpoint (no call), alpha batch 2 retried,
        # beta from checkpoint (no call).
        calls: list[str] = []

        def counting_fake(cluster: RepoCluster, base_dir: Path, goal: str,
                          all_files: "list[str] | None" = None) -> list:
            calls.append(cluster.name)
            if cluster.name == "alpha (batch 2/2)":
                return None
            return []

        mock = MagicMock()
        mock.side_effect = counting_fake
        with patch.object(ClusterReviewer, "_review_one_cluster", mock):
            results2 = reviewer.review_clusters([alpha, beta], tmp_path, goal="g",
                                                checkpoint_path=ckpt)

        titles2 = [r.title for r in results2]
        assert titles2 == ["Alpha ok", "Beta ok"], titles2
        # Only the previously-failed batch is re-called.
        assert calls == ["alpha (batch 2/2)"], calls


# ── AC-5: concurrent successful batches all survive checkpoint ────────────

class TestCheckpointSurvival:
    """Concurrent successful batches are all checkpointed and restored on the
    next invocation without another model call."""

    def test_all_batches_checkpointed_and_restored(
        self, base_cfg: configparser.ConfigParser, tmp_path: Path,
    ) -> None:
        base_cfg.set("architect", "max_parallel_requests", "4")
        clusters = [
            _make_cluster(f"c{i}", [f"c{i}.py"], tmp_path) for i in range(3)
        ]
        ckpt = tmp_path / "arch_ckpt.json"

        fake = _FakeReview()
        for c in clusters:
            fake.results[c.name] = [_make_candidate(f"Task {c.name}", c.name)]

        reviewer = _reviewer(base_cfg)
        with patch.object(ClusterReviewer, "_review_one_cluster", new=fake):
            fake.release()
            results = reviewer.review_clusters(clusters, tmp_path, goal="g",
                                               checkpoint_path=ckpt)

        assert len(results) == 3
        data = json.loads(ckpt.read_text())
        assert len(data) == 3, f"expected 3 checkpoint keys, got {len(data)}"

        # Second run: everything from checkpoint, no LLM calls.
        call_count = [0]

        def counting_fake(cluster: RepoCluster, base_dir: Path, goal: str,
                          all_files: "list[str] | None" = None) -> list:
            call_count[0] += 1
            return []

        mock = MagicMock()
        mock.side_effect = counting_fake
        with patch.object(ClusterReviewer, "_review_one_cluster", mock):
            results2 = reviewer.review_clusters(clusters, tmp_path, goal="g",
                                                checkpoint_path=ckpt)

        assert call_count[0] == 0, f"expected 0 LLM calls on cache hit, got {call_count[0]}"
        assert [r.title for r in results2] == [r.title for r in results]


# ── AC-6: on_cluster_done fires once per cluster ─────────────────────────

class TestOnClusterDone:
    """on_cluster_done fires exactly once per cluster after all its batches
    have completed or failed, including empty, checkpointed, and partly failed
    clusters."""

    def test_callback_once_per_cluster(
        self, base_cfg: configparser.ConfigParser, tmp_path: Path,
    ) -> None:
        base_cfg.set("architect", "max_parallel_requests", "3")
        empty = RepoCluster(name="empty", patterns=["*"], files=[])
        alpha = _make_cluster("alpha", ["a.py"], tmp_path)
        beta = _make_cluster("beta", ["b.py"], tmp_path)
        ckpt = tmp_path / "arch_ckpt.json"

        calls: list[str] = []

        def on_done():
            calls.append("done")

        fake = _FakeReview()
        fake.results["alpha"] = [_make_candidate("Alpha", "alpha")]
        fake.failures.add("beta")  # beta's only batch fails

        reviewer = _reviewer(base_cfg)
        # First run: empty (callback), alpha (callback), beta fails (callback).
        with patch.object(ClusterReviewer, "_review_one_cluster", new=fake):
            fake.release()
            reviewer.review_clusters([empty, alpha, beta], tmp_path, goal="g",
                                     checkpoint_path=ckpt, on_cluster_done=on_done)

        # empty: 1, alpha: 1, beta: 1 → 3 total
        assert calls.count("done") == 3, f"expected 3 callbacks, got {len(calls)}"

        # Second run: alpha checkpointed (callback), beta retried (callback),
        # empty (callback).
        calls.clear()

        def counting_fake(cluster: RepoCluster, base_dir: Path, goal: str,
                          all_files: "list[str] | None" = None) -> list:
            if cluster.name == "beta":
                return None  # beta fails again
            return []

        mock = MagicMock()
        mock.side_effect = counting_fake
        with patch.object(ClusterReviewer, "_review_one_cluster", mock):
            reviewer.review_clusters([empty, alpha, beta], tmp_path, goal="g",
                                     checkpoint_path=ckpt, on_cluster_done=on_done)

        assert calls.count("done") == 3, f"expected 3 callbacks on 2nd run, got {len(calls)}"


# ── AC-7: probing enabled → concurrency limited to 1 ─────────────────────

class TestProbeSafety:
    """With probing enabled and a usable collect probe, concurrency is
    limited to 1 (serial) and probe state is not shared across requests."""

    def test_probe_usable_limits_concurrency_to_one(
        self, base_cfg: configparser.ConfigParser, tmp_path: Path,
    ) -> None:
        base_cfg.set("architect", "max_parallel_requests", "4")
        base_cfg.set("architect", "probe_enabled", "true")
        clusters = [
            _make_cluster(f"c{i}", [f"c{i}.py"], tmp_path) for i in range(3)
        ]
        fake = _FakeReview()
        for c in clusters:
            fake.results[c.name] = [_make_candidate(f"Task {c.name}", c.name)]

        mock_probe = MagicMock()
        mock_probe.usable = True

        reviewer = _reviewer(base_cfg)
        with patch.object(ClusterReviewer, "_review_one_cluster", new=fake), \
             patch.object(reviewer, "_get_probe", return_value=mock_probe):
            fake.release()
            reviewer.review_clusters(clusters, tmp_path, goal="g")

        assert fake.peak == 1, (
            f"expected serial (peak=1) with usable probe, got peak={fake.peak}"
        )

    def test_no_probe_this_run_allows_parallel(
        self, base_cfg: configparser.ConfigParser, tmp_path: Path,
    ) -> None:
        base_cfg.set("architect", "max_parallel_requests", "3")
        base_cfg.set("architect", "probe_enabled", "true")
        clusters = [
            _make_cluster(f"c{i}", [f"c{i}.py"], tmp_path) for i in range(3)
        ]
        fake = _FakeReview()
        for c in clusters:
            fake.results[c.name] = [_make_candidate(f"Task {c.name}", c.name)]

        reviewer = _reviewer(base_cfg)
        result_holder: dict = {}

        with patch.object(ClusterReviewer, "_review_one_cluster", new=fake), \
             patch.object(reviewer, "_get_probe", return_value=None):
            fake.go_event.clear()
            t = threading.Thread(
                target=lambda: result_holder.__setitem__(
                    "results",
                    reviewer.review_clusters(clusters, tmp_path, goal="g"),
                )
            )
            t.start()

            deadline = time.time() + 10
            while fake.started_count < 2 and time.time() < deadline:
                time.sleep(0.005)

            assert fake.started_count >= 2, (
                f"expected >= 2 concurrent starts when probe unusable, got {fake.started_count}"
            )

            fake.release()
            t.join(timeout=10)
            assert not t.is_alive(), "review_clusters did not finish after release"

        results = result_holder["results"]
        assert fake.peak >= 2, f"expected overlap when probe unusable, got peak={fake.peak}"
        assert fake.peak <= 3, f"expected peak <= 3, got peak={fake.peak}"
        assert len(results) == 3


# ── Shipped profiles ────────────────────────────────────────────────────────

# An operator's own agents*.local.ini is git-ignored, not shipped: it is not held to the shipped ceiling.
_PROFILES = sorted(
    p for p in Path(__file__).resolve().parent.parent.glob("agents*.ini")
    if not p.name.endswith(".local.ini")
)


def test_the_shipped_profiles_are_found():
    assert len(_PROFILES) >= 9, [p.name for p in _PROFILES]


@pytest.mark.parametrize("profile", _PROFILES, ids=[p.name for p in _PROFILES])
def test_a_shipped_profile_plans_with_three_parallel_requests(profile: Path):
    """Every profile ships the same ceiling, and its probe does not turn it back to serial."""
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"), interpolation=None)
    cfg.read(profile, encoding="utf-8")
    assert _reviewer(cfg)._max_parallel_requests == 3
    assert cfg.getboolean("architect", "probe_enabled") is False
