"""Holding a key longer than the provider's own answer said to.

Every number this plugin produces is read off a provider — except one. When a
key is handed back the instant its cooldown lapses and refused again within
minutes, twice in a row, the deadline was too short and there is no other
reading of that sequence. From v0.1.0 the next bench on that exact key, model
and window is widened to match.

The risk runs the other way from everything else here: a bench that is too
long costs a healthy key its time, silently, and nothing fails to reveal it.
So the rules below are all about *not* firing — a single repeat, a repeat on a
different window, a repeat on another key, a depletion no wait can fix, a
number that has already hit the ceiling.

The counterweight is v0.0.9. A widened bench is still a prediction: the
escape hatch tests it when a model has nothing else usable, and one clean call
retires it for good. Escalation was built after refutation and not before,
because the reverse order is how a plugin that sizes cooldowns turns into a
plugin that hides keys.

The last section is the one that pays for the new field. ``reset_at`` stays
exactly what the host stored — it is the fingerprint — and the longer hold
lives in ``extended_to``. Merging them would have bought a longer bench at the
cost of the per-model release this whole plugin exists for.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation")
)

from core import escalate  # noqa: E402
from core.journal import (  # noqa: E402
    UNDER_PREDICTION_GRACE_SECONDS,
    Journal,
    short_streak,
)
from core.ledger import Ledger  # noqa: E402
from core.probe import eligible  # noqa: E402
from core.quota import QuotaWindow  # noqa: E402
from core.reconcile import (  # noqa: E402
    HOLD,
    RELEASE,
    STATUS_EXHAUSTED,
    EntryView,
    plan,
)

NOW = 1_000_000.0
MINUTE = 60.0
HOUR = 3600.0
DAY = 86400.0
MAIN = "gemini-3.6-flash"
AUX = "gemini-3.5-flash-lite"


def a_journal(*rows, credential_id="k0", model=MAIN, window="per_minute"):
    """A journal holding one block per ``(at, held_for)`` row.

    ``held_for`` is how long that refusal benched the key, so the next row's
    ``at`` decides whether the deadline was measured short.
    """
    book = Journal()
    for at, held_for in rows:
        book.record_block(
            at=at,
            provider="gemini",
            model=model,
            credential_id=credential_id,
            window=window,
            reset_at=None if held_for is None else at + held_for,
        )
    return book


class TestTheArithmetic:
    def test_one_strike_changes_nothing(self):
        assert escalate.factor_for(1) == 1.0
        assert escalate.stretch(reset_at=NOW + MINUTE, now=NOW, strikes=1) is None

    def test_two_strikes_double_it(self):
        assert escalate.factor_for(2) == 2.0
        assert escalate.stretch(reset_at=NOW + MINUTE, now=NOW, strikes=2) == NOW + 2 * MINUTE

    def test_it_keeps_doubling_and_then_stops(self):
        assert escalate.factor_for(3) == 4.0
        assert escalate.factor_for(4) == 8.0
        assert escalate.factor_for(9) == escalate.MAX_FACTOR

    def test_a_widened_bench_never_outlasts_a_day(self):
        # The longest bench anything else in this plugin produces. A number
        # KAME learned should not be able to out-hold one it read.
        stretched = escalate.stretch(reset_at=NOW + 12 * HOUR, now=NOW, strikes=4)
        assert stretched == NOW + escalate.MAX_HOLD_SECONDS

    def test_a_bench_already_at_the_ceiling_is_left_alone(self):
        assert escalate.stretch(reset_at=NOW + DAY, now=NOW, strikes=4) is None

    def test_a_depletion_is_not_a_timing_problem(self):
        # Out of credits does not become in-credit by waiting longer, so the
        # only thing a stretch buys here is a key sitting out for nothing.
        for reason in ("insufficient_quota", "billing_hard_limit", "invalid_api_key"):
            assert escalate.stretch(
                reset_at=NOW + HOUR, now=NOW, strikes=4, reason=reason
            ) is None

    def test_an_account_window_is_not_widened(self):
        assert escalate.stretch(
            reset_at=NOW + HOUR, now=NOW, strikes=4, window=QuotaWindow.ACCOUNT
        ) is None

    def test_a_deadline_already_past_is_not_a_bench_to_widen(self):
        assert escalate.stretch(reset_at=NOW - 1, now=NOW, strikes=4) is None

    def test_rubbish_in_is_declined_not_guessed(self):
        assert escalate.stretch(reset_at=None, now=NOW, strikes=4) is None
        assert escalate.stretch(reset_at=float("nan"), now=NOW, strikes=4) is None


class TestCountingWhatWasMeasured:
    """``short_streak`` counts the refusal in flight as the newest link."""

    def test_nothing_recorded_is_no_evidence(self):
        assert short_streak(
            Journal(), credential_id="k0", model=MAIN, window="per_minute", at=NOW
        ) == 0

    def test_a_repeat_right_after_the_deadline_counts(self):
        book = a_journal((NOW, MINUTE))
        assert short_streak(
            book, credential_id="k0", model=MAIN, window="per_minute", at=NOW + MINUTE + 1
        ) == 1

    def test_two_in_a_row_is_the_threshold(self):
        book = a_journal((NOW, MINUTE), (NOW + MINUTE + 1, MINUTE))
        streak = short_streak(
            book,
            credential_id="k0",
            model=MAIN,
            window="per_minute",
            at=NOW + 2 * MINUTE + 2,
        )
        assert streak == 2
        assert streak >= escalate.STRIKES_BEFORE_STRETCHING

    def test_a_refusal_at_an_ordinary_time_breaks_the_run(self):
        # This is what makes the widening self-clearing: no decay to tune, no
        # timer to expire. One refusal that is not a repeat resets it.
        book = a_journal((NOW, MINUTE), (NOW + MINUTE + 1, MINUTE))
        assert short_streak(
            book,
            credential_id="k0",
            model=MAIN,
            window="per_minute",
            at=NOW + 2 * MINUTE + UNDER_PREDICTION_GRACE_SECONDS + 10,
        ) == 0

    def test_the_run_stops_where_the_window_changes(self):
        # A per-minute throttle proving short says nothing about a daily cap.
        book = a_journal((NOW, MINUTE))
        assert short_streak(
            book, credential_id="k0", model=MAIN, window="per_day", at=NOW + MINUTE + 1
        ) == 0

    def test_another_key_is_not_charged_for_this_ones_tuition(self):
        book = a_journal((NOW, MINUTE))
        assert short_streak(
            book, credential_id="k1", model=MAIN, window="per_minute", at=NOW + MINUTE + 1
        ) == 0

    def test_another_model_is_a_different_question(self):
        book = a_journal((NOW, MINUTE))
        assert short_streak(
            book, credential_id="k0", model=AUX, window="per_minute", at=NOW + MINUTE + 1
        ) == 0

    def test_a_block_with_no_deadline_proves_nothing(self):
        # The host's default TTL. Nothing here knows when that key was handed
        # back, so no repeat can be attributed to it.
        book = a_journal((NOW, None))
        assert short_streak(
            book, credential_id="k0", model=MAIN, window="per_minute", at=NOW + MINUTE
        ) == 0

    def test_a_refusal_before_the_deadline_is_not_a_repeat(self):
        # Some other key's turn, or a second failure inside the same burst.
        # The key was still benched, so nothing was measured.
        book = a_journal((NOW, HOUR))
        assert short_streak(
            book, credential_id="k0", model=MAIN, window="per_minute", at=NOW + MINUTE
        ) == 0

    def test_a_record_from_the_future_is_not_a_chain(self):
        book = a_journal((NOW + DAY, MINUTE))
        assert short_streak(
            book, credential_id="k0", model=MAIN, window="per_minute", at=NOW
        ) == 0


class TestTwoDeadlinesOnOneBench:
    """``reset_at`` is what the host holds; ``until`` is what KAME holds."""

    def _extended(self, *, seconds=MINUTE, to=2 * MINUTE):
        ledger = Ledger()
        ledger.record(
            credential_id="k0",
            provider="gemini",
            model=MAIN,
            reset_at=NOW + seconds,
            now=NOW,
            extend_to=NOW + to,
        )
        return ledger

    def test_the_key_is_withheld_for_the_longer_one(self):
        ledger = self._extended()
        assert ledger.benched_until("k0", MAIN, NOW + MINUTE + 1) == NOW + 2 * MINUTE
        assert ledger.is_spent_for("k0", MAIN, NOW + MINUTE + 1) is True

    def test_the_fingerprint_is_still_the_hosts_number(self):
        bench = self._extended().find("k0", MAIN)
        assert bench.reset_at == NOW + MINUTE
        assert bench.until == NOW + 2 * MINUTE
        assert bench.is_extended is True

    def test_the_row_outlives_the_hosts_deadline(self):
        ledger = self._extended()
        ledger.prune(NOW + MINUTE + 1)
        assert ledger.find("k0", MAIN) is not None

    def test_an_extension_shorter_than_the_bench_is_not_one(self):
        ledger = self._extended(seconds=HOUR, to=MINUTE)
        bench = ledger.find("k0", MAIN)
        assert bench.extended_to == 0.0
        assert bench.is_extended is False
        assert bench.until == NOW + HOUR

    def test_it_survives_being_written_down(self):
        restored = Ledger.from_dict(self._extended().to_dict())
        bench = restored.find("k0", MAIN)
        assert bench.extended_to == NOW + 2 * MINUTE
        assert bench.until == NOW + 2 * MINUTE

    def test_a_success_ends_the_extension_too(self):
        # A widened deadline is still a prediction, and it loses to an
        # observation exactly like the one it was widened from.
        ledger = self._extended()
        ledger.note_success("k0", MAIN, NOW + MINUTE + 1)
        assert ledger.is_spent_for("k0", MAIN, NOW + MINUTE + 1) is False

    def test_the_extension_is_testable(self):
        # Long enough to be worth a probe, and the probe policy has to see the
        # length KAME is really holding, not the host's shorter number.
        ledger = Ledger()
        ledger.record(
            credential_id="k0",
            provider="gemini",
            model=MAIN,
            reset_at=NOW + 2 * MINUTE,
            now=NOW,
            extend_to=NOW + HOUR,
        )
        assert eligible(ledger.find("k0", MAIN), now=NOW + 5 * MINUTE) is True


class TestWhatTheExtensionCosts:
    """The per-model release has to keep working while KAME holds longer."""

    def _entry(self, reset_at):
        return EntryView(credential_id="k0", status=STATUS_EXHAUSTED, reset_at=reset_at)

    def test_another_model_is_released_while_the_host_still_holds_it(self):
        # The reason ``extended_to`` is a field of its own. If the longer
        # deadline had been written over ``reset_at``, the host's cooldown
        # would match nothing in the ledger, KAME would read the bench as
        # somebody else's, and the key would be locked out of every other
        # model for as long as the host held it.
        ledger = Ledger()
        ledger.record(
            credential_id="k0",
            provider="gemini",
            model=MAIN,
            reset_at=NOW + MINUTE,
            now=NOW,
            extend_to=NOW + HOUR,
        )
        actions = plan([self._entry(NOW + MINUTE)], ledger, model=AUX, now=NOW + 1)
        assert [(a.kind, a.credential_id) for a in actions] == [(RELEASE, "k0")]

    def test_the_model_that_spent_it_is_held_past_the_hosts_deadline(self):
        ledger = Ledger()
        ledger.record(
            credential_id="k0",
            provider="gemini",
            model=MAIN,
            reset_at=NOW + MINUTE,
            now=NOW,
            extend_to=NOW + HOUR,
        )
        # The host's cooldown has lapsed and it counts the key usable again.
        actions = plan([self._entry(NOW + MINUTE)], ledger, model=MAIN, now=NOW + MINUTE + 1)
        assert [(a.kind, a.reset_at) for a in actions] == [(HOLD, NOW + HOUR)]


class TestANewRefusalOnTopOfAStandingBench:
    """v0.0.9 promised a bench never shortens. v0.1.0 keeps that promise
    without paying for it with the fingerprint."""

    def test_a_shorter_refusal_does_not_free_a_key_held_for_the_day(self):
        ledger = Ledger()
        ledger.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + DAY, now=NOW,
        )
        ledger.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + 360, now=NOW + 300,
        )
        assert ledger.benched_until("k0", MAIN, NOW + 400) == NOW + DAY

    def test_the_fingerprint_follows_the_hosts_latest_number(self):
        # The host is holding the *new* deadline, so that is the one an
        # ownership check has to match. Before v0.1.0 the carried deadline was
        # written over it and the per-model release stopped working.
        ledger = Ledger()
        ledger.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + DAY, now=NOW,
        )
        ledger.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + 360, now=NOW + 300,
        )
        bench = ledger.find("k0", MAIN)
        assert bench.reset_at == NOW + 360
        assert bench.until == NOW + DAY

        actions = plan(
            [EntryView(credential_id="k0", status=STATUS_EXHAUSTED, reset_at=NOW + 360)],
            ledger,
            model=AUX,
            now=NOW + 310,
        )
        assert [(a.kind, a.credential_id) for a in actions] == [(RELEASE, "k0")]

    def test_a_refuted_bench_carries_nothing_forward(self):
        # The pair worked in between, so this is a new episode. Carrying the
        # old deadline would re-apply a number already disproved.
        ledger = Ledger()
        ledger.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + DAY, now=NOW,
        )
        ledger.note_success("k0", MAIN, NOW + 60)
        ledger.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + 400, now=NOW + 300,
        )
        assert ledger.benched_until("k0", MAIN, NOW + 310) == NOW + 400


class TestAKeyThatWorkedInBetween:
    """A deadline is only "measured short" if the key never worked meanwhile.

    v0.1.0 read the sequence off the blocks alone: refused, deadline, refused
    again within minutes. That reading is right only when the key spent the
    whole stretch on the bench. If it answered a call in between — because a
    probe released it early, or because it simply worked for a while after
    being handed back — then the second refusal is a fresh limit being hit,
    not proof that the first deadline was too short. Charging it as a strike
    would widen a deadline on a coincidence, and on a small pool the two
    situations are common, not exotic: refutation exists precisely to hand
    keys back early.

    The suppression leans safe on purpose. The recovery it reads comes from
    the best-effort selection mirror, so it can be missed — and a missed one
    only leaves v0.1.0's behaviour, while a spurious one merely declines to
    widen. Evidence is required to hold a key longer; nothing is required to
    stop holding it.
    """

    @staticmethod
    def _book(*, worked_at=None, window="per_minute"):
        """Two refusals that would be a run, and maybe a success between them."""
        book = Journal()
        book.record_block(
            at=NOW - 2000.0, provider="gemini", model=MAIN, credential_id="k0",
            window=window, reset_at=NOW - 1400.0,
        )
        book.record_block(
            at=NOW - 1399.0, provider="gemini", model=MAIN, credential_id="k0",
            window=window, reset_at=NOW - 799.0,
        )
        if worked_at is not None:
            book.record_success(
                at=worked_at, provider="gemini", model=MAIN, credential_id="k0",
            )
        return book

    def _streak(self, book, *, at=NOW - 798.0, window="per_minute"):
        return short_streak(
            book, credential_id="k0", model=MAIN, window=window, at=at
        )

    def test_the_run_is_real_when_the_key_never_answered(self):
        # The control. Without this the rest of the class could pass by
        # measuring nothing at all.
        assert self._streak(self._book()) == 2

    def test_a_success_before_the_deadline_means_it_was_released_early(self):
        # A probe answered, so v0.0.9 put the key back in rotation. Whatever
        # the refusal at the old deadline means, it does not mean the deadline
        # was short — the key was never held to it.
        assert self._streak(self._book(worked_at=NOW - 1000.0)) == 0

    def test_a_success_after_the_deadline_means_it_simply_ran_out_again(self):
        # Handed back on time, worked, then hit the limit again inside the
        # grace window. That is the throttle doing its job, not a bad guess.
        assert self._streak(self._book(worked_at=NOW - 790.0), at=NOW - 700.0) == 0

    def test_an_older_success_does_not_break_a_later_run(self):
        # The recovery belongs to a cycle that closed before this run started.
        # Reading it as "the key worked in between" would make one good day
        # suppress every measurement after it.
        book = Journal()
        book.record_block(
            at=NOW - 5000.0, provider="gemini", model=MAIN, credential_id="k0",
            window="per_minute", reset_at=None,
        )
        book.record_success(
            at=NOW - 4000.0, provider="gemini", model=MAIN, credential_id="k0",
        )
        book.record_block(
            at=NOW - 2000.0, provider="gemini", model=MAIN, credential_id="k0",
            window="per_minute", reset_at=NOW - 1400.0,
        )
        book.record_block(
            at=NOW - 1399.0, provider="gemini", model=MAIN, credential_id="k0",
            window="per_minute", reset_at=NOW - 799.0,
        )
        assert self._streak(book) == 2

    def test_another_key_working_says_nothing_about_this_one(self):
        book = self._book()
        book.record_block(
            at=NOW - 1500.0, provider="gemini", model=MAIN, credential_id="k1",
            window="per_minute", reset_at=NOW - 1450.0,
        )
        book.record_success(
            at=NOW - 900.0, provider="gemini", model=MAIN, credential_id="k1",
        )
        assert self._streak(book) == 2

    def test_the_same_key_working_on_another_model_says_nothing_either(self):
        # Per-model benches are the whole plugin: a key answering on the
        # auxiliary model is the normal state while the main model holds it.
        book = self._book()
        book.record_block(
            at=NOW - 1500.0, provider="gemini", model=AUX, credential_id="k0",
            window="per_minute", reset_at=NOW - 1450.0,
        )
        book.record_success(
            at=NOW - 900.0, provider="gemini", model=AUX, credential_id="k0",
        )
        assert self._streak(book) == 2

    def test_the_report_counts_it_the_same_way(self):
        # The tally is looser than the rule that acts — it groups by provider,
        # model and window over a fortnight — but it must not claim an
        # under-prediction the widening itself would refuse to charge.
        #
        # Three recorded refusals, because ``summarize`` only sees what is
        # written down: there is no refusal in flight for it to count.
        from core.journal import summarize

        def three(worked_at=None):
            book = self._book(worked_at=None)
            if worked_at is not None:
                book.record_success(
                    at=worked_at, provider="gemini", model=MAIN, credential_id="k0",
                )
            book.record_block(
                at=NOW - 798.0, provider="gemini", model=MAIN, credential_id="k0",
                window="per_minute", reset_at=NOW - 198.0,
            )
            return book

        assert [stat.under_predictions for stat in summarize(three(), now=NOW)] == [2]

        # The key answered between the second refusal and the third, so only
        # the first repeat survives as evidence.
        excused = summarize(three(worked_at=NOW - 1000.0), now=NOW)
        assert [stat.under_predictions for stat in excused] == [1]


class TestAnAnchorIsMovedNotMultiplied:
    """Two kinds of deadline, two kinds of correction.

    A stopwatch — "come back in 21 seconds", "re-probe in an hour" — is a
    *length*, and a length measured short is scaled. An anchor — midnight
    US/Pacific, the instant a daily counter is believed to roll — is a
    *moment*, and a moment measured early is moved.

    This is not a stylistic preference. Scaling an anchor is a no-op on the
    case that matters most: a key refused just after midnight is benched until
    the *next* midnight, so the deadline is already a day out, and the 24-hour
    ceiling eats the entire multiplier. Until v0.1.2 the deadline in this
    plugin that most needed correcting was the only one escalation could not
    touch — and its failure repeats every single day, costing a full day of
    that key each time, because a rollover five minutes late is enough.
    """

    ANCHOR = "anchor"

    def test_the_case_doubling_could_not_reach(self):
        # Refused just after midnight: the next anchor is a day away.
        moved = escalate.stretch(
            reset_at=NOW + DAY, now=NOW, strikes=2,
            window="per_day", source=self.ANCHOR,
        )
        assert moved == NOW + DAY + 30 * MINUTE

    def test_the_nudge_grows_with_the_evidence(self):
        def moved(strikes):
            return escalate.stretch(
                reset_at=NOW + DAY, now=NOW, strikes=strikes,
                window="per_day", source=self.ANCHOR,
            )

        assert moved(2) == NOW + DAY + 30 * MINUTE
        assert moved(3) == NOW + DAY + HOUR
        assert moved(4) == NOW + DAY + 2 * HOUR

    def test_and_stops(self):
        # Past this the anchor is not slightly wrong, it is wrong, and holding
        # a key a third of a day past a deadline the provider gave is not a
        # correction any evidence here supports.
        assert escalate.nudge_for(9) == escalate.MAX_NUDGE_SECONDS
        assert escalate.stretch(
            reset_at=NOW + DAY, now=NOW, strikes=9,
            window="per_day", source=self.ANCHOR,
        ) == NOW + DAY + escalate.MAX_NUDGE_SECONDS

    def test_one_strike_moves_nothing(self):
        assert escalate.nudge_for(1) == 0.0
        assert escalate.stretch(
            reset_at=NOW + DAY, now=NOW, strikes=1,
            window="per_day", source=self.ANCHOR,
        ) is None

    def test_a_depletion_is_not_an_anchor_problem(self):
        assert escalate.stretch(
            reset_at=NOW + DAY, now=NOW, strikes=3,
            window="per_day", source=self.ANCHOR, reason="insufficient_quota",
        ) is None

    def test_an_account_window_is_never_moved_either(self):
        assert escalate.stretch(
            reset_at=NOW + DAY, now=NOW, strikes=3,
            window="account", source=self.ANCHOR,
        ) is None

    def test_a_stopwatch_still_scales(self):
        # The regression guard. A daily cap from a provider whose clock KAME
        # does not know is a one-hour re-probe wearing the same window name,
        # and scaling it towards a day is the right search.
        assert escalate.stretch(
            reset_at=NOW + HOUR, now=NOW, strikes=2,
            window="per_day", source="window",
        ) == NOW + 2 * HOUR
        assert escalate.stretch(
            reset_at=NOW + MINUTE, now=NOW, strikes=2,
            window="per_minute", source="headers",
        ) == NOW + 2 * MINUTE

    def test_the_anchor_is_moved_from_itself_not_from_now(self):
        # An anchor names an instant. Measuring the correction from the moment
        # of the refusal would drag the whole deadline around by however long
        # after the rollover the retry happened to land.
        late = NOW + 300.0
        assert escalate.stretch(
            reset_at=NOW + DAY, now=late, strikes=2,
            window="per_day", source=self.ANCHOR,
        ) == NOW + DAY + 30 * MINUTE

    def test_a_deadline_already_past_is_still_no_bench(self):
        assert escalate.stretch(
            reset_at=NOW - 1.0, now=NOW, strikes=3,
            window="per_day", source=self.ANCHOR,
        ) is None
