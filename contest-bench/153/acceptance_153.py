"""Judge's acceptance suite for round 153, written from the ticket alone.

A model whose window the spawn overlay (`KILO_CONFIG_CONTENT`) already sets is
not re-sized by `PATCH /config`: Kilo answers 200, and `GET /provider` still
shows the old limit. The runner must check that the patch took — or keep the
`context_watch` armed. Checked by behaviour only, through whole runs on
`tests/test_contest_runner._run_one`; no private helper of any entry is named.

The fake here is the *base* `tests/_kilo_fake.py` plus an overlay this file
installs by itself (so an entry's own fake edits never matter): `PATCH /config`
keeps a model's limit and `GET /provider` reports it, except for models the
test lists as pinned by the "env" — those keep their limit and the patch is
answered 200 and ignored, as live 7.6.2 does.

Copy into an entry's checkout (with the base `tests/_kilo_fake.py` and
`tests/test_contest_overflow_wording.py` restored) and run from there:
    python3 -m pytest contest-bench/153/acceptance_153.py -n 0 -q
"""

from __future__ import annotations

import copy
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_contest_context_memory as ctm  # noqa: E402
import test_contest_overflow_wording as tow  # noqa: E402
import test_contest_runner as tr  # noqa: E402
import _kilo_fake as kf  # noqa: E402

DECLARED = tow.DECLARED      # 131 072: what Kilo says the model holds
REFUSED_AT = tow.REFUSED_AT  # 98 777: what the memory says it really holds
MODEL = "agent-a:free"


# ── the overlay: a fake that models the spawn overlay's priority ─────────────

def _limits(fake) -> dict:
    """model id -> the limit `GET /provider` reports; starts at Kilo's window."""
    return fake.__dict__.setdefault("_limits", {})


