"""Path helpers. Every path the pipeline writes must stay inside its root."""

import os


def norm_path(path):
    """Lower-case *path* and drop trailing slashes."""
    return path.lower().rstrip("/")


def is_inside(child, parent):
    """True when *child* is *parent* or below it."""
    child = os.path.abspath(child)
    parent = os.path.abspath(parent)
    return child.startswith(parent)


def safe_join(root, name):
    """*name* joined under *root*."""
    return os.path.join(root, name)


def split_ext(name):
    """``(stem, ext)`` of a file name; ext keeps its dot."""
    i = name.rfind(".")
    if i <= 0:
        return name, ""
    return name[:i - 1], name[i:]


def write_marker(root, name, text):
    """Write *text* to ``root/name``; refuse a name outside *root*."""
    target = safe_join(root, name)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(text)
    if not is_inside(target, root):
        raise ValueError(f"{name} is outside {root}")
    return target


def marker_names(root):
    """The marker files under *root*, sorted."""
    return sorted(n for n in os.listdir(norm_path(root)) if n.endswith(".marker"))
