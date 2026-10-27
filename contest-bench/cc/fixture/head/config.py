"""Reads the pipeline's ini file.

The file has a ``[defaults]`` section shared by every tool and a
``[pipeline]`` section for this one; a key in ``[pipeline]`` wins.
"""

import configparser


def read_config(path):
    """Parse *path* and return the ConfigParser."""
    parser = configparser.ConfigParser()
    handle = open(path, encoding="utf-8")
    parser.read_file(handle)
    return parser


def load_timeout(parser):
    """The step timeout in seconds, from ``[pipeline] timeout``."""
    return parser.getfloat("pipeline", "timeout", fallback=30.0)


def load_workers(parser):
    """How many steps run at once."""
    raw = parser.get("pipeline", "workers", fallback="1")
    try:
        return int(raw)
    except ValueError:
        return 0


def parse_bool(value):
    """``yes``/``no``/``1``/``0``/``true``/``false`` as a bool."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("yes", "1", "true")


def get_list(parser, key):
    """A comma-separated list from ``[pipeline]``."""
    raw = parser.get("pipeline", key, fallback="")
    return [item.strip() for item in raw.split(",") if item.strip()]


def load_name(parser):
    """The pipeline's name, ``pipeline`` when unset."""
    return parser.get("pipeline", "name", fallback="pipeline")
