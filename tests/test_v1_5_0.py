"""1.5.0 — the evidence was on the call, named, and thrown away.

1.4.0 fixed the attributes it read off the exception and walked past the
exception itself. The host hands the classifier hook two more things:

* ``error_type``, which ``agent/error_classifier.py:680`` computes as literally
  ``type(error).__name__``;
* ``error_code``, from ``_extract_error_code`` (``:1808``), which walks the
  exception's ``__cause__``/``__context__`` chain five levels deep, parses JSON
  nested inside ``error.message``, and knows the ``error_code``/``errorCode``
  spellings this plugin's own fixed path list does not.

Both landed in ``**_ignored``, under a comment explaining that they were
discarded. The class name is the *only* evidence a transport failure carries —
no status, no body, ever — and the only evidence left for any SDK class the
host's ``RateLimitError -> 429`` repair (``:684``) does not cover.

The trap this release had to avoid is the reason the class table is curated
rather than folded into ``_TABLE``: ``_n`` strips separators, so
``AuthenticationError`` normalises to the same key as ``authentication_error``,
which reads as ``AUTH_DEAD`` — retire the credential, permanently. But
``AuthenticationError`` is the class of *every* 401, including the expired
OAuth token that was about to refresh. Feeding class names into ``look_up``
would have recreated the defect 1.4.0 removed when it took ``"unauthorized"``
out of the permanent-auth patterns, after 21 hour-long quarantines of healthy
keys.
"""

from __future__ import annotations

import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"

catalog = importlib.import_module("hermes-kame-api-rotation.core.catalog")
classify_mod = importlib.import_module("hermes-kame-api-rotation.core.classify")
carousel = importlib.import_module("hermes-kame-api-rotation.core.carousel")
classify = classify_mod.classify


class _Bare(Exception):
    """An exception carrying nothing at all — no status, no body, no fields.

    This is not a contrivance. Every transport failure looks like this, and so
    does an SDK error whose ``status_code`` did not survive whatever wrapped
    it. It is the payload against which the whole release is aimed.
    """


class RateLimitError(_Bare):
    pass


class AuthenticationError(_Bare):
    pass


class PermissionDeniedError(_Bare):
    pass


class APIConnectionError(_Bare):
    pass


class ProviderStreamError(_Bare):
    pass


class TestTheClassNameIsEvidence:
    def test_a_bare_rate_limit_class_is_sized_instead_of_declined(self):
        """No status, no body, no headers — and still an answer.

        Before 1.5.0 this returned ``None``: nothing in the payload matched a
        catalogue row, so KAME declined and the key went back to the host
        unrested. The class name was on the call the whole time.
        """
        exc = RateLimitError("429 Too Many Requests")

        assert classify(error=exc, error_message=str(exc)) is None

        verdict = classify(
            error=exc, error_message=str(exc), error_type=type(exc).__name__
        )
        assert verdict is not None
        assert verdict.reason == "rate_limit"

    def test_a_transport_failure_rests_briefly_rather_than_blindly(self):
        """``APIConnectionError`` has no status and no body, by construction.

        It used to fall to the twenty-second rest for the unrecognised. Three
        seconds and a rotation is the whole of the right answer, and the
        carousel now reads the same table the classifier does.
        """
        delay, kind, _status = carousel.classify(APIConnectionError("connection reset"))
        assert kind == "timeout"
        assert delay == carousel.TIMEOUT_S

    def test_the_catalogue_and_the_carousel_share_one_table(self):
        """The five names that used to be an inline set are still recognised.

        The set moved rather than changed: a regression here means somebody
        reintroduced a second copy of the same facts.
        """
        for name in (
            "TimeoutError",
            "CancelledError",
            "ReadTimeout",
            "ConnectTimeout",
            "StreamTimeout",
        ):
            reading = catalog.read_exception_class(name)
            assert reading is not None, name
            assert reading.family == catalog.TIMEOUT, name


class TestTheOmissionsAreDeliberate:
    """The rows that are *not* there, and the reason each one is not.

    An omission nobody wrote down gets "fixed" by the next reader, which is
    how a removed pattern comes back. These assert the absence.
    """

    def test_an_authentication_class_never_retires_a_credential(self):
        """The 1.4.0 lesson, defended at a new door.

        ``AuthenticationError`` is the class of every 401 — an expired OAuth
        token, a gateway hiccup, a refresh that was one second from
        succeeding. Reading it as "this key is not a key" retired healthy
        credentials 21 times. It normalises onto ``authentication_error``,
        which *is* ``AUTH_DEAD``, so the only thing standing between the two
        is that the class table is separate and this name is not in it.
        """
        assert catalog.read_exception_class("AuthenticationError") is None

        exc = AuthenticationError("401 Unauthorized")
        verdict = classify(
            error=exc, error_message=str(exc), error_type=type(exc).__name__
        )
        assert verdict is None, "a bare 401 must be left to the host"

    def test_a_permission_class_does_not_bench_for_an_hour_on_its_own(self):
        """A status-less 403 is rare; a wrong one-hour bench is not cheap.

        When the status is present the classifier's own denial path already
        handles it. The class alone cannot tell "wrong model for this tier"
        from "this gateway refused once".
        """
        assert catalog.read_exception_class("PermissionDeniedError") is None

        exc = PermissionDeniedError("403 Forbidden")
        assert (
            classify(error=exc, error_message=str(exc), error_type=type(exc).__name__)
            is None
        )

    def test_the_host_stream_wrapper_does_not_get_a_family_guessed_for_it(self):
        """``ProviderStreamError`` says *how* a failure arrived, not *what*.

        Hermes raises it when a provider encodes an API error as streaming
        content instead of as an SDK error, and synthesizes ``status_code``
        and ``body`` from the parsed event. The evidence is in those fields;
        a family guessed from the wrapper would shadow them.
        """
        assert catalog.read_exception_class("ProviderStreamError") is None

    def test_every_deliberate_omission_is_named_in_the_module(self):
        """The list exists so the reason survives the next reader."""
        for name in catalog._UNMAPPED_ON_PURPOSE:
            assert catalog.read_exception_class(name) is None, name


