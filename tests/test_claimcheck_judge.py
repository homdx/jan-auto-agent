"""CC-6 (280): evidence-bound voting — prompt v2, the vote parser v2, the quote check, fake voters end to end."""

from __future__ import annotations

import json
import re
import socket
import sys
from pathlib import Path

import pytest

from tools.claimcheck import judge
from tools.claimcheck.judge import (
    CHUNK_NOT_IN_PACK, QUOTE_NOT_IN_PACK, RULES, Vote, ask_with_packs, build_prompt_v2,
    parse_votes_v2, verify_quotes,
)
from tools.claimcheck.model import Chunk, Pack
from tools.claimcheck.pack import assemble_pack

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote as cv  # noqa: E402


# ------------------------------------------------------------------ no network, for the whole module

@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("CC-6's judge must not open a socket")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_no_network():
    with pytest.raises(AssertionError, match="must not open a socket"):
        socket.create_connection(("example.com", 80))
    with pytest.raises(AssertionError):
        socket.socket()


# ------------------------------------------------------------------ helpers

def src(path: str, a: int, lines: list, why: str = "x") -> Chunk:
    """A `src:` chunk the way CC-3 prints it: the file's own numbers in a `NNN| ` gutter."""
    b = a + len(lines) - 1
    width = len(str(b))
    text = "\n".join(f"{str(n).rjust(width)}| {line}" for n, line in zip(range(a, b + 1), lines))
    return Chunk(f"src:{path}:{a}-{b}", "source", path, a, b, text, why)


def note(cid: str, text: str) -> Chunk:
    return Chunk(cid, "note", "", 0, 0, text, "x")


def pack_of(claim: str, *chunks: Chunk, sha: str = "abc1234def") -> Pack:
    return Pack(claim, sha, tuple(chunks), False)


RUN_FALSE = src("app.py", 10, ["def run_step(cmd):", "    return subprocess.run(cmd, check=False)"])
RUN_TRUE = src("app.py", 20, ["def run_safe(cmd):", "    return subprocess.run(cmd, check=True)"])
HUNK = Chunk("git:abc1234:app.py:10-11", "git", "app.py", 10, 11,
             " 10|  def run_step(cmd):\n 11| +    return subprocess.run(cmd, check=False)", "x")


# ------------------------------------------------------------------ the prompt

def test_prompt_carries_each_claims_pack_and_the_rules():
    p1 = pack_of("`run_step` passes check=False.", RUN_FALSE)
    p2 = pack_of("`run_safe` passes check=False.", RUN_TRUE)
    items = [("`run_step` passes check=False.", p1), ("`run_safe` passes check=False.", p2)]
    prompt, order = build_prompt_v2(items, 0, 1)
    assert RULES in prompt
    assert RULES.startswith("For a claim about this repository, the evidence under it is your only source")
    assert "answer UNSURE — that is a correct answer" in RULES
    assert "copy, character for character, up to 200 characters" in RULES
    assert '"chunk": "<id>", "quote": "<text>"' in prompt
    heads = [m.start() for m in re.finditer(r"=== CLAIM (\d+) ===", prompt)]
    assert [int(n) for n in re.findall(r"=== CLAIM (\d+) ===", prompt)] == [1, 2]   # ids in the order shown
    blocks = [prompt[a:b] for a, b in zip(heads, heads[1:] + [len(prompt)])]
    for n, block in enumerate(blocks):
        claim, pack = items[order[n]]
        assert block.splitlines()[1] == claim                 # the claim, then its own pack under it
        assert pack.render() in block
        other = items[order[1 - n]][1]
        assert f"[[{other.chunks[0].id}]]" not in block


def test_prompt_variation_is_kept():
    items = [(f"claim number {i} about `f{i}`.", pack_of("c", RUN_FALSE)) for i in range(6)]
    runs = [build_prompt_v2(items, run, 7) for run in range(3)]
    assert len({p.split(".")[0] for p, _ in runs}) == 3                 # three first sentences
    assert len({tuple(order) for _, order in runs}) > 1                 # the claim order moves
    fixed = [build_prompt_v2(items, run, 7, fixed=True) for run in range(3)]
    assert len({p for p, _ in fixed}) == 1 and fixed[0] == runs[0]      # the control: identical bytes


