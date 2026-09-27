"""KC-70 — intake's variant probes: a 24-hour memory, a retry, and a parallel pool."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_contest_cli as tc  # noqa: E402
from tools.contest import cli, probe_memory  # noqa: E402
from tools.contest.probe_memory import ProbeRecord  # noqa: E402
from tools.contest.roster import AgentSpec, RosterError, load_roster  # noqa: E402
from tools.contest.variant import (  # noqa: E402
    ProbeReason,
    VariantPick,
    retry_wait,
    retryable,
    with_retries,
)

# the CLI half binds the fake's ephemeral port, as test_contest_cli does
pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")

# the fixtures of the CLI tests, reused as they are
gate_key = tc.gate_key
sandbox = tc.sandbox
spawn_holder = tc.spawn_holder

HOUR = 3600.0
NOW = 1_800_000_000.0


def _write(path: Path, entries) -> Path:
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _rec(at, provider="kenary", model="hy3:free", variant="high") -> dict:
    return {"at": at, "provider": provider, "model": model, "variant": variant}


# ─────────────────────────────────────────────────────────────────────────────
# the memory file: read, age cut, eviction
# ─────────────────────────────────────────────────────────────────────────────

def test_a_missing_file_is_no_memory(tmp_path):
    assert probe_memory.load(tmp_path / "none.json", now=NOW) == []


@pytest.mark.parametrize("raw", ["", "not json", "{}", '{"at": 1}', "42", '"text"', "null"])
def test_a_file_that_is_not_a_list_is_no_memory(tmp_path, raw):
    path = tmp_path / "m.json"
    path.write_text(raw, encoding="utf-8")
    assert probe_memory.load(path, now=NOW) == []


@pytest.mark.parametrize("entry", [
    None, 7, "x", [], {},
    {"provider": "kenary", "model": "hy3:free", "variant": "high"},               # no time
    {"at": "soon", "provider": "kenary", "model": "hy3:free", "variant": "high"},
    {"at": True, "provider": "kenary", "model": "hy3:free", "variant": "high"},
    {"at": float("nan"), "provider": "kenary", "model": "hy3:free", "variant": "high"},
    {"at": NOW, "provider": "", "model": "hy3:free", "variant": "high"},
    {"at": NOW, "provider": "kenary", "model": " ", "variant": "high"},
    {"at": NOW, "provider": "kenary", "model": "hy3:free", "variant": None},
    {"at": NOW, "provider": "kenary", "model": "hy3:free"},
])
def test_an_entry_that_is_not_a_record_is_skipped_and_its_neighbour_kept(tmp_path, entry):
    path = _write(tmp_path / "m.json", [entry, _rec(NOW - 60)])
    assert probe_memory.load(path, now=NOW) == [ProbeRecord(NOW - 60, "kenary", "hy3:free",
                                                            "high")]


def test_the_age_cut_is_inclusive_at_24_hours_and_drops_one_second_past(tmp_path):
    path = _write(tmp_path / "m.json", [
        _rec(NOW - 24 * HOUR - 1, model="old"),
        _rec(NOW - 24 * HOUR, model="edge"),
        _rec(NOW - 23 * HOUR, model="young"),
    ])
    assert [r.model for r in probe_memory.load(path, now=NOW)] == ["edge", "young"]


def test_the_hours_are_the_config_s_not_a_constant(tmp_path):
    path = _write(tmp_path / "m.json", [_rec(NOW - 2 * HOUR)])
    assert probe_memory.load(path, hours=1, now=NOW) == []
    assert len(probe_memory.load(path, hours=3, now=NOW)) == 1


def test_a_record_from_the_future_is_trusted_only_within_the_slack(tmp_path):
    slack = probe_memory.FUTURE_SLACK_SEC
    path = _write(tmp_path / "m.json", [_rec(NOW + slack, model="skew"),
                                        _rec(NOW + slack + 1, model="future")])
    assert [r.model for r in probe_memory.load(path, now=NOW)] == ["skew"]


@pytest.mark.parametrize("hours", [0, -1, float("nan"), float("inf"), "abc", None])
def test_hours_that_are_no_age_are_no_memory(tmp_path, hours):
    path = _write(tmp_path / "m.json", [_rec(NOW)])
    assert probe_memory.load(path, hours=hours, now=NOW) == []


def test_duplicates_of_a_key_read_back_as_the_newest(tmp_path):
    path = _write(tmp_path / "m.json", [_rec(NOW - 50), _rec(NOW - 10), _rec(NOW - 30)])
    assert probe_memory.load(path, now=NOW) == [ProbeRecord(NOW - 10, "kenary", "hy3:free",
                                                            "high")]


def test_answered_matches_provider_model_and_variant_exactly():
    records = [ProbeRecord(NOW, "kenary", "hy3:free", "high")]
    assert probe_memory.answered(records, "kenary", "hy3:free", "high")
    assert not probe_memory.answered(records, "kenary", "hy3:free", "max")
    assert not probe_memory.answered(records, "kenary", "mimo-v2-5:free", "high")
    assert not probe_memory.answered(records, "sensenova", "hy3:free", "high")
    assert not probe_memory.answered(None, "kenary", "hy3:free", "high")
    assert not probe_memory.answered([_rec(NOW)], "kenary", "hy3:free", "high"), \
        "a raw dict is not a loaded record"


def test_update_writes_a_success_stamped_now_and_makes_the_parent(tmp_path):
    path = tmp_path / "deep" / "er" / "m.json"
    assert probe_memory.update(path, [("kenary", "hy3:free", "high")], now=NOW)
    assert json.loads(path.read_text()) == [_rec(NOW)]
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


def test_update_evicts_every_record_past_the_age_from_the_file_itself(tmp_path):
    path = _write(tmp_path / "m.json", [
        _rec(NOW - 30 * HOUR, model="gone-1"),
        _rec(NOW - 25 * HOUR, model="gone-2"),
        _rec(NOW - HOUR, model="kept"),
        _rec(NOW + 10 * HOUR, model="gone-future"),
        {"junk": True},
    ])
    assert probe_memory.update(path, [("sensenova", "sensenova-6.7-flash-lite", "high")],
                               now=NOW)
    on_disk = json.loads(path.read_text())
    assert [(e["provider"], e["model"]) for e in on_disk] == [
        ("kenary", "kept"), ("sensenova", "sensenova-6.7-flash-lite")]


def test_update_with_nothing_new_still_prunes_a_stale_file(tmp_path):
    path = _write(tmp_path / "m.json", [_rec(NOW - 48 * HOUR), _rec(NOW - HOUR, model="kept")])
    assert probe_memory.update(path, now=NOW)
    assert [e["model"] for e in json.loads(path.read_text())] == ["kept"]


def test_update_collapses_duplicates_to_one_line_per_key(tmp_path):
    path = _write(tmp_path / "m.json", [_rec(NOW - 50), _rec(NOW - 10), _rec(NOW - 30)])
    assert probe_memory.update(path, now=NOW)
    assert json.loads(path.read_text()) == [_rec(NOW - 10)]


def test_update_replaces_a_key_s_old_success_with_the_new_one(tmp_path):
    path = _write(tmp_path / "m.json", [_rec(NOW - 20 * HOUR), _rec(NOW - HOUR, model="other")])
    assert probe_memory.update(path, [("kenary", "hy3:free", "high")], now=NOW)
    assert json.loads(path.read_text()) == [_rec(NOW - HOUR, model="other"), _rec(NOW)]


def test_a_failure_takes_the_key_s_success_out(tmp_path):
    path = _write(tmp_path / "m.json", [_rec(NOW - HOUR), _rec(NOW - HOUR, variant="max")])
    assert probe_memory.update(path, failures=[("kenary", "hy3:free", "high")], now=NOW)
    assert json.loads(path.read_text()) == [_rec(NOW - HOUR, variant="max")]


def test_a_key_that_both_answered_and_failed_is_not_remembered(tmp_path):
    """Two agents, one key, two answers in one intake: the failure wins —
    the round must not trust a variant whose last word included a refusal."""
    path = tmp_path / "m.json"
    key = ("kenary", "hy3:free", "high")
    assert probe_memory.update(path, [key], [key], now=NOW)
    assert not path.exists(), "nothing to keep, and no empty file made for it"


def test_an_unchanged_file_is_not_rewritten(tmp_path):
    path = _write(tmp_path / "m.json", [_rec(NOW - HOUR)])
    path.write_text(json.dumps([_rec(NOW - HOUR)], indent=2) + "\n", encoding="utf-8")
    before = path.stat()
    os.utime(path, (before.st_atime - 100, before.st_mtime - 100))
    stamp = path.stat().st_mtime_ns
    assert probe_memory.update(path, now=NOW)
    assert path.stat().st_mtime_ns == stamp


def test_update_with_no_age_writes_nothing(tmp_path):
    path = tmp_path / "m.json"
    assert not probe_memory.update(path, [("kenary", "hy3:free", "high")], hours=0, now=NOW)
    assert not path.exists()


def test_a_write_that_cannot_be_made_is_false_not_an_exception(tmp_path):
    target = tmp_path / "is-a-dir"
    target.mkdir()
    assert not probe_memory.update(target, [("kenary", "hy3:free", "high")], now=NOW)
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_a_full_day_of_one_success(tmp_path):
    """Answered at t0: trusted at t0 + 23 h and at exactly 24 h, not a second
    later — and the next write takes it out of the file for good."""
    path = tmp_path / "m.json"
    key = ("sensenova", "sensenova-6.8-flash-lite", "high")
    assert probe_memory.update(path, [key], now=NOW)
    for later, trusted in ((0, True), (23 * HOUR, True), (24 * HOUR, True),
                           (24 * HOUR + 1, False), (72 * HOUR, False)):
        records = probe_memory.load(path, now=NOW + later)
        assert probe_memory.answered(records, *key) is trusted, later
    assert probe_memory.update(path, now=NOW + 24 * HOUR + 1)
    assert json.loads(path.read_text()) == []


def test_memory_path_defaults_next_to_the_rounds_output(tmp_path):
    config = tc.Sandbox(tmp_path).config()
    assert probe_memory.memory_path(config, tmp_path) == tmp_path / "contest-out" / \
        "probe-memory.json"
    assert probe_memory.memory_path(replace(config, probe_memory_file="m/p.json"),
                                    tmp_path) == tmp_path / "m" / "p.json"
    assert probe_memory.memory_path(replace(config, probe_memory_file="/abs/p.json"),
                                    tmp_path) == Path("/abs/p.json")
    assert probe_memory.memory_path(None, tmp_path) == tmp_path / "contest-out" / \
        "probe-memory.json"


@pytest.mark.parametrize("value, hours", [(24.0, 24.0), (1.5, 1.5), (0, 0.0), (-3, 0.0),
                                          ("x", 0.0), (float("nan"), 0.0)])
def test_hours_of_reads_the_key_and_zero_is_off(tmp_path, value, hours):
    config = tc.Sandbox(tmp_path).config()
    assert probe_memory.hours_of(replace(config, probe_memory_hours=value)) == hours
    assert probe_memory.hours_of(None) == probe_memory.DEFAULT_HOURS


# ─────────────────────────────────────────────────────────────────────────────
# the retry: what is asked again, how long it waits, how many times
# ─────────────────────────────────────────────────────────────────────────────

QUOTA_RE = re.compile("free-models-per-day", re.IGNORECASE)


@pytest.mark.parametrize("reason", [
    "no answer (timeout)", "no answer (closed)", "empty reply", "session.error",
    "Rate limit exceeded", "Too Many Requests", "HTTP POST /session -> 429: busy",
    "HTTP POST /session -> 503: down", "HTTP POST /session -> 408: slow",
    "HTTP POST /session -> 0: connection refused",
    "The model service is temporarily unavailable", "Provider is overloaded",
    "ConnectionResetError: [Errno 104]",
])
def test_passing_refusals_are_asked_again(reason):
    assert retryable(reason, QUOTA_RE)


@pytest.mark.parametrize("reason", [
    None,
    "the model's provider rejected the request. check the model id",
    "request was blocked by a gateway or proxy",
    "HTTP POST /session -> 400: {'error': 'bad variant'}",
    "HTTP POST /session -> 404: no such model",
    "A valid API key is required.",
    "You need to sign in to use this model.",
    "401 Unauthorized",
    "Rate limit exceeded: free-models-per-day",
])
def test_final_refusals_are_not(reason):
    assert not retryable(reason, QUOTA_RE)


def test_a_provider_quota_reason_is_final_even_without_phrases():
    reason = ProbeReason("Rate limit exceeded")
    reason.quota = True
    assert not retryable(reason, None)
    assert str(reason) == "Rate limit exceeded", "the text is what every caller prints"
    assert retryable(ProbeReason("Rate limit exceeded"), None)


@pytest.mark.parametrize("reason, attempt, wait", [
    ("no answer (timeout)", 1, 15.0), ("no answer (timeout)", 2, 30.0),
    ("Rate limit exceeded", 1, 30.0), ("HTTP POST /session -> 429: x", 2, 60.0),
    ("empty reply", 0, 15.0),
])
def test_the_wait_grows_with_the_attempt_and_doubles_for_a_rate_limit(reason, attempt, wait):
    assert retry_wait(reason, attempt, 15) == wait


def _scripted(*answers):
    """A `try_one` answering *answers* in turn; the calls land in `.calls`."""
    queue = list(answers)
    calls: list = []

    def try_one(variant):
        calls.append(variant)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    try_one.calls = calls
    return try_one


def test_an_answer_on_the_first_ask_waits_for_nothing():
    sleeps: list = []
    inner = _scripted(None)
    assert with_retries(inner, retries=2, wait_sec=15, sleep=sleeps.append)("high") is None
    assert inner.calls == ["high"] and sleeps == []


def test_a_timeout_then_an_answer_is_an_answer():
    sleeps: list = []
    inner = _scripted("no answer (timeout)", None)
    assert with_retries(inner, retries=2, wait_sec=15, sleep=sleeps.append)("high") is None
    assert inner.calls == ["high", "high"] and sleeps == [15.0]


def test_a_refusal_that_never_passes_is_asked_one_plus_retries_times():
    sleeps: list = []
    inner = _scripted("empty reply", "empty reply", "no answer (timeout)")
    ask = with_retries(inner, retries=2, wait_sec=15, sleep=sleeps.append)
    assert ask("xhigh") == "no answer (timeout)"
    assert len(inner.calls) == 3 and sleeps == [15.0, 30.0]


def test_a_rate_limit_waits_twice_as_long_before_each_ask():
    sleeps: list = []
    inner = _scripted("Rate limit exceeded", "Too Many Requests", None)
    assert with_retries(inner, retries=2, wait_sec=15, sleep=sleeps.append)("max") is None
    assert sleeps == [30.0, 60.0]


def test_a_final_refusal_is_not_asked_again():
    sleeps: list = []
    inner = _scripted("the model's provider rejected the request.")
    ask = with_retries(inner, retries=5, wait_sec=15, sleep=sleeps.append)
    assert ask("max") == "the model's provider rejected the request."
    assert len(inner.calls) == 1 and sleeps == []


def test_a_quota_is_not_asked_again():
    sleeps: list = []
    inner = _scripted("Rate limit exceeded: free-models-per-day")
    ask = with_retries(inner, retries=2, wait_sec=15, quota_re=QUOTA_RE, sleep=sleeps.append)
    assert ask("high") == "Rate limit exceeded: free-models-per-day"
    assert sleeps == []


def test_an_exception_is_a_refusal_that_is_asked_again():
    sleeps: list = []
    inner = _scripted(ConnectionError("reset"), None)
    assert with_retries(inner, retries=1, wait_sec=0, sleep=sleeps.append)(None) is None
    assert inner.calls == [None, None]


def test_an_exception_on_the_last_ask_is_the_answer_not_a_crash():
    inner = _scripted(ConnectionError("reset"), ConnectionError("reset again"))
    assert with_retries(inner, retries=1, wait_sec=0, sleep=lambda s: None)("high") == \
        "ConnectionError: reset again"


@pytest.mark.parametrize("retries", [0, -1, None])
def test_no_retries_is_the_probe_itself(retries):
    inner = _scripted("empty reply")
    assert with_retries(inner, retries=retries, wait_sec=15) is inner


# ─────────────────────────────────────────────────────────────────────────────
# the pool: side by side, capped per provider, the same answers in roster order
# ─────────────────────────────────────────────────────────────────────────────

def _offer(models: dict) -> dict:
    """`GET /provider` with *models* = {provider: {model_id: [variants]}}."""
    return {"all": [{"id": provider, "models": {
        model_id: {"id": model_id, "variants": {v: {} for v in variants}}
        for model_id, variants in ids.items()}} for provider, ids in models.items()]}


def _agent(name, provider, model_id, variant):
    return AgentSpec(name=name, provider_id=provider,
                     model_id=model_id, variant=variant)


ROUND_87 = _offer({
    "sensenova": {"sensenova-6.8-flash-lite": ["low", "medium", "high"],
                  "sensenova-6.7-flash-lite": ["low", "medium", "high"]},
    "kenary": {m: ["low", "medium", "high", "xhigh", "max"]
               for m in ("agnes-2-5-flash:free", "mimo-v2-5:free", "hy3:free",
                         "agnes-2-0-flash:free")},
})
ROUND_87_AGENTS = (
    _agent("sn68-var1", "sensenova", "sensenova-6.8-flash-lite", "high"),
    _agent("sn68-var2", "sensenova", "sensenova-6.8-flash-lite", "high"),
    _agent("sn67-var1", "sensenova", "sensenova-6.7-flash-lite", "high"),
    _agent("sn67-var2", "sensenova", "sensenova-6.7-flash-lite", "high"),
    _agent("agnes-2-5-flash", "kenary", "agnes-2-5-flash:free", "high"),
    _agent("mimo-v2-5", "kenary", "mimo-v2-5:free", "high"),
    _agent("hy3", "kenary", "hy3:free", "high"),
    _agent("agnes-2-0-flash", "kenary", "agnes-2-0-flash:free", "high"),
)


class _Probes:
    """A `probe_for` that counts asks, and how many run at once per provider."""

    def __init__(self, answer=lambda agent, variant: None, hold: float = 0.05):
        self.answer = answer
        self.hold = hold
        self.lock = threading.Lock()
        self.asked: list = []
        self.now: dict = {}
        self.peak: dict = {}
        self.peak_all = 0

    def __call__(self, agent):
        def try_one(variant):
            with self.lock:
                self.asked.append((agent.model, variant))
                self.now[agent.provider_id] = self.now.get(agent.provider_id, 0) + 1
                self.peak[agent.provider_id] = max(self.peak.get(agent.provider_id, 0),
                                                   self.now[agent.provider_id])
                self.peak_all = max(self.peak_all, sum(self.now.values()))
            try:
                time.sleep(self.hold)
                return self.answer(agent, variant)
            finally:
                with self.lock:
                    self.now[agent.provider_id] -= 1
        return try_one


def test_round_87_asks_six_probes_not_eight():
    probes = _Probes(hold=0)
    cli.resolve_variants(ROUND_87, ROUND_87_AGENTS, probes, parallel=8, per_provider=3)
    assert len(probes.asked) == 6 and len(set(probes.asked)) == 6


def test_the_probes_run_side_by_side():
    """Six probes that each wait for all six: only a pool of six gets past it."""
    barrier = threading.Barrier(6, timeout=10)

    def answer(agent, variant):
        barrier.wait()
        return None

    probes = _Probes(answer, hold=0)
    started = time.monotonic()
    agents, failures, notes = cli.resolve_variants(ROUND_87, ROUND_87_AGENTS, probes,
                                                   parallel=8, per_provider=0)
    assert time.monotonic() - started < 10
    assert failures == [] and notes == [] and agents == ROUND_87_AGENTS
    assert probes.peak_all == 6


def test_one_provider_never_runs_more_than_its_cap():
    probes = _Probes(hold=0.1)
    cli.resolve_variants(ROUND_87, ROUND_87_AGENTS, probes, parallel=8, per_provider=2)
    assert probes.peak["kenary"] <= 2 and probes.peak["sensenova"] <= 2
    assert probes.peak_all >= 3, "the other provider's slots are not held by the capped one"


def test_the_pool_is_capped_by_probe_parallel():
    probes = _Probes(hold=0.1)
    cli.resolve_variants(ROUND_87, ROUND_87_AGENTS, probes, parallel=2, per_provider=0)
    assert probes.peak_all <= 2


def _mixed_answer(agent, variant):
    if agent.model_id == "hy3:free":
        return "Rate limit exceeded: free-models-per-day"
    if agent.model_id == "mimo-v2-5:free":
        return "the model's provider rejected the request."
    if agent.model_id == "glm:free" and variant in ("max", "xhigh"):
        return "the model's provider rejected the request."
    return None


MIXED_OFFER = _offer({
    "sensenova": {"sensenova-6.8-flash-lite": ["low", "medium", "high"]},
    "kenary": {"hy3:free": ["low", "high"], "mimo-v2-5:free": ["high"],
               "glm:free": ["low", "high", "xhigh", "max"], "plain:free": []},
})
MIXED_AGENTS = (
    _agent("sn68", "sensenova", "sensenova-6.8-flash-lite", "high"),
    _agent("hy3", "kenary", "hy3:free", "high"),
    _agent("glm-1", "kenary", "glm:free", "highest"),
    _agent("mimo", "kenary", "mimo-v2-5:free", "high"),
    _agent("glm-2", "kenary", "glm:free", "highest"),
    _agent("typo", "kenary", "hy3:free", "turbo"),
    _agent("plain", "kenary", "plain:free", "highest"),
    _agent("none", "kenary", "plain:free", None),
)


def test_the_pool_gives_what_the_one_at_a_time_walk_gives_line_for_line():
    one = cli.resolve_variants(MIXED_OFFER, MIXED_AGENTS, _Probes(_mixed_answer, hold=0),
                               quota_re=QUOTA_RE)
    pooled = _Probes(_mixed_answer, hold=0.02)
    many = cli.resolve_variants(MIXED_OFFER, MIXED_AGENTS, pooled, quota_re=QUOTA_RE,
                                parallel=8, per_provider=2)
    assert many == one
    agents, failures, notes = many
    assert [a.name for a in agents] == ["sn68", "glm-1", "mimo", "glm-2", "typo", "plain",
                                        "none"], "hy3 is out for its quota"
    assert [a.variant for a in agents if a.name.startswith("glm")] == ["high", "high"]
    assert failures == ["[typo] kenary/hy3:free: no variant 'turbo' — listed: low, high"]
    assert notes[0].startswith("[hy3] kenary/hy3:free: provider_quota")
    assert notes[1].startswith("kenary/glm:free@highest → high (failed: max:")
    assert notes[2].startswith("kenary/mimo-v2-5:free@high: the probe refused it")
    assert len(notes) == 3, "glm is noted once, however many agents run it"
    assert pooled.asked.count(("kenary/glm:free", "max")) == 1


def test_a_pool_of_one_is_the_walk_itself_in_roster_order():
    probes = _Probes(hold=0)
    cli.resolve_variants(ROUND_87, ROUND_87_AGENTS, probes, parallel=1)
    assert [model for model, _ in probes.asked] == [
        "sensenova/sensenova-6.8-flash-lite", "sensenova/sensenova-6.7-flash-lite",
        "kenary/agnes-2-5-flash:free", "kenary/mimo-v2-5:free", "kenary/hy3:free",
        "kenary/agnes-2-0-flash:free"]


# ─────────────────────────────────────────────────────────────────────────────
# the roster keys
# ─────────────────────────────────────────────────────────────────────────────

def test_the_defaults(tmp_path):
    config = tc.Sandbox(tmp_path).config()
    assert (config.probe_memory_file, config.probe_memory_hours, config.probe_parallel,
            config.probe_per_provider, config.probe_retries,
            config.probe_retry_wait_sec) == ("", 24.0, 8, 3, 2, 15.0)


def _roster_with(sb, extra: str) -> Path:
    roster = sb.repo / "contest.ini"
    roster.write_text(roster.read_text().replace(
        "out_dir = contest-out", "out_dir = contest-out\n" + extra, 1))
    return roster


@pytest.mark.parametrize("key", ["probe_parallel", "probe_per_provider", "probe_retries"])
def test_a_negative_count_is_refused(tmp_path, key):
    sb = tc.Sandbox(tmp_path)
    with pytest.raises(RosterError, match=key):
        load_roster(_roster_with(sb, f"{key} = -1"))


def test_a_negative_wait_is_refused(tmp_path):
    sb = tc.Sandbox(tmp_path)
    with pytest.raises(RosterError, match="probe_retry_wait_sec"):
        load_roster(_roster_with(sb, "probe_retry_wait_sec = -5"))


def test_a_pool_of_zero_is_one_and_negative_hours_are_off(tmp_path):
    sb = tc.Sandbox(tmp_path)
    config = load_roster(_roster_with(sb, "probe_parallel = 0\nprobe_memory_hours = -1"))
    assert config.probe_parallel == 1 and config.probe_memory_hours == 0.0


def test_hours_that_are_not_a_number_are_the_default_as_in_kc67(tmp_path):
    """A typo must not switch the memory off silently: KC-67's `num` rule."""
    sb = tc.Sandbox(tmp_path)
    config = load_roster(_roster_with(sb, "probe_memory_hours = soon"))
    assert config.probe_memory_hours == 24.0


