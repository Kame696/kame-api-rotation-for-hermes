"""1.8.4.1 (K1) - one credential is one credential.

1.8.4.0 (F10) cut a single resolved credential that holds a separator (``12|<secret>``,
``Bearer <secret>``) down to the piece the parser keeps, and with a pool row holding the whole value
the piece became a second, phantom key. 1.8.3.1 sent such a value whole. Now:

* no separator: the value;
* a separator and two or more keys: those keys;
* a separator and exactly one key, the rest being only separators, whitespace, quotes or brackets:
  that key;
* a separator and no key, the rest being only those characters: nothing;
* anything else: the value whole, as Hermes resolved it.

Needs no Hermes tree: the pool read is stubbed; no key is a real one (40 fake characters).
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_onecred_1841_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
facade = importlib.import_module(f"{PACKAGE}.facade")

K1 = "AIzaSyONECRED1" + "0" * 26      # 40 characters
K2 = "AIzaSyONECRED2" + "0" * 26


def keys_of(value: str, pool=None):
    pool_keys = list(pool or [])
    facade._pool_keys = lambda provider, base_url="": (list(pool_keys), {k: None for k in pool_keys})
    return facade._KeySource("gemini", value).keys()


@pytest.fixture(autouse=True)
def restore_pool_read():
    original = facade._pool_keys
    yield
    facade._pool_keys = original


@pytest.mark.parametrize("value", ["12|" + K1, "Bearer " + K1, "not needed", K1 + ",short", K1 + " | short", "short,tiny"])
def test_a_credential_that_is_not_a_list_is_kept_whole(value):
    assert keys_of(value) == [value]


@pytest.mark.parametrize("value", [K1 + ",", K1 + "," + K1, " " + K1 + " \n", '"' + K1 + '",'])
def test_a_key_with_only_noise_around_it_is_that_key(value):
    assert keys_of(value) == [K1]


def test_a_list_of_two_keys_gives_both():
    assert keys_of(K1 + "," + K2) == [K1, K2]
    assert keys_of(K1 + " " + K2) == [K1, K2]


def test_only_separators_give_nothing():
    assert keys_of(",,") == []
    assert keys_of(" ; | ") == []


def test_no_separator_is_the_value_as_it_is():
    assert keys_of(K1) == [K1]
    assert keys_of("shortkey") == ["shortkey"]
    assert keys_of("") == []


def test_a_pool_row_holding_the_whole_value_gives_no_second_key():
    value = "12|" + K1
    assert keys_of(value, pool=[value]) == [value]
    value = "Bearer " + K1
    assert keys_of(value, pool=[value]) == [value]


def test_a_value_with_no_separator_is_not_looked_at():
    quoted = '"' + K1 + '"'
    assert keys_of(quoted) == [quoted]      # as in 1.8.3.1 and 1.8.4.0: the quotes stay
