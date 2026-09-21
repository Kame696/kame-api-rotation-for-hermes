"""1.6.0.2 — three ways weaker evidence was overruling stronger evidence.

1.6.0.1 was measured in production and the measurement is the whole reason
this release exists. On 2026-09-03 the owner's Hermes made 64 calls, rotated
133 times, and asked for help about the waits it was showing. The log said
KAME was resting keys for one and five seconds. The panel showed credentials
held for minutes. **Both were true**, and the gap between them is this file.

--------------------------------------------------------------------------
One: silence is not neutrality
--------------------------------------------------------------------------

Nineteen of that run's refusals were this, verbatim:

    Gemini HTTP 429 (RESOURCE_EXHAUSTED): Resource has been exhausted
    (e.g. check quota).

No stated delay, no quota id, no metric. ``classify`` recognises it — a spent
per-credential counter — and deliberately leaves ``reset_at`` unset, saying so
in a comment: *"bench it for nothing"*. That reasoning is correct about one of
the two clocks and was never checked against the other:

* ``dispatch_binding`` uses the verdict's ``reset_at`` as the rest between
  attempts **inside** a turn. Unset means ``_escalate``'s one-second floor,
  which is what NVIDIA's seconds-long burst limits need and what 1.5.0 tuned.
* ``agent/credential_pool`` benches the credential **across** turns. Unset
  does not mean "no bench" there. ``_exhausted_until`` (:426) reads the
  deadline KAME supplied and, finding none, applies ``_exhausted_ttl``
  (:333) — ``EXHAUSTED_TTL_429_SECONDS = 60 * 60``.

So the same silence bought one second in one place and **one hour** in the
other. The fix is at the seam that owns the second clock — a short floor in
``PoolBinding._carry_deadline`` — and not in ``classify``, because changing
the verdict would have reintroduced the NVIDIA ladder that 1.5.0 removed. The
whole unit suite passing unchanged is the evidence that the classifier's
contract was not disturbed.

--------------------------------------------------------------------------
Two: the host's own voice, read as the provider's
--------------------------------------------------------------------------

``agent/gemini_native_adapter`` appends advice to the error message before any
hook sees it (:907, :913). Two of ``_BILLING_PATTERNS`` match that advice:

    "...so the free tier is exhausted in a handful of messages..."
    "...regenerate the key in a billing-enabled project..."

The first pattern was written for Alibaba Model Studio, where "the free tier of
the model has been exhausted" is a real and decisive account fact. Hermes says
the same words about the *user's* situation, and no pattern can separate them,
because the difference is not in the words — it is in who wrote them.

So a 429 whose payload said "Please retry in 6.89161299s" was classified as an
empty account and benched for an hour at **account** scope, on sentences Google
never wrote. It is not weak evidence. It is not evidence: the host is not a
party to the failure. Appended 340 times in one week of the owner's logs.

--------------------------------------------------------------------------
Three: the SDK's class name, read before the provider's words
--------------------------------------------------------------------------

``read_exception_class`` sat as a third equal in the same ``or`` chain as the
field lookup and the status lookup, which gave it the power to end the
classification for the four families KAME does not act on — **before a single
pattern read the provider's sentence**. Measured:

    400 "API key not valid. Please pass a valid API key."
        + error_type "BadRequestError"    -> declined
    402 "Usage limit reached, try again in 5 minutes"
        + error_type "APIStatusError"     -> declined

The first is Google's only way of saying a key is revoked, so a genuinely dead
credential could never be retired. The second is the payload 1.5.0 shipped a
fix for, silently dead ever since because the class name got there first.

--------------------------------------------------------------------------
The principle
--------------------------------------------------------------------------

One rule covers all three, and the module already half-stated it: *a field
outranks a sentence, and the provider outranks the library*. 1.6.0.2 finishes
it:

    1. a machine-readable field the provider filled in
    2. a number the provider stated
    3. the provider's own sentence
    4. the HTTP status on its own
    5. the SDK exception class name
    6. text the **host** appended  <- not evidence at all

Anything that lets a lower row overturn a higher one is a defect, whatever
number it produces.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1602_under_test"


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


plugin = _load_package()
classify_mod = importlib.import_module(f"{PACKAGE}.core.classify")
quota = importlib.import_module(f"{PACKAGE}.core.quota")
catalog = importlib.import_module(f"{PACKAGE}.core.catalog")
runtime = importlib.import_module(f"{PACKAGE}.runtime")
pool_binding = importlib.import_module(f"{PACKAGE}.pool_binding")

classify = classify_mod.classify
strip_host_prose = classify_mod.strip_host_prose
FLOOR = quota.DEFAULT_UNSIZED_THROTTLE_BENCH_SECONDS

NOW = 1_700_000_000.0

# --------------------------------------------------------------------------
# The host's own text, copied from the installed Hermes rather than described.
# ``tools/host_prose.py`` is the gate that keeps these honest against an
# upgrade; this copy is what the assertions below run against.
# --------------------------------------------------------------------------

FREE_TIER_FOOTER = (
    "\n\nYour Google API key is on the free tier (a few hundred requests/day "
    "for Gemini Flash models). Hermes typically makes 3-10 API calls per user turn, "
    "so the free tier is exhausted in a handful of messages and cannot sustain "
    "an agent session. Enable billing on your Google Cloud project and "
    "regenerate the key in a billing-enabled project: "
    "https://aistudio.google.com/apikey"
)

STANDARD_KEY_FOOTER = (
    "\n\nGoogle Gemini rejected this API key's type — you do NOT need OAuth. "
    "Google began rejecting legacy 'Standard' Google Cloud keys for the "
    "Gemini API on June 19, 2026, and all Standard keys stop working in "
    "September 2026. Open https://aistudio.google.com/api-keys, check the "
    "key's type and status, and create a replacement Gemini API key (or, as "
    "a temporary bridge, restrict the Standard key to "
    "generativelanguage.googleapis.com). Then update GEMINI_API_KEY / "
    "GOOGLE_API_KEY in ~/.hermes/.env and restart your session. "
    "Details: https://ai.google.dev/gemini-api/docs/api-key"
)

#: The refusal the owner's run actually produced, nineteen times.
RUN_429 = ("Gemini HTTP 429 (RESOURCE_EXHAUSTED): Resource has been exhausted "
           "(e.g. check quota).")

#: Google's free-tier 429 when it *does* state a delay.
GEMINI_SIZED = (
    "Gemini HTTP 429 (RESOURCE_EXHAUSTED): You exceeded your current quota, "
    "please check your plan and billing details. For more information on this "
    "error, head to: https://ai.google.dev/gemini-api/docs/rate-limits.\n"
    "* Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_input_token_count, limit: 250000, model: "
    "gemini-3.7-flash\nPlease retry in 6.89161299s."
)


def verdict(message, *, status=429, error_type="GeminiAPIError",
            error_code="gemini_rate_limited", provider="gemini",
            model="gemini-3.7-flash", **extra):
    return classify(
        provider=provider, model=model, status_code=status,
        error_message=message, error_type=error_type, error_code=error_code,
        now_epoch=NOW, **extra,
    )


def seconds(v):
    return None if v is None or v.reset_at is None else round(v.reset_at - NOW, 1)


# ==========================================================================
# A. The host's prose is not evidence
# ==========================================================================

class TestTheHostsOwnVoice:
    """Hermes talking to the user is not Google describing a failure."""

    def test_a_stated_delay_survives_the_footer(self):
        """The measured defect, in one assertion.

        Same payload, twice. The provider said seven seconds both times; the
        only difference is text Hermes appended for the user's benefit.
        """
        bare = verdict(GEMINI_SIZED)
        footed = verdict(GEMINI_SIZED + FREE_TIER_FOOTER)
        assert bare is not None and footed is not None
        assert footed.reason == bare.reason == "rate_limit"
        assert seconds(footed) == seconds(bare) == pytest.approx(6.9, abs=0.1)

    def test_the_footer_alone_claims_nothing(self):
        """It needed no help from the provider's half of the string.

        On a neutral 500 the free-tier footer used to return ``billing`` all
        by itself — an hour, at account scope, with the re-probe and the
        escalation both disarmed by the reason. That is the clearest possible
        statement that the classifier was reading the wrong author.
        """
        assert verdict("Internal error." + FREE_TIER_FOOTER,
                       status=500, error_code="") is None
        assert verdict("Internal error." + STANDARD_KEY_FOOTER,
                       status=500, error_code="") is None

    def test_the_footer_never_reaches_a_pattern(self):
        """Stripping happens before classification, not inside one branch."""
        assert strip_host_prose("Boom." + FREE_TIER_FOOTER) == "Boom."
        assert strip_host_prose("Boom." + STANDARD_KEY_FOOTER) == "Boom."
        assert strip_host_prose("") == ""
        assert strip_host_prose("nothing to strip") == "nothing to strip"

    def test_it_removes_the_host_text_and_nothing_else(self):
        """A footer in the middle takes its tail and leaves the head alone."""
        kept = strip_host_prose(GEMINI_SIZED + FREE_TIER_FOOTER)
        assert kept == GEMINI_SIZED
        assert "6.89161299" in kept
        assert "Enable billing" not in kept

    def test_the_billing_sentences_are_what_it_cost(self):
        """Naming the mechanism, so a reader can check the claim.

        Two of ``_BILLING_PATTERNS`` match the host's footer, and the second
        is the instructive one: ``free tier ... exhausted`` was written for
        Alibaba's ``AllocationQuota.FreeTierOnly``, where it is a real and
        decisive account fact. Hermes' footer says the same words about the
        user's situation. The pattern cannot separate them because the
        difference is not in the words — it is in who wrote them, which is
        exactly why the fix is to remove the host's text rather than to
        narrow the pattern.

        Asserted rather than described so that a reworded pattern turns the
        comment above into a failing test instead of a lie.
        """
        for sentence in ("so the free tier is exhausted in a handful of messages",
                         "regenerate the key in a billing-enabled project"):
            assert classify_mod._matches(
                classify_mod._BILLING_PATTERNS, sentence, ""
            ), sentence


# ==========================================================================
# B. The SDK class name is the weakest machine-readable evidence
# ==========================================================================

class TestTheClassNameComesLast:

    def test_a_revoked_key_is_seen_through_BadRequestError(self):
        """Google's only way of saying a key is dead, in its usual wrapper."""
        v = verdict("API key not valid. Please pass a valid API key.",
                    status=400, error_type="BadRequestError", error_code="")
        assert v is not None, "the class name was deciding for the provider"
        assert v.reason == "auth_permanent"

    def test_a_stated_wait_is_seen_through_APIStatusError(self):
        """1.5.0's own fix, restored.

        ``APIStatusError`` maps to ``server`` in the class table, so this
        payload — the one 1.5.0 shipped ``look_up_status``'s billing override
        for — was being declined before that override could matter.
        """
        v = verdict("Usage limit reached, try again in 5 minutes",
                    status=402, error_type="APIStatusError", error_code="")
        assert v is not None
        assert seconds(v) == pytest.approx(300.0, abs=1.0)

    def test_a_malformed_request_is_still_left_alone(self):
        """The other half. The same class, a sentence that means what it says.

        This is the assertion that makes the one above safe: the difference
        between a revoked key and a bad payload was always in the sentence,
        never in the class, and both readings have to survive.
        """
        assert verdict("Invalid JSON payload", status=400,
                       error_type="BadRequestError", error_code="") is None
        assert verdict("model `gemini-99` not found", status=404,
                       error_type="NotFoundError", error_code="") is None

    def test_a_bare_rate_limit_class_still_answers(self):
        """The row that earns the class table its place is untouched.

        A ``RateLimitError`` with an empty body is a throttle and nothing else
        says so. Actionable families keep their position ahead of the
        patterns; only the four "stay out of it" families were moved.
        """
        v = verdict("Rate limit reached", status=429,
                    error_type="RateLimitError", error_code="")
        assert v is not None and v.reason == "rate_limit"

    def test_the_four_families_that_yield_are_named(self):
        """A rule stated in a set, not spread across branches."""
        for name, family in (
            ("BadRequestError", catalog.TERMINAL),
            ("NotFoundError", catalog.TERMINAL),
            ("APIStatusError", catalog.SERVER),
            ("InternalServerError", catalog.SERVER),
            ("APITimeoutError", catalog.TIMEOUT),
            ("APIConnectionError", catalog.TIMEOUT),
        ):
            reading = catalog.read_exception_class(name)
            assert reading is not None, name
            assert reading.family == family, name