def test_world_claim_has_no_pack_and_no_quote_needed():
    items = [("Python lists are ordered.", None)]
    prompt, order = build_prompt_v2(items, 0, 1)
    block = prompt.split("=== CLAIM 1 ===", 1)[1]
    assert judge.WORLD_BLOCK in block and "EVIDENCE" not in block
    votes, rejected = verify_quotes({0: Vote("TRUE")}, [None])
    assert votes == {0: Vote("TRUE")} and rejected == []
    votes, rejected = verify_quotes({0: Vote("FALSE", "src:nowhere", "whatever")}, {})
    assert votes[0].verdict == "FALSE" and rejected == []          # an index with no pack is a world claim


# ------------------------------------------------------------------ the parser

ROW = '{"id": 1, "verdict": "TRUE", "chunk": "src:app.py:10-11", "quote": "check=False"}'


@pytest.mark.parametrize("reply", [
    f"[{ROW}]",
    f"```json\n[{ROW}]\n```",
    f"```\n[{ROW}]\n```",
    f"Here are my verdicts:\n[{ROW}]\nHope this helps.",
    f"[{ROW},]",
    f"[{ROW.replace('}', ',}')}]",
    f"[\n  {ROW},\n]\n",
    f"[{ROW.replace('TRUE', 'true')}]",
    f"[{ROW.replace('TRUE', ' True ')}]",
    f"[{ROW.replace('1,', chr(34) + '1' + chr(34) + ',', 1)}]",
    f"[{ROW.replace('1,', chr(34) + 'CLAIM-1' + chr(34) + ',', 1)}]",
    f"<think>see [[src:app.py:10-11]] and [1]</think> the answer: [{ROW}]",
])
def test_parse_tolerates_fences_prose_trailing_comma(reply):
    got = parse_votes_v2(reply, [4])
    assert got == {4: Vote("TRUE", "src:app.py:10-11", "check=False")}


def test_parse_drops_unknown_verdicts_and_bad_ids():
    reply = json.dumps([
        {"id": 1, "verdict": "MAYBE", "chunk": "a", "quote": "b"},
        {"id": 0, "verdict": "TRUE"}, {"id": 4, "verdict": "TRUE"}, {"id": "x", "verdict": "TRUE"},
        {"id": True, "verdict": "TRUE"}, {"verdict": "TRUE"}, "not a row",
        {"id": 2, "verdict": "FALSE", "chunk": 7, "quote": None},
        {"id": 3, "verdict": "unsure"},
    ])
    assert parse_votes_v2(reply, [10, 11, 12]) == {11: Vote("FALSE"), 12: Vote("UNSURE")}
    for garbage in ("", "no json at all", "[1, 2, 3]", "{\"id\": 1}", None, "[{\"id\": 1, \"verdict\":"):
        assert parse_votes_v2(garbage, [0]) == {}


# ------------------------------------------------------------------ the quote check

CLAIM = "`run_step` in `app.py` calls subprocess.run with check=False."


@pytest.mark.parametrize("chunk_id, quote", [
    ("src:app.py:10-11", "return subprocess.run(cmd, check=False)"),                 # exact
    ("src:app.py:10-11", "return   subprocess.run(cmd,\n check=False)"),             # whitespace differs
    ("src:app.py:10-11", "11|     return subprocess.run(cmd, check=False)"),         # gutter pasted
    ("src:app.py:10-11", "10| def run_step(cmd):\n11|     return subprocess.run(cmd,"),
    ("[[src:app.py:10-11]]", "subprocess.run(cmd, check=False)"),                    # brackets copied
    ("[src:app.py:10-11]", "subprocess.run(cmd, check=False)"),
    ("`src:app.py:10-11`", "subprocess.run(cmd, check=False)"),
    ("[[src:app.py:10-11]]  (source)", "subprocess.run(cmd, check=False)"),          # the label tail
    ("git:abc1234:app.py:10-11", "subprocess.run(cmd, check=False)"),                # the lower-ranked twin
])
def test_quote_in_the_named_chunk_is_accepted(chunk_id, quote):
    pack = pack_of(CLAIM, RUN_FALSE, HUNK, RUN_TRUE)
    votes, rejected = verify_quotes({0: Vote("TRUE", chunk_id, quote)}, [pack])
    assert rejected == []
    assert votes[0].verdict == "TRUE"
    assert votes[0].chunk in {c.id for c in pack.chunks}           # the pack's own spelling


