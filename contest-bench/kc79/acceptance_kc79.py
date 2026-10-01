"""KC-79 (round 126) judge's acceptance suite: a plain brief becomes a ticket
grounded in the collect maps.

Written from the ticket alone, through its public contract only:

    python3 -m tools.contest draft --target REPO "brief" [--round NN] [--out FILE]

run as a subprocess from a folder holding the roster `contest.ini` (the
default roster, as for `run`), with `[contest] draft_llm_profile` naming a
profile whose `base_url` is a fake OpenAI chat server in this process. The
fake answers from a queue of canned drafts and records every request, so the
suite sees what reached the model (the brief, the maps, the lint list on the
second try) without knowing where an entry keeps its LLM call. Plus
`lint_ticket(text, artifact)` as the ticket spells it.

Must be red on the base (35c1188): there is no `draft` subcommand.

G* — a good draft lands at epic-tasks/NN-*.md, one call, the path printed.
P* — the prompt: brief verbatim, the maps, the format; a huge map is cut.
L* — lint: missing file / unknown symbol / no Acceptance / taken NN / Status.
R* — retry: bad then good → written, the fake saw the problems; bad twice → exit 2.
C* — config: no draft_llm_profile → error naming the key, no call.
N* — numbering and --out; no commit, no branch change.
"""
from __future__ import annotations

import json
import os
import re
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


# ─── the fake model ──────────────────────────────────────────────────────


