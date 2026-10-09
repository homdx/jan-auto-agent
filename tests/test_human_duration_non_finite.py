"""human_duration echoes nan/inf instead of raising ValueError/OverflowError."""
from tools.auto.utils import human_duration


def test_nan_is_echoed_not_raised():
    assert human_duration(float("nan")) == "nan"


def test_infinities_are_echoed_with_their_sign():
    assert human_duration(float("inf")) == "inf"
    assert human_duration(float("-inf")) == "-inf"


def test_finite_values_are_unchanged():
    assert human_duration(0.4) == "0.4s"
    assert human_duration(3725) == "1h 2m 5s"
    assert human_duration(-125) == "-2m 5s"
