"""run_one_kc4.py <worktree> <scenario> — run ONE black-box scenario against
ONE entrant's tools/contest/workspace.py, on a disposable sandbox git repo
built fresh for every call (never the entrant's own checkout, never this
repo). Prints one line `@@RESULT@@{json}` with per-check pass/fail + detail.

This mirrors contest-bench/harness's method (README.md) but for KC-4: there
is no LLM/provider layer to stub — the black box here is real `git`, and the
"fake provider" is simply a throwaway two-commit repo with a committed
epic-tasks/ folder, built the same way the ticket's own Acceptance list
describes it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

WT = Path(sys.argv[1]).resolve()
SCEN = sys.argv[2]
sys.path.insert(0, str(WT))

IDENT = ["-c", "user.name=kc4-bench", "-c", "user.email=kc4-bench@local"]


def sh(cwd, *args, check=True):
    p = subprocess.run(["git", *IDENT, *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)} (in {cwd})\n{p.stdout}{p.stderr}")
    return p


def make_sandbox(root: Path) -> str:
    """A minimal repo: one commit, then a second commit adding epic-tasks/
    (committed, per the ticket's own setup). Returns the base sha (HEAD)."""
    root.mkdir(parents=True)
    sh(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("kc4 sandbox\n")
    sh(root, "add", "README.md")
    sh(root, "commit", "-q", "-m", "init")
    (root / "epic-tasks").mkdir()
    (root / "epic-tasks" / "INDEX.md").write_text("- ticket one\n")
    sh(root, "add", "epic-tasks/INDEX.md")
    sh(root, "commit", "-q", "-m", "epic-tasks")
    return sh(root, "rev-parse", "HEAD").stdout.strip()


class R:
    def __init__(self):
        self.chk = []

    def ck(self, name, ok, detail=""):
        self.chk.append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})

    def as_dict(self):
        return {"checks": self.chk}


