"""CC-4 (270): git evidence — a commit's header and hunks, a diff range, a ticket's text, a file's history."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.claimcheck import evidence_git
from tools.claimcheck.anchors import PathRepoView, extract_anchors, resolve_anchors
from tools.claimcheck.evidence_git import git_chunks, hunks_of, ticket_chunk
from tools.claimcheck.model import Anchor, ResolvedAnchor
from tools.claimcheck.target import Target

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "claimcheck" / "git_golden"
_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Ann", "GIT_AUTHOR_EMAIL": "ann@example.com",
        "GIT_COMMITTER_NAME": "Ann", "GIT_COMMITTER_EMAIL": "ann@example.com"}


def _git(root: Path, *args: str, date: str = "2026-01-01T00:00:00+00:00") -> str:
    env = {**_ENV, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    return subprocess.run(["git", "-C", str(root), *args], env=env, capture_output=True,
                          check=True).stdout.decode("utf-8", "surrogateescape").strip()


def build(root: Path, history: list) -> list:
    """A repository from *history*: [{"message", "files": {path: text|bytes|None}, "merge"?}];
    None deletes, {"from": old} renames. Fixed identity and dates: the shas are the same on
    every machine. Returns the commit shas in order."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    shas = []
    for n, step in enumerate(history):
        date = f"2026-01-{n + 1:02d}T00:00:00+00:00"
        if step.get("branch"):
            _git(root, "checkout", "-q", "-b", step["branch"], shas[step["at"]])
        if step.get("checkout"):
            _git(root, "checkout", "-q", step["checkout"])
        for rel, content in step.get("files", {}).items():
            full = root / rel
            if content is None:
                _git(root, "rm", "-q", "--", rel)
            elif isinstance(content, dict):
                full.parent.mkdir(parents=True, exist_ok=True)
                _git(root, "mv", content["from"], rel)
                if "text" in content:
                    full.write_text(content["text"], encoding="utf-8", newline="")
            else:
                full.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    full.write_bytes(content)
                else:
                    full.write_text(content, encoding="utf-8", newline="")
        _git(root, "add", "-A")
        if step.get("merge"):
            _git(root, "merge", "-q", "--no-ff", "-m", step["message"], step["merge"], date=date)
        else:
            _git(root, "commit", "-q", "--allow-empty", "-m", step["message"], date=date)
        shas.append(_git(root, "rev-parse", "HEAD"))
    return shas


def chunks_for(view, claim: str, **kw) -> list:
    return git_chunks(resolve_anchors(extract_anchors(claim), view), view, claim=claim, **kw)


def ids(chunks) -> list:
    return [c.id for c in chunks]


def numbered(text: str) -> dict:
    """new-side line number -> the diff line (`+x`, ` x`) as the chunk prints it."""
    out = {}
    for line in text.split("\n"):
        head, sep, rest = line.partition("| ")
        if sep and head.strip().isdigit():
            out[int(head)] = rest
    return out


def _lines(n: int, tag: str = "x") -> str:
    return "".join(f"{tag}_{i} = {i}\n" for i in range(n))


# ------------------------------------------------------------------ commit

def test_commit_header_chunk(tmp_path):
    body = "Why it changed.\n\n" + ("word " * 400)
    shas = build(tmp_path, [{"message": "base", "files": {"a.py": "A = 1\n"}},
                            {"message": "fix: a reads B\n\n" + body, "files": {"a.py": "A = 2\nB = 3\n"}}])
    view = PathRepoView(tmp_path)
    (head, *_) = chunks_for(view, f"Commit {shas[1][:7]} adds `B`.")
    assert head.id == f"git:{shas[1][:7]}:header" and head.kind == "git"
    assert head.text.startswith(f"commit {shas[1]}\nAuthor: Ann <ann@example.com>\nDate:   2026-01-02T00:00:00+00:00")
    assert "\n    fix: a reads B\n" in head.text
    assert "    Why it changed." in head.text
    assert "chars of the message cut]" in head.text and head.text.count("word") < 300
    assert " a.py | 3 ++-" in head.text and "1 file changed, 2 insertions(+), 1 deletion(-)" in head.text