def test_quote_from_another_chunk_is_downgraded():
    pack = pack_of(CLAIM, RUN_FALSE, RUN_TRUE)
    votes, rejected = verify_quotes(
        {0: Vote("FALSE", "src:app.py:10-11", "subprocess.run(cmd, check=True)"),   # in the pack, not in that chunk
         1: Vote("TRUE", "src:other.py:1-9", "subprocess.run(cmd, check=False)")},  # no such chunk
        [pack, pack])
    assert {i: v.verdict for i, v in votes.items()} == {0: "UNSURE", 1: "UNSURE"}
    assert [(r["claim"], r["reason"]) for r in rejected] == [(0, QUOTE_NOT_IN_PACK), (1, CHUNK_NOT_IN_PACK)]
    # a chunk of *another* claim's pack is not in this claim's pack
    other = pack_of("other", RUN_TRUE)
    _v, rejected = verify_quotes({0: Vote("TRUE", RUN_FALSE.id, "subprocess.run(cmd, check=False)")}, [other])
    assert rejected[0]["reason"] == CHUNK_NOT_IN_PACK


def test_fabricated_quote_is_downgraded():
    pack = pack_of(CLAIM, RUN_FALSE)
    fabricated = ["subprocess.run(cmd, check=False, timeout=30)",   # sounds right, is not there
                  "return subprocess.check_call(cmd)",
                  CLAIM,                                           # the claim's own text as the quote
                  "def run_step(cmd): return subprocess.run(cmd, check=False) # and more"]
    votes = {i: Vote("TRUE", RUN_FALSE.id, q) for i, q in enumerate(fabricated)}
    out, rejected = verify_quotes(votes, [pack] * len(fabricated))
    assert all(v.verdict == "UNSURE" for v in out.values())
    assert len(rejected) == len(fabricated)                        # 100 % of the injected ones
    assert {r["reason"] for r in rejected} == {QUOTE_NOT_IN_PACK}
    assert judge.rejected_counts(rejected) == {QUOTE_NOT_IN_PACK: len(fabricated)}


def test_short_quote_is_downgraded():
    pack = pack_of(CLAIM, RUN_FALSE)
    for quote in ("", "check", "  run(  ", "return"):            # under 8 characters once normalised
        out, rejected = verify_quotes({0: Vote("TRUE", RUN_FALSE.id, quote)}, [pack])
        assert out[0].verdict == "UNSURE" and rejected[0]["reason"] == QUOTE_NOT_IN_PACK
    out, rejected = verify_quotes({0: Vote("TRUE", RUN_FALSE.id, "run_step")}, [pack])   # 8: enough
    assert out[0].verdict == "TRUE" and rejected == []


def test_dangling_false_with_the_note_quote_is_accepted():
    text = "`Store.compact` is not there; the file `store.py` exists and defines: Store, Store.load"
    dangling = note("note:dangling:Store.compact", text)
    pack = assemble_pack("`Store.compact` in `store.py` drops deleted rows.", [],
                         [dangling, src("store.py", 1, ["class Store:", "    def load(self): ..."])])
    assert pack.chunks[0].id == "note:dangling:Store.compact"       # notes rank above the code
    out, rejected = verify_quotes(
        {0: Vote("FALSE", "note:dangling:Store.compact", "`Store.compact` is not there")}, [pack])
    assert out[0].verdict == "FALSE" and rejected == []


def test_unsure_needs_no_quote():
    pack = pack_of(CLAIM, RUN_FALSE)
    empty = Pack("x", "", (), False)
    out, rejected = verify_quotes({0: Vote("UNSURE"), 1: Vote("UNSURE", "junk", "junk"),
                                   2: Vote("UNSURE")}, [pack, pack, empty])
    assert {i: v.verdict for i, v in out.items()} == {0: "UNSURE", 1: "UNSURE", 2: "UNSURE"}
    assert rejected == []
    # an empty pack: a TRUE with no quote cannot stand
    out, rejected = verify_quotes({0: Vote("TRUE")}, [empty])
    assert out[0].verdict == "UNSURE" and rejected[0]["reason"] == CHUNK_NOT_IN_PACK


