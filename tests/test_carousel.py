"""The rules that decide which key goes out, and what a refusal costs it.

These are the decisions the plugin is named for, and every one of them was
learned from a real failure rather than designed:

* a 503 that ended a turn while fourteen untouched keys sat in the pool;
* a daily quota whose ``retryDelay`` said twelve seconds and meant eleven
  hours, so the key returned, failed, and repeated — hourly, all day;
* an invalid key reported by Google as ``400 INVALID_ARGUMENT``, which every
  status-first classifier reads as a permanent client error and aborts on;
* fifteen keys in one variable, all of which were used one at a time because
  nothing chose a key until something refused.

The tests below pin each of those, plus the two properties that make the whole
thing safe under load: a chosen key is stamped before the lock is released, and
a cooldown is never shortened.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_carousel_under_test"


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
carousel = importlib.import_module(f"{PACKAGE}.core.carousel")

Carousel = carousel.Carousel
ID = "google:gemini-3.7-flash"


class Boom(Exception):
    """A provider exception, shaped the way the SDKs shape them."""

    def __init__(self, message="", status_code=None, headers=None, retry_after=None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if headers is not None:
            self.headers = headers
        if retry_after is not None:
            self.retry_after = retry_after


# --- selection --------------------------------------------------------------


class TestChoosingAKey:
    """Every call picks a key. That is the whole feature, so it is pinned first."""

    def test_a_fresh_pool_spreads_instead_of_repeating(self):
        engine = Carousel()
        keys = ["a", "b", "c"]
        picked = [engine.select(ID, keys, now=100.0 + i)[0] for i in range(3)]
        assert sorted(picked) == keys

    def test_the_least_used_key_wins_before_the_oldest_one(self):
        # ``b`` was used twice recently and ``a`` once a long time ago. Load
        # decides first: a rate limit counts requests, not birthdays.
        engine = Carousel()
        engine.select(ID, ["a", "b"], now=100.0)
        engine.select(ID, ["a", "b"], now=101.0)
        engine.select(ID, ["a", "b"], now=102.0)
        # a:2 uses (100, 102), b:1 use (101) -> b is next.
        assert engine.select(ID, ["a", "b"], now=103.0)[0] == "b"

    def test_use_older_than_the_window_stops_counting(self):
        engine = Carousel()
        engine.select(ID, ["a", "b"], now=100.0)  # a
        engine.select(ID, ["a", "b"], now=100.5)  # b
        engine.select(ID, ["a", "b"], now=101.0)  # a
        # Two minutes later both windows are empty, so the tie falls to the
        # least recently used, which is ``b``.
        assert engine.select(ID, ["a", "b"], now=220.0)[0] == "b"

    def test_a_resting_key_is_not_offered(self):
        engine = Carousel()
        engine.mark(ID, "a", False, 60.0, "per_minute", now=100.0)
        key, status = engine.select(ID, ["a", "b"], now=101.0)
        assert (key, status) == ("b", "SUCCESS")

    def test_a_fully_resting_pool_names_the_soonest_key_and_says_so(self):
        # The caller must be able to tell "here is a healthy key" from "here is
        # the least bad one" — the first is a call to make, the second is a
        # wait. Returning a key either way with no status would send a request
        # we already know will be refused.
        engine = Carousel()
        engine.mark(ID, "a", False, 300.0, "per_minute", now=100.0)
        engine.mark(ID, "b", False, 30.0, "per_minute", now=100.0)
        key, status = engine.select(ID, ["a", "b"], now=101.0)
        assert (key, status) == ("b", "EXHAUSTED")

    def test_the_pick_is_stamped_before_the_lock_is_released(self):
        # Anti-dogpile: two turns entering selection at the same instant must
        # not both be handed the same key. The stamp happens inside the lock,
        # so the second caller already sees the first caller's request.
        engine = Carousel()
        keys = [f"k{i}" for i in range(8)]
        picked = []
        lock = threading.Lock()

        def take():
            key, _ = engine.select(ID, keys)
            with lock:
                picked.append(key)

        threads = [threading.Thread(target=take) for _ in keys]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(picked) == sorted(keys)

    def test_no_keys_is_answered_honestly(self):
        assert Carousel().select(ID, []) == (None, "EMPTY")

    def test_health_is_per_model_not_per_key(self):
        # Google meters free-tier quota per key *per model*. A key spent on the
        # main model still has its whole allowance on a smaller one, and a
        # pool that forgot this benches fifteen keys over one model's limit.
        engine = Carousel()
        engine.mark("google:big", "a", False, 3600.0, "daily", now=100.0)
        assert engine.select("google:small", ["a"], now=101.0)[1] == "SUCCESS"
        assert engine.select("google:big", ["a"], now=101.0)[1] == "EXHAUSTED"


# --- learning ---------------------------------------------------------------


class TestWhatARefusalCosts:
    def test_a_cooldown_is_never_shortened(self):
        # A key that said "out for the day" must not be released in twenty
        # seconds because a softer refusal arrived from it a moment later.
        engine = Carousel()
        engine.mark(ID, "a", False, 3600.0, "daily", now=100.0)
        engine.mark(ID, "a", False, 5.0, "server", now=101.0)
        assert engine.select(ID, ["a"], now=200.0)[1] == "EXHAUSTED"

    def test_a_sized_throttle_is_believed_every_time_not_just_once(self):
        # 1.6.0.3. This test used to assert the opposite — 6s then 12s, under
        # the name "believed once then doubted" — and the behaviour it locked
        # in is the one the owner's 1.6.0.2 log caught: repeating a throttle
        # was read as evidence the provider's number was wrong.
        #
        # It is not. On a rolling window a key refused twice in a row is a key
        # asked again while its window is still full, and the provider answers
        # with a fresh number each time. Doubling a restatement treats it as a
        # refutation. Measured refutation is the journal's job, not this
        # ladder's: ``escalate.stretch`` widens only after two recorded
        # ``under_predictions``, and it is untouched by this change.
        engine = Carousel()
        rests = [
            engine.mark(ID, "a", False, 6.0, "per_minute", now=100.0)
            for _ in range(6)
        ]
        assert rests == [6.0] * 6

    def test_a_sized_throttle_is_not_overruled_by_the_invented_floor(self):
        # The half-fix would have been ``max(delay, ladder)``, which still
        # loses: by the sixth strike the ladder stands at 32s, so a provider
        # asking for 22.3s — a real number from the owner's log — would be
        # overruled by an invention. The provider's number is the answer.
        engine = Carousel()
        for _ in range(5):
            engine.mark(ID, "a", False, 40.0, "per_minute", now=100.0)
        assert engine.mark(ID, "a", False, 22.3, "per_minute", now=100.0) == 22.3

    def test_a_sized_throttle_longer_than_the_cap_is_obeyed_not_clamped(self):
        # The ceiling bounds the ladder KAME invents. Clamping the provider's
        # own number down to it would re-probe early, into a window the
        # provider has just said is still spent.
        engine = Carousel()
        rest = engine.mark(ID, "a", False, 600.0, "per_minute", now=100.0)
        assert rest == 600.0 > carousel.RL_BACKOFF_CAP_S

    def test_an_unsized_throttle_rests_flat_and_never_climbs(self):
        # 1.6.0.3. This asserted a climb to five minutes; the climb is gone.
        # A provider that has never named a number for this model gets the
        # same flat re-probe every time, because there is nothing to infer
        # from having been refused before that the provider did not already
        # answer — and a wrong short rest costs one failed request while a
        # wrong long one costs a healthy key.
        engine = Carousel()
        rests = [
            engine.mark(ID, "a", False, 0.0, "per_minute", now=100.0)
            for _ in range(12)
        ]
        assert rests == [carousel.UNSIZED_THROTTLE_REST_S] * 12

    def test_the_owners_gemini_log_never_rests_past_what_google_asked(self):
        # The 340 throttles Gemini returned in 46 minutes under 1.6.0.2, as
        # the range the log actually contains. Under the old ladder this
        # sequence produced ten 5m benches and a 468s stall; under the new one
        # no rest may exceed the longest number Google itself stated.
        stated = [53.8, 41.1, 37.2, 31.5, 44.0, 22.3, 1.5, 59.8, 12.0, 48.2]
        engine = Carousel()
        rests = [
            engine.mark(ID, "a", False, s, "rate_limit", now=100.0)
            for s in stated
        ]
        assert rests == stated
        assert max(rests) <= max(stated)

    def test_a_5xx_rests_briefly_and_caps_at_ninety_seconds(self):
        # The provider is unwell, not the key. A pool taken offline for an hour
        # by a two-minute outage is worse than no pool at all.
        engine = Carousel()
        assert engine.mark(ID, "a", False, 5.0, "server", now=100.0) == 5.0
        assert engine.mark(ID, "a", False, 5.0, "server", now=100.0) == 10.0
        for _ in range(10):
            applied = engine.mark(ID, "a", False, 5.0, "server", now=100.0)
        assert applied == carousel.SERVER_BACKOFF_CAP_S

    def test_a_daily_refusal_climbs_to_the_configured_cooldown(self):
        engine = Carousel(daily_cooldown_s=3600.0)
        applied = 0.0
        for _ in range(12):
            applied = engine.mark(ID, "a", False, 3600.0, "daily", now=100.0)
        assert applied == 3600.0

    def test_success_clears_the_ladder(self):
        engine = Carousel()
        engine.mark(ID, "a", False, 6.0, "per_minute", now=100.0)
        engine.mark(ID, "a", False, 6.0, "per_minute", now=100.0)
        engine.mark(ID, "a", True, now=200.0)
        assert engine.mark(ID, "a", False, 6.0, "per_minute", now=300.0) == 6.0

    def test_nothing_ever_rests_longer_than_a_day(self):
        engine = Carousel(daily_cooldown_s=86400.0)
        applied = engine.mark(ID, "a", False, 999999.0, "other", now=100.0)
        assert applied == carousel.HARD_DELAY_CAP_S


class TestRecoveringFromAnOutage:
    def test_a_success_pulls_the_other_server_cooled_keys_forward(self):
        # When one key answers after a provider-wide outage, the *provider*
        # recovered. Making the rest serve out a 90-second sentence turns a
        # recovered pool into a trickle.
        engine = Carousel()
        for key in ("a", "b", "c"):
            for _ in range(6):
                engine.mark(ID, key, False, 5.0, "server", now=100.0)
        engine.mark(ID, "a", True, now=110.0)
        thawed = engine.thaw_server_cooled(ID, "a", now=110.0)
        assert thawed == 2
        assert engine.select(ID, ["b", "c"], now=120.0)[1] == "SUCCESS"

    def test_a_quota_cooldown_is_never_thawed(self):
        # That key learned something about *itself*. A provider recovering
        # says nothing about a quota it already spent.
        engine = Carousel()
        engine.mark(ID, "a", False, 5.0, "server", now=100.0)
        engine.mark(ID, "b", False, 3600.0, "daily", now=100.0)
        engine.thaw_server_cooled(ID, "a", now=100.0)
        assert engine.select(ID, ["b"], now=200.0)[1] == "EXHAUSTED"

    def test_thawing_only_ever_shortens(self):
        # ``b`` is already 1.5 seconds from recovering; the thaw target is
        # three seconds out. A key whose own deadline is sooner keeps it —
        # "recover fast" must never become "recover later than you would have".
        engine = Carousel()
        engine.mark(ID, "a", False, 5.0, "server", now=100.0)
        engine.mark(ID, "b", False, 5.0, "server", now=100.0)
        before = engine.next_recovery_seconds(ID, ["b"], now=103.5)
        engine.thaw_server_cooled(ID, "a", now=103.5)
        after = engine.next_recovery_seconds(ID, ["b"], now=103.5)
        assert after == before

    def test_the_wait_is_reported_from_the_soonest_key(self):
        engine = Carousel()
        engine.mark(ID, "a", False, 300.0, "per_minute", now=100.0)
        engine.mark(ID, "b", False, 30.0, "per_minute", now=100.0)
        assert engine.next_recovery_seconds(ID, ["a", "b"], now=100.0) == 30.0

    def test_a_healthy_pool_reports_no_wait(self):
        assert Carousel().next_recovery_seconds(ID, ["a"], now=100.0) is None


# --- classification ---------------------------------------------------------


class TestReadingAFailure:
    def test_a_5xx_is_checked_before_the_words(self):
        # A provider under load has been seen to return 503 with a body
        # mentioning quota. Reading the words first turns a two-minute outage
        # into an hour-long bench across every key at once.
        delay, kind, status = carousel.classify(
            Boom("quota exceeded, service unavailable", status_code=503)
        )
        assert (kind, status) == ("server", 503)
        assert delay == carousel.SERVER_BASE_S

    def test_a_daily_cap_ignores_the_delay_the_provider_named(self):
        # This is the single most expensive lie a provider tells a rotation
        # engine: a quota that resets at midnight, reported as twelve seconds.
        delay, kind, _ = carousel.classify(
            Boom(
                "429 RESOURCE_EXHAUSTED: quota exceeded for requests per day",
                status_code=429,
                retry_after=12,
            ),
            daily_cooldown_s=3600.0,
        )
        assert kind == "daily"
        assert delay == 3600.0

    def test_a_per_minute_throttle_keeps_the_delay_it_named(self):
        delay, kind, _ = carousel.classify(
            Boom("429 rate limit exceeded", status_code=429, retry_after=17)
        )
        assert (kind, delay) == ("per_minute", 17.0)

    def test_an_invalid_key_arrives_as_a_400_and_is_not_terminal(self):
        # Google's shape, verbatim. Every status-first classifier reads this as
        # a permanent client error and ends the run; here it quarantines one
        # key and the other fourteen carry the turn.
        exc = Boom(
            "400 INVALID_ARGUMENT: API key not valid. Please pass a valid API key.",
            status_code=400,
        )
        assert carousel.is_auth_failure(exc) is True
        assert carousel.is_terminal(exc) is False
        _, kind, _ = carousel.classify(exc)
        # 1.6.0.1 splits the family in two. This wording is the provider
        # saying the words, so it is ``revoked`` — the only kind that may
        # take a key out of rotation on the first one. A *bare* 401, which
        # says nothing about why, stays ``auth`` and needs three in a row.
        assert kind == "revoked"

    def test_a_genuine_bad_request_is_terminal(self):
        assert carousel.is_terminal(Boom("400 unknown field 'foo'", status_code=400)) is True
        assert carousel.is_terminal(Boom("404 model not found", status_code=404)) is True

    def test_a_content_policy_refusal_is_terminal(self):
        # No key answers this one, and rotating through fifteen would turn one
        # clear refusal into fifteen slow ones.
        assert carousel.is_terminal(Boom("blocked by safety filter", status_code=200)) is True

    def test_a_429_is_never_terminal(self):
        assert carousel.is_terminal(Boom("429 too many requests", status_code=429)) is False

    def test_a_timeout_rests_three_seconds(self):
        delay, kind, _ = carousel.classify(TimeoutError("read timed out"))
        assert (kind, delay) == ("timeout", carousel.TIMEOUT_S)

    def test_gemini_streaming_read_timeout_is_classified_as_timeout_and_non_terminal(self):
        exc = Exception("Gemini streaming request failed: The read operation timed out")
        delay, kind, _ = carousel.classify(exc)
        assert (kind, delay) == ("timeout", carousel.TIMEOUT_S)
        assert carousel.is_terminal(exc) is False

    def test_a_spending_limit_403_is_a_quota_not_a_bad_key(self):
        # Rotating past it is right; quarantining the key as invalid is not.
        exc = Boom("403 key limit exceeded — rate limit reached", status_code=403)
        assert carousel.is_auth_failure(exc) is False

    def test_an_unrecognised_failure_gets_a_short_flat_rest(self):
        delay, kind, _ = carousel.classify(Boom("something went sideways"))
        assert (kind, delay) == ("other", carousel.OTHER_S)


class TestReadingTheDelay:
    def test_an_sdk_attribute_is_preferred(self):
        assert carousel.extract_delay(Boom("x", retry_after=42)) == 42.0

    def test_a_header_is_read_when_no_attribute_exists(self):
        assert carousel.extract_delay(Boom("x", headers={"retry-after": "31"})) == 31.0

    def test_googles_retry_info_object_is_understood(self):
        class RetryInfo:
            seconds = 6
            nanos = 500_000_000

        assert carousel.extract_delay(Boom("x", retry_after=RetryInfo())) == 6.5

    def test_a_compound_duration_in_the_text_is_understood(self):
        assert carousel.parse_duration("6m 11.52s") == pytest.approx(371.52)

    def test_a_delay_beyond_a_day_is_refused(self):
        assert carousel.extract_delay(Boom("x", retry_after=999999)) is None

    def test_nothing_to_read_says_nothing(self):
        assert carousel.extract_delay(Boom("plain failure")) is None


class TestWhatGetsLogged:
    def test_a_fingerprint_carries_no_key_material(self):
        # Not even a prefix: eight real characters of a live credential in a
        # log is a log that cannot be pasted into an issue.
        key = "AIzaSyEXAMPLE" + "0" * 26
        label = carousel.fingerprint(key)
        assert key[:8] not in label
        assert label == carousel.fingerprint(key)
        assert label != carousel.fingerprint(key + "x")

    def test_durations_are_written_for_a_person(self):
        assert carousel.format_duration(45) == "45s"
        assert carousel.format_duration(371) == "6m 11s"
        assert carousel.format_duration(7200) == "2h 0m"
        assert carousel.format_duration(None) == "unknown"

    def test_the_snapshot_counts_without_naming(self):
        # ``/kame-quota`` reports how the pool is doing. It reports counts,
        # never credentials — a status command that prints a key is a status
        # command nobody can screenshot.
        engine = Carousel()
        first, second = "AIzaSyFIRST" + "0" * 28, "AIzaSySECOND" + "0" * 27
        engine.select(ID, [first, second], now=100.0)
        engine.mark(ID, first, False, 60.0, "per_minute", now=100.0)
        snap = engine.snapshot(now=101.0)[ID]
        assert (snap["keys"], snap["healthy"], snap["resting"]) == (2, 1, 1)
        assert first not in str(snap) and second not in str(snap)
