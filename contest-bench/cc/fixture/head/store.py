"""A tiny JSON key-value store used by the pipeline for step results."""

import copy
import json


class StoreError(Exception):
    """The store's file cannot be read or written."""


class Store:
    def __init__(self, path):
        self.path = path
        self._data = {}

    def load(self):
        """Read the file; a missing file is an empty store."""
        try:
            with open(self.path, encoding="utf-8") as handle:
                self._data = json.load(handle)
        except FileNotFoundError:
            self._data = {}
        except ValueError as exc:
            raise StoreError(f"{self.path} is not valid JSON: {exc}") from exc
        except OSError as exc:
            raise StoreError(str(exc)) from exc
        return self._data

    def save(self):
        """Write the whole store back."""
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(self._data, ensure_ascii=False))

    def get(self, key, default=None):
        """The value under *key*, else *default*."""
        if key in self._data:
            return self._data[key]
        return default

    def put(self, key, value):
        self._data[key] = value

    def delete(self, key):
        """Remove *key*."""
        self._data.pop(key, None)

    def items(self):
        """A copy of the store's contents."""
        return copy.deepcopy(self._data)

    def __len__(self):
        return len(self._data)