# ==========================================================================
# C. Silence is not neutrality — the pool floor
# ==========================================================================

class TestTheUnsizedThrottleFloor:
    """The clock the 1.5.0 comment never looked at."""

    def test_the_classifier_still_names_no_number(self):
        """Deliberate, and load-bearing.

        The in-turn rest reads this. Putting a number here is what would
        rebuild the NVIDIA ladder — 20s, 40s, 1m20s — that 1.5.0 removed, so
        the fix is at the pool seam and this assertion is what keeps it there.
        """
        v = verdict(RUN_429)
        assert v is not None
        assert v.reason == "rate_limit"
        assert v.reset_at is None

    def test_the_pool_gets_a_number_anyway(self):
        """What the credential is actually held for.

        Without this the host reads no deadline and applies
        ``EXHAUSTED_TTL_429_SECONDS`` — one hour, per key, on the commonest
        refusal there is.
        """
        judgement = runtime.Judgement(
            provider="gemini", model="gemini-3.7-flash", window="unknown",
            source="catalog", reset_at=None, at=NOW, reason="rate_limit",
        )
        floor = pool_binding.PoolBinding._floor_for_unsized(judgement, NOW)
        assert floor == pytest.approx(NOW + FLOOR)

    def test_the_floor_is_seconds_not_an_hour(self):
        """The number itself, checked against what it is replacing."""
        assert 0 < FLOOR <= 60, "a floor this long is the bug it replaces"
        assert FLOOR < 3600

    @pytest.mark.parametrize("reason", ["auth_permanent", "billing", "auth", ""])
    def test_only_a_throttle_gets_the_floor(self, reason):
        """Everything else keeps the host's fallback exactly as it was.

        ``auth_permanent`` is the one that matters: a credential the provider
        named dead is *meant* to sit out, and ``_is_terminal_auth_failure``
        reads the very context this would have written into.
        """
        judgement = runtime.Judgement(
            provider="gemini", model="gemini-3.7-flash", window="unknown",
            source="pattern", reset_at=None, at=NOW, reason=reason,
        )
        assert pool_binding.PoolBinding._floor_for_unsized(judgement, NOW) is None

    def test_a_judgement_without_a_reason_is_left_alone(self):
        """An older build's staged verdict is not guessed at."""
        judgement = runtime.Judgement(
            provider="gemini", model="gemini-3.7-flash", window="unknown",
            source="catalog", reset_at=None, at=NOW,
        )
        assert pool_binding.PoolBinding._floor_for_unsized(judgement, NOW) is None

    def test_a_stated_deadline_is_never_overwritten(self):
        """The floor is a floor, not a policy. A real number wins."""
        judgement = runtime.Judgement(
            provider="gemini", model="gemini-3.7-flash", window="per_minute",
            source="text", reset_at=NOW + 6.9, at=NOW, reason="rate_limit",
        )
        # ``_floor_for_unsized`` is only consulted when ``reset_at`` is None;
        # asserting the caller's guard rather than trusting the comment.
        source = pool_binding.PoolBinding._carry_deadline.__doc__ or ""
        assert "Only when the host has none" in source
        assert judgement.reset_at is not None


