"""tests/test_arena_entry_merge.py — AR-8: `entry merge`, the winner's entry becomes one commit."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

import pytest

from tools.arena import merge, rounds
from tools.contest import cli as contest_cli

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
NN = 195
INI = "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = test/a\n"
TICKET = f"{NN}-ar-8-entry-merge.md"
BASE_TEXT = "def thing():\n    return 1\n"
PATCH_TAIL = ("\n---\n src/thing.py | 1 +\n 1 file changed, 1 insertion(+)\n\n"
              "diff --git a/src/thing.py b/src/thing.py\n")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _arg(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A clean checkout on `main` with round 195's ticket, INDEX row and base commit."""
    r = tmp_path / "repo"
    r.mkdir()
    _write(r / "contest.ini", INI)
    _write(r / "epic-tasks" / TICKET,
           f"# AR-8 — {NN}\n\n**Status:** open\n**Size:** M\n\nbody of {NN}\n")
    _write(r / "epic-tasks" / "INDEX.md",
           "# Index\n\n| 131 | `AR-1` | open — old row | MEDIUM | S | [t](x) | `a.py` |\n"
           f"| {NN} | `AR-8` | open — waiting | LARGE | S | [t](x) | `tools/arena/merge.py` |\n")
    _write(r / "src" / "thing.py", BASE_TEXT)
    _write(r / ".arena" / "rounds" / f"{NN}.json", json.dumps({"branch": "main"}))
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def _export(repo: Path, agent: str, *, message: str, body: str = "", commits: int = 1,
            state: str = "", files: list[str] | None = None) -> str:
    """*commits* commit(s) on top of the checkout, exported to `out/NN/<agent>.patch`
    and undone — the round's own output, and the tree back at its base."""
    base = _git(repo, "rev-parse", "HEAD")
    targets = files or (["src/thing.py"] if commits == 1
                        else [f"src/a.py", "src/b.py"][:commits])
    messages = [message] if commits == 1 else [f"{message} ({n})" for n in ("one", "two")][:commits]
    for index, (name, target) in enumerate(zip(messages, targets), start=2):
        _write(repo / target, f"def thing():\n    return {index}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", name, *(["-m", body] if body else []))
    out = repo / "out" / str(NN)
    out.mkdir(parents=True, exist_ok=True)
    name = f"{agent}.{state}patch" if state else f"{agent}.patch"
    _write(out / name, subprocess.run(
        ["git", "format-patch", f"-{commits}", f"HEAD~{commits}..HEAD", "--stdout"],
        cwd=str(repo), check=True, capture_output=True, text=True).stdout)
    _git(repo, "reset", "--hard", base)
    return base


def _roster(repo: Path, base: str, agent: str, source: str, *,
            model: str = "kenary/glm-4.7:free", extra: dict | None = None) -> None:
    """The round's `entrants.json` and `state.json`: one READY agent plus *extra*."""
    out = repo / "out" / str(NN)
    entrants = {agent: {"source": source}}
    agents = [{"agent": {"name": agent, "model_id": model, "provider_id": "kenary"},
               "workspace": {"path": "/x"}, "state": "READY"}]
    if extra:
        entrants.update(extra)
        agents += [{"agent": {"name": name, "model_id": model, "provider_id": "kenary"},
                    "workspace": {"path": "/x"}, "state": "READY"} for name in extra]
    _write(out / "entrants.json", json.dumps({"base": base, "entrants": entrants}))
    _write(out / "state.json", json.dumps(
        {"round_no": NN, "ticket": f"epic-tasks/{TICKET}", "base_sha": base,
         "started_at": "2026-10-06T00:00:00Z", "agents": agents}))


def _merge(capsys, repo: Path, agent: str, **kw) -> int:
    """`merge.entry_merge(repo, args, {})`: `-y` is `yes`, `--squash` and `--uncommitted` too."""
    return merge.entry_merge(
        repo, _arg(number=str(NN), agent=agent,
                   yes=bool(kw.pop("yes", False)), squash=bool(kw.pop("squash", False)),
                   uncommitted=bool(kw.pop("uncommitted", False)), passthrough=[]), {})


