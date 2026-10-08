"""207 bug 36: an Infinity in a run's tokens is a 0 cell, not an OverflowError out of the export."""
from tools.contest import export as ex


def test_number_of_inf_and_nan_is_zero():
    assert ex._number(float("inf")) == 0
    assert ex._number(float("nan")) == 0
    assert ex._number(float("-inf")) == 0


def test_number_of_the_ordinary_still_reads():
    assert ex._number(7) == 7
    assert ex._number("12") == 12
    assert ex._number(None) == 0


def test_token_dict_with_infinity_still_gets_its_pair():
    assert ex._token_pair({"input": float("inf"), "output": 5}) == (0, 5)
    assert ex._token_pair({"input": 3, "cache": {"read": float("inf")}, "output": 2}) == (3, 2)