# ==========================================================================
# D. The verdict travels — reason is carried, not inferred
# ==========================================================================

class TestTheReasonReachesThePool:

    def test_note_judgement_carries_the_reason(self):
        runtime.forget_judgement()
        runtime.note_judgement(
            "gemini", "gemini-3.7-flash", window="unknown", source="catalog",
            reset_at=None, now=NOW, reason="rate_limit",
        )
        staged = runtime.peek_judgement("gemini", "gemini-3.7-flash", now=NOW)
        assert staged is not None
        assert staged.reason == "rate_limit"
        runtime.forget_judgement()

    def test_an_older_caller_still_constructs(self):
        """``reason`` is defaulted for the same reason ``scope`` was."""
        runtime.forget_judgement()
        runtime.note_judgement(
            "gemini", "gemini-3.7-flash", window="unknown", source="catalog",
            reset_at=None, now=NOW,
        )
        staged = runtime.peek_judgement("gemini", "gemini-3.7-flash", now=NOW)
        assert staged is not None and staged.reason == ""
        runtime.forget_judgement()


# ==========================================================================
# E. The rules are written down, not spread out
# ==========================================================================

class TestTheRulesAreLegible:

    def test_the_host_prose_markers_are_anchored_to_a_boundary(self):
        """A footer starts at a blank line. Matching mid-sentence would let a
        provider's own words be cut by a pattern meant for the host's."""
        for pattern in classify_mod._HOST_APPENDED_PROSE:
            assert pattern.pattern.startswith("\\n\\n"), pattern.pattern

    def test_stripping_is_idempotent(self):
        once = strip_host_prose(GEMINI_SIZED + FREE_TIER_FOOTER)
        assert strip_host_prose(once) == once

    def test_stripping_never_raises_on_odd_input(self):
        for value in ("", "\n\n", "\x00", "a" * 20000, "\n\nYour Google API key is on the free tier"):
            assert isinstance(strip_host_prose(value), str)