class FakeLLM:
    """OpenAI chat completions: JSON or SSE, one canned reply per request."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def do_GET(self):  # a /models probe, if any
                body = json.dumps({"data": [{"id": "test/drafter"}]}).encode()
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
                    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                    data = body.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                else:
                    data = json.dumps({
                        "id": "x", "object": "chat.completion", "model": "test/drafter",
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": reply}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.last = self.replies[-1] if self.replies else ""
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def everything(self) -> str:
        # json-escaped bodies: decode each so newlines/quotes compare plainly
        out = []
        for r in self.requests:
            try:
                out.append(json.dumps(json.loads(r), ensure_ascii=False))
                out.append("\n".join(
                    str(m.get("content", "")) for m in json.loads(r).get("messages", [])))
            except ValueError:
                out.append(r)
        return "\n".join(out)


# ─── the target repo and the roster ──────────────────────────────────────


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()


def make_target(root: Path, *, tickets=(), n_tests: int = 2, n_mods: int = 0) -> Path:
    repo = root / "target"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "epic-tasks").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "calc.py").write_text(
        '"""A tiny calculator."""\n\n\n'
        "def add(a, b):\n    return a + b\n\n\n"
        "class Calc:\n    def total(self, xs):\n        return sum(xs)\n")
    body = ["from pkg.calc import add\n"]
    for i in range(n_tests):
        body.append(f"\n\ndef test_add_{i}():\n    assert add({i}, 1) == {i + 1}\n")
    (repo / "tests" / "test_calc.py").write_text("".join(body))
    # TEST_MAP grows by module × covering test file, not by test count (P4)
    for i in range(n_mods):
        (repo / "pkg" / f"module_with_a_long_name_{i:05d}.py").write_text(
            f"def fn_{i}():\n    return {i}\n")
        (repo / "tests" / f"test_module_with_a_long_name_{i:05d}.py").write_text(
            f"from pkg.module_with_a_long_name_{i:05d} import fn_{i}\n\n\n"
            f"def test_fn():\n    assert fn_{i}() == {i}\n")
    for name in tickets:
        (repo / "epic-tasks" / name).write_text(f"# {name}\n\n**Status:** open\n")
    (repo / ".gitignore").write_text(".collect/\n__pycache__/\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def make_roster(root: Path, url: str | None) -> Path:
    cfg = root / "cfg"
    cfg.mkdir()
    lines = ["[contest]", "kilo_bin = kilo", "out_dir = contest-out",
             f"rounds_dir = {root / 'rounds'}"]
    if url is not None:
        lines.append("draft_llm_profile = drafter")
    lines += ["", "[drafter]", f"base_url = {url or 'http://127.0.0.1:9/v1'}",
              "api_key = test-key-not-real", "model = test/drafter", "api_format = openai",
              "", "[contest.agent.a]", "model = kenary/a:free", ""]
    (cfg / "contest.ini").write_text("\n".join(lines))
    return cfg


def draft(cfg: Path, repo: Path, *extra, brief=BRIEF):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    env.pop("CONTEST_GATE_API_KEY", None)
    # KC79_ROSTER_FLAG=1: a second, diagnostic pass that names the roster
    # explicitly — the scored pass relies on the default (`contest.ini` in
    # the CWD, as `run` does); this one shows what else works in an entry
    # that misses the default.
    if os.environ.get("KC79_ROSTER_FLAG"):
        extra = ("--roster", str(cfg / "contest.ini"), *extra)
    return subprocess.run(
        [sys.executable, "-m", "tools.contest", "draft", "--target", str(repo), brief, *extra],
        cwd=cfg, env=env, text=True, capture_output=True, timeout=RUN_TIMEOUT)


def ticket(*, round_no=7, status="open", file="pkg/calc.py", symbol="add",
           also="tests/test_calc_more.py", acceptance=True, title="Cover add in calc",
           block="both"):
    # Markdown has two code blocks: the 4-space indent (the KC tickets' house
    # style, this ticket's own Acceptance) and the fence. The drafts carry
    # both, so one reading missed does not fail every test; G1 and G6 score
    # each kind alone.
    cmd = "python3 -m pytest tests/test_calc_more.py -q"
    indented, fenced = f"    {cmd}\n", f"```\n{cmd}\n```\n"
    block = {"both": indented + "\n" + fenced, "indented": indented,
             "fenced": fenced}[block]
    acc = f"## Acceptance\n\n{block}\n" if acceptance else ""
    return (f"# CALC-{round_no} — {title}\n\n"
            f"**Status:** {status}\n**Severity:** LOW\n**File:** `{file}`\n"
            f"**Symbol:** `{symbol}`\n**Round:** {round_no}\n**Size:** S\n"
            f"**Also touches:** `{also}`\n\n---\n\n"
            "## Why\n\nadd has one test. marker-good-ticket\n\n"
            "## What to build\n\nMore tests for add.\n\n"
            f"{acc}"
            "## Rules\n\n- No network.\n")


GOOD = ticket()


@pytest.fixture
def env(tmp_path):
    fakes = []

    def make(replies, *, tickets=(), n_tests=2, n_mods=0, profile=True):
        fake = FakeLLM(replies)
        fakes.append(fake)
        repo = make_target(tmp_path, tickets=tickets, n_tests=n_tests, n_mods=n_mods)
        cfg = make_roster(tmp_path, fake.url if profile else None)
        return fake, repo, cfg

    yield make
    for f in fakes:
        f.close()


def written(repo: Path) -> list[Path]:
    # `.rejected.md` is saved "next to the output" — for the default output
    # that is epic-tasks/ itself, so it is not a written ticket.
    return sorted(p for p in (repo / "epic-tasks").glob("*.md")
                  if not p.name.endswith(".rejected.md"))


def rejected(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.rejected.md")]


def _out(r):
    return r.stdout + r.stderr


# ─── G: the good path ────────────────────────────────────────────────────


def test_G1_good_draft_lands_at_epic_tasks_nn(env):
    """The ticket's own Acceptance style: an indented command only."""
    fake, repo, cfg = env([ticket(block="indented")])
    r = draft(cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)
    files = [p for p in written(repo) if p.name.startswith("07-")]
    assert len(files) == 1, (written(repo), _out(r))
    assert "marker-good-ticket" in files[0].read_text()


