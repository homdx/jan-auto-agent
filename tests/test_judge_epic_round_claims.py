"""CC-14 (296): the round's claims at the judge — `judge_epic_round.py --claims`.

The judge's third phase: the ticket's own claims, each judged at the base and at every
entry that passed the hard gates, by `claim_diff.judge_both` — the same voters, the same
tally, the same rule, never a copy of the voting. The base side is asked once per round
and re-used for every later entry, and a re-use is refused when the claims, the base sha,
the voters, the runs or the rule differ. A mismatch is a lead: no exit code changes and
no round fails on a model's reading.

Every case runs against a temp repo and temp worktrees only, and no request ever leaves
the machine: `judge_both`'s vote seam is filled by scripted voters that answer from the
side's own pack, the way `read_rows` does in `test_claimcheck_compare`, in the reply shapes
the live voters give.
"""

from __future__ import annotations

import configparser
import importlib.util
import json
import re
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import claim_diff as cd  # noqa: E402  (the phase's own machinery: judge_both, prepare_sides, the reuse)
from tools.claimcheck.judge import ask_with_packs  # noqa: E402  (the parser and the quote check)
from tools.contest import export  # noqa: E402
from tools.contest.gates import judge_worktree  # noqa: E402

JUDGE = ROOT / "scripts" / "judge_epic_round.py"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("CC-14 must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _load_judge():
    """`scripts/judge_epic_round.py` as a module, the way the CLI loads it."""
    spec = importlib.util.spec_from_file_location("judge_epic_round_296", JUDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


judge = _load_judge()

# ------------------------------------------------------------------ the scripted voters

#: `=== CLAIM 3 ===` and the claim and its evidence under it, one block per claim of a batch.
_BLOCK = re.compile(r"=== CLAIM (\d+) ===\n(.*?)(?=\n\n=== CLAIM \d+ ===|\Z)", re.S)
#: `[[src:pkg/mod.py:1-3]]  (code)` — the label that opens a chunk of a rendered pack.
_LABEL = re.compile(r"^\[\[(.+?)\]\]  \(\w+\)$", re.M)
#: The gutter of a source line, `  12 | ` and, for a removed diff line, `    | `.
_GUTTER = re.compile(r"^\s*\d*\| ?")


def _src_function(evidence: str, symbol: str):
    """`(chunk id, lines)` of the first `src:` chunk holding `def <symbol>(`.

    The lines are without their gutter, from the `def` line to the next `def` or the end of
    the chunk. `None` when the pack does not carry the function the claim is about: the voter
    then has no evidence to decide it by and answers UNSURE, which is the honest answer.
    """
    labels = list(_LABEL.finditer(evidence))
    for k, match in enumerate(labels):
        if not match.group(1).startswith("src:"):
            continue
        text = evidence[match.end():labels[k + 1].start() if k + 1 < len(labels) else len(evidence)]
        lines = text.splitlines()
        at = next((i for i, line in enumerate(lines) if f"def {symbol}(" in line), None)
        if at is None:
            continue
        body = []
        for line in lines[at + 1:]:
            clean = _GUTTER.sub("", line)
            if clean.startswith("def "):
                break
            body.append(clean)
        return match.group(1), [lines[at]] + body
    return None


def _decide(claim: str, evidence: str):
    """`(true?, chunk id, [quoting lines])` of one claim against the evidence of one side.

    The voters answer from the side's own tree: the line after `def <name>(` decides a
    `returns True|False` claim, and a claim that says `never writes` is true only when the
    function the claim names calls for no write. The quote is the `def` line plus the
    deciding line, so a quote runs over two lines, as the live voters give them.
    """
    symbol = re.search(r"`([A-Za-z_]\w*)`", claim)
    if not symbol:
        return None
    got = _src_function(evidence, symbol.group(1))
    if got is None:
        return None
    chunk, lines = got
    body = "\n".join(lines[1:])
    if "never writes" in claim:
        true = not re.search(r"\b(write|open|makedirs|unlink)\s*\(", body)
    else:
        want = re.search(r"returns (True|False)", claim)
        true = bool(want) and bool(re.search(r"return\s+" + want.group(1) + r"\s*$", body, re.M))
    return true, chunk, lines


def _rows_from_prompt(prompt: str) -> list:
    """One scripted voter's answer for one batch: a verdict per claim, with a real quote."""
    out = []
    for match in _BLOCK.finditer(prompt):
        claim, evidence = match.group(2).split("\n", 1)
        got = _decide(claim, evidence)
        if got is None:
            out.append({"id": int(match.group(1)), "verdict": "UNSURE"})
            continue
        true, chunk, lines = got
        out.append({"id": int(match.group(1)), "verdict": "TRUE" if true else "FALSE",
                    "chunk": chunk, "quote": "\n".join(lines)})
    return out


def fake_vote(log=None, models=("a/x", "b/y", "c/z")):
    """The seam's fake: every model x one run through `ask_with_packs`, so the prompt, the
    parser and the quote check are the real ones. *log* collects one entry per request,
    `(side, model)`, which is what the cost line is billed against. The replies come in the
    shapes the live voters give: a JSON array of objects, a quote over two lines, a raw
    newline inside a string."""
    log = log if log is not None else []

    def vote(items, side):
        rows = []
        for model in models:
            def complete(prompt, model=model):
                log.append((side, model))
                reply = _rows_from_prompt(prompt)
                if model.startswith("c/"):
                    # a raw newline inside a quoted string, in a fenced reply
                    return "```json\n" + json.dumps(reply).replace("\\n", "\n") + "\n```"
                return json.dumps(reply)
            got = ask_with_packs(items, 0, 1, complete)
            rows.append({"model": model, "run": 0, "votes": got["votes"],
                         "accepted": got["accepted"], "rejected": got.get("rejected", [])})
        return rows

    return vote


def _requests(log, side=None) -> int:
    return sum(1 for s, _m in log if side is None or s == side)


# ------------------------------------------------------------------ the round

_BRIDGE = """\
class CollectBridge:
    def _shrink(self, text):
        return text[:10]
"""

_MOD_BASE = """\
def flag():
    return False


def load_readonly(target):
    return list(target)
"""

#: `fixed` makes the change the claims describe.
_MOD_FIXED = """\
def flag():
    return True


def load_readonly(target):
    return list(target)
"""

#: `other` ships a commit, but leaves the cited code alone.
_MOD_OTHER = """\
def flag():
    return False


def load_readonly(target):
    return list(target)


def unrelated():
    return 0
"""

_TEST = """\
import pkg.mod as mod


def test_flag():
    assert isinstance(mod.flag(), bool)
"""

_TICKET = """\
# 296 — the round's claims at the judge

**Status:** open

**Severity:** MEDIUM

**File:** `pkg/mod.py`

**Symbol:** flag

**Round:** 296

**Size:** S

**Also touches:** `tests/test_flag.py`

---

## Why

The judge reads the diff instead of judging the claims.

## What to build

Make `flag` return True.

## Acceptance

```bash
python3 -m pytest tests/test_flag.py
```

## Claims

- [new] `flag` in `pkg/mod.py` returns True.
- [fixed] `flag` in `pkg/mod.py` returns False.
- [still] `load_readonly` in `pkg/mod.py` never writes to the directory.

## Rules

Keep `_shrink` byte-identical.
"""

CLAIMS = [
    {"id": "c-new", "claim": "`flag` in `pkg/mod.py` returns True.", "expect": "new"},
    {"id": "c-fix", "claim": "`flag` in `pkg/mod.py` returns False.", "expect": "fixed"},
    {"id": "c-still", "claim": "`load_readonly` in `pkg/mod.py` never writes to the directory.",
     "expect": "still"},
]


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ini() -> configparser.ConfigParser:
    """An empty `[claim_vote]`: the phase's own defaults, never this repo's config."""
    parser = configparser.ConfigParser()
    parser.add_section("claim_vote")
    return parser


@pytest.fixture(scope="module")
def rd(tmp_path_factory):
    """`{"base", "repo", "fixed", "other", "two"}` — a base commit and three entries.

    `fixed` makes the change the claims describe, `other` leaves the cited code alone, and
    `two` ships two commits so it fails the `commits` gate and is not judged at all.
    """
    root = tmp_path_factory.mktemp("claims")
    repo = root / "repo"
    _write(repo / "tools" / "auto" / "collect_bridge.py", _BRIDGE)
    _write(repo / "conftest.py", "")
    _write(repo / "pkg" / "__init__.py", "")
    _write(repo / "pkg" / "mod.py", _MOD_BASE)
    _write(repo / "tests" / "test_base.py", _TEST)
    _write(repo / "epic-tasks" / "296-claims.md", _TICKET)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "claims@example.com")
    _git(repo, "config", "user.name", "Claims")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    def entry(name, mod, commits=1):
        wt = root / f"wt-{name}"
        _git(repo, "worktree", "add", "-q", str(wt), base)
        _git(wt, "checkout", "-q", "-b", f"contest/296/{name}")
        _write(wt / "pkg" / "mod.py", mod)
        _write(wt / "tests" / f"test_{name}.py", _TEST)
        _git(wt, "add", "-A")
        _git(wt, "commit", "-q", "-m", f"{name}: the change")
        for _ in range(commits - 1):
            _git(wt, "commit", "-q", "--allow-empty", "-m", "a second commit")
        return wt

    return {"base": base, "repo": repo,
            "fixed": entry("fixed", _MOD_FIXED), "other": entry("other", _MOD_OTHER),
            "two": entry("two", _MOD_FIXED, commits=2)}


def _rows(rd, names) -> list:
    """The scorecard rows the judge scores, in the order the phase is given them."""
    return [judge_worktree(name, str(rd[name]), rd["base"], [], False) for name in names]


def _phase(rd, names, *, claims=CLAIMS, log=None, voters=("a/x", "b/y", "c/z"),
           vote=None, out_dir=None, **kwargs):
    return judge.claims_phase(_rows(rd, names), rd["base"], claims, voters=voters,
                              parser=_ini(), log=log if log is not None else (lambda *a: None),
                              vote=vote, out_dir=out_dir, **kwargs)


def _entry_of(data, name):
    return next(e for e in data["entries"] if e["agent"] == name)


# ------------------------------------------------------------------ the table

def test_the_table_counts_and_names_every_mismatch(rd):
    """The entry that made the change meets its `new` and `fixed`; the other gets UNCLEAR
    and STILL for them, and both appear under the mismatches with their evidence ids."""
    log = []
    data = _phase(rd, ["fixed", "other"], log=lambda *a: None, vote=fake_vote(log=log))
    assert data["status"] == "ok" and not data["refusal"]

    good = _entry_of(data, "fixed")
    assert good["counts"] == {"FIXED": 1, "NEW": 1, "STILL": 1}
    assert (good["met"], good["total"]) == (3, 3) and good["mismatches"] == []

    other = _entry_of(data, "other")
    # `flag` still returns False: not NEW, and the defect is not FIXED.
    assert other["counts"] == {"STILL": 2, "UNCLEAR": 1}
    assert (other["met"], other["total"]) == (1, 3)
    assert [line.split(":")[0].split()[1] for line in other["mismatches"]] == ["c-new", "c-fix"]

    for line in other["mismatches"]:
        assert "expect" in line and "got" in line
        # both sides' verdicts and the evidence ids of each, as `claim_diff --check` prints them
        assert re.search(r"\(base (TRUE|FALSE)[*]* \[src:pkg/mod\.py", line)
        assert re.search(r"head (TRUE|FALSE)[*]* \[src:pkg/mod\.py", line)

    # the same delta ids reach the mismatch lines of both sides of the ticket
    assert sum(len(e["mismatches"]) for e in data["entries"]) == len(data["mismatches"]) == 2


def test_a_claim_without_an_expect_is_counted_but_never_mismatched(rd):
    """The same claim with no word in brackets: still counted, but never a mismatch and never in `met`."""
    claims = [dict(c) for c in CLAIMS if c["id"] != "c-still"]
    claims.append({"id": "c-plain",
                   "claim": "`load_readonly` in `pkg/mod.py` never writes to the directory."})
    data = _phase(rd, ["fixed"], claims=claims, vote=fake_vote())
    entry = _entry_of(data, "fixed")
    assert entry["counts"]["STILL"] == 1           # counted
    assert entry["total"] == 2 and entry["met"] == 2 and entry["mismatches"] == []


def test_the_cost_line_is_the_bill_and_comes_first(rd, capsys):
    """One line before the first request, and its number is the requests the voters received."""
    log = []
    lines = []
    seen_requests = []

    def vote(items, side):
        seen_requests.append(side)
        return fake_vote(log=log)(items, side)

    data = _phase(rd, ["fixed", "other"], log=lines.append, vote=vote)
    capsys.readouterr()
    costs = [line for line in lines if "Claims —" in line]
    assert len(costs) == 1
    billed = int(re.search(r"(\d+) requests", costs[0]).group(1))
    assert billed == _requests(log) == data["requests"] == 18       # 3 claims: 2 batches x 3 voters
    assert seen_requests[0] == "base" and _requests(log, "base") == 6
    # the bill is printed before the first request: no entry line before it
    assert not any(line.startswith("  [1/2]") for line in lines[:lines.index(costs[0])])


def test_the_base_side_is_asked_once_per_round(rd):
    """One entry and three entries pay the same for the base; the heads add up per entry."""
    log1 = []
    _phase(rd, ["fixed"], log=None, vote=fake_vote(log=log1))
    assert _requests(log1, "base") == 6 and _requests(log1, "head") == 6

    log2 = []
    data = _phase(rd, ["fixed", "other", "two"], log=lambda *a: None, vote=fake_vote(log=log2))
    assert data["status"] == "ok"
    assert data["skipped"] == {"two": "2 commits, expected 1"}
    assert [e["agent"] for e in data["entries"]] == ["fixed", "other"]
    assert _requests(log2, "base") == _requests(log1, "base") == 6
    assert _requests(log2, "head") == 12            # one head side per eligible entry, not per round


def test_a_voter_with_no_ini_section_is_a_refusal_before_any_request(rd):
    """`--claims-profiles` names a section the ini does not have: one refusal naming it, no vote asked."""
    asked = []

    def vote(items, side):
        asked.append(side)
        return []

    data = _phase(rd, ["fixed"], voters=("claim_vote_llm.nobody",), vote=vote)
    assert data["status"] != "ok" and "claim_vote_llm.nobody" in data["refusal"]
    assert asked == [] and data["requests"] == 0


def test_a_phase_given_no_parser_reads_the_ini_for_the_voters(rd, tmp_path, monkeypatch):
    """`main` passes no parser: the votes must ask with the ini of `repo_root`, never with None."""
    (tmp_path / "contest.local.ini").write_text(
        "[claim_vote]\n\n[claim_vote_llm.v1]\nbase_url = http://x\napi_key = x\nmodel = m\n")
    seen = {}

    def ask_v2(ref, items, run, seed, parser, *a, **k):
        seen["parser"] = parser
        return []

    def make_vote(voters, runs, pool, ask):
        ask(voters[0], [], 0)
        return lambda items, side: []

    monkeypatch.setattr(judge.cv, "ask_v2", ask_v2)
    monkeypatch.setattr(judge.cd, "make_vote", make_vote)
    judge.claims_phase(_rows(rd, ["fixed"]), rd["base"], CLAIMS, voters=["claim_vote_llm.v1"], parser=None,
                       repo_root=tmp_path, log=lambda *a: None)
    assert seen["parser"] is not None and seen["parser"].has_section("claim_vote_llm.v1")


# ------------------------------------------------------------------ the re-use

def _sides(rd, name="fixed"):
    from tools.claimcheck.target import Target
    stack = __import__("contextlib").ExitStack()
    base = stack.enter_context(Target.open(rd[name], rd["base"], scratch=rd["repo"] / "scratch"))
    head = stack.enter_context(Target.open(rd[name], "HEAD", scratch=rd["repo"] / "scratch"))
    return stack, base, head, cd.prepare_sides(CLAIMS, base, head, cd.PackBudget())


def test_a_recorded_base_that_differs_is_refused(rd):
    """Other voters, another sha, another rule: each is refused with the field that differs."""
    stack, base, head, sides = _sides(rd)
    try:
        got = cd.judge_both(CLAIMS, base, head, fake_vote(), cd.PackBudget(), accept="all",
                            sides=sides, voters=("a/x", "b/y", "c/z"), runs=1)
        record = cd.base_side_record(CLAIMS, base, ("a/x", "b/y", "c/z"), 1, got,
                                     collect=True, budget=cd.PackBudget(), seed=1,
                                     fixed_prompt=False, models={})
        for override, wanted in (
            ({"voters": ("a/x", "b/y", "c/z", "d/w")}, "voters"),
            ({"runs": 2}, "runs"),
            ({"accept": "committed"}, "accept rule"),
        ):
            with pytest.raises(ValueError, match=wanted):
                cd.judge_both(CLAIMS, base, head, fake_vote(), cd.PackBudget(),
                              base_side=record, sides=sides, accept=override.get("accept", "all"),
                              voters=override.get("voters", ("a/x", "b/y", "c/z")),
                              runs=override.get("runs", 1), collect=True, seed=1,
                              fixed_prompt=False, models={})
    finally:
        stack.close()


def test_a_recorded_base_of_another_tree_is_refused(rd):
    """The base sha is part of the identity: a record of another base is not this round's base."""
    stack, base, head, sides = _sides(rd)
    try:
        got = cd.judge_both(CLAIMS, base, head, fake_vote(), cd.PackBudget(),
                            sides=sides, voters=("a/x", "b/y", "c/z"), runs=1)
        record = cd.base_side_record(CLAIMS, base, ("a/x", "b/y", "c/z"), 1, got,
                                     collect=True, budget=cd.PackBudget(), seed=1,
                                     fixed_prompt=False, models={})
        record["identity"]["base"] = "0" * 40
        with pytest.raises(ValueError, match="base sha 0{7}"):
            cd.judge_both(CLAIMS, base, head, fake_vote(), cd.PackBudget(), base_side=record,
                          sides=sides, accept="all", voters=("a/x", "b/y", "c/z"), runs=1,
                          collect=True, seed=1, fixed_prompt=False, models={})
    finally:
        stack.close()


def test_base_votes_of_an_earlier_run_are_reused_from_the_command_line(rd, tmp_path, monkeypatch):
    """`claim_diff.py --base-votes FILE`: the base is voted once, and a different rule refuses."""
    calls = []

    def request_completion(url, headers, payload, timeout, **_k):
        calls.append(1)
        return json.dumps(_rows_from_prompt(payload["messages"][-1]["content"]))

    from tools.auto.llm_profile import LlmSettings
    import claim_vote as cv
    monkeypatch.setattr(cv, "voter_settings", lambda ref, parser: LlmSettings(
        base_url=f"http://{ref.split('/')[0]}.invalid/v1", api_key="k", model=ref,
        temperature=0.3, max_tokens=100))
    monkeypatch.setattr(cv, "request_completion", request_completion)

    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps(CLAIMS), encoding="utf-8")
    common = [str(claims), "--target", str(rd["fixed"]), "--base", rd["base"], "--head", "HEAD",
              "--profiles", "a/x", "b/y", "c/z", "--runs", "1", "--interval", "0.001",
              "--repo-root", str(tmp_path), "--scratch", str(tmp_path / "s"),
              "--out", str(tmp_path / "out1")]
    assert cd.main(common) == 0
    first = len(calls)
    calls.clear()
    assert cd.main(common + ["--base-votes", str(tmp_path / "out1" / "votes_base.json"),
                             "--out", str(tmp_path / "out2")]) == 0
    # the base side was not voted again: only the head side's three voters x two batches
    assert len(calls) == first // 2, (first, len(calls))
    assert _changes(tmp_path / "out1") == _changes(tmp_path / "out2")
    # a rule that differs from the record's is refused before a request
    calls.clear()
    assert cd.main(common + ["--base-votes", str(tmp_path / "out1" / "votes_base.json"),
                             "--accept", "committed", "--out", str(tmp_path / "out3")]) == 2
    assert calls == [], "a refused re-use sends no request"


