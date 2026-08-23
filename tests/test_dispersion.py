"""Which of the healthy keys goes out next.

Every other rule in this plugin decides whether a key is usable. This is the
one that decides which usable key is used, and it is the piece the Agent Zero
engine has had since its v1.0.0 while this plugin did not: the host's default
is to hand out the same key until the provider refuses it, which turns a pool
of fifteen keys into fifteen consecutive walls.

The rules are stated here against the module directly, because they are
arithmetic and must be checkable without a pool at all. That the pool
actually consults them is a separate question, answered in
``test_binding.py``.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_dispersion_under_test"


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
dispersion = importlib.import_module(f"{PACKAGE}.core.dispersion")

NOW = 1_000_000.0
BUCKET = "gemini:gemini-3.6-flash"


class TestTheBucket:
    def test_provider_and_model_are_one_counter(self):
        assert dispersion.bucket_for("Gemini", "Gemini-3.6-Flash") == BUCKET

    def test_no_model_falls_back_to_the_provider_alone(self):
        # Two keys on the same provider still spread better against a shared
        # count than against no count at all.
        assert dispersion.bucket_for("gemini", "") == "gemini"
        assert dispersion.bucket_for("gemini", None) == "gemini"

    def test_a_model_makes_a_different_counter(self):
        # Per-minute quota is metered per key per model, so the main model's
        # traffic must not make the auxiliary model's keys look busy.
        assert dispersion.bucket_for("gemini", "a") != dispersion.bucket_for("gemini", "b")


class TestTheName:
    """What a credential is counted as. The key, not the row that holds it."""

    def test_one_key_in_two_rows_is_one_counter(self):
        assert dispersion.mark_id("row-a", "sk-same") == dispersion.mark_id("row-b", "sk-same")

    def test_two_keys_in_one_row_are_two_counters(self):
        assert dispersion.mark_id("row", "sk-old") != dispersion.mark_id("row", "sk-new")

    def test_a_row_with_no_key_is_counted_as_itself(self):
        # An OAuth entry: the host rotates its access token, and naming the
        # counter after the token would make the same credential look brand
        # new after every refresh.
        assert dispersion.mark_id("oauth-row", "") == "oauth-row"
        assert dispersion.mark_id("oauth-row", None) == "oauth-row"

    def test_the_key_cannot_be_read_back_out_of_the_name(self):
        name = dispersion.mark_id("row", "sk-super-secret-value")
        assert "sk-super-secret-value" not in name
        assert "secret" not in name


class TestTheOrder:
    def test_a_pool_it_has_never_seen_comes_back_untouched(self):
        spread = dispersion.Dispersion()
        assert spread.order(BUCKET, ["a", "b", "c"], NOW) == ["a", "b", "c"]

    def test_the_busy_key_goes_last(self):
        spread = dispersion.Dispersion()
        spread.note(BUCKET, "a", NOW)
        assert spread.order(BUCKET, ["a", "b"], NOW) == ["b", "a"]

    def test_fewest_requests_wins_regardless_of_position(self):
        spread = dispersion.Dispersion()
        for _ in range(3):
            spread.note(BUCKET, "a", NOW)
        spread.note(BUCKET, "b", NOW)
        assert spread.order(BUCKET, ["a", "b", "c"], NOW) == ["c", "b", "a"]

    def test_a_tie_on_count_breaks_on_least_recently_used(self):
        spread = dispersion.Dispersion()
        spread.note(BUCKET, "a", NOW)
        spread.note(BUCKET, "b", NOW + 1)
        assert spread.order(BUCKET, ["b", "a"], NOW + 2) == ["a", "b"]

    def test_a_tie_on_both_keeps_the_order_it_was_given(self):
        # The host arranged these; with nothing to say about them, KAME does
        # not get to rearrange them.
        spread = dispersion.Dispersion()
        spread.note(BUCKET, "a", NOW)
        spread.note(BUCKET, "b", NOW)
        assert spread.order(BUCKET, ["b", "a"], NOW) == ["b", "a"]

    def test_the_window_forgets(self):
        spread = dispersion.Dispersion()
        spread.note(BUCKET, "a", NOW)
        # A minute later that request is not evidence about anything: the
        # per-minute counter it was spent against has already rolled over.
        later = NOW + dispersion.WINDOW_SECONDS + 1
        assert spread.order(BUCKET, ["a", "b"], later) == ["a", "b"]
        assert spread.load(BUCKET, "a", later) == (0, 0.0)

    def test_counters_do_not_leak_between_models(self):
        spread = dispersion.Dispersion()
        other = dispersion.bucket_for("gemini", "gemini-3.5-flash-lite")
        for _ in range(5):
            spread.note(BUCKET, "a", NOW)
        assert spread.order(other, ["a", "b"], NOW) == ["a", "b"]

    def test_an_id_it_has_never_seen_is_treated_as_idle(self):
        spread = dispersion.Dispersion()
        spread.note(BUCKET, "a", NOW)
        assert spread.order(BUCKET, ["a", "z"], NOW)[0] == "z"

    def test_the_set_is_never_changed(self):
        # The order changes, the set does not. No request may fail because of
        # anything decided in this module.
        spread = dispersion.Dispersion()
        for _ in range(4):
            spread.note(BUCKET, "b", NOW)
        assert sorted(spread.order(BUCKET, ["a", "b", "c"], NOW)) == ["a", "b", "c"]


class TestTheOneThatJustCameBack:
    """A key handed back at its deadline is the likeliest to refuse again.

    That is not a hunch: it is the pattern ``escalate.py`` exists for — a
    deadline measured too short shows up as a re-refusal within minutes of the
    release. So a rested key wins a tie, and only a tie.
    """

    def test_a_rested_key_goes_before_a_just_released_one(self):
        spread = dispersion.Dispersion()
        assert spread.order(BUCKET, ["a", "b"], NOW, just_released={"a"}) == ["b", "a"]

    def test_it_is_a_preference_and_never_an_exclusion(self):
        spread = dispersion.Dispersion()
        got = spread.order(BUCKET, ["a", "b"], NOW, just_released={"a"})
        assert sorted(got) == ["a", "b"]

    def test_when_every_key_just_came_back_the_load_decides(self):
        # Degrades to the ordinary answer instead of to no answer.
        spread = dispersion.Dispersion()
        for _ in range(3):
            spread.note(BUCKET, "a", NOW)
        assert spread.order(BUCKET, ["a", "b"], NOW, just_released={"a", "b"}) == ["b", "a"]

    def test_rest_outranks_load(self):
        # The rested key is the busier one and still goes first: one failed
        # call costs more than one unevenly spread one.
        spread = dispersion.Dispersion()
        for _ in range(5):
            spread.note(BUCKET, "b", NOW)
        assert spread.order(BUCKET, ["a", "b"], NOW, just_released={"a"}) == ["b", "a"]

    def test_saying_nothing_changes_nothing(self):
        spread = dispersion.Dispersion()
        assert spread.order(BUCKET, ["a", "b"], NOW) == ["a", "b"]
        assert spread.order(BUCKET, ["a", "b"], NOW, just_released=set()) == ["a", "b"]


class TestTheLoad:
    def test_an_untouched_key_reads_as_idle(self):
        assert dispersion.Dispersion().load(BUCKET, "a", NOW) == (0, 0.0)

    def test_the_count_is_the_requests_inside_the_window(self):
        spread = dispersion.Dispersion()
        spread.note(BUCKET, "a", NOW - 90)   # outside
        spread.note(BUCKET, "a", NOW - 30)   # inside
        spread.note(BUCKET, "a", NOW)        # inside
        count, last_used = spread.load(BUCKET, "a", NOW)
        assert (count, last_used) == (2, NOW)


class TestTheCeilings:
    def test_marks_per_key_are_bounded(self):
        spread = dispersion.Dispersion(max_marks=10)
        for index in range(50):
            spread.note(BUCKET, "a", NOW + index * 0.001)
        count, _last = spread.load(BUCKET, "a", NOW)
        assert count == 10

    def test_buckets_are_bounded_and_the_oldest_goes_first(self):
        spread = dispersion.Dispersion(max_buckets=2)
        spread.note("one", "a", NOW)
        spread.note("two", "a", NOW)
        spread.note("three", "a", NOW)
        assert spread.load("one", "a", NOW) == (0, 0.0)
        assert spread.load("three", "a", NOW)[0] == 1

    def test_a_long_walk_across_models_does_not_grow_without_end(self):
        spread = dispersion.Dispersion(max_buckets=8)
        for index in range(200):
            spread.note(f"gemini:model-{index}", "a", NOW + index)
        assert len(spread._marks) <= 8


class TestConcurrency:
    def test_every_request_from_every_thread_is_counted(self):
        # Selection happens inside the pool's lock on some paths and outside
        # it on others. A structure that is only usually consistent shows up
        # as a rare wrong-key choice nobody can reproduce.
        spread = dispersion.Dispersion(max_marks=10_000)
        threads = [
            threading.Thread(
                target=lambda offset=offset: [
                    spread.note(BUCKET, "a", NOW + offset + step * 0.0001)
                    for step in range(100)
                ]
            )
            for offset in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert spread.load(BUCKET, "a", NOW + 8)[0] == 800


class TestItHoldsNoSecret:
    def test_only_identifiers_are_stored(self):
        # Ids are hashes or short labels and appear in logs already. A key
        # never reaches this module, and this is the assertion that keeps it
        # that way when somebody later adds a field "for debugging".
        spread = dispersion.Dispersion()
        spread.note(BUCKET, "cred-1", NOW)
        stored = repr(spread._marks)
        assert "cred-1" in stored
        assert all(
            isinstance(value, list) and all(isinstance(mark, float) for mark in value)
            for series in spread._marks.values()
            for value in series.values()
        )


class TestShowingItsWork:
    """``snapshot`` — the same numbers selection ordered on, for the report.

    Worth its own tests because a report that lies is worse than no report:
    the user reads this to decide whether rotation is happening at all, and a
    count that includes requests outside the window would say "spread" about a
    pool that has not been touched in an hour.
    """

    def test_it_counts_what_the_window_holds(self):
        spread = dispersion.Dispersion()
        spread.note(BUCKET, "a", NOW)
        spread.note(BUCKET, "a", NOW + 1)
        spread.note(BUCKET, "b", NOW + 2)
        assert spread.snapshot(NOW + 3) == {BUCKET: {"a": 2, "b": 1}}

    def test_marks_older_than_the_window_are_gone_from_the_report_too(self):
        # Not merely hidden: the same prune selection uses, so the report and
        # the decision can never disagree.
        spread = dispersion.Dispersion()
        spread.note(BUCKET, "a", NOW)
        assert spread.snapshot(NOW + dispersion.WINDOW_SECONDS + 1) == {}

    def test_a_bucket_that_emptied_out_is_dropped_not_shown_as_zero(self):
        # A heading with nothing under it reads like a fault. There is no
        # difference worth showing between "this model went quiet" and "this
        # model was never used".
        spread = dispersion.Dispersion()
        spread.note(BUCKET, "a", NOW)
        spread.note("other:model", "b", NOW + dispersion.WINDOW_SECONDS + 5)
        assert spread.snapshot(NOW + dispersion.WINDOW_SECONDS + 6) == {
            "other:model": {"b": 1}
        }

    def test_nothing_seen_means_nothing_reported(self):
        assert dispersion.Dispersion().snapshot(NOW) == {}

    def test_the_caller_cannot_reach_into_live_state(self):
        # The returned mapping is handed to a rendering function that runs
        # while other threads are selecting. If it were the live dictionary,
        # a selection mid-render would be a RuntimeError in a chat turn.
        spread = dispersion.Dispersion()
        spread.note(BUCKET, "a", NOW)
        taken = spread.snapshot(NOW)
        taken[BUCKET]["a"] = 99
        taken["invented"] = {"x": 1}
        assert spread.snapshot(NOW) == {BUCKET: {"a": 1}}

    def test_it_holds_no_secret_either(self):
        spread = dispersion.Dispersion()
        spread.note(BUCKET, dispersion.mark_id("row-1", "sk-live-secret"), NOW)
        assert "sk-live-secret" not in repr(spread.snapshot(NOW))


class TestTheLongerView:
    """``totals`` — has this key ever been used, not just in the last minute.

    Selection reads the window and nothing else; this exists because sixty
    seconds is a short thing to be looking at, and the case worth seeing is a
    key that has taken nothing all day.
    """

    def test_it_counts_past_the_window(self):
        spread = dispersion.Dispersion()
        spread.note(BUCKET, "a", NOW)
        spread.note(BUCKET, "a", NOW + dispersion.WINDOW_SECONDS * 10)
        assert spread.snapshot(NOW + dispersion.WINDOW_SECONDS * 10)[BUCKET] == {"a": 1}
        assert spread.totals()[BUCKET] == {"a": 2}

    def test_it_counts_past_the_per_key_ceiling_too(self):
        # The window drops the oldest marks at the cap, which understates a
        # key being hammered. The running count is the number that stays true.
        spread = dispersion.Dispersion(max_marks=5)
        for step in range(20):
            spread.note(BUCKET, "a", NOW + step * 0.1)
        assert spread.totals()[BUCKET]["a"] == 20

    def test_a_bucket_dropped_at_the_ceiling_takes_its_total_with_it(self):
        # Otherwise the one structure with no expiry is also the one with no
        # bound, which is how a long-running agent leaks.
        spread = dispersion.Dispersion(max_buckets=2)
        for index in range(6):
            spread.note(f"bucket-{index}", "a", NOW)
        assert len(spread.totals()) <= 2

    def test_the_caller_cannot_reach_into_live_state(self):
        spread = dispersion.Dispersion()
        spread.note(BUCKET, "a", NOW)
        taken = spread.totals()
        taken[BUCKET]["a"] = 99
        assert spread.totals()[BUCKET]["a"] == 1

    def test_nothing_seen_means_nothing_counted(self):
        assert dispersion.Dispersion().totals() == {}

    def test_it_decides_nothing(self):
        # The ordering must keep reading the window alone: a key that took a
        # thousand requests an hour ago and nothing since is the *best* choice
        # now, and letting the running count weigh in would bury it.
        spread = dispersion.Dispersion()
        for step in range(50):
            spread.note(BUCKET, "old", NOW + step)
        later = NOW + dispersion.WINDOW_SECONDS * 5
        spread.note(BUCKET, "new", later)
        assert spread.order(BUCKET, ["new", "old"], later + 1)[0] == "old"

    def test_the_running_count_gets_no_vote_even_when_it_is_the_only_signal(self):
        # The case above still separates the two keys by the window — "old"
        # wins there whether or not the total is consulted, so it would not
        # notice a running count sneaking into the sort key. Here the window
        # says nothing at all: both keys are outside it, neither has a last-use
        # inside it, and the only thing telling them apart is that one has
        # taken fifty requests since start and the other none. Ordering must
        # come back untouched, in the order the pool handed it over.
        spread = dispersion.Dispersion()
        for step in range(50):
            spread.note(BUCKET, "busy", NOW + step)
        spread.introduce(BUCKET, ["busy", "untouched"])
        later = NOW + dispersion.WINDOW_SECONDS * 5
        assert spread.totals()[BUCKET] == {"busy": 50, "untouched": 0}
        assert spread.order(BUCKET, ["busy", "untouched"], later) == [
            "busy",
            "untouched",
        ]
        assert spread.order(BUCKET, ["untouched", "busy"], later) == [
            "untouched",
            "busy",
        ]


class TestTheKeyNobodyPicked:
    """``introduce`` — a credential that exists but has never gone out.

    The counters are written when a key is handed out, so a pool where one
    key takes everything produces a report listing exactly that one key. The
    fourteen idle ones — the whole reason somebody is reading the report —
    were invisible.
    """

    def test_a_key_that_was_never_picked_still_has_a_counter(self):
        spread = dispersion.Dispersion()
        spread.introduce(BUCKET, ["a", "b"])
        spread.note(BUCKET, "a", NOW)
        assert spread.totals()[BUCKET] == {"a": 1, "b": 0}

    def test_it_never_resets_a_key_that_has_been_used(self):
        spread = dispersion.Dispersion()
        spread.note(BUCKET, "a", NOW)
        spread.introduce(BUCKET, ["a", "b"])
        assert spread.totals()[BUCKET]["a"] == 1

    def test_it_changes_no_decision(self):
        # Introducing a key must not make it look used, or the ordering would
        # start passing over exactly the key it should hand out next.
        spread = dispersion.Dispersion()
        spread.introduce(BUCKET, ["a", "b"])
        assert spread.snapshot(NOW) == {}
        assert spread.order(BUCKET, ["a", "b"], NOW) == ["a", "b"]

    def test_saying_nothing_introduces_nothing(self):
        spread = dispersion.Dispersion()
        spread.introduce(BUCKET, [])
        spread.introduce("", ["a"])
        assert spread.totals() == {}

    def test_it_respects_the_bucket_ceiling(self):
        spread = dispersion.Dispersion(max_buckets=2)
        for index in range(6):
            spread.introduce(f"bucket-{index}", ["a"])
        assert len(spread.totals()) <= 2