def _install(monkeypatch, pinned=(), keep_input=False):
    """Teach the bench handler `PATCH /config` -> `GET /provider` read-back."""
    handler = tr._BenchHandler
    real_get = handler.do_GET

    def do_PATCH(self):
        body = self._body()
        path, _query = self._record(body)
        if path != "/config":
            return self._not_found(path)
        # live 7.6.2 writes the patch to the directory's project file, pinned or not
        _dir = _query.get("directory") or self.fake.directory
        if _dir:
            _file = Path(_dir) / ".kilo" / "kilo.jsonc"
            _file.parent.mkdir(parents=True, exist_ok=True)
            _file.write_text(__import__("json").dumps(body), encoding="utf-8")
        self.fake._emit({"type": "server.instance.disposed", "properties": {}})
        self.fake._config_reloads += 1
        with self.fake._bus._lock:
            subs = list(self.fake._bus._subs)
        for q in subs:
            q.put(None)
        for pid, prov in (body.get("provider") or {}).items():
            for mid, model in (prov.get("models") or {}).items():
                if mid in pinned:
                    continue  # the env content outranks the patch: 200, nothing changes
                if "limit" in model:
                    new = dict(model["limit"])
                    if keep_input:  # the env pins `input` only: context moves, input stays
                        old = _limits(self.fake).get(mid, {"context": DECLARED, "output": 20_000})
                        new["input"] = old.get("input", DECLARED)
                    _limits(self.fake)[mid] = new
        return self._json(200, body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        status = int(self.fake.scenario.get("providers_status", 200))
        if path == "/provider" and status == 200:
            self._record(None)
            offer = copy.deepcopy(self.fake.scenario.get("providers", kf.DEFAULT_OFFER))
            for prov in offer["all"]:
                for mid, model in prov["models"].items():
                    model["limit"] = _limits(self.fake).get(
                        mid, {"context": DECLARED, "output": 20_000})
            return self._json(200, offer)
        return real_get(self)

    monkeypatch.setattr(handler, "do_PATCH", do_PATCH)
    monkeypatch.setattr(handler, "do_GET", do_GET)


def _watch_scenario():
    """A turn that fills 90 000 tokens and never ends by itself (91.1 % of the
    remembered 98 777), then a continue that finishes the work. Only the
    watch — not the provider — can stop the first turn."""
    return {"abort_idles": True, "summary_tokens": 3_000, "turns": [
        {"events": ["busy"], "idle": False,
         "message_info": {"tokens": {"input": 90_000, "output": 0}}},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}


def _run(tmp_path, scenario, **over):
    memory = tow._remembered(tmp_path)
    config = tow._declared(ctm._config(tmp_path, memory=memory, context_watch_sec=0.2, **over))
    return tr._run_one(tmp_path, scenario, config)


def _watch_stopped(caplog):
    """The watch itself stopped the turn (the harness's silence clock also ends a
    turn that never idles, so `aborted` alone cannot tell the two apart)."""
    return any("inside the turn" in r.getMessage() and "91.1%" in r.getMessage()
               for r in caplog.records)


def _warned(caplog, *needles):
    return [r for r in caplog.records if r.levelno >= logging.WARNING
            and all(n in r.getMessage().replace(",", "").replace(" ", "") for n in needles)]


# ── 1. the pinned model: the patch is ignored, the watch must stop the turn ──

def test_a_pinned_window_keeps_the_watch_armed(tmp_path, monkeypatch, caplog):
    """The ticket's test 1: the env keeps Kilo at 131 072 whatever is patched.
    One PATCH at most, the watch stops the turn at the smaller size, and the
    run says that Kilo kept the larger window."""
    caplog.set_level(logging.INFO)
    _install(monkeypatch, pinned={MODEL})
    sb, fake, _h, run, aborted = _run(tmp_path, _watch_scenario(),
                                      max_continues_per_attempt=3)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert aborted, "the watch — the provider would not — stopped the turn"
    assert len(fake.calls("PATCH")) <= 1, "a refused size is not re-patched at every prompt"
    assert any("inside the turn" in r.getMessage() and "91.1%" in r.getMessage()
               for r in caplog.records)


def test_a_pinned_window_is_reported_once(tmp_path, monkeypatch, caplog):
    """The ticket asks for one warning line naming the window Kilo kept."""
    caplog.set_level(logging.INFO)
    _install(monkeypatch, pinned={MODEL})
    _run(tmp_path, _watch_scenario(), max_continues_per_attempt=3)
    kept = _warned(caplog, "131072")
    assert len(kept) == 1, [r.getMessage() for r in kept]


# ── 2. the model the patch does take: today's behaviour ──────────────────────

def test_a_patch_that_took_stands_the_watch_down(tmp_path, monkeypatch, caplog):
    """The ticket's test 2: no env limit for the model — the patch is read
    back, matches, and Kilo (not the watch) owns the window."""
    caplog.set_level(logging.INFO)
    _install(monkeypatch, pinned=())
    scenario = {"turns": [
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"],
         "message_info": {"tokens": {"input": 90_000, "output": 0}}},
    ]}
    sb, fake, _h, run, aborted = _run(tmp_path, scenario)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert len(fake.calls("PATCH")) == 1
    assert not aborted
    assert not _warned(caplog, "131072"), "a window that took is not reported as kept"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING
                and ("kept" in r.getMessage() or "armed" in r.getMessage())], \
        "a window that took leaves no 'kept' / 'armed' warning"
    assert _limits(fake)[MODEL]["input"] == 98_777 or _limits(fake)[MODEL]["context"] == 98_777 \
        or _limits(fake)[MODEL].get("input", 0) < DECLARED


# ── 3. the read-back that fails ──────────────────────────────────────────────

def test_a_failed_read_back_keeps_the_watch_armed(tmp_path, monkeypatch, caplog):
    """The ticket's test 3: `GET /provider` answers 500 after the patch. Nothing
    shows the window took, so the watch stays armed and stops the turn."""
    caplog.set_level(logging.INFO)
    _install(monkeypatch, pinned=())
    scenario = _watch_scenario()
    scenario["providers_status"] = 500
    sb, fake, _h, run, aborted = _run(tmp_path, scenario, max_continues_per_attempt=3)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert aborted and _watch_stopped(caplog), "an unverified patch is not a window"