# ── 1. one commit, the checkout's identity, the trailer, the subject ─────────
def test_lands_one_commit_as_the_checkouts_identity(repo: Path, capsys) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo / "src" / "thing.py", BASE_TEXT.replace("1", "2"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--author", "Agent <agent@elsewhere.invalid>",
         "-m", "the agent's own subject", "-m", "the agent's own body")
    out = repo / "out" / str(NN)
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "agent.patch", subprocess.run(
        ["git", "format-patch", "-1", "HEAD", "--stdout"], cwd=str(repo), check=True,
        capture_output=True, text=True).stdout)
    _git(repo, "reset", "--hard", base)
    _roster(repo, base, "agent", f"out/{NN}/agent.patch")

    code = _merge(capsys, repo, "agent")
    cap = capsys.readouterr()

    assert code == merge.EXIT_OK, cap.err
    assert "merged" in cap.out
    assert _git(repo, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert _git(repo, "log", "-1", "--format=%an <%ae>") == "t <t@example.invalid>"
    assert _git(repo, "log", "-1", "--format=%s") == f"{NN}: the agent's own subject"
    assert _git(repo, "log", "-1", "--format=%b") == (
        "the agent's own body\n\nCo-Authored-By: glm-4-7 <glm-4-7@round-195.contest>")
    assert _git(repo, "diff", "--name-only", "HEAD~1..HEAD").splitlines() == ["src/thing.py"]


@pytest.mark.parametrize("subject,want", [
    ("bare subject", f"{NN}: bare subject"),
    (f"{NN}: already numbered", f"{NN}: already numbered"),
    ("AR-8: prefixed with the ticket", "AR-8: prefixed with the ticket"),
])
def test_subject_gets_the_round_prefix_once(repo: Path, subject: str, want: str, capsys) -> None:
    base = _export(repo, "agent", message=subject)
    _roster(repo, base, "agent", f"out/{NN}/agent.patch")
    assert _merge(capsys, repo, "agent") == merge.EXIT_OK
    assert _git(repo, "log", "-1", "--format=%s") == want


@pytest.mark.parametrize("subject_line,subject", [
    ("Subject: [PATCH 1/2] a rather long subject that is\n folded onto two lines",
     "a rather long subject that is folded onto two lines"),
    ("Subject: [PATCH] =?utf-8?Q?caf=C3=A9 r=C3=A9sum=C3=A9?=", "café résumé"),
])
def test_subject_is_unfolded_and_decoded(repo: Path, subject_line: str, subject: str) -> None:
    """The parser unfolds and RFC 2047-decodes; a `format-patch` bracket never survives."""
    _write(repo / "out" / str(NN) / "agent.patch",
           "From 0123456789abcdef Mon Sep 17 00:00:00 2001\nFrom: a <a@b.invalid>\n"
           f"{subject_line}\n\nbody here\n" + PATCH_TAIL)
    entry = merge.Entry(agent="agent", path=repo / "out" / str(NN) / "agent.patch",
                        state=None, is_diff=False, untracked=[])

    got_subject, message = merge.build_message(NN, entry, "model")

    assert got_subject == f"{NN}: {subject}"
    assert message.startswith(f"{NN}: {subject}\n\n")
    assert "Co-Authored-By: model <model@round-195.contest>" in message


# ── 2. the winner's bookkeeping is dropped ───────────────────────────────────
def test_dropped_paths_are_not_in_the_commit(repo: Path, capsys) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo / "src" / "thing.py", BASE_TEXT.replace("1", "2"))
    _write(repo / "epic-tasks" / f"{NN}-agent-work.md", "# the agent's own ticket\n")
    _write(repo / "runs" / "agent" / "PROGRESS.csv", "1,2,3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "work plus bookkeeping")
    out = repo / "out" / str(NN)
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "agent.patch", subprocess.run(
        ["git", "format-patch", "-1", "HEAD", "--stdout"], cwd=str(repo), check=True,
        capture_output=True, text=True).stdout)
    _git(repo, "reset", "--hard", base)
    _roster(repo, base, "agent", f"out/{NN}/agent.patch")

    assert _merge(capsys, repo, "agent") == merge.EXIT_OK
    cap = capsys.readouterr()

    assert _git(repo, "diff", "--name-only", "HEAD~1..HEAD").splitlines() == ["src/thing.py"]
    for dropped in (f"epic-tasks/{NN}-agent-work.md", "runs/agent/PROGRESS.csv"):
        assert dropped in cap.out
    assert (repo / "src" / "thing.py").read_text(encoding="utf-8").endswith("return 2\n")


