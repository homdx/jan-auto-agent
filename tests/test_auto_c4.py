"""tests/test_auto_c4.py — AUTO-C4: outer round loop + feedback files.

ACs:
  * Passes in round 1 when the inner loop passes immediately.
  * Passes in a later round; a failed round writes ONE feedback_round_<n>.md.
  * Exhaustion after max_rounds → task BLOCKED, exhausted=True.
  * Fresh context: the prior_feedback handed to the inner loop on round N is
    exactly the N-1 compact per-round summaries — NOT the 5*(N-1) attempt logs.
    (This is the bounded-context guarantee.)
  * round/attempt counters are persisted to the StateStore.
  * Resume: pre-existing feedback files make the loop continue from the next
    round instead of redoing completed rounds.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.outer_loop import OuterLoop, make_outer_loop
from tools.auto.state import (
    StateStore, make_task, STATUS_DONE, STATUS_BLOCKED,
)


# ── fake inner loop ──────────────────────────────────────────────────────────

@dataclass
class FakeInnerResult:
    task_id: str = "AUTO-T1"
    passed: bool = False
    attempts_used: int = 5
    records: list = field(default_factory=list)
    last_feedback: str = "still broken"


class FakeInnerLoop:
    """Returns scripted results; records the prior_feedback it receives each call."""
    def __init__(self, results):
        self._results = list(results)
        self.seen_prior = []        # list of prior_feedback lists, one per round
    def run_task(self, task, base_dir, prior_feedback=None, prior_implementations=None):
        self.seen_prior.append(list(prior_feedback or []))
        return self._results.pop(0) if self._results else FakeInnerResult()


def _state(tmp_path) -> StateStore:
    st = StateStore(tmp_path / ".agent")
    st.initialise("goal", tmp_path)
    st.upsert_task(make_task(id="AUTO-T1", title="t", instruction="x",
                             target_files=["f.py"]))
    return st


TASK = {"id": "AUTO-T1", "title": "t", "instruction": "x",
        "target_files": ["f.py"], "acceptance_check": "pytest -q"}


# ── tests ────────────────────────────────────────────────────────────────────

class TestPass:
    def test_pass_round_one(self, tmp_path):
        st = _state(tmp_path)
        inner = FakeInnerLoop([FakeInnerResult(passed=True, attempts_used=1)])
        r = OuterLoop(inner, st, max_rounds=10).run_task(TASK, tmp_path)
        assert r.passed and r.rounds_used == 1 and not r.exhausted
        assert st.get_task("AUTO-T1")["status"] == STATUS_DONE
        # no feedback files written on a clean pass
        assert r.feedback_files == []

    def test_pass_round_three_writes_two_feedback_files(self, tmp_path):
        st = _state(tmp_path)
        inner = FakeInnerLoop([
            FakeInnerResult(passed=False, last_feedback="err A"),
            FakeInnerResult(passed=False, last_feedback="err B"),
            FakeInnerResult(passed=True, attempts_used=2),
        ])
        r = OuterLoop(inner, st, max_rounds=10).run_task(TASK, tmp_path)
        assert r.passed and r.rounds_used == 3
        # two failed rounds → two feedback files
        files = sorted((st.task_dir("AUTO-T1")).glob("feedback_round_*.md"))
        assert [p.name for p in files] == ["feedback_round_1.md", "feedback_round_2.md"]
        assert "err A" in files[0].read_text()


class TestExhaustion:
    def test_exhausted_marks_blocked(self, tmp_path):
        st = _state(tmp_path)
        inner = FakeInnerLoop([FakeInnerResult(passed=False)] * 10)
        r = OuterLoop(inner, st, max_rounds=10).run_task(TASK, tmp_path)
        assert r.passed is False and r.exhausted and r.rounds_used == 10
        assert st.get_task("AUTO-T1")["status"] == STATUS_BLOCKED
        # one feedback file per failed round
        files = list((st.task_dir("AUTO-T1")).glob("feedback_round_*.md"))
        assert len(files) == 10

    def test_respects_max_rounds(self, tmp_path):
        st = _state(tmp_path)
        inner = FakeInnerLoop([FakeInnerResult(passed=False)] * 9)
        r = OuterLoop(inner, st, max_rounds=3).run_task(TASK, tmp_path)
        assert r.rounds_used == 3 and r.exhausted
        assert len(inner.seen_prior) == 3

    def test_knowledge_concatenates_feedback(self, tmp_path):
        st = _state(tmp_path)
        inner = FakeInnerLoop([
            FakeInnerResult(passed=False, last_feedback="alpha"),
            FakeInnerResult(passed=False, last_feedback="beta"),
        ])
        r = OuterLoop(inner, st, max_rounds=2).run_task(TASK, tmp_path)
        kn = r.knowledge()
        assert "alpha" in kn and "beta" in kn


class TestFreshContext:
    def test_prior_feedback_is_one_summary_per_round(self, tmp_path):
        st = _state(tmp_path)
        # 4 failed rounds then we inspect what each round was seeded with
        inner = FakeInnerLoop([FakeInnerResult(passed=False,
                                               last_feedback=f"issue {i}") for i in range(4)])
        OuterLoop(inner, st, max_rounds=4).run_task(TASK, tmp_path)
        # round 1 seeded with 0, round 2 with 1, round 3 with 2, round 4 with 3
        assert [len(p) for p in inner.seen_prior] == [0, 1, 2, 3]
        # bounded-context guarantee: grows by ONE compact entry per round,
        # never by the number of attempts (5) per round.
        assert len(inner.seen_prior[-1]) == 3          # not 15

    def test_each_round_sees_only_compact_summaries(self, tmp_path):
        st = _state(tmp_path)
        inner = FakeInnerLoop([FakeInnerResult(passed=False, attempts_used=5,
                                               last_feedback="X" * 5000)] * 2)
        OuterLoop(inner, st, max_rounds=2).run_task(TASK, tmp_path)
        # round 2's single prior entry is the compact (truncated) round-1 file,
        # not the full 5000-char attempt log.
        seed = inner.seen_prior[1][0]
        assert len(seed) < 2000
        assert "Round 1" in seed


class TestPersistenceAndResume:
    def test_counters_persisted(self, tmp_path):
        st = _state(tmp_path)
        inner = FakeInnerLoop([
            FakeInnerResult(passed=False, attempts_used=5),
            FakeInnerResult(passed=True, attempts_used=3),
        ])
        OuterLoop(inner, st, max_rounds=10).run_task(TASK, tmp_path)
        t = st.get_task("AUTO-T1")
        assert t["round"] == 2            # two rounds ran
        assert t["attempt"] == 8          # 5 + 3 attempts accumulated

    def test_resume_skips_completed_rounds(self, tmp_path):
        st = _state(tmp_path)
        # simulate a prior session that completed rounds 1 and 2
        st.write_task_file("AUTO-T1", "feedback_round_1.md", "# Round 1 — task AUTO-T1\n")
        st.write_task_file("AUTO-T1", "feedback_round_2.md", "# Round 2 — task AUTO-T1\n")
        inner = FakeInnerLoop([FakeInnerResult(passed=True, attempts_used=1)])
        r = OuterLoop(inner, st, max_rounds=10).run_task(TASK, tmp_path)
        # only ONE new round should have run (round 3), and it passed
        assert len(inner.seen_prior) == 1
        assert len(inner.seen_prior[0]) == 2     # seeded by the 2 prior round files
        assert r.passed and r.rounds_used == 3

    def test_resume_already_exhausted_is_blocked(self, tmp_path):
        st = _state(tmp_path)
        for i in range(1, 4):
            st.write_task_file("AUTO-T1", f"feedback_round_{i}.md", f"# Round {i}\n")
        inner = FakeInnerLoop([FakeInnerResult(passed=True)])   # should never be called
        r = OuterLoop(inner, st, max_rounds=3).run_task(TASK, tmp_path)
        assert r.exhausted and not r.passed
        assert inner.seen_prior == []        # no rounds run
        assert st.get_task("AUTO-T1")["status"] == STATUS_BLOCKED


class TestFactory:
    def test_make_outer_loop_injected_inner(self, tmp_path):
        import configparser
        st = _state(tmp_path)
        cfg = configparser.ConfigParser()
        cfg["auto"] = {"max_rounds_per_task": "7"}
        inner = FakeInnerLoop([FakeInnerResult(passed=True)])
        outer = make_outer_loop(cfg, tmp_path, st, inner_loop=inner)
        assert outer.max_rounds == 7
        assert outer.run_task(TASK, tmp_path).passed


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