# ─────────────────────────────────────────────────────────────────────────────
# `run`: the file is created, used, and cleaned — and nothing is printed
# ─────────────────────────────────────────────────────────────────────────────

MEDIUM = ["--ticket", "1", "--models", "glm-4-7-flash:free", "--variant", "medium",
          "--no-gate", "--no-tests"]
KEY = ("kenary", "glm-4-7-flash:free", "medium")


def _memory(sb) -> Path:
    return sb.repo / "contest-out" / "probe-memory.json"


def _probe_titles(fake) -> list:
    return [s["body"]["title"] for s in tc._sessions(fake)
            if s["body"]["title"].startswith("variant-probe/")]


def test_the_first_run_writes_the_memory_and_the_second_asks_nothing(sandbox, capsys,
                                                                      spawn_holder):
    code, fake = tc.run_fake(sandbox, tc.KC49_SCENARIO, MEDIUM, spawn_holder)
    first = capsys.readouterr()
    assert code == 0, first.err
    assert _probe_titles(fake) == ["variant-probe/glm-4-7-flash:free/medium"]
    (entry,) = json.loads(_memory(sandbox).read_text())
    assert (entry["provider"], entry["model"], entry["variant"]) == KEY
    assert time.time() - entry["at"] < 120

    code, fake = tc.run_fake(sandbox, tc.KC49_SCENARIO, ["--fresh", *MEDIUM], spawn_holder)
    second = capsys.readouterr()
    assert code == 0, second.err
    assert _probe_titles(fake) == [], "a named variant that answered today is not asked"
    assert tc._plan(second.out)["agents"] == "1: kenary/glm-4-7-flash:free@medium"
    assert json.loads(_memory(sandbox).read_text()) == [entry], "a hit does not re-stamp"
    for out in (first, second):
        assert "variant:" not in out.out and "probe" not in out.out + out.err


