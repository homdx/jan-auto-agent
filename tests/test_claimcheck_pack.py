"""CC-5 (275): the pack — rank, merge, trim, render, and find a quote."""

from __future__ import annotations

import configparser
from dataclasses import replace
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.claimcheck import evidence_git, pack as pack_mod
from tools.claimcheck.anchors import PathRepoView, extract_anchors, resolve_anchors
from tools.claimcheck.model import Anchor, Chunk, Pack, ResolvedAnchor
from tools.claimcheck.pack import PackBudget, assemble_pack, build_pack, find_quote, render_pack

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "claimcheck" / "pack_golden"
_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Ann", "GIT_AUTHOR_EMAIL": "ann@example.com",
        "GIT_COMMITTER_NAME": "Ann", "GIT_COMMITTER_EMAIL": "ann@example.com",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00"}


# ------------------------------------------------------------------ helpers

def write(root: Path, files: dict) -> PathRepoView:
    for rel, content in files.items():
        full = root / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8", newline="")
    return PathRepoView(root)


def git_repo(root: Path, files: dict) -> tuple:
    """A one-commit repository over *files*: (its view, the commit's full sha)."""
    view = write(root, files)
    for args in (("init", "-q", "-b", "main"), ("add", "-A"), ("commit", "-q", "-m", "files")):
        subprocess.run(["git", "-C", str(root), *args], env=_ENV, check=True, capture_output=True)
    sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], env=_ENV, check=True,
                         capture_output=True, text=True).stdout.strip()
    return view, sha


def chunk(cid: str, kind: str, text: str, *, path: str = "", start: int = 0, end: int = 0,
          why: str = "") -> Chunk:
    return Chunk(cid, kind, path, start, end, text, why)


def src_chunk(path: str, a: int, b: int, shown=None, why: str = "x", text_of=None) -> Chunk:
    """A `src:` chunk the way CC-3 prints it: file numbers, `# … N lines omitted (a-b)` for a gap."""
    text_of = text_of or (lambda n: f"line {n}")
    shown = shown or [(a, b)]
    width = len(str(b))
    out, at = [], a
    for x, y in shown:
        if x > at:
            out.append(f"# … {x - at} lines omitted ({at}-{x - 1})")
        out.extend(f" {n:>{width}}| {text_of(n)}" for n in range(x, y + 1))
        at = y + 1
    if at <= b:
        out.append(f"# … {b - at + 1} lines omitted ({at}-{b})")
    return Chunk(f"src:{path}:{a}-{b}", "source", path, a, b, "\n".join(out), why)


def hunk_chunk(path: str, a: int, rows: list, sha7: str = "a73e389", why: str = "x") -> Chunk:
    """A `git:` hunk: the diff's lines behind the new side's number, blank for a removed line."""
    width = len(str(a + len(rows)))
    out, n = ["diff --git a/f b/f", "--- a/f", "+++ b/f", f"@@ -1,2 +{a},{len(rows)} @@"], a
    for row in rows:
        if row.startswith("-"):
            out.append(f" {'':>{width}}| {row}")
        else:
            out.append(f" {n:>{width}}| {row}")
            n += 1
    return Chunk(f"git:{sha7}:{path}:{a}-{n - 1}", "git", path, a, n - 1, "\n".join(out), why)


def found(kind: str, text: str, path: str, qualname: str = "", lines=(0, 0)) -> ResolvedAnchor:
    return ResolvedAnchor(Anchor(kind, text, 0, len(text)), True, path=path, qualname=qualname, lines=lines)


def missing(kind: str, text: str, path: str = "") -> ResolvedAnchor:
    return ResolvedAnchor(Anchor(kind, text, 0, len(text)), False, path=path)


def ids(pack: Pack) -> list:
    return [c.id for c in pack.chunks]


POLICY = '''"""Policy."""


class Policy:
    """Decides."""

    def decide(self, item):
        if item.size > 10:
            return "big"
        return "small"

    def _mechanical_pass(self, item):
        return item.check(strict=True)


def helper(x):
    return x + 1
'''


def pack_for(view, claim: str, **kw) -> Pack:
    return build_pack(claim, resolve_anchors(extract_anchors(claim), view), view, **kw)


# ------------------------------------------------------------------ budget