def load_workspace():
    mod_path = WT / "tools" / "contest" / "workspace.py"
    if not mod_path.exists():
        return None, f"missing {mod_path}"
    spec = importlib.util.spec_from_file_location("kc4_workspace", mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["kc4_workspace"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None, traceback.format_exc()
    return mod, None


def make_config(rounds_dir, agent_names):
    """The real tools.contest.roster.ContestConfig/AgentSpec — KC-4 does not
    touch roster.py, so every entrant is built against the same real type,
    not a hand-rolled stand-in."""
    from tools.contest.roster import AgentSpec, ContestConfig
    agents = tuple(AgentSpec(name=n, provider_id="kenary", model_id=f"{n}:free")
                    for n in agent_names)
    return ContestConfig(rounds_dir=str(rounds_dir), agents=agents)


def call_prepare_round(mod, repo, config, round_no, base_ref, clones=None, force=None):
    import inspect
    sig = inspect.signature(mod.prepare_round)
    kwargs = {}
    if clones is not None and "clones" in sig.parameters:
        kwargs["clones"] = clones
    if force is not None:
        for name in ("force", "force_clone"):
            if name in sig.parameters:
                kwargs[name] = force
                break
    return mod.prepare_round(repo, config, round_no, base_ref, **kwargs)


def run(scenario: str) -> dict:
    r = R()
    mod, err = load_workspace()
    if mod is None:
        r.ck("import", False, err)
        return r.as_dict()
    r.ck("import", True)

    tmp = Path(tempfile.mkdtemp(prefix="kc4-sbx-"))
    try:
        repo = tmp / "repo"
        base_sha = make_sandbox(repo)
        rounds_dir = tmp / "rounds"
        agents = ["alice", "bob"]
        config = make_config(rounds_dir, agents)
        head_before = sh(repo, "rev-parse", "HEAD").stdout.strip()
        status_before = sh(repo, "status", "--porcelain").stdout

        if scenario == "fresh_round":
            wss = call_prepare_round(mod, repo, config, 1, "main", )
            r.ck("returned_n", len(wss) == len(agents), f"got {len(wss)}")
            for ws, agent in zip(wss, agents):
                p = Path(ws.path)
                sha = sh(p, "rev-parse", "HEAD").stdout.strip()
                r.ck(f"{agent}.at_base", sha == base_sha, sha)
                r.ck(f"{agent}.runs_empty", not any((p / "runs" / agent).glob("*")) if (p / "runs" / agent).exists() else True)
                r.ck(f"{agent}.clean", sh(p, "status", "--porcelain").stdout.strip() == "")

        elif scenario == "rerun_after_dirty_crash":
            wss = call_prepare_round(mod, repo, config, 2, "main")
            ws0 = wss[0]
            p = Path(ws0.path)
            (p / "scratch.txt").write_text("mid-run garbage\n")
            runs_agent = p / "runs" / agents[0]
            runs_agent.mkdir(parents=True, exist_ok=True)
            (runs_agent / "PROGRESS.csv").write_text("task1,done\n")
            wss2 = call_prepare_round(mod, repo, config, 2, "main")
            p2 = Path(wss2[0].path)
            sha = sh(p2, "rev-parse", "HEAD").stdout.strip()
            r.ck("reset_to_base", sha == base_sha, sha)
            r.ck("runs_cleared", not (p2 / "runs" / agents[0] / "PROGRESS.csv").exists())
            r.ck("status_clean", sh(p2, "status", "--porcelain").stdout.strip() == "")

        elif scenario == "foreign_folder_refused":
            # Learn this entrant's own <label>-<agent> naming (padded or not)
            # from a real round instead of guessing it, then reuse that exact
            # naming for the NEXT round number so the probe never depends on
            # one entrant's zero-padding convention.
            probe = call_prepare_round(mod, repo, config, 20, "main")
            p20 = Path(probe[0].path)
            sh(repo, "worktree", "remove", "--force", str(p20))
            sh(repo, "branch", "-D", probe[0].branch, check=False)
            path_guess = Path(str(p20).replace("20-", "21-", 1))
            if path_guess == p20:  # naming didn't contain "20-" verbatim
                path_guess = p20.parent / f"21-{agents[0]}"
            path_guess.mkdir(parents=True)
            (path_guess / "not_a_worktree.txt").write_text("hi\n")
            threw = False
            try:
                call_prepare_round(mod, repo, config, 21, "main")
            except Exception as e:
                threw = True
                r.ck("raised", True, type(e).__name__)
            if not threw:
                r.ck("raised", False, "no exception")
            r.ck("left_untouched", (path_guess / "not_a_worktree.txt").exists())

        elif scenario == "untracked_epic_tasks_refused":
            (repo / "epic-tasks" / "UNTRACKED.md").write_text("oops\n")
            threw = False
            try:
                call_prepare_round(mod, repo, config, 4, "main")
            except Exception as e:
                threw = True
                r.ck("raised", True, str(e)[:200])
                r.ck("mentions_epic_tasks", "epic-tasks" in str(e))
            if not threw:
                r.ck("raised", False, "no exception")

        elif scenario == "unresolvable_base":
            threw = False
            try:
                call_prepare_round(mod, repo, config, 5, "not-a-real-ref-zzz")
            except Exception as e:
                threw = True
                r.ck("raised", True, type(e).__name__)
            if not threw:
                r.ck("raised", False, "no exception")

        elif scenario == "idempotent_reset_worktree":
            import inspect
            sig = inspect.signature(mod.reset_worktree)
            ws1 = mod.reset_worktree(repo, str(rounds_dir), 6, "carol", base_sha)
            p = Path(ws1.path)
            sha1 = sh(p, "rev-parse", "HEAD").stdout.strip()
            ws2 = mod.reset_worktree(repo, str(rounds_dir), 6, "carol", base_sha)
            p2 = Path(ws2.path)
            sha2 = sh(p2, "rev-parse", "HEAD").stdout.strip()
            r.ck("same_sha_twice", sha1 == sha2 == base_sha)
            r.ck("clean_after_second", sh(p2, "status", "--porcelain").stdout.strip() == "")

        elif scenario == "attach_clone_dirty_then_force":
            clone = tmp / "clone"
            sh(tmp, "clone", "-q", str(repo), str(clone))
            (clone / "dirty.txt").write_text("uncommitted\n")
            threw = False
            try:
                mod.attach_clone(clone, "dave", base_sha, "contest/7/dave")
            except Exception as e:
                threw = True
                r.ck("raised_without_force", True, type(e).__name__)
            if not threw:
                r.ck("raised_without_force", False, "no exception")
            ws = mod.attach_clone(clone, "dave", base_sha, "contest/7/dave", force=True)
            sha = sh(Path(ws.path), "rev-parse", "HEAD").stdout.strip()
            r.ck("force_resets_to_base", sha == base_sha, sha)
            r.ck("clean_after_force", sh(Path(ws.path), "status", "--porcelain").stdout.strip() == "")

        elif scenario == "attach_clone_missing_base":
            other = tmp / "other_repo"
            other.mkdir()
            sh(other, "init", "-q", "-b", "main")
            (other / "x.txt").write_text("x\n")
            sh(other, "add", "x.txt")
            sh(other, "commit", "-q", "-m", "unrelated")
            threw = False
            try:
                mod.attach_clone(other, "erin", base_sha, "contest/8/erin")
            except Exception as e:
                threw = True
                r.ck("raised", True, type(e).__name__)
            if not threw:
                r.ck("raised", False, "no exception")

        elif scenario == "remove_round_cleans_up":
            wss = call_prepare_round(mod, repo, config, 9, "main")
            mod.remove_round(repo, config, 9)
            listing = sh(repo, "worktree", "list", "--porcelain").stdout
            leftover_wt = sum(1 for ws in wss if str(ws.path) in listing)
            r.ck("no_worktrees_left", leftover_wt == 0, listing[:200])
            branches = sh(repo, "branch", "--list", "contest/9/*").stdout.strip()
            r.ck("no_branches_left", branches == "", branches)

        elif scenario == "stale_branch_recreated":
            import inspect
            ws1 = mod.reset_worktree(repo, str(rounds_dir), 30, "alice", base_sha)
            p1 = Path(ws1.path)
            branch = ws1.branch
            sh(repo, "worktree", "remove", "--force", str(p1))
            r.ck("branch_survives_removal", sh(repo, "branch", "--list", branch).stdout.strip() != "")
            ws2 = mod.reset_worktree(repo, str(rounds_dir), 30, "alice", base_sha)
            p2 = Path(ws2.path)
            sha2 = sh(p2, "rev-parse", "HEAD").stdout.strip()
            r.ck("recreated_at_base", sha2 == base_sha, sha2)
            r.ck("on_same_branch", ws2.branch == branch, ws2.branch)

        elif scenario == "worktree_of_other_repo":
            other_repo = tmp / "other_main"
            other_repo.mkdir()
            sh(other_repo, "init", "-q", "-b", "main")
            (other_repo / "f.txt").write_text("f\n")
            sh(other_repo, "add", "f.txt")
            sh(other_repo, "commit", "-q", "-m", "init")
            probe = call_prepare_round(mod, repo, config, 40, "main")
            p40 = Path(probe[0].path)
            sh(repo, "worktree", "remove", "--force", str(p40))
            sh(repo, "branch", "-D", probe[0].branch, check=False)
            foreign_path = Path(str(p40).replace("40-", "41-", 1))
            if foreign_path == p40:
                foreign_path = p40.parent / f"41-{agents[0]}"
            sh(other_repo, "worktree", "add", "-q", "-b", "unrelated-branch", str(foreign_path))
            threw = False
            try:
                call_prepare_round(mod, repo, config, 41, "main")
            except Exception as e:
                threw = True
                r.ck("raised", True, type(e).__name__)
            if not threw:
                r.ck("raised", False, "no exception")
            r.ck("left_untouched", sh(foreign_path, "rev-parse", "HEAD", check=False).returncode == 0)

        elif scenario == "repo_checkout_never_touched":
            call_prepare_round(mod, repo, config, 10, "main")
            head_after = sh(repo, "rev-parse", "HEAD").stdout.strip()
            status_after = sh(repo, "status", "--porcelain").stdout
            r.ck("head_unchanged", head_after == head_before, f"{head_before}->{head_after}")
            r.ck("status_unchanged", status_after == status_before)

        else:
            r.ck("unknown_scenario", False, scenario)
    except Exception:
        r.ck("crash", False, traceback.format_exc()[-500:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return r.as_dict()


if __name__ == "__main__":
    out = run(SCEN)
    print("@@RESULT@@" + json.dumps(out))
