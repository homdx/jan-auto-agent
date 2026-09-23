"""KC-49 — a roster model runs at a named reasoning variant, or at `highest`.

The pure half (`listed_variants`, `ladder`, `pick_variant`, `resolve_variants`,
`agents_from_models`' `@variant`) is tested without a server. The client's
`model.variant` / `variant` / `DELETE /session/{id}` and `hello_probe` are
tested against `_kilo_fake`, whose `reject_variants` answers a variant with the
`session.error` a provider sends — the shape `kenary/glm-4-7-flash:free` gives
`max` on 7.6.2 (HTTP 400, "the model's provider rejected the request").
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from _kilo_fake import FakeKiloServer

from tools.contest import cli
from tools.contest.kilo_client import EventTap, KiloClient, KiloServer
from tools.contest.roster import AgentSpec
from tools.contest.variant import (
    HIGHEST,
    VariantPick,
    hello_probe,
    ladder,
    listed_variants,
    pick_variant,
)

RULES = [{"permission": "*", "pattern": "*", "action": "allow"}]

#: What glm-4-7-flash:free's provider answered `max` with, trimmed to the
#: fields the runner reads.
REJECTED = {"name": "APIError",
            "data": {"message": "the model's provider rejected the request. check the "
                                "model id, request fields, and context length",
                     "statusCode": 400, "isRetryable": False}}

GLM_VARIANTS = ("none", "low", "medium", "high", "xhigh", "max")


def _model(model_id, variants=()):
    return {"id": model_id, "status": "active",
            "capabilities": {"reasoning": bool(variants), "toolcall": True},
            "variants": {name: {"reasoningEffort": name} for name in variants}}


def _offer(models, provider_id="kenary"):
    return {"all": [{"id": provider_id, "name": provider_id, "source": "static",
                     "models": {m["id"]: m for m in models}}],
            "default": {}, "connected": [provider_id], "failed": []}


OFFER = _offer([_model("glm-4-7-flash:free", GLM_VARIANTS),
                _model("hy3:free", ("low", "medium", "high")),
                _model("plain:free")])


# ── the pure half ────────────────────────────────────────────────────────────

def test_listed_variants_reads_the_models_variants_in_order():
    assert listed_variants(OFFER, "kenary", "glm-4-7-flash:free") == list(GLM_VARIANTS)
    assert listed_variants(OFFER, "kenary", "plain:free") == []
    assert listed_variants(OFFER, "kenary", "absent:free") == []
    assert listed_variants(OFFER, "other", "hy3:free") == []
    assert listed_variants({}, "kenary", "hy3:free") == []


def test_ladder_is_strongest_first_then_no_variant():
    assert ladder(GLM_VARIANTS) == ["max", "xhigh", "high", "medium", "low", "none", None]
    assert ladder(("low", "high", "medium")) == ["high", "medium", "low", None]
    assert ladder(()) == [None]


def test_ladder_leaves_out_a_name_it_cannot_rank():
    assert ladder(("turbo", "high")) == ["high", None]


def test_pick_variant_takes_the_first_rung_that_answers():
    asked = []

    def try_one(rung):
        asked.append(rung)
        return "rejected" if rung in ("max", "xhigh") else None

    pick = pick_variant(ladder(GLM_VARIANTS), try_one)
    assert pick == VariantPick(variant="high", usable=True,
                               tried=(("max", "rejected"), ("xhigh", "rejected")))
    assert asked == ["max", "xhigh", "high"]
    assert pick.describe() == "high (failed: max: rejected; xhigh: rejected)"


def test_pick_variant_falls_back_to_no_variant_and_then_to_unusable():
    only_plain = pick_variant(["high", None], lambda rung: None if rung is None else "400")
    assert (only_plain.variant, only_plain.usable) == (None, True)
    assert only_plain.describe() == "(none) (failed: high: 400)"

    nothing = pick_variant(["high", None], lambda rung: "down")
    assert (nothing.variant, nothing.usable) == (None, False)
    assert nothing.tried == (("high", "down"), (None, "down"))


def test_pick_variant_counts_an_exception_as_a_failed_rung():
    def try_one(rung):
        if rung == "high":
            raise OSError("socket closed")

    pick = pick_variant(["high", "low", None], try_one)
    assert pick.variant == "low"
    assert pick.tried == (("high", "OSError: socket closed"),)


def test_agents_from_models_reads_an_at_variant_and_keeps_it_out_of_the_name():
    agents = cli.agents_from_models(
        "sensenova123/sensenova-6.8-flash-lite@high,glm-4-7-flash:free@highest,hy3:free")
    assert [(a.name, a.provider_id, a.model_id, a.variant) for a in agents] == [
        ("sensenova-6-8-flash-lite", "sensenova123", "sensenova-6.8-flash-lite", "high"),
        ("glm-4-7-flash", "kenary", "glm-4-7-flash:free", HIGHEST),
        ("hy3", "kenary", "hy3:free", None),
    ]


def test_two_variants_of_one_model_are_two_named_agents():
    agents = cli.agents_from_models("hy3:free@high,hy3:free@low")
    assert [(a.name, a.variant) for a in agents] == [("hy3-var1", "high"), ("hy3-var2", "low")]


def test_resolve_variants_refuses_a_name_the_model_does_not_list():
    agents = cli.agents_from_models("hy3:free@max,plain:free@high")
    resolved, failures, notes = cli.resolve_variants(OFFER, agents)
    assert failures == [
        "[hy3] kenary/hy3:free: no variant 'max' — listed: low, medium, high",
        "[plain] kenary/plain:free: no variant 'high' — listed: (none)",
    ]
    assert resolved == agents and notes == []


def test_resolve_variants_passes_a_listed_name_through_untouched_and_asks_nothing():
    agents = cli.agents_from_models("hy3:free@high,plain:free")

    def probe_for(agent):
        raise AssertionError("a named variant is never probed")

    resolved, failures, notes = cli.resolve_variants(OFFER, agents, probe_for)
    assert (resolved, failures, notes) == (agents, [], [])


def test_resolve_variants_probes_highest_once_per_model():
    agents = cli.agents_from_models("glm-4-7-flash:free@highest,glm-4-7-flash:free@highest")
    probed = []

    def probe_for(agent):
        probed.append(agent.name)
        return lambda rung: "rejected" if rung in ("max", "xhigh") else None

    resolved, failures, notes = cli.resolve_variants(OFFER, agents, probe_for)
    assert failures == []
    assert [a.variant for a in resolved] == ["high", "high"]
    assert probed == ["glm-4-7-flash-var1"]
    assert notes == [("kenary/glm-4-7-flash:free@highest → "
                      "high (failed: max: rejected; xhigh: rejected)")]


def test_resolve_variants_refuses_highest_when_nothing_answers_or_nothing_can_ask():
    agents = cli.agents_from_models("hy3:free@highest")
    _, failures, _ = cli.resolve_variants(OFFER, agents, lambda agent: (lambda rung: "down"))
    assert failures == [("[hy3] kenary/hy3:free: no variant answered 'say: hello' — unusable "
                         "(failed: high: down; medium: down; low: down; (none): down)")]
    _, failures, _ = cli.resolve_variants(OFFER, agents)
    assert failures == [("[hy3] kenary/hy3:free: variant 'highest' needs GET /provider and "
                         "a server to ask — neither is attached")]


def test_highest_on_a_model_that_lists_no_variants_asks_nothing_and_sends_none():
    agents = cli.agents_from_models("plain:free@highest")

    def probe_for(agent):
        raise AssertionError("nothing to choose, nothing to ask")

    resolved, failures, notes = cli.resolve_variants(OFFER, agents, probe_for)
    assert [a.variant for a in resolved] == [None]
    assert (failures, notes) == ([], [])


class _Flags:
    models = "hy3:free,glm-4-7-flash:free@max"
    provider = None
    variant = None
    max_parallel = None
    no_gate = False


def test_with_no_flag_every_agent_that_names_none_is_highest():
    config = cli._apply_flags(cli.ContestConfig(), _Flags)
    assert [(a.name, a.variant) for a in config.agents] == [("hy3", HIGHEST),
                                                            ("glm-4-7-flash", "max")]


def test_the_roster_key_is_the_default_behind_no_flag():
    config = cli._apply_flags(cli.ContestConfig(variant="medium"), _Flags)
    assert [a.variant for a in config.agents] == ["medium", "max"]


def test_variant_default_sends_none():
    class Flags(_Flags):
        models = "hy3:free,glm-4-7-flash:free@max,x:free@default"
        variant = "default"

    config = cli._apply_flags(cli.ContestConfig(), Flags)
    assert [a.variant for a in config.agents] == [None, "max", None]


def test_variant_flag_is_the_default_behind_an_agent_that_names_none():
    class Args:
        models = "hy3:free,glm-4-7-flash:free@max"
        provider = None
        variant = "high"
        max_parallel = None
        no_gate = False

    base = cli.ContestConfig(agents=(AgentSpec("x", "kenary", "x:free"),))
    config = cli._apply_flags(base, Args)
    assert [(a.name, a.variant) for a in config.agents] == [("hy3", "high"),
                                                            ("glm-4-7-flash", "max")]


# ── against the fake server ──────────────────────────────────────────────────

def _attached(scenario, tmp_path):
    fake = FakeKiloServer(scenario, directory=str(tmp_path)).start()
    return fake, KiloServer.attach(fake.url)


def _tap(fake, tmp_path):
    tap = EventTap(fake.url, str(tmp_path), str(tmp_path / "events.jsonl")).start()
    deadline = time.monotonic() + 30
    while not fake.subscribers and time.monotonic() < deadline:
        time.sleep(0.02)
    assert fake.subscribers, "the tap never connected"
    return tap


def test_a_session_with_a_variant_sends_it_on_create_and_on_every_prompt(tmp_path):
    fake, server = _attached({"turns": [{"events": ["busy", "idle"], "assistant": "a"},
                                        {"events": ["busy", "idle"], "assistant": "b"}]},
                             tmp_path)
    try:
        client = KiloClient(server, str(tmp_path))
        session = client.create_session("kenary", "hy3:free", rules=RULES, title="t",
                                        variant="high")
        assert session.variant == "high"
        client.prompt(session, "one")
        client.prompt(session, "two")
    finally:
        server.close()
        fake.stop()

    (created,) = fake.calls(method="POST", path="/session")
    assert created["body"]["model"] == {"providerID": "kenary", "id": "hy3:free",
                                        "variant": "high"}
    prompts = fake.calls(method="POST", path="/prompt_async")
    assert [p["body"]["variant"] for p in prompts] == ["high", "high"]
    assert all(p["body"]["model"] == {"providerID": "kenary", "modelID": "hy3:free"}
               for p in prompts)


def test_a_session_without_a_variant_sends_no_variant_key(tmp_path):
    fake, server = _attached({"turns": [{"events": ["busy", "idle"], "assistant": "a"}]},
                             tmp_path)
    try:
        client = KiloClient(server, str(tmp_path))
        session = client.create_session("kenary", "hy3:free", rules=RULES, title="t")
        client.prompt(session, "one")
    finally:
        server.close()
        fake.stop()
    assert session.variant is None
    (created,) = fake.calls(method="POST", path="/session")
    assert "variant" not in created["body"]["model"]
    (prompt,) = fake.calls(method="POST", path="/prompt_async")
    assert "variant" not in prompt["body"]


def test_delete_session_removes_it(tmp_path):
    fake, server = _attached({}, tmp_path)
    try:
        client = KiloClient(server, str(tmp_path))
        session = client.create_session("kenary", "hy3:free", rules=RULES, title="t")
        client.delete_session(session)
        assert fake.sessions() == []
        assert [r["path"] for r in fake.calls(method="DELETE")] == [f"/session/{session.id}"]
    finally:
        server.close()
        fake.stop()


def test_hello_probe_walks_down_past_the_variants_the_provider_rejects(tmp_path):
    """glm-4-7-flash:free's case: `max` and `xhigh` are listed and rejected,
    `high` answers — and every probe session is deleted."""
    scenario = {"reject_variants": {"max": REJECTED, "xhigh": REJECTED},
                "turns": [{"events": ["busy", "idle"], "assistant": "hello"}]}
    fake, server = _attached(scenario, tmp_path)
    try:
        try_one = hello_probe(server, "kenary", "glm-4-7-flash:free", timeout=20)
        pick = pick_variant(ladder(GLM_VARIANTS), try_one)
    finally:
        server.close()
        fake.stop()

    assert pick.variant == "high" and pick.usable
    assert [rung for rung, _ in pick.tried] == ["max", "xhigh"]
    # the reasons in the message: a stress-run red here once showed only
    # `all(<generator>)`, which cannot say whether the error event was lost
    # (`no answer (timeout)`) or the HTTP call failed (`HTTP …`)
    assert all("rejected the request" in reason for _, reason in pick.tried), pick.tried
    created = fake.calls(method="POST", path="/session")
    assert [c["body"]["model"].get("variant") for c in created] == ["max", "xhigh", "high"]
    assert all(c["body"]["permission"] == [{"permission": "*", "pattern": "*",
                                            "action": "deny"}] for c in created)
    assert [p["body"]["parts"][0]["text"] for p in fake.calls(method="POST",
                                                                path="/prompt_async")] \
        == ["say: hello"] * 3
    assert len(fake.calls(method="DELETE")) == 3 and fake.sessions() == []


def test_hello_probe_counts_an_empty_reply_as_a_failed_rung(tmp_path):
    fake, server = _attached({"turns": [{"events": ["busy", "idle"], "assistant": ""}]},
                             tmp_path)
    try:
        reason = hello_probe(server, "kenary", "hy3:free", timeout=20)("high")
    finally:
        server.close()
        fake.stop()
    assert reason == "empty reply"


@pytest.mark.parametrize("variant", [None, "low"])
def test_hello_probe_accepts_an_answer(tmp_path, variant):
    fake, server = _attached({"turns": [{"events": ["busy", "idle"], "assistant": "hello"}]},
                             tmp_path)
    try:
        assert hello_probe(server, "kenary", "hy3:free", timeout=20)(variant) is None
    finally:
        server.close()
        fake.stop()