# ── 3. a patch that will not apply ───────────────────────────────────────────
def test_non_applying_patch_leaves_the_tree_as_it_was(repo: Path, capsys) -> None:
    """Two commits on another branch: the second one's context is not on `main`, so it cannot apply."""
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "other")
    _write(repo / "src" / "thing.py", "def thing():\n    return 42\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the branch's first move")
    _write(repo / "src" / "thing.py", "def thing():\n    return 99\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "built on another branch")
    patch = subprocess.run(["git", "format-patch", "-1", "HEAD~1..HEAD", "--stdout"],
                           cwd=str(repo), check=True, capture_output=True, text=True).stdout
    _git(repo, "checkout", "-q", "-B", "main", base)
    _git(repo, "branch", "-D", "other")
    out = repo / "out" / str(NN)
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "agent.patch", patch)
    _roster(repo, base, "agent", f"out/{NN}/agent.patch")

    before = _git(repo, "status", "--porcelain")
    code = _merge(capsys, repo, "agent")
    cap = capsys.readouterr()

    assert code == merge.EXIT_FAILED
    assert _git(repo, "status", "--porcelain") == before
    assert _git(repo, "rev-list", "--count", f"{base}..HEAD") == "0"
    assert "<<<" not in (repo / "src" / "thing.py").read_text(encoding="utf-8")
    assert "git am -3" in cap.out


# ── 4. the flags that name the entry ─────────────────────────────────────────
def test_diff_needs_uncommitted(repo: Path, capsys) -> None:
    """A `.diff` is the agent's uncommitted tree; `--uncommitted` names that, and the
    names in its trailing comment are listed for the operator to copy by hand."""
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo / "src" / "thing.py", BASE_TEXT.replace("1", "7"))
    out = repo / "out" / str(NN)
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "agent.diff", subprocess.run(
        ["git", "diff", "--", "src/thing.py"], cwd=str(repo), check=True,
        capture_output=True, text=True).stdout + "\nUntracked files: new.py scratch.txt\n")
    _git(repo, "reset", "--hard", base)  # the diff is the agent's, not this tree's
    _roster(repo, base, "agent", f"out/{NN}/agent.diff")

    capsys.readouterr()
    assert _merge(capsys, repo, "agent") == merge.EXIT_USAGE
    assert "uncommitted" in capsys.readouterr().err

    _write(repo / "new.py", "print('new')\n")
    _write(repo / "scratch.txt", "junk\n")
    assert _merge(capsys, repo, "agent", uncommitted=True) == merge.EXIT_OK
    cap = capsys.readouterr()
    assert "new.py" in cap.out and "scratch.txt" in cap.out
    assert _git(repo, "log", "-1", "--format=%s") == (
        f"{NN}: agent's uncommitted tree, taken as-is")
    assert _git(repo, "diff", "--name-only", "HEAD~1..HEAD").splitlines() == ["src/thing.py"]


def test_stalled_patch_needs_yes(repo: Path, capsys) -> None:
    base = _export(repo, "agent", message="a stalled agent's commit", state="STALLED.")
    _roster(repo, base, "agent", f"out/{NN}/agent.STALLED.patch")

    capsys.readouterr()
    assert _merge(capsys, repo, "agent") == merge.EXIT_USAGE
    assert "STALLED" in capsys.readouterr().err
    assert _git(repo, "rev-list", "--count", f"{base}..HEAD") == "0"

    assert _merge(capsys, repo, "agent", yes=True) == merge.EXIT_OK
    assert _git(repo, "rev-list", "--count", f"{base}..HEAD") == "1"