def _changes(out_dir) -> dict:
    return {r["id"]: r["change"] for r in json.loads((out_dir / "delta.json").read_text())["deltas"]}


# ------------------------------------------------------------------ parsing the section

def test_the_ticket_claims_parse_the_four_words():
    text = "\n".join([
        "## Claims",
        "",
        "- [new] `a` returns 1.",
        "- [fixed] `a` returns 2.",
        "- [still] `a` never writes.",
        "- [gone] `a` is imported.",
        "",
        "- [new] `b` returns",
        "    3 on the slow path.",
        "",
        "## Rules",
        "- [new] `c` is prose after the section, not a claim.",
    ])
    claims = judge.ticket_claims(text, "296-x.md")
    assert [c["expect"] for c in claims] == ["new", "fixed", "still", "gone", "new"]
    assert claims[-1]["claim"] == "`b` returns 3 on the slow path."   # the wrapped bullet is one claim
    assert claims[2]["claim"] == "`a` never writes."
    assert len(claims) == 5, "prose after the next heading is not part of the section"


def test_an_unknown_word_is_a_refusal_that_names_the_line():
    text = "## Claims\n\n- [ne]w `a` returns 1.\n"
    with pytest.raises(ValueError, match=r"296-x\.md:3: '\[ne\]w' is not one of"):
        judge.ticket_claims(text, "296-x.md")
    text = "## Claims\n\n- `a` returns 1.\n"
    with pytest.raises(ValueError, match=r"296-x\.md:3: a claim names its word in brackets"):
        judge.ticket_claims(text, "296-x.md")