def test_commit_hunks_follow_the_claim_path(tmp_path):
    shas = build(tmp_path, [{"message": "base", "files": {"a.py": _lines(30), "b.py": "B = 1\n"}},
                            {"message": "two", "files": {"a.py": _lines(30, "y"), "b.py": "B = 2\n"}}])
    got = chunks_for(PathRepoView(tmp_path), f"Commit {shas[1][:7]} sets B to 2 in `b.py`.")
    assert ids(got) == [f"git:{shas[1][:7]}:header", f"git:{shas[1][:7]}:b.py:1-1"]
    assert numbered(got[1].text) == {1: "+B = 2"}
    assert "|  -B = 1" not in got[1].text and "| -B = 1" in got[1].text


def test_commit_without_a_named_path_takes_the_largest_files(tmp_path):
    sizes = {"a.py": 2, "b.py": 9, "c.py": 5, "d.py": 1, "e.py": 7}
    shas = build(tmp_path, [{"message": "base", "files": {p: "" for p in sizes}},
                            {"message": "grow", "files": {p: _lines(n) for p, n in sizes.items()}}])
    got = chunks_for(PathRepoView(tmp_path), f"Commit {shas[1][:7]} grows the modules.")
    assert sorted(c.path for c in got[1:]) == ["b.py", "c.py", "e.py"]   # in patch order


def test_symbol_anchor_selects_its_file(tmp_path):
    shas = build(tmp_path, [{"message": "base", "files": {"lib/a.py": "def load_value():\n    return 1\n",
                                                          "lib/z.py": _lines(40)}},
                            {"message": "two", "files": {"lib/a.py": "def load_value():\n    return 2\n",
                                                         "lib/z.py": _lines(40, "q")}}])
    got = chunks_for(PathRepoView(tmp_path), f"Commit {shas[1][:7]} makes `load_value` return 2.")
    assert [c.path for c in got[1:]] == ["lib/a.py"]


def test_diff_range_hunks(tmp_path):
    base_text = _lines(40)
    head_lines = base_text.split("\n")
    head_lines[5] = "x_5 = 'five'"
    head_lines[30:30] = ["inserted_a = 1", "inserted_b = 2"]
    head_text = "\n".join(head_lines)
    shas = build(tmp_path, [{"message": "base", "files": {"m.py": base_text, "o.py": "O = 1\n"}},
                            {"message": "head", "files": {"m.py": head_text, "o.py": "O = 2\n"}}])
    view = PathRepoView(tmp_path)
    got = chunks_for(view, "`m.py` sets x_5 to 'five'.", base=shas[0], head=shas[1])
    prefix = f"diff:{shas[0][:7]}..{shas[1][:7]}:m.py:"
    assert all(c.id.startswith(prefix) for c in got) and len(got) == 2
    on_disk = head_text.split("\n")
    for c in got:
        for n, line in numbered(c.text).items():
            assert line[1:] == on_disk[n - 1]            # the gutter is the head file's line number
            assert c.start <= n <= c.end
    assert got[0].id == prefix + "3-9"                    # the hunk with the claim's token first
    assert "| -x_5 = 5" in got[0].text


def test_range_path_left_alone_is_a_note(tmp_path):
    shas = build(tmp_path, [{"message": "base", "files": {"m.py": "M = 1\n", "o.py": "O = 1\n"}},
                            {"message": "head", "files": {"o.py": "O = 2\n"}}])
    got = chunks_for(PathRepoView(tmp_path), "`m.py` sets M.", base=shas[0], head=shas[1])
    assert [(c.kind, c.text) for c in got] == [
        ("note", f"m.py is unchanged between {shas[0][:7]} and {shas[1][:7]}")]


def test_range_without_a_path_gives_stat_and_keyword_hunks(tmp_path):
    shas = build(tmp_path, [{"message": "base", "files": {"a.py": "A = 1\n", "b.py": "B = 1\n"}},
                            {"message": "head", "files": {"a.py": "A = 2\n", "b.py": "B = check_exit\n"}}])
    got = chunks_for(PathRepoView(tmp_path), "The fix replaced the literal with check_exit.",
                     base=shas[0], head=shas[1])
    prefix = f"diff:{shas[0][:7]}..{shas[1][:7]}"
    assert ids(got) == [f"{prefix}:stat", f"{prefix}:b.py:1-1"]
    assert " 2 files changed" in got[0].text