def test_quote_with_a_literal_pipe_and_across_two_chunks():
    pipe = src("cli.py", 5, ['    flags = a | b  # either', "    return flags"])
    pack = pack_of("`flags` is a | b.", pipe, RUN_FALSE)
    out, rejected = verify_quotes({0: Vote("TRUE", pipe.id, "flags = a | b  # either")}, [pack])
    assert out[0].verdict == "TRUE" and rejected == []
    across = "return flags def run_step(cmd):"                     # the end of one chunk, the head of the next
    out, rejected = verify_quotes({0: Vote("TRUE", pipe.id, across)}, [pack])
    assert rejected[0]["reason"] == QUOTE_NOT_IN_PACK


def test_find_all_lists_every_chunk_that_holds_the_quote():
    pack = pack_of(CLAIM, RUN_FALSE, HUNK, RUN_TRUE)
    assert pack.find_all("subprocess.run(cmd, check=False)") == [RUN_FALSE.id, HUNK.id]
    assert pack.find("subprocess.run(cmd, check=False)") == RUN_FALSE.id
    assert pack.find_all("nowhere at all") == [] and pack.find_all("x") == []


# ------------------------------------------------------------------ fake voters, end to end

_BLOCK = re.compile(r"=== CLAIM (\d+) ===\n(.*?)(?=\n\n=== CLAIM \d+ ===|\Z)", re.S)
_LABEL = re.compile(r"^\[\[(.+?)\]\]  \(\w+\)$", re.M)


def _blocks(prompt: str):
    """(n, claim block text) for every claim the prompt shows."""
    return [(int(m.group(1)), m.group(2)) for m in _BLOCK.finditer(prompt)]


def _chunk_with(block: str, needle: str):
    """The id of the chunk in *block* whose text holds *needle*, else None."""
    labels = list(_LABEL.finditer(block))
    for k, m in enumerate(labels):
        end = labels[k + 1].start() if k + 1 < len(labels) else len(block)
        if needle in block[m.end():end]:
            return m.group(1)
    return None


def reader(lie: "set[str]" = frozenset(), bad_chunk: "set[str]" = frozenset()):
    """A fake voter that answers from the pack text: TRUE iff it holds `check=False`, quoting
    that line from its chunk. A claim whose text is in *lie* gets a fabricated quote, one in
    *bad_chunk* names a chunk that is not in the pack."""
    def complete(prompt: str) -> str:
        rows = []
        for n, block in _blocks(prompt):
            claim, block = block.split("\n", 1)       # the claim's own words are not evidence
            if judge.WORLD_BLOCK in block:
                rows.append({"id": n, "verdict": "TRUE"})
                continue
            needle, verdict = ("check=False", "TRUE") if "check=False" in block else ("check=True", "FALSE")
            cid = _chunk_with(block, needle)
            if cid is None:
                rows.append({"id": n, "verdict": "UNSURE"})
                continue
            line = next(ln for ln in block.splitlines() if needle in ln).split("| ", 1)[-1].strip()
            if claim in lie:
                line = line.replace(needle, needle + ", timeout=30")
            if claim in bad_chunk:
                cid = "src:made_up.py:1-9"
            rows.append({"id": n, "verdict": verdict, "chunk": f"[[{cid}]]", "quote": line})
        return "```json\n" + json.dumps(rows) + "\n```"
    return complete


def _items():
    claims = [f"`step{i}` in `app.py` passes check=False to subprocess.run." for i in range(4)]
    items = []
    for i, claim in enumerate(claims):
        body = RUN_FALSE if i % 2 == 0 else RUN_TRUE
        items.append((claim, assemble_pack(claim, [], [body], sha="abc1234")))
    items.append(("The Python standard library includes subprocess.", None))
    return items