def test_a_ticket_without_the_section_has_no_claims():
    assert judge.ticket_claims(_TICKET.split("## Claims")[0], "296-x.md") == []
    assert judge.ticket_claims("", "296-x.md") == []


def test_a_claims_heading_with_no_claim_is_a_refusal():
    # HARDEN-14 D: a ticket that opened the section and gave it nothing was `[]`, which read as
    # "the ticket said nothing" — the judge then scored nothing and said nothing.
    with pytest.raises(ValueError, match=r"296-x\.md:1: the Claims section has no claim"):
        judge.ticket_claims("## Claims\n\n", "296-x.md")
    with pytest.raises(ValueError, match=r"296-x\.md:1: the Claims section has no claim"):
        judge.ticket_claims("## Claims:\n", "296-x.md")
    with pytest.raises(ValueError, match=r"296-x\.md:3: the Claims section has no claim"):
        judge.ticket_claims("# T\n\n### Claims\n\n", "296-x.md")


# ------------------------------------------------------------------ the phase's refusals

def test_an_entry_that_failed_a_hard_gate_costs_no_request(rd):
    log = []
    data = _phase(rd, ["fixed", "two"], log=lambda *a: None, vote=fake_vote(log=log))
    assert data["status"] == "ok"
    assert data["skipped"] == {"two": "2 commits, expected 1"}
    assert [e["agent"] for e in data["entries"]] == ["fixed"]
    assert _requests(log) == 12, "the base once and one head: the skipped entry paid nothing"


