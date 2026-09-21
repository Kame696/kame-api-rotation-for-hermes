"""What happens when a bench turns out to be wrong.

v0.0.7 gave the plugin a way to test its own predictions: when a model has no
usable credential left, one KAME-written bench is offered back on a widening
schedule. It asked the question and then threw the answer away. A probe that
*failed* re-registered the bench and widened the backoff, which is right; a
probe that *succeeded* changed nothing at all, so the key was withheld again
on the very next selection and tested again five minutes later, for as long as
the wrong deadline lasted.

That is the whole subject here. A deadline in the ledger was reasoned out of
an error message; none of them was ever measured. When one is contradicted by
an actual successful call, the contradiction wins — and it has to win
permanently, or the escape hatch is a way to leak one call every few minutes
rather than a way to recover.

Three rules, and the third is what keeps the first two from being reckless:

* a success on a pair refutes that pair's bench, for good;
* a success anywhere on a key refutes an *account-wide* claim about that key,
  but only its reach — the model that actually hit the limit was never
  retried, so its own deadline stands;
* nothing else moves a bench. A refusal never shortens one that is already
  standing, and a success on a pair nobody was asking about writes nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation")
)

from core import report  # noqa: E402
from core.ledger import (  # noqa: E402
    MAX_BENCHES,
    SCOPE_ACCOUNT,
    SCOPE_PER_MODEL,
    SCOPE_UNKNOWN,
    Bench,
    Ledger,
)
from core.reconcile import (  # noqa: E402
    HOLD,
    RELEASE,
    STATUS_EXHAUSTED,
    EntryView,
    plan,
)

NOW = 1_000_000.0
HOUR = 3600.0
DAY = 86400.0
MAIN = "gemini-3.6-flash"
AUX = "gemini-3.5-flash-lite"


def a_ledger(*rows, scope=SCOPE_UNKNOWN):
    """A ledger holding one bench per ``(credential, model, seconds)`` row."""
    ledger = Ledger()
    for credential_id, model, seconds in rows:
        ledger.record(
            credential_id=credential_id,
            provider="gemini",
            model=model,
            reset_at=NOW + seconds,
            now=NOW,
            scope=scope,
        )
    return ledger


class TestASuccessSettlesIt:
    def test_a_working_key_is_no_longer_spent_here(self):
        ledger = a_ledger(("k0", MAIN, DAY))
        assert ledger.is_spent_for("k0", MAIN, NOW) is True

        refutation = ledger.note_success("k0", MAIN, NOW + 300)
        assert refutation.refuted is not None
        assert ledger.is_spent_for("k0", MAIN, NOW + 300) is False

    def test_it_stays_settled_for_the_rest_of_the_deadline(self):
        # The failure this replaces: the key came back, worked, and was
        # withheld again on the next selection because nothing recorded it.
        ledger = a_ledger(("k0", MAIN, DAY))
        ledger.note_success("k0", MAIN, NOW + 300)
        for later in (NOW + 301, NOW + 3600, NOW + DAY - 1):
            assert ledger.is_spent_for("k0", MAIN, later) is False

    def test_the_row_is_kept_because_it_is_the_proof_of_ownership(self):
        # Deleting it would strand the key: the host is still holding a
        # cooldown, and the deadline in this row is the only thing that proves
        # the cooldown is KAME's to unwind.
        ledger = a_ledger(("k0", MAIN, DAY))
        ledger.note_success("k0", MAIN, NOW + 300)
        bench = ledger.find("k0", MAIN)
        assert bench is not None
        assert bench.reset_at == NOW + DAY
        assert bench.is_refuted is True
        assert bench.holds(NOW + 300) is False

    def test_a_success_on_a_pair_nobody_asked_about_writes_nothing(self):
        ledger = a_ledger(("k0", MAIN, DAY))
        assert not ledger.note_success("k0", AUX, NOW + 300)
        assert not ledger.note_success("k9", MAIN, NOW + 300)
        assert ledger.is_spent_for("k0", MAIN, NOW + 300) is True

    def test_a_success_after_the_bench_lapsed_settles_nothing(self):
        # There was no standing claim to disprove, so there is nothing to
        # write down and no reason to touch the disk.
        ledger = a_ledger(("k0", MAIN, 60))
        assert not ledger.note_success("k0", MAIN, NOW + 120)

    def test_an_unusable_success_is_ignored(self):
        ledger = a_ledger(("k0", MAIN, DAY))
        assert not ledger.note_success("", MAIN, NOW)
        assert not ledger.note_success("k0", "", NOW)
        assert not ledger.note_success("k0", MAIN, float("nan"))
        assert ledger.is_spent_for("k0", MAIN, NOW + 1) is True


class TestHowFarOneSuccessReaches:
    def test_a_key_wide_claim_loses_its_reach(self):
        # "Every model on this key is spent" — and one of them just answered.
        ledger = a_ledger(("k0", MAIN, DAY), scope=SCOPE_ACCOUNT)
        assert ledger.spent_until("k0", AUX, NOW) == NOW + DAY

        refutation = ledger.note_success("k0", AUX, NOW + 300)
        assert [b.model for b in refutation.narrowed] == [MAIN]
        assert ledger.spent_until("k0", AUX, NOW + 300) is None

    def test_but_the_model_that_actually_hit_the_limit_still_waits(self):
        # It was never retried. Nothing about it was tested, so nothing about
        # it is known to be wrong.
        ledger = a_ledger(("k0", MAIN, DAY), scope=SCOPE_ACCOUNT)
        ledger.note_success("k0", AUX, NOW + 300)
        assert ledger.is_spent_for("k0", MAIN, NOW + 300) is True
        assert ledger.find("k0", MAIN).scope == SCOPE_PER_MODEL

    def test_a_success_on_the_model_that_earned_it_settles_the_whole_row(self):
        ledger = a_ledger(("k0", MAIN, DAY), scope=SCOPE_ACCOUNT)
        ledger.note_success("k0", MAIN, NOW + 300)
        assert ledger.spent_until("k0", MAIN, NOW + 300) is None
        assert ledger.spent_until("k0", AUX, NOW + 300) is None
        assert ledger.find("k0", MAIN).covers_every_model is False

    def test_one_key_answering_says_nothing_about_another(self):
        ledger = a_ledger(("k0", MAIN, DAY), ("k1", MAIN, DAY), scope=SCOPE_ACCOUNT)
        ledger.note_success("k0", AUX, NOW + 300)
        assert ledger.spent_until("k1", AUX, NOW + 300) == NOW + DAY


class TestABenchNeverShrinks:
    def test_a_shorter_refusal_does_not_undo_a_longer_one(self):
        # Reachable through the escape hatch: a key benched until midnight is
        # probed, fails again, and the provider answers with a per-minute
        # complaint. The daily counter is still spent; the smaller truth must
        # not overwrite the larger one.
        ledger = a_ledger(("k0", MAIN, DAY))
        ledger.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + 360, now=NOW + 300,
        )
        assert ledger.benched_until("k0", MAIN, NOW + 300) == NOW + DAY

    def test_a_deadline_already_in_the_past_is_not_a_refusal_at_all(self):
        # It reconciles to a no-op whichever way it is read, so it never gets
        # as far as the comparison above.
        ledger = a_ledger(("k0", MAIN, DAY))
        assert ledger.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + 60, now=NOW + 300,
        ) is None
        assert ledger.benched_until("k0", MAIN, NOW + 300) == NOW + DAY

    def test_a_longer_refusal_still_extends(self):
        ledger = a_ledger(("k0", MAIN, HOUR))
        ledger.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + DAY, now=NOW + 300,
        )
        assert ledger.benched_until("k0", MAIN, NOW + 300) == NOW + DAY

    def test_a_refusal_after_a_success_opens_a_new_episode(self):
        # The refuted deadline was disproved; re-applying it would be applying
        # a number the key itself contradicted.
        ledger = a_ledger(("k0", MAIN, DAY))
        ledger.note_success("k0", MAIN, NOW + 300)
        ledger.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + 360, now=NOW + 301,
        )
        bench = ledger.find("k0", MAIN)
        assert bench.reset_at == NOW + 360
        assert bench.is_refuted is False

    def test_the_new_episode_starts_its_backoff_from_scratch(self):
        # Probe counts widen the schedule. Carrying them across a proven
        # recovery would start the next lockout at half an hour.
        ledger = a_ledger(("k0", MAIN, DAY))
        ledger.note_probe("k0", MAIN, NOW + 300)
        ledger.note_probe("k0", MAIN, NOW + 900)
        ledger.note_success("k0", MAIN, NOW + 901)
        ledger.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + 1200, now=NOW + 902,
        )
        assert ledger.find("k0", MAIN).probes == 0

    def test_a_failed_probe_still_keeps_its_count(self):
        # The v0.0.7 rule, unchanged: without it the backoff never widens.
        ledger = a_ledger(("k0", MAIN, DAY))
        ledger.note_probe("k0", MAIN, NOW + 300)
        ledger.record(
            credential_id="k0", provider="gemini", model=MAIN,
            reset_at=NOW + DAY, now=NOW + 301,
        )
        assert ledger.find("k0", MAIN).probes == 1


class TestWhatThePoolIsToldAfterwards:
    def _entry(self, reset_at):
        return EntryView("k0", STATUS_EXHAUSTED, reset_at)

    def test_a_refuted_bench_hands_the_key_back(self):
        ledger = a_ledger(("k0", MAIN, DAY))
        ledger.note_success("k0", MAIN, NOW + 300)
        actions = plan([self._entry(NOW + DAY)], ledger, model=MAIN, now=NOW + 300)
        assert [a.kind for a in actions] == [RELEASE]
        assert actions[0].why == f"tested on {MAIN} and it worked"

    def test_and_keeps_handing_it_back_on_every_selection(self):
        # The host's cooldown is untouched — the plugin writes nothing to the
        # pool — so the release has to be re-decided each time it is asked.
        ledger = a_ledger(("k0", MAIN, DAY))
        ledger.note_success("k0", MAIN, NOW + 300)
        for later in (NOW + 301, NOW + HOUR, NOW + DAY - 1):
            actions = plan([self._entry(NOW + DAY)], ledger, model=MAIN, now=later)
            assert [a.kind for a in actions] == [RELEASE]

    def test_the_return_leg_no_longer_re_holds_it(self):
        # Once the host's own cooldown lapses the entry comes back available.
        # Before this version the ledger would have withheld it again.
        ledger = a_ledger(("k0", MAIN, DAY))
        ledger.note_success("k0", MAIN, NOW + 300)
        assert plan([EntryView("k0")], ledger, model=MAIN, now=NOW + 300) == []

    def test_an_untested_bench_is_still_a_bench(self):
        ledger = a_ledger(("k0", MAIN, DAY))
        actions = plan([EntryView("k0")], ledger, model=MAIN, now=NOW + 300)
        assert [a.kind for a in actions] == [HOLD]

    def test_a_key_wide_claim_that_was_disproved_stops_covering_other_models(self):
        ledger = a_ledger(("k0", MAIN, DAY), scope=SCOPE_ACCOUNT)
        ledger.note_success("k0", AUX, NOW + 300)
        assert plan([EntryView("k0")], ledger, model=AUX, now=NOW + 300) == []
        # ...and the model that earned it is still held.
        assert [a.kind for a in plan([EntryView("k0")], ledger, model=MAIN, now=NOW + 300)] == [
            HOLD
        ]

    def test_a_lapsed_cooldown_is_held_even_with_a_stale_status(self):
        # The host counts an entry usable the moment its deadline passes,
        # whether or not anything has cleared the status yet. Reading the
        # stale status as "still benched" sent the decision down the ownership
        # branch and skipped the hold — handing back a key the ledger knows is
        # spent here.
        ledger = a_ledger(("k0", MAIN, DAY))
        stale = EntryView("k0", STATUS_EXHAUSTED, NOW + 60)
        actions = plan([stale], ledger, model=MAIN, now=NOW + 120)
        assert [(a.kind, a.reset_at) for a in actions] == [(HOLD, NOW + DAY)]

    def test_somebody_else_s_bench_is_still_never_touched(self):
        ledger = a_ledger(("k0", MAIN, DAY))
        ledger.note_success("k0", MAIN, NOW + 300)
        # The host's deadline matches nothing this ledger wrote.
        assert plan([self._entry(NOW + 12345)], ledger, model=MAIN, now=NOW + 300) == []


class TestPersistenceAndReporting:
    def test_a_refutation_survives_a_restart(self):
        ledger = a_ledger(("k0", MAIN, DAY))
        ledger.note_success("k0", MAIN, NOW + 300)
        restored = Ledger.from_dict(ledger.to_dict())
        assert restored.find("k0", MAIN).is_refuted is True
        assert restored.is_spent_for("k0", MAIN, NOW + 300) is False

    def test_a_row_written_before_refutations_existed_reads_as_untested(self):
        ledger = Ledger.from_dict(
            {
                "version": 1,
                "benches": [
                    {
                        "credential_id": "k0",
                        "provider": "gemini",
                        "model": MAIN,
                        "reset_at": NOW + DAY,
                        "recorded_at": NOW,
                    }
                ],
            }
        )
        assert ledger.find("k0", MAIN).is_refuted is False
        assert ledger.is_spent_for("k0", MAIN, NOW) is True

    def test_a_corrupt_marker_reads_as_untested(self):
        ledger = Ledger.from_dict(
            {
                "version": 1,
                "benches": [
                    {
                        "credential_id": "k0",
                        "provider": "gemini",
                        "model": MAIN,
                        "reset_at": NOW + DAY,
                        "recorded_at": NOW,
                        "refuted_at": "yesterday",
                    }
                ],
            }
        )
        assert ledger.find("k0", MAIN).is_refuted is False

    def test_refuted_rows_are_evicted_first_when_the_cap_bites(self):
        # They withhold nothing, so losing one costs at most a release that
        # the host's own clock would have made anyway.
        ledger = Ledger()
        for index in range(MAX_BENCHES):
            ledger.record(
                credential_id="k0", provider="gemini", model=f"model-{index}",
                reset_at=NOW + DAY, now=NOW,
            )
        ledger.note_success("k0", "model-0", NOW + 1)
        ledger.record(
            credential_id="k0", provider="gemini", model="model-new",
            reset_at=NOW + 10, now=NOW + 2,
        )
        assert ledger.find("k0", "model-0") is None
        assert ledger.find("k0", "model-1") is not None

    def test_the_report_does_not_claim_a_working_key_is_waiting(self):
        ledger = a_ledger(("k0", MAIN, DAY))
        ledger.note_success("k0", MAIN, NOW + 300)
        lines = report.render_benches(ledger, now=NOW + 300)
        rendered = "\n".join(lines)
        assert "tested and it worked" in rendered
        assert "free in" not in rendered
