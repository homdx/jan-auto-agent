"""tests_bugfix/test_arena_status_verbs_review.py — bugs in AR-14's status verbs (tools/arena/tickets.py).

`set_status_text` promises "everything else byte for byte", and the verbs are meant
to change a ticket's `**Status:**` line and nothing else.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tools.arena import cli, rounds, tickets

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

INI = "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = test/a\n"

HEADER = ("**Status:** open\n**Severity:** 2\n**File:** pkg/x.py\n**Symbol:** x\n"
          "**Round:** 9\n**Size:** M\n**Also touches:** -\n\n")


# ── set_status_text, the pure part ───────────────────────────────────────────
# Bug: closing then reopening a ticket whose `**Status:**` line is followed by a
# blank line took the blank line with it (`_CLOSED_LINE_RE` already ends in `\n?`,
# and the code then skipped one more `\n`).

S = tickets.set_status_text

ROUND_TRIP_TEXTS = [
    "# t\n\n**Status:** open\n\nbody\n",                      # blank line under the status
    "# t\n\n**Status:** open\n**Severity:** 2\n\nbody\n",     # the usual header
    "# t\n\n**Status:** open\nbody\n",                         # nothing between
    "# t\n\n**Status:** queued (judged)\n\n\nbody\n",         # two blank lines, a note
    "**Status:** open\n",                                      # the status line is the file
    "# t\r\n\r\n**Status:** open\r\n\r\nbody\r\n",            # CRLF
    "# t\r\n\r\n**Status:** open\r\n**Severity:** 2\r\nbody\r\n",
]


@pytest.mark.parametrize("text", ROUND_TRIP_TEXTS)
def test_close_then_reopen_gives_back_the_ticket_it_started_from(text):
    word = tickets.status_word(text)
    note = tickets.status_note(text)
    closed = S(text, "closed", reason="done")
    assert tickets.status_word(closed) == "closed"
    assert tickets.closed_reason(closed) == "done"
    assert S(closed, word, note) == text


# Bug: on a CRLF ticket the status line lost its `\r` and the `**Closed:**` line
# was written with a bare `\n`.

def test_a_crlf_ticket_keeps_crlf_on_the_status_line():
    text = "# t\r\n\r\n**Status:** open\r\n\r\nbody\r\n"
    assert S(text, "queued") == "# t\r\n\r\n**Status:** queued\r\n\r\nbody\r\n"
    assert S(text, "queued", "judged") == "# t\r\n\r\n**Status:** queued (judged)\r\n\r\nbody\r\n"


def test_a_crlf_ticket_gets_a_crlf_closed_line():
    text = "# t\r\n\r\n**Status:** open\r\n\r\nbody\r\n"
    assert S(text, "closed", reason="done") == \
        "# t\r\n\r\n**Status:** closed\r\n**Closed:** done\r\n\r\nbody\r\n"


def test_an_lf_ticket_is_unchanged_behaviour():
    assert S("# t\n\n**Status:** open\n\nbody\n", "closed", reason="done") == \
        "# t\n\n**Status:** closed\n**Closed:** done\n\nbody\n"


# Bug: a `**Closed:**` line ABOVE the `**Status:**` line moved the status line when
# it was removed, and the stale offsets of the first search then cut the text in
# the wrong place: `**Status:** clos**Status:** open`.

def test_a_closed_line_above_the_status_line_does_not_corrupt_the_text():
    text = "**Closed:** why\n**Status:** closed\nbody\n"
    assert S(text, "open") == "**Status:** open\nbody\n"


def test_closing_again_with_the_closed_line_above_the_status_line():
    text = "# t\n**Closed:** old\n**Status:** closed\nbody\n"
    again = S(text, "closed", reason="new")
    assert again == "# t\n**Status:** closed\n**Closed:** new\nbody\n"
    assert again.count("**Closed:**") == 1


# ── the verbs, end to end ────────────────────────────────────────────────────
# Bug: the verbs read the ticket through `printable()` — "never feed it back to git
# as content" — and committed what they had set the status on, so a byte that is not
# UTF-8 came back as U+FFFD: `arena issue queue` rewrote the body of the ticket.

NN = 9
LATIN1 = b"latin1 byte here: caf\xe9 and more\n"
TITLE = "# AR-9 \u2014 t\n\n".encode("utf-8")
RAW = TITLE + HEADER.encode("utf-8") + LATIN1
REL = f"epic-tasks/{NN}-ticket-{NN}.md"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _blob(repo: Path) -> bytes:
    proc = subprocess.run(["git", "show", f"refs/heads/arena:{REL}"], cwd=str(repo),
                          capture_output=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _swap_status(raw: bytes, old: str, new: str) -> bytes:
    return raw.replace(f"**Status:** {old}\n".encode(), f"**Status:** {new}\n".encode(), 1)


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "arena")
    (r / "contest.ini").write_text(INI, encoding="utf-8")
    (r / ".gitignore").write_text(".arena/\nout/\n", encoding="utf-8")
    (r / "epic-tasks").mkdir()
    (r / REL).write_bytes(RAW)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "tickets")
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, ENV[key])
    monkeypatch.setattr(cli, "REPO_ROOT", r)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return r


def _run(capsys, *argv: str):
    code = cli.main(["-y", *argv])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_queue_changes_the_status_line_and_no_other_byte(repo, capsys):
    code, _, err = _run(capsys, "issue", "queue", str(NN))
    assert code == 0, err
    assert _blob(repo) == _swap_status(RAW, "open", "queued")


def test_close_and_reopen_give_back_the_original_bytes(repo, capsys):
    assert _run(capsys, "issue", "close", str(NN), "--reason", "moved")[0] == 0
    assert b"caf\xe9" in _blob(repo)
    assert _run(capsys, "issue", "reopen", str(NN))[0] == 0
    assert _blob(repo) == RAW


def test_edit_keeps_every_byte_it_was_not_asked_to_change(repo, capsys, tmp_path, monkeypatch):
    editor = tmp_path / "ed.sh"
    editor.write_text("#!/bin/sh\nprintf 'appended line\\n' >> \"$1\"\n", encoding="utf-8")
    editor.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(editor))
    code, _, err = _run(capsys, "issue", "edit", str(NN))
    assert code == 0, err
    assert _blob(repo) == RAW + b"appended line\n"


# ── bug: `_uncommitted` / `_modified` cut the first path's first letter ───────
# The same strip of `git status --porcelain` as `rounds._dirty_tasks`: ` M path`
# lost its leading space, then `line[3:]` took the path's first character. The
# refusal that says which of the ticket's edits are uncommitted named a path that
# does not exist.

def test_uncommitted_names_the_modified_ticket_in_full(repo):
    with open(repo / REL, "ab") as handle:
        handle.write(b"an edit\n")
    assert tickets._uncommitted(repo, REL) == [REL]


def test_modified_names_a_lone_modified_file_in_full(repo):
    with open(repo / REL, "ab") as handle:
        handle.write(b"an edit\n")
    assert tickets._modified(repo) == [REL]


def test_modified_still_skips_arena_state_and_lists_the_rest(repo):
    (repo / ".arena").mkdir()
    (repo / ".arena" / "x.json").write_text("{}", encoding="utf-8")
    (repo / "new.txt").write_text("n\n", encoding="utf-8")
    with open(repo / REL, "ab") as handle:
        handle.write(b"an edit\n")
    assert sorted(tickets._modified(repo)) == sorted([REL, "new.txt"])


def test_a_verb_refuses_on_a_dirty_ticket_and_names_its_real_path(repo, capsys):
    with open(repo / REL, "ab") as handle:
        handle.write(b"an uncommitted edit\n")
    code, _, err = _run(capsys, "issue", "queue", str(NN))
    assert code == 2
    assert f"uncommitted: {REL}" in err
    assert "uncommitted: pic-tasks" not in err
    assert b"an uncommitted edit" in (repo / REL).read_bytes()      # never overwritten


# ── bug: the status verbs and intake could not see a non-ASCII ticket name ────
# `_on_branch` and `blocking_tickets` listed `epic-tasks/` with `ls-tree --name-only`
# and no `-z`, so git's octal quoting hid `12-café.md`: `issue queue 12` said "no
# ticket 12", and a lower-numbered open ticket of that name never blocked intake.

CAFE = "12-café.md"
CAFE_RAW = ("# AR-12 \u2014 café\n\n" + HEADER).encode("utf-8") + b"body\n"


@pytest.fixture
def cafe(repo) -> Path:
    (repo / "epic-tasks" / CAFE).write_bytes(CAFE_RAW)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "12: café")
    return repo


def test_on_branch_finds_a_non_ascii_ticket(cafe):
    assert tickets._on_branch(cafe, "arena", 12) == [CAFE]


def test_a_lower_open_non_ascii_ticket_blocks_intake(cafe):
    blockers = tickets.blocking_tickets(cafe, "arena", 20)
    assert [(b["number"], b["name"], b["status"]) for b in blockers] == [(NN, f"{NN}-ticket-{NN}.md", "open"),
                                                                        (12, CAFE, "open")]


def test_queue_works_on_a_non_ascii_ticket(cafe, capsys):
    code, _, err = _run(capsys, "issue", "queue", "12")
    assert code == 0, err
    proc = subprocess.run(["git", "show", f"refs/heads/arena:epic-tasks/{CAFE}"], cwd=str(cafe),
                          capture_output=True, env=ENV)
    assert proc.stdout == CAFE_RAW.replace(b"**Status:** open\n", b"**Status:** queued\n", 1)