def test_range_with_an_unknown_rev_is_a_note(tmp_path):
    shas = build(tmp_path, [{"message": "base", "files": {"a.py": "A = 1\n"}}])
    got = chunks_for(PathRepoView(tmp_path), "`a.py` sets A.", base="deadbee", head=shas[0])
    assert [(c.id, c.text) for c in got] == [("note:git:deadbee", "commit deadbee is not in this repository")]


def test_hunks_ranked_by_keyword_hits():
    patch = ("diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n"
             "@@ -1,1 +1,1 @@\n-a = 1\n+a = 2\n"
             "@@ -10,1 +10,1 @@\n-b = 1\n+b = load_timeout()\n"
             "@@ -20,1 +20,1 @@\n-c = 1\n+c = 2\n")
    got = hunks_of(patch, keywords=["load_timeout"])
    assert [h.new_start for h in got] == [10, 1, 20]
    assert [h.hits for h in got] == [1, 0, 0]
    assert all(h.header[0] == "diff --git a/m.py b/m.py" for h in got)
    assert hunks_of(patch, keywords=[], paths=["other.py"]) == []


def test_long_hunk_keeps_the_keyword_lines(tmp_path):
    shas = build(tmp_path, [{"message": "base", "files": {"m.py": ""}},
                            {"message": "head", "files": {"m.py": _lines(300) + "deep_marker = 1\n" + _lines(300, "z")}}])
    (hunk,) = [c for c in chunks_for(PathRepoView(tmp_path), "`m.py` sets deep_marker.",
                                     base=shas[0], head=shas[1], max_chunk_chars=1200)]
    assert len(hunk.text) <= 1200
    assert numbered(hunk.text)[301] == "+deep_marker = 1"
    assert numbered(hunk.text)[1] == "+x_0 = 0"
    assert "diff lines cut" in hunk.text


def test_rename_is_shown_as_git_shows_it(tmp_path):
    shas = build(tmp_path, [{"message": "base", "files": {"old_name.py": _lines(20)}},
                            {"message": "rename", "files": {"new_name.py": {"from": "old_name.py"}}}])
    got = chunks_for(PathRepoView(tmp_path), f"Commit {shas[1][:7]} renamed `old_name.py`.")
    assert "old_name.py => new_name.py" in got[0].text
    assert got[1].id == f"git:{shas[1][:7]}:new_name.py:0-0"
    assert got[1].text.split("\n")[0] == "diff --git a/old_name.py b/new_name.py"
    assert "rename from old_name.py\nrename to new_name.py" in got[1].text


def test_binary_file_is_skipped_with_a_note(tmp_path):
    shas = build(tmp_path, [{"message": "base", "files": {"i.png": b"\x89PNG\x00\x01"}},
                            {"message": "pic", "files": {"i.png": b"\x89PNG\x00\x02"}}])
    got = chunks_for(PathRepoView(tmp_path), f"Commit {shas[1][:7]} changes `i.png`.")
    assert [(c.id, c.kind, c.text) for c in got[1:]] == [("note:git:binary:i.png", "note", "binary file: i.png")]
    assert "\x00" not in "".join(c.text for c in got)


def test_huge_diff_is_summarised(tmp_path):
    base = "".join(f"line_{i:05d} = 'some padding text here, long enough'\n" for i in range(9000))
    rows = base.split("\n")
    for i in range(0, 9000, 10):                 # 900 hunks, one of them holds the keyword
        rows[i] = rows[i].replace("'some", "'more")
    rows[4505] = rows[4505].replace("'some", "'needle_here")
    shas = build(tmp_path, [{"message": "base", "files": {"big.py": base}},
                            {"message": "huge", "files": {"big.py": "\n".join(rows)}}])
    assert len(_git(tmp_path, "diff", shas[0], shas[1])) > 300_000
    got = chunks_for(PathRepoView(tmp_path), f"Commit {shas[1][:7]} puts needle_here into `big.py`.")
    assert got[0].id.endswith(":header") and "big.py | 1802" in got[0].text
    assert len(got) == 2 and numbered(got[1].text)[4506] == "+line_04505 = 'needle_here padding text here, long enough'"
    assert all(len(c.text) <= 2400 for c in got)


