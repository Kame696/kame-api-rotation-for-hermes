"""``core.answer`` — the one success that proves nothing.

The rule under test is narrow on purpose. A call that returns is normally
proof that the key works, and this plugin acts on that proof by retiring a
bench for good. The exception is the answer with nothing in it, which is how a
squeezed free-tier key can fail without ever saying so.

Everything here is about which way to fail when the question cannot be
answered, because that is the direction that matters: reading an unknown as
"empty" would silence the release path for every provider at once.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_answer_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
answer = importlib.import_module(f"{PACKAGE}.core.answer")


class TestAnAnswerThatCarriedNothing:
    def test_no_text_and_no_tool_call_is_nothing(self):
        assert answer.carried_nothing(content_chars=0, tool_calls=0) is True

    def test_text_is_an_answer(self):
        assert answer.carried_nothing(content_chars=1, tool_calls=0) is False

    def test_a_tool_call_with_no_prose_is_a_full_answer(self):
        # The commonest shape of a working turn: the model chose to act
        # instead of talk. Reading this as empty would file most of a busy
        # agent's traffic as evidence of nothing.
        assert answer.carried_nothing(content_chars=0, tool_calls=1) is False

    def test_both_present_is_an_answer(self):
        assert answer.carried_nothing(content_chars=120, tool_calls=2) is False


class TestWhenTheHostSaysNothing:
    """A field that is missing is not a field that is zero."""

    def test_a_missing_character_count_is_not_an_empty_answer(self):
        # An older Hermes, or a dispatch site that does not pass the field.
        # Read as zero it would turn every successful call in the process into
        # an empty one and take the release path down with it.
        assert answer.carried_nothing(content_chars=None, tool_calls=0) is False

    def test_a_missing_tool_count_is_not_an_empty_answer(self):
        assert answer.carried_nothing(content_chars=0, tool_calls=None) is False

    def test_both_missing_is_not_an_empty_answer(self):
        assert answer.carried_nothing(content_chars=None, tool_calls=None) is False

    def test_a_value_that_is_not_a_number_says_nothing(self):
        assert answer.carried_nothing(content_chars="lots", tool_calls=0) is False

    def test_a_negative_count_says_nothing(self):
        # Not a count any host would send, and not one worth guessing at.
        assert answer.carried_nothing(content_chars=-1, tool_calls=0) is False

    def test_a_boolean_is_not_a_count(self):
        # ``False`` is ``0`` to ``int()``, and a host passing a flag where a
        # count belongs has not told us the answer was empty.
        assert answer.carried_nothing(content_chars=False, tool_calls=False) is False

    def test_a_numeric_string_is_still_a_number(self):
        # Hosts serialise. "0" is a reported zero, not a missing field.
        assert answer.carried_nothing(content_chars="0", tool_calls="0") is True

    def test_a_float_counts(self):
        assert answer.carried_nothing(content_chars=0.0, tool_calls=0.0) is True