# ==========================================================================
# F. A small pool cannot afford the full rest for a dropped stream
# ==========================================================================

carousel = importlib.import_module(f"{PACKAGE}.core.carousel")
dispatch = importlib.import_module(f"{PACKAGE}.dispatch_binding")

import time as _time  # noqa: E402

#: The agent and response shapes the binding reads, borrowed from
#: ``tests/test_dispatch.py`` rather than re-invented, so a change to what the
#: binding expects breaks both files together instead of leaving this one
#: passing against a shape nothing uses.
_dispatch_tests = importlib.import_module("tests.test_dispatch")
_Agent = _dispatch_tests.Agent
_Answer = _dispatch_tests.Answer


class TestTheDropRestOnASmallPool:
    """Measured on the owner's NVIDIA pool, which has exactly two keys.

        kame: nvidia:moonshotai/kimi-k3 key:b65dd9 cut the answer after 568
              character(s) — resting it 30s and continuing on another key

    The answer was fine. The half-minute afterwards is the problem: one key
    withdrawn out of two, so a single refusal on the survivor leaves the
    carousel with nothing to pick. 1.1.3 already exempted a pool of one on the
    reasoning that a rest whose only job is to route the next request elsewhere
    should not cost more than that; this is the same reasoning, one step out.
    """

    IDENTITY = "nvidia:moonshotai/kimi-k3"

    def _rest(self, key_count):
        engine = carousel.Carousel()
        keys = [f"k{i}" for i in range(key_count)]
        return engine, keys, dispatch._rest_unless_it_is_the_only_one(
            engine, self.IDENTITY, keys, keys[0], dispatch.DROP_REST_S, "timeout"
        )

    def test_one_key_rests_for_nothing(self):
        """Unchanged since 1.1.3. There is nowhere to route to."""
        _engine, _keys, rested = self._rest(1)
        assert rested == 0.0

    def test_two_keys_get_the_short_rest(self):
        """Half the pool, so the rest is only long enough to move the pick."""
        _engine, _keys, rested = self._rest(2)
        assert rested == dispatch.DROP_REST_SMALL_POOL_S
        assert rested < dispatch.DROP_REST_S

    def test_three_keys_keep_the_full_rest(self):
        """A third of the capacity is affordable; nothing changes for big pools."""
        _engine, _keys, rested = self._rest(3)
        assert rested == dispatch.DROP_REST_S

    def test_the_failure_is_recorded_either_way(self):
        """The rest is shortened, not skipped. The key's history is the same.

        Losing the record would be a worse bug than the one being fixed: a
        flapping key that is never marked is a key nothing can learn about.
        """
        for count in (1, 2, 3):
            engine, keys, _rested = self._rest(count)
            snap = engine.snapshot()
            row = snap.get(self.IDENTITY, {})
            assert row, count

    def test_the_short_rest_still_moves_the_next_pick(self):
        """The entire stated purpose of this rest, asserted rather than assumed."""
        engine = carousel.Carousel()
        keys = ["a", "b"]
        dispatch._rest_unless_it_is_the_only_one(
            engine, self.IDENTITY, keys, "a", dispatch.DROP_REST_S, "timeout"
        )
        assert engine.healthy_count(self.IDENTITY, keys) == 1

    def test_a_provider_stated_wait_never_reaches_this_function(self):
        """Guard on the boundary, not on the number.

        This function may only shorten cooldowns of the second kind — the ones
        that exist to move the next pick. A ``Retry-After`` or a daily quota
        binds the whole pool and is applied directly, so if one ever arrived
        here it would be silently halved.
        """
        source = dispatch._rest_unless_it_is_the_only_one.__doc__ or ""
        assert "second kind" in source
        callers = [
            line for line in
            (dispatch.__file__ and open(dispatch.__file__, encoding="utf-8").read()).splitlines()
            if "_rest_unless_it_is_the_only_one(" in line and "def " not in line
        ]
        assert callers, "the guard has no call sites; this test is measuring nothing"


