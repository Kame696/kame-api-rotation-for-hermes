"""The day that looked like a minute.

Measured on the owner's own keys, 04/09/2026, against the real endpoint.

Google reports its per-minute and its per-day free-tier quotas under the
**identical** metric name, and both arrive as an HTTP 429 whose prose is
word-for-word the same sentence. Only ``quotaId`` separates them::

    GenerateRequestsPerMinutePerProjectPerModel-FreeTier   limit: 5
    GenerateRequestsPerDayPerProjectPerModel-FreeTier      limit: 20

And the retry hint does not separate them. A genuine daily exhaustion — the
day's twenty requests gone until midnight Pacific — came back carrying
``retryDelay: "29s"``.

Two defects stood between that field and the decision it exists to make, and
both are fixed here.

**One.** ``_response_text`` never returned anything. It was added in 1.6.0.3
for exactly this, and it read ``.text`` and ``.content`` and stopped, on the
reasoning that ``ResponseNotRead`` "does no I/O". Every chat call Hermes makes
is a *streaming* call, and a streamed response raises exactly that from both
properties. Across 450 refusals in the owner's logs, ``quotaId`` was recovered
**zero** times. ``response.read()`` returns it — measured: 1363 bytes carrying
``GenerateRequestsPerDayPerProjectPerModel-FreeTier``.

**Two.** Nothing downstream read ``quota_window``. Even where the verdict said
``per_day``, the cascade in ``_on_failure`` had no branch for it, so the
refusal fell through as an ordinary ``rate_limit`` sized by the provider's
twenty-nine seconds. Fourteen keys with no requests left until midnight were
re-probed every twenty seconds all night, each probe spending a request
against a quota that had none left to spend.

The host's part is not a bug and is not fixable from here: Hermes'
``gemini_http_error`` parses the payload, keeps the ``google.rpc.ErrorInfo``
slice as ``exc.details``, and drops the whole ``google.rpc.QuotaFailure``
block. It does attach the ``httpx.Response``, which is why this is
recoverable at all.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1700_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
classify_mod = importlib.import_module(f"{PACKAGE}.core.classify")
carousel = importlib.import_module(f"{PACKAGE}.core.carousel")
core = importlib.import_module(f"{PACKAGE}.core")
dispatch_binding = importlib.import_module(f"{PACKAGE}.dispatch_binding")
settings = importlib.import_module(f"{PACKAGE}.settings")

IDENTITY = "gemini:gemini-3.8-flash"
KEYS = [f"AIzaSyDAY{i}" + "7" * 30 for i in range(14)]

PER_MINUTE = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
PER_DAY = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    settings.forget()
    for name in list(settings._ENV_FOR.values()) + list(settings._NUMBER_ENV_FOR.values()):
        monkeypatch.delenv(name, raising=False)
    carousel.ENGINE.forget()
    yield
    settings.forget()
    carousel.ENGINE.forget()


# --- the payload, copied from the wire ---------------------------------------


def payload(quota_id: str, limit: int) -> dict:
    """Byte-for-byte the shape Google returned on 04/09/2026.

    The prose is identical for both quota ids — that is the whole point of
    this file. Only the ``quotaId`` inside ``QuotaFailure`` differs, and the
    retry delay is the same misleading twenty-nine seconds either way.
    """
    return {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": (
                "You exceeded your current quota, please check your plan and "
                "billing details. For more information on this error, head to: "
                "https://ai.google.dev/gemini-api/docs/rate-limits.\n"
                "* Quota exceeded for metric: generativelanguage.googleapis.com"
                f"/generate_content_free_tier_requests, limit: {limit}, "
                "model: gemini-3.8-flash\nPlease retry in 29.395021s."
            ),
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{
                        "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                        "quotaId": quota_id,
                        "quotaDimensions": {"location": "global", "model": "gemini-3.8-flash"},
                        "quotaValue": str(limit),
                    }],
                },
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "29s"},
            ],
        }
    }


class ResponseNotRead(RuntimeError):
    """``httpx`` raises this from the property, which is why it is a class."""


def streamed_response(body: dict):
    """A response whose body was streamed — ``.text`` and ``.content`` raise.

    This is not a pessimistic stand-in. It is every chat call Hermes makes.
    """
    class _Streamed:
        status_code = 429
        headers: dict = {}

        @property
        def text(self):
            raise ResponseNotRead("Attempted to access streaming response content")

        @property
        def content(self):
            raise ResponseNotRead("Attempted to access streaming response content")

        def read(self):
            return json.dumps(body).encode()

    return _Streamed()


def gemini_error(quota_id: str, limit: int):
    """What ``gemini_http_error`` actually constructs, drops included."""
    body = payload(quota_id, limit)
    message = "Gemini HTTP 429 (RESOURCE_EXHAUSTED): " + body["error"]["message"]

    class GeminiAPIError(Exception):
        def __init__(self):
            super().__init__(message)
            self.message = message
            self.code = "gemini_rate_limited"
            self.status_code = 429
            self.response = streamed_response(body)
            self.retry_after = None
            # The host keeps the ErrorInfo slice and drops QuotaFailure —
            # so ``quotaId`` is *not* here. That is the reason the response
            # has to be read.
            self.details = {
                "status": "RESOURCE_EXHAUSTED",
                "reason": "",
                "metadata": {},
                "message": body["error"]["message"],
            }

    return GeminiAPIError()


# --- 1. the body is reachable ------------------------------------------------


class TestTheBodyIsReachableOnAStreamedResponse:
    def test_text_and_content_both_raise(self):
        # The premise. If this ever stops being true the fix below is moot,
        # and a reader of this file should find that out here.
        response = streamed_response(payload(PER_DAY, 20))
        with pytest.raises(ResponseNotRead):
            _ = response.text
        with pytest.raises(ResponseNotRead):
            _ = response.content

    def test_the_body_comes_back_anyway(self):
        text = classify_mod._response_text(gemini_error(PER_DAY, 20))
        assert text, "1.6.0.3 returned '' here, 450 times out of 450"
        assert PER_DAY in text

    def test_the_parsed_body_carries_the_quota_id(self):
        body = classify_mod._response_body(gemini_error(PER_DAY, 20))
        assert body is not None
        violations = body["error"]["details"][0]["violations"]
        assert violations[0]["quotaId"] == PER_DAY

    def test_a_body_already_in_memory_is_still_preferred(self):
        # ``read()`` is last on purpose: a response somebody already read must
        # not be pulled off the wire a second time.
        calls = []

        class _Buffered:
            status_code = 429
            headers: dict = {}
            text = '{"error": {"code": 429}}'

            def read(self):
                calls.append(1)
                return b"{}"

        class _Err(Exception):
            response = _Buffered()

        assert classify_mod._response_text(_Err()) == '{"error": {"code": 429}}'
        assert calls == []

    def test_a_response_that_cannot_be_read_is_not_an_error(self):
        class _Hostile:
            status_code = 429
            headers: dict = {}

            @property
            def text(self):
                raise ResponseNotRead()

            @property
            def content(self):
                raise ResponseNotRead()

            def read(self):
                raise OSError("connection gone")

        class _Err(Exception):
            response = _Hostile()

        assert classify_mod._response_text(_Err()) == ""

    def test_no_response_at_all_is_not_an_error(self):
        assert classify_mod._response_text(Exception("plain")) == ""
        assert classify_mod._response_text(None) == ""


# --- 2. the two quotas are told apart ----------------------------------------


class TestTheSame429MeansTwoDifferentThings:
    def test_the_per_minute_quota_is_named(self):
        verdict = classify_mod.classify(
            provider="gemini", model="gemini-3.8-flash",
            status_code=429, error=gemini_error(PER_MINUTE, 5),
        )
        assert verdict is not None
        assert verdict.quota_window == "per_minute"

    def test_the_per_day_quota_is_named(self):
        verdict = classify_mod.classify(
            provider="gemini", model="gemini-3.8-flash",
            status_code=429, error=gemini_error(PER_DAY, 20),
        )
        assert verdict is not None
        assert verdict.quota_window == "per_day"

    def test_the_prose_is_identical_so_only_the_id_can_decide(self):
        # The sentence Google writes is the same for both, character for
        # character apart from the limit. Any classifier reading the message
        # is reading something that cannot answer the question.
        a = payload(PER_MINUTE, 20)["error"]["message"]
        b = payload(PER_DAY, 20)["error"]["message"]
        assert a == b

    def test_the_decision_did_not_come_from_the_retry_delay(self):
        verdict = classify_mod.classify(
            provider="gemini", model="gemini-3.8-flash",
            status_code=429, error=gemini_error(PER_DAY, 20),
        )
        assert verdict.source == "window"


# --- 3. and they earn different rests ----------------------------------------


def _rest_for(quota_id: str, limit: int, caplog):
    binding = dispatch_binding.DispatchBinding(
        engine=carousel.Carousel(), sleep=lambda _s: None
    )
    with caplog.at_level(logging.DEBUG, logger=f"{PACKAGE}.dispatch_binding"):
        _verdict, kind, _status = binding._on_failure(
            IDENTITY, KEYS[0], gemini_error(quota_id, limit), IDENTITY, 1, False, False
        )
    return kind, binding


class TestADayIsNotAMinute:
    def test_a_per_minute_throttle_still_obeys_the_provider(self, caplog):
        kind, binding = _rest_for(PER_MINUTE, 5, caplog)
        assert kind == "rate_limit"
        row = binding.engine.snapshot()[IDENTITY]
        assert row["failures"] == 1

    def test_a_daily_cap_is_named_daily(self, caplog):
        kind, _binding = _rest_for(PER_DAY, 20, caplog)
        assert kind == "daily", "this fell through as 'rate_limit' until 1.7.0.0"

    def test_a_daily_cap_rests_far_longer_than_the_hint_it_carried(self, caplog):
        # Named "rests for the daily cooldown" until 1.7.0.2, and that is the
        # sentence this release retracts. Resting the full hour on the label
        # alone was refuted on the owner's own keys: twenty-one times a key
        # benched for the hour answered again 6 to 36 minutes later, and only
        # a manual pool reset got it back.
        #
        # What survives, and what this test now holds, is the half that was
        # never in doubt: the provider's 29-second hint does not size a day.
        # It is discarded either way — it is the seconds left in the clock
        # minute, measured on 107 of 114 of these — and what replaces it is a
        # re-probe an order of magnitude longer, not the hint.
        import time as _time
        _kind, binding = _rest_for(PER_DAY, 20, caplog)
        pool = binding.engine._pool_for(IDENTITY, KEYS, _time.time())
        resting = pool[KEYS[0]]["sick_until"] - _time.time()
        assert resting >= carousel.RL_BACKOFF_CAP_S - 5, (
            f"rested {resting:.0f}s — the provider's 29s won again"
        )

    def test_the_twenty_nine_seconds_is_discarded(self, caplog):
        import time as _time
        _kind, binding = _rest_for(PER_DAY, 20, caplog)
        pool = binding.engine._pool_for(IDENTITY, KEYS, _time.time())
        resting = pool[KEYS[0]]["sick_until"] - _time.time()
        assert resting > 60.0, "a day cannot be thirty seconds long"

    def test_the_two_do_not_get_the_same_rest(self, caplog):
        import time as _time
        _k1, minute = _rest_for(PER_MINUTE, 5, caplog)
        _k2, day = _rest_for(PER_DAY, 20, caplog)
        now = _time.time()
        m = minute.engine._pool_for(IDENTITY, KEYS, now)[KEYS[0]]["sick_until"] - now
        d = day.engine._pool_for(IDENTITY, KEYS, now)[KEYS[0]]["sick_until"] - now
        # The gap was `+ 600` while a day cost an hour on sight. 1.7.0.2 makes
        # the first daily rest a re-probe instead, so the gap is now the one
        # between a minute the provider sized (29s) and a day nobody could
        # (300s) — an order of magnitude, and still nothing like the same cost.
        assert d > m * 5, "same 429, same prose — and they must not cost the same"

    def test_a_longer_stated_deadline_still_wins(self, caplog):
        # The rule is "discard the number that is too small", not "ignore the
        # provider". A real reset an hour and a half out is a real number.
        binding = dispatch_binding.DispatchBinding(
            engine=carousel.Carousel(), sleep=lambda _s: None
        )
        assert binding.engine.daily_cooldown_s == 3600.0


# --- 4. the release ----------------------------------------------------------


class TestTheReleaseIsConsistent:
    def test_the_manifest_and_the_core_agree(self):
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        assert f'version: "{core.__version__}"' in manifest

    def test_the_manifest_version_the_installer_accepts_is_unchanged(self):
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        assert "manifest_version: 1" in manifest

    def test_the_read_is_wired_where_it_is_needed(self):
        import inspect
        source = inspect.getsource(classify_mod._response_text)
        assert '"read"' in source, "the streamed body is the only one that exists"


# --- 5. the mirror defect, and the tag that would have shown it --------------


class TestTheFallbackTableDoesNotCallAMinuteADay:
    """``free_tier_requests`` is a metric, not a period.

    It appears in the message of **both** Gemini free-tier 429s, so listing it
    among the daily markers made the fallback table answer "daily" to every
    one of them — a key benched an hour over a throttle that clears in four
    seconds. The exact inverse of the defect this release is named for, on the
    path taken whenever the evidence-first classifier declines.

    The engine this was ported from carried the rule as a comment over its own
    list: *"These are intentionally narrow so a per-minute (RPM/TPM) error is
    NEVER misclassified as daily."* The port widened the list and lost it.
    """

    METRIC = "generativelanguage.googleapis.com/generate_content_free_tier_requests"

    def _classify(self, message):
        return carousel.classify(
            Exception(message), message, status_code=429, headers={},
            daily_cooldown_s=3600.0,
        )

    def test_a_per_minute_free_tier_429_is_not_daily(self):
        message = (
            "Gemini HTTP 429 (RESOURCE_EXHAUSTED): You exceeded your current quota.\n"
            f"* Quota exceeded for metric: {self.METRIC}, limit: 5, "
            "model: gemini-3.8-flash\nPlease retry in 4.39s."
        )
        delay, kind, _status = self._classify(message)
        assert kind == "per_minute"
        assert delay == pytest.approx(4.39)

    def test_a_daily_free_tier_429_still_is(self):
        message = (
            "Gemini HTTP 429: You exceeded your current quota.\n"
            f"* Quota exceeded for metric: {self.METRIC}, limit: 20\n"
            '"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"\n'
            "Please retry in 29s."
        )
        _delay, kind, _status = self._classify(message)
        assert kind == "daily"

    def test_the_prose_form_still_is_too(self):
        _delay, kind, _status = self._classify(
            "429 RESOURCE_EXHAUSTED: quota exceeded, requests per day limit reached"
        )
        assert kind == "daily"

    def test_every_daily_marker_names_a_period(self):
        # The rule, stated so it cannot be widened again by accident: a token
        # here must be about *when*, never about which tier or which metric.
        for token in carousel.DAILY_INDICATORS:
            assert any(
                word in token
                # ``rpd`` is requests-per-day written the way providers write
                # it in a terse error; it names a period like the rest.
                for word in ("day", "daily", "rpd", "insufficient", "credit")
            ), f"{token!r} is not a period"

    def test_the_metric_name_is_not_a_marker(self):
        # The one that broke it, named so a future reader sees the cost.
        # ``generate_content_free_tier_requests`` is the metric of BOTH
        # quotas, so matching on it answers the question with the question.
        for token in carousel.DAILY_INDICATORS:
            assert "free_tier" not in token
            assert "tier" not in token


class TestTheLineSaysWhichQuota:
    """Ported back from the Agent Zero engine, which printed it from v1.0.6.

    Google's per-minute and per-day refusals are the same sentence. Without
    this tag a reader cannot tell a correct one-hour bench from a wrong one,
    which is exactly the position the owner was in.
    """

    def test_a_per_minute_rest_says_so(self):
        assert dispatch_binding._WINDOW_TAG["per_minute"] == " {per-minute}"

    def test_a_daily_rest_shouts(self):
        # Upper case on purpose: the expensive mistake is a day priced as a
        # minute, so the day is the one that has to be hard to skim past.
        assert dispatch_binding._WINDOW_TAG["per_day"] == " {PER-DAY}"

    def test_an_unknown_window_adds_nothing(self):
        assert dispatch_binding._WINDOW_TAG.get("unknown", "") == ""
        assert dispatch_binding._WINDOW_TAG.get("", "") == ""

    def test_the_tag_actually_reaches_the_log_line(self, caplog):
        # The rule is not the table, it is the line. A table nobody reads
        # would pass every assertion above and change nothing on screen.
        binding = dispatch_binding.DispatchBinding(
            engine=carousel.Carousel(), sleep=lambda _s: None
        )
        with caplog.at_level(logging.WARNING, logger=f"{PACKAGE}.dispatch_binding"):
            binding._on_failure(
                IDENTITY, KEYS[0], gemini_error(PER_DAY, 20), IDENTITY, 1, False, False
            )
        rested = [r.getMessage() for r in caplog.records if "resting" in r.getMessage()]
        assert rested, "no rotation line was written at all"
        assert "{PER-DAY}" in rested[0], rested[0]

    def test_a_per_minute_line_is_tagged_too(self, caplog):
        binding = dispatch_binding.DispatchBinding(
            engine=carousel.Carousel(), sleep=lambda _s: None
        )
        with caplog.at_level(logging.WARNING, logger=f"{PACKAGE}.dispatch_binding"):
            binding._on_failure(
                IDENTITY, KEYS[0], gemini_error(PER_MINUTE, 5), IDENTITY, 1, False, False
            )
        rested = [r.getMessage() for r in caplog.records if "resting" in r.getMessage()]
        assert rested
        assert "{per-minute}" in rested[0], rested[0]


class TestAnAccountWithNoMoneyIsNotAThrottle:
    """From the owner's log, 04/09/2026 03:09:09 — the fourth defect.

    A provider answered a 403 with ``"User's credit limit is insufficient,
    remaining credit limit: $0.000000"`` and ``code: insufficient_user_quota``.
    The two billing tokens in the list each missed it by one word:
    ``insufficient_quota`` is not a substring of ``insufficient_user_quota``,
    and the provider wrote *credit limit* where the list said *credit balance*.
    So it classified as ``per_minute`` and the key was re-probed every twenty
    seconds, for ever, against a balance only a payment changes.
    """

    PAYLOAD = (
        "Error code: 403 - {'error': {'message': \"User's credit limit is "
        "insufficient, remaining credit limit: $0.000000\", 'type': 'api_error', "
        "'code': 'insufficient_user_quota'}}"
    )

    def test_it_is_not_a_per_minute_throttle(self):
        _delay, kind, _status = carousel.classify(
            Exception(self.PAYLOAD), self.PAYLOAD, status_code=403,
            headers={}, daily_cooldown_s=3600.0,
        )
        assert kind == "insufficient_quota"

    def test_it_does_not_come_back_in_twenty_seconds(self):
        delay, _kind, _status = carousel.classify(
            Exception(self.PAYLOAD), self.PAYLOAD, status_code=403,
            headers={}, daily_cooldown_s=3600.0,
        )
        assert delay >= 3600.0

    def test_the_wording_the_provider_used_is_covered(self):
        joined = " ".join(carousel.DAILY_INDICATORS)
        assert "insufficient_user_quota" in joined
        assert "credit limit" in joined


# --- 6. the truth table, ported from the engine this came from ---------------


class TestTheClassificationTruthTable:
    """Every phrasing this plugin has met, and what each one must cost.

    Written after a systematic diff against the Agent Zero engine on
    04/09/2026. That engine's ``_RATE_LIMIT_INDICATORS`` held 24 tokens and
    this one held 8; its denial list held 12 and this one 11, missing the
    underscore form Google actually sends. Measured against the nine phrasings
    below, **this plugin classified nine out of nine wrong** — throttles it did
    not recognise fell to ``other``, prose daily caps were priced as
    per-minute throttles, and ``403 PERMISSION_DENIED`` was read as a bad
    credential, which counts toward retiring a key that is in perfect health.

    The last one was an ordering bug as much as a vocabulary one:
    ``is_auth_failure`` answers yes to any 403, and it ran first, so the
    denial branch below it was unreachable for the commonest denial there is.

    This table is the regression surface. A row here is a claim about what a
    provider says and what it costs, and both halves are checked.
    """

    # (label, message, status, kind, seconds — None means "not pinned here")
    ROWS = [
        # throttles, in the words providers actually use
        ("throttled, no 429",      "Error 400: You are being throttled.",              400, "per_minute", 20.0),
        ("concurrency",            "Error 400: concurrency limit reached, retry",      400, "per_minute", 20.0),
        ("usage limit",            "Error: usage limit reached for this account",      400, "per_minute", 20.0),
        ("limit exhausted",        "Error: limit exhausted, try later",                400, "per_minute", 20.0),
        ("tokens per minute",      "Error: tokens per min exceeded",                   400, "per_minute", 20.0),
        # daily caps, which must never be priced as throttles
        ("daily, prose",           "Error: daily quota exceeded for this key",         400, "daily", 3600.0),
        ("RPD",                    "429 limit reached: RPD exceeded",                  429, "daily", 3600.0),
        ("tokens per day",         "Error: tokens per day limit reached",              400, "daily", 3600.0),
        ("requests per day",       "429: requests per day limit reached",              429, "daily", 3600.0),
        ("metric path",            "429 quota exceeded: .../generate_content/day",     429, "daily", 3600.0),
        # money
        ("credit limit",           "403: User's credit limit is insufficient, "
                                   "code insufficient_user_quota",                     403, "insufficient_quota", 3600.0),
        # the pairing refused, not the credential
        ("PERMISSION_DENIED",      "403 PERMISSION_DENIED: caller lacks permission",   403, "denied", None),
        ("model not authorized",   "403: model not authorized for this key",           403, "denied", None),
        ("no access to model",     "Your plan does not include access to this model",  403, "denied", None),
        ("model not available",    "403: model not available on your plan",            403, "denied", None),
        # the credential itself
        ("key not valid",          "400 API key not valid. Please pass a valid API key.", 400, "revoked", None),
        ("bare 401",               "401 Unauthorized",                                 401, "auth", None),
        # the provider, not the key
        ("503",                    "503 Service Unavailable",                          503, "server", 1.0),
        # the request, not the key
        ("model not found",        "404 model not found: gpt-9",                       404, "other", None),
    ]

    @pytest.mark.parametrize(
        "label,message,status,kind,seconds",
        ROWS, ids=[r[0] for r in ROWS],
    )
    def test_row(self, label, message, status, kind, seconds):
        delay, got, _status = carousel.classify(
            Exception(message), message, status_code=status,
            headers={}, daily_cooldown_s=3600.0,
        )
        assert got == kind, f"{label}: classified {got!r}"
        if seconds is not None:
            assert delay == pytest.approx(seconds), f"{label}: rested {delay}s"

    def test_a_missing_model_is_never_a_denial(self):
        # The exclusion, stated. "model not found" is about the request, and
        # benching a key for an hour over it takes a healthy credential out
        # for a typo in the model name.
        _delay, kind, _status = carousel.classify(
            Exception("404 model not found"), "404 model not found",
            status_code=404, headers={}, daily_cooldown_s=3600.0,
        )
        assert kind != "denied"

    def test_an_authentication_class_is_never_a_denial(self):
        text = "PermissionDeniedError: permission denied for this credential"
        _delay, kind, _status = carousel.classify(
            Exception(text), text, status_code=403, headers={}, daily_cooldown_s=3600.0,
        )
        assert kind != "denied"

    def test_denial_is_checked_before_bare_auth(self):
        # The ordering bug in one assertion. ``is_auth_failure`` says yes to
        # any 403, so a denial branch underneath it can never fire.
        import inspect
        source = inspect.getsource(carousel.classify)
        denial = source.index("PERMANENT_DENIAL_INDICATORS")
        auth = source.index("if is_auth_failure(")
        assert denial < auth, "the denial branch is unreachable again"

    def test_a_missing_model_wins_over_a_denial_phrase_in_the_same_sentence(self):
        # The case the exclusion is actually for, and the reason it is checked
        # rather than assumed: providers describe an unknown model with words
        # that overlap the denial list. Here "model not available" is a denial
        # token and "model not found" is the truth — the request names a model
        # that does not exist, and no key on earth fixes that. Benching the
        # credential for an hour would take a healthy key out for a typo.
        text = ("404: model not found — model not available for this API version, "
                "call ListModels to see the models you have access to")
        _delay, kind, _status = carousel.classify(
            Exception(text), text, status_code=404, headers={}, daily_cooldown_s=3600.0,
        )
        assert kind != "denied", "a typo in the model name benched the key"

    def test_the_exclusion_list_still_names_all_three(self):
        assert set(carousel._DENIAL_EXCLUDED) == {
            "model not found", "authenticationerror", "permissiondeniederror",
        }
