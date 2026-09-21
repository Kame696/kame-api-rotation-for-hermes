"""1.7.0.3 — a millisecond is not a minute, and one package needs one parser.

The owner ran his own pool through 1.7.0.2 for ninety minutes on 07/09/2026,
483 calls. The release did what it promised — 207 daily labels cost a 300s
re-probe instead of an hour — and then five keys vanished for the rest of the
day, and only `/kame clear_pool` brought them back:

    14:31:00  key da0c7b  gemini-3.8-flash  rest 40983s = 11.4h
    14:41:00  key 65f97d  gemini-3.8-flash  rest 40926s = 11.4h
    14:47:01  key ebad1f  gemini-3.8-flash  rest 15135s =  4.2h
    15:00:00  key 0a7709  gemini-3.8-flash  rest 44677s = 12.4h
    15:05:01  key 2c604b  gemini-3.8-flash  rest 43442s = 12.1h

None of those numbers was stated by anybody. Each is the provider's hint read
in the wrong unit:

    "Please retry in 683.050353ms."   683.050353 x 60 = 40983.02
    "Please retry in 252.247733ms."   252.247733 x 60 = 15134.86

`_RETRY_HINT` listed its units as `(seconds?|secs?|s|minutes?|mins?|m)` — no
`ms` branch at all — so on `683.050353ms` the alternation took `m`, left the
`s` behind, and handed back a unit spelled `m`. The consumer's guard read
`if unit.startswith("m") and not unit.startswith("ms")`, which looks exactly
like the fix for this and could never once have fired: the regex it was
guarding had no way to produce the string it tested for.

Two things made this expensive rather than merely wrong.

First, Google sends the millisecond spelling precisely when the wait is under
a second — so the *shortest* hints of the session became the *longest* benches
of the day, and the two smallest, 252ms and 683ms, cost the most.

Second, 1.7.0.2 is what put this parser on the daily path. Before it, a
`source == "window"` daily verdict was sized `max(delay, 3600)` and the hint
never reached `mark()`; after it, the branch asks `carousel.extract_delay` for
the provider's own number on purpose. The regression is the previous release's,
and the parser bug is older than both.

`core.quota.parse_duration_to_seconds` had been reading these correctly the
whole time. Two parsers in one package, disagreeing by a factor of 60000, and
which one ran depended on the branch — so the second one is gone.

Measured blast radius over every real refusal on disk: 8 of 386 retry hints are
in milliseconds. Rare, and each one costs a key for most of a day.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1_7_0_3_under_test"


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
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
quota_mod = importlib.import_module(f"{PACKAGE}.core.quota")
Carousel = carousel_mod.Carousel

ID = "gemini:gemini-3.8-flash"
NOW = 1_000_000.0


# --- 1. the five sentences that cost the owner five keys ---------------------

#: Verbatim from `refusals.jsonl`, 07/09/2026, with the bench 1.7.0.2 applied
#: and the number the provider actually named.
THE_FIVE = [
    ("Please retry in 683.050353ms.", 40983.0, 0.683050353),
    ("Please retry in 682.104641ms.", 40926.0, 0.682104641),
    ("Please retry in 252.247733ms.", 15135.0, 0.252247733),
    ("Please retry in 744.616994ms.", 44677.0, 0.744616994),
    ("Please retry in 724.036730ms.", 43442.0, 0.724036730),
]


@pytest.mark.parametrize("sentence,what_it_cost,what_it_said", THE_FIVE)
def test_a_millisecond_hint_is_read_as_a_millisecond(
    sentence, what_it_cost, what_it_said
):
    """The exact sentences, and the exact benches they used to buy."""
    read = carousel_mod.extract_delay(None, sentence, {})
    assert read == pytest.approx(what_it_said, abs=1e-6)
    # And nowhere near what it cost. Stated as its own assertion because the
    # approx above would still pass if someone reintroduced the x60 with a
    # compensating divide somewhere else.
    assert read < 1.0
    # The arithmetic of the defect, pinned so the story cannot rot: the old
    # reader took the digits as MINUTES and multiplied by 60, which against a
    # number that was really milliseconds is a factor of sixty thousand.
    assert what_it_cost == pytest.approx(what_it_said * 60_000.0, rel=1e-4), (
        "the old bench really was this sentence's digits read as minutes"
    )


def test_the_worst_case_is_a_bench_not_a_day():
    """End to end: the 683ms sentence through `mark()`, as dispatch runs it.

    `dispatch_binding` hands `extract_delay`'s answer to `mark()` with
    `stated=True` when the verdict's own number was KAME's default. Under
    1.7.0.2 that arrived as 40983 and `_escalate` served it whole, because a
    stated number at or above the daily cooldown is exactly the case the daily
    branch defers to. The guard is upstream: the number has to be right.
    """
    engine = Carousel(daily_cooldown_s=3600.0)
    stated = carousel_mod.extract_delay(None, "Please retry in 683.050353ms.", {})
    applied = engine.mark(
        ID, "k0", False, stated, "daily", now=NOW, stated=bool(stated)
    )
    assert applied == carousel_mod.RL_BACKOFF_CAP_S
    assert applied < 3600.0


# --- 2. one package, one answer to "what is a millisecond" -------------------

#: Every spelling worth having an opinion about, and what it means in seconds.
UNITS = [
    ("Please retry in 900ms.", 0.9),
    ("Please retry in 900 ms.", 0.9),
    ("Please retry in 900msec.", 0.9),
    ("Please retry in 900 milliseconds.", 0.9),
    ("Please retry in 45.610682767s.", 45.610682767),
    ("Please retry in 11s.", 11.0),
    ("Please retry in 11 seconds.", 11.0),
    ("Please retry in 2m.", 120.0),
    ("Please retry in 2 min.", 120.0),
    ("Please retry in 30 minutes.", 1800.0),
    ("retry-after: 90", 90.0),
]


@pytest.mark.parametrize("sentence,seconds", UNITS)
def test_both_readers_agree_on_every_unit(sentence, seconds):
    """`carousel` and `quota` must not disagree about a unit.

    They disagreed by a factor of 60000 for every release up to this one, and
    which reader ran depended on which branch the verdict took — so the same
    refusal was worth 0.68 seconds or 11.4 hours depending on a code path the
    provider knows nothing about. This is the test that says they are one
    parser now.
    """
    from_carousel = carousel_mod.extract_delay(None, sentence, {})
    from_quota = quota_mod.extract_from_text(sentence)[0]
    assert from_carousel == pytest.approx(seconds, rel=1e-9)
    assert from_quota == pytest.approx(seconds, rel=1e-9)


def test_the_two_parsers_are_one_parser():
    """Not behaviour — structure. The duplicate is gone, not merely corrected.

    A second parser that happens to agree today is a second parser that can
    drift tomorrow, and this one drifted for forty-odd releases without a
    single test noticing.
    """
    assert not hasattr(carousel_mod, "_DURATION"), (
        "carousel's own duration regex should be gone, not fixed in place"
    )
    assert carousel_mod._quota_parse_duration is quota_mod.parse_duration_to_seconds
    assert carousel_mod._UNIT_SECONDS is quota_mod.UNIT_SECONDS


@pytest.mark.parametrize(
    "text,seconds",
    [("6m 11.52s", 371.52), ("2h 30m", 9000.0), ("1h", 3600.0), ("90", 90.0)],
)
def test_the_compound_forms_still_read(text, seconds):
    """The formats the old regex existed to handle, kept under the new one."""
    assert carousel_mod.parse_duration(text) == pytest.approx(seconds)


def test_the_bound_survived_the_move():
    """`parse_duration` keeps its own cap after delegating the parsing.

    Bounding is this module's rule about what it will bench for, not part of
    reading a number, and quota's parser does not apply it. Losing it in the
    move would be silent: nothing else in `extract_delay`'s attribute branch
    re-checks.
    """
    assert carousel_mod.parse_duration("100h") is None
    assert carousel_mod.parse_duration("23h") == pytest.approx(82800.0)


# --- 3. the guard that could never fire --------------------------------------


def test_a_sub_second_hint_never_outranks_the_reprobe():
    """The property the five keys needed, stated without reference to units.

    Whatever a provider writes, a hint the plugin reads as *shorter than a
    minute* must never produce a bench *longer than an hour*. If a future
    parser change reintroduces a unit slip, this fails even if nobody thought
    to add that spelling to the table above.
    """
    for sentence, _cost, said in THE_FIVE:
        read = carousel_mod.extract_delay(None, sentence, {})
        assert read is not None
        engine = Carousel(daily_cooldown_s=3600.0)
        applied = engine.mark(ID, "k0", False, read, "daily", now=NOW, stated=True)
        assert applied <= 3600.0, (
            f"{sentence!r} read as {read}s bought a {applied}s bench"
        )
        assert said < 1.0