def _random_chunks(rng: random.Random) -> list:
    out = []
    for i in range(rng.randint(1, 14)):
        kind = rng.choice(["note", "source", "source", "git", "git", "ticket", "collect"])
        lines = [("x" * rng.randint(0, 150)) for _ in range(rng.randint(1, 60))]
        if kind == "source":
            width = len(str(len(lines)))
            text = "\n".join(f" {n:>{width}}| {line}" for n, line in enumerate(lines, 1))
            out.append(chunk(f"src:f{i}.py:1-{len(lines)}", "source", text, path=f"f{i}.py",
                             start=1, end=len(lines), why="w"))
        elif kind == "git":
            if rng.random() < 0.5:
                out.append(chunk(f"git:abc1234:f{i}.py:1-{len(lines)}", "git", "\n".join(lines),
                                 path=f"f{i}.py", start=1, end=len(lines)))
            else:
                out.append(chunk(f"gitlog:f{i}.py", "git", "\n".join(lines), path=f"f{i}.py"))
        else:
            out.append(chunk(f"{kind}:{i}", kind, "\n".join(lines)))
    return out


def test_budget_is_never_exceeded():
    rng = random.Random(275)
    for _ in range(200):
        budget = PackBudget(chars=rng.randint(300, 8000), chunks=rng.randint(1, 8),
                            per_chunk=rng.randint(100, 3000))
        got = assemble_pack("The `thing_one` is `thing_two`.", [], _random_chunks(rng), sha="3f2a9c1" * 5,
                            budget=budget)
        assert len(got.chunks) <= budget.chunks
        assert len(got.render()) <= budget.chars, (budget, len(got.render()))


def test_a_budget_too_small_for_a_label_and_a_line_still_shows_the_top_chunk():
    rng = random.Random(7)
    for _ in range(50):
        budget = PackBudget(chars=rng.randint(20, 300), chunks=3, per_chunk=rng.randint(10, 500))
        got = assemble_pack("c", [], _random_chunks(rng), sha="3f2a9c1", budget=budget)
        assert len(got.chunks) >= 1
        assert len(got.render()) <= budget.chars + 300      # the title, the label and a line of the top chunk


def test_budget_of_one_chunk():
    chunks = [chunk(f"note:{i}", "note", f"text number {i}") for i in range(3)]
    got = assemble_pack("c", [], chunks, sha="3f2a9c1", budget=PackBudget(chunks=1))
    assert ids(got) == ["note:0"] and got.truncated and (got.omitted, got.cut) == (2, 0)
    assert got.render().endswith("(evidence trimmed: 2 chunks omitted)")


def test_nothing_trimmed_means_no_trailer_and_not_truncated():
    got = assemble_pack("c", [], [chunk("note:a", "note", "short text")], sha="3f2a9c1")
    assert not got.truncated and "trimmed" not in got.render()


# ------------------------------------------------------------------ rank

def _one_of_each() -> tuple:
    resolved = [found("symbol", "Policy.decide", "tools/policy.py", "Policy.decide", (7, 10)),
                found("path", "tools/named.py", "tools/named.py")]
    chunks = [
        chunk("gitlog:tools/named.py", "git", "git log -n 5 -- tools/named.py", path="tools/named.py"),
        chunk("collect:callers:tools/policy.py", "collect", "caller: a.py", path="tools/policy.py"),
        chunk("ticket:275", "ticket", "# 275 — CC-5"),
        src_chunk("tools/named.py", 1, 3, why="tools/named.py"),
        hunk_chunk("tools/other.py", 4, [" a", "+b"], sha7="1111111"),
        hunk_chunk("tools/named.py", 4, [" a", "+b"], sha7="2222222"),
        src_chunk("tools/policy.py", 7, 10, why="Policy.decide"),
        chunk("note:dangling:Policy._mechanical", "note", "does not exist"),
    ]
    return resolved, chunks


@pytest.mark.parametrize("claim", ["The thing works.", "The `alpha_beta` works."])
def test_rank_order_table(claim):
    resolved, chunks = _one_of_each()
    if "alpha_beta" in claim:       # the same keyword in every chunk: the same bonus, the same order
        chunks = [replace(c, text=c.text + "\nalpha_beta") for c in chunks]
    got = assemble_pack(claim, resolved, chunks, sha="3f2a9c1", budget=PackBudget(chars=20000, chunks=20))
    assert ids(got) == [
        "note:dangling:Policy._mechanical",       # 110
        "src:tools/policy.py:7-10",               # 100  source of a symbol anchor
        "git:2222222:tools/named.py:4-5",         # 90   hunk of a file the claim names
        "git:1111111:tools/other.py:4-5",         # 70   hunk of a file it does not
        "src:tools/named.py:1-3",                 # 60   source of a path anchor
        "ticket:275",                             # 50
        "collect:callers:tools/policy.py",        # 40
        "gitlog:tools/named.py",                  # 30
    ]


def test_every_kind_of_note_ranks_as_a_dangling_note():
    got = assemble_pack("c", [], [src_chunk("a.py", 1, 2, why="X"), chunk("note:git:abc", "note", "no commit")],
                        sha="3f2a9c1")
    assert ids(got)[0] == "note:git:abc"


