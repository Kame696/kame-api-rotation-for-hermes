"""What the classifier was asked, and how much of it it could answer.

The plugin declines by design, and that makes a working install and an inert
one look identical: both are quiet. This counter is the only thing that tells
them apart without going and reading the source of a provider's latest error
payload — which is how the last two of these were found, both times late.

Two properties matter more than the arithmetic. It must hold no error text,
because the payload behind every count may be an unredacted provider dump.
And it must never be the reason a failing call fails worse.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_tally_under_test"


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
tally = importlib.import_module(f"{PACKAGE}.core.tally")


class TestCounting:
    def test_it_separates_what_was_sized_from_what_was_declined(self):
        counter = tally.Tally()
        counter.note("gemini", 429, sized=True)
        counter.note("gemini", 429, sized=False)
        counter.note("gemini", 429, sized=False)
        row = counter.snapshot()[0]
        assert (row.total, row.sized, row.declined) == (3, 1, 2)

    def test_a_provider_and_a_status_are_different_rows(self):
        counter = tally.Tally()
        counter.note("gemini", 429, sized=True)
        counter.note("gemini", 401, sized=False)
        counter.note("openai", 429, sized=True)
        assert len(counter.snapshot()) == 3

    def test_the_busiest_row_comes_first(self):
        counter = tally.Tally()
        counter.note("openai", 401, sized=False)
        for _ in range(4):
            counter.note("gemini", 429, sized=True)
        assert counter.snapshot()[0].provider == "gemini"

    def test_provider_names_are_compared_the_way_the_rest_of_the_plugin_does(self):
        counter = tally.Tally()
        counter.note("Gemini", 429, sized=True)
        counter.note(" gemini ", 429, sized=False)
        assert len(counter.snapshot()) == 1

    def test_a_failure_with_no_status_is_still_counted(self):
        # A transport error that never reached HTTP. Dropping it would hide
        # exactly the case where a provider is unreachable rather than busy.
        counter = tally.Tally()
        counter.note("gemini", None, sized=False)
        counter.note("gemini", "not a number", sized=False)
        rows = counter.snapshot()
        assert len(rows) == 1
        assert rows[0].status_code is None and rows[0].total == 2

    def test_nothing_seen_is_an_empty_answer_not_a_zero_row(self):
        assert tally.Tally().snapshot() == []


class TestPointingAtTheOneThatMatters:
    def test_a_rate_limit_nothing_could_size_is_flagged(self):
        counter = tally.Tally()
        counter.note("gemini", 429, sized=False)
        assert counter.snapshot()[0].worth_pointing_at is True

    def test_one_reading_is_enough_to_stop_flagging_it(self):
        # The claim is "KAME cannot read this provider's waits at all". A
        # single successful reading refutes it, and a partial count is not
        # evidence of anything: providers mix throttles with depletions.
        counter = tally.Tally()
        for _ in range(9):
            counter.note("gemini", 429, sized=False)
        counter.note("gemini", 429, sized=True)
        assert counter.snapshot()[0].worth_pointing_at is False

    def test_declining_every_auth_failure_is_not_flagged(self):
        # Declining a 401 is the plugin working: the host classifies those
        # and overriding it with a guess is worse than silence. Flagging it
        # would make the normal state look like a fault.
        counter = tally.Tally()
        for _ in range(20):
            counter.note("anthropic", 401, sized=False)
        assert counter.snapshot()[0].worth_pointing_at is False


class TestStayingSmallAndSafe:
    def test_it_does_not_grow_without_a_bound(self):
        counter = tally.Tally(max_rows=4)
        for index in range(50):
            counter.note(f"provider-{index}", 429, sized=False)
        assert len(counter.snapshot()) <= 4

    def test_at_the_ceiling_it_starts_over_rather_than_reporting_a_part(self):
        # Evicting one row would leave a total that is not a total, and the
        # section is read as "everything since Hermes started".
        counter = tally.Tally(max_rows=2)
        counter.note("a", 429, sized=False)
        counter.note("b", 429, sized=False)
        counter.note("c", 429, sized=False)
        assert [row.provider for row in counter.snapshot()] == ["c"]

    def test_the_caller_cannot_reach_into_live_state(self):
        counter = tally.Tally()
        counter.note("gemini", 429, sized=True)
        taken = counter.snapshot()
        taken.clear()
        assert len(counter.snapshot()) == 1

    def test_counting_from_many_threads_loses_nothing(self):
        counter = tally.Tally()
        threads = [
            threading.Thread(
                target=lambda: [counter.note("gemini", 429, sized=False) for _ in range(200)]
            )
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert counter.snapshot()[0].total == 1600

    def test_it_holds_nothing_but_numbers(self):
        # The hook payload behind every one of these carries error_message and
        # error_body, which the contract warns may be an unredacted dump. This
        # is the assertion that keeps a "just for debugging" field out.
        counter = tally.Tally()
        counter.note("gemini", 429, sized=False)
        stored = repr(counter._rows)
        assert "gemini" in stored
        assert all(
            isinstance(value, int)
            for row in counter._rows.values()
            for value in row
        )
