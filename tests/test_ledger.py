"""Tests for per-model quota memory and the reconciliation planner.

No Hermes, no filesystem, no clock — every rule takes ``now`` as an argument,
so the tests state the moment they are asking about instead of sleeping.

The scenario these two modules exist for, stated once here because every test
below is a slice of it: a pool of keys, a main model, and a smaller auxiliary
model. Google meters the free tier per key *per model*. Hermes benches per
key. So the moment the main model exhausts a key, that key stops being used
for auxiliary work it has full allowance for.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation")
)

from core.ledger import (  # noqa: E402
    MAX_BENCHES,
    SCHEMA_VERSION,
    Bench,
    Ledger,
    normalize_model,
)
from core.reconcile import (  # noqa: E402
    HOLD,
    RELEASE,
    STATUS_DEAD,
    STATUS_EXHAUSTED,
    Action,
    EntryView,
    plan,
    would_leave_available,
)

NOW = 1_000_000.0
MAIN = "gemini-3.6-flash"
AUX = "gemini-3.5-flash-lite"


def a_ledger(*rows) -> Ledger:
    """Build a ledger from ``(credential_id, model, seconds_from_now)`` rows."""
    ledger = Ledger()
    for credential_id, model, offset in rows:
        ledger.record(
            credential_id=credential_id,
            provider="gemini",
            model=model,
            reset_at=NOW + offset,
            now=NOW,
        )
    return ledger


# ── model names ───────────────────────────────────────────────────────────


class TestModelNormalisation:
    """One quota bucket must not fragment across spellings of its name."""

    @pytest.mark.parametrize(
        "raw",
        [
            "gemini-3.6-flash",
            "models/gemini-3.6-flash",
            "gemini/gemini-3.6-flash",
            "  Gemini-3.6-Flash  ",
            "models/gemini/gemini-3.6-flash",
        ],
    )
    def test_routing_dress_is_stripped(self, raw):
        assert normalize_model(raw) == MAIN

    def test_variants_stay_distinct(self):
        # -flash and -flash-lite are separate quotas. A normaliser that
        # collapsed them would bench an allowance that was never spent.
        assert normalize_model(MAIN) != normalize_model(AUX)
        assert normalize_model("gemini-2.0-flash-exp") != normalize_model("gemini-2.0-flash")

    @pytest.mark.parametrize("raw", [None, "", "   ", "models/"])
    def test_unusable_names_are_empty(self, raw):
        assert normalize_model(raw) == ""


# ── the ledger ────────────────────────────────────────────────────────────


class TestRecording:
    def test_a_bench_is_scoped_to_its_model(self):
        ledger = a_ledger(("key-a", MAIN, 600))
        assert ledger.is_spent_for("key-a", MAIN, NOW) is True
        assert ledger.is_spent_for("key-a", AUX, NOW) is False

    def test_the_same_pair_is_replaced_not_stacked(self):
        ledger = a_ledger(("key-a", MAIN, 60))
        ledger.record(
            credential_id="key-a", provider="gemini", model=MAIN,
            reset_at=NOW + 600, now=NOW,
        )
        assert len(ledger) == 1
        assert ledger.benched_until("key-a", MAIN, NOW) == NOW + 600

    def test_spelling_of_the_model_does_not_matter(self):
        ledger = a_ledger(("key-a", "models/" + MAIN, 600))
        assert ledger.is_spent_for("key-a", "gemini/" + MAIN, NOW) is True

    def test_a_bench_expires_on_its_own(self):
        ledger = a_ledger(("key-a", MAIN, 60))
        assert ledger.is_spent_for("key-a", MAIN, NOW + 59) is True
        assert ledger.is_spent_for("key-a", MAIN, NOW + 61) is False

    @pytest.mark.parametrize(
        "credential_id, model, reset_at",
        [
            ("", MAIN, NOW + 60),
            ("key-a", "", NOW + 60),
            ("key-a", MAIN, None),
            ("key-a", MAIN, "soon"),
            ("key-a", MAIN, float("nan")),
            ("key-a", MAIN, float("inf")),
        ],
    )
    def test_an_unusable_record_is_refused(self, credential_id, model, reset_at):
        # A bench nobody can match later is worse than no bench: it occupies
        # the cap and can never be unwound.
        ledger = Ledger()
        assert ledger.record(
            credential_id=credential_id, provider="gemini", model=model,
            reset_at=reset_at, now=NOW,
        ) is None
        assert len(ledger) == 0

    def test_a_deadline_already_past_is_refused(self):
        ledger = Ledger()
        assert ledger.record(
            credential_id="key-a", provider="gemini", model=MAIN,
            reset_at=NOW - 1, now=NOW,
        ) is None


class TestSummarising:
    def test_latest_reset_spans_every_model(self):
        ledger = a_ledger(("key-a", MAIN, 300), ("key-a", AUX, 900))
        # The host has one cooldown field. Releasing early re-hammers a limit
        # that is still spent, so the safe summary is the furthest deadline.
        assert ledger.latest_reset_for("key-a", NOW) == NOW + 900

    def test_expired_benches_do_not_count(self):
        ledger = a_ledger(("key-a", MAIN, 300), ("key-a", AUX, 900))
        assert ledger.latest_reset_for("key-a", NOW + 400) == NOW + 900
        assert ledger.latest_reset_for("key-a", NOW + 1000) is None

    def test_credentials_do_not_bleed_into_each_other(self):
        ledger = a_ledger(("key-a", MAIN, 300), ("key-b", MAIN, 900))
        assert ledger.latest_reset_for("key-a", NOW) == NOW + 300
        assert [b.credential_id for b in ledger.live_benches_for("key-b", NOW)] == ["key-b"]


class TestForgetting:
    def test_a_removed_credential_takes_its_benches(self):
        ledger = a_ledger(("key-a", MAIN, 300), ("key-a", AUX, 300), ("key-b", MAIN, 300))
        assert ledger.forget_credential("key-a") == 2
        assert len(ledger) == 1

    def test_prune_drops_only_what_has_elapsed(self):
        ledger = a_ledger(("key-a", MAIN, 60), ("key-b", MAIN, 6000))
        assert ledger.prune(NOW + 100) == 1
        assert ledger.is_spent_for("key-b", MAIN, NOW + 100) is True

    def test_growth_is_bounded(self):
        # A provider echoing a fresh model name per call must not grow the
        # persisted file without bound.
        ledger = Ledger()
        for index in range(MAX_BENCHES + 50):
            ledger.record(
                credential_id="key-a", provider="gemini", model=f"model-{index}",
                reset_at=NOW + 1000 + index, now=NOW,
            )
        assert len(ledger) == MAX_BENCHES
        # Eviction is by deadline, so the longest-lived benches survive.
        assert ledger.is_spent_for("key-a", f"model-{MAX_BENCHES + 49}", NOW) is True
        assert ledger.is_spent_for("key-a", "model-0", NOW) is False


class TestPersistence:
    def test_a_ledger_survives_the_round_trip(self):
        before = a_ledger(("key-a", MAIN, 300), ("key-b", AUX, 900))
        after = Ledger.from_dict(before.to_dict())
        assert after.benched_until("key-a", MAIN, NOW) == NOW + 300
        assert after.benched_until("key-b", AUX, NOW) == NOW + 900

    @pytest.mark.parametrize(
        "payload",
        [
            None, "", 42, [], {},
            {"version": SCHEMA_VERSION + 1, "benches": [{"credential_id": "k", "model": "m", "reset_at": 9e9}]},
            {"version": SCHEMA_VERSION, "benches": "not-a-list"},
        ],
    )
    def test_unreadable_state_degrades_to_empty(self, payload):
        # Losing the optimisation is the right failure. Acting on a
        # half-understood record is not.
        assert len(Ledger.from_dict(payload)) == 0

    def test_a_corrupt_row_does_not_take_its_neighbours(self):
        payload = {
            "version": SCHEMA_VERSION,
            "benches": [
                {"credential_id": "key-a", "model": MAIN, "reset_at": NOW + 300},
                {"model": MAIN, "reset_at": NOW + 300},          # no id
                {"credential_id": "key-c", "reset_at": NOW + 300},  # no model
                "not a dict",
                {"credential_id": "key-d", "model": AUX, "reset_at": NOW + 900},
            ],
        }
        ledger = Ledger.from_dict(payload)
        assert len(ledger) == 2
        assert ledger.is_spent_for("key-a", MAIN, NOW) is True
        assert ledger.is_spent_for("key-d", AUX, NOW) is True

    def test_persisted_rows_carry_no_key_material(self):
        # The ledger stores the pool's opaque entry id, never the credential.
        payload = a_ledger(("key-a", MAIN, 300)).to_dict()
        fields = set(payload["benches"][0])
        assert fields == {
            "credential_id",
            "provider",
            "model",
            "reset_at",
            "recorded_at",
            "reason",
            "probes",
            "last_probe_at",
            "scope",
            "refuted_at",
            "extended_to",
        }


# ── the planner ───────────────────────────────────────────────────────────


def benched(credential_id: str, reset_at: float) -> EntryView:
    return EntryView(credential_id=credential_id, status=STATUS_EXHAUSTED, reset_at=reset_at)


def healthy(credential_id: str) -> EntryView:
    return EntryView(credential_id=credential_id, status=None, reset_at=None)


class TestTheCaseThisExistsFor:
    def test_a_key_spent_on_the_main_model_comes_back_for_the_auxiliary(self):
        ledger = a_ledger(("key-a", MAIN, 600))
        actions = plan([benched("key-a", NOW + 600)], ledger, model=AUX, now=NOW)
        assert [(a.kind, a.credential_id) for a in actions] == [(RELEASE, "key-a")]

    def test_and_stays_benched_for_the_model_that_spent_it(self):
        ledger = a_ledger(("key-a", MAIN, 600))
        assert plan([benched("key-a", NOW + 600)], ledger, model=MAIN, now=NOW) == []

    def test_the_return_leg_re_applies_the_bench(self):
        # KAME released key-a so the auxiliary model could use it. The agent
        # switches back to the main model, where it is genuinely spent.
        ledger = a_ledger(("key-a", MAIN, 600))
        actions = plan([healthy("key-a")], ledger, model=MAIN, now=NOW)
        assert [(a.kind, a.credential_id, a.reset_at) for a in actions] == [
            (HOLD, "key-a", NOW + 600)
        ]

    def test_a_full_lap_is_stable(self):
        # main -> aux -> main must not drift: each leg produces exactly the
        # edit that the other leg undoes, and nothing else.
        ledger = a_ledger(("key-a", MAIN, 600))
        assert plan([benched("key-a", NOW + 600)], ledger, model=AUX, now=NOW)[0].kind == RELEASE
        assert plan([healthy("key-a")], ledger, model=MAIN, now=NOW)[0].kind == HOLD
        assert plan([benched("key-a", NOW + 600)], ledger, model=MAIN, now=NOW) == []


class TestOwnership:
    """The gate that keeps this from resurrecting somebody else's bench."""

    def test_a_bench_kame_did_not_write_is_untouched(self):
        ledger = a_ledger(("key-a", MAIN, 600))
        # Host benched it until a different moment — another writer owns it.
        assert plan([benched("key-a", NOW + 99999)], ledger, model=AUX, now=NOW) == []

    def test_a_default_ttl_bench_is_untouched(self):
        # No stored deadline means the host applied its flat TTL. KAME never
        # writes one of those, so it cannot be KAME's.
        ledger = a_ledger(("key-a", MAIN, 600))
        entry = EntryView(credential_id="key-a", status=STATUS_EXHAUSTED, reset_at=None)
        assert plan([entry], ledger, model=AUX, now=NOW) == []

    def test_a_bench_with_no_ledger_record_is_untouched(self):
        assert plan([benched("key-a", NOW + 600)], Ledger(), model=AUX, now=NOW) == []

    def test_a_json_round_tripped_deadline_still_matches(self):
        ledger = a_ledger(("key-a", MAIN, 600))
        actions = plan([benched("key-a", NOW + 600.0000001)], ledger, model=AUX, now=NOW)
        assert actions and actions[0].kind == RELEASE

    def test_a_dead_credential_is_never_released(self):
        # Revoked keys do not come back on a timer. Releasing one puts a key
        # that cannot authenticate straight back into rotation.
        ledger = a_ledger(("key-a", MAIN, 600))
        entry = EntryView(credential_id="key-a", status=STATUS_DEAD, reset_at=NOW + 600)
        assert plan([entry], ledger, model=AUX, now=NOW) == []

    def test_a_dead_credential_is_never_held(self):
        ledger = a_ledger(("key-a", MAIN, 600))
        entry = EntryView(credential_id="key-a", status=STATUS_DEAD, reset_at=None)
        assert plan([entry], ledger, model=MAIN, now=NOW) == []