def test_keyword_bonus_and_cap():
    claim = "`k_one`, `k_two`, `k_three`, `k_four` and `k_five` are set."
    tokens = pack_mod.keyword_tokens(claim)
    assert {"k_one", "k_two", "k_three", "k_four", "k_five"} <= set(tokens)

    def score(text: str, kind: str = "collect") -> int:
        return pack_mod._score(chunk("c:1", kind, text), tokens, set(), set())

    assert score("nothing here") == 40
    assert score("k_one k_two k_three") == 40 + 30
    assert score("k_one k_one k_one") == 40 + 10                 # distinct keywords, not hits
    assert score("k_one k_two k_three k_four k_five") == 40 + 40  # five keywords: +40, not +50
    assert score("k_one k_two k_three k_four k_five " * 3) == 40 + 40
    # a lower kind with keywords passes a higher one only as the scores say:
    resolved = [found("symbol", "S.f", "s.py", "S.f", (1, 2))]
    five = chunk("collect:1", "collect", "k_one k_two k_three k_four k_five")          # 40 + 40 = 80
    ticket = chunk("ticket:1", "ticket", "no keyword at all")                          # 50
    source = src_chunk("s.py", 1, 2, why="S.f")                                        # 100
    got = assemble_pack(claim, resolved, [ticket, five, source], sha="", budget=PackBudget(chars=9000))
    assert ids(got) == ["src:s.py:1-2", "collect:1", "ticket:1"]


def test_the_gutter_is_not_text_for_the_keyword_bonus():
    tokens = ["120"]
    gutter_only = src_chunk("a.py", 118, 122, text_of=lambda n: "plain")        # ` 120| plain`
    real = src_chunk("b.py", 1, 2, text_of=lambda n: "limit = 120")
    assert pack_mod._score(gutter_only, tokens, set(), set()) == pack_mod.SCORE_PATH_SOURCE
    assert pack_mod._score(real, tokens, set(), set()) == pack_mod.SCORE_PATH_SOURCE + 10


def test_ties_break_by_id():
    chunks = [chunk("note:b", "note", "same"), chunk("note:c", "note", "same"), chunk("note:a", "note", "same")]
    first = assemble_pack("c", [], chunks, sha="3f2a9c1")
    again = assemble_pack("c", [], list(reversed(chunks)), sha="3f2a9c1")
    assert ids(first) == ["note:a", "note:b", "note:c"] == ids(again)


# ------------------------------------------------------------------ merge

def _file_view(tmp_path: Path) -> PathRepoView:
    return write(tmp_path, {"a.py": "".join(f"line {n}\n" for n in range(1, 41))})


def test_overlapping_ranges_merge(tmp_path):
    view = _file_view(tmp_path)
    got = assemble_pack("c", [], [src_chunk("a.py", 10, 20, why="one"), src_chunk("a.py", 18, 30, why="two")],
                        view, sha="3f2a9c1")
    assert ids(got) == ["src:a.py:10-30"]
    assert got.chunks[0].text == "\n".join(f" {n}| line {n}" for n in range(10, 31))
    assert (got.chunks[0].start, got.chunks[0].end, got.chunks[0].why) == (10, 30, "one, two")
    assert not got.truncated


def test_touching_ranges_merge(tmp_path):
    view = _file_view(tmp_path)
    got = assemble_pack("c", [], [src_chunk("a.py", 10, 20), src_chunk("a.py", 21, 30)], view, sha="3f2a9c1")
    assert ids(got) == ["src:a.py:10-30"] and len(got.chunks[0].text.split("\n")) == 21


def test_ranges_with_a_line_between_do_not_merge(tmp_path):
    view = _file_view(tmp_path)
    got = assemble_pack("c", [], [src_chunk("a.py", 10, 20), src_chunk("a.py", 22, 30)], view, sha="3f2a9c1")
    assert sorted(ids(got)) == ["src:a.py:10-20", "src:a.py:22-30"]


def test_a_merge_keeps_the_gap_neither_chunk_shows(tmp_path):
    view = _file_view(tmp_path)
    first = src_chunk("a.py", 10, 20, shown=[(10, 12)])        # 13-20 omitted
    second = src_chunk("a.py", 18, 30)
    got = assemble_pack("c", [], [first, second], view, sha="3f2a9c1")
    lines = got.chunks[0].text.split("\n")
    assert lines[:3] == [" 10| line 10", " 11| line 11", " 12| line 12"]
    assert lines[3] == "# … 5 lines omitted (13-17)" and lines[4] == " 18| line 18"
    assert lines[-1] == " 30| line 30"