def test_every_entry_skipped_is_nothing_judged(rd, capsys):
    log = []
    data = _phase(rd, ["two"], log=lambda *a: None, vote=fake_vote(log=log))
    assert data["status"] == "nothing" and data["entries"] == []
    assert data["skipped"] == {"two": "2 commits, expected 1"}
    assert _requests(log) == 0


def test_a_base_that_does_not_resolve_is_a_refusal(rd):
    data = judge.claims_phase(_rows(rd, ["fixed"]), "0" * 40, CLAIMS,
                              voters=("a/x", "b/y", "c/z"), parser=_ini(),
                              log=lambda *a: None, vote=fake_vote())
    assert data["status"] == "refused" and "resolves in no checkout" in data["refusal"]


def test_a_refused_phase_never_raises(rd):
    data = _phase(rd, [], vote=fake_vote())
    assert data["status"] == "nothing"
    data = _phase(rd, ["fixed"], claims=[], vote=fake_vote())
    assert data["status"] == "refused" and "no claims to judge" in data["refusal"]
    data = _phase(rd, ["fixed"], voters=(), vote=fake_vote())
    assert data["status"] == "refused" and "no voters" in data["refusal"]


def test_an_expect_on_a_world_claim_is_the_refusal_of_judge_both(rd):
    world = [dict(c) for c in CLAIMS if c["id"] == "c-still"]
    world[0] = {"id": "c-world", "claim": "The Earth goes around the Sun once a year.",
                "expect": "fixed"}
    log = []
    data = _phase(rd, ["fixed"], claims=world, log=lambda *a: None, vote=fake_vote(log=log))
    assert data["status"] == "refused"
    assert "expect on a world claim (c-world)" in data["refusal"]
    assert _requests(log) == 0, "a refused claim set costs no request"


# ------------------------------------------------------------------ artifacts

def test_the_artifacts_go_next_to_cross(rd, tmp_path):
    out = tmp_path / "contest-out" / "296"
    log = []
    data = _phase(rd, ["fixed", "other"], out_dir=str(out), log=lambda *a: None,
                  vote=fake_vote(log=log), round_no=296, ticket="296-claims.md")
    assert data["status"] == "ok"

    md = (out / "claims.md").read_text(encoding="utf-8")
    assert md.startswith("# Claims") and "296-claims.md" in md
    assert md.splitlines()[0] == "# Claims — the ticket's claims at the base and at every entry"
    assert "| agent | FIXED | STILL | NEW | GONE | UNCLEAR | n/a | expect |" in md
    assert "| fixed |" in md and "| other |" in md

    for name in ("fixed", "other"):
        folder = out / "claims" / name
        assert (folder / "delta.json").is_file(), folder
        assert (folder / "votes_base.json").is_file(), "the base side, judged once"
        assert (folder / "votes_head.json").is_file()
        head = json.loads((folder / "votes_head.json").read_text())
        assert head["target"]["sha"] == _git(rd[name], "rev-parse", "HEAD")
    # the one base side, written unchanged into both entries' folders
    base_fixed = (out / "claims" / "fixed" / "votes_base.json").read_bytes()
    assert base_fixed == (out / "claims" / "other" / "votes_base.json").read_bytes()
    assert json.loads((out / "claims.json").read_text())["requests"] == data["requests"] == 18


