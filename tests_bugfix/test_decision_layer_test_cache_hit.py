"""A test-cache hit builds its Decision: the layer name the runner uses is one Decision accepts."""

from tools.contest.policy import LAYERS, Decision


def test_test_cache_layer_is_a_decision_layer():
    decision = Decision(reply="reject", layer="test-cache", reason="same tree, same command")
    assert decision.layer in LAYERS
