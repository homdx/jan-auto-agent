"""Settings read from an ini file."""

import configparser
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    def typed_section(name: str) -> dict: ...

DEFAULT_TIMEOUT = 30

to_int = lambda value: int(value.strip())


def read_config(path: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    return parser


def load_timeout(parser) -> int:
    """The step timeout, in seconds."""
    if not parser.has_section("defaults"):
        return DEFAULT_TIMEOUT
    return parser.getint("defaults", "timeout", fallback=DEFAULT_TIMEOUT)


def parse_bool(text: str) -> bool:
    return text.strip().lower() in ("1", "yes", "true", "on")


def get_list(parser, key: str) -> list:
    raw = parser.get("pipeline", key, fallback="")
    return [item for item in raw.split(",") if item]
