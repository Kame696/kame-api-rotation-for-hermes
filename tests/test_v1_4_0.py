"""1.4.0 — the evidence was on the exception the whole time.

Every test here is written against a fact measured on one real pool between
2026-08-17 and 2026-08-26: 276 recorded blocks, 30 recoveries, 20 MB of host
logs. Where a number appears in a docstring it came from that data, not from a
guess about what providers do.

The four findings under test:

* ``getattr(exc, "message", "")`` returned the empty string on every Gemini
  failure there has ever been, because the host's ``GeminiAPIError`` defines no
  such attribute. ``reset_at`` was set on **0 of 276** blocks and 67 % of
  cooldowns were guesses.
* Google's per-minute sentence and OpenAI's out-of-credits sentence are
  identical, and the legacy table read both as a daily cap: **79** hour-long
  benches on a limit that clears in a minute.
* ``Verdict.reason`` said ``rate_limit`` and the escalation ladder spoke only
  ``per_minute``, so an unsized throttle benched a key for **zero seconds**.
* An install missing its whole ``core/`` package published
  ``installed: true, reason: "active"`` and rotated nothing for nine days.
"""

from __future__ import annotations

import importlib
import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"

core_mod = importlib.import_module("hermes-kame-api-rotation.core")
catalog = importlib.import_module("hermes-kame-api-rotation.core.catalog")
classify_mod = importlib.import_module("hermes-kame-api-rotation.core.classify")
carousel = importlib.import_module("hermes-kame-api-rotation.core.carousel")
evidence = importlib.import_module("hermes-kame-api-rotation.core.evidence")
events_mod = importlib.import_module("hermes-kame-api-rotation.core.events")
redact_mod = importlib.import_module("hermes-kame-api-rotation.core.redact")
integrity = importlib.import_module("hermes-kame-api-rotation.integrity")
host_text = importlib.import_module("hermes-kame-api-rotation.host_text")

classify = classify_mod.classify
NOW = 1_787_000_000.0


# --- the host's real exception shape ---------------------------------------

#: Verbatim from `agent/gemini_native_adapter.py`. The point of reproducing it
#: rather than using a mock: `GeminiAPIError` passes its text to
#: `Exception.__init__` and defines **no** `message` attribute. That single
#: fact is what made every sizing attempt fail, and a mock with a `.message`
#: would have hidden it.
class GeminiAPIError(Exception):
    def __init__(self, message, *, code="gemini_api_error", status_code=None,
                 response=None, retry_after=None, details=None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.response = response
        self.retry_after = retry_after
        self.details = details or {}


class UnreadResponse:
    """An httpx.Response that was never read.

    Both `.json()` and `.text` raise, which is the shape that cost 1.3.1 a
    hotfix. `getattr` with a default does not suppress an exception raised by a
    property, so every read has to be inside its own `try`.
    """

    status_code = 429
    headers = {"content-type": "application/json"}

    def json(self):
        raise RuntimeError("ResponseNotRead")

    @property
    def text(self):
        raise RuntimeError("ResponseNotRead")

    @property
    def content(self):
        raise RuntimeError("ResponseNotRead")


#: The host appends this to every free-tier 429. It contains "requests/day",
#: and `quota._PER_DAY_MARKERS` matches "/day".
FREE_TIER_GUIDANCE = (
    "\n\nYour Google API key is on the free tier (a few hundred requests/day "
    "for Gemini Flash models). Hermes typically makes 3-10 API calls per user turn, "
    "so the free tier is exhausted in a handful of messages and cannot sustain "
    "an agent session. Enable billing on your Google Cloud project and "
    "regenerate the key in a billing-enabled project: "
    "https://aistudio.google.com/apikey"
)

GEMINI_PER_MINUTE_BODY = {
    "error": {
        "code": 429,
        "message": (
            "You exceeded your current quota, please check your plan and "
            "billing details."
        ),
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                        "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                        "quotaDimensions": {"location": "global", "model": "gemini-3.7-flash"},
                    }
                ],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "21s"},
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "RATE_LIMIT_EXCEEDED",
                "metadata": {"service": "generativelanguage.googleapis.com"},
            },
        ],
    }
}