def test_two_message_patch_is_refused_then_squashed(repo: Path, capsys) -> None:
    """Two commits on two different files both apply, so only the count refuses."""
    base = _export(repo, "agent", message="work", commits=2,
                   files=["src/a.py", "src/b.py"])
    _roster(repo, base, "agent", f"out/{NN}/agent.patch")

    capsys.readouterr()
    assert _merge(capsys, repo, "agent") == merge.EXIT_USAGE
    assert "2" in capsys.readouterr().err

    assert _merge(capsys, repo, "agent", squash=True) == merge.EXIT_OK
    cap = capsys.readouterr()
    assert _git(repo, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert "(squashed from 2 commits)" in _git(repo, "log", "-1", "--format=%b")
    assert cap.err == ""


# ── 5. the refusals, nothing staged ──────────────────────────────────────────
def test_refusals_before_anything_is_touched(repo: Path, capsys) -> None:
    base = _export(repo, "agent", message="work")
    _roster(repo, base, "agent", f"out/{NN}/agent.patch")

    # a round still running: a fake /proc with a pid whose cmdline names the ticket
    fake = repo / "fake-proc"
    (fake / "4242").mkdir(parents=True)
    (fake / "4242" / "cmdline").write_bytes(
        f"python3\x00-m\x00tools.contest\x00run\x00--ticket\x00{NN}\x00".encode())
    (fake / "4242" / "cwd").symlink_to(str(repo.resolve()))
    proc_root = rounds.PROC_ROOT
    rounds.PROC_ROOT = str(fake)
    try:
        capsys.readouterr()
        assert _merge(capsys, repo, "agent") == merge.EXIT_USAGE
        assert "is running" in capsys.readouterr().err
    finally:
        rounds.PROC_ROOT = proc_root

    capsys.readouterr()
    assert _merge(capsys, repo, "no-such-agent") == merge.EXIT_USAGE
    assert "no agent" in capsys.readouterr().err

    _write(repo / ".arena" / "rounds" / f"{NN}.json", json.dumps({"branch": "elsewhere"}))
    capsys.readouterr()
    assert _merge(capsys, repo, "agent") == merge.EXIT_USAGE
    assert "elsewhere" in capsys.readouterr().err
    _write(repo / ".arena" / "rounds" / f"{NN}.json", json.dumps({"branch": "main"}))

    _write(repo / "src" / "thing.py", "def thing():\n    return 5\n")
    capsys.readouterr()
    assert _merge(capsys, repo, "agent") == merge.EXIT_USAGE
    assert "working tree" in capsys.readouterr().err
    assert "M src/thing.py" in _git(repo, "status", "--porcelain").splitlines()


def test_no_git_identity_is_a_refusal(repo: Path, capsys, tmp_path, monkeypatch) -> None:
    base = _export(repo, "agent", message="work")
    _roster(repo, base, "agent", f"out/{NN}/agent.patch")
    empty = tmp_path / "empty.ini"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    _git(repo, "config", "--unset", "user.email")

    capsys.readouterr()
    code = _merge(capsys, repo, "agent")
    cap = capsys.readouterr()

    assert code == merge.EXIT_USAGE
    assert "identity" in cap.err
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"


# ── 8 and 9. no ticket commit on the branch, a rename lands whole ────────────
def test_the_branch_never_gains_a_ticket_commit(repo: Path, capsys) -> None:
    base = _export(repo, "agent", message="work")
    _roster(repo, base, "agent", f"out/{NN}/agent.patch")

    assert _merge(capsys, repo, "agent") == merge.EXIT_OK
    subjects = _git(repo, "log", "--format=%s").splitlines()
    assert f"{NN}: ticket for the round" not in subjects
    assert _git(repo, "branch", "--list", "contest-legs") == ""
    assert _git(repo, "branch", "--show-current") == "main"


def test_a_rename_lands_source_and_destination(repo: Path, capsys) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "mv", "src/thing.py", "src/renamed.py")
    _git(repo, "commit", "-q", "-m", "rename thing")
    out = repo / "out" / str(NN)
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "agent.patch", subprocess.run(
        ["git", "format-patch", "-1", "HEAD", "--stdout"], cwd=str(repo), check=True,
        capture_output=True, text=True).stdout)
    _git(repo, "reset", "--hard", base)
    _roster(repo, base, "agent", f"out/{NN}/agent.patch")

    assert _merge(capsys, repo, "agent") == merge.EXIT_OK
    # `--no-renames`: a plain diff would show the rename as one line and hide the deletion
    assert sorted(_git(repo, "show", "--name-only", "--format=", "--no-renames", "HEAD")
                  .splitlines()) == ["src/renamed.py", "src/thing.py"]
    assert not (repo / "src" / "thing.py").exists()
    assert (repo / "src" / "renamed.py").read_text(encoding="utf-8") == BASE_TEXT


# ── 10. the trailer's model, duplicates and the merge record ─────────────────
@pytest.mark.parametrize("model_id,want", [("kenary/glm-4.7:free", "glm-4-7"),
                                           ("kenary/x/y-6.8-z:free", "y-6-8-z")])
