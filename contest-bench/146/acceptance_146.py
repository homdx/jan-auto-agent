"""Judge's acceptance suite for round 146 (AR-63), written from the ticket alone.

`arena model set-role / unset-role`, the three profile keys and `arena issue
create --writer/--reviewer/--same-model` through `tools.arena.cli.main`: a
throw-away git repo on branch `arena`, `REPO_ROOT` patched, `contest.local.ini`
with a direct provider whose key is `${B146_KEY}`, `models._direct_for` a fake
list (`model-a`, `model-b`), `models.KILO_LIST` a fake that knows only
`kiloprov/model-k`, `draft.run_collect` a no-op and `draft.llm_call_for` a fake
that records which model and which system prompt each callable was built for.
Nothing dials a model.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/146/acceptance_146.py -n 0 -q
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.arena import cli  # noqa: E402
from tools.arena import models  # noqa: E402
from tools.arena import profile  # noqa: E402
from tools.arena import rounds  # noqa: E402
from tools.contest import draft  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t"}
KEY = "sk-bench146-ROLESECRET"
OLD_KEY = "sk-bench146-OLDSECRET"
LOCAL = "contest.local.ini"

PROVIDER = ("[arena.provider.direct]\napi_key = ${B146_KEY}\nbase_url = http://127.0.0.1:9/v1\n"
            "\n[arena.provider.lit]\napi_key = sk-literal-in-the-file\n"
            "base_url = http://127.0.0.1:8/v1\n")


def old_keys(*, writer: str | None = "old-writer", reviewer: str | None = "old-reviewer",
             review_key: str = "gate_llm_profile", reviewer_url: str = "http://127.0.0.1:2/v1") -> str:
    """The AR-7 keys: `[contest] draft_llm_profile` and a reviewer section."""
    head = "[contest]\n"
    body = ""
    if writer:
        head += "draft_llm_profile = w_llm\n"
        body += f"\n[w_llm]\nbase_url = http://127.0.0.1:1/v1\napi_key = {OLD_KEY}\nmodel = {writer}\n"
    if reviewer:
        head += f"{review_key} = r_llm\n"
        body += f"\n[r_llm]\nbase_url = {reviewer_url}\napi_key = {OLD_KEY}\nmodel = {reviewer}\n"
    return head + body + "\n"


TICKET = """# Bench ticket for the roles

**Status:** open
**Severity:** LOW
**File:** pkg/a.py
**Symbol:** a
**Round:** {nn}
**Size:** S
**Also touches:** -

## Why

Because.

## What to build

A thing.

## Acceptance

```bash
python3 -m pytest tests -q
```

## Rules

- none
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, env=ENV, check=True, capture_output=True,
                          text=True).stdout


def put(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "arena")
    put(r, "contest.ini", "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = p/m-a\n")
    put(r, "pkg/a.py", "def a():\n    return 1\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    put(r, LOCAL, PROVIDER)
    proc = tmp_path / "proc"
    proc.mkdir()
    for mod in (cli, rounds, models):
        if isinstance(getattr(mod, "REPO_ROOT", None), Path):
            monkeypatch.setattr(mod, "REPO_ROOT", r)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(proc))
    monkeypatch.setenv("B146_KEY", KEY)
    for name in list(os.environ):
        if name.startswith(("ARENA_KEY_", "ARENA_URL_")):
            monkeypatch.delenv(name)

    def direct_for(provider, url, key, now):
        assert provider == "direct", provider
        return [{"provider": provider, "model": m, "free": "yes", "ctx": 0, "at": now,
                 "via": "direct"} for m in ("model-a", "model-b")]

    def kilo_list(repo_, providers):
        known = {"kiloprov": {"model-k": {}}}
        return {p: known[p] for p in providers if p in known} if providers else known

    monkeypatch.setattr(models, "_direct_for", direct_for)
    monkeypatch.setattr(models, "KILO_LIST", kilo_list)
    monkeypatch.setattr(draft, "run_collect", lambda root: None)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return r