class TestTheProviderNeverLosesToTheLibrary:
    """A field beats a class, every time, because the provider outranks the SDK."""

    def test_a_billing_code_beats_a_rate_limit_class(self):
        """OpenAI's ``insufficient_quota`` inside a ``RateLimitError``.

        The SDK raises its 429 class; the provider says the balance is empty.
        Waiting does not refill a balance, so the field has to win — and it
        wins because the class is consulted only when the lookups above it
        come back empty.
        """
        exc = RateLimitError("You exceeded your current quota")
        verdict = classify(
            error=exc,
            error_message=str(exc),
            error_type=type(exc).__name__,
            error_body={"error": {"code": "insufficient_quota"}},
        )
        assert verdict is not None
        assert verdict.reason == "billing"

    def test_the_hosts_deeper_code_extraction_is_used(self):
        """``error_code`` arrives already dug out of places KAME does not look.

        The host walks the cause chain and parses JSON nested inside the
        message. Passing the result in is inheriting a better extractor, not
        duplicating one.
        """
        exc = _Bare("something went wrong")
        verdict = classify(
            error=exc, error_message=str(exc), error_code="insufficient_quota"
        )
        assert verdict is not None
        assert verdict.reason == "billing"


class TestTheHookStopsDiscardingWhatItIsGiven:
    def test_the_callback_accepts_error_type_and_error_code_by_name(self):
        """They were in ``**_ignored`` with a comment explaining the loss."""
        import inspect

        plugin = importlib.import_module("hermes-kame-api-rotation")
        signature = inspect.signature(plugin._on_api_error_classification)
        assert "error_type" in signature.parameters
        assert "error_code" in signature.parameters

    def test_an_unknown_future_field_still_cannot_break_dispatch(self):
        """``**_ignored`` stays, so a Hermes that adds a field is survivable."""
        plugin = importlib.import_module("hermes-kame-api-rotation")
        assert plugin._on_api_error_classification(
            provider="custom",
            model="glm",
            status_code=None,
            error_message="hello",
            some_field_invented_in_2027=True,
        ) is None


class TestTheCorpusTaughtTheseTwo:
    """Two payloads 1.4.0 shipped while claiming an error it does not own.

    Neither was introduced by 1.5.0 — running ``tools/host_corpus.py`` against
    the pristine 1.4.0 tree reproduces both. They shipped because the gate was
    never run before the release, which is the same shape of failure as the
    nine days spent debugging a plugin that was not installed: the tool that
    would have said so existed and nobody asked it.
    """

    def test_a_402_that_names_a_way_out_is_not_an_empty_balance(self):
        """``_STATUS_READINGS[402]`` must yield to a stated retry window.

        Hermes' corpus: a 402 whose body says *"Usage limit reached, try again
        in 5 minutes"*. Read on the status alone it is ``billing`` — key
        retired, not retryable, never re-probed. But an empty balance does not
        tell you to come back in five minutes.
        """
        exc = _Bare("inner")
        body = {"error": {"message": "Usage limit reached, try again in 5 minutes"}}
        verdict = classify(
            status_code=402,
            error_message="Usage limit reached, try again in 5 minutes",
            error_body=body,
            error=exc,
        )
        assert verdict is None or verdict.reason != "billing"

    def test_a_402_with_nothing_to_wait_for_is_still_an_empty_balance(self):
        """The guard is narrow: no stated window, and 402 means what it says."""
        exc = _Bare("Payment Required")
        verdict = classify(status_code=402, error_message="Payment Required", error=exc)
        assert verdict is not None
        assert verdict.reason == "billing"

    def test_a_monthly_allowance_simply_gone_is_a_wall(self):
        """"Monthly quota reached." — nothing to wait for, so rotating is the
        only thing that helps. KAME used to call this ``rate_limit`` and
        re-probe hourly against a wall that stands for weeks."""
        exc = _Bare("Monthly quota reached.")
        verdict = classify(
            provider="groq",
            model="llama-3",
            status_code=429,
            error_message="Monthly quota reached.",
            error=exc,
        )
        assert verdict is not None
        assert verdict.reason == "billing"
        assert verdict.retryable is False

    def test_a_weekly_allowance_that_names_its_reset_is_a_wait(self):
        """The other half of the same rule, and the reason it is conditional.

        Codex: *"Weekly usage limit reached. Resets in 6hr 29min."* Same
        window as the case above, opposite verdict, and the only difference is
        that the provider said when it comes back. Calling this billing would
        retire a key that returns this evening.
        """
        exc = _Bare("Weekly usage limit reached. Resets in 6hr 29min.")
        verdict = classify(
            provider="openai-codex",
            model="gpt-5-codex",
            status_code=429,
            error_message="Weekly usage limit reached. Resets in 6hr 29min.",
            error=exc,
        )
        assert verdict is not None
        assert verdict.reason == "rate_limit"
        assert verdict.retryable is True
