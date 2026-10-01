"""KC-80 (round 127) judge's acceptance suite: a drafted ticket is reviewed by
the gate model, committed, and can start the round.

Written from the ticket alone, through its public contract only:

    python3 -m tools.contest draft --target REPO "brief" [--round NN]
                                   [--no-review] [--run [run options]]

run as a subprocess from a folder holding the roster `contest.ini`, with
`[contest] draft_llm_profile` and `[contest] gate_llm_profile` naming two
profiles whose `base_url`s are two fake OpenAI chat servers in this process —
one drafter, one reviewer — so the suite sees who was asked what without
knowing where an entry keeps its calls.

`--run` must not start a real round. A `sitecustomize.py` on the child's
PYTHONPATH swaps `tools.contest.cli.cmd_run` — the round entry every way of
starting it ends in: `cmd_run(ns)`, `main(["run", ...])` and a
`python -m tools.contest run ...` child alike — for a recorder that writes
its namespace to a log and returns 0.

Must be red on the base (e565f27): `draft` has no review, no commit, no --run.

G* — ok first time: one commit on contest-legs, only the ticket (+ the two
     scripts when missing), the original branch untouched, "ready" printed.
V* — review: what the reviewer saw; not ok → the drafter saw the problems;
     not ok ×3 → exit 2, .rejected.md, no commit; non-JSON → not ok.
D* — dirty REPO refused before any LLM call.
X* — --no-review; no gate_llm_profile.
U* — --run hands the round entry --ticket NN --target REPO and the options.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")

BRIEF = "raise coverage of calc.py — zebra-brief-7731"
RUN_TIMEOUT = 300
OK = json.dumps({"ok": True, "problems": []})


def NOT_OK(tag):
    return json.dumps({"ok": False, "problems": [f"acceptance cannot fail {tag}"]})


# ─── the fake models ─────────────────────────────────────────────────────


class FakeLLM:
    """OpenAI chat completions: JSON or SSE, one canned reply per request."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[str] = []
        self.last = self.replies[-1] if self.replies else ""
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"data": [{"id": "test/model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                text = raw.decode("utf-8", "replace")
                fake.requests.append(text)
                reply = fake.replies.pop(0) if fake.replies else fake.last
                fake.last = reply
                try:
                    stream = bool(json.loads(text).get("stream"))
                except ValueError:
                    stream = False
                if stream:
                    chunks = [
                        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": reply}}]},
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                    ]
                    data = ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
                            + "data: [DONE]\n\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                else:
                    data = json.dumps({
                        "id": "x", "object": "chat.completion", "model": "test/model",
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": reply}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def text(self, i: int) -> str:
        r = self.requests[i]
        try:
            msgs = json.loads(r).get("messages", [])
            return "\n".join(str(m.get("content", "")) for m in msgs) + "\n" + r
        except ValueError:
            return r


# ─── the target repo, the roster, the fake round entry ───────────────────


def _git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, text=True,
                          capture_output=True).stdout.strip()


def make_target(root: Path, *, with_scripts=False) -> Path:
    repo = root / "target"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "epic-tasks").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "calc.py").write_text(
        '"""A tiny calculator."""\n\n\ndef add(a, b):\n    return a + b\n')
    (repo / "tests" / "test_calc.py").write_text(
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n")
    (repo / "epic-tasks" / "INDEX.md").write_text("# Tasks\n")
    if with_scripts:
        (repo / "scripts").mkdir()
        for name in ("next_task.py", "append_task.py"):
            (repo / "scripts" / name).write_text((REPO_ROOT / "scripts" / name).read_text())
    (repo / ".gitignore").write_text(".collect/\n__pycache__/\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def make_roster(root: Path, draft_url: str, gate_url: str | None, extra=()) -> Path:
    cfg = root / "cfg"
    cfg.mkdir()
    lines = ["[contest]", "kilo_bin = kilo", "out_dir = contest-out",
             f"rounds_dir = {root / 'rounds'}", "draft_llm_profile = drafter"]
    if gate_url is not None:
        lines.append("gate_llm_profile = reviewer")
    lines += list(extra)
    lines += ["", "[drafter]", f"base_url = {draft_url}",
              "api_key = test-key-not-real", "model = test/drafter", "api_format = openai"]
    lines += ["", "[reviewer]", f"base_url = {gate_url or 'http://127.0.0.1:9/v1'}",
              "api_key = test-key-not-real", "model = test/reviewer", "api_format = openai",
              "response_format = false", "temperature = 0.0", "max_tokens = 512"]
    lines += ["", "[contest.agent.a]", "model = kenary/a:free", ""]
    (cfg / "contest.ini").write_text("\n".join(lines))
    return cfg


SITECUSTOMIZE = r'''
import importlib.abc, importlib.util, json, os, sys

_LOG = os.environ.get("KC80_RUN_LOG")


def _fake_cmd_run(args, *a, **k):
    ns = {}
    for key, val in vars(args).items():
        if key == "func":
            continue
        ns[key] = val if isinstance(val, (int, float, str, bool, type(None))) else str(val)
    with open(_LOG, "a") as fh:
        fh.write(json.dumps({"ns": ns, "argv": sys.argv}) + "\n")
    return 0


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "tools.contest.cli":
            return None
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            sys.meta_path.insert(0, self)
        if spec is None or spec.loader is None:
            return spec
        orig = spec.loader.exec_module

        def exec_module(module):
            orig(module)
            module.cmd_run = _fake_cmd_run
        spec.loader.exec_module = exec_module
        return spec


if _LOG:
    sys.meta_path.insert(0, _Finder())
'''


def draft(root: Path, cfg: Path, repo: Path, *extra, brief=BRIEF):
    site = root / "site"
    site.mkdir(exist_ok=True)
    (site / "sitecustomize.py").write_text(SITECUSTOMIZE)
    env = dict(os.environ, PYTHONPATH=f"{site}{os.pathsep}{REPO_ROOT}",
               KC80_RUN_LOG=str(root / "run.log"))
    env.pop("CONTEST_GATE_API_KEY", None)
    env.pop("CONTEST_DRAFT_API_KEY", None)
    return subprocess.run(
        [sys.executable, "-m", "tools.contest", "draft", "--target", str(repo), brief, *extra],
        cwd=cfg, env=env, text=True, capture_output=True, timeout=RUN_TIMEOUT)


def runs(root: Path) -> list[dict]:
    log = root / "run.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def ticket(*, round_no=7, marker="marker-good-ticket"):
    cmd = "python3 -m pytest tests/test_calc_more.py -q"
    return (f"# CALC-{round_no} — Cover add in calc\n\n"
            f"**Status:** open\n**Severity:** LOW\n**File:** `pkg/calc.py`\n"
            f"**Symbol:** `add`\n**Round:** {round_no}\n**Size:** S\n"
            f"**Also touches:** `tests/test_calc_more.py`\n\n---\n\n"
            f"## Why\n\nadd has one test. {marker}\n\n"
            "## What to build\n\nMore tests for add.\n\n"
            f"## Acceptance\n\n    {cmd}\n\n```\n{cmd}\n```\n\n"
            "## Rules\n\n- No network.\n")


GOOD = ticket()


@pytest.fixture
def env(tmp_path):
    fakes = []

    def make(drafts, reviews, *, gate=True, with_scripts=False, extra=()):
        drafter, reviewer = FakeLLM(drafts), FakeLLM(reviews)
        fakes.extend([drafter, reviewer])
        repo = make_target(tmp_path, with_scripts=with_scripts)
        cfg = make_roster(tmp_path, drafter.url, reviewer.url if gate else None, extra)
        return drafter, reviewer, repo, cfg

    yield make
    for f in fakes:
        f.close()


def _out(r):
    return r.stdout + r.stderr


def legs_exists(repo) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "-q", "contest-legs^{commit}"],
                          cwd=repo, capture_output=True).returncode == 0


def legs_files(repo) -> list[str]:
    """Files the commits on contest-legs above main added or changed."""
    out = _git(repo, "diff", "--name-only", "main", "contest-legs")
    return sorted(line for line in out.splitlines() if line)


def rejected(root: Path) -> list[Path]:
    return list(root.rglob("*.rejected.md"))


# ─── G: ok the first time ────────────────────────────────────────────────


def test_G1_ok_first_time_commits_ticket_and_scripts_on_contest_legs(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], [OK])
    main_before = _git(repo, "rev-parse", "main")
    r = draft(tmp_path, cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)
    assert legs_exists(repo), _out(r)
    files = legs_files(repo)
    tickets = [f for f in files if f.startswith("epic-tasks/07-") and f.endswith(".md")]
    assert len(tickets) == 1, files
    assert set(files) - set(tickets) <= {"scripts/next_task.py", "scripts/append_task.py",
                                         "epic-tasks/INDEX.md"}, files
    assert {"scripts/next_task.py", "scripts/append_task.py"} <= set(files), \
        "the two scripts were missing and are copied in"
    assert "marker-good-ticket" in _git(repo, "show", f"contest-legs:{tickets[0]}")
    assert _git(repo, "rev-parse", "main") == main_before, "the original branch is untouched"
    assert len(reviewer.requests) == 1
    assert len(drafter.requests) == 1