def test_a_small_diff_keeps_every_hunk(tmp_path):
    base = "".join(f"v_{i} = 0\n" for i in range(100))
    head = base.replace("v_10 = 0", "v_10 = 1").replace("v_50 = 0", "v_50 = needle_here")
    shas = build(tmp_path, [{"message": "base", "files": {"s.py": base}},
                            {"message": "two", "files": {"s.py": head}}])
    got = chunks_for(PathRepoView(tmp_path), f"Commit {shas[1][:7]} puts needle_here into `s.py`.")
    assert [c.start for c in got[1:]] == [48, 8]          # keyword hunk first, the other kept


def test_file_over_the_line_cap_is_a_note(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence_git, "MAX_FILE_LINES", 10)
    shas = build(tmp_path, [{"message": "base", "files": {"a.py": ""}},
                            {"message": "two", "files": {"a.py": _lines(20)}}])
    got = chunks_for(PathRepoView(tmp_path), f"Commit {shas[1][:7]} fills `a.py`.")
    assert [(c.kind, c.text) for c in got[1:]] == [("note", "diff too large: a.py (+20 -0 lines); see --stat")]


def test_unknown_sha_is_a_note_not_an_error(tmp_path):
    build(tmp_path, [{"message": "base", "files": {"a.py": "A = 1\n"}}])
    got = chunks_for(PathRepoView(tmp_path), "Commit deadbee changed the gate.")
    assert [(c.id, c.kind, c.text) for c in got] == [
        ("note:git:deadbee", "note", "commit deadbee is not in this repository")]


def test_a_view_without_git_gives_notes(tmp_path):
    (tmp_path / "a.py").write_text("A = 1\n")
    got = chunks_for(PathRepoView(tmp_path), "Commit deadbee fixed `a.py`.")
    assert [c.kind for c in got] == ["note", "note"]      # the commit; the history of a.py
    assert got[0].id == "note:git:deadbee"


def test_a_bug_of_this_module_is_a_note(tmp_path, monkeypatch):
    shas = build(tmp_path, [{"message": "base", "files": {"a.py": "A = 1\n"}}])

    def broken(*a, **k):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(evidence_git, "hunks_of", broken)
    got = chunks_for(PathRepoView(tmp_path), f"Commit {shas[0][:7]} sets A.")
    assert [(c.id, c.kind, c.text) for c in got] == [
        (f"note:git:{shas[0][:7]}", "note", "git evidence failed: RuntimeError: parser bug")]


def test_one_commit_named_twice_gives_its_chunks_once(tmp_path):
    shas = build(tmp_path, [{"message": "base", "files": {"a.py": "A = 1\n"}}])
    got = chunks_for(PathRepoView(tmp_path), f"Commit {shas[0][:7]} sets A; commit {shas[0][:9]} too.")
    assert len(got) == len(set(ids(got))) == 2


def test_repository_config_cannot_turn_renames_off(tmp_path):
    shas = build(tmp_path, [{"message": "base", "files": {"old_name.py": _lines(20)}},
                            {"message": "rename", "files": {"new_name.py": {"from": "old_name.py"}}}])
    _git(tmp_path, "config", "diff.renames", "false")
    got = chunks_for(PathRepoView(tmp_path), f"Commit {shas[1][:7]} renamed `old_name.py`.")
    assert "old_name.py => new_name.py" in got[0].text
    assert got[1].text.split("\n")[0] == "diff --git a/old_name.py b/new_name.py"


def test_an_unexpected_output_is_a_note(tmp_path):
    shas = build(tmp_path, [{"message": "base", "files": {"a.py": "A = 1\n"}}])

    class Odd(PathRepoView):
        def git(self, *args):
            return None if args[0] == "log" else super().git(*args)

    got = chunks_for(Odd(tmp_path), f"Commit {shas[0][:7]} sets A.")
    assert got and all(c.id.startswith(("note:git:", "git:")) for c in got)


# ------------------------------------------------------------------ edge cases

def test_merge_commit_is_diffed_against_its_first_parent(tmp_path):
    shas = build(tmp_path, [
        {"message": "base", "files": {"a.py": "A = 1\n", "b.py": "B = 1\n"}},
        {"message": "side", "branch": "side", "at": 0, "files": {"b.py": "B = 2\n"}},
        {"message": "main", "checkout": "main", "files": {"a.py": "A = 2\n"}},
        {"message": "merge side", "merge": "side"},
    ])
    got = chunks_for(PathRepoView(tmp_path), f"Commit {shas[3][:7]} merged the side branch.")
    assert ids(got)[1:] == [f"git:{shas[3][:7]}:b.py:1-1"]   # side's change, not main's
    assert "b.py | 2 +-" in got[0].text and "a.py" not in got[0].text