class TestDeadlineCorrection:
    def test_the_longer_deadline_of_the_model_in_play_wins(self):
        # Spent on both, but the host stored the main model's shorter one.
        # Using it here would release the key while the auxiliary quota is
        # still spent.
        ledger = a_ledger(("key-a", MAIN, 300), ("key-a", AUX, 900))
        actions = plan([benched("key-a", NOW + 300)], ledger, model=AUX, now=NOW)
        assert [(a.kind, a.reset_at) for a in actions] == [(HOLD, NOW + 900)]

    def test_a_matching_deadline_needs_no_edit(self):
        ledger = a_ledger(("key-a", MAIN, 300), ("key-a", AUX, 900))
        assert plan([benched("key-a", NOW + 900)], ledger, model=AUX, now=NOW) == []

    def test_the_reason_names_a_different_model_only_when_there_is_one(self):
        # Two reasons reach this edit and they are not the same sentence. Here
        # it is genuinely another model's deadline standing in the way.
        ledger = a_ledger(("key-a", MAIN, 300), ("key-a", AUX, 900))
        actions = plan([benched("key-a", NOW + 300)], ledger, model=AUX, now=NOW)
        assert MAIN in actions[0].why and AUX in actions[0].why

    def test_and_says_something_true_when_the_model_is_holding_itself(self):
        # From v0.1.0 the same edit fires when KAME is holding the key past
        # the host's deadline on the model in flight. "the deadline was X's,
        # X resets later" reads as a per-model correction that never happened,
        # in the one log a reader consults to find out why a key is missing.
        ledger = Ledger()
        ledger.record(
            credential_id="key-a", provider="gemini", model=MAIN,
            reset_at=NOW + 300, now=NOW, extend_to=NOW + 900,
        )
        actions = plan([benched("key-a", NOW + 300)], ledger, model=MAIN, now=NOW)
        assert [(a.kind, a.reset_at) for a in actions] == [(HOLD, NOW + 900)]
        assert "deadline was" not in actions[0].why
        assert "held past" in actions[0].why