def test_G2_scripts_present_commit_holds_only_the_ticket(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], [OK], with_scripts=True)
    r = draft(tmp_path, cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)
    files = [f for f in legs_files(repo) if f != "epic-tasks/INDEX.md"]
    assert len(files) == 1 and files[0].startswith("epic-tasks/07-"), files


def test_G3_explicit_paths_an_untracked_file_is_not_committed(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], [OK], with_scripts=True)
    (repo / "notes-untracked.txt").write_text("mine, not the ticket's\n")
    r = draft(tmp_path, cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)
    assert "notes-untracked.txt" not in legs_files(repo)
    assert (repo / "notes-untracked.txt").exists()


def test_G4_ready_message_names_the_ticket_and_the_run_command(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], [OK])
    r = draft(tmp_path, cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)
    out = _out(r)
    assert "ready" in out.lower(), out
    assert "07-" in out, out
    assert "--ticket 7" in out or "--ticket 07" in out, out
    assert "--target" in out, out
    assert runs(tmp_path) == [], "without --run the round is not started"


def test_G5_one_commit_on_contest_legs(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], [OK])
    r = draft(tmp_path, cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)
    assert _git(repo, "rev-list", "--count", "main..contest-legs") == "1"


# ─── V: the review ───────────────────────────────────────────────────────


