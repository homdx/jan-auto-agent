"""tests_bugfix/test_kilo_client_nan_retry_wait_210.py — bug 49: a NaN retry wait is no time, not a long delay.

`nan <= bound` is False, so `_retry_past_the_bound` read a NaN wait as a delay
past the bound: "quota" on a first retry, "backoff" after. Ticket 210: a NaN
wait is treated as None ("Kilo gave no numeric time"); inf stays a real, very
long delay.
"""

from __future__ import annotations

from tools.contest.kilo_client import _retry_past_the_bound

NAN, INF = float("nan"), float("inf")


def test_a_nan_wait_is_no_time():
    assert _retry_past_the_bound(NAN, 3, False, 60.0) is None
    assert _retry_past_the_bound(NAN, 1, False, 60.0) is None
    assert _retry_past_the_bound(NAN, 3, True, 60.0) == "quota"   # as None: the phrase decides
    assert _retry_past_the_bound(NAN, 3, False, 60.0) == _retry_past_the_bound(None, 3, False, 60.0)


def test_inf_is_still_a_very_long_delay():
    assert _retry_past_the_bound(INF, 1, False, 60.0) == "quota"
    assert _retry_past_the_bound(INF, 3, False, 60.0) == "quota"   # past 4x the bound
    assert _retry_past_the_bound(INF, 3, True, 60.0) == "quota"


def test_nan_answers_exactly_what_none_answers():
    """From the Sonnet 5.5 entry: every attempt, with and without a quota phrase."""
    for attempt in (1, 2, 9):
        for named in (False, True):
            assert (_retry_past_the_bound(NAN, attempt, named, 60.0)
                    == _retry_past_the_bound(None, attempt, named, 60.0))