class TestTheAttributeThatWasNeverThere:
    """`getattr(exc, "message", "")` — the most expensive line in the plugin."""

    def test_gemini_error_really_has_no_message_attribute(self):
        # The premise. If this ever becomes false upstream the bug is gone and
        # so is the reason for half this release, so it is asserted rather
        # than assumed.
        error = GeminiAPIError("Gemini HTTP 429 (RESOURCE_EXHAUSTED): quota")
        assert getattr(error, "message", "") == ""

    def test_harvest_finds_the_text_anyway(self):
        error = GeminiAPIError("Gemini HTTP 429 (RESOURCE_EXHAUSTED): quota")
        ev = evidence.harvest(error, message=str(error))
        assert "RESOURCE_EXHAUSTED" in ev.message

    def test_harvest_reads_every_field_the_exception_carries(self):
        error = GeminiAPIError(
            "Gemini HTTP 429 (RESOURCE_EXHAUSTED): quota",
            code="gemini_rate_limited",
            status_code=429,
            retry_after=21.0,
            details={"reason": "RATE_LIMIT_EXCEEDED"},
        )
        ev = evidence.harvest(error, message=str(error))
        assert ev.status_code == 429
        assert ev.code == "gemini_rate_limited"
        assert ev.retry_after == 21.0
        assert ev.reason == "RATE_LIMIT_EXCEEDED"
        assert ev.has_structured_evidence()

    def test_a_response_that_was_never_read_costs_nothing(self):
        # 1.3.1 crashed here. Every read is guarded individually, so three
        # raising properties cost three fields and not the harvest.
        error = GeminiAPIError("boom", status_code=429, response=UnreadResponse())
        ev = evidence.harvest(error, message=str(error))
        assert ev.body is None
        assert ev.status_code == 429
        assert ev.headers == {"content-type": "application/json"}

    def test_retry_delay_is_recovered_from_the_body_the_host_discards(self):
        # The host harvests only `google.rpc.ErrorInfo` into `exc.details` and
        # drops `RetryInfo` — and `retry_after` is populated only from a
        # `Retry-After` header, which Gemini does not send. That is why
        # `reset_at` was null on 276 of 276 recorded blocks.
        assert evidence.retry_info_seconds(GEMINI_PER_MINUTE_BODY) == 21.0

    def test_status_is_recovered_from_a_body_that_carries_it(self):
        # 15 of 38 recorded NVIDIA blocks arrived with `status_code=None`.
        error = GeminiAPIError("Too Many Requests")
        ev = evidence.harvest(error, message="Too Many Requests")
        assert ev.status_code is None
        error.body = {"status": 429, "title": "Too Many Requests"}
        ev = evidence.harvest(error, message="Too Many Requests")
        assert ev.status_code == 429