def test_V1_reviewer_sees_the_ticket_and_the_brief(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], [OK])
    r = draft(tmp_path, cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)
    seen = reviewer.text(0)
    assert "marker-good-ticket" in seen
    assert "zebra-brief-7731" in seen


def test_V2_not_ok_then_ok_drafter_sees_the_problems(env, tmp_path):
    drafter, reviewer, repo, cfg = env([ticket(marker="first-draft"), ticket(marker="second-draft")],
                                       [NOT_OK("problem-owl-4417"), OK])
    r = draft(tmp_path, cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)
    assert len(drafter.requests) == 2, _out(r)
    assert "problem-owl-4417" in drafter.text(1), "the redraft is told what the reviewer said"
    assert legs_exists(repo)
    t = [f for f in legs_files(repo) if f.startswith("epic-tasks/07-")]
    assert "second-draft" in _git(repo, "show", f"contest-legs:{t[0]}")


def test_V3_not_ok_three_times_exit_2_rejected_no_commit(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], [NOT_OK("problem-cat-2290")])
    main_before = _git(repo, "rev-parse", "main")
    r = draft(tmp_path, cfg, repo, "--round", "7")
    assert r.returncode == 2, _out(r)
    assert "problem-cat-2290" in _out(r), "the problems are printed"
    assert rejected(tmp_path), "a .rejected.md is saved"
    assert not legs_exists(repo) or legs_files(repo) == [], "nothing committed"
    assert _git(repo, "rev-parse", "main") == main_before


def test_V4_default_is_two_review_rounds(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], [NOT_OK("problem-cat-2290")])
    r = draft(tmp_path, cfg, repo, "--round", "7")
    assert r.returncode == 2, _out(r)
    assert len(reviewer.requests) == 3, "first review + at most 2 review rounds"
    assert len(drafter.requests) == 3