class Calls:
    """`draft.llm_call_for`, faked: which model, which system prompt, and the calls."""

    def __init__(self):
        self.built: list[tuple[str, bool]] = []    # (model, is the review prompt)
        self.writer_calls: list[str] = []
        self.review_calls: list[tuple[str, str]] = []

    def llm_call_for(self, settings, system=draft.DRAFT_SYSTEM_PROMPT, **kw):
        model = str(getattr(settings, "model", ""))
        review = system == draft.REVIEW_SYSTEM_PROMPT
        self.built.append((model, review))

        def writer(prompt: str) -> str:
            self.writer_calls.append(model)
            m = re.search(r"\*\*Round:\*\*\D{0,5}(\d+)", prompt)
            return TICKET.format(nn=m.group(1) if m else "1")

        def reviewer(prompt: str) -> str:
            self.review_calls.append((model, prompt))
            return '{"ok": true}'

        return reviewer if review else writer

    @property
    def writers(self) -> list[str]:
        return [m for m, r in self.built if not r]

    @property
    def reviewers(self) -> list[str]:
        return [m for m, r in self.built if r]


@pytest.fixture
def calls(monkeypatch) -> Calls:
    c = Calls()
    monkeypatch.setattr(draft, "llm_call_for", c.llm_call_for)
    return c


def run(capsys, monkeypatch, *argv, stdin: str | None = None):
    if stdin is not None:
        import io
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    try:
        rc = cli.main(list(argv))
    except SystemExit as e:
        rc = e.code
    c = capsys.readouterr()
    for secret in (KEY, OLD_KEY, "sk-literal-in-the-file"):
        assert secret not in c.out and secret not in c.err, (c.out, c.err)
    return rc, c.out, c.err


def local(repo: Path) -> bytes:
    return (repo / LOCAL).read_bytes()


def section(repo: Path, name: str = "p1") -> dict[str, str]:
    import configparser
    p = configparser.ConfigParser(interpolation=None)
    p.read_string(local(repo).decode())
    sec = f"arena.profile.{name}"
    return dict(p[sec]) if p.has_section(sec) else {}


def add_profile(repo: Path, body: str, name: str = "p1") -> None:
    with open(repo / LOCAL, "a") as fh:
        fh.write(f"\n[arena.profile.{name}]\n{body}")


