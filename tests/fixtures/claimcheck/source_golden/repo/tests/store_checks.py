"""Checks of lib.store, written like tests (named so pytest does not collect them)."""

import pytest

from lib.store import Store


@pytest.fixture
def tmp_store(tmp_path):
    return Store(tmp_path / "s.json")


def helper(value):
    return value * 2


@pytest.fixture(name="loaded")
def _loaded_store(tmp_store):
    tmp_store.load()
    return tmp_store


def test_get_default(loaded):
    assert loaded.get("missing", 3) == 3


def test_items_is_the_dict(tmp_store):
    assert tmp_store.items() is tmp_store._data