def test_trailer_model_is_the_rule_not_a_copy(repo: Path, capsys, model_id: str, want: str) -> None:
    """`<model>` is `agents_from_models`' own name, not a second copy of its rule."""
    assert want == contest_cli.agents_from_models(model_id)[0].name
    base = _export(repo, "agent", message="work")
    _roster(repo, base, "agent", f"out/{NN}/agent.patch", model=model_id)

    assert merge.trailer_model(repo / "out" / str(NN), "agent") == want
    assert _merge(capsys, repo, "agent") == merge.EXIT_OK
    assert f"Co-Authored-By: {want} <{want}@round-{NN}.contest>" in _git(
        repo, "log", "-1", "--format=%b")


def test_duplicate_of_uses_the_named_patch_and_keeps_other_rounds(repo: Path, capsys) -> None:
    """A `duplicate_of` entry carries no patch of its own: the named agent's is merged."""
    base = _export(repo, "agent", message="the original agent's work")
    _roster(repo, base, "agent", f"out/{NN}/agent.patch",
            extra={"copy": {"duplicate_of": "agent"}})
    _write(repo / ".arena" / "merged.json",
           json.dumps({"77": {"agent": "other", "sha": "f" * 40, "subject": "77: earlier"}}))

    assert _merge(capsys, repo, "copy") == merge.EXIT_OK
    assert _git(repo, "log", "-1", "--format=%s") == f"{NN}: the original agent's work"

    record = json.loads((repo / ".arena" / "merged.json").read_text(encoding="utf-8"))
    assert record["77"]["agent"] == "other"
    assert record[str(NN)]["agent"] == "copy"
    assert record[str(NN)]["sha"] == _git(repo, "rev-parse", "HEAD")


# ── the two commands in a row: two commits, the second names the first ───────
def test_entry_merge_then_issue_land_are_two_commits_and_the_second_names_the_first(
        repo: Path, capsys) -> None:
    base = _export(repo, "agent", message="make thing add up")
    _roster(repo, base, "agent", f"out/{NN}/agent.patch")
    assert _merge(capsys, repo, "agent") == merge.EXIT_OK
    entry = _git(repo, "rev-parse", "HEAD")
    assert json.loads((repo / ".arena" / "merged.json").read_text())[str(NN)]["sha"] == entry

    capsys.readouterr()
    code = merge.issue_land(repo, _arg(number=str(NN), score="19/19 on contest-bench/195",
                                       message="", note="", passthrough=[]), prof={})
    assert code == merge.EXIT_OK, capsys.readouterr().err
    land = _git(repo, "rev-parse", "HEAD")
    assert land != entry and _git(repo, "rev-list", "--count", f"{base}..HEAD") == "2"
    assert _git(repo, "diff", "--name-only", "HEAD~1..HEAD").splitlines() == [
        f"epic-tasks/{TICKET}", "epic-tasks/INDEX.md"]
    status = [line for line in (repo / "epic-tasks" / TICKET).read_text().splitlines()
              if line.startswith("**Status:**")]
    assert status == [f"**Status:** landed — round {NN}, winner agent "
                      f"(19/19 on contest-bench/195), {entry[:7]} as-is"]
    assert f"winner agent taken as-is in {entry[:7]}" in _git(repo, "log", "-1", "--format=%B")


def test_a_stalled_diff_needs_uncommitted_alone_and_no_yes(repo: Path, capsys) -> None:
    """The ticket's table: a `.STALLED.diff` is refused unless `--uncommitted`; `-y` is for patches."""
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo / "src" / "thing.py", BASE_TEXT.replace("1", "7"))
    out = repo / "out" / str(NN)
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "agent.STALLED.diff", subprocess.run(
        ["git", "diff", "--", "src/thing.py"], cwd=str(repo), check=True,
        capture_output=True, text=True).stdout)
    _git(repo, "reset", "--hard", base)
    _roster(repo, base, "agent", f"out/{NN}/agent.STALLED.diff")
    entrants = json.loads((out / "entrants.json").read_text())
    entrants["entrants"]["agent"]["state"] = "STALLED"
    _write(out / "entrants.json", json.dumps(entrants))
    capsys.readouterr()
    assert _merge(capsys, repo, "agent") == merge.EXIT_USAGE
    assert _merge(capsys, repo, "agent", uncommitted=True) == merge.EXIT_OK
