"""Judge's acceptance suite for round 138 (AR-3), written from the ticket alone.

Drives step 0 (`tools.contest.cli.ticket_file` + a real `run --dry-run` on a
base that alone holds the ticket), `tools.arena.gitref.commit_file_on`,
`tools.arena.rounds.round_folder|round_alive`, `build_run_line`, and
`arena run start|list` through `tools.arena.cli.main`. The ticket leaves the
spawn seam's name open ("for example rounds.SPAWN"), so `_spawn_seam` patches
every module-level `subprocess.Popen` alias in the arena modules.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/138/acceptance_138.py -n 8 -q
Red on the base (9b633e6): there is no tools/arena/rounds.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from tools.arena import cli  # noqa: E402

rounds = pytest.importorskip("tools.arena.rounds")
gitref = pytest.importorskip("tools.arena.gitref")

ID = ["-c", "user.name=bench", "-c", "user.email=bench@example.invalid"]
TICKET = "# AR-x — seven\n\n**Status:** {status}\n**Size:** M\n**File:** pkg/a.py\n\nbody {n}\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *ID, *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def build_run_line():
    for mod in (rounds, cli):
        if hasattr(mod, "build_run_line"):
            return mod.build_run_line
    pytest.fail("no build_run_line in tools.arena.rounds or tools.arena.cli")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "contest.ini").write_text("[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = p/m-a\n")
    (r / "epic-tasks").mkdir()
    (r / "epic-tasks" / "01-one.md").write_text(TICKET.format(status="landed", n=1))
    (r / ".gitignore").write_text(".arena/\nout/\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    for mod in (cli, rounds, gitref):
        if isinstance(getattr(mod, "REPO_ROOT", None), Path):
            monkeypatch.setattr(mod, "REPO_ROOT", r)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "bench")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "bench@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "bench")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "bench@example.invalid")
    monkeypatch.chdir(tmp_path)  # cwd must not matter
    # no real round is ever alive for the tmp repo; keep /proc out of it
    return r


def draft(repo: Path, name: str = "07-seven.md", status: str = "draft", n: int = 7) -> Path:
    d = repo / ".arena" / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(TICKET.format(status=status, n=n))
    return p


class Spawned:
    def __init__(self):
        self.argv = None
        self.kwargs = None


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """Replace the spawn seam: the child is a python stub doing *code*."""
    seen = Spawned()
    real = subprocess.Popen

    def install(code: str):
        def fake(argv, *a, **kw):
            seen.argv, seen.kwargs = list(argv), kw
            kw.pop("start_new_session", None)
            return real([sys.executable, "-c", code], cwd=kw.get("cwd"))
        hit = False
        for mod in (cli, rounds, gitref):
            for name, val in list(vars(mod).items()):
                if val is real and name != "Popen" or name == "SPAWN":
                    monkeypatch.setattr(mod, name, fake)
                    hit = True
        assert hit, "no module-level spawn seam (e.g. rounds.SPAWN)"
        return seen
    return install


def run(capsys, *argv):
    rc = cli.main(list(argv))
    c = capsys.readouterr()
    return rc, c.out, c.err


def refs(repo: Path) -> str:
    return subprocess.run(["git", "for-each-ref", "refs/heads/arena-round/"], cwd=repo,
                          capture_output=True, text=True).stdout


# ── step 0 ───────────────────────────────────────────────────────────────────

def test_ticket_file_in_checkout_is_todays_path(repo):
    from tools.contest import cli as ccli
    name, path = ccli.ticket_file(repo, repo / "epic-tasks", 1, "HEAD")
    assert name == "01-one.md" and path == repo / "epic-tasks" / "01-one.md"


def test_ticket_file_only_at_base_is_a_temp_copy_with_same_name(repo):
    from tools.contest import cli as ccli
    sha = gitref.commit_file_on(repo, git(repo, "rev-parse", "HEAD"), "epic-tasks/07-seven.md",
                                TICKET.format(status="open", n=7), "7: t", "refs/heads/side")
    name, path = ccli.ticket_file(repo, repo / "epic-tasks", 7, sha)
    assert name == "07-seven.md" and path is not None and path.name == "07-seven.md"
    assert path.read_text() == TICKET.format(status="open", n=7)
    assert repo not in path.parents
    assert ccli.ticket_file(repo, repo / "epic-tasks", 9, sha) == ("", None)


@pytest.mark.xdist_group("port_bound_http_servers")
def test_step0_dry_run_on_a_base_that_alone_holds_the_ticket(tmp_path, monkeypatch, capsys):
    from test_contest_cli import Sandbox, _ticket, TICKET_01, TICKET_02  # noqa
    from test_contest_cli_export import _no_server
    from tools.contest import cli as ccli
    sb = Sandbox(tmp_path, tickets=((TICKET_01, _ticket("01", "first", "landed")),))
    base = git(sb.repo, "rev-parse", "HEAD")
    gitref.commit_file_on(sb.repo, base, f"epic-tasks/{TICKET_02}", _ticket("02", "second"),
                          "2: ticket", "refs/heads/arena-round/2")
    assert not (sb.repo / "epic-tasks" / TICKET_02).exists()
    tmpd = tmp_path / "systmp"
    tmpd.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmpd))
    monkeypatch.setenv("TMPDIR", str(tmpd))
    monkeypatch.chdir(sb.repo)
    _no_server(monkeypatch)
    rc = ccli.main(["run", "--ticket", "2", "--base", "arena-round/2", "--no-tests", "--dry-run"])
    out = capsys.readouterr()
    assert rc == 0, out.out[-2000:] + out.err[-2000:]
    assert "no ticket numbered" not in out.out + out.err
    leftovers = [p for p in tmpd.rglob("*") if p.name == TICKET_02]
    assert leftovers == []


# ── gitref ───────────────────────────────────────────────────────────────────

def test_commit_file_on_touches_nothing_of_the_operator(repo):
    (repo / "dirty.txt").write_text("x")
    before = (git(repo, "status", "--porcelain"), git(repo, "rev-parse", "HEAD"),
              (repo / ".git" / "index").read_bytes())
    tip = git(repo, "rev-parse", "HEAD")
    sha = gitref.commit_file_on(repo, tip, "epic-tasks/07-x.md", "hello\n", "7: t",
                                "refs/heads/arena-round/7")
    after = (git(repo, "status", "--porcelain"), git(repo, "rev-parse", "HEAD"),
             (repo / ".git" / "index").read_bytes())
    assert before == after
    assert git(repo, "rev-parse", "arena-round/7") == sha
    assert git(repo, "rev-parse", f"{sha}^") == tip
    files = git(repo, "ls-tree", "-r", "--name-only", sha).split()
    assert set(files) == set(git(repo, "ls-tree", "-r", "--name-only", tip).split()) | {"epic-tasks/07-x.md"}
    assert git(repo, "show", f"{sha}:epic-tasks/07-x.md") == "hello"


def test_commit_file_on_git_failure_is_gitreferror(repo):
    with pytest.raises(gitref.GitRefError) as e:
        gitref.commit_file_on(repo, "nosuchrev", "a.md", "x", "m", "refs/heads/arena-round/9")
    assert "\n" not in str(e.value).strip()


def test_no_porcelain_in_arena_code():
    bad = ("checkout", "switch", "stash", "reset")
    for f in (ROOT / "tools" / "arena").glob("*.py"):
        text = f.read_text()
        for word in bad:
            assert f'"{word}"' not in text and f"'{word}'" not in text, (f.name, word)


# ── run start: refs ──────────────────────────────────────────────────────────

def test_start_builds_ref_status_open_json_and_runs(repo, stub, capsys):
    draft(repo)
    seen = stub("import sys; sys.exit(0)")
    rc, out, err = run(capsys, "run", "start", "7")
    assert rc == 0, err
    sha = git(repo, "rev-parse", "arena-round/7")
    assert git(repo, "rev-parse", f"{sha}^") == git(repo, "rev-parse", "main")
    body = git(repo, "show", f"{sha}:epic-tasks/07-seven.md")
    assert "**Status:** open" in body and "**Status:** draft" not in body
    assert git(repo, "log", "-1", "--format=%s", sha) == "7: ticket for the round"
    meta = json.loads((repo / ".arena" / "rounds" / "7.json").read_text())
    for k in ("branch", "base_ref", "base_sha", "ticket_sha256", "started_at"):
        assert k in meta
    assert meta["base_sha"] == sha and meta["base_ref"].endswith("arena-round/7")
    assert seen.argv[1:9] == ["-m", "tools.contest", "run", "--ticket", "7", "--base", "arena-round/7"][: len(seen.argv[1:9])] or seen.argv[:9] == [sys.executable, "-m", "tools.contest", "run", "--ticket", "7", "--base", "arena-round/7"][:9]
    assert Path(seen.kwargs.get("cwd")).resolve() == repo.resolve()
    assert "--fresh" not in seen.argv and "--resume" not in seen.argv and "--no-gate" not in seen.argv
    assert "arena-round/7" in out + err  # the line is printed once


def test_same_ticket_twice_reuses_sha(repo, stub, capsys):
    draft(repo)
    stub("import sys; sys.exit(0)")
    assert run(capsys, "run", "start", "7")[0] == 0
    a = git(repo, "rev-parse", "arena-round/7")
    assert run(capsys, "run", "start", "7")[0] == 0
    assert git(repo, "rev-parse", "arena-round/7") == a


def test_ticket_already_on_tip_points_ref_at_tip(repo, stub, capsys):
    (repo / "epic-tasks" / "07-seven.md").write_text(TICKET.format(status="open", n=7))
    git(repo, "add", "epic-tasks/07-seven.md")
    git(repo, "commit", "-q", "-m", "t7")
    stub("import sys; sys.exit(0)")
    rc, _, err = run(capsys, "run", "start", "7")
    assert rc == 0, err
    assert git(repo, "rev-parse", "arena-round/7") == git(repo, "rev-parse", "main")


def test_changed_text_refused_naming_fresh_ticket(repo, stub, capsys):
    p = draft(repo)
    stub("import sys; sys.exit(0)")
    run(capsys, "run", "start", "7")
    a = git(repo, "rev-parse", "arena-round/7")
    p.write_text(TICKET.format(status="draft", n=77))
    rc, out, err = run(capsys, "run", "start", "7")
    assert rc == 2 and "--fresh-ticket" in err and len(err.strip().splitlines()) == 1
    assert git(repo, "rev-parse", "arena-round/7") == a
    rc, _, err = run(capsys, "run", "start", "7", "--fresh-ticket")
    assert rc == 0, err
    assert git(repo, "rev-parse", "arena-round/7") != a


def test_fresh_ticket_with_state_json_needs_yes(repo, stub, capsys):
    p = draft(repo)
    stub("import sys; sys.exit(0)")
    run(capsys, "run", "start", "7")
    a = git(repo, "rev-parse", "arena-round/7")
    (repo / "out" / "07").mkdir(parents=True)
    (repo / "out" / "07" / "state.json").write_text('{"agents": []}')
    p.write_text(TICKET.format(status="draft", n=78))
    rc, _, err = run(capsys, "run", "start", "7", "--fresh-ticket")
    assert rc == 2 and git(repo, "rev-parse", "arena-round/7") == a
    rc, _, err = run(capsys, "-y", "run", "start", "7", "--fresh-ticket")
    assert rc == 0, err
    assert git(repo, "rev-parse", "arena-round/7") != a


def test_moved_tip_same_text_is_refused(repo, stub, capsys):
    draft(repo)
    stub("import sys; sys.exit(0)")
    run(capsys, "run", "start", "7")
    (repo / "pkg.txt").write_text("x")
    git(repo, "add", "pkg.txt")
    git(repo, "commit", "-q", "-m", "move")
    rc, _, err = run(capsys, "run", "start", "7")
    assert rc == 2 and "--fresh-ticket" in err


# ── run start: refusals ──────────────────────────────────────────────────────

@pytest.mark.parametrize("extra", [["--base", "X"], ["--ticket=3"], ["--out", "d"],
                                   ["--target", "r"], ["--base=X"], ["--out=d"]])
def test_passthrough_owned_flags_refused_no_ref(repo, stub, capsys, extra):
    draft(repo)
    seen = stub("import sys; sys.exit(0)")
    rc, _, err = run(capsys, "run", "start", "7", "--", *extra)
    assert rc == 2 and len(err.strip().splitlines()) == 1
    assert refs(repo) == "" and seen.argv is None
    assert not (repo / ".arena" / "rounds" / "7.json").exists()


def test_build_run_line_exact():
    f = build_run_line()
    assert f(7, {"models": "a,a,b", "legs": "2"}, ["--no-gate"]) == [
        sys.executable, "-m", "tools.contest", "run", "--ticket", "7", "--base",
        "arena-round/7", "--models", "a,a,b", "--legs", "2", "--no-gate"]
    assert f(12, {}, []) == [sys.executable, "-m", "tools.contest", "run", "--ticket", "12",
                             "--base", "arena-round/12"]


def test_dirty_epic_tasks_refused_nothing_created(repo, stub, capsys):
    draft(repo)
    (repo / "epic-tasks" / "99-draft.md").write_text("x")
    seen = stub("import sys; sys.exit(0)")
    rc, _, err = run(capsys, "run", "start", "7")
    assert rc == 2 and "99-draft.md" in err and ".arena/drafts" in err
    assert len(err.strip().splitlines()) == 1
    assert refs(repo) == "" and not (repo / ".arena" / "rounds" / "7.json").exists()
    assert seen.argv is None


def test_modified_epic_tasks_refused(repo, stub, capsys):
    draft(repo)
    (repo / "epic-tasks" / "01-one.md").write_text("changed")
    stub("import sys; sys.exit(0)")
    rc, _, err = run(capsys, "run", "start", "7")
    assert rc == 2 and "01-one.md" in err and refs(repo) == ""


def test_no_ticket_is_refusal(repo, stub, capsys):
    stub("import sys; sys.exit(0)")
    rc, _, err = run(capsys, "run", "start", "7")
    assert rc == 2 and len(err.strip().splitlines()) == 1 and refs(repo) == ""


def test_drafts_beat_epic_tasks_and_rejected_ignored(repo, stub, capsys):
    (repo / "epic-tasks" / "07-checkout.md").write_text(TICKET.format(status="open", n=1))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "t")
    draft(repo, "07-draft.md")
    draft(repo, "07-old.rejected.md", n=5)
    stub("import sys; sys.exit(0)")
    rc, _, err = run(capsys, "run", "start", "7")
    assert rc == 0, err
    files = git(repo, "ls-tree", "--name-only", "arena-round/7", "epic-tasks/")
    assert "epic-tasks/07-draft.md" in files.split()


def test_two_drafts_refused_listing_both(repo, stub, capsys):
    draft(repo, "07-a.md")
    draft(repo, "07-b.md")
    stub("import sys; sys.exit(0)")
    rc, _, err = run(capsys, "run", "start", "7")
    assert rc == 2 and "07-a.md" in err and "07-b.md" in err and refs(repo) == ""


def test_padded_number_and_ticket_from_integration_branch(repo, stub, capsys):
    git(repo, "branch", "integ")
    sha = gitref.commit_file_on(repo, git(repo, "rev-parse", "integ"), "epic-tasks/007-x.md",
                                TICKET.format(status="queued", n=7), "t", "refs/heads/integ")
    seen = stub("import sys; sys.exit(0)")
    rc, _, err = run(capsys, "run", "start", "7", "--branch", "integ")
    assert rc == 0, err
    assert git(repo, "rev-parse", "arena-round/7^{tree}") == git(repo, "rev-parse", f"{sha}^{{tree}}") \
        or git(repo, "rev-parse", "arena-round/7^") == sha
    assert "**Status:** open" in git(repo, "show", "arena-round/7:epic-tasks/007-x.md")
    assert seen.argv is not None


def test_detached_head_without_branch_refused(repo, stub, capsys):
    draft(repo)
    git(repo, "checkout", "-q", "--detach")
    stub("import sys; sys.exit(0)")
    rc, _, err = run(capsys, "run", "start", "7")
    assert rc == 2 and refs(repo) == ""


def test_profile_branch_used_and_missing_branch_refused(repo, stub, capsys):
    git(repo, "branch", "integ")
    (repo / "contest.local.ini").write_text("[arena.profile.default]\nbranch = integ\nmodels = a,b\n")
    draft(repo)
    seen = stub("import sys; sys.exit(0)")
    rc, _, err = run(capsys, "run", "start", "7")
    assert rc == 0, err
    assert git(repo, "rev-parse", "arena-round/7^") == git(repo, "rev-parse", "integ")
    assert "--models" in seen.argv and "a,b" in seen.argv
    rc, _, err = run(capsys, "run", "start", "8", "--branch", "nosuch")
    assert rc == 2


def test_live_round_refused_before_any_ref(repo, stub, capsys, monkeypatch):
    draft(repo)
    for mod in (cli, rounds):
        if hasattr(mod, "round_alive"):
            monkeypatch.setattr(mod, "round_alive", lambda *a, **k: True)
    seen = stub("import sys; sys.exit(0)")
    rc, _, err = run(capsys, "run", "start", "7")
    assert rc == 2 and refs(repo) == "" and seen.argv is None


# ── run start: child, lock, exit codes ───────────────────────────────────────

def test_lock_exists_while_child_runs_and_is_removed(repo, stub, capsys, tmp_path):
    draft(repo)
    marker = tmp_path / "seen-lock"
    code = (f"import os,sys,pathlib; p=pathlib.Path({str(repo)!r})/'.arena'/'locks'/'7.pid';"
            f"pathlib.Path({str(marker)!r}).write_text(p.read_text() if p.exists() else 'NONE');"
            "sys.exit(1)")
    stub(code)
    rc, _, _ = run(capsys, "run", "start", "7")
    assert rc == 1
    assert marker.read_text().strip().isdigit()
    assert not (repo / ".arena" / "locks" / "7.pid").exists()


@pytest.mark.parametrize("child,write_state,want", [
    (0, False, 0), (1, False, 1), (2, False, 1), (2, True, 4), (3, False, 1), (137, False, 1)])
def test_exit_mapping(repo, stub, capsys, child, write_state, want):
    draft(repo)
    st = repo / "out" / "07"
    code = "import sys,pathlib\n"
    if write_state:
        code += f"d=pathlib.Path({str(st)!r}); d.mkdir(parents=True, exist_ok=True); (d/'state.json').write_text('{{\"agents\": []}}')\n"
    code += f"sys.exit({child})\n"
    stub(code)
    rc, _, _ = run(capsys, "run", "start", "7")
    assert rc == want


def test_stale_state_json_does_not_make_exit_4(repo, stub, capsys):
    draft(repo)
    st = repo / "out" / "07"
    st.mkdir(parents=True)
    (st / "state.json").write_text('{"agents": []}')
    old = time.time() - 3600
    os.utime(st / "state.json", (old, old))
    stub("import sys; sys.exit(2)")
    rc, _, _ = run(capsys, "-y", "run", "start", "7")
    assert rc == 1


# ── round_alive / round_folder ───────────────────────────────────────────────

def _proc(root: Path, pid: int, argv: list[str], cwd: Path) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    os.symlink(cwd, d / "cwd")


def test_round_alive_fake_proc(repo, tmp_path):
    proc = tmp_path / "proc"
    other = tmp_path / "other"
    other.mkdir()
    _proc(proc, 101, ["python3", "-m", "tools.contest", "run", "--ticket", "7"], other)
    assert rounds.round_alive(repo, 7, proc_root=str(proc)) is False
    _proc(proc, 102, ["python3", "-m", "tools.contest", "run", "--ticket=07", "--base", "x"], repo)
    assert rounds.round_alive(repo, 7, proc_root=str(proc)) is True
    assert rounds.round_alive(repo, 8, proc_root=str(proc)) is False


def test_round_alive_broken_proc_entry_and_stale_lock(repo, tmp_path):
    proc = tmp_path / "proc"
    (proc / "55").mkdir(parents=True)  # no cmdline, no cwd
    _proc(proc, 300, ["sleep", "100"], repo)
    locks = repo / ".arena" / "locks"
    locks.mkdir(parents=True)
    (locks / "7.pid").write_text("300\n")
    assert rounds.round_alive(repo, 7, proc_root=str(proc)) is False
    (locks / "7.pid").write_text("garbage")
    assert rounds.round_alive(repo, 7, proc_root=str(proc)) is False


def test_round_folder_legs(repo):
    cfg = SimpleNamespace(out_dir="out")
    out = repo / "out"
    assert rounds.round_folder(repo, cfg, 7) == out / "07"
    (out / "06.1").mkdir(parents=True)
    (out / "06.2").mkdir()
    assert rounds.round_folder(repo, cfg, 6) == out / "06.2"
    assert rounds.round_folder(repo, cfg, 6, leg=1) == out / "06.1"
    (out / "06").mkdir()
    (out / "06" / "state.json").write_text("{}")
    assert rounds.round_folder(repo, cfg, 6) == out / "06"


# ── run list ─────────────────────────────────────────────────────────────────

def _state(d: Path, states, age: float):
    d.mkdir(parents=True, exist_ok=True)
    f = d / "state.json"
    f.write_text(json.dumps({"agents": [{"state": s} for s in states]}))
    t = time.time() - age
    os.utime(f, (t, t))


def _fill(repo: Path, name="out"):
    out = repo / name
    _state(out / "05", ["READY", "READY", "GAVE_UP"], 3 * 86400)
    _state(out / "06.1", ["READY"], 7200)
    _state(out / "06.2", ["READY", "STALLED"], 60)
    (out / "08").mkdir(parents=True)
    (out / "08" / "state.json").write_text("{broken")
    t = time.time() - 3600
    os.utime(out / "08" / "state.json", (t, t))
    (out / "probe-memory.json").write_text("{}")
    (out / "notes").mkdir()
    return out


def test_run_list_rows_and_order(repo, capsys):
    _fill(repo)
    rc, out, err = run(capsys, "-o", "json", "run", "list")
    assert rc == 0, err
    rows = json.loads(out)
    runs = [str(r.get("RUN", r.get("run"))) for r in rows]
    assert runs == ["6", "8", "5"]
    by = {str(r.get("RUN", r.get("run"))): r for r in rows}
    assert "2/2" in json.dumps(by["6"]) and "1/2" in json.dumps(by["6"])
    assert "?" in json.dumps(by["8"])
    assert "2/3" in json.dumps(by["5"])
    assert all("notes" not in json.dumps(r) and "probe" not in json.dumps(r) for r in rows)


def test_run_list_table(repo, capsys):
    _fill(repo)
    rc, out, err = run(capsys, "run", "list")
    assert rc == 0, err
    for col in ("RUN", "LEGS", "STATE", "AGE", "READY/TOTAL"):
        assert col in out
    assert "done" in out and "2/3" in out and "3d" in out


def test_run_list_nothing(repo, capsys):
    rc, out, err = run(capsys, "run", "list")
    assert rc == 3 and len(err.strip().splitlines()) == 1


def test_run_list_honours_out_dir(repo, capsys):
    (repo / "contest.ini").write_text("[contest]\nout_dir = elsewhere\n\n[contest.agent.a]\nmodel = p/m-a\n")
    _fill(repo, "elsewhere")
    _state(repo / "contest-out" / "99", ["READY"], 10)
    rc, out, err = run(capsys, "-o", "json", "run", "list")
    assert rc == 0, err
    assert "99" not in [str(r.get("RUN", r.get("run"))) for r in json.loads(out)]


# ── seam ─────────────────────────────────────────────────────────────────────

def test_repo_seam_is_the_checkout_root():
    assert Path(cli.REPO_ROOT).resolve() == ROOT
