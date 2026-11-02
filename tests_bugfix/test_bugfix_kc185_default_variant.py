"""KC-185 follow-up: a `|` list survives `run`'s default variant.

`tools.contest run --models "a/m|b/n"` crashed before the round started:
`_apply_flags` gives every agent without a variant the default one through
`AgentSpec.with_default_variant`, which unpacked each `ModelRef` of the list as a
`(provider, model, variant)` tuple (`TypeError: cannot unpack non-iterable
ModelRef object`). The KC-185 tests built their agents with `agents_from_models`
and never went through it.
"""

from __future__ import annotations

from tools.contest.cli import agents_from_models


def test_a_pipe_agent_takes_the_default_variant_on_every_model_without_one():
    agent = agents_from_models("kenary/m-a:free|zai/m-b@low")[0].with_default_variant("high")
    assert [(r.provider_id, r.model_id, r.variant) for r in agent.models] == [
        ("kenary", "m-a:free", "high"), ("zai", "m-b", "low")]
    assert agent.variant == "high"


def test_a_one_model_agent_is_unchanged():
    agent = agents_from_models("kenary/m:free")[0].with_default_variant("high")
    assert (agent.provider_id, agent.model_id, agent.variant, agent.models) == ("kenary", "m:free", "high", ())
