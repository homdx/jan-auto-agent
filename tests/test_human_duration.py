"""Tests for tools.auto.utils.human_duration (selfhost pilot task)."""
from tools.auto.utils import human_duration


def test_subsecond():
    assert human_duration(0.4) == "0.4s"


def test_seconds_only():
    assert human_duration(42) == "42s"


def test_minutes_seconds():
    assert human_duration(125) == "2m 5s"


def test_hours():
    assert human_duration(3725) == "1h 2m 5s"


def test_days():
    assert human_duration(90061) == "1d 1h 1m 1s"


def test_zero():
    assert human_duration(0) == "0.0s"


def test_negative():
    assert human_duration(-125) == "-2m 5s"


def test_exact_minute():
    assert human_duration(120) == "2m"