def test_a_merge_pads_the_gutter_to_the_merged_end(tmp_path):
    view = write(tmp_path, {"a.py": "".join(f"line {n}\n" for n in range(1, 120))})
    got = assemble_pack("c", [], [src_chunk("a.py", 95, 99), src_chunk("a.py", 98, 101)], view, sha="3f2a9c1")
    assert got.chunks[0].text.split("\n")[0] == "  95| line 95"          # width of 101


def test_git_chunks_never_merge_with_source_or_each_other(tmp_path):
    view = _file_view(tmp_path)
    chunks = [src_chunk("a.py", 10, 20), hunk_chunk("a.py", 10, ["+a"] * 10, sha7="1111111"),
              hunk_chunk("a.py", 15, ["+b"] * 10, sha7="2222222")]
    got = assemble_pack("c", [], chunks, view, sha="3f2a9c1")
    assert len(got.chunks) == 3


def test_chunks_that_cannot_be_read_back_stay_as_they_are(tmp_path):
    one, two = src_chunk("gone.py", 10, 20), src_chunk("gone.py", 18, 30)
    got = assemble_pack("c", [], [one, two], PathRepoView(tmp_path), sha="3f2a9c1")
    assert sorted(ids(got)) == ["src:gone.py:10-20", "src:gone.py:18-30"]
    assert len(assemble_pack("c", [], [one, two], None, sha="3f2a9c1").chunks) == 2     # no view at all
    big = write(tmp_path, {"big.py": "x = 1\n" * 100_000})                                # over the 400 KB cap
    two_big = [src_chunk("big.py", 1, 5), src_chunk("big.py", 4, 9)]
    assert len(assemble_pack("c", [], two_big, big, sha="3f2a9c1").chunks) == 2


def test_the_same_id_twice_is_one_chunk():
    got = assemble_pack("c", [], [chunk("note:a", "note", "first"), chunk("note:a", "note", "second")], sha="")
    assert len(got.chunks) == 1 and got.chunks[0].text == "first"


# ------------------------------------------------------------------ trim

def test_top_chunk_is_cut_not_dropped():
    big = src_chunk("a.py", 1, 400, text_of=lambda n: f"statement_number_{n} = {n}")      # ~12 KB
    assert len(big.text) > 10_000
    got = assemble_pack("c", [], [big], sha="3f2a9c1")
    only = got.chunks[0]
    assert len(got.chunks) == 1 and got.truncated and (got.omitted, got.cut) == (0, 1)
    assert len(only.text) <= 2400
    last_shown = int(only.text.split("\n")[-2].split("|")[0])
    assert only.text.split("\n")[-1] == f"# … {400 - last_shown} lines omitted ({last_shown + 1}-400)"
    assert got.render().endswith("(evidence trimmed: 1 chunk cut)")
    tight = assemble_pack("c", [], [big], sha="3f2a9c1", budget=PackBudget(chars=500))
    assert len(tight.chunks) == 1 and len(tight.render()) <= 500 and tight.cut == 1


def test_a_hunk_is_cut_with_a_diff_marker():
    rows = [f"+    row_{n} = {n}" for n in range(300)]
    got = assemble_pack("c", [], [hunk_chunk("a.py", 1, rows)], sha="3f2a9c1")
    text = got.chunks[0].text
    assert len(text) <= 2400 and text.split("\n")[-1].endswith("diff lines cut") and got.cut == 1


def test_a_plain_chunk_is_cut_with_a_line_marker():
    got = assemble_pack("c", [], [chunk("ticket:1", "ticket", "\n".join(f"paragraph {n} " * 4 for n in range(300)))],
                        sha="3f2a9c1")
    assert got.chunks[0].text.split("\n")[-1].endswith("lines omitted") and len(got.chunks[0].text) <= 2400


def test_the_next_chunk_that_does_not_fit_ends_the_pack():
    a = chunk("note:a", "note", "a" * 400)
    b = chunk("note:b", "note", "b" * 400)
    c = chunk("note:c", "note", "c" * 10)
    got = assemble_pack("x", [], [a, b, c], sha="3f2a9c1", budget=PackBudget(chars=800, per_chunk=2400))
    assert ids(got) == ["note:a"] and got.omitted == 2        # `c` would fit, but `b` ended the list


def test_a_line_over_2000_characters_is_cut_and_the_numbering_stays():
    big = src_chunk("a.py", 10, 14, text_of=lambda n: ("y" * 5000) if n == 12 else f"statement {n}")
    got = assemble_pack("c", [], [big], sha="3f2a9c1", budget=PackBudget(per_chunk=9000, chars=20000))
    lines = got.chunks[0].text.split("\n")
    assert lines[2] == " 12| " + "y" * 2000 and lines[3] == "# … 3000 chars cut"
    assert lines[4] == " 13| statement 13" and lines[1] == " 11| statement 11"
    assert got.truncated and got.cut == 1
    assert find_quote(got, " 13| statement 13") == big.id and find_quote(got, "# … 3000 chars cut") is None


