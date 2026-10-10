"""A JSON-backed key-value store."""

import json
from pathlib import Path


class Store:
    """Keys and values in one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict = {}

    @property
    def size(self) -> int:
        return len(self._data)

    @size.setter
    def size(self, value: int) -> None:
        raise AttributeError("size is read-only")

    def load(self) -> None:
        try:
            self._data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self._data = {}

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def items(self) -> dict:
        return self._data

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, sort_keys=True))
        tmp.replace(self.path)