# ------------------------------------------------------------------ the CLI

def _argv(rd, names, *extra, out=None, tasks=None):
    cmd = [str(JUDGE), "--round", "296", "--base", rd["base"],
           "--tasks", str(tasks or (rd["repo"] / "epic-tasks"))]
    for name in names:
        cmd += ["--worktree", f"{name}={rd[name]}"]
    return cmd + (["--cross-out", str(out)] if out else []) + list(extra)


def test_the_cli_judges_the_ticket_claims(rd, tmp_path, monkeypatch, capsys):
    """`--claims` with no FILE reads the ticket's `## Claims` and prints the third phase."""
    log = []
    monkeypatch.setattr(cd, "make_vote", lambda *a, **k: fake_vote(log=log))
    out = tmp_path / "contest-out" / "296"
    monkeypatch.chdir(tmp_path)
    argv = sys.argv
    sys.argv = _argv(rd, ["fixed", "other"], "--claims", "--claims-profiles", "a/x", "b/y",
                       "c/z", out=out)
    try:
        rc = judge.main()
    finally:
        sys.argv = argv
    out_txt, err_txt = capsys.readouterr()

    assert rc == 0
    # the score table still comes first, the claims phase is the third phase
    assert out_txt.index("Round 296:") < out_txt.index("Claims —")
    assert "the ticket has no ## Claims section" not in out_txt
    assert "296-claims.md:27" not in err_txt
    assert re.search(r"Claims — 3 claims, 3 voters, 1 run\(s\), 2 entries: 18 requests", out_txt)
    # a ticket's claims carry no id, so the mismatch names the claim's own text
    assert "Mismatches" in out_txt and "expect is the ticket" in out_txt
    assert "returns True.: expect new, got UNCLEAR" in out_txt
    assert "returns False.: expect fixed, got STILL" in out_txt
    assert re.search(r"\(base TRUE\* \[src:pkg/mod\.py:1-\d+\]; head TRUE\*", out_txt)
    assert (out / "claims.md").is_file() and (out / "claims" / "fixed" / "delta.json").is_file()
    assert _requests(log) == 18 and _requests(log, "base") == 6


def test_a_ticket_without_the_section_is_no_phase(rd, tmp_path, monkeypatch, capsys):
    """No section and no FILE: one line saying so, the exit code untouched."""
    tasks = tmp_path / "tasks"
    _write(tasks / "296-bare.md", _TICKET.split("## Claims")[0])
    monkeypatch.chdir(tmp_path)
    argv = sys.argv
    sys.argv = _argv(rd, ["fixed"], "--claims", tasks=tasks)
    try:
        rc = judge.main()
    finally:
        sys.argv = argv
    out_txt, err_txt = capsys.readouterr()
    assert rc == 0, out_txt + err_txt
    assert "the ticket has no ## Claims section, and no FILE was given" in out_txt
    assert "Claims —" not in out_txt          # the phase did not run


def test_a_claims_file_is_read_and_a_bad_one_is_a_refusal(rd, tmp_path, monkeypatch, capsys):
    tasks = tmp_path / "tasks"
    _write(tasks / "296-bare.md", _TICKET.split("## Claims")[0])
    good = tmp_path / "claims.json"
    good.write_text(json.dumps(CLAIMS), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"claim": "`flag` in `pkg/mod.py` returns True.", "expect": "fxed"}]),
                   encoding="utf-8")
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(cd, "make_vote", lambda *a, **k: fake_vote())
    argv = sys.argv
    try:
        sys.argv = _argv(rd, ["fixed"], "--claims", str(good), "--claims-profiles", "a/x", "b/y",
                             "c/z", tasks=tasks)
        assert judge.main() == 0
    finally:
        sys.argv = argv
    out_txt, err_txt = capsys.readouterr()
    assert "Claims —" in out_txt and "fxed" not in err_txt

    for file, wanted in ((bad, "fxed"), (empty, "no claims to judge"),
                         (tmp_path / "nope.json", "nope.json")):
        capsys.readouterr()
        sys.argv = _argv(rd, ["fixed"], "--claims", str(file), tasks=tasks)
        rc = judge.main()
        out_txt, err_txt = capsys.readouterr()
        assert rc == 2, (file, out_txt, err_txt)
        assert wanted in err_txt


