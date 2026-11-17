"""1.9.0.2 (H4) - the wait is said in the provider's words; nothing waits or stops differently.

Live (audit/2026-11-15-pre-1902-bug-hunt.md, F5): the owner watched minutes of "trying the next key"
with no estimate. Two shapes were behind it. A refusal that names no time at all (74% of Gemini's
429s since 2026-11-01): KAME has no estimate, and now says that, after a minute of every key refusing
that way. A spent day that Google DOES date (``retryDelay`` up to the next midnight US/Pacific -
68,173 s seen on 2026-11-15) while ``max_hold_seconds`` caps the bench at an hour: the line said "next
back in 54m" and never the provider's own time; now it says both.
"""
from __future__ import annotations

import threading
import time

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_9_0_2_cancel as C

transport = R.transport
fresh = R.fresh
KEYS = R.KEYS[:2]


def per_day(delay_s="68173s"):
    return R.Refusal(429, "You exceeded your current quota, please check your plan and billing details.", [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                         "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay_s}])


class Refusing(C.RefusingCall):
    def __init__(self, refusal, keys=KEYS):
        super().__init__(keys=keys)
        self.refusal = refusal

    def client_for(self, key, attempt):
        def create(**request):
            with self.lock:
                self.starts.append(time.monotonic())
            raise self.refusal()
        return R.SimpleNamespace(chat=R.SimpleNamespace(completions=R.SimpleNamespace(create=create)))


@pytest.fixture()
def lines(monkeypatch):
    seen = []
    original = transport._Spinner.update

    def capture(owner, text, *a, **k):
        seen.append((time.monotonic(), str(text)))
        try:
            return original(owner, text, *a, **k)
        except Exception:
            return None

    monkeypatch.setattr(transport._Spinner, "update", staticmethod(capture))
    return seen


def _run(t, call, stop_after):
    C._cancel_after(call, stop_after)
    C._drive(t, call)


class TestNoTimeGiven:
    def test_after_the_bound_every_key_refused_with_no_time_is_said(self, fresh, lines):
        fresh.NO_TIME_AFTER_S = 0.3
        _run(fresh, Refusing(C.bare_429), 2.6)
        said = [text for _, text in lines if "the provider gave no time" in text]
        assert said, [text for _, text in lines][-5:]
        assert said[0].startswith("↻") and "every key refused for" in said[0]

    def test_before_the_bound_the_line_is_unchanged(self, fresh, lines):
        _run(fresh, Refusing(C.bare_429), 1.5)          # the default bound is a minute
        assert not [text for _, text in lines if "gave no time" in text]
        assert [text for _, text in lines if "trying the next key" in text]

    def test_a_refusal_that_names_its_time_is_not_called_timeless(self, fresh, lines):
        fresh.NO_TIME_AFTER_S = 0.0
        _run(fresh, Refusing(R.per_minute), 1.5)
        assert not [text for _, text in lines if "gave no time" in text]


class TestTheProvidersOwnTime:
    def test_a_dated_spent_day_is_said_with_the_providers_time(self, fresh, lines):
        call = Refusing(lambda: per_day("68173s"))
        _run(fresh, call, 1.2)
        waits = [text for _, text in lines if "next key in" in text]
        assert waits, [text for _, text in lines][-5:]
        assert any("Gemini says the quota is back around" in text for text in waits)

    def test_a_short_stated_time_adds_nothing(self, fresh, lines):
        # The provider's time is not later than KAME's next try: nothing to add.
        call = Refusing(lambda: per_day("40s"))
        _run(fresh, call, 1.2)
        assert not [text for _, text in lines if "says the quota is back" in text]

    def test_the_handover_message_carries_the_providers_time_but_the_event_row_does_not(self, fresh):
        fresh._stated_back[(R.IDENTITY, KEYS[0])] = time.time() + 68173
        fresh._stated_back[(R.IDENTITY, KEYS[1])] = time.time() + 70000
        note = fresh._provider_back_note(R.IDENTITY, KEYS, 3500.0)
        assert note.startswith(" - Gemini says the quota is back around ")
        assert fresh._provider_back_note(R.IDENTITY, KEYS + ["k3"], 3500.0) == ""   # one key undated
