"""HARDEN-8 A/B (306): a recorded base side is re-keyed in the right direction, and a second
entry is not refused for the claims another entry anchored.

`claim_diff.judge_both` reuses a recorded base side claim by claim (CC-16, Part E). Two of the
re-use's own mappings were wrong, and only the shipped order — the claims in the same order as
in the record, every one of them a code claim — hid both:

* **A** the mapping handed to `_rekey_votes` ran this side's position -> the record's, while the
  function's contract is old -> new. The claims reordered, a reused side handed each claim
  another claim's base verdict, and the deltas computed from them were another claim's change.
* **B** the record handed back under `voted` was keyed by `base_index`, which holds *claim*
  indices, with a mapping built from *positions in the code-claim list*. The two coincide only
  while every claim is a code claim; with an entry whose code claims are a subset, the second
  entry died with `ValueError: <position> is not in list` — after the head and the fresh claim
  had been voted and paid for, which `claims_phase` reports as a refusal of that entry.

The voters here are scripted per claim text, so a claim's verdict is the same wherever it sits
in the item list: every case is then judged twice, once re-using a record and once voting
afresh, and the two base sides must agree claim by claim.
"""

from __future__ import annotations

import contextlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import claim_diff as cd  # noqa: E402  (the re-use itself: judge_both, base_side_record, the tally)
from tools.claimcheck.target import Target  # noqa: E402

MODELS = ("a/x", "b/y", "c/z")

#: The three claims of `tests/test_judge_epic_round_claims.py`'s round, and the verdict each
#: gets: TRUE, FALSE, TRUE — two of them alike, so a swap is visible either way.
_CLAIMS = [
    {"id": "c-new", "claim": "`flag` in `pkg/mod.py` returns True.", "expect": "new"},
    {"id": "c-fix", "claim": "`flag` in `pkg/mod.py` returns False.", "expect": "fixed"},
    {"id": "c-still", "claim": "`load_readonly` in `pkg/mod.py` never writes to the directory.",
     "expect": "still"},
]
_VERDICTS = {"`flag` in `pkg/mod.py` returns True.": "TRUE",
             "`flag` in `pkg/mod.py` returns False.": "FALSE",
             "`load_readonly` in `pkg/mod.py` never writes to the directory.": "TRUE"}

#: Part B's set: a world claim, one that anchors in the first entry alone and one in the second.
_SUBSET_CLAIMS = [
    {"id": "c-world", "claim": "The Earth goes around the Sun once a year."},
    {"id": "c-helper", "claim": "`helper()` returns 1."},
    {"id": "c-widget", "claim": "`widget()` returns 1."},
]
_SUBSET_VERDICTS = {"`helper()` returns 1.": "TRUE", "`widget()` returns 1.": "TRUE"}

_MOD_BASE = """\
def flag():
    return False


def load_readonly(target):
    return list(target)
"""

_HELPER = _MOD_BASE + "\n\ndef helper():\n    return 1\n"
_WIDGET = _MOD_BASE + "\n\ndef widget():\n    return 1\n"