# ------------------------------------------------------------------ notes

def test_dangling_anchor_becomes_a_note(tmp_path):
    view, sha = git_repo(tmp_path, {"tools/policy.py": POLICY})
    got = pack_for(view, "`Policy.decide` returns big, and `Policy._mechanical` never calls check.")
    note = next(c for c in got.chunks if c.id == "note:dangling:Policy._mechanical")
    assert note.kind == "note"
    assert note.text == (f"`Policy._mechanical` does not exist at {sha[:7]}; the file `tools/policy.py` "
                         "exists and defines: `Policy`, `helper`; `Policy` has: `decide`, `_mechanical_pass`.")
    assert got.chunks[0] is note                      # a note outranks the source it sits beside
    assert got.sha == sha


def test_the_note_lists_at_most_ten_names(tmp_path):
    body = "".join(f"def fn_{n:02d}():\n    return {n}\n\n\n" for n in range(13))
    view, _ = git_repo(tmp_path, {"tools/many.py": body})
    got = pack_for(view, "The caller reaches `tools/many.py::gone_fn` directly.")
    text = next(c.text for c in got.chunks if c.id.startswith("note:dangling:tools/many.py::gone_fn"))
    assert text.count("`fn_") == 10 and text.endswith("`fn_09`, … 3 more.")


def test_the_note_reuses_the_definition_list_a_chunk_carries(tmp_path, monkeypatch):
    view, sha = git_repo(tmp_path, {"tools/policy.py": POLICY})
    carried = chunk("src:tools/policy.py:1-16", "source",
                    "  1| \"\"\"Policy.\"\"\"\n# definitions (name  line):\n#   Policy  4\n#   helper  15\n#   … 7 more",
                    path="tools/policy.py", start=1, end=16, why="tools/policy.py")
    reads = []
    real_read = view.read
    monkeypatch.setattr(view, "read", lambda p: (reads.append(p), real_read(p))[1])
    notes = pack_mod._dangling_notes([missing("symbol", "Policy.gone", "tools/policy.py")], sha, [carried], view)
    assert "defines: `Policy`, `helper`, … 7 more." in notes[0].text and reads == []


def test_note_for_missing_file(tmp_path):
    view, sha = git_repo(tmp_path, {"tools/policy.py": POLICY})
    got = pack_for(view, "`tools/gone.py` still exists.")
    assert ids(got) == ["note:dangling:tools/gone.py"]
    assert got.chunks[0].text == (f"`tools/gone.py` does not exist at {sha[:7]}; "
                                  "the directory `tools` exists and holds: `policy.py`.")


def test_note_for_a_bare_file_name_the_repository_has_no_file_of(tmp_path):
    view, sha = git_repo(tmp_path, {"tools/policy.py": POLICY})
    note = pack_mod._dangling_notes([missing("path", "other.py", ".")], sha, [], view)[0]
    assert note.text == f"`other.py` does not exist at {sha[:7]}; no file of that name is in the repository."


def test_an_ambiguous_file_name_is_a_note_that_says_so(tmp_path):
    view, sha = git_repo(tmp_path, {"a/gates.py": "x = 1\n", "b/gates.py": "y = 2\n"})
    ra = ResolvedAnchor(Anchor("path", "gates.py", 0, 8), False, path="a/gates.py",
                        candidates=("a/gates.py", "b/gates.py"))
    note = pack_mod._dangling_notes([ra], sha, [], view)[0]
    assert note.id == "note:ambiguous:gates.py" and "`a/gates.py`, `b/gates.py`" in note.text


def test_a_name_outside_our_code_is_no_note(tmp_path):
    view, _ = git_repo(tmp_path, {"tools/policy.py": POLICY})
    assert pack_mod._dangling_notes([missing("symbol", "requests.get", "")], "x" * 40, [], view) == []
    assert ids(pack_for(view, "requests.get returns a response.")) == []


def test_no_sha_says_in_the_repository(tmp_path):
    view = write(tmp_path, {"tools/policy.py": POLICY})             # a plain directory: no commit
    note = pack_mod._dangling_notes([missing("symbol", "Policy.gone", "tools/policy.py")], "", [], view)[0]
    assert note.text.startswith("`Policy.gone` does not exist in the repository; the file")


# ------------------------------------------------------------------ render

def _golden_cases() -> list:
    return json.loads((GOLDEN / "cases.json").read_text(encoding="utf-8"))


