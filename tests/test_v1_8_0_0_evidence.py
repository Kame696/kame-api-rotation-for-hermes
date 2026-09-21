"""Two independent evidence gaps 1.8.0.0 closes, both settled in decisions/0004.

**Change 1 — D3.** 1.7.0.7 added a blanket `if provider_timed(source):
return None/0` to `escalate.stretch` and `journal.short_streak`, plus a third
copy inside `short_streak`'s own backward walk (`if
provider_timed(previous.source): break`). Measured on the owner's corpus that
guard blocked 31 of ~530 widenings — and those 31 were exactly the case R13
authorizes: a stated deadline served in full, refused again. All three copies
are removed here. The two *narrower* chain breaks 1.7.0.7 also added — a
previous entry from a different failure family, and an anchor-vs-estimated-
duration mismatch — are sound and stay, verified below alongside the removal.

**Change 2 — D1's consequence.** `recorder.py` never wrote response headers,
so D1 (a stated 5xx deadline is obeyed under the owner's ceiling) had zero
evidence in either direction: 1,387 real refusals and not one carried a
`Retry-After`. This file also covers the allowlisted, redacted, capped header
capture that closes that gap.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"

# Change 1 needs only the framework-free `core` modules, imported bare — the
# same way tests/test_escalation.py does it, since journal/escalate/
# vocabulary's relative imports never reach outside `core`.
sys.path.insert(0, str(PLUGIN_DIR))

from core import escalate  # noqa: E402
from core import journal as journal_mod  # noqa: E402
from core import vocabulary  # noqa: E402

# Change 2 needs the whole plugin package — recorder.py reaches for `.
# state` and `. settings`, both siblings one level up from `core`.
sys.path.insert(0, str(ROOT))
from tests.test_binding import PACKAGE  # noqa: E402  (loads the package once)

recorder = importlib.import_module(f"{PACKAGE}.recorder")


NOW = 1_000_000.0


def _block(journal, *, at, reset_at, source="header", reason="rate_limit", window="per_minute"):
    return journal.record_block(
        at=at, provider="gemini", model="gemini:model", credential_id="key:a",
        status_code=429, window=window, source=source, reset_at=reset_at,
        sized_by=journal_mod.SIZED_BY_KAME, reason=reason,
    )


# ---------------------------------------------------------------------------
# Change 1 — decision 0004 D3
# ---------------------------------------------------------------------------


class TestTheGuardIsGoneButTheTwoStrikeRuleStillDecides:
    def test_two_stated_deadlines_served_in_full_and_refused_again_widen(self):
        """The exact 31-of-530 case R13 authorizes: every block here carries a
        freshly stated ("header") duration, and the guard 1.7.0.7 added would
        have zeroed this to 0 and blocked `stretch` outright. Removed: this
        must now behave exactly like the same sequence under `source="table"`
        already covered by test_replay_timeline.py's
        `test_two_on_time_same_window_refusals_produce_a_widening`.
        """
        book = journal_mod.Journal()
        _block(book, at=1000.0, reset_at=1020.0)
        _block(book, at=1025.0, reset_at=1045.0)

        strikes = journal_mod.short_streak(
            book, credential_id="key:a", model="gemini:model", window="per_minute",
            at=1046.0, source="header", reason="rate_limit",
        )
        assert strikes == 2, "a stated deadline served in full, refused twice, is a strike"
        assert vocabulary.provider_timed("header") is True  # the source really is fresh

        stretched = escalate.stretch(
            reset_at=1066.0, now=1046.0, strikes=strikes, window="per_minute",
            reason="rate_limit", source="header",
        )
        assert stretched is not None, "stretch must no longer refuse a provider-timed source"
        assert stretched - 1046.0 == pytest.approx(40.0)  # factor_for(2) == 2.0, 20s * 2

    def test_a_run_of_fresh_deadlines_that_were_never_served_in_full_does_not_widen(self):
        """R10/R12 guard: repetition alone must never multiply a stated number.
        Three refusals, each carrying its own fresh ``header`` deadline, but
        each one landing *seconds* after the previous rather than after that
        deadline actually lapsed — nothing here was ever waited out, so
        nothing may count as a strike, guard or no guard.
        """
        book = journal_mod.Journal()
        _block(book, at=1000.0, reset_at=1020.0)   # stated: wait until 1020
        _block(book, at=1005.0, reset_at=1025.0)   # arrived 15s early, not after 1020

        strikes = journal_mod.short_streak(
            book, credential_id="key:a", model="gemini:model", window="per_minute",
            at=1010.0, source="header", reason="rate_limit",   # also arrives before 1025
        )
        assert strikes == 0, "landing before a deadline lapses proves nothing about it"

        stretched = escalate.stretch(
            reset_at=1030.0, now=1010.0, strikes=strikes, window="per_minute",
            reason="rate_limit", source="header",
        )
        assert stretched is None

    def test_a_different_failure_family_still_breaks_the_chain(self):
        """1.7.0.7's own narrower break, kept: a daily-cap block cannot lend
        its history to a rate-limit streak just because both landed on the
        same (credential, model, window).
        """
        book = journal_mod.Journal()
        _block(book, at=1000.0, reset_at=1020.0, reason="daily_cap")
        _block(book, at=1025.0, reset_at=1045.0, reason="rate_limit")

        strikes = journal_mod.short_streak(
            book, credential_id="key:a", model="gemini:model", window="per_minute",
            at=1046.0, source="header", reason="rate_limit",
        )
        # The newest block (reason="rate_limit") counts once; walking one
        # further back hits the daily_cap block and the family break stops it.
        assert strikes == 1

    def test_an_anchor_vs_estimated_mismatch_still_breaks_the_chain(self):
        """1.7.0.7's other narrower break, kept: a calendar anchor and an
        estimated stopwatch duration are different kinds of evidence even
        when they land on the same window, and must not be chained together.
        """
        book = journal_mod.Journal()
        _block(book, at=1000.0, reset_at=1020.0, source="anchor")
        _block(book, at=1025.0, reset_at=1045.0, source="header")

        strikes = journal_mod.short_streak(
            book, credential_id="key:a", model="gemini:model", window="per_minute",
            at=1046.0, source="header", reason="rate_limit",
        )
        assert strikes == 1  # the newest block only; the anchor block breaks the walk

    def test_provider_timed_still_reports_correctly_for_the_replay_tools_measurement(self):
        """`provider_timed` itself is not removed — only its use as a guard is.
        `tools/replay_timeline.py` still calls it to *measure*
        `blocked_by_provider_timed_guard`, never again to gate one.
        """
        assert vocabulary.provider_timed("header") is True
        assert vocabulary.provider_timed("body.retryDelay") is True
        assert vocabulary.provider_timed("table") is False
        assert vocabulary.provider_timed("window") is False


# ---------------------------------------------------------------------------
# Change 2 — decision 0004 D1's consequence: recorder.py keeps headers
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, headers):
        self.headers = headers


class _Error(Exception):
    def __init__(self, headers=None, response_headers=None, response=None):
        super().__init__("refused")
        if headers is not None:
            self.headers = headers
        if response_headers is not None:
            self.response_headers = response_headers
        if response is not None:
            self.response = response


class TestRecorderKeepsHeadersAllowlistedAndRedacted:
    def _record_one(self, monkeypatch, tmp_path, **kwargs):
        out = tmp_path / "refusals.jsonl"
        monkeypatch.setattr(recorder, "_destination", lambda: str(out))
        monkeypatch.setattr(recorder, "_silenced", False)
        monkeypatch.setenv("KAME_RECORDER_DISABLED", "0")
        recorder.record(provider="gemini", model="gemini:model", status_code=429, **kwargs)
        lines = out.read_text(encoding="utf-8").splitlines() if out.exists() else []
        assert len(lines) == 1, "exactly one refusal was recorded"
        return json.loads(lines[0])

    def test_retry_and_rate_limit_headers_survive(self, monkeypatch, tmp_path):
        headers = {
            "Retry-After": "37",
            "X-RateLimit-Remaining-Requests": "0",
            "X-RateLimit-Reset-Requests": "12s",
            "anthropic-ratelimit-tokens-reset": "2026-09-16T00:00:00Z",
            "Date": "Wed, 16 Sep 2026 07:28:00 GMT",
        }
        row = self._record_one(monkeypatch, tmp_path, error=_Error(headers=headers))
        assert row["headers"]["retry-after"] == "37"
        assert row["headers"]["x-ratelimit-remaining-requests"] == "0"
        assert row["headers"]["x-ratelimit-reset-requests"] == "12s"
        assert row["headers"]["anthropic-ratelimit-tokens-reset"] == "2026-09-16T00:00:00Z"
        assert row["headers"]["date"] == "Wed, 16 Sep 2026 07:28:00 GMT"

    def test_authorization_header_never_reaches_disk_in_any_casing(self, monkeypatch, tmp_path):
        secret = "Bearer sk-ant-abcdefghijklmnopqrstuvwx0123456789"
        headers = {
            "authorization": secret,
            "Authorization": secret,
            "AUTHORIZATION": secret,
            "X-Api-Key": "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456",
            "x-goog-api-key": "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456",
            "Cookie": "session=abcdefghijklmnopqrstuvwxyz0123456789",
            "Set-Cookie": "session=abcdefghijklmnopqrstuvwxyz0123456789",
            "Retry-After": "5",  # control: proves headers were processed at all
        }
        row = self._record_one(monkeypatch, tmp_path, error=_Error(headers=headers))
        kept_names = set(row["headers"].keys())
        assert kept_names == {"retry-after"}
        raw = json.dumps(row)
        assert secret not in raw
        assert "sk-ant-" not in raw
        assert "AIzaSy" not in raw
        assert "abcdefghijklmnopqrstuvwxyz0123456789" not in raw

    def test_a_long_opaque_token_value_is_dropped(self, monkeypatch, tmp_path):
        opaque = "Zx9" + "q" * 60  # allowlisted name, credential-shaped value
        headers = {
            "X-RateLimit-Reset-Requests": opaque,
            "Retry-After": "5",
        }
        row = self._record_one(monkeypatch, tmp_path, error=_Error(headers=headers))
        assert "x-ratelimit-reset-requests" not in row["headers"]
        assert row["headers"]["retry-after"] == "5"
        assert opaque not in json.dumps(row)

    def test_the_size_and_count_caps_hold(self, monkeypatch, tmp_path):
        # Retry-After goes in first: the cap must still apply across the
        # whole set, and this keeps the assertion below independent of
        # exactly which 20 of the 41 raw headers survive the count cap.
        headers = {"Retry-After": "A " + ("z " * 400)}  # long, but not opaque (spaces)
        headers.update({f"X-RateLimit-Reset-Slot-{i}": "1" for i in range(40)})
        row = self._record_one(monkeypatch, tmp_path, error=_Error(headers=headers))
        assert len(row["headers"]) <= recorder.MAX_HEADERS
        assert len(row["headers"]["retry-after"]) <= recorder.MAX_HEADER_VALUE_LEN

    def test_a_refusal_with_no_headers_records_exactly_what_it_did_before(self, monkeypatch, tmp_path):
        row = self._record_one(
            monkeypatch, tmp_path,
            error_message="rate limited", error_body={"error": "slow down"},
            error=_Error(), error_type="RateLimitError",
        )
        assert "headers" not in row
        assert set(row.keys()) == {
            "at", "provider", "model", "status", "type", "code", "message", "body", "response",
        }
