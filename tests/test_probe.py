"""The escape hatch — when a prediction gets tested instead of trusted.

The rules here decide whether the user's agent sits out a deadline KAME
invented or gets one more try at it. Both answers are expensive when wrong,
in opposite ways, so each condition below is stated as its own test rather
than folded into a happy path.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_binding import PACKAGE  # noqa: E402

probe = importlib.import_module(f"{PACKAGE}.core.probe")
ledger_module = importlib.import_module(f"{PACKAGE}.core.ledger")

Bench = ledger_module.Bench
Ledger = ledger_module.Ledger

NOW = 1_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR


def bench(
    *,
    credential_id="k0",
    model="gemini-3.6-flash",
    reset_at=NOW + DAY,
    recorded_at=NOW,
    reason="rate_limit",
    probes=0,
    last_probe_at=0.0,
):
    return Bench(
        credential_id=credential_id,
        provider="gemini",
        model=model,
        reset_at=reset_at,
        recorded_at=recorded_at,
        reason=reason,
        probes=probes,
        last_probe_at=last_probe_at,
    )


class TestTheSchedule:
    def test_it_widens_and_then_stops_widening(self):
        assert probe.interval_for(0) == 300.0
        assert probe.interval_for(1) == 600.0
        assert probe.interval_for(2) == 1200.0
        assert probe.interval_for(3) == 1800.0
        assert probe.interval_for(9) == 1800.0

    def test_a_negative_count_is_read_as_none(self):
        # Persisted state can be edited by hand. A nonsense count must not
        # produce a nonsense interval.
        assert probe.interval_for(-4) == 300.0

    def test_the_first_probe_is_counted_from_the_bench(self):
        # Not from whenever somebody first looked: a five-minute-old lockout
        # should be tested now, not five minutes after the next query.
        row = bench()
        assert probe.next_probe_at(row) == NOW + 300.0

    def test_later_probes_are_counted_from_the_last_attempt(self):
        row = bench(probes=1, last_probe_at=NOW + 300.0)
        assert probe.next_probe_at(row) == NOW + 300.0 + 600.0


class TestWhatIsWorthTesting:
    def test_a_long_sentence_qualifies(self):
        assert probe.eligible(bench(), now=NOW + 400)

    def test_a_short_throttle_does_not(self):
        # Twenty-one seconds of waiting is cheaper than a call that fails.
        row = bench(reset_at=NOW + 21)
        assert not probe.eligible(row, now=NOW + 1)

    def test_a_bench_about_to_lapse_does_not(self):
        # It answers the question by lapsing, and a refusal provoked this
        # close to the deadline would look like an under-prediction to the
        # journal — poisoning the statistic the probe exists to feed.
        row = bench()
        assert not probe.eligible(row, now=row.reset_at - 100)

    def test_an_expired_bench_does_not(self):
        assert not probe.eligible(bench(), now=NOW + 2 * DAY)

    def test_running_out_of_credit_is_never_probed(self):
        # Not a clock. No number of retries converts an empty balance into a
        # working key, and Hermes keeps the full bench for the same reason.
        for reason in ("billing", "insufficient_quota", "payment_required"):
            assert not probe.eligible(bench(reason=reason), now=NOW + 400), reason

    def test_a_rejected_credential_is_never_probed(self):
        for reason in ("auth", "invalid_api_key", "permission_denied", "revoked"):
            assert not probe.eligible(bench(reason=reason), now=NOW + 400), reason

    def test_a_rate_limit_is_probed(self):
        assert probe.eligible(bench(reason="rate_limit"), now=NOW + 400)

    def test_it_gives_up_eventually(self):
        row = bench(probes=probe.MAX_PROBES, last_probe_at=NOW)
        assert not probe.eligible(row, now=NOW + 10 * HOUR)


class TestChoosing:
    def test_nothing_is_due_before_the_first_interval(self):
        assert probe.choose([bench()], now=NOW + 100) is None

    def test_the_first_probe_is_issued_on_time(self):
        chosen = probe.choose([bench()], now=NOW + 300)
        assert chosen is not None
        assert chosen.fresh is True
        assert chosen.credential_id == "k0"

    def test_an_empty_ledger_asks_for_nothing(self):
        assert probe.choose([], now=NOW) is None

    def test_the_soonest_deadline_is_tested_first(self):
        # The bench closest to lapsing is the most likely to already be wrong
        # and the cheapest to be wrong about.
        rows = [
            bench(credential_id="far", reset_at=NOW + DAY),
            bench(credential_id="near", reset_at=NOW + 2 * HOUR),
        ]
        chosen = probe.choose(rows, now=NOW + 300)
        assert chosen.credential_id == "near"

    def test_a_probe_in_flight_stays_the_answer(self):
        # ``_available_entries`` is asked several times per turn. If the key
        # were offered on one call and withheld on the next, which key gets
        # used would depend on call order.
        row = bench(probes=1, last_probe_at=NOW + 300)
        chosen = probe.choose([row], now=NOW + 310)
        assert chosen is not None
        assert chosen.fresh is False

    def test_the_window_closes(self):
        row = bench(probes=1, last_probe_at=NOW + 300)
        assert probe.choose([row], now=NOW + 300 + probe.PROBE_WINDOW_SECONDS) is None

    def test_only_one_probe_at_a_time(self):
        # Two keys, one already being tested: spending a second call to ask a
        # second question answers neither faster.
        rows = [
            bench(credential_id="waiting", reset_at=NOW + 2 * HOUR),
            bench(credential_id="in-flight", probes=1, last_probe_at=NOW + 300),
        ]
        chosen = probe.choose(rows, now=NOW + 310)
        assert chosen.credential_id == "in-flight"
        assert chosen.fresh is False

    def test_after_the_window_the_backoff_applies(self):
        row = bench(probes=1, last_probe_at=NOW + 300)
        assert probe.choose([row], now=NOW + 800) is None
        chosen = probe.choose([row], now=NOW + 300 + 600)
        assert chosen is not None
        assert chosen.fresh is True

    def test_choosing_is_stable_within_a_moment(self):
        rows = [
            bench(credential_id="b", reset_at=NOW + 2 * HOUR),
            bench(credential_id="a", reset_at=NOW + 2 * HOUR),
        ]
        first = probe.choose(rows, now=NOW + 300)
        second = probe.choose(list(reversed(rows)), now=NOW + 300)
        assert first.credential_id == second.credential_id == "a"


class TestTheLedgerSideOfIt:
    def test_a_probe_is_counted(self):
        led = Ledger()
        led.record(
            credential_id="k0", provider="gemini", model="gemini-3.6-flash",
            reset_at=NOW + DAY, now=NOW,
        )
        updated = led.note_probe("k0", "gemini-3.6-flash", NOW + 300)
        assert updated.probes == 1
        assert updated.last_probe_at == NOW + 300
        assert led.find("k0", "gemini-3.6-flash").probes == 1

    def test_counting_a_probe_changes_nothing_else(self):
        led = Ledger()
        before = led.record(
            credential_id="k0", provider="gemini", model="gemini-3.6-flash",
            reset_at=NOW + DAY, now=NOW, reason="rate_limit",
        )
        after = led.note_probe("k0", "gemini-3.6-flash", NOW + 300)
        assert after.reset_at == before.reset_at
        assert after.recorded_at == before.recorded_at
        assert after.reason == before.reason

    def test_probing_an_unknown_pair_is_a_no_op(self):
        assert Ledger().note_probe("nobody", "nothing", NOW) is None

    def test_a_failed_probe_does_not_reset_the_backoff(self):
        # The refusal a probe provokes re-records the bench. If that wiped the
        # attempt history, the schedule would restart at five minutes forever
        # and never widen — the one bug that would turn this feature into a
        # way to hammer a spent key all day.
        led = Ledger()
        led.record(
            credential_id="k0", provider="gemini", model="gemini-3.6-flash",
            reset_at=NOW + DAY, now=NOW,
        )
        led.note_probe("k0", "gemini-3.6-flash", NOW + 300)
        led.record(
            credential_id="k0", provider="gemini", model="gemini-3.6-flash",
            reset_at=NOW + DAY, now=NOW + 301,
        )
        row = led.find("k0", "gemini-3.6-flash")
        assert row.probes == 1
        assert row.last_probe_at == NOW + 300
        assert probe.next_probe_at(row) == NOW + 300 + 600

    def test_a_new_episode_starts_fresh(self):
        # Once the deadline has genuinely lapsed, the next refusal is a new
        # claim and deserves the full schedule again.
        led = Ledger()
        led.record(
            credential_id="k0", provider="gemini", model="gemini-3.6-flash",
            reset_at=NOW + HOUR, now=NOW,
        )
        led.note_probe("k0", "gemini-3.6-flash", NOW + 300)
        led.record(
            credential_id="k0", provider="gemini", model="gemini-3.6-flash",
            reset_at=NOW + 3 * HOUR, now=NOW + 2 * HOUR,
        )
        row = led.find("k0", "gemini-3.6-flash")
        assert row.probes == 0
        assert row.last_probe_at == 0.0

    def test_probe_state_survives_a_round_trip(self):
        led = Ledger()
        led.record(
            credential_id="k0", provider="gemini", model="gemini-3.6-flash",
            reset_at=NOW + DAY, now=NOW,
        )
        led.note_probe("k0", "gemini-3.6-flash", NOW + 300)
        restored = Ledger.from_dict(led.to_dict())
        row = restored.find("k0", "gemini-3.6-flash")
        assert row.probes == 1
        assert row.last_probe_at == NOW + 300

    def test_a_row_written_before_probing_existed_still_loads(self):
        # No schema bump was taken for this, so old rows have to read as
        # "never tested" rather than being discarded — discarding would
        # un-bench live keys on upgrade.
        row = Bench.from_dict(
            {
                "credential_id": "k0",
                "provider": "gemini",
                "model": "gemini-3.6-flash",
                "reset_at": NOW + DAY,
                "recorded_at": NOW,
                "reason": "rate_limit",
            }
        )
        assert row.probes == 0
        assert row.last_probe_at == 0.0

    def test_a_hand_edited_count_cannot_break_the_schedule(self):
        row = Bench.from_dict(
            {
                "credential_id": "k0",
                "provider": "gemini",
                "model": "gemini-3.6-flash",
                "reset_at": NOW + DAY,
                "recorded_at": NOW,
                "probes": "not a number",
                "last_probe_at": None,
            }
        )
        assert row.probes == 0
        assert probe.next_probe_at(row) == NOW + 300.0