def _golden_pack(case: dict) -> Pack:
    resolved = [ResolvedAnchor(Anchor(r["kind"], r["text"], 0, len(r["text"])), r["found"], path=r["path"],
                               qualname=r["qualname"], lines=tuple(r["lines"])) for r in case["resolved"]]
    chunks = [Chunk(**c) for c in case["chunks"]]
    return assemble_pack(case["claim"], resolved, chunks, sha=case["sha"], budget=PackBudget(**case["budget"]))


def test_render_format_golden():
    cases = _golden_cases()
    assert [c["name"] for c in cases] == ["mixed", "trimmed", "empty"]
    for case in cases:
        want = (GOLDEN / f"{case['name']}.txt").read_text(encoding="utf-8")
        assert _golden_pack(case).render() == want.rstrip("\n"), case["name"]


if __name__ == "__main__" and sys.argv[1:] == ["--regen"]:
    for case in _golden_cases():
        (GOLDEN / f"{case['name']}.txt").write_text(_golden_pack(case).render() + "\n", encoding="utf-8")


def test_render_empty_pack(tmp_path):
    sentence = "EVIDENCE: none found for this claim."
    assert Pack("c", "3f2a9c1", (), False).render() == sentence
    assert pack_for(write(tmp_path, {"a.py": "x = 1\n"}), "The sky is blue.").render() == sentence


def test_render_without_a_commit_has_no_at_clause():
    assert render_pack(Pack("c", "", (chunk("note:a", "note", "t"),), False)).split("\n")[0] == \
        "EVIDENCE for the claim"


def test_render_trailer_wording():
    one = (chunk("note:a", "note", "t"),)
    assert render_pack(Pack("c", "3f2a9c1", one, True, 2, 0)).endswith("(evidence trimmed: 2 chunks omitted)")
    assert render_pack(Pack("c", "3f2a9c1", one, True, 1, 3)).endswith(
        "(evidence trimmed: 1 chunk omitted, 3 chunks cut)")
    assert render_pack(Pack("c", "3f2a9c1", one, True)).endswith("(evidence trimmed)")


def test_the_render_parses_back(tmp_path):
    view, _ = git_repo(tmp_path, {"tools/policy.py": POLICY})
    got = pack_for(view, "Commit HEAD changed `Policy.decide` in tools/policy.py, not `Policy._mechanical`.")
    rendered = got.render().split("\n")
    labels = [line for line in rendered if line.startswith("[[")]
    assert [line[2:line.index("]]")] for line in labels] == ids(got)
    order = ids(got)
    assert len(got.chunks) >= 3
    for c in got.chunks:
        for run in pack_mod._segments(c):
            hit = got.find(run)                      # a whole run of the chunk's lines is a quote of it ...
            assert hit is not None and order.index(hit) <= order.index(c.id)   # ... or of an earlier chunk with it
            if len(run) >= 20:
                assert got.find(run[5:-3]) is not None


# ------------------------------------------------------------------ find

def test_find_normalises_whitespace_and_gutter():
    c = src_chunk("tools/gates.py", 118, 123, text_of=lambda n: {
        120: "def _declared_paths(body):", 121: "    return   [p for p in body]"}.get(n, f"pass_{n}"))
    got = assemble_pack("c", [], [c], sha="")
    assert got.find("def _declared_paths(body):") == c.id
    assert got.find(" 120| def _declared_paths(body):\n 121|     return [p for p in body]") == c.id
    assert got.find("120|\tdef\t_declared_paths(body):\n121|\treturn\t[p for p in body]") == c.id
    assert got.find("def   _declared_paths(body):  return [p for p in body]") == c.id
    assert got.find("  def _declared_paths(body):  ") == c.id


def test_find_rejects_short_and_spanning_quotes():
    a = src_chunk("a.py", 1, 3, text_of=lambda n: {1: "alpha = 1", 2: "beta = 2", 3: "gamma = 3"}[n])
    b = src_chunk("b.py", 1, 2, text_of=lambda n: {1: "delta = 4", 2: "omega = 5"}[n])
    got = assemble_pack("c", [], [a, b], sha="")
    assert got.find("x = 123") is None and got.find("x = 1234") is None      # 7 and 8 characters, neither there
    assert got.find("beta = 2") == a.id                                       # 8 characters: enough
    assert got.find("beta") is None and got.find("") is None and got.find("   \n  ") is None
    assert got.find("gamma = 3\ndelta = 4") is None                            # across two chunks
    assert find_quote(got, None) is None                                       # not a string