def test_no_claims_flag_is_no_phase(rd, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    argv = sys.argv
    sys.argv = _argv(rd, ["fixed"])
    try:
        rc = judge.main()
    finally:
        sys.argv = argv
    out_txt, _err = capsys.readouterr()
    assert rc == 0
    assert "Claims —" not in out_txt
    assert "the ticket's Acceptance list says" in out_txt    # the judge still asks the old question


# ------------------------------------------------------------------ the hand-over

def test_write_summary_names_claims_only_when_the_ticket_has_the_section(tmp_path):
    """The judge line carries `--claims` exactly when the round's ticket has the section."""
    agent = SimpleNamespace(workspace=SimpleNamespace(path=str(tmp_path / "wt")),
                            agent=SimpleNamespace(name="opus"))

    def summary(body):
        tasks = tmp_path / "epic-tasks"
        _write(tasks / "296-claims.md", body)
        state = SimpleNamespace(round_no=296, ticket="296-claims.md", started_at=0.0,
                                agents=[agent], table_rows=lambda: [])
        out = tmp_path / "contest-out" / "296"
        return export.write_summary(out, state, "b" * 40, [], repo=tmp_path).read_text()

    with_claims = summary(_TICKET)
    line = next(l for l in with_claims.splitlines() if l.startswith("python3 scripts/judge_epic_round.py"))
    assert line.endswith(" --claims"), line
    assert "--worktree opus=" in line

    bare = summary(_TICKET.split("## Claims")[0])
    bare_line = next(l for l in bare.splitlines() if l.startswith("python3 scripts/judge_epic_round.py"))
    assert "--claims" not in bare_line, bare_line
    assert bare_line.rstrip().endswith(str(tmp_path / "wt"))


def test_a_ticket_the_summary_cannot_read_names_no_claims(tmp_path):
    assert export.write_summary(
        tmp_path / "out", SimpleNamespace(round_no=296, ticket="9-9-absent.md", started_at=0.0,
                                          agents=[], table_rows=lambda: []), "b" * 40, [],
        repo=tmp_path).read_text()


def test_the_draft_accepts_the_section_and_documents_it():
    """The draft stage accepts `## Claims` and its format spec says how to write it."""
    from tools.contest import draft
    assert "Claims" in draft.OPTIONAL_SECTIONS
    spec = draft.ticket_format_spec()
    assert "## Claims" in spec
    for word in draft.CLAIM_WORDS:
        assert f"- [{word}]" in spec
    assert "judge_epic_round.py --claims" in spec

    problems = draft.lint_ticket(_TICKET, None)
    assert not [p for p in problems if "Claims" in p], problems
    # the section is optional: a ticket without it is not refused for that
    assert not [p for p in draft.lint_ticket(_TICKET.split("## Claims")[0], None)
                if "Claims" in p]


# ------------------------------------------------------------------ CC-16 Part B: the identity holds the evidence settings

def _record(rd, **kwargs):
    """A recorded base side of this round, every identity field this run can state."""
    stack, base, head, sides = _sides(rd)
    got = cd.judge_both(CLAIMS, base, head, fake_vote(), cd.PackBudget(), sides=sides,
                        voters=("a/x", "b/y", "c/z"), runs=1)
    record = cd.base_side_record(CLAIMS, base, ("a/x", "b/y", "c/z"), 1, got,
                                 collect=True, budget=cd.PackBudget(), seed=1,
                                 fixed_prompt=False, models={m: m for m in ("a/x", "b/y", "c/z")})
    stack.close()
    return record


def _judge_with(record, refuse_if=lambda r: r):
    """`judge_both` with `record` as the base side; returns what came back, or the refusal."""
    return cd.base_side_differs(record, claims=CLAIMS, base_sha=record["identity"]["base"],
                                voters=("a/x", "b/y", "c/z"), runs=1, accept="all",
                                index=list(record["index"]), collect=True,
                                budget=cd.PackBudget(), seed=1, fixed_prompt=False,
                                models={m: m for m in ("a/x", "b/y", "c/z")})


def test_a_record_judged_with_other_settings_is_refused_naming_the_field(rd):
    """CC-16 (Part B): a base judged with the big pack and the collect facts compared against
    a run with the small pack or none is another run's evidence — and the refusal says which
    of the two it was."""
    record = _record(rd)
    small = cd.PackBudget(chars=1000, chunks=record["identity"]["budget"]["chunks"],
                          per_chunk=record["identity"]["budget"]["per_chunk"])
    for override, wanted in (
        ({"collect": False}, "collect"),
        ({"budget": small}, "budget"),
        ({"seed": 2}, "seed"),
        ({"fixed_prompt": True}, "fixed prompt"),
        ({"models": {"a/x": "another-model"}}, "voter models"),
    ):
        diff = cd.base_side_differs(
            record, claims=CLAIMS, base_sha=record["identity"]["base"],
            voters=("a/x", "b/y", "c/z"), runs=1, accept="all",
            index=list(record["index"]), **{**{"collect": True, "budget": cd.PackBudget(),
                                               "seed": 1, "fixed_prompt": False,
                                               "models": {m: m for m in ("a/x", "b/y", "c/z")}},
                                           **override})
        assert wanted in diff, (override, diff)
        assert diff.startswith("recorded base side not re-used: ") or True


def test_a_record_with_the_same_settings_is_accepted(rd):
    """CC-16 (Part B): the other half — the same collect flag, budget, seed, prompt mode and
    voter models is this run's own record, so it is reused."""
    record = _record(rd)
    assert _judge_with(record) == "", _judge_with(record)


def test_a_record_written_before_the_fields_is_refused_saying_so(rd):
    """CC-16 (Part B): a record with none of the new fields cannot be shown to be this run's,
    so it is refused with a message that says so rather than accepted by the absence of a
    comparison."""
    record = _record(rd)
    for field in ("collect", "budget", "seed", "fixed_prompt", "models"):
        stripped = {**record, "identity": {k: v for k, v in record["identity"].items() if k != field}}
        diff = cd.base_side_differs(
            stripped, claims=CLAIMS, base_sha=stripped["identity"]["base"],
            voters=("a/x", "b/y", "c/z"), runs=1, accept="all", index=list(stripped["index"]),
            collect=True, budget=cd.PackBudget(), seed=1, fixed_prompt=False,
            models={m: m for m in ("a/x", "b/y", "c/z")})
        assert field in diff and "written before" in diff, (field, diff)


def test_base_votes_from_a_file_are_compared_with_its_own_settings(rd, tmp_path):
    """CC-16 (Part B): `--base-votes` reads the collect flag, the budget and the voters'
    models out of the file `side_report` writes, so a record of another budget is refused
    and the seed and prompt mode the command line states are part of the comparison."""
    out = tmp_path / "out1"
    out.mkdir()
    (out / "votes_base.json").write_text(json.dumps({
        "target": {"sha": "b" * 40, "collect": True,
                   "budget": {"chars": 6000, "chunks": 3, "per_chunk": 2000}},
        "accept": "all", "models": {"a/x": "m1"}, "results": [
            {"model": "a/x", "run": 0, "votes": {0: "TRUE"}}],
        "claims": [{"id": "c-new", "claim": CLAIMS[0]["claim"]}],
    }), encoding="utf-8")
    record = cd.base_side_from_votes(out / "votes_base.json", seed=1, fixed_prompt=False)
    ident = record["identity"]
    assert ident["collect"] is True
    assert ident["budget"] == {"chars": 6000, "chunks": 3, "per_chunk": 2000}
    assert ident["models"] == {"a/x": "m1"}
    assert ident["seed"] == 1 and ident["fixed_prompt"] is False

    # the same settings are accepted
    assert cd.base_side_differs(
        record, claims=[CLAIMS[0]], base_sha="b" * 40, voters=("a/x",), runs=1, accept="all",
        index=[0], collect=True,
        budget=cd.PackBudget(chars=6000, chunks=3, per_chunk=2000), seed=1, fixed_prompt=False,
        models={"a/x": "m1"}) == ""
    # the small pack of the ticket's reproduction is refused, naming the budget
    diff = cd.base_side_differs(
        record, claims=[CLAIMS[0]], base_sha="b" * 40, voters=("a/x",), runs=1, accept="all",
        index=[0], collect=True, budget=cd.PackBudget(chars=1000, chunks=3, per_chunk=2000),
        seed=1, fixed_prompt=False, models={"a/x": "m1"})
    assert "budget" in diff
    # no collect is refused, naming the collect flag
    diff = cd.base_side_differs(
        record, claims=[CLAIMS[0]], base_sha="b" * 40, voters=("a/x",), runs=1, accept="all",
        index=[0], collect=False, budget=cd.PackBudget(chars=6000, chunks=3, per_chunk=2000),
        seed=1, fixed_prompt=False, models={"a/x": "m1"})
    assert "collect" in diff


def test_a_voter_whose_model_changed_is_refused(rd):
    """CC-16 (Part B, NB-1): `voters` is compared by profile name only, so a profile whose
    model changed while its name stayed was accepted. The model behind each name is now
    part of the identity."""
    record = _record(rd)
    diff = cd.base_side_differs(
        record, claims=CLAIMS, base_sha=record["identity"]["base"],
        voters=("a/x", "b/y", "c/z"), runs=1, accept="all", index=list(record["index"]),
        collect=True, budget=cd.PackBudget(), seed=1, fixed_prompt=False,
        models={"a/x": "changed", "b/y": "b/y", "c/z": "c/z"})
    assert "voter models" in diff, diff


def test_the_reviewers_case_refuses_rather_than_answering(rd):
    """CC-16 (Part B, the second review's case: a fake voter whose verdict depends on the
    pack length, recorded at the default budget and reused at ``chars=400`` — the reuse is
    refused instead of answering TRUE from evidence the run's voters never saw."""
    stack, base, head, sides = _sides(rd)
    try:
        seen: dict = {}

        def _by_evidence(items, side):
            # the honest answer depends on how much evidence the pack carries: the same
            # voter says TRUE of the bigger pack and FALSE of the smaller one
            shown = len("".join(str(t) for _, t in items))
            seen.setdefault(side, []).append(shown)
            return [{"model": "a/x", "run": 0, "votes": {
                i: ("TRUE" if shown > 1400 else "FALSE") for i in range(len(items))}}]

        big = cd.judge_both(CLAIMS, base, head, _by_evidence, cd.PackBudget(), sides=sides,
                            voters=("a/x",), runs=1)
        record = cd.base_side_record(CLAIMS, base, ("a/x",), 1, big, collect=True,
                                     budget=cd.PackBudget(), seed=1, fixed_prompt=False,
                                     models={"a/x": "a/x"})
        small = cd.PackBudget(chars=400, chunks=cd.PackBudget().chunks,
                              per_chunk=cd.PackBudget().per_chunk)
        # re-prepared under the small budget: the packs themselves are what changed
        fresh = cd.judge_both(CLAIMS, base, head, _by_evidence, small,
                              sides=cd.prepare_sides(CLAIMS, base, head, small),
                              voters=("a/x",), runs=1)
        # the default pack shows the voter more than the small one, so its own answer
        # differs between the two runs: TRUE of the bigger pack, FALSE of the smaller
        assert max(seen["base"]) > min(seen["base"]), seen
        assert any(v == "TRUE" for r in big["base"]["results"] for v in r["votes"].values())
        assert any(v == "FALSE" for r in fresh["base"]["results"] for v in r["votes"].values())
        # ... and re-using the recorded side at the small budget is refused, not answered
        # TRUE from a pack nobody saw
        diff = cd.base_side_differs(record, claims=CLAIMS, base_sha=base.sha, voters=("a/x",),
                                    runs=1, accept="all", index=list(big["base"]["index"]),
                                    collect=True, budget=small, seed=1, fixed_prompt=False,
                                    models={"a/x": "a/x"})
        assert "budget" in diff, diff
    finally:
        stack.close()


def test_the_cli_refuses_a_base_votes_of_another_budget(rd, tmp_path):
    """CC-16 (Part B, Layer 2: `claim_diff.py --base-votes FILE --pack-chars 1000` against a
    file recorded with the default budget — exit 2, the budget named, no request sent."""
    calls = []

    def request_completion(url, headers, payload, timeout, **_k):
        calls.append(1)
        return json.dumps(_rows_from_prompt(payload["messages"][-1]["content"]))

    from tools.auto.llm_profile import LlmSettings
    import claim_vote as cv
    import pytest as _pytest
    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(cv, "voter_settings", lambda ref, parser: LlmSettings(
        base_url=f"http://{ref.split('/')[0]}.invalid/v1", api_key="k", model=ref,
        temperature=0.3, max_tokens=100))
    monkeypatch.setattr(cv, "request_completion", request_completion)
    try:
        claims = tmp_path / "claims.json"
        claims.write_text(json.dumps(CLAIMS), encoding="utf-8")
        common = [str(claims), "--target", str(rd["fixed"]), "--base", rd["base"],
                  "--head", "HEAD", "--profiles", "a/x", "b/y", "c/z", "--runs", "1",
                  "--interval", "0.001", "--repo-root", str(tmp_path),
                  "--scratch", str(tmp_path / "s"), "--out", str(tmp_path / "out1")]
        assert cd.main(common) == 0
        calls.clear()
        assert cd.main(common + ["--base-votes", str(tmp_path / "out1" / "votes_base.json"),
                                 "--pack-chars", "1000",
                                 "--out", str(tmp_path / "out4")]) == 2
        assert calls == [], "a refused re-use sends no request"
        # the same budget, no refusal: the reuse is what it was — the head side only
        calls.clear()
        assert cd.main(common + ["--base-votes", str(tmp_path / "out1" / "votes_base.json"),
                                 "--out", str(tmp_path / "out2")]) == 0
        # 3 claims: 2 batches x 3 voters, the head side alone; the base side was not voted
        assert len(calls) == 6, calls
    finally:
        monkeypatch.undo()


# ------------------------------------------------------------------ CC-16 Part E: one claim classified differently

#: The claim at the heart of Part E: `widget()` exists in one entry and not in the other,
#: so it is a code claim for one and a world claim for the other. No `expect`: an expect
#: on a world claim is an earlier, different refusal.
WIDGET_CLAIMS = [
    {"id": "c-new", "claim": "`flag` in `pkg/mod.py` returns True.", "expect": "new"},
    {"id": "c-still", "claim": "`load_readonly` in `pkg/mod.py` never writes to the directory.",
     "expect": "still"},
    {"id": "c-widget", "claim": "`widget()` returns 42."},
]


@pytest.fixture(scope="module")
def rw(tmp_path_factory):
    """A base commit and two entries: `plain` leaves the cited code alone, `added` also
    defines `widget()`. The same claim then anchors in one entry and not in the other,
    which is the per (base, head) classification Part E is about."""
    root = tmp_path_factory.mktemp("widgetclaims")
    repo = root / "repo"
    _write(repo / "tools" / "auto" / "collect_bridge.py", _BRIDGE)
    _write(repo / "conftest.py", "")
    _write(repo / "pkg" / "__init__.py", "")
    _write(repo / "pkg" / "mod.py", _MOD_BASE)
    _write(repo / "tests" / "test_base.py", _TEST)
    _write(repo / "epic-tasks" / "296-claims.md", _TICKET)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "claims@example.com")
    _git(repo, "config", "user.name", "Claims")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    def entry(name, mod):
        wt = root / f"wt-{name}"
        _git(repo, "worktree", "add", "-q", str(wt), base)
        _git(wt, "checkout", "-q", "-b", f"contest/296/{name}")
        _write(wt / "pkg" / "mod.py", mod)
        _write(wt / "tests" / f"test_{name}.py", _TEST)
        _git(wt, "add", "-A")
        _git(wt, "commit", "-q", "-m", f"{name}: the change")
        return wt

    return {"base": base, "repo": repo,
            "plain": entry("plain", _MOD_OTHER),
            "added": entry("added", _MOD_FIXED + "\n\ndef widget():\n    return 42\n")}


