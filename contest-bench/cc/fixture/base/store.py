"""A tiny JSON key-value store used by the pipeline for step results."""

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
        except ValueError:
            self._data = {}
        except OSError as exc:
            raise StoreError(str(exc)) from exc
        return self._data

    def save(self):
        """Write the whole store back."""
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(self._data, ensure_ascii=False) + "\n")

    def get(self, key, default=None):
        """The value under *key*, else *default*."""
        return self._data.get(key) or default

    def put(self, key, value):
        self._data[key] = value

    def delete(self, key):
        """Remove *key*."""
        del self._data[key]

    def items(self):
        """A copy of the store's contents."""
        return dict(self._data)

    def __len__(self):
        return len(self._data)