def test_find_does_not_span_an_omission_and_ignores_markers():
    c = src_chunk("a.py", 1, 9, shown=[(1, 2), (7, 9)], text_of=lambda n: f"statement_{n} = {n}")
    got = assemble_pack("c", [], [c], sha="")
    assert got.find("statement_1 = 1\nstatement_2 = 2") == c.id
    assert got.find("statement_2 = 2\nstatement_7 = 7") is None              # lines 3-6 are between them
    assert got.find("statement_2 = 2\n# … 4 lines omitted (3-6)\nstatement_7 = 7") is None
    assert got.find("# … 4 lines omitted (3-6)") is None


def test_find_is_case_sensitive():
    c = src_chunk("a.py", 1, 1, text_of=lambda n: "run(cmd, check=False)")
    got = assemble_pack("c", [], [c], sha="")
    assert got.find("run(cmd, check=False)") == c.id and got.find("run(cmd, check=false)") is None


def test_find_across_kinds():
    h = hunk_chunk("tools/x.py", 18, [" ctx_line = 1", "-    check=True", "+    check=False", " tail_line = 2"])
    note = chunk("note:dangling:X", "note", "`X` does not exist at 3f2a9c1; the file exists")
    header = chunk("git:a73e389:header", "git", "commit a73e389abc\nAuthor: Ann\n\n    fix the thing\n")
    table = chunk("ticket:1", "ticket", "| a | b |\n|---|---|\n| 1 | 2 |")
    got = assemble_pack("c", [], [h, note, header, table], sha="")
    assert got.find("+    check=False") == h.id                 # the diff's own `+` is part of the quote
    assert got.find(" 19| +    check=False") == h.id           # with the gutter a voter copied along
    assert got.find("-    check=True") == h.id                 # a removed line: its gutter is blank
    assert got.find("    | -    check=True") == h.id
    assert got.find("-    check=False") is None                # the sign is not stripped: no such line
    assert got.find("+    check=True") is None
    assert got.find("does not exist at 3f2a9c1") == note.id
    assert got.find("Author: Ann") == header.id
    assert got.find("| a | b |") == table.id and got.find("| 1 | 2 |") == table.id


def test_find_returns_the_first_chunk_in_rank_order():
    first = chunk("note:a", "note", "the very same sentence")
    second = chunk("note:b", "note", "the very same sentence")
    assert assemble_pack("c", [], [second, first], sha="").find("the very same sentence") == "note:a"


def test_find_in_a_merged_chunk(tmp_path):
    view = _file_view(tmp_path)
    got = assemble_pack("c", [], [src_chunk("a.py", 10, 20), src_chunk("a.py", 18, 30)], view, sha="")
    assert got.find("line 15\nline 16") == "src:a.py:10-30"
    assert got.find("line 19\nline 28") is None


# ------------------------------------------------------------------ whole chain

def test_unicode(tmp_path):
    body = 'name = "été — 日本語"\nsymbol = "Ωmega ✓"\n\n\ndef grüße():\n    return "straße"\n'
    view, _ = git_repo(tmp_path, {"tools/uni.py": body})
    got = pack_for(view, "`grüße` returns the string in tools/uni.py.")
    assert any("def grüße():" in c.text for c in got.chunks)
    assert "return \"straße\"" in got.render()
    assert got.find('def grüße():\n    return "straße"') is not None
    assert got.find("name = \"été — 日本語\"") is not None


def test_determinism(tmp_path):
    view, _ = git_repo(tmp_path, {"tools/policy.py": POLICY})
    claim = "Commit HEAD changed `Policy.decide` in tools/policy.py; `Policy._mechanical` is gone and `helper` stays."
    runs = [pack_for(view, claim) for _ in range(2)]
    assert runs[0] == runs[1] and runs[0].render().encode() == runs[1].render().encode()
    shuffled = assemble_pack(claim, [], list(reversed(runs[0].chunks)), sha=runs[0].sha)
    assert ids(shuffled) == ids(runs[0])


def test_a_dozen_anchors_still_fit_the_budget(tmp_path):
    files = {f"tools/mod_{n:02d}.py": f"def func_{n:02d}(x):\n    return x + {n}\n" * 30 for n in range(12)}
    view, _ = git_repo(tmp_path, files)
    claim = " and ".join(f"`func_{n:02d}` in tools/mod_{n:02d}.py" for n in range(12)) + " all return x."
    got = pack_for(view, claim)
    assert 1 <= len(got.chunks) <= 6 and got.truncated and got.omitted >= 1
    assert len(got.render()) <= 6000


def test_two_anchors_that_resolve_to_one_symbol_give_one_chunk(tmp_path):
    view, _ = git_repo(tmp_path, {"tools/policy.py": POLICY})
    got = pack_for(view, "`Policy.decide` is what `tools/policy.py::Policy.decide` calls.")
    assert [c.id for c in got.chunks if c.kind == "source"] == ["src:tools/policy.py:7-10"]