# ==========================================================================
# G. An empty list and a stopped plugin must not look the same
# ==========================================================================

state_mod = importlib.import_module(f"{PACKAGE}.state")


class TestProofOfLife:
    """Events records failures. A day with none draws what a dead plugin draws.

    The owner cleared the list, restarted twice, sent messages that all
    succeeded, and reported the panel as frozen. It was quiet. Nothing on the
    screen could say so, and the counts alone cannot: "53 calls" reads the
    same a second later and an hour later.
    """

    def test_a_real_call_stamps_the_clock(self):
        """Driven through ``run``, not by setting the attribute.

        A test that assigns ``last_call_at`` and reads it back proves the
        attribute exists and nothing else — the stamp could be deleted from
        ``run`` and it would still pass. This routes an actual call.
        """
        binding = dispatch.DispatchBinding(engine=carousel.Carousel())
        assert binding.last_call_at == 0.0, "must exist from construction"

        agent = _Agent()
        binding.run(lambda agent, api_kwargs, **kw: _Answer(), agent, {}, (), {})

        assert binding.calls == 1
        assert binding.last_call_at > 0.0
        assert abs(binding.last_call_at - _time.time()) < 30.0

    def test_it_moves_with_every_call(self):
        """Not just set once: the question is *when the last one was*."""
        binding = dispatch.DispatchBinding(engine=carousel.Carousel())
        agent = _Agent()
        binding.run(lambda agent, api_kwargs, **kw: _Answer(), agent, {}, (), {})
        first = binding.last_call_at
        binding.run(lambda agent, api_kwargs, **kw: _Answer(), agent, {}, (), {})
        assert binding.last_call_at >= first
        assert binding.calls == 2

    def test_the_snapshot_carries_it(self):
        class _Binding:
            calls = 7
            last_call_at = 1_700_000_000.0

        snap = state_mod.snapshot(_Binding())
        assert snap["counters"]["calls"] == 7
        assert snap["counters"]["last_call_at"] == 1_700_000_000.0

    def test_a_binding_without_it_still_snapshots(self):
        """An older build's binding, or none at all."""
        class _Old:
            calls = 3

        snap = state_mod.snapshot(_Old())
        assert snap["counters"]["last_call_at"] == 0.0
        assert state_mod.snapshot(None)["counters"]["last_call_at"] == 0.0

    def test_the_panel_renders_the_line(self):
        """The component exists and is mounted under the list, not instead of it."""
        panel = (PLUGIN_DIR / "desktop-ui" / "plugin.js").read_text(encoding="utf-8")
        assert "function Heartbeat(" in panel
        assert "h(Heartbeat, { key: 'heartbeat'" in panel
        # It must survive the empty case, which is the only case it is for.
        assert "An empty list above means nothing failed." in panel

    def test_the_menu_says_it_too(self):
        """``/kame events`` has the same ambiguity and the same answer."""
        menu = importlib.import_module(f"{PACKAGE}.menu")

        class _Binding:
            calls = 0
            last_call_at = 0.0

        assert "not installed" in menu.MenuCommand(None)._alive()
        assert "not been asked to route" in menu.MenuCommand(_Binding())._alive()
        _Binding.calls = 12
        _Binding.last_call_at = 1.0
        assert "12 call(s) routed" in menu.MenuCommand(_Binding())._alive()