def test_root_commit_diffs_against_the_empty_tree(tmp_path):
    shas = build(tmp_path, [{"message": "root", "files": {"a.py": "A = 1\n"}}])
    got = chunks_for(PathRepoView(tmp_path), f"Commit {shas[0][:7]} holds `a.py`.")
    assert ids(got) == [f"git:{shas[0][:7]}:header", f"git:{shas[0][:7]}:a.py:1-1"]
    assert "create mode 100644 a.py" in got[0].text and numbered(got[1].text) == {1: "+A = 1"}


def test_empty_commit_has_a_header_only(tmp_path):
    shas = build(tmp_path, [{"message": "base", "files": {"a.py": "A = 1\n"}}, {"message": "nothing"}])
    got = chunks_for(PathRepoView(tmp_path), f"Commit {shas[1][:7]} changes `a.py`.")
    assert ids(got) == [f"git:{shas[1][:7]}:header", "gitlog:a.py"]
    assert got[0].text.endswith("(no changes)")


def test_crlf_and_a_very_long_line(tmp_path):
    long = "data = '" + "z" * 50_000 + "'\r\n"
    shas = build(tmp_path, [{"message": "base", "files": {"w.py": "A = 1\r\n"}},
                            {"message": "two", "files": {"w.py": "A = 2\r\n" + long}}])
    (_, hunk) = chunks_for(PathRepoView(tmp_path), f"Commit {shas[1][:7]} sets A in `w.py`.")
    assert "\r" not in hunk.text
    assert numbered(hunk.text)[1] == "+A = 2"
    assert len(hunk.text) <= 2400 and "chars cut]" in hunk.text


def test_path_with_spaces_and_unicode(tmp_path):
    shas = build(tmp_path, [{"message": "base", "files": {"a b/é.py": "E = 1\n"}},
                            {"message": "two", "files": {"a b/é.py": "E = 2\n"}}])
    view = PathRepoView(tmp_path)
    # CC-1 cuts a path at a space (`a b/é.py` is read as `b/é.py`: the text alone cannot tell
    # it from `python3 x.py`), so the path anchor is given resolved, as a view would find it
    path = ResolvedAnchor(Anchor("path", "a b/é.py", 0, 8), True, path="a b/é.py")
    commit = resolve_anchors([Anchor("commit", shas[1][:7], 0, 7)], view)
    got = git_chunks([*commit, path], view, claim="sets E")
    assert [c.id for c in got[1:]] == [f"git:{shas[1][:7]}:a b/é.py:1-1"]
    assert numbered(got[1].text) == {1: "+E = 2"}
    assert '"b/a b/\\303\\251.py"' in got[1].text          # the header as git prints it
    ranged = git_chunks([path], view, claim="sets E", base=shas[0], head=shas[1])
    assert [c.id for c in ranged] == [f"diff:{shas[0][:7]}..{shas[1][:7]}:a b/é.py:1-1"]


# ------------------------------------------------------------------ ticket, history

_TICKET = ("# 07 — the store keeps a backup\n\n**Status:** landed `abc1234`\n**File:** `store.py`\n\n---\n\n"
           "## Why\n\nThe store lost data on a crash.\n\n## What it does\n\nWrites a .bak first.\n")


def test_ticket_chunk_title_status_first_section(tmp_path):
    files = {"epic-tasks/07-store-backup.md": _TICKET, "epic-tasks/08-x.md": "no title here\n"}
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    view = PathRepoView(tmp_path)
    (r,) = resolve_anchors(extract_anchors("Ticket 07 is landed."), view)
    chunk = ticket_chunk(r, view)
    assert (chunk.id, chunk.kind, chunk.path, chunk.start) == ("ticket:07", "ticket", "epic-tasks/07-store-backup.md", 1)
    rows = numbered(chunk.text)
    assert rows[1] == "# 07 — the store keeps a backup" and rows[3] == "**Status:** landed `abc1234`"
    assert rows[10] == "The store lost data on a crash."
    assert "Writes a .bak" not in chunk.text and "the rest of the ticket omitted" in chunk.text
    (missing,) = resolve_anchors(extract_anchors("Ticket 99 is landed."), view)
    assert ticket_chunk(missing, view) is None
    unresolved = ResolvedAnchor(r.anchor, False)          # found through view.ticket_file
    assert ticket_chunk(unresolved, view) == chunk