class TestTheHostsOwnHandwriting:
    """79 hour-long benches came from a paragraph Hermes wrote itself."""

    def test_the_guidance_block_is_removed(self):
        message = "You exceeded your current quota." + FREE_TIER_GUIDANCE
        cleaned, removed = evidence.strip_trailing_blocks(message, [FREE_TIER_GUIDANCE])
        assert "requests/day" not in cleaned
        assert removed

    def test_a_per_minute_throttle_survives_the_footer(self):
        # The whole failure, end to end: the host's paragraph says
        # "requests/day", the day-marker table matches "/day", and a
        # sixty-second throttle becomes an hour-long bench on every key in
        # turn. With the block off, the quotaId is what decides.
        error = GeminiAPIError(
            "Gemini HTTP 429 (RESOURCE_EXHAUSTED): You exceeded your current "
            "quota, please check your plan and billing details." + FREE_TIER_GUIDANCE,
            code="gemini_rate_limited",
            status_code=429,
        )
        error.body = GEMINI_PER_MINUTE_BODY
        ev = evidence.harvest(
            error, message=str(error), guidance_blocks=[FREE_TIER_GUIDANCE]
        )
        assert "requests/day" not in ev.message

        verdict = classify(
            provider="gemini",
            model="gemini:gemini-3.7-flash",
            status_code=ev.status_code,
            error_message=ev.message,
            error_body=ev.body,
            headers=ev.headers,
            error=error,
            now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "rate_limit"
        assert verdict.quota_window == "per_minute"
        # 21 seconds, not an hour.
        assert verdict.reset_at is not None
        assert verdict.reset_at - NOW < 120

    def test_a_short_block_is_never_cut(self):
        # Matching too eagerly would eat the provider's own words, which is
        # the one direction that cannot be recovered from.
        cleaned, removed = evidence.strip_trailing_blocks("quota exceeded", ["quota"])
        assert cleaned == "quota exceeded"
        assert removed == []

    def test_the_blocks_are_resolved_from_somewhere(self):
        blocks = host_text.guidance_blocks()
        assert blocks
        assert host_text.guidance_source() in ("fallback",) or \
            host_text.guidance_source().startswith("host:")


class TestOneSentenceTwoMeanings:
    """Google's throttle and OpenAI's empty balance are the same words."""

    SENTENCE = (
        "You exceeded your current quota, please check your plan and billing details."
    )

    def test_google_says_it_for_a_twenty_one_second_throttle(self):
        verdict = classify(
            provider="gemini",
            status_code=429,
            error_message=self.SENTENCE,
            error_body=GEMINI_PER_MINUTE_BODY,
            now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "rate_limit"

    def test_openai_says_it_when_the_balance_is_empty(self):
        verdict = classify(
            provider="openai",
            status_code=429,
            error_message=self.SENTENCE,
            error_body={
                "error": {
                    "message": self.SENTENCE,
                    "type": "insufficient_quota",
                    "code": "insufficient_quota",
                }
            },
            now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "billing"

    def test_the_legacy_table_no_longer_reads_the_sentence_as_daily(self):
        # `DAILY_INDICATORS` carried both "exceeded your current quota" and
        # "billing" until 1.4.0. Between them they turned every Gemini
        # throttle into an hour: 1,088 occurrences of this sentence in nine
        # days, 79 resulting `daily [429] — resting 1h 0m` lines.
        assert "exceeded your current quota" not in carousel.DAILY_INDICATORS
        assert "billing" not in carousel.DAILY_INDICATORS
        _, kind, _ = carousel.classify(None, self.SENTENCE, status_code=429)
        assert kind != "daily"


class TestTheCatalogue:
    """Structured fields, which never collide the way sentences do."""

    def test_nvidias_only_structured_field_is_read(self):
        # `{"status": 429, "title": "Too Many Requests"}` — `title` is all
        # NVIDIA fills in, and nothing read it before 1.4.0.
        reading = catalog.look_up("Too Many Requests")
        assert reading is not None
        assert reading.family == catalog.THROTTLE

    def test_a_wastebasket_never_beats_a_fact(self):
        # Three real payloads, and they do not even agree on which field holds
        # the bucket — which is why the rank is on the value, not the field.
        assert catalog.look_up("INVALID_ARGUMENT", "API_KEY_INVALID").family == catalog.AUTH_DEAD
        assert catalog.look_up("invalid_request_error", "invalid_api_key").family == catalog.AUTH_DEAD
        # DeepSeek inverts OpenAI: the precise name is in `type`.
        assert catalog.look_up("authentication_error", "invalid_request_error").family == catalog.AUTH_DEAD

    def test_a_bare_wastebasket_still_means_a_bad_request(self):
        reading = catalog.look_up("invalid_request_error")
        assert reading is not None
        assert reading.family == catalog.TERMINAL

    def test_the_statuses_a_rotation_engine_ignores(self):
        # 402 Anthropic/DeepSeek, 498 Groq flex capacity, 529 Anthropic
        # overloaded. None of them is in a standard status set, so all three
        # used to land in the generic 20-second bucket.
        assert catalog.look_up_status(402).family == catalog.BILLING
        assert catalog.look_up_status(498).family == catalog.SERVER
        assert catalog.look_up_status(529).family == catalog.SERVER

    def test_alibaba_spells_its_whole_429_family_with_one_word(self):
        assert catalog.look_up("Throttling.RateQuota").family == catalog.THROTTLE
        assert catalog.look_up("Throttling.BurstRate").family == catalog.THROTTLE
        # …except the one that is billing, which is matched exactly and must
        # beat the prefix rule.
        assert catalog.look_up("AllocationQuota.FreeTierOnly").family == catalog.BILLING

    def test_googles_failed_precondition_is_not_a_bad_request(self):
        # 400 FAILED_PRECONDITION means "the free tier is unavailable in your
        # country, enable billing". Read as terminal it ends a turn no
        # rotation could have saved, without saying why.
        assert catalog.look_up("FAILED_PRECONDITION").family == catalog.BILLING

    def test_openrouter_length_errors_are_not_failures(self):
        # The aggregator turns these into a *successful* completion with
        # `finish_reason: length`. Rotating on one spends a healthy key over
        # an answer that arrived.
        assert catalog.look_up("max_tokens_exceeded").family == catalog.NOT_A_FAILURE

    def test_the_quota_id_names_the_window_and_the_scope(self):
        window, scope = catalog.read_quota_id(json.dumps(GEMINI_PER_MINUTE_BODY))
        assert window == "per_minute"
        assert scope == "per_model"

    def test_free_tier_is_a_tier_not_a_window(self):
        window, _ = catalog.read_quota_id('{"quotaId": "SomethingFreeTier"}')
        assert window == "unknown"


class TestRestingZeroSeconds:
    """Two halves of one plugin disagreeing about the name of a failure."""

    def test_rate_limit_is_the_same_family_as_per_minute(self):
        pool = carousel.Carousel()
        applied = pool.mark("p:m", "k1", False, delay=0.0, kind="rate_limit")
        # Zero was what shipped. The floor is not an invented cooldown — it is
        # the smallest rest that cannot spin.
        assert applied >= 1.0

    def test_it_still_escalates_like_a_throttle(self):
        pool = carousel.Carousel()
        first = pool.mark("p:m", "k1", False, delay=0.0, kind="rate_limit")
        second = pool.mark("p:m", "k1", False, delay=0.0, kind="rate_limit")
        assert second > first

    def test_a_provider_that_names_a_number_is_still_obeyed(self):
        pool = carousel.Carousel()
        applied = pool.mark("p:m", "k1", False, delay=21.0, kind="rate_limit")
        assert applied == pytest.approx(21.0)


class TestRedactedBeforeStored:
    """1.1.1 kept no payload; 1.2.9 kept it raw. Neither was the answer."""

    def test_credentials_do_not_survive(self):
        payload = (
            "Invalid key AIzaSyD-1234567890abcdefghijklmnopqrstu and "
            "nvapi-EXAMPLE_NOT_A_REAL_KEY_00000000000000000000 and "
            "sk-EXAMPLE_NOT_A_REAL_KEY_000000000000000000000000"
        )
        cleaned = redact_mod.redact(payload)
        assert "AIzaSyD-1234567890abcdefghijklmnopqrstu" not in cleaned
        assert "nvapi-EXAMPLE_NOT_A_REAL_KEY_00000000000000000000" not in cleaned
        assert "sk-EXAMPLE_NOT_A_REAL_KEY_000000000000000000000000" not in cleaned
        assert redact_mod.looks_redacted(cleaned)

    def test_a_starred_key_is_still_removed(self):
        # Providers redact partially and inconsistently; a shape rule alone
        # would miss `sk-fake-***0000`.
        assert "sk-fake-***0000" not in redact_mod.redact(
            "Incorrect API key provided: sk-fake-***0000."
        )

    def test_a_named_secret_field_goes_whatever_it_holds(self):
        cleaned = redact_mod.redact('{"api_key": "short", "model": "gemini-3.7-flash"}')
        assert "short" not in cleaned
        assert "gemini-3.7-flash" in cleaned

    def test_the_evidence_is_kept(self):
        # A screen that hides the evidence to protect the secret protects
        # nothing — the secret was never in the evidence.
        payload = (
            "Gemini HTTP 429 (RESOURCE_EXHAUSTED): quota exceeded. "
            "quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier, "
            "retryDelay: 21s"
        )
        cleaned = redact_mod.redact(payload)
        assert "RESOURCE_EXHAUSTED" in cleaned
        assert "PerMinute" in cleaned
        assert "21s" in cleaned

    def test_an_echoed_prompt_is_bounded(self):
        cleaned = redact_mod.redact("error: " + ("secret prompt text " * 200))
        assert len(cleaned) <= redact_mod.DEFAULT_LIMIT + 8

    def test_the_event_store_scrubs_on_the_way_in(self):
        store = events_mod.Events()
        row = store.add(
            "rotation",
            identity="gemini:gemini-3.7-flash",
            key="AIzaSy…q7R8",
            reason="rate_limit",
            detail="key AIzaSyD-1234567890abcdefghijklmnopqrstu refused",
            sized_by="retryinfo",
        )
        assert "AIzaSyD-1234567890abcdefghijklmnopqrstu" not in row["detail"]
        assert row["sized_by"] == "retryinfo"

    def test_redact_never_raises(self):
        class Hostile:
            def __str__(self):
                raise RuntimeError("no")

        assert redact_mod.redact(Hostile()) == ""


class TestTheInstallCannotLieAnyMore:
    """`installed: true, reason: "active"` on a plugin with no engine."""

    def test_this_install_is_complete(self):
        report = integrity.verify(str(PLUGIN_DIR))
        assert report["complete"], report["missing_required"]
        assert report["fingerprint"] != "unknown"
        assert len(report["fingerprint"]) == 12

    def test_a_missing_core_is_named(self, tmp_path):
        # Exactly the shape found on disk: every root module present, the
        # whole `core/` package gone, because the copy did not recurse.
        for name in integrity.REQUIRED_MODULES:
            if name.startswith("core/"):
                continue
            (tmp_path / name).write_text("# stub\n", encoding="utf-8")
        report = integrity.verify(str(tmp_path))
        assert report["complete"] is False
        assert any(m.startswith("core/") for m in report["missing_required"])

    def test_the_sentence_says_what_to_do(self):
        report = {"complete": False, "missing_required": ["core/carousel.py"],
                  "root": "/somewhere", "fingerprint": "abc"}
        described = integrity.describe(report)
        assert "core/carousel.py" in described
        assert "recurse" in described

    def test_the_fingerprint_follows_the_bytes(self, tmp_path):
        (tmp_path / "a.py").write_text("one", encoding="utf-8")
        before = integrity.fingerprint(str(tmp_path))
        (tmp_path / "a.py").write_text("two", encoding="utf-8")
        assert integrity.fingerprint(str(tmp_path)) != before

    def test_a_missing_file_changes_it_too(self, tmp_path):
        (tmp_path / "a.py").write_text("one", encoding="utf-8")
        (tmp_path / "b.py").write_text("two", encoding="utf-8")
        before = integrity.fingerprint(str(tmp_path))
        (tmp_path / "b.py").unlink()
        # The failure this exists for: a manifest a partial copy updated as
        # faithfully as a complete one.
        assert integrity.fingerprint(str(tmp_path)) != before

    def test_bytecode_is_not_the_source(self, tmp_path):
        # On the broken install the `.pyc` files were stale relative to their
        # own sources and had been flattened out of `__pycache__`, so counting
        # them would describe neither.
        (tmp_path / "a.py").write_text("one", encoding="utf-8")
        before = integrity.fingerprint(str(tmp_path))
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "a.cpython-314.pyc").write_bytes(b"\x00\x01")
        assert integrity.fingerprint(str(tmp_path)) == before


class TestTheReleaseIsConsistent:
    def test_the_version_is_the_same_everywhere(self):
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        # Pinned as a floor, not an equality: this file asserts that 1.4.0's
        # rules are still in force, and a later release must be free to ship
        # without editing every version file behind it.
        assert tuple(int(part) for part in core_mod.__version__.split(".")) >= (1, 4, 0)
        assert f'version: "{core_mod.__version__}"' in manifest
        assert f"## [{core_mod.__version__}]" in changelog
        assert "manifest_version: 1" in manifest

    def test_there_is_only_one_changelog(self):
        # 1.3.x wrote its entries into a second file next to the plugin, so
        # the version-agreement check above could not see them and the two
        # disagreed for a week.
        assert not (PLUGIN_DIR / "CHANGELOG.md").exists()

    def test_the_fingerprint_hashes_exactly_what_ships(self):
        # The published repository keeps the plugin flattened at its root with
        # `tests/` and `tools/` beside it; the archive built from that same
        # checkout contains neither. If the two skip lists drift, a checkout
        # and its own zip fingerprint differently -- and a handshake that
        # disagrees with itself when nothing is wrong is a handshake people
        # learn to ignore, which is the entire failure this release exists to
        # end.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "kame_packager", ROOT / "tools" / "package.py"
        )
        packager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(packager)

        assert packager.SKIP_DIRS <= set(integrity._NOT_SHIPPED)
        assert set(integrity._NOT_SHIPPED) - packager.SKIP_DIRS == {
            "tests",
            "tools",
        }

    def test_a_checkout_and_its_own_archive_agree(self):
        # The claim above, made against the real bytes rather than against two
        # constants that happen to look alike.
        import importlib.util
        import shutil
        import tempfile
        import zipfile

        spec = importlib.util.spec_from_file_location(
            "kame_packager2", ROOT / "tools" / "package.py"
        )
        packager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(packager)

        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "plugin.zip"
            with zipfile.ZipFile(archive, "w") as out:
                for path in packager.shipped_files():
                    out.write(path, path.relative_to(packager.SOURCE.parent).as_posix())
            zipfile.ZipFile(archive).extractall(tmp)
            unpacked = Path(tmp) / packager.SOURCE.name

            # And the flattened layout the published repo actually uses: the
            # plugin at the root with the proof trees beside it.
            flat = Path(tmp) / "flat"
            shutil.copytree(unpacked, flat)
            (flat / "tests").mkdir()
            (flat / "tests" / "test_noise.py").write_text("x = 1\n", encoding="utf-8")
            (flat / "tools").mkdir()
            (flat / "tools" / "noise.py").write_text("y = 2\n", encoding="utf-8")

            assert integrity.fingerprint(str(unpacked)) == integrity.fingerprint()
            assert integrity.fingerprint(str(flat)) == integrity.fingerprint()