# ==========================================================================
# H. The neighbour rows say whether the other profile is doing anything
# ==========================================================================

class TestNeighboursShowTheirWork:
    """"5 of 5 ready" reads identically on a profile whose KAME never loaded.

    The owner tried profile ``k``, it did not answer, and this card had no way
    to show that its plugin was inert — the keys were fine either way.
    """

    def test_the_row_reports_calls_and_installation(self):
        panel = (PLUGIN_DIR / "desktop-ui" / "plugin.js").read_text(encoding="utf-8")
        assert "KAME not installed there" in panel
        assert "other.counters?.calls" in panel
        assert "other.installed === false" in panel

    def test_the_hook_is_read_before_the_early_return(self):
        """A neighbour appearing must not change how many hooks the component ran."""
        panel = (PLUGIN_DIR / "desktop-ui" / "plugin.js").read_text(encoding="utf-8")
        body = panel.split("function Neighbours(", 1)[1]
        hook = body.index("useValue($now)")
        early = body.index("return null")
        assert hook < early, "the hook must run before the early return"


# ==========================================================================
# I. The first-token setting names a number to try
# ==========================================================================

def test_the_stream_silence_help_recommends_a_value():
    """Advice with no number is advice nobody can act on.

    The help said what the setting does and that zero is right for almost
    everyone, and stopped there — so a user who *did* have a hanging provider
    had to guess, between 5 (the floor) and 120 (Hermes' own).
    """
    settings_mod = importlib.import_module(f"{PACKAGE}.settings")
    # Through the public description, not the private table: what matters
    # is what a user actually reads in the panel.
    described = settings_mod.describe(settings_mod.STREAM_SILENCE_TIMEOUT)
    help_text = str(described.get("help", ""))
    assert "60" in help_text
    # And it still says zero is the default, which is the more important half.
    assert "Zero, the default" in help_text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