def test_ticket_over_budget_keeps_title_and_status(tmp_path):
    text = "# 05 — long\n\n" + "".join(f"para {i} " * 20 + "\n" for i in range(40)) + "**Status:** open\n"
    (tmp_path / "epic-tasks").mkdir()
    (tmp_path / "epic-tasks/05-long.md").write_text(text)
    view = PathRepoView(tmp_path)
    (r,) = resolve_anchors(extract_anchors("Ticket 05 is open."), view)
    chunk = ticket_chunk(r, view, max_chunk_chars=800)
    assert len(chunk.text) <= 800
    assert numbered(chunk.text)[1] == "# 05 — long" and numbered(chunk.text)[43] == "**Status:** open"


def test_ticket_without_status_and_two_with_one_number(tmp_path):
    (tmp_path / "epic-tasks").mkdir()
    (tmp_path / "epic-tasks/12-b-second.md").write_text("# 12 — second\n")
    (tmp_path / "epic-tasks/12-a-first.md").write_text("# 12 — first\n\nNo status line.\n")
    view = PathRepoView(tmp_path)
    (r,) = resolve_anchors(extract_anchors("Ticket 12 says no status."), view)
    chunk = ticket_chunk(r, view)
    assert chunk.path == "epic-tasks/12-a-first.md"
    assert numbered(chunk.text) == {1: "# 12 — first", 2: "", 3: "No status line."}


def test_ticket_named_by_path_through_git_chunks(tmp_path):
    (tmp_path / "epic-tasks").mkdir()
    (tmp_path / "epic-tasks/07-store-backup.md").write_text(_TICKET)
    got = chunks_for(PathRepoView(tmp_path), "Ticket `epic-tasks/07-store-backup.md` is landed.")
    assert [(c.id, c.kind) for c in got] == [("ticket:epic-tasks/07-store-backup.md", "ticket")]


def test_history_chunk_only_for_history_words(tmp_path):
    build(tmp_path, [{"message": "add a", "files": {"a.py": "A = 1\n"}},
                     {"message": "fix a", "files": {"a.py": "A = 2\n"}}])
    view = PathRepoView(tmp_path)
    (log,) = chunks_for(view, "The bug in `a.py` was fixed.")
    assert log.id == "gitlog:a.py" and log.kind == "git"
    assert re.fullmatch(r"git log -n 5 -- a\.py\n[0-9a-f]{7} 2026-01-02 fix a\n[0-9a-f]{7} 2026-01-01 add a",
                        log.text)
    assert chunks_for(view, "`a.py` reads the config.") == []
    assert chunks_for(view, "`a.py` addresses the config.") == []


# ------------------------------------------------------------------ safety

class Spy(PathRepoView):
    def __init__(self, root, sleep: float = 0.0):
        super().__init__(root)
        self.calls, self.sleep = [], sleep

    def git(self, *args):
        self.calls.append(args)
        if self.sleep:
            time.sleep(self.sleep)
        return super().git(*args)


def test_read_only(tmp_path):
    shas = build(tmp_path, [{"message": "base", "files": {"a.py": "A = 1\n", "epic-tasks/01-t.md": "# 01\n"}},
                            {"message": "fix", "files": {"a.py": "A = 2\n"}}])
    (tmp_path / "a.py").write_text("A = 3  # a local edit\n")
    before = (_git(tmp_path, "status", "--porcelain"), _git(tmp_path, "rev-parse", "HEAD"),
              (tmp_path / ".git" / "index").read_bytes())
    view = Spy(tmp_path)
    claim = f"Commit {shas[1][:7]} fixed `a.py`; ticket 01 and {shas[0][:7]}."
    got = git_chunks(resolve_anchors(extract_anchors(claim), view), view, claim=claim,
                     base=shas[0], head=shas[1])
    assert got and not (tmp_path / ".git" / "index.lock").exists()
    assert {a[0] for a in view.calls} <= {"show", "diff", "log", "cat-file", "rev-parse", "ls-tree"}
    after = (_git(tmp_path, "status", "--porcelain"), _git(tmp_path, "rev-parse", "HEAD"),
             (tmp_path / ".git" / "index").read_bytes())
    assert after == before