def test_G2_path_is_printed(env):
    fake, repo, cfg = env([GOOD])
    r = draft(cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)
    f = [p for p in written(repo) if p.name.startswith("07-")][0]
    assert f.name in r.stdout, r.stdout


def test_G3_one_llm_call_for_a_clean_draft(env):
    fake, repo, cfg = env([GOOD])
    r = draft(cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)
    assert len(fake.requests) == 1


def test_G4_slug_from_title(env):
    fake, repo, cfg = env([ticket(title="Cover add in calc")])
    r = draft(cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)
    f = [p for p in written(repo) if p.name.startswith("07-")][0]
    assert re.search(r"cover.*add.*calc", f.name.lower()), f.name


def test_G6_fenced_acceptance_accepted(env):
    fake, repo, cfg = env([ticket(block="fenced")])
    r = draft(cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)
    f = [p for p in written(repo) if p.name.startswith("07-")]
    assert f and "python3 -m pytest tests/test_calc_more.py" in f[0].read_text()


def test_G5_collect_ran_on_the_target(env):
    fake, repo, cfg = env([GOOD])
    r = draft(cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)
    assert (repo / ".collect" / "artifact.json").is_file()
    assert (repo / ".collect" / "MODULE_MAP.md").is_file()


# ─── P: the prompt ───────────────────────────────────────────────────────


def test_P1_brief_verbatim_in_prompt(env):
    fake, repo, cfg = env([GOOD])
    draft(cfg, repo, "--round", "7")
    assert fake.requests and BRIEF in fake.everything


def test_P2_maps_in_prompt(env):
    fake, repo, cfg = env([GOOD])
    draft(cfg, repo, "--round", "7")
    text = fake.everything
    assert "pkg/calc.py" in text
    assert "test_calc" in text   # TEST_MAP


def test_P3_format_fields_in_prompt(env):
    fake, repo, cfg = env([GOOD])
    draft(cfg, repo, "--round", "7")
    text = fake.everything
    for word in ("Status", "Severity", "Symbol", "Size", "Also touches",
                 "## Acceptance", "## Rules", "## Why"):
        assert word in text, word


def test_P4_huge_test_map_is_cut(env):
    fake, repo, cfg = env([ticket()], n_mods=3000)
    r = draft(cfg, repo, "--round", "7")
    tm = repo / ".collect" / "TEST_MAP.md"
    assert tm.is_file(), _out(r)
    size = len(tm.read_text())
    assert size > 150_000, size   # the input really is huge
    assert fake.requests, _out(r)
    assert max(len(q) for q in fake.requests) < size, "TEST_MAP reached the prompt whole"


# ─── L / R: lint and the retry ───────────────────────────────────────────


def _rejected_case(env, bad, *, tickets=()):
    fake, repo, cfg = env([bad, bad], tickets=tickets)
    r = draft(cfg, repo, "--round", "7")
    return fake, repo, cfg, r


def _assert_rejected(fake, repo, cfg, r, before=()):
    assert r.returncode == 2, _out(r)
    assert [p.name for p in written(repo)] == sorted(before), written(repo)
    assert len(fake.requests) == 2, len(fake.requests)
    assert rejected(repo.parent), "no .rejected.md saved"


def test_L1_missing_file_rejected(env):
    """`File` in a directory that does not exist — a new file under an
    existing one is allowed (L7), so the missing path needs a missing dir."""
    _assert_rejected(*_rejected_case(env, ticket(file="nosuchpkg/nosuch.py")))


def test_L2_new_file_in_missing_dir_rejected(env):
    _assert_rejected(*_rejected_case(env, ticket(also="nodir/deeper/x.py")))


def test_L3_unknown_symbol_rejected(env):
    _assert_rejected(*_rejected_case(env, ticket(symbol="multiply_everything")))


def test_L4_no_acceptance_rejected(env):
    _assert_rejected(*_rejected_case(env, ticket(acceptance=False)))