# ── 4. a window that took only in part is not a window ───────────────────────

def test_a_partly_applied_patch_keeps_the_watch_armed(tmp_path, monkeypatch, caplog):
    """`limit.input` is what Kilo compacts by. A read-back whose `context`
    matches but whose `input` stayed at Kilo's own is a mismatch."""
    caplog.set_level(logging.INFO)
    _install(monkeypatch, pinned=(), keep_input=True)
    sb, fake, _h, run, aborted = _run(tmp_path, _watch_scenario(), max_continues_per_attempt=3)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert aborted and _watch_stopped(caplog), "the compact threshold did not move, so the watch has to"


# ── 5. where and when the read-back is made ──────────────────────────────────

def test_the_read_back_follows_the_patch_for_the_same_directory(tmp_path, monkeypatch):
    """The patch is per directory, so is the answer: `GET /provider` with the
    worktree's `directory`, after the patch — a read before it, or without the
    directory, shows another instance's config."""
    _install(monkeypatch, pinned=())
    scenario = {"turns": [{"on_prompt": tr.work_ready, "events": ["busy", "idle"]}]}
    sb, fake, _h, run, _aborted = _run(tmp_path, scenario)
    tr._assert_ready(run, sb.ws("agent-a"))
    calls = fake.calls()
    patch_at = max(i for i, r in enumerate(calls) if r["method"] == "PATCH")
    reads = [(i, r) for i, r in enumerate(calls)
             if r["method"] == "GET" and r["path"] == "/provider" and i > patch_at]
    assert reads, "nothing read the model back after the patch"
    wt = Path(sb.ws("agent-a").path).resolve()
    assert any(r["query"].get("directory") and Path(r["query"]["directory"]).resolve() == wt
               for _i, r in reads), [r["query"] for _i, r in reads]


# ── 6. a pinned model is not patched again at every prompt ───────────────────

def test_a_pinned_window_is_not_repatched_at_every_prompt(tmp_path, monkeypatch, caplog):
    """Three overflows in a row on a pinned window: still one patch."""
    caplog.set_level(logging.INFO)
    _install(monkeypatch, pinned={MODEL})
    scenario = {"abort_idles": True, "summary_tokens": 3_000, "turns": [
        {"events": ["busy"], "idle": False,
         "message_info": {"tokens": {"input": 90_000, "output": 0}}},
        {"events": ["busy"], "idle": False,
         "message_info": {"tokens": {"input": 90_500, "output": 0}}},
        {"events": ["busy"], "idle": False,
         "message_info": {"tokens": {"input": 91_000, "output": 0}}},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    _sb, fake, _h, _run_, _aborted = _run(tmp_path, scenario, max_continues_per_attempt=6)
    assert len(fake.calls("PATCH")) <= 1, len(fake.calls("PATCH"))
    assert _watch_stopped(caplog)


# ── 7. what the patch wrote does not outlive the run ─────────────────────────

@pytest.mark.parametrize("pinned", [True, False])
def test_the_project_file_the_patch_wrote_is_gone_after_the_run(tmp_path, monkeypatch, pinned):
    """Kilo writes `.kilo/kilo.jsonc` for a patch it then ignores as well as for
    one it takes. Either way the run's own file must not stay in the agent's
    worktree (it is ignored by git, but the next spawn would read it)."""
    _install(monkeypatch, pinned={MODEL} if pinned else ())
    scenario = {"turns": [{"on_prompt": tr.work_ready, "events": ["busy", "idle"]}]}
    sb, fake, _h, run, _aborted = _run(tmp_path, scenario)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert fake.calls("PATCH"), "the scenario pushed a window"
    assert not (Path(sb.ws("agent-a").path) / ".kilo" / "kilo.jsonc").exists()
