"""1.8.4.0 round 2 (review F10) - a line with a separator is never sent whole.

``_KeySource.keys`` adds the key Hermes resolved for the client to the pool's keys: a working
credential whether or not the pool knows it. When that value held a separator but parsed to a
single key (``"K1,"``, ``"K1,K1"``, ``"K1,short"``), the whole line was added as a key and sent to
the provider. Now a value with a separator adds the keys it parses to (the parser the pool uses),
and nothing when it parses to none; a value without a separator is kept as it is. The chat path uses
the same class, so this also protects the chat.

Needs no Hermes tree: the pool read is stubbed to an empty pool, no key is a real one (fake keys of
40 characters; KAME's parser drops tokens under 16).
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_keyline_1840_under_test"


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

K1 = "AIzaSyKEYLINE1" + "0" * 26      # 40 characters
K2 = "AIzaSyKEYLINE2" + "0" * 26


@pytest.fixture(autouse=True)
def an_empty_pool(monkeypatch):
    monkeypatch.setattr(facade, "_pool_keys", lambda provider, base_url="": ([], {}))


def keys_of(value: str):
    return facade._KeySource("gemini", value).keys()


@pytest.mark.parametrize("line", [K1 + ",", K1 + "," + K1, ";" + K1])
def test_a_line_with_a_separator_that_parses_to_one_key_sends_that_key_only(line):
    got = keys_of(line)
    assert got == [K1], got
    assert line not in got, "the raw line is never a key"


# 1.8.4.1 (K1): these two rows expected [K1]. A key with a word after it cannot be told from
# ``id|secret`` by its shape, and Hermes without KAME sends the same line, so it goes whole again.
@pytest.mark.parametrize("line", [K1 + ",short", K1 + " | short"])
def test_a_key_with_a_word_after_it_is_one_credential_sent_whole(line):
    assert keys_of(line) == [line]


def test_a_line_with_a_separator_that_parses_to_two_keys_sends_both():
    assert keys_of(K1 + "," + K2) == [K1, K2]
    assert keys_of(K1 + " " + K2) == [K1, K2]
    assert keys_of(K1 + "\n" + K2) == [K1, K2]


def test_a_line_with_a_separator_that_parses_to_no_key_adds_nothing():
    assert keys_of(",,,") == []


def test_a_made_up_key_with_a_separator_is_kept_whole():
    # 1.8.4.1 (K1): this row expected []. "short,tiny" has the shape of "not needed", the made-up key of
    # a local server; with nothing added the call would leave KAME (fallback list, continuation lost).
    assert keys_of("short," + "tiny") == ["short,tiny"]


def test_a_single_bare_key_is_unchanged():
    assert keys_of(K1) == [K1]


def test_a_key_that_is_short_but_has_no_separator_is_kept_as_it_is():
    # not KAME's call to drop a credential the host resolved: a value without a separator is kept
    assert keys_of("shortkey") == ["shortkey"]


def test_no_key_at_all_is_no_key():
    assert keys_of("") == []
