from __future__ import annotations

import os
from pathlib import Path



def resolve_path(path: str, base_dir: str | None = None) -> str:
    """
    Expand '~', make absolute, and optionally resolve relative paths against base_dir.
    """
    if path is None:
        raise TypeError("path must be a string, got None")

    p = Path(path).expanduser()

    if not p.is_absolute():
        if base_dir is not None:
            p = Path(base_dir).expanduser() / p
        p = p.resolve(strict=False)
    else:
        p = p.resolve(strict=False)

    return str(p)


def read_file(path: str) -> str:
    """
    Read a local file safely.

    - Raises FileNotFoundError with a clear message if the file is missing
    - Returns empty string for empty files
    - Tries UTF-8 first, then falls back to latin-1
    """
    resolved = Path(resolve_path(path))

    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {resolved}")
    if not resolved.is_file():
        raise FileNotFoundError(f"Not a file: {resolved}")

    if resolved.stat().st_size == 0:
        return ""

    try:
        return resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return resolved.read_text(encoding="latin-1")


def list_py_files(base_dir: str, skip_dirs: list) -> list[str]:
    """
    Walk a directory recursively and return all Python source files,
    skipping directories listed in skip_dirs.
    """
    root = Path(resolve_path(base_dir))

    if not root.exists():
        raise FileNotFoundError(f"Directory not found: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    skip_set = set(skip_dirs)
    results: list[str] = []

    for current_dir, dirnames, filenames in os.walk(root):
        # Remove skipped directories in-place so os.walk does not descend into them
        dirnames[:] = [d for d in dirnames if d not in skip_set]

        for filename in filenames:
            if filename.endswith(".py"):
                results.append(str(Path(current_dir) / filename))

    return results