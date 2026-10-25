"""A `%` in a numeric setting falls back with a warning, like any other malformed value."""
import configparser
import logging

import pytest

from tools.config_safe import safe_getboolean, safe_getfloat, safe_getint


def _config(value: str) -> configparser.ConfigParser:
    config = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))   # main.py's parser
    config.read_string(f"[auto]\nkey = {value}\n")
    return config


@pytest.mark.parametrize("value", ["50%", "%(missing)s", "%", "12 %"])
@pytest.mark.parametrize("helper,fallback", [(safe_getint, 3), (safe_getfloat, 3.0), (safe_getboolean, True)])
def test_an_interpolation_error_falls_back_and_names_the_key(value, helper, fallback, caplog):
    with caplog.at_level(logging.WARNING):
        assert helper(_config(value), "auto", "key", fallback=fallback) == fallback
    assert "[auto] key" in caplog.text


@pytest.mark.parametrize("value,want", [("5", 5), ("50%%", None)])
def test_a_good_value_and_an_escaped_percent_read_as_before(value, want, caplog):
    got = safe_getint(_config(value), "auto", "key", fallback=3)
    assert got == (3 if want is None else want)     # "50%%" is "50%": not a number, so the fallback


def test_the_canon_validator_builds_with_a_percent_in_its_setting():
    from tools.auto.canon_validator import make_canon_validator
    config = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    config.read_string("[auto]\ncanon_check_every = 50%\n")
    make_canon_validator(config)                    # raised InterpolationSyntaxError