def test_a_success_older_than_24_hours_is_asked_again_and_evicted(sandbox, capsys,
                                                                   spawn_holder):
    path = _memory(sandbox)
    path.parent.mkdir(parents=True)
    stale = time.time() - 24 * HOUR - 60
    _write(path, [_rec(stale, *KEY), _rec(stale, "kenary", "gone:free", "high"),
                  _rec(time.time() - HOUR, "kenary", "kept:free", "high")])

    code, fake = tc.run_fake(sandbox, tc.KC49_SCENARIO, MEDIUM, spawn_holder)
    assert code == 0, capsys.readouterr().err
    assert _probe_titles(fake) == ["variant-probe/glm-4-7-flash:free/medium"]
    entries = json.loads(path.read_text())
    assert [e["model"] for e in entries] == ["kept:free", "glm-4-7-flash:free"]
    assert entries[1]["at"] > stale + 24 * HOUR


def test_reprobe_asks_anyway_and_a_refusal_takes_the_success_out(sandbox, capsys,
                                                                 spawn_holder):
    path = _memory(sandbox)
    path.parent.mkdir(parents=True)
    _write(path, [_rec(time.time() - HOUR, *KEY)])
    scenario = dict(tc.KC49_SCENARIO, reject_variants={"medium": tc.KC49_REJECTED})

    code, fake = tc.run_fake(sandbox, scenario, ["--reprobe", *MEDIUM], spawn_holder)
    captured = capsys.readouterr()
    # the fake rejects `medium` in the round's own session too, so the round
    # ends with no READY (2); what matters here is that intake let it start
    assert code != cli.EXIT_FAILED, captured.err
    assert not [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert _probe_titles(fake) == ["variant-probe/glm-4-7-flash:free/medium"], \
        "a rejected request is not asked again"
    assert "the probe refused it" in captured.out
    assert json.loads(path.read_text()) == []


def test_zero_hours_writes_no_memory_and_always_asks(sandbox, capsys, spawn_holder):
    _roster_with(sandbox, "probe_memory_hours = 0")
    for _ in range(2):
        code, fake = tc.run_fake(sandbox, tc.KC49_SCENARIO, ["--fresh", *MEDIUM],
                                 spawn_holder)
        assert code == 0, capsys.readouterr().err
        assert len(_probe_titles(fake)) == 1
    assert not _memory(sandbox).exists()


def test_highest_is_kc11_s_and_leaves_the_probe_memory_alone(sandbox, capsys, spawn_holder):
    code, _fake = tc.run_fake(sandbox, tc.KC49_SCENARIO,
                              ["--ticket", "1", "--models", "glm-4-7-flash:free",
                               "--no-gate", "--no-tests"], spawn_holder)
    assert code == 0, capsys.readouterr().err
    assert not _memory(sandbox).exists()


def test_run_retries_a_passing_refusal_and_remembers_the_answer(sandbox, capsys,
                                                                spawn_holder, monkeypatch):
    """The intake's own probe: a 503 first, then hello — one retry, no wait
    (`probe_retry_wait_sec = 0`), and the answer is what the memory keeps."""
    _roster_with(sandbox, "probe_retry_wait_sec = 0")
    asked: list = []
    answers = ["The model service is temporarily unavailable", None]

    def fake_hello_probe(server, provider_id, model_id, **kwargs):
        def try_one(variant):
            asked.append((model_id, variant))
            return answers.pop(0)
        return try_one

    monkeypatch.setattr(cli, "hello_probe", fake_hello_probe)
    code, _fake = tc.run_fake(sandbox, tc.KC49_SCENARIO, MEDIUM, spawn_holder)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert asked == [("glm-4-7-flash:free", "medium")] * 2
    assert "variant:" not in captured.out
    (entry,) = json.loads(_memory(sandbox).read_text())
    assert (entry["provider"], entry["model"], entry["variant"]) == KEY


def test_run_gives_up_after_the_retries_and_remembers_nothing(sandbox, capsys,
                                                              spawn_holder, monkeypatch):
    _roster_with(sandbox, "probe_retry_wait_sec = 0\nprobe_retries = 3")
    asked: list = []

    def fake_hello_probe(server, provider_id, model_id, **kwargs):
        def try_one(variant):
            asked.append(variant)
            return "no answer (timeout)"
        return try_one

    monkeypatch.setattr(cli, "hello_probe", fake_hello_probe)
    code, _fake = tc.run_fake(sandbox, tc.KC49_SCENARIO, MEDIUM, spawn_holder)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert asked == ["medium"] * 4
    assert "the probe refused it — no answer (timeout)" in captured.out
    assert not _memory(sandbox).exists()
