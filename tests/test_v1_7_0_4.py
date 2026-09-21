"""1.7.0.4 — a 5xx is not metered, so it never escalates.

The owner's rule, and the arithmetic behind it is his: **a 503 costs no
quota.** Nothing is spent by asking again, so a longer rest buys nothing at
all — it only holds back a credential that was never at fault.

What turned a preference into a defect is that the ladder climbed on a
*healthy* pool. `consecutive_server` is per key, and only that key's own
success clears it. `thaw_server_cooled` shortens the deadline but deliberately
does not touch the counter. So:

    key A gets a 503        -> strikes 1, rests 5s
    key B answers           -> thaw pulls A forward, strikes still 1
    key A gets a 503        -> strikes 2, rests 10s   <- the pool is fine

Measured on the owner's own evidence, every 503 that cost more than the 5s
base:

    escalated while another key had answered in the last 120s   7
    escalated with nothing answering (the case the ladder is for) 9

The worst of the seven: 07/09/2026 14:13:26, key 6e98e0 rested **40 seconds**
while key 65f97d was answering normally. Forty seconds of a healthy key, for a
blip that belonged to the model.

That is the wrong question being asked. A 5xx is the *model's* condition — both
ports have said so since the 1.0.2-era fix that stopped a 503 body mentioning
"quota" from buying an hour — and a counter asking "how many times has this
key seen one" is asking about the credential.

### What this costs, stated rather than discovered later

The ladder had a real job, written down in the Agent Zero port when it was
added: on a large pool the lap is faster than a flat 5s rest, so the pool never
goes fully cold, `_wait_for_recovery` never sleeps, and the carousel turns for
as long as the outage lasts. An 83-minute Gemini outage is on record.

The owner made that trade knowingly: it spends no quota, the storm-collapse
keeps the log readable, and `decisions/0002-eternal-carousel-no-timeout` says
the carousel turning is what this plugin is for. Recorded here so that a later
reader finds the cost beside the choice.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1_7_0_4_under_test"


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
Carousel = carousel_mod.Carousel

ID = "gemini:gemini-3.8-flash"
NOW = 1_000_000.0


# --- 1. it does not climb, however many times it is asked --------------------


@pytest.mark.parametrize("repeats", [2, 3, 6, 12, 40])
def test_a_5xx_costs_the_same_however_often_it_repeats(repeats):
    """The old ladder reached its 90s cap on the fifth strike."""
    engine = Carousel()
    rests = [
        engine.mark(ID, "a", False, 0.0, "server", now=NOW) for _ in range(repeats)
    ]
    assert rests == [carousel_mod.SERVER_BASE_S] * repeats


def test_the_strike_counter_still_counts_because_the_thaw_reads_it():
    """Structure, not behaviour: removing the counter would break the thaw.

    ``thaw_server_cooled`` uses ``consecutive_server > 0`` to tell a key
    benched by somebody else's outage from a key benched by its own quota.
    Stopping the increment because nothing sizes a rest from it any more would
    silently stop the pool snapping back after an outage — and the test that
    catches that is in ``test_carousel.py``, a file away from this change.
    """
    engine = Carousel()
    engine.mark(ID, "a", False, 5.0, "server", now=NOW)
    assert engine._pools[ID]["a"]["consecutive_server"] == 1
    engine.mark(ID, "a", False, 5.0, "server", now=NOW)
    assert engine._pools[ID]["a"]["consecutive_server"] == 2


# --- 2. the exact shape that cost a healthy key forty seconds ----------------


def test_a_blip_on_one_key_while_the_pool_answers_never_escalates():
    """07/09/2026 14:13:26 reconstructed: 6e98e0 blips, 65f97d keeps serving.

    Four unlucky minutes on one key, spread across a session in which another
    key answers between each one. The old ladder read those four as a streak —
    5, 10, 20, **40** — because a neighbour's success clears the deadline
    through the thaw but never the strike count. Forty seconds of a healthy
    key, for a condition that belonged to the model.

    The interleaved successes are the whole point of the reconstruction: they
    are what makes the pool demonstrably fine while the counter climbs.
    """
    engine = Carousel()
    rests = []
    for n in range(4):
        at = NOW + n * 60.0
        engine.mark(ID, "65f97d", True, now=at)
        engine.thaw_server_cooled(ID, "65f97d", now=at)
        rests.append(engine.mark(ID, "6e98e0", False, 0.0, "server", now=at + 1.0))

    assert rests == [carousel_mod.SERVER_BASE_S] * 4
    # What the same four refusals used to cost, spelled out so the number in
    # the docstring cannot drift away from the arithmetic that produced it.
    old_ladder = [5.0 * (2 ** n) for n in range(4)]   # the ladder as it was
    assert old_ladder[-1] == 40.0
    assert sum(rests) < sum(old_ladder)


def test_the_whole_pool_blipping_costs_each_key_the_same_base():
    """The sustained-outage case, which is now handled by rotating, not resting.

    Every key refuses, every key rests the base, and the pool is usable again
    a second later. That is the trade this release makes: no key is
    punished for the model's bad minute, and the carousel keeps turning
    through an outage rather than sleeping through it.
    """
    keys = [f"k{n}" for n in range(14)]
    engine = Carousel()
    for _ in range(3):
        for key in keys:
            assert (
                engine.mark(ID, key, False, 0.0, "server", now=NOW)
                == carousel_mod.SERVER_BASE_S
            )
    assert engine.select(ID, keys, now=NOW + carousel_mod.SERVER_BASE_S + 0.1)[1] == "SUCCESS"


# --- 3. what a provider states still outranks us -----------------------------


def test_a_stated_wait_on_a_5xx_is_obeyed():
    """The rule the whole module follows. A 5xx does not become an exception."""
    engine = Carousel()
    assert engine.mark(ID, "a", False, 45.0, "server", now=NOW, stated=True) == 45.0


def test_a_stated_wait_on_a_5xx_is_still_bounded():
    """A server error must not be able to bench a key past the owner's ceiling.

    This is all the cap does now, and it is why it was kept rather than
    deleted along with the ladder that used to reach it.

    1.8.0.0 (``PLAN_1.8.0.0.md`` G8): the bound that actually bites by default
    tightened from ``HARD_DELAY_CAP_S`` (a day, fixed) to ``max_hold_s`` (an
    hour by default, the owner's own dial via ``settings.MAX_HOLD``). A
    stated wait still outranks anything this module invents; it still cannot
    outrank the owner's ceiling.
    """
    engine = Carousel()
    applied = engine.mark(ID, "a", False, 7200.0, "server", now=NOW, stated=True)
    assert applied == engine.max_hold_s
    assert engine.mark(ID, "b", False, 1e9, "server", now=NOW, stated=True) == engine.max_hold_s


def test_an_unstated_delay_larger_than_the_base_still_wins():
    """``max(delay, base)``, not ``base``. The classifier's number is not lost.

    Worth pinning because the obvious way to write a flat rest is `return
    SERVER_BASE_S`, and that would throw away a delay the cascade had already
    sized from a header nobody re-reads down here.
    """
    engine = Carousel()
    assert engine.mark(ID, "a", False, 12.0, "server", now=NOW) == 12.0


# --- 4. the ladder is gone from the module, not merely unreachable -----------


def test_no_kind_but_a_refused_credential_still_climbs():
    """One ladder left in ``_escalate``, and it is a counter, not a clock.

    ``auth`` / ``denied`` / ``revoked`` grow because growing is how the pool
    counts to "this credential is not coming back" before retiring it. If a
    later release adds a climbing rest back to a *quota* or *server* kind, this
    fails.
    """
    engine = Carousel()

    for kind in ("server", "rate_limit", "daily"):
        fresh = Carousel(daily_cooldown_s=3600.0)
        rests = [
            fresh.mark(ID, "a", False, 0.0, kind, now=NOW) for _ in range(4)
        ]
        assert len(set(rests)) == 1, f"{kind} climbed: {rests}"

    climbing = [engine.mark(ID, "b", False, 0.0, "auth", now=NOW) for _ in range(3)]
    assert climbing == sorted(climbing)
    assert len(set(climbing)) == 3


# --- 5. the base is one second, and it is not a new number -------------------


def test_the_server_base_is_the_modules_own_spin_floor():
    """``SERVER_BASE_S`` is ``RL_BASE_S``, and this is what keeps them equal.

    1.7.0.4 took the base from 5s to 1s, and the justification is that it is
    not a new number: ``RL_BASE_S`` is already defined as "the smallest rest
    that is a cooldown rather than a spin", already reasoned about, already
    tested. Reusing it is the same move as routing the daily re-probe through
    ``RL_BACKOFF_CAP_S`` in 1.7.0.2 instead of inventing a fresh constant.

    If a later release moves one of them, this fails, and whoever moved it has
    to say which of the two rules changed.
    """
    assert carousel_mod.SERVER_BASE_S == carousel_mod.RL_BASE_S == 1.0


def test_the_five_second_rest_was_never_reachable_on_a_real_pool():
    """Why lowering it was safe: on fourteen keys the old base never bound.

    Measured over 59 real 503 episodes in the owner's evidence, the gap
    between a key's 503 and that same key being offered again was **never
    below 7.2 seconds** — the carousel has thirteen others to try first. The
    base only binds when the lap is shorter than the rest, so this builds the
    pool where it does bind: one key, nothing else to rotate to.
    """
    engine = Carousel()
    engine.mark(ID, "sole", False, 0.0, "server", now=NOW)

    # A pool of fourteen: the key is not wanted again for a whole lap anyway,
    # so the base is invisible either way.
    keys = [f"k{n}" for n in range(14)]
    big = Carousel()
    big.mark(ID, keys[0], False, 0.0, "server", now=NOW)
    assert big.select(ID, keys, now=NOW + 0.1)[1] == "SUCCESS"

    # A pool of one: the base is the entire wait, and it is now a second
    # rather than five.
    assert engine.select(ID, ["sole"], now=NOW + 0.5)[1] == "EXHAUSTED"
    assert engine.select(ID, ["sole"], now=NOW + 1.1)[1] == "SUCCESS"