def test_user_git_config_is_ignored(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    shas = build(repo, [{"message": "base", "files": {"a.py": "A = 1\n"}},
                        {"message": "two", "files": {"a.py": "A = 2\n"}}])
    marker = tmp_path / "RAN"
    cfg = tmp_path / "gitconfig"
    cfg.write_text(f"[color]\n\tui = always\n[core]\n\tpager = touch {marker}; cat\n"
                   f"[diff]\n\texternal = touch {marker}; true\n\tnoprefix = true\n\trenames = false\n"
                   f"[log]\n\tdate = relative\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    claim = f"Commit {shas[1][:7]} changed `a.py`."
    plain = chunks_for(PathRepoView(repo), claim)
    with Target.open(repo, "main", scratch=tmp_path / "s") as t:
        pinned = chunks_for(t.view(), claim)
    for got in (plain, pinned):
        text = "\n".join(c.text for c in got)
        assert "\x1b[" not in text and "diff --git a/a.py b/a.py" in text
        assert re.search(r"^[0-9a-f]{7} 2026-01-02 two$", text, re.M)   # not "N months ago"
    assert [c.text for c in plain] == [c.text for c in pinned]
    assert not marker.exists()


def test_timeout_gives_a_note(tmp_path, monkeypatch):
    shas = build(tmp_path, [{"message": "base", "files": {"a.py": "A = 1\n"}}])
    monkeypatch.setattr(evidence_git, "GIT_TIMEOUT", 0.2)
    view = Spy(tmp_path, sleep=1.0)
    started = time.monotonic()
    got = chunks_for(view, f"Commit {shas[0][:7]} sets A.")
    assert time.monotonic() - started < 0.9
    assert [(c.kind, c.text) for c in got] == [("note", "git show timed out after 0.2 s")]


def test_determinism(tmp_path):
    shas = build(tmp_path, [{"message": "base", "files": {"a.py": _lines(30), "b.py": "B\n"}},
                            {"message": "two", "files": {"a.py": _lines(30, "y"), "b.py": "C\n"}}])
    view = PathRepoView(tmp_path)
    claim = f"Commit {shas[1][:7]} fixed `a.py` and `b.py`."
    one = chunks_for(view, claim, base=shas[0], head=shas[1])
    two = chunks_for(PathRepoView(tmp_path), claim, base=shas[0], head=shas[1])
    assert one == two and len(one) >= 4


# ------------------------------------------------------------------ golden

def render_golden(tmp_path: Path) -> list:
    """The golden cases as the code renders them today (`PYTHONPATH=. python3 tests/test_claimcheck_git.py --regen`)."""
    spec = json.loads((GOLDEN / "history.json").read_text(encoding="utf-8"))
    shas = build(tmp_path / "repo", spec)
    view = PathRepoView(tmp_path / "repo")
    cases = json.loads((GOLDEN / "cases.json").read_text(encoding="utf-8"))
    out = []
    for case in cases:
        sub = lambda s: re.sub(r"\{c(\d+)(?::(\d+))?\}",
                               lambda m: shas[int(m.group(1))][:int(m.group(2) or 40)], s)
        kw = {k: sub(case[k]) for k in ("base", "head") if case.get(k)}
        chunks = chunks_for(view, sub(case["claim"]), max_chunk_chars=1200, **kw)
        out.append({**{k: case[k] for k in ("claim", "base", "head") if k in case},
                    "chunks": [{"id": c.id, "kind": c.kind, "start": c.start, "end": c.end,
                                "why": c.why, "text": c.text} for c in chunks]})
    return out


def test_golden_chunks(tmp_path):
    want = json.loads((GOLDEN / "cases.json").read_text(encoding="utf-8"))
    assert len(want) == 10
    assert render_golden(tmp_path) == want


if __name__ == "__main__" and sys.argv[1:] == ["--regen"]:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        (GOLDEN / "cases.json").write_text(json.dumps(render_golden(Path(tmp)), indent=1, ensure_ascii=False) + "\n",
                                           encoding="utf-8")