def test_L5_taken_nn_rejected(env):
    _assert_rejected(*_rejected_case(env, GOOD, tickets=("07-old.md",)), before=("07-old.md",))


def test_L6_status_not_open_rejected(env):
    _assert_rejected(*_rejected_case(env, ticket(status="done")))


def test_L7_new_test_file_in_existing_dir_is_fine(env):
    fake, repo, cfg = env([ticket(also="tests/test_calc_brand_new.py")])
    r = draft(cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)


def test_R1_bad_then_good_written_and_lint_list_sent_back(env):
    fake, repo, cfg = env([ticket(file="nosuchpkg/nosuch.py"), GOOD])
    r = draft(cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)
    assert len(fake.requests) == 2
    assert [p for p in written(repo) if p.name.startswith("07-")]
    second = fake.requests[1]
    assert "nosuch.py" in json.loads(second).__str__() or "nosuch.py" in second
    assert not rejected(repo.parent)


def test_R2_rejected_problems_printed(env):
    fake, repo, cfg, r = _rejected_case(env, ticket(file="nosuchpkg/nosuch.py"))
    assert r.returncode == 2
    assert "nosuch.py" in _out(r)


def test_R3_rejected_file_holds_last_draft(env):
    fake, repo, cfg = env([ticket(symbol="first_bad_sym"), ticket(symbol="second_bad_sym")])
    r = draft(cfg, repo, "--round", "7")
    assert r.returncode == 2, _out(r)
    rej = rejected(repo.parent)
    assert rej and "second_bad_sym" in rej[0].read_text()


# ─── C: config ───────────────────────────────────────────────────────────


def test_C1_no_profile_names_the_key_and_no_call(env):
    fake, repo, cfg = env([GOOD], profile=False)
    r = draft(cfg, repo, "--round", "7")
    assert r.returncode != 0
    assert "draft_llm_profile" in _out(r)
    assert fake.requests == []
    assert not written(repo)


def test_C2_no_url_in_draft_code():
    src = (REPO_ROOT / "tools" / "contest" / "draft.py").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"""["']https?://""", code)


# ─── N: numbering, --out, git untouched ──────────────────────────────────


def test_N1_next_free_nn_without_round(env):
    # 01..03 taken: "next free" read as the first gap or as max+1 is 04 both ways
    fake, repo, cfg = env([ticket(round_no=4)], tickets=("01-a.md", "02-b.md", "03-old.md"))
    r = draft(cfg, repo)
    assert r.returncode == 0, _out(r)
    assert [p for p in written(repo) if p.name.startswith("04-")], written(repo)


def test_N2_out_file(env, tmp_path):
    fake, repo, cfg = env([GOOD])
    out = tmp_path / "elsewhere" / "t.md"
    out.parent.mkdir()
    r = draft(cfg, repo, "--round", "7", "--out", str(out))
    assert r.returncode == 0, _out(r)
    assert out.is_file() and "marker-good-ticket" in out.read_text()


def test_N3_no_commit_no_branch_change(env):
    fake, repo, cfg = env([GOOD])
    head, branch = _git(repo, "rev-parse", "HEAD"), _git(repo, "branch", "--show-current")
    r = draft(cfg, repo, "--round", "7")
    assert r.returncode == 0, _out(r)
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "branch", "--show-current") == branch


# ─── lint_ticket as a function ───────────────────────────────────────────


def _artifact(env):
    fake, repo, cfg = env([GOOD])
    draft(cfg, repo, "--round", "7")
    p = repo / ".collect" / "artifact.json"
    return repo, json.loads(p.read_text()) if p.is_file() else {}


def test_F1_lint_ticket_two_args_flags_no_acceptance(env, monkeypatch):
    from tools.contest.draft import lint_ticket
    repo, art = _artifact(env)
    monkeypatch.chdir(repo)
    probs = lint_ticket(ticket(round_no=8, acceptance=False), art)
    assert isinstance(probs, list) and probs
    assert all(isinstance(p, str) for p in probs)