class TestQuietByDefault:
    def test_an_untouched_pool_produces_no_plan(self):
        entries = [healthy("key-a"), healthy("key-b")]
        assert plan(entries, Ledger(), model=MAIN, now=NOW) == []

    def test_an_unknown_model_produces_no_plan(self):
        # Without a model there is no per-model question to answer, and the
        # host's provider-scoped view is simply correct.
        ledger = a_ledger(("key-a", MAIN, 600))
        assert plan([benched("key-a", NOW + 600)], ledger, model="", now=NOW) == []

    def test_an_expired_bench_is_left_for_the_host_to_clear(self):
        # The host clears elapsed cooldowns itself on the next selection.
        # Racing it would be a write for no gain.
        ledger = a_ledger(("key-a", MAIN, 60))
        assert plan([benched("key-a", NOW + 60)], ledger, model=AUX, now=NOW + 120) == []


class TestAvailabilityReporting:
    def test_a_release_adds_a_usable_key(self):
        entries = [benched("key-a", NOW + 600), healthy("key-b")]
        actions = [Action(RELEASE, "key-a", None, "test")]
        assert would_leave_available(entries, actions) == 2

    def test_a_hold_removes_one(self):
        entries = [healthy("key-a"), healthy("key-b")]
        actions = [Action(HOLD, "key-a", NOW + 600, "test")]
        assert would_leave_available(entries, actions) == 1

    def test_dead_keys_never_count(self):
        entries = [EntryView("key-a", STATUS_DEAD, None), healthy("key-b")]
        assert would_leave_available(entries, []) == 1

    def test_holding_the_last_key_is_reported_not_prevented(self):
        # If every key really is spent on this model, saying so is what lets
        # the host fall back to a different model instead of hammering a wall.
        entries = [healthy("key-a")]
        actions = [Action(HOLD, "key-a", NOW + 600, "test")]
        assert would_leave_available(entries, actions) == 0


class TestSeveralKeysAtOnce:
    def test_each_key_is_judged_on_its_own_evidence(self):
        ledger = a_ledger(
            ("key-a", MAIN, 600),   # spent on main only -> release for aux
            ("key-b", AUX, 600),    # spent on aux -> stays benched
        )
        entries = [
            benched("key-a", NOW + 600),
            benched("key-b", NOW + 600),
            healthy("key-c"),
            EntryView("key-d", STATUS_DEAD, None),
        ]
        actions = plan(entries, ledger, model=AUX, now=NOW)
        assert [(a.kind, a.credential_id) for a in actions] == [(RELEASE, "key-a")]
        assert would_leave_available(entries, actions) == 2