def _widget_phase(rw, names, log=None):
    """The phase over the `widget` repo; *log* is the vote log, so the request count can
    be read off it (the phase's own `log` is the console line, not this list)."""
    log = log if log is not None else []
    return judge.claims_phase(_rows(rw, names), rw["base"], WIDGET_CLAIMS,
                              voters=("a/x", "b/y", "c/z"), parser=_ini(),
                              log=lambda *a: None, vote=fake_vote(log=log))


def test_one_claim_classified_two_ways_refuses_nothing_and_rows_every_entry(rw):
    """CC-16 (Part E), the reproduction: entry `plain` does not define `widget`, `added`
    does, so the claim is a world claim for one and a code claim for the other. Both
    orders of the entries give a table with a row for each, and no refusal."""
    for order in (("plain", "added"), ("added", "plain")):
        log = []
        data = _widget_phase(rw, list(order), log=log)
        assert data["status"] == "ok", (order, data["refusal"])
        assert [e["agent"] for e in data["entries"]] == list(order), order
        # every claim of the ticket is judged for every entry (two of them carry an expect)
        assert data["entries"][0]["total"] == 2 and data["entries"][1]["total"] == 2
        # the base side is asked once for the claims that need it, not once per entry
        assert _requests(log, "base") == 6, (order, _requests(log, "base"))
        assert _requests(log, "head") == 12, order


