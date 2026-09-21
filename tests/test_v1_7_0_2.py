"""1.7.0.2 — a daily label is evidence, not proof, and the pool can tell.

Every number in this file was measured on the owner's own keys on 2026-09-06
and 2026-09-07 (`audit/10-fase0-os-dois-portos-no-mesmo-banco.md`). Nothing here
is hypothetical, and where a case is synthetic it says so.

The measurement that produced this release, in one table:

    interval between two 200s on a key ALREADY labelled PerDay
      samples   21
      min        6 min
      median    16 min
      max       36 min
      >= 1 hour  0 of 21

    longest silence of the WHOLE pool while the model still had capacity
      3.8-flash  15 min
      3.7-flash  14 min
      3.6-flash  12 min

So: one key refusing proves nothing about the day, and the pool going quiet
proves rather a lot. This release moves the decision from the first to the
second.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1_7_0_2_under_test"


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

# The real shape, from `refusals.jsonl` on 06/09: Google names the day and
# attaches a retry hint that is the seconds left in the current clock minute.
GOOGLE_DAILY_HINT_S = 45.0

# OpenRouter names a real one. `test_binding.py` holds this contract already.
OPENROUTER_DAY_S = 9 * 3600.0


def engine() -> Carousel:
    return Carousel(daily_cooldown_s=3600.0)


# --- 1. the label alone no longer buys the hour ------------------------------


class TestTheFirstDailyLabelIsNotTheHour:
    def test_the_provider_hint_is_discarded_because_it_is_a_clock(self):
        """The 45s is not obeyed, and this is the one place that is right.

        Of the 114 PerDay refusals recorded on 06/09, 107 carried a hint equal
        to the seconds left in the current clock minute — 85 exact to the
        second. A field counting down to the top of the minute says nothing
        about a daily counter, so there is no stated number for this condition
        and the flat re-probe applies.
        """
        eng = engine()
        applied = eng.mark(
            ID, "a", False, GOOGLE_DAILY_HINT_S, "daily", now=NOW, stated=True
        )
        assert applied == pytest.approx(carousel_mod.RL_BACKOFF_CAP_S)
        assert applied > GOOGLE_DAILY_HINT_S, "a day is not thirty seconds"
        assert applied < eng.daily_cooldown_s, "and it is not an hour either"

    def test_a_daily_label_with_no_number_rests_the_same_reprobe(self):
        """Same rest whether the hint was there or not — it was never used."""
        eng = engine()
        applied = eng.mark(ID, "a", False, 0.0, "daily", now=NOW)
        assert applied == pytest.approx(carousel_mod.RL_BACKOFF_CAP_S)

    def test_the_whole_pool_labelled_at_once_comes_back_in_minutes(self):
        """The 18:26 sweep: fourteen keys, eight seconds, all labelled PerDay.

        Before this release every one of them rested 3600s and the owner had
        to reset the pool by hand — twice in one session.
        """
        eng = engine()
        keys = ["k%d" % i for i in range(14)]
        rests = [
            eng.mark(ID, k, False, GOOGLE_DAILY_HINT_S, "daily",
                     now=NOW + i, stated=True)
            for i, k in enumerate(keys)
        ]
        assert max(rests) == pytest.approx(carousel_mod.RL_BACKOFF_CAP_S)
        assert eng.healthy_count(ID, keys, now=NOW) == 0
        back = NOW + carousel_mod.RL_BACKOFF_CAP_S + 20.0
        assert eng.healthy_count(ID, keys, now=back) == 14


# --- 2. a silent pool does buy the hour --------------------------------------


class TestASilentPoolIsTheProof:
    def test_the_hour_arrives_once_the_pool_has_been_quiet_long_enough(self):
        """No key answered for the whole window, so believe the label.

        The threshold is measured, not chosen: the longest the owner's pool
        ever went without a single answer while the model still had capacity
        was 15 minutes, across three models.
        """
        eng = engine()
        eng.mark(ID, "a", False, GOOGLE_DAILY_HINT_S, "daily", now=NOW, stated=True)
        later = NOW + carousel_mod.POOL_SILENCE_BEFORE_THE_DAY_S + 1.0
        applied = eng.mark(
            ID, "a", False, GOOGLE_DAILY_HINT_S, "daily", now=later, stated=True
        )
        assert applied == pytest.approx(eng.daily_cooldown_s)

    def test_one_answer_anywhere_in_the_pool_starts_the_clock_over(self):
        """A different key answering is proof the day is not over.

        This is the same reasoning as `thaw_server_cooled`, which the owner
        wrote for 5xx: when one credential gets through, the *provider* is
        serving, and what the others are resting on is not what they think.
        """
        eng = engine()
        eng.mark(ID, "a", False, GOOGLE_DAILY_HINT_S, "daily", now=NOW, stated=True)
        eng.mark(ID, "b", True, now=NOW + 60.0)
        later = NOW + carousel_mod.POOL_SILENCE_BEFORE_THE_DAY_S + 1.0
        applied = eng.mark(
            ID, "a", False, GOOGLE_DAILY_HINT_S, "daily", now=later, stated=True
        )
        assert applied == pytest.approx(carousel_mod.RL_BACKOFF_CAP_S)

    def test_the_doubt_is_per_identity_not_per_key(self):
        eng = engine()
        eng.mark(ID, "a", False, 0.0, "daily", now=NOW)
        other = "gemini:gemini-3.7-flash"
        later = NOW + carousel_mod.POOL_SILENCE_BEFORE_THE_DAY_S + 1.0
        # A different model has its own day and its own silence.
        assert eng.mark(other, "a", False, 0.0, "daily", now=later) == pytest.approx(
            carousel_mod.RL_BACKOFF_CAP_S
        )


# --- 3. a genuinely stated long deadline still wins --------------------------


class TestAStatedDeadlineOutranksAll:
    """1.8.0.0 (``PLAN_1.8.0.0.md`` G8, settled 2026-09-15) narrows this title.

    A stated deadline still outranks anything this module invents — that part
    of 1.7.0.2 is untouched, and ``test_a_shorter_stated_number_still_wins``
    below pins it. What it no longer outranks is the owner's own ceiling: G8
    is written to replace exactly the "9h served whole" case these two tests
    used to assert, because a credential held for nine hours on one stated
    number is a pool that cannot spread load onto it for those nine hours,
    silently, on the strength of a single provider's word. Past the ceiling
    the key returns to selection and is probed instead.
    """

    def test_openrouter_nine_hours_is_honoured_up_to_the_ceiling(self):
        """The contract `test_binding.py` already holds, narrowed on purpose.

        OpenRouter states a real reset, and this module still believes it —
        `test_an_unstated_number_never_reaches_this_far` below shows what
        happens without one. It just cannot believe it past the owner's own
        ceiling any more, default one hour, which is exactly what replaces
        the eight hours of pointless probing the old docstring here warned
        against: an hour of probing costs one request, not eight.
        """
        eng = engine()
        applied = eng.mark(
            ID, "a", False, OPENROUTER_DAY_S, "daily", now=NOW, stated=True
        )
        assert applied == pytest.approx(eng.max_hold_s)

    def test_a_stated_deadline_is_bounded_even_after_a_silent_pool(self):
        eng = engine()
        eng.mark(ID, "a", False, 0.0, "daily", now=NOW)
        later = NOW + carousel_mod.POOL_SILENCE_BEFORE_THE_DAY_S + 1.0
        applied = eng.mark(
            ID, "a", False, OPENROUTER_DAY_S, "daily", now=later, stated=True
        )
        assert applied == pytest.approx(eng.max_hold_s)

    def test_the_owner_can_still_raise_the_ceiling_past_a_days_deadline(self):
        # The ceiling is the owner's dial, not a second hard-coded cap: set it
        # back to (or past) what 1.7.0.2 always allowed and the full nine
        # hours is honoured exactly as it was before G8.
        eng = Carousel(daily_cooldown_s=3600.0, max_hold_s=carousel_mod.HARD_DELAY_CAP_S)
        applied = eng.mark(
            ID, "a", False, OPENROUTER_DAY_S, "daily", now=NOW, stated=True
        )
        assert applied == pytest.approx(OPENROUTER_DAY_S)


# --- 4. insufficient_quota keeps its own meaning ------------------------------


class TestInsufficientQuotaIsNotADailyLabel:
    def test_an_account_out_of_credit_still_rests_the_hour(self):
        """`insufficient_quota` is money, not a window.

        No amount of pool liveness makes a key with no credit work, so this
        one does not get the short probe. Kept separate deliberately: the two
        shared a branch before and the sharing is what this release un-does.
        """
        eng = engine()
        applied = eng.mark(ID, "a", False, 0.0, "insufficient_quota", now=NOW)
        assert applied == pytest.approx(eng.daily_cooldown_s)


# --- 5. the constant 1.7.0.1 added and never fired ----------------------------


def test_the_strike_counter_is_gone():
    """`DAILY_STRIKES_BEFORE_THE_HOUR` was dead code from the day it shipped.

    It sized the probe with `max(delay, DAILY_BASE_S)`, and `dispatch_binding`
    had already replaced `delay` with 3600 upstream, so `max(3600, 20)` was
    3600 on every one of the owner's 114 daily refusals. It also contradicted
    a written decision. Removed rather than repaired.
    """
    assert not hasattr(carousel_mod, "DAILY_STRIKES_BEFORE_THE_HOUR")


# --- 6. the spinner told two different times ---------------------------------


def test_a_changed_line_is_not_held_back_by_the_new_cadence():
    """The photograph: `next key in 0s` beside a bar reading `58m 43s`.

    A wait that ends draws its last frame at the one-second cadence a near-zero
    countdown deserves. If the pool then refuses at once and an hour-long wait
    opens, the new frame asks for a 30-second cadence — and the old guard
    compared *that* against the one second since the last draw and suppressed
    it. The stale `0s` stayed on screen while the truth was 58 minutes.
    """
    dispatch = importlib.import_module(f"{PACKAGE}.dispatch_binding")
    spinner = dispatch._Spinner
    spinner.reset()
    said = []

    class Agent:
        session_id = "spinner-transition"
        _emit_wait_notice = staticmethod(said.append)

    agent = Agent()
    # The last frame of a wait that just ended, drawn at the fast cadence.
    spinner.update(agent, "next key in 0s", interval=spinner.cadence_for(0.0))
    # Eleven seconds later — the age of the owner's screenshot.
    key = spinner.key_for(agent)
    at, text = spinner._state[key]
    spinner._state[key] = (at - 11.0, text)
    # A fresh hour-long wait opens and asks for the slow cadence.
    spinner.update(agent, "next key in 58m 43s", interval=spinner.cadence_for(3523.0))

    assert said == ["next key in 0s", "next key in 58m 43s"], (
        "the correction has to reach the screen; suppressing it is how the "
        "chat and the status bar came to disagree"
    )


def test_the_same_line_is_still_throttled():
    """The guard the fix must not remove: identical text stays rationed."""
    dispatch = importlib.import_module(f"{PACKAGE}.dispatch_binding")
    spinner = dispatch._Spinner
    spinner.reset()
    said = []

    class Agent:
        session_id = "spinner-repeat"
        _emit_wait_notice = staticmethod(said.append)

    agent = Agent()
    spinner.update(agent, "same line", interval=1.0)
    for _ in range(20):
        spinner.update(agent, "same line", interval=30.0)
    assert said == ["same line"]


def test_a_changed_line_still_has_a_floor():
    """A burst inside one second is a flood, changed text or not."""
    dispatch = importlib.import_module(f"{PACKAGE}.dispatch_binding")
    spinner = dispatch._Spinner
    spinner.reset()
    said = []

    class Agent:
        session_id = "spinner-burst"
        _emit_wait_notice = staticmethod(said.append)

    agent = Agent()
    for i in range(20):
        spinner.update(agent, "line %d" % i, interval=30.0)
    assert said == ["line 0"]


# --- 7. the timing field nobody ever filled ----------------------------------


def _record_calls(source: str):
    """Every `timings.record(...)` block in a module, balanced on parentheses."""
    blocks = []
    marker = "timings.record("
    at = source.find(marker)
    while at != -1:
        i = at + len(marker)
        depth = 1
        while i < len(source) and depth:
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
            i += 1
        blocks.append(source[at:i])
        at = source.find(marker, i)
    return blocks


def test_every_failure_records_the_rest_it_cost():
    """`rest_s` was declared, documented, and left `null` 2,458 times.

    `timings` has carried the field since it shipped and no caller passed it,
    so the one number that explains a stall — how long this refusal cost the
    key — could only be recovered by opening `state.json` and reading the
    event ring by hand. That is the position I was in when the owner asked
    what had benched his pool.
    """
    import inspect

    dispatch = importlib.import_module(f"{PACKAGE}.dispatch_binding")
    blocks = _record_calls(inspect.getsource(dispatch))
    assert len(blocks) == 2, "one lane for the refusal, one for the answer"

    failure = next(b for b in blocks if "outcome=verdict" in b)
    answered = next(b for b in blocks if 'outcome="answered"' in b)

    assert "rest_s=" in failure, "the failure lane is the one with a rest"
    assert "rest_s=" not in answered, (
        "an answered attempt benched nothing, and writing a rest there would "
        "be inventing one"
    )


def test_a_pool_reset_clears_the_daily_doubt():
    """`clear_pool` says "as if it had never been tried". It has to mean it.

    The owner reset his pool five times in the session of 06/09, every time to
    escape a bench the label had bought. A reset that left the doubt standing
    would hand the very next refusal an hour again.
    """
    eng = engine()
    eng.mark(ID, "a", False, 0.0, "daily", now=NOW)
    eng.forget()
    later = NOW + carousel_mod.POOL_SILENCE_BEFORE_THE_DAY_S + 1.0
    assert eng.mark(ID, "a", False, 0.0, "daily", now=later) == pytest.approx(
        carousel_mod.RL_BACKOFF_CAP_S
    )


# --- 8. the suite must never write into the owner's logs again ---------------


def test_the_suite_cannot_reach_the_installed_state_directory():
    """The guard in `conftest.py`, held by a test so it cannot quietly lapse.

    Twice now this suite has filled the owner's evidence with synthetic rows —
    1,131 attempts on 2026-09-07 alone — and the second time cost the raw body
    of the one refusal he had asked me to explain. Switching the two recorders
    off was not enough: 5 of 45 modules wrote anyway, because several of them
    turn a recorder on deliberately in order to test it.

    So the *destination* is what moved. This test proves it moved.
    """
    from . import conftest

    state = importlib.import_module(f"{PACKAGE}.state")
    where = state.state_dir()
    assert where is not None, "the plugin still needs somewhere to write"
    assert str(conftest.SANDBOX_HOME) in str(where), (
        f"tests would write to {where} — that is the owner's live install"
    )
    assert r"AppData\Local\hermes\plugin-data" not in str(where)


def test_the_event_names_the_source_the_number_came_from():
    """`sized_by` must not credit `window` for a number this branch discarded.

    `quota` writes `source = "window"` to mean *this is my own default, nobody
    stated it*. 1.7.0.2 throws that default away and lets the carousel re-probe,
    so an event still reading `window` would name a source the rest did not come
    from — the same small lie `mark` refuses to tell when it returns what was
    stored rather than what was asked for.
    """
    import inspect

    dispatch = importlib.import_module(f"{PACKAGE}.dispatch_binding")
    source = inspect.getsource(dispatch)
    assert '"reprobe" if window_number_declined' in source
    panel = (PLUGIN_DIR / "desktop" / "plugin.js").read_text(encoding="utf-8")
    assert "reprobe:" in panel, "the panel has to have a word for it too"