def test_a_commit_claim_gets_its_hunk_before_its_header(tmp_path):
    view, sha = git_repo(tmp_path, {"tools/policy.py": POLICY})
    got = pack_for(view, f"Commit {sha[:7]} added `Policy.decide` in tools/policy.py.")
    order = ids(got)
    assert order.index(f"git:{sha[:7]}:tools/policy.py:1-17") < order.index(f"git:{sha[:7]}:header")
    assert got.sha == sha


# ------------------------------------------------------------------ never raises

def test_a_provider_that_raises_is_a_note(tmp_path, monkeypatch):
    view, _ = git_repo(tmp_path, {"tools/policy.py": POLICY})

    def boom(*a, **k):
        raise RuntimeError("provider bug")

    monkeypatch.setattr(pack_mod, "source_chunks", boom)
    got = pack_for(view, "`Policy.decide` returns big.")
    assert "note:pack:source" in ids(got) and not got.truncated
    monkeypatch.setattr(pack_mod, "git_chunks", boom)
    both = pack_for(view, "`Policy.decide` returns big.")
    assert sorted(ids(both)) == ["note:pack:git", "note:pack:source"]


def test_a_view_whose_git_hangs_gives_a_note(tmp_path, monkeypatch):
    class Hanging(PathRepoView):
        def git(self, *args):
            time.sleep(5)

    view, sha = git_repo(tmp_path, {"tools/policy.py": POLICY})
    hung = Hanging(tmp_path)
    monkeypatch.setattr(evidence_git, "GIT_TIMEOUT", 0.2)
    monkeypatch.setattr(pack_mod, "VIEW_TIMEOUT", 0.2)
    started = time.monotonic()
    got = pack_for(hung, f"Commit {sha[:7]} changed `Policy.decide` in tools/policy.py.")
    assert time.monotonic() - started < 4
    assert any(c.kind == "note" and "timed out" in c.text for c in got.chunks) and not got.truncated
    assert any(c.id == "src:tools/policy.py:7-10" for c in got.chunks)         # the source side is unaffected


def test_a_view_whose_read_and_rev_parse_fail(tmp_path):
    class Broken(PathRepoView):
        def read(self, path):
            raise OSError("disk gone")

        def rev_parse(self, rev):
            raise RuntimeError("no git")

        def exists(self, path):
            raise RuntimeError("no stat")

    view = write(tmp_path, {"tools/policy.py": POLICY})
    broken = Broken(tmp_path)
    resolved = resolve_anchors(extract_anchors("`Policy.decide` and `Policy._gone` in tools/policy.py"), view)
    got = build_pack("claim", resolved, broken)
    assert isinstance(got, Pack) and not got.truncated and got.sha == ""
    assert all(c.kind == "note" for c in got.chunks)


def test_anything_else_that_goes_wrong_is_one_note(monkeypatch):
    monkeypatch.setattr(pack_mod, "assemble_pack", lambda *a, **k: 1 / 0)
    got = build_pack("claim", [found("path", "a.py", "a.py")], PathRepoView("/nonexistent-root"))
    assert ids(got) == ["note:pack:build"] and not got.truncated


def test_no_anchors_is_the_empty_pack_without_asking_a_provider(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("asked a provider")

    monkeypatch.setattr(pack_mod, "source_chunks", boom)
    monkeypatch.setattr(pack_mod, "git_chunks", boom)
    got = build_pack("The sky is blue.", [], write(tmp_path, {"a.py": "x\n"}), base="a", head="b")
    assert got.chunks == () and not got.truncated and got.render() == "EVIDENCE: none found for this claim."
    assert build_pack(None, None, None).chunks == ()


# ------------------------------------------------------------------ config

def test_pack_budget_defaults_and_config():
    assert PackBudget() == PackBudget(chars=6000, chunks=6, per_chunk=2400)
    cfg = configparser.ConfigParser()
    assert PackBudget.from_config(cfg) == PackBudget()                       # no section
    cfg.read_string("[claim_vote]\npack_chars = 4000\npack_chunks = 3\npack_chunk_chars = 1500\n")
    assert PackBudget.from_config(cfg) == PackBudget(chars=4000, chunks=3, per_chunk=1500)
    cfg.read_string("[claim_vote]\npack_chars = lots\npack_chunks = 0\npack_chunk_chars = -5\n")
    assert PackBudget.from_config(cfg) == PackBudget()                       # junk, zero, negative: the defaults
    cfg.read_string("[claim_vote]\npack_chunks = 2\n")
    assert PackBudget.from_config(cfg) == PackBudget(chunks=2)               # a key alone
    assert PackBudget.from_config(None) == PackBudget()
    with pytest.raises(Exception):
        PackBudget().chars = 1                                              # frozen