def test_V5_draft_review_rounds_config(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], [NOT_OK("problem-cat-2290")],
                                       extra=["draft_review_rounds = 1"])
    r = draft(tmp_path, cfg, repo, "--round", "7")
    assert r.returncode == 2, _out(r)
    assert len(reviewer.requests) == 2


def test_V6_non_json_review_is_not_ok_with_the_raw_text(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], ["looks fine to me raw-heron-5521"])
    r = draft(tmp_path, cfg, repo, "--round", "7")
    assert r.returncode == 2, _out(r)
    where = _out(r) + "".join(p.read_text() for p in rejected(tmp_path))
    assert "raw-heron-5521" in where
    assert not legs_exists(repo) or legs_files(repo) == []


def test_V7_non_json_first_then_ok_commits(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD, GOOD], ["no json here raw-heron-5521", OK])
    r = draft(tmp_path, cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)
    assert "raw-heron-5521" in drafter.text(1)
    assert legs_exists(repo)


# ─── D: a dirty REPO ─────────────────────────────────────────────────────


def test_D1_dirty_repo_refused_before_any_llm_call(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], [OK])
    (repo / "pkg" / "calc.py").write_text("# edited, not committed\n")
    r = draft(tmp_path, cfg, repo, "--round", "7")
    assert r.returncode != 0, _out(r)
    assert drafter.requests == [] and reviewer.requests == [], "refused before any call"
    assert not legs_exists(repo)
    assert (repo / "pkg" / "calc.py").read_text() == "# edited, not committed\n"


# ─── X: --no-review and no gate profile ──────────────────────────────────


def test_X1_no_review_skips_the_reviewer_and_says_so(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], [NOT_OK("never-asked")])
    r = draft(tmp_path, cfg, repo, "--round", "7", "--no-review")
    assert r.returncode == 0, _out(r)
    assert reviewer.requests == []
    assert "review" in _out(r).lower() and ("skip" in _out(r).lower()
                                            or "no-review" in _out(r).lower()), _out(r)
    assert legs_exists(repo)


def test_X2_no_gate_profile_names_the_key(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], [OK], gate=False)
    r = draft(tmp_path, cfg, repo, "--round", "7")
    assert r.returncode != 0, _out(r)
    assert "gate_llm_profile" in _out(r)
    assert not legs_exists(repo)


def test_X3_no_gate_profile_with_no_review_works(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], [OK], gate=False)
    r = draft(tmp_path, cfg, repo, "--round", "7", "--no-review")
    assert r.returncode == 0, _out(r)
    assert legs_exists(repo)


# ─── U: --run ────────────────────────────────────────────────────────────


def _same_path(a, b) -> bool:
    return Path(str(a)).resolve() == Path(str(b)).resolve()


def test_U1_run_starts_the_round_on_the_ticket_and_target(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], [OK])
    r = draft(tmp_path, cfg, repo, "--round", "7", "--run")
    assert r.returncode == 0, _out(r)
    got = runs(tmp_path)
    assert len(got) == 1, (got, _out(r))
    ns = got[0]["ns"]
    assert int(ns["ticket"]) == 7, ns
    assert _same_path(ns["target"], repo), ns
    assert legs_exists(repo), "committed before the round starts"


def test_U2_run_passes_legs_and_models_through(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], [OK])
    r = draft(tmp_path, cfg, repo, "--round", "7", "--run",
              "--legs", "2", "--models", "kenary/x:free,kenary/y:free")
    assert r.returncode == 0, _out(r)
    got = runs(tmp_path)
    assert len(got) == 1, (got, _out(r))
    ns = got[0]["ns"]
    assert str(ns.get("legs")) == "2", ns
    assert "kenary/x:free" in str(ns.get("models")) and "kenary/y:free" in str(ns.get("models")), ns


def test_U3_run_not_started_when_the_review_rejects(env, tmp_path):
    drafter, reviewer, repo, cfg = env([GOOD], [NOT_OK("problem-cat-2290")])
    r = draft(tmp_path, cfg, repo, "--round", "7", "--run")
    assert r.returncode == 2, _out(r)
    assert "problem-cat-2290" in _out(r), "rejected by the review, not by argparse"
    assert runs(tmp_path) == []