def last_line(err: str) -> str:
    lines = [ln for ln in err.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def drafts(repo: Path) -> list[str]:
    d = repo / ".arena" / "drafts"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


# ── set-role ─────────────────────────────────────────────────────────────────

def test_set_role_writes_one_line_and_keeps_every_other_byte(repo, capsys, monkeypatch):
    before = local(repo)
    rc, out, err = run(capsys, monkeypatch, "model", "set-role", "writer", "direct/model-a",
                       "-p", "p1", "-y")
    assert rc == 0, err
    assert section(repo)["writer"] == "direct/model-a"
    after = local(repo).decode()
    assert after.startswith(before.decode())                 # nothing before it moved
    assert "api_key = ${B146_KEY}" in after
    added = after[len(before):]
    assert "[arena.profile.p1]" in added and "writer = direct/model-a" in added


def test_set_role_reviewer_global_flags_before_the_object(repo, capsys, monkeypatch):
    rc, out, err = run(capsys, monkeypatch, "-p", "p1", "-y", "model", "set-role", "reviewer",
                       "direct/model-b")
    assert rc == 0, err
    assert section(repo)["reviewer"] == "direct/model-b"


def test_set_role_shows_before_after(repo, capsys, monkeypatch):
    add_profile(repo, "writer = direct/model-b\n")
    rc, out, err = run(capsys, monkeypatch, "model", "set-role", "writer", "direct/model-a",
                       "-p", "p1", "-y")
    assert rc == 0, err
    assert "profile 'p1' — writer =" in out
    assert re.search(r"before:\s+direct/model-b", out) and re.search(r"after:\s+direct/model-a", out)


@pytest.mark.parametrize("stdin", ["n\n", ""])
def test_set_role_no_or_eof_writes_nothing(repo, capsys, monkeypatch, stdin):
    before = local(repo)
    rc, out, err = run(capsys, monkeypatch, "model", "set-role", "writer", "direct/model-a",
                       "-p", "p1", stdin=stdin)
    assert rc == 2
    assert "not applied" in err and LOCAL in err
    assert "before:" in out and "after:" in out
    assert local(repo) == before


def test_set_role_same_value_is_unchanged(repo, capsys, monkeypatch):
    add_profile(repo, "writer = direct/model-a\n")
    before = local(repo)
    mtime = (repo / LOCAL).stat().st_mtime_ns
    rc, out, err = run(capsys, monkeypatch, "model", "set-role", "writer", "direct/model-a",
                       "-p", "p1")                           # no -y: must not even ask
    assert rc == 0, err
    assert "profile 'p1': writer = direct/model-a (unchanged)" in out
    assert local(repo) == before and (repo / LOCAL).stat().st_mtime_ns == mtime


@pytest.mark.parametrize("argv,needle", [
    (["judge", "direct/model-a"], "writer"),
    (["writer", "model-a"], "name the provider: PROVIDER/MODEL"),
    (["writer", "direct/model-a@high"], "@"),
    (["writer", "direct/model-a,direct/model-b"], ","),
    (["writer", "direct/nosuch"], "'direct/nosuch' is not a model"),
    (["writer", "kiloprov/model-k"], "writer needs a direct provider: 'KILOPROV'"),
    (["reviewer", "kiloprov/model-k"], "reviewer needs a direct provider"),
    (["writer", "lit/model-a"], "[arena.provider.lit] api_key must be a ${ENV} reference"),
])
def test_set_role_refusals_one_line_nothing_written(repo, capsys, monkeypatch, argv, needle):
    before = local(repo)
    rc, out, err = run(capsys, monkeypatch, "model", "set-role", *argv, "-p", "p1", "-y")
    assert rc == 2, (out, err)
    assert needle.lower() in last_line(err).lower(), err    # 'P': the ticket's case is ambiguous
    assert last_line(err).startswith("arena: ")
    assert local(repo) == before


def test_set_role_role_refusal_lists_both(repo, capsys, monkeypatch):
    rc, out, err = run(capsys, monkeypatch, "model", "set-role", "judge", "direct/model-a", "-y")
    assert rc == 2
    assert "writer" in err and "reviewer" in err


def test_set_role_kilo_only_names_the_env_and_section(repo, capsys, monkeypatch):
    rc, out, err = run(capsys, monkeypatch, "model", "set-role", "writer", "kiloprov/model-k", "-y")
    assert rc == 2
    line = last_line(err)
    for part in ("has no base_url/api_key", "api_key = ${ENV}", "ARENA_KEY_KILOPROV",
                 "ARENA_URL_KILOPROV"):
        assert part in line, line
    assert "[arena.provider.kiloprov]" in line.lower(), line


def test_set_role_key_without_url_is_not_direct(repo, capsys, monkeypatch):
    monkeypatch.setenv("ARENA_KEY_HALF", "sk-half-secret")
    before = local(repo)
    rc, out, err = run(capsys, monkeypatch, "model", "set-role", "writer", "half/model-a", "-y")
    assert rc == 2
    assert "needs a direct provider" in last_line(err)
    assert "sk-half-secret" not in out + err
    assert local(repo) == before


def test_set_role_same_model_hint_and_its_silence(repo, capsys, monkeypatch):
    add_profile(repo, "writer = direct/model-a\n")
    rc, out, err = run(capsys, monkeypatch, "model", "set-role", "reviewer", "direct/model-a",
                       "-p", "p1", "-y")
    assert rc == 0, err
    assert section(repo)["reviewer"] == "direct/model-a"
    hint = ("arena: writer and reviewer are the same model — issue create will refuse without "
            "--same-model (or profile set p1 same_model_review=yes)")
    assert err.count(hint) == 1, err


def test_set_role_same_model_hint_case_insensitive(repo, capsys, monkeypatch):
    add_profile(repo, "writer = other/MODEL-A\n")
    rc, out, err = run(capsys, monkeypatch, "model", "set-role", "reviewer", "direct/model-a",
                       "-p", "p1", "-y")
    assert rc == 0, err
    assert "writer and reviewer are the same model" in err


def test_set_role_no_hint_when_same_model_review_on(repo, capsys, monkeypatch):
    add_profile(repo, "writer = direct/model-a\nsame_model_review = yes\n")
    rc, out, err = run(capsys, monkeypatch, "model", "set-role", "reviewer", "direct/model-a",
                       "-p", "p1", "-y")
    assert rc == 0, err
    assert "same model" not in err


def test_set_role_ignores_the_judge_check(repo, capsys, monkeypatch):
    put(repo, LOCAL, old_keys(writer="model-a", reviewer="model-b") + PROVIDER)
    rc, out, err = run(capsys, monkeypatch, "model", "set-role", "writer", "direct/model-b",
                       "-p", "p1", "-y")
    assert rc == 0, err


# ── unset-role ───────────────────────────────────────────────────────────────

def test_unset_role_removes_only_that_line(repo, capsys, monkeypatch):
    add_profile(repo, "writer = direct/model-a\nreviewer = direct/model-b\n")
    before = local(repo).decode()
    rc, out, err = run(capsys, monkeypatch, "model", "unset-role", "writer", "-p", "p1", "-y")
    assert rc == 0, err
    assert local(repo).decode() == before.replace("writer = direct/model-a\n", "")
    assert "before: direct/model-a" in out
    rc, out, err = run(capsys, monkeypatch, "model", "unset-role", "writer", "-p", "p1", "-y")
    assert rc == 2 and "profile 'p1' has no writer" in last_line(err)


def test_unset_role_unknown_profile_and_eof(repo, capsys, monkeypatch):
    rc, out, err = run(capsys, monkeypatch, "model", "unset-role", "writer", "-p", "nope", "-y")
    assert rc == 2 and "profile 'nope' has no writer" in last_line(err), err   # the ticket's one line
    add_profile(repo, "writer = direct/model-a\n")
    before = local(repo)
    rc, out, err = run(capsys, monkeypatch, "model", "unset-role", "writer", "-p", "p1", stdin="")
    assert rc == 2 and "not applied" in err
    assert local(repo) == before


# ── profile keys ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["writer", "reviewer"])
def test_profile_set_refuses_roles_naming_set_role(repo, capsys, monkeypatch, role):
    before = local(repo)
    rc, out, err = run(capsys, monkeypatch, "profile", "set", "p1", f"{role}=direct/model-a", "-y")
    assert rc == 2
    assert f"{role} is set by `arena model set-role`, not `profile set`" in err
    assert local(repo) == before


def test_profile_set_same_model_review(repo, capsys, monkeypatch):
    before = local(repo)
    rc, out, err = run(capsys, monkeypatch, "profile", "set", "p1", "same_model_review=maybe", "-y")
    assert rc == 2 and local(repo) == before
    rc, out, err = run(capsys, monkeypatch, "profile", "set", "p1", "same_model_review=yes", "-y")
    assert rc == 0, err
    assert section(repo)["same_model_review"] == "yes"


def test_load_profiles_refuses_a_bad_same_model_review(repo):
    add_profile(repo, "same_model_review = maybe\n")
    with pytest.raises(profile.ProfileError) as e:
        profile.load_profiles(repo)
    assert "arena.profile.p1" in str(e.value)


def test_profile_flags_ignore_the_three_keys(repo):
    base = {"models": "p/m-a,p/m-b", "max_parallel": "2", "fresh": "yes"}
    more = {**base, "writer": "direct/model-a", "reviewer": "direct/model-b",
            "same_model_review": "yes"}
    assert profile.profile_flags(more) == profile.profile_flags(base)
    for k in ("writer", "reviewer", "same_model_review"):
        assert k in profile.KNOWN_KEYS and profile.KNOWN_KEYS[k] is None


def test_profile_view_shows_the_keys(repo, capsys, monkeypatch):
    add_profile(repo, "models = p/m-a\nwriter = direct/model-a\nreviewer = direct/model-b\n"
                      "same_model_review = no\n")
    rc, out, err = run(capsys, monkeypatch, "profile", "view", "p1")
    assert rc == 0, err
    for word in ("writer", "direct/model-a", "reviewer", "direct/model-b", "same_model_review"):
        assert word in out


# ── issue create: which models ───────────────────────────────────────────────

def create(capsys, monkeypatch, *extra):
    return run(capsys, monkeypatch, "-p", "p1", "issue", "create", *extra, "make a ticket")


def test_profile_pair_is_used_not_the_old_keys(repo, capsys, monkeypatch, calls):
    put(repo, LOCAL, old_keys() + PROVIDER)
    add_profile(repo, "writer = direct/model-a\nreviewer = direct/model-b\n")
    rc, out, err = create(capsys, monkeypatch)
    assert rc == 0, err
    assert calls.writers == ["model-a"] and calls.reviewers == ["model-b"]
    assert calls.writer_calls and all(m == "model-a" for m in calls.writer_calls)
    assert calls.review_calls and all(m == "model-b" for m, _ in calls.review_calls)
    assert "writer direct/model-a · reviewer direct/model-b" in err
    assert re.match(r"ticket \d+ drafted: ", out)            # AR-7's lines unchanged


def test_flags_win_over_the_profile(repo, capsys, monkeypatch, calls):
    add_profile(repo, "writer = direct/model-a\nreviewer = direct/model-b\n")
    rc, out, err = create(capsys, monkeypatch, "--writer", "direct/model-b", "--reviewer",
                          "direct/model-a")
    assert rc == 0, err
    assert calls.writers == ["model-b"] and calls.reviewers == ["model-a"]


def test_profile_writer_with_the_old_gate_reviewer(repo, capsys, monkeypatch, calls):
    put(repo, LOCAL, old_keys(writer=None) + PROVIDER)
    add_profile(repo, "writer = direct/model-a\n")
    rc, out, err = create(capsys, monkeypatch)
    assert rc == 0, err
    assert calls.writers == ["model-a"] and calls.reviewers == ["old-reviewer"]


def test_old_writer_with_the_reviewer_flag(repo, capsys, monkeypatch, calls):
    put(repo, LOCAL, old_keys(reviewer=None) + PROVIDER)
    add_profile(repo, "models = p/m-a\n")                   # `-p p1` must name a profile
    rc, out, err = create(capsys, monkeypatch, "--reviewer", "direct/model-b")
    assert rc == 0, err
    assert calls.writers == ["old-writer"] and calls.reviewers == ["model-b"]


def test_old_keys_alone_still_work(repo, capsys, monkeypatch, calls):
    put(repo, LOCAL, old_keys(review_key="draft_review_llm_profile") + PROVIDER)
    add_profile(repo, "models = p/m-a\n")
    rc, out, err = create(capsys, monkeypatch)
    assert rc == 0, err
    assert calls.writers == ["old-writer"] and calls.reviewers == ["old-reviewer"]


def test_same_pair_refused_before_anything(repo, capsys, monkeypatch, calls):
    add_profile(repo, "writer = direct/model-a\nreviewer = direct/model-a\n")
    rc, out, err = create(capsys, monkeypatch)
    assert rc == 2
    assert "writer and reviewer are the same model (model-a) — pick another reviewer, or pass " \
           "--same-model" in last_line(err)
    assert calls.built == [] and drafts(repo) == []


def test_same_model_behind_another_url_refused(repo, capsys, monkeypatch, calls):
    put(repo, LOCAL, old_keys(writer=None, reviewer="model-a",
                              reviewer_url="http://127.0.0.1:7/other") + PROVIDER)
    add_profile(repo, "writer = direct/model-a\n")
    rc, out, err = create(capsys, monkeypatch)
    assert rc == 2 and "same model" in last_line(err)
    assert calls.writer_calls == [] and calls.review_calls == [] and drafts(repo) == []


def test_same_model_flag_reviews_as_a_separate_call(repo, capsys, monkeypatch, calls):
    add_profile(repo, "writer = direct/model-a\nreviewer = direct/model-a\n")
    rc, out, err = create(capsys, monkeypatch, "--same-model")
    assert rc == 0, err
    assert calls.writers == ["model-a"] and calls.reviewers == ["model-a"]
    assert len(calls.review_calls) >= 1
    assert err.count("review by the writer's own model (model-a) — --same-model") == 1


def test_same_model_writer_only_reviews_itself(repo, capsys, monkeypatch, calls):
    add_profile(repo, "writer = direct/model-a\n")
    rc, out, err = create(capsys, monkeypatch, "--same-model")
    assert rc == 0, err
    assert calls.reviewers == ["model-a"]
    assert "reviewer direct/model-a (same model)" in err or "(same model)" in err


def test_same_model_with_two_models_prints_nothing_more(repo, capsys, monkeypatch, calls):
    add_profile(repo, "writer = direct/model-a\nreviewer = direct/model-b\n")
    rc, out, err = create(capsys, monkeypatch, "--same-model")
    assert rc == 0, err
    assert calls.writers == ["model-a"] and calls.reviewers == ["model-b"]
    assert "own model" not in err


def test_same_model_review_key_acts_like_the_flag(repo, capsys, monkeypatch, calls):
    add_profile(repo, "writer = direct/model-a\nsame_model_review = yes\n")
    rc, out, err = create(capsys, monkeypatch)
    assert rc == 0, err
    assert calls.reviewers == ["model-a"]
    assert err.count("review by the writer's own model (model-a)") == 1


def test_same_model_and_no_review_contradict(repo, capsys, monkeypatch, calls):
    add_profile(repo, "writer = direct/model-a\n")
    rc, out, err = create(capsys, monkeypatch, "--same-model", "--no-review")
    assert rc == 2
    assert "--same-model and --no-review contradict" in last_line(err)
    assert calls.built == [] and drafts(repo) == []


def test_nothing_set_names_set_role(repo, capsys, monkeypatch, calls):
    add_profile(repo, "models = p/m-a\n")
    rc, out, err = create(capsys, monkeypatch)
    assert rc == 2
    assert last_line(err).startswith("arena: no ticket writer — arena model set-role writer")
    assert calls.built == []


def test_no_reviewer_anywhere(repo, capsys, monkeypatch, calls):
    add_profile(repo, "writer = direct/model-a\n")
    rc, out, err = create(capsys, monkeypatch)
    assert rc == 2
    assert "no ticket reviewer" in last_line(err) and "--no-review" in last_line(err)
    assert calls.built == [] and drafts(repo) == []


def test_no_review_needs_no_reviewer(repo, capsys, monkeypatch, calls):
    add_profile(repo, "writer = direct/model-a\n")
    rc, out, err = create(capsys, monkeypatch, "--no-review")
    assert rc == 0, err
    assert calls.reviewers == [] and calls.writers == ["model-a"]
    assert "reviewer skipped (--no-review)" in err


def test_a_stored_role_that_broke(repo, capsys, monkeypatch, calls):
    add_profile(repo, "writer = direct/model-a\nreviewer = direct/model-b\n")
    monkeypatch.delenv("B146_KEY")
    rc, out, err = create(capsys, monkeypatch)
    assert rc == 2
    assert "profile 'p1': writer = direct/model-a" in last_line(err)
    assert calls.built == [] and drafts(repo) == []


@pytest.mark.parametrize("flag,name,needle", [
    ("--writer", "model-a", "name the provider"),
    ("--reviewer", "direct/nosuch", "is not a model"),
    ("--writer", "kiloprov/model-k", "needs a direct provider"),
])
def test_bad_flag_names(repo, capsys, monkeypatch, calls, flag, name, needle):
    add_profile(repo, "writer = direct/model-a\nreviewer = direct/model-b\n")
    rc, out, err = create(capsys, monkeypatch, flag, name)
    assert rc == 2 and needle in last_line(err), err
    assert calls.built == [] and drafts(repo) == []


def test_json_has_the_three_fields(repo, capsys, monkeypatch, calls):
    add_profile(repo, "writer = direct/model-a\nreviewer = direct/model-b\n")
    rc, out, err = run(capsys, monkeypatch, "-p", "p1", "-o", "json", "issue", "create", "brief")
    assert rc == 0, err
    data = json.loads(out)
    assert data["writer"] == "direct/model-a" and data["reviewer"] == "direct/model-b"
    assert data["same_model"] is False and data["reviewed"] is True
    rc, out, err = run(capsys, monkeypatch, "-p", "p1", "-o", "json", "issue", "create",
                       "--no-review", "brief")
    assert rc == 0, err
    data = json.loads(out)
    assert data["reviewer"] is None and data["reviewed"] is False


def test_json_same_model_true(repo, capsys, monkeypatch, calls):
    add_profile(repo, "writer = direct/model-a\n")
    rc, out, err = run(capsys, monkeypatch, "-p", "p1", "-o", "json", "issue", "create",
                       "--same-model", "brief")
    assert rc == 0, err
    data = json.loads(out)
    assert data["same_model"] is True and data["reviewer"] == "direct/model-a"


# ── the seams the ticket names ───────────────────────────────────────────────

def test_role_settings_shape(repo):
    s = models.role_settings(repo, "direct/model-a")
    assert s.model == "model-a" and s.base_url == "http://127.0.0.1:9/v1" and s.api_key == KEY
    from tools.contest import roster
    assert s.temperature == roster.DEFAULTS_DRAFT.temperature


def test_role_settings_refuses_kilo_only(repo):
    with pytest.raises(models.ModelError):
        models.role_settings(repo, "kiloprov/model-k")


def test_draft_callables_untouched():
    from tools.contest import cli as contest_cli
    import inspect
    src = inspect.getsource(contest_cli.draft_callables)
    assert "the review model is the draft model" in src


# ── found while judging ──────────────────────────────────────────────────────

def test_same_model_flag_with_the_old_keys_naming_one_model(repo, capsys, monkeypatch, calls):
    """Step 3 plus step 4's switch: the old keys name one model, `--same-model` allows it."""
    put(repo, LOCAL, old_keys(writer="model-a", reviewer="model-a") + PROVIDER)
    add_profile(repo, "models = p/m-a\n")
    rc, out, err = create(capsys, monkeypatch, "--same-model")
    assert rc == 0, err
    assert calls.writers == ["model-a"] and calls.reviewers == ["model-a"]
    assert err.count("review by the writer's own model (model-a) — --same-model") == 1



def test_no_review_on_a_profile_with_same_model_review(repo, capsys, monkeypatch, calls):
    """The ticket's contradiction is the flag with `--no-review`, not the profile key."""
    add_profile(repo, "writer = direct/model-a\nsame_model_review = yes\n")
    rc, out, err = create(capsys, monkeypatch, "--no-review")
    assert rc == 0, err
    assert calls.writers == ["model-a"] and calls.reviewers == []


def test_old_keys_alone_go_through_draft_callables(repo, capsys, monkeypatch):
    """Step 3 is AR-7's `draft_callables`, unchanged — its seam is what AR-7's tests fake.

    Found by the cross phase: an entry that builds the old pair itself bypasses
    the fake, and AR-7's own tests then dial the test's URL and hang.
    """
    from tools.contest import cli as contest_cli
    put(repo, LOCAL, old_keys() + PROVIDER)
    add_profile(repo, "models = p/m-a\n")
    used = []

    def fake(config, no_review):
        used.append(no_review)
        return (lambda p: TICKET.format(nn=re.search(r"\*\*Round:\*\*\D{0,5}(\d+)", p).group(1)),
                None if no_review else (lambda p: '{"ok": true}'))

    def no_network(settings, system=None, **kw):
        raise AssertionError(f"llm_call_for({settings.model}) on AR-7's path")

    monkeypatch.setattr(contest_cli, "draft_callables", fake)
    monkeypatch.setattr(draft, "llm_call_for", no_network)
    rc, out, err = create(capsys, monkeypatch)
    assert rc == 0, err
    assert used == [False]