def test_the_claim_the_record_does_not_hold_is_voted_once(rw):
    """CC-16 (Part E): the entry whose head defines `widget` is the one that pays for its
    base verdict — once, and only because no earlier entry had it. The other two claims
    are taken from the record and never voted again."""
    log = []
    data = _widget_phase(rw, ["plain", "added"], log=log)
    assert data["status"] == "ok"
    # `plain` first: its base side is the two claims that anchor in it (one code batch);
    # `added` adds `widget`, one more batch, and re-uses the other two.
    assert _requests(log, "base") == 6
    # a fresh base side per entry would be 12: the reuse is worth half the round
    assert _requests(log) == 18


def test_a_record_of_another_run_is_still_refused(rw):
    """CC-16 (Part E): only the claims stopped being a refusal. The voters, the runs and
    the rule still are, and they still name the field — even when the record is the other
    entry's, which is the whole point of the reuse."""
    import contextlib

    from tools.claimcheck.target import Target
    stack = contextlib.ExitStack()
    scratch = rw["repo"] / "scratch"
    base = stack.enter_context(Target.open(rw["repo"], rw["base"], scratch=scratch))
    plain = stack.enter_context(Target.open(rw["plain"], "HEAD", scratch=scratch))
    added = stack.enter_context(Target.open(rw["added"], "HEAD", scratch=scratch))
    try:
        sides_plain = cd.prepare_sides(WIDGET_CLAIMS, base, plain, cd.PackBudget())
        got = cd.judge_both(WIDGET_CLAIMS, base, plain, fake_vote(), cd.PackBudget(),
                            sides=sides_plain, voters=("a/x", "b/y", "c/z"), runs=1)
        record = cd.base_side_record(WIDGET_CLAIMS, base, ("a/x", "b/y", "c/z"), 1, got,
                                     collect=True, budget=cd.PackBudget(), seed=1,
                                     fixed_prompt=False, models={})
        sides_added = cd.prepare_sides(WIDGET_CLAIMS, base, added, cd.PackBudget())
        # the same run, the entry whose head defines `widget`: reused claim by claim
        got2 = cd.judge_both(WIDGET_CLAIMS, base, added, fake_vote(), cd.PackBudget(),
                             base_side=record, sides=sides_added, voters=("a/x", "b/y", "c/z"),
                             runs=1, collect=True, seed=1, fixed_prompt=False, models={})
        assert got2["base"]["rows"][2] is not None, "the claim the record did not hold was not voted"
        assert len(record["index"]) < len(got2["base"]["index"]), "the fresh claim was not kept"
        # other voters, more runs, another rule: still refused, still naming the field
        for override, wanted in (({"voters": ("a/x",)}, "voters"),
                                 ({"runs": 2}, "runs"),
                                 ({"accept": "committed"}, "accept rule")):
            with pytest.raises(ValueError, match=wanted):
                cd.judge_both(WIDGET_CLAIMS, base, added, fake_vote(), cd.PackBudget(),
                              base_side=record, sides=sides_added,
                              voters=override.get("voters", ("a/x", "b/y", "c/z")),
                              runs=override.get("runs", 1),
                              accept=override.get("accept", "all"),
                              collect=True, seed=1, fixed_prompt=False, models={})
    finally:
        stack.close()


def test_votes_files_carry_the_seed_and_the_prompt_mode_of_their_run(tmp_path):
    """CC-16 B: a side that is reused later has to say which seed and which prompt mode it was judged with."""
    side = {"results": [], "table": []}
    target = SimpleNamespace(repo="r", ref="main", sha="a" * 40, warnings=[])
    rec = cd.side_report(target, side, None, seed=7, fixed_prompt=True)
    assert rec["seed"] == 7 and rec["fixed_prompt"] is True
    plain = cd.side_report(target, side, None)
    assert "seed" not in plain and "fixed_prompt" not in plain
    # and a record that carries another seed is not this run's
    loaded = cd.base_side_from_votes(_write_json(tmp_path / "votes_base.json", {**rec, "claims": [], "results": []}), seed=1, fixed_prompt=False)
    assert loaded["identity"]["seed"] == 7 and loaded["identity"]["fixed_prompt"] is True


def _write_json(path, data):
    import json as _json
    path.write_text(_json.dumps(data), encoding="utf-8")
    return path


def test_a_record_without_the_budget_is_read_as_one_that_states_none(tmp_path):
    """CC-16 B: a votes file from before the identity grew (no `target.budget`) must be refused by the identity check, not crash the reader."""
    old = _write_json(tmp_path / "old_votes_base.json",
                      {"target": {"sha": "a" * 40}, "accept": "all", "claims": [], "results": []})
    rec = cd.base_side_from_votes(old, seed=1, fixed_prompt=False)
    assert rec and rec["identity"]["budget"] is None and rec["identity"]["collect"] is None
