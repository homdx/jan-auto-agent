"""Black-box bench for ticket 197 (arena: four readers of git and provider output), from the ticket only.

Bugs 206 (quoted ticket names), 207 (a key from `contest.ini` that `profile set key=` / `unset-role`
cannot remove), 208 (odd `/models` replies), 209 (the `_dirty_tasks` names). Public entry points only:
`rounds.find_ticket`, `tickets._branch_names`, `rounds._dirty_tasks`, the `arena` CLI (`cli.main` with
`cli.REPO_ROOT` patched at a scratch checkout), `py_model_test.free_from_direct_api`, `models._direct_for`.

Run from a checkout root:  python3 -m pytest contest-bench/197/acceptance_197.py -n 0 -q
"""

from __future__ import annotations

import http.server
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.arena import cli, models, rounds, tickets  # noqa: E402
from tools.arena.gitref import GitRefError  # noqa: E402
from tools.contest import roster  # noqa: E402

TASKS = "epic-tasks"


def _git(repo, *args):
    return subprocess.run(["git", "-c", "core.quotepath=on", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    (r / TASKS).mkdir()
    (r / TASKS / "01-a.md").write_text("# 1 a\n\n**Status:** queued\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def _commit_on_branch(repo, branch, names, status="queued"):
    """Commit *names* under epic-tasks/ on *branch*, then go back to main (they are not in the worktree)."""
    _git(repo, "checkout", "-q", "-b", branch)
    for n in names:
        (repo / TASKS / n).write_text(f"# t\n\n**Status:** {status}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "tickets")
    _git(repo, "checkout", "-q", "main")


# ── 206 ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["07-а.md", '07-"q".md', "07-b\\s.md", "07-é.md"])
def test_206_find_ticket_on_a_branch(repo, name):
    _commit_on_branch(repo, "br", [name])
    fname, text = rounds.find_ticket(repo, 7, "br")
    assert fname == name
    assert "queued" in text


@pytest.mark.parametrize("name", ["07-а.md", '07-"q".md', "07-b\\s.md"])
def test_206_branch_names_lists_the_real_name(repo, name):
    _commit_on_branch(repo, "br", [name])
    names = tickets._branch_names(repo, "br")
    assert name in names
    assert not any(n.startswith('"') or "\\3" in n for n in names)


def test_206_ascii_names_still_found(repo):
    _commit_on_branch(repo, "br", ["07-plain.md"])
    assert rounds.find_ticket(repo, 7, "br")[0] == "07-plain.md"
    assert "07-plain.md" in tickets._branch_names(repo, "br")


def test_206_two_non_ascii_tickets_of_one_number_are_ambiguous(repo):
    _commit_on_branch(repo, "br", ["07-а.md", "07-б.md"])
    with pytest.raises(rounds.RoundError, match="more than one ticket 7"):
        rounds.find_ticket(repo, 7, "br")


def test_206_missing_ticket_still_refuses(repo):
    _commit_on_branch(repo, "br", ["07-а.md"])
    with pytest.raises(rounds.RoundError, match="no ticket 8"):
        rounds.find_ticket(repo, 8, "br")


# ── 209 ─────────────────────────────────────────────────────────────────────
def test_209_unstaged_modification_named_whole(repo):
    (repo / TASKS / "01-a.md").write_text("# 1 a\n\n**Status:** open\n", encoding="utf-8")
    assert rounds._dirty_tasks(repo) == [f"{TASKS}/01-a.md"]


def test_209_untracked_named_whole(repo):
    (repo / TASKS / "02-b.md").write_text("x\n", encoding="utf-8")
    assert rounds._dirty_tasks(repo) == [f"{TASKS}/02-b.md"]


def test_209_staged_modification_named_whole(repo):
    (repo / TASKS / "01-a.md").write_text("changed\n", encoding="utf-8")
    _git(repo, "add", TASKS)
    assert rounds._dirty_tasks(repo) == [f"{TASKS}/01-a.md"]


@pytest.mark.parametrize("name", ["03-а.md", '03-"q".md', "03-é.md"])
def test_209_non_ascii_and_quote_names_are_raw(repo, name):
    (repo / TASKS / name).write_text("x\n", encoding="utf-8")
    assert rounds._dirty_tasks(repo) == [f"{TASKS}/{name}"]


def test_209_rename_gives_the_new_name_only(repo):
    _git(repo, "mv", f"{TASKS}/01-a.md", f"{TASKS}/01-renamed.md")
    got = rounds._dirty_tasks(repo)
    assert got == [f"{TASKS}/01-renamed.md"]


def test_209_clean_tree_is_empty(repo):
    assert rounds._dirty_tasks(repo) == []


def test_209_two_changes_each_whole(repo):
    (repo / TASKS / "01-a.md").write_text("changed\n", encoding="utf-8")
    (repo / TASKS / "09-z.md").write_text("x\n", encoding="utf-8")
    assert sorted(rounds._dirty_tasks(repo)) == [f"{TASKS}/01-a.md", f"{TASKS}/09-z.md"]


# ── 207 ─────────────────────────────────────────────────────────────────────
INI = """[arena.profile.p]
models = a/one,b/two
max_parallel = 3
writer = w/model
reviewer = r/model
"""


@pytest.fixture
def arena_repo(tmp_path, monkeypatch):
    r = tmp_path / "arena"
    r.mkdir()
    (r / "contest.ini").write_text(INI, encoding="utf-8")
    monkeypatch.setattr(cli, "REPO_ROOT", r)
    monkeypatch.setattr(models, "REPO_ROOT", r, raising=False)
    return r


def _run(argv, capsys):
    rc = cli.main(argv)
    cap = capsys.readouterr()
    return rc, cap.out + cap.err


def _key(repo, key):
    profiles, _ = cli.profile.load_profiles(repo)
    return (profiles.get("p", {}).get(key) or "").strip()


def _acceptable(rc, text, repo, key, local_before):
    """Either a refusal that names the committed file and writes nothing, or the key really gone."""
    if rc != 0:
        assert "contest.ini" in text, text
        assert _key(repo, key), "refused but the key is gone anyway"
        local = repo / roster.LOCAL_FILENAME
        assert (local.read_text(encoding="utf-8") if local.exists() else "") == local_before
    else:
        assert _key(repo, key) == "", f"exit 0 but {key} is still {_key(repo, key)!r}: silent no-op"


def test_207_profile_set_empty_removes_inherited_key(arena_repo, capsys):
    rc, text = _run(["profile", "set", "-y", "p", "max_parallel="], capsys)
    _acceptable(rc, text, arena_repo, "max_parallel", "")


@pytest.mark.parametrize("role", ["writer", "reviewer"])
def test_207_unset_role_removes_inherited_role(arena_repo, capsys, role):
    rc, text = _run(["-p", "p", "model", "unset-role", "-y", role], capsys)
    _acceptable(rc, text, arena_repo, role, "")


def test_207_key_only_in_local_file_still_removable(arena_repo, capsys):
    (arena_repo / roster.LOCAL_FILENAME).write_text("[arena.profile.p]\nlegs = 2\n", encoding="utf-8")
    rc, _ = _run(["profile", "set", "-y", "p", "legs="], capsys)
    assert rc == 0
    assert _key(arena_repo, "legs") == ""


def test_207_set_value_still_works(arena_repo, capsys):
    rc, _ = _run(["profile", "set", "-y", "p", "max_parallel=5"], capsys)
    assert rc == 0
    assert _key(arena_repo, "max_parallel") == "5"


def test_207_committed_file_is_never_edited(arena_repo, capsys):
    _run(["profile", "set", "-y", "p", "max_parallel="], capsys)
    _run(["-p", "p", "model", "unset-role", "-y", "writer"], capsys)
    assert (arena_repo / "contest.ini").read_text(encoding="utf-8") == INI


# ── 208 ─────────────────────────────────────────────────────────────────────
GOOD = {"id": "good-model", "pricing": {"prompt": "0", "completion": "0"},
        "capabilities": ["tool-use"], "context_window": 32000}

BODIES = {
    "clean": {"data": [GOOD]},
    "pricing-string": {"data": [{"id": "x", "pricing": "free"}, GOOD]},
    "pricing-list": {"data": [{"id": "x", "pricing": [1, 2]}, GOOD]},
    "currency-null": {"data": [{"id": "x", "pricing": {"currency": None, "prompt": "0"}}, GOOD]},
    "currency-number": {"data": [{"id": "x", "pricing": {"currency": 5}}, GOOD]},
    "caps-objects": {"data": [{"id": "x", "capabilities": [{"name": "tools"}]}, GOOD]},
    "caps-string": {"data": [{"id": "x", "capabilities": "tool-use"}, GOOD]},
    "caps-dict": {"data": [{"id": "x", "capabilities": {"tools": True}}, GOOD]},
    "min-plan-number": {"data": [{"id": "x", "pricing": {"prompt": "0"}, "min_plan": 3}, GOOD]},
    "id-object": {"data": [{"id": {"a": 1}}, GOOD]},
    "id-list": {"data": [{"id": ["a"]}, GOOD]},
    "ctx-string": {"data": [{"id": "x", "context_window": "big", "pricing": {"prompt": "0"}}, GOOD]},
    "ctx-object": {"data": [{"id": "x", "context_window": {"n": 1}, "pricing": {"prompt": "0"}}, GOOD]},
    "text-shape": {"text": [{"id": "x", "pricing": "p"}, GOOD]},
    "bare-list": [{"id": "x", "pricing": "p"}, GOOD],
}
ERRORS = {
    "error-object": {"error": {"message": "bad key", "code": 401}},
    "null": None,
    "string": "ok",
    "number": 7,
    "text-string": {"text": "Unauthorized"},
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        import json
        parts = self.path.strip("/").split("/")
        kind, case = parts[0], parts[1]
        if kind == "cut":
            body = b'{"data": [{"id": "a"}, {"id": "b"'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body) + 400))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True
            return
        payload = (BODIES if kind == "body" else ERRORS)[case]
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def server():
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1"
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def raw_garbage():
    """A server that answers with a line that is not HTTP at all, then closes."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)

    def loop():
        while True:
            try:
                c, _ = sock.accept()
            except OSError:
                return
            try:
                c.recv(4096)
                c.sendall(b"NOT HTTP AT ALL\r\n\r\n")
            finally:
                c.close()

    threading.Thread(target=loop, daemon=True).start()
    yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    sock.close()


def _ids(recs):
    return {r["model"] for r in recs}


@pytest.mark.parametrize("case", sorted(BODIES))
def test_208_direct_for_survives_a_malformed_entry(server, case):
    recs = models._direct_for("prov", f"{server}/body/{case}", "k", 0.0)
    assert "good-model" in _ids(recs), f"the well-formed model next to the bad one was lost: {recs}"


@pytest.mark.parametrize("case", sorted(BODIES))
def test_208_free_from_direct_api_survives_a_malformed_entry(server, case):
    from scripts import py_model_test as t
    rows = t.free_from_direct_api(f"{server}/body/{case}", "k")
    assert "good-model" in {r[0] for r in rows}


@pytest.mark.parametrize("case", sorted(ERRORS))
def test_208_error_replies_are_a_one_line_hint_or_empty(server, case):
    try:
        recs = models._direct_for("prov", f"{server}/err/{case}", "k", 0.0)
    except models.ModelError as err:
        assert "\n" not in str(err) and str(err)
    else:
        assert recs == []


def test_208_cut_body_is_a_one_line_model_error(server):
    with pytest.raises(models.ModelError) as ei:
        models._direct_for("prov", f"{server}/cut/x", "k", 0.0)
    assert "\n" not in str(ei.value)
    assert "prov" in str(ei.value)


def test_208_bad_status_line_is_a_one_line_model_error(raw_garbage):
    with pytest.raises(models.ModelError) as ei:
        models._direct_for("prov", raw_garbage, "k", 0.0)
    assert "\n" not in str(ei.value)


def test_208_key_is_not_leaked_in_the_hint(server):
    with pytest.raises(models.ModelError) as ei:
        models._direct_for("prov", f"{server}/cut/x", "SECRETKEY123", 0.0)
    assert "SECRETKEY123" not in str(ei.value)


def test_208_well_formed_reply_unchanged(server):
    recs = models._direct_for("prov", f"{server}/body/clean", "k", 0.0)
    good = [r for r in recs if r["model"] == "good-model"][0]
    assert good["provider"] == "prov" and good["via"] == "direct" and good["ctx"] == 32000


# ── 206, the other places the ticket lists (tickets.py, contest/cli.py, merge.py) ───────────────
ODD = ["05-а.md", '05-"q".md', "05-b\\s.md"]


@pytest.mark.parametrize("name", ODD)
def test_206_on_branch_lists_the_ticket(repo, name):
    _commit_on_branch(repo, "br", [name])
    assert tickets._on_branch(repo, "br", 5) == [name]


@pytest.mark.parametrize("name", ODD)
def test_206_blocking_tickets_sees_a_lower_odd_name(repo, name):
    _commit_on_branch(repo, "br", [name], status="open")
    got = tickets.blocking_tickets(repo, "br", 9)
    assert [b["name"] for b in got if b["number"] == 5] == [name]


@pytest.mark.parametrize("name", ODD)
def test_206_contest_tickets_at_a_commit_names_the_file(repo, name):
    from tools.contest import cli as ccli
    _commit_on_branch(repo, "br", [name])
    rows = ccli._tickets(repo / TASKS, at="br")
    assert name in [Path(r[1]).name for r in rows]


@pytest.mark.parametrize("name", ODD)
def test_206_contest_ticket_file_from_the_base(repo, name):
    from tools.contest import cli as ccli
    _commit_on_branch(repo, "br", [name])
    got, path = ccli.ticket_file(repo, repo / TASKS, 5, "br")
    assert got == name and path is not None and Path(path).read_text(encoding="utf-8").startswith("# t")


@pytest.mark.parametrize("name", ODD)
def test_206_merge_read_ticket_from_the_round_ref(repo, name):
    from tools.arena import merge
    _commit_on_branch(repo, "br", [name])
    _git(repo, "update-ref", f"{rounds.REF_PREFIX}5", "br")
    got = merge.read_ticket(repo, 5)
    assert got is not None and got[0] == name