def _vote(items, side):
    verdicts = dict(_VERDICTS, **_SUBSET_VERDICTS)
    rows = []
    for model in MODELS:
        rows.append({"model": model, "run": 0,
                     "votes": {pos: verdicts.get(text, "UNSURE")
                               for pos, (text, _pack) in enumerate(items)},
                     "accepted": {}})
    return rows


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture(scope="module")
def round_repo(tmp_path_factory):
    """A base commit and two entries: `helper` defines `helper()`, `widget` defines `widget()`.

    The two entries are the whole point: a symbol-only claim anchors in one entry and not in the
    other, so the same three claims give one entry two code claims and the other one.
    """
    root = tmp_path_factory.mktemp("harden8")
    repo = root / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "mod.py").write_text(_MOD_BASE, encoding="utf-8")
    (repo / "tools" / "auto").mkdir(parents=True)
    (repo / "tools" / "auto" / "collect_bridge.py").write_text(
        "class CollectBridge:\n    def _shrink(self, text):\n        return text[:10]\n",
        encoding="utf-8")
    (repo / "conftest.py").write_text("", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "h8@example.com")
    _git(repo, "config", "user.name", "H8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    def entry(name, extra):
        wt = root / f"wt-{name}"
        _git(repo, "worktree", "add", "-q", str(wt), base)
        _git(wt, "checkout", "-q", "-b", f"h8/{name}")
        (wt / "pkg" / "extra.py").write_text(extra, encoding="utf-8")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-q", "-m", f"{name}: the change")
        return wt

    return {"base": base, "repo": repo, "scratch": root / "scratch",
            "helper": entry("helper", _HELPER), "widget": entry("widget", _WIDGET)}


@pytest.fixture()
def both_sides(round_repo):
    """`(base, head)` of the `widget` entry, opened and closed by the test."""
    with contextlib.ExitStack() as stack:
        scratch = str(round_repo["scratch"])
        base = stack.enter_context(Target.open(str(round_repo["repo"]), round_repo["base"],
                                               scratch=scratch))
        head = stack.enter_context(Target.open(str(round_repo["widget"]), "HEAD", scratch=scratch))
        yield base, head


#: The settings a record states and this run has to state back (CC-16, Part B): `collect` and
#: `budget` are `judge_both`'s own parameters, so they are named rather than folded into a dict.
SETTINGS = dict(voters=MODELS, runs=1, collect=True, seed=1, fixed_prompt=False, models={},
                accept="all")


def _judge(claims, base, head, *, sides=None, record=None, budget=None):
    return cd.judge_both(claims, base, head, _vote, budget or cd.PackBudget(),
                         base_side=record, sides=sides, **SETTINGS)


def _record(claims, base, got):
    return cd.base_side_record(claims, base, MODELS, 1, got, collect=True,
                               budget=cd.PackBudget(), seed=1, fixed_prompt=False, models={})


def _base_verdicts(got):
    """The base side's own verdict per claim — `None` for a claim judged at head alone."""
    return [None if row is None else row["verdict"] for row in got["base"]["rows"]]


def _reorder(claims, order):
    return [claims[i] for i in order]


# ------------------------------------------------------------------ Part A

def test_a_reordered_claims_keep_their_own_base_verdict(both_sides):
    """The reproduction: `[still, new, fix]` judged against a record of `[new, fix, still]`.

    A reused side must give, for the same claims in any order, the same base verdicts as a fresh
    vote of those claims — not the verdict of the claim that sat at that position in the record.
    """
    base, head = both_sides
    record_claims = _CLAIMS
    fresh = _judge(record_claims, base, head)
    record = _record(record_claims, base, fresh)

    for order in ([2, 0, 1], [1, 2, 0], [0, 2, 1], [2, 1, 0]):
        reordered = _reorder(record_claims, order)
        sides = cd.prepare_sides(reordered, base, head, cd.PackBudget())
        reused = _judge(reordered, base, head, sides=sides, record=record)
        wanted = _judge(reordered, base, head, sides=sides)
        assert _base_verdicts(reused) == _base_verdicts(wanted), order
        # and the changes that follow are the fresh vote's, not another claim's
        assert [d.change for d in reused["deltas"]] == [d.change for d in wanted["deltas"]], order


def test_a_a_subset_of_the_recorded_claims_keeps_its_verdict(both_sides):
    """A record of all three, judged over one: that claim's verdict is its own."""
    base, head = both_sides
    record = _record(_CLAIMS, base, _judge(_CLAIMS, base, head))
    for one in _CLAIMS:
        sides = cd.prepare_sides([one], base, head, cd.PackBudget())
        reused = _judge([one], base, head, sides=sides, record=record)
        wanted = _judge([one], base, head, sides=sides)
        assert _base_verdicts(reused) == _base_verdicts(wanted) == ["TRUE" if "returns False" not in one["claim"] else "FALSE"]


def test_a_a_claim_the_record_does_not_hold_is_voted_here(both_sides):
    """A superset: the claims the record holds come from it, the one it does not is voted here,
    and every base verdict is the fresh vote's."""
    base, head = both_sides
    record_claims = _CLAIMS[:1]
    record = _record(record_claims, base, _judge(record_claims, base, head))
    sides = cd.prepare_sides(_CLAIMS, base, head, cd.PackBudget())
    reused = _judge(_CLAIMS, base, head, sides=sides, record=record)
    wanted = _judge(_CLAIMS, base, head, sides=sides)
    assert _base_verdicts(reused) == _base_verdicts(wanted) == ["TRUE", "FALSE", "TRUE"]
    assert [d.change for d in reused["deltas"]] == [d.change for d in wanted["deltas"]]


def test_a_the_shipped_order_is_unchanged(both_sides):
    """The one case the earlier tests put under test — the claims in the record's own order —
    still reuses every claim and votes none of them again."""
    base, head = both_sides
    sides = cd.prepare_sides(_CLAIMS, base, head, cd.PackBudget())
    record = _record(_CLAIMS, base, _judge(_CLAIMS, base, head, sides=sides))
    reused = _judge(_CLAIMS, base, head, sides=sides, record=record)
    wanted = _judge(_CLAIMS, base, head, sides=sides)
    assert _base_verdicts(reused) == _base_verdicts(wanted)
    assert [d.change for d in reused["deltas"]] == [d.change for d in wanted["deltas"]]


# ------------------------------------------------------------------ Part B

def test_b_a_second_entry_is_not_refused_for_the_claims_another_anchored(round_repo):
    """The reproduction: entry `helper` judges the middle claim alone, entry `widget` the last
    one alone. The second used to die in the `voted` merge, after its votes were paid for."""
    with contextlib.ExitStack() as stack:
        scratch = str(round_repo["scratch"])
        base = stack.enter_context(Target.open(str(round_repo["repo"]), round_repo["base"],
                                               scratch=scratch))
        helper = stack.enter_context(Target.open(str(round_repo["helper"]), "HEAD", scratch=scratch))
        widget = stack.enter_context(Target.open(str(round_repo["widget"]), "HEAD", scratch=scratch))
        sides_helper = cd.prepare_sides(_SUBSET_CLAIMS, base, helper, cd.PackBudget())
        sides_widget = cd.prepare_sides(_SUBSET_CLAIMS, base, widget, cd.PackBudget())
        # one entry judges the middle claim, the other the last: the world flags say so
        assert sides_helper["world"] == [True, False, True]
        assert sides_widget["world"] == [True, True, False]
        first = _judge(_SUBSET_CLAIMS, base, helper, sides=sides_helper)
        record = _record(_SUBSET_CLAIMS, base, first)
        second = _judge(_SUBSET_CLAIMS, base, widget, sides=sides_widget, record=record)
        alone = _judge(_SUBSET_CLAIMS, base, widget, sides=sides_widget)
        assert _base_verdicts(second) == _base_verdicts(alone)
        assert [d.change for d in second["deltas"]] == [d.change for d in alone["deltas"]]
        # the claim the record did not hold was voted here, and the record keeps both claims
        assert len(record["index"]) == 1
        assert len(second["base"]["index"]) == 2


def test_b_the_round_judged_entry_by_entry_is_the_round_judged_alone(round_repo):
    """Each entry, judged alone, and the round judged in either order: the same table."""
    with contextlib.ExitStack() as stack:
        scratch = str(round_repo["scratch"])
        base = stack.enter_context(Target.open(str(round_repo["repo"]), round_repo["base"],
                                               scratch=scratch))
        heads = {name: stack.enter_context(Target.open(str(round_repo[name]), "HEAD", scratch=scratch))
                 for name in ("helper", "widget")}
        alone = {}
        for name, head in heads.items():
            sides = cd.prepare_sides(_SUBSET_CLAIMS, base, head, cd.PackBudget())
            got = _judge(_SUBSET_CLAIMS, base, head, sides=sides)
            alone[name] = [d.change for d in got["deltas"]]
        for order in (("helper", "widget"), ("widget", "helper")):
            record = None
            for name in order:
                sides = cd.prepare_sides(_SUBSET_CLAIMS, base, heads[name], cd.PackBudget())
                got = _judge(_SUBSET_CLAIMS, base, heads[name], sides=sides, record=record)
                record = _record(_SUBSET_CLAIMS, base, got)
                assert [d.change for d in got["deltas"]] == alone[name], (order, name)