def _run(voters: dict, items) -> tuple:
    results = []
    for model, fn in voters.items():
        for run in range(3):
            got = ask_with_packs(items, run, 1, fn, batch=10, code_batch=2)
            results.append({"model": model, "run": run, **got})
    meta = [{"kind": "world" if p is None else "code", "dangling": False, "sha": "abc1234"}
            for _c, p in items]
    claims = [{"id": f"c{i}", "claim": c, "truth": None} for i, (c, _p) in enumerate(items)]
    return cv.tally(claims, results, meta=meta), results


def test_end_to_end_with_fake_voters():
    items = _items()
    table, results = _run({"a/m": reader(), "b/m": reader(), "c/m": reader()}, items)
    assert [t["verdict"] for t in table] == ["TRUE", "FALSE", "TRUE", "FALSE", "TRUE"]
    assert all(t["unanimous"] for t in table)
    assert table[0]["evidence"] == [RUN_FALSE.id] and table[1]["evidence"] == [RUN_TRUE.id]
    assert table[0]["quotes"] == ["return subprocess.run(cmd, check=False)"]
    assert table[4]["evidence"] == [] and table[4]["kind"] == "world"
    assert all(t["downgrades"] == 0 and t["rejected"] == {} for t in table)
    assert table[0]["id"] == "c0" and table[0]["sha"] == "abc1234"
    assert all(r["rejected"] == [] for r in results)

    lying = items[0][0]
    table, results = _run({"a/m": reader(), "b/m": reader(), "c/m": reader(lie={lying})}, items)
    assert table[0]["unanimous"] is False                          # one fabricates: not unanimous
    assert table[0]["by_model"]["c/m"] == "UNSURE"
    assert table[0]["rejected"] == {QUOTE_NOT_IN_PACK: 3}           # three runs of the liar
    assert len([r for r in results if r["model"] == "c/m" and r["rejected"]]) == 3
    assert sum(len(r["rejected"]) for r in results if r["run"] == 0) == 1   # one per voter per run
    assert all(t["unanimous"] for t in table[1:])

    table, _results = _run({"a/m": reader(), "b/m": reader(bad_chunk={lying}), "c/m": reader()}, items)
    assert table[0]["unanimous"] is False                          # a chunk that is not there: the same
    assert table[0]["rejected"] == {CHUNK_NOT_IN_PACK: 3}
    assert table[0]["downgrades"] == 3


def test_lazy_always_true_voter_is_caught_on_code_claims():
    def lazy(prompt: str) -> str:
        return json.dumps([{"id": n, "verdict": "TRUE", "chunk": "", "quote": ""}
                           for n, _b in _blocks(prompt)])
    items = _items()
    got = ask_with_packs(items, 0, 1, lazy)
    code = [i for i, (_c, p) in enumerate(items) if p is not None]
    assert all(got["votes"][i] == "UNSURE" for i in code)
    assert len(got["rejected"]) == len(code)
    assert got["votes"][4] == "TRUE"                               # a world claim needs no quote


def test_batching_and_a_dead_voter():
    items = _items() + [(f"world fact {i}.", None) for i in range(11)]
    seen = []

    def log(prompt: str) -> str:
        blocks = _blocks(prompt)
        seen.append((len(blocks), sum(judge.WORLD_BLOCK in b for _n, b in blocks)))
        return reader()(prompt)
    got = ask_with_packs(items, 0, 1, log, batch=10, code_batch=2)
    assert seen == [(10, 10), (2, 2), (2, 0), (2, 0)]               # world 10 + 2, code 2 + 2
    assert len(got["votes"]) == len(items)

    def dead(_prompt: str) -> str:
        raise TimeoutError("provider timed out")
    got = ask_with_packs(items, 0, 1, dead)
    assert got["votes"] == {} and got["errors"][0] == "provider timed out"
    got = ask_with_packs(items, 0, 1, lambda _p: "I cannot help with that.")
    assert got["votes"] == {} and got["raw_head"].startswith("I cannot")


def test_four_voters_two_of_one_family_still_need_all_four():
    items = _items()
    table, _r = _run({"fam1/a": reader(), "fam1/b": reader(), "fam2/c": reader(),
                      "fam3/d": reader(lie={items[2][0]})}, items)
    assert table[0]["unanimous"] is True and table[2]["unanimous"] is False
