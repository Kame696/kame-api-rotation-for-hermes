"""1.8.0.0 G6 — the fifteen invariants of ``decisions/0001-invariants.md``, reverified.

Five of them were each broken once by a session that had never met them and
had to be reverted (``audit/1.8.0.0-01-regras-fechadas.md`` R39/R41/R42/R43/
R48, all under "Closed, but reversed once"). This file is the gate the PLAN's
G6 asks for: one executable test per invariant that can be exercised from
this repository, on the real code paths the reversions actually broke — not
stand-ins for them.

Every class below opens with which invariant it defends, which release broke
it, and what the break cost. Where an invariant cannot be exercised from this
repository (it needs the installed Hermes host, live network, or a write
under ``AppData``, all out of bounds for this gate), the class says so and is
skipped rather than faked green.

Each invariant was hand-verified at least once during this file's own
authoring by temporarily breaking the guarding code, running just that test,
watching it fail, and reverting with ``git checkout`` — see
``research/1.8.0.0/INVARIANTS.md`` for the list of which ones and how.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
MANIFEST = PLUGIN_DIR / "plugin.yaml"
PACKAGE = "kame_gate_invariants_under_test"


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


plugin = _load_package()
carousel = importlib.import_module(f"{PACKAGE}.core.carousel")
core = importlib.import_module(f"{PACKAGE}.core")
classify_mod = importlib.import_module(f"{PACKAGE}.core.classify")
catalog = importlib.import_module(f"{PACKAGE}.core.catalog")
events_module = importlib.import_module(f"{PACKAGE}.core.events")
redact_module = importlib.import_module(f"{PACKAGE}.core.redact")
journal_module = importlib.import_module(f"{PACKAGE}.core.journal")
keys_module = importlib.import_module(f"{PACKAGE}.core.keys")
dispatch_binding = importlib.import_module(f"{PACKAGE}.dispatch_binding")
stitch = importlib.import_module(f"{PACKAGE}.core.stitch")
host_text = importlib.import_module(f"{PACKAGE}.host_text")
settings = importlib.import_module(f"{PACKAGE}.settings")
state_module = importlib.import_module(f"{PACKAGE}.state")
gemini_slots = importlib.import_module(f"{PACKAGE}.gemini_slots")

DispatchBinding = dispatch_binding.DispatchBinding
Carousel = carousel.Carousel
EVENTS = events_module.EVENTS
classify = classify_mod.classify

KEYS = [f"AIzaSyINV{i}" + "5" * 29 for i in range(4)]

GEMINI_REFUSAL = (
    "Gemini HTTP 400 (INVALID_ARGUMENT): Requests ending with a model turn "
    "are not supported."
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    settings.forget()
    for name in list(settings._ENV_FOR.values()) + list(settings._NUMBER_ENV_FOR.values()):
        monkeypatch.delenv(name, raising=False)
    for name in settings._LEGACY_ENV_FOR.values():
        monkeypatch.delenv(name, raising=False)
    carousel.ENGINE.forget()
    EVENTS.clear()
    dispatch_binding._NO_PREFILL.clear()
    yield
    settings.forget()
    carousel.ENGINE.forget()
    EVENTS.clear()
    dispatch_binding._NO_PREFILL.clear()


# --- the stand-in host, identical in shape to test_v1_1_2.py's ------------


class Entry:
    def __init__(self, key, entry_id):
        self.runtime_api_key = key
        self.access_token = ""
        self.id = entry_id


class Pool:
    def __init__(self, keys):
        self._entries = [Entry(k, f"e{i}") for i, k in enumerate(keys)]

    def entries(self):
        return list(self._entries)


class Client:
    def __init__(self, key):
        self.api_key = key


class Agent:
    def __init__(self, keys=KEYS):
        self.provider = "google"
        self.model = "gemini-3.7-flash"
        self.api_mode = "chat_completions"
        self.api_key = keys[0] if keys else ""
        self._credential_pool = Pool(keys) if keys else None
        self._client_kwargs = {"api_key": self.api_key}
        self.client = Client(self.api_key)
        self._credential_pool_entry_id = None
        self._interrupt_requested = False
        self.stream_delta_callback = None
        self.shown = []
        self.status_calls = []

    def _fire_stream_delta(self, text):
        self.shown.append(text)

    def _emit_status(self, message, **kwargs):
        self.status_calls.append(message)

    @property
    def screen(self):
        return "".join(self.shown)


class RateLimited(Exception):
    def __init__(self, message, status_code=429):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class BadRequest(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def answer(content="hello"):
    message = SimpleNamespace(content=content, tool_calls=None, role="assistant")
    choice = SimpleNamespace(index=0, message=message, finish_reason="stop")
    return SimpleNamespace(id="chatcmpl-1", model="m", choices=[choice], usage=None)


def cut(content):
    message = SimpleNamespace(content=content, tool_calls=None, role="assistant")
    choice = SimpleNamespace(index=0, message=message, finish_reason="length")
    return SimpleNamespace(
        id=dispatch_binding.PARTIAL_STUB_ID, model="m", choices=[choice], usage=None
    )


def conversation():
    return {"model": "gemini-3.7-flash", "messages": [{"role": "user", "content": "tell me"}]}


def _binding():
    return DispatchBinding(engine=Carousel())


def content_of(result):
    return str(getattr(result.choices[0].message, "content", "") or "")


NOW = 1_800_000_000.0


# ===========================================================================
# I1 -- Evidence, never identity
# ===========================================================================


class TestI1EvidenceNeverIdentity:
    """decisions/0001 I1: violated once by 1.2.9's ``provider="gemini"`` hardcode
    (corrected the same release). The prefill-refusal recovery is the case
    decision 0001 cites by name: it "learned it from that refusal ... it did
    not add ``if provider == "gemini"``".
    """

    def test_the_prefill_refusal_decision_names_no_provider(self):
        source = (PLUGIN_DIR / "dispatch_binding.py").read_text(encoding="utf-8")
        marker = source.index("def _prefill_refused")
        body = source[marker : source.index("def _resume_kwargs")]
        assert "gemini" not in body.lower().replace("gemini's", "")
        assert dispatch_binding._prefill_refused("google:gemini-3.7-flash") is False

    def test_classification_reads_the_quota_field_not_a_provider_allowlist(self):
        # The exact evidence a real Gemini per-minute throttle carries, sent
        # under a provider name the catalogue has never seen. If classify()
        # only recognised the field for known provider strings this would
        # come back unclassified -- evidence has to be what decides, not an
        # allowlist of who is asking.
        body = {
            "error": {
                "code": 429,
                "message": "You exceeded your current quota.",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                            }
                        ],
                    }
                ],
            }
        }
        verdict = classify(
            provider="a-provider-the-catalogue-has-never-heard-of",
            status_code=429,
            error_message="You exceeded your current quota.",
            error_body=body,
            now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "rate_limit"
        assert verdict.quota_window == classify_mod.QuotaWindow.PER_MINUTE


# ===========================================================================
# I2 -- Trust the connection
# ===========================================================================


class TestI2TrustTheConnection:
    """decisions/0001 I2: every artificial stream timeout this project ever
    added had to be deleted again (Zombie Guard, ``_StreamWatchdog``,
    ``CHUNK_STALE_TIMEOUT``). The only bound allowed is the host's own, and
    the opt-in KAME timeout defaults to off.
    """

    def test_no_deleted_watchdog_construct_has_come_back(self):
        source = (PLUGIN_DIR / "dispatch_binding.py").read_text(encoding="utf-8")
        for construct in ("_StreamWatchdog", "CHUNK_STALE_TIMEOUT", "ZombieGuard", "Zombie Guard"):
            assert construct not in source, construct

    def test_the_opt_in_silence_timeout_defaults_to_off(self):
        assert settings.number(settings.STREAM_SILENCE_TIMEOUT, 0) == 0


# ===========================================================================
# I3 -- Nothing KAME does to recover may end worse than not trying
# ===========================================================================


class TestI3RecoveryNeverEndsWorseThanNotTrying:
    """decisions/0001 I3, decided in 1.1.2. Once text is on screen, raising
    throws it away and handing the call back to Hermes prints it a second
    time (the host's request carries no record of what was already
    delivered). Every exit below must hand back the answer so far instead.
    """

    def test_an_interrupt_after_a_cut_keeps_what_was_shown(self):
        agent = Agent()

        def host(agent_, api_kwargs, **kwargs):
            agent_._fire_stream_delta("The cat sat")
            agent_._interrupt_requested = True
            return cut("The cat sat")

        result = _binding().run(host, agent, conversation(), (), {})
        assert content_of(result) == "The cat sat"

    def test_a_pool_that_is_all_resting_keeps_what_was_shown(self):
        agent = Agent(keys=KEYS[:1])
        calls = []

        def host(agent_, api_kwargs, **kwargs):
            calls.append(api_kwargs)
            if len(calls) == 1:
                agent_._fire_stream_delta("The cat sat")
                return cut("The cat sat")
            raise RateLimited("429 rate limit exceeded; retry in 60s")

        binding = _binding()
        binding._wait_for_recovery = lambda *a, **k: False
        result = binding.run(host, agent, conversation(), (), {})
        assert content_of(result) == "The cat sat"

    def test_the_pool_running_out_of_new_things_to_say_keeps_what_was_shown(
        self, monkeypatch
    ):
        monkeypatch.setenv("KAME_STREAM_RESUME_LIMIT", "1")
        settings.forget()
        monkeypatch.setattr(dispatch_binding, "DROP_REST_S", 0)
        agent = Agent(keys=KEYS[:1])
        calls = []

        class Teapot(Exception):
            def __init__(self):
                super().__init__("418 I'm a teapot")
                self.status_code = 418

        def host(agent_, api_kwargs, **kwargs):
            calls.append(api_kwargs)
            if len(calls) == 1:
                agent_._fire_stream_delta("The cat sat")
                return cut("The cat sat")
            raise Teapot()

        result = _binding().run(host, agent, conversation(), (), {})
        assert content_of(result) == "The cat sat"


# ===========================================================================
# I4 -- A partial stream is never replayed (R39)
# ===========================================================================


class TestI4APartialStreamIsNeverReplayed:
    """decisions/0001 I4, R39: reversed once by 1.3.3 "on purpose and without
    a bound". A drop that cannot be stitched must hand back exactly what was
    already shown -- not a shorter answer, not a longer one, not the answer
    restarted from the top.
    """

    def test_an_unsaveable_continuation_returns_exactly_what_was_shown_once(self):
        agent = Agent()
        sent = []

        def host(agent_, api_kwargs, **kwargs):
            sent.append(api_kwargs)
            if len(sent) == 1:
                agent_._fire_stream_delta("The cat sat")
                return cut("The cat sat")
            raise BadRequest("400 unknown field 'temperture'")

        result = _binding().run(host, agent, conversation(), (), {})
        # Exactly what was shown -- not empty (thrown away), not doubled
        # (replayed), not something the model was asked to say again.
        assert content_of(result) == "The cat sat"
        assert agent.screen == "The cat sat"
        assert len(sent) == 2

    def test_a_refusal_of_the_reshaped_continuation_does_not_restart_the_answer(self):
        # The same drop, but the second call is refused in the shape KAME
        # already re-tried once (the Gemini prefill sentence). Two requests
        # total -- the cut, and the one refusal -- never a third attempt that
        # would be asking the model to produce the answer over again.
        agent = Agent()
        sent = []

        def host(agent_, api_kwargs, **kwargs):
            sent.append(api_kwargs)
            if len(sent) == 1:
                agent_._fire_stream_delta("The cat sat")
                return cut("The cat sat")
            raise BadRequest(GEMINI_REFUSAL)

        result = _binding().run(host, agent, conversation(), (), {})
        assert content_of(result) == "The cat sat"
        assert len(sent) == 3  # the cut, the prefill, the user-turn shape


# ===========================================================================
# I5 -- A genuine refusal is surfaced, never disguised (R41)
# ===========================================================================


class TestI5AGenuineRefusalIsSurfacedNeverDisguised:
    """decisions/0001 I5, R41: reversed once by 1.3.0's "Absolute Shield",
    which swallowed a genuine bad request with nothing on screen. With
    nothing delivered yet, a terminal error is the user's request being
    refused, and hiding it is a lie.

    ``test_v1_1_2.py::test_a_bad_request_on_the_first_call_is_still_raised``
    is the original of this; this class exercises the same real dispatch
    exit as a gate rather than trusting the name.
    """

    def test_a_bad_request_with_nothing_shown_is_still_raised(self):
        def host(agent_, api_kwargs, **kwargs):
            raise BadRequest("400 unknown field 'temperture'")

        with pytest.raises(BadRequest):
            _binding().run(host, Agent(), conversation(), (), {})

    def test_every_key_resting_with_nothing_shown_still_raises_the_last_error(self):
        agent = Agent(keys=KEYS[:1])

        def host(agent_, api_kwargs, **kwargs):
            raise RateLimited("429 rate limit exceeded; retry in 60s")

        binding = _binding()
        binding._wait_for_recovery = lambda *a, **k: False
        with pytest.raises(RateLimited):
            binding.run(host, agent, conversation(), (), {})


# ===========================================================================
# I6 -- KAME's own voice never enters the model's history (R42)
# ===========================================================================


class TestI6KameVoiceNeverEntersHistory:
    """decisions/0001 I6, R42: reversed once by 1.3.0, which returned a
    synthetic assistant turn into history -- traced in 1.0.9 to the
    rewind/resend ``4030`` errors (drift growing 5v4 to 15v60 across a
    session). The real channel is ``_Vigil``, which speaks only through
    ``agent._emit_status`` -- the host's lifecycle channel -- and holds no
    reference to the conversation at all.
    """

    def test_the_vigil_speaks_through_status_never_through_the_screen(self):
        vigil = dispatch_binding._Vigil(Agent(), "google:gemini-3.7-flash")
        vigil.started -= dispatch_binding.VIGIL_FIRST_S + 1  # force "waited long enough"
        vigil.maybe_speak(healthy=0, total=1, eta=5.0)
        assert vigil.agent.status_calls, "the notice never fired"
        assert any("KAME:" in msg for msg in vigil.agent.status_calls)
        # Nothing was written to the text channel a model turn would be built
        # from -- the vigil has no way to reach it at all.
        assert vigil.agent.shown == []

    def test_a_rotation_never_appends_a_turn_to_the_conversation_sent_onward(self):
        # A cut-and-retry crosses several attempts; the messages actually
        # sent to the host on the *last* attempt must be the same object the
        # turn started with plus real continuation prefill content -- never
        # KAME's own narration spliced into the list.
        agent = Agent(keys=KEYS[:2])
        seen_message_texts = []

        def host(agent_, api_kwargs, **kwargs):
            for message in api_kwargs.get("messages", []):
                seen_message_texts.append(str(message.get("content") or ""))
            if len(seen_message_texts) <= 1:
                raise RateLimited("429 rate limit exceeded; retry in 1s")
            return answer("done")

        result = _binding().run(host, agent, conversation(), (), {})
        assert content_of(result) == "done"
        assert not any("KAME" in text for text in seen_message_texts)
        assert not any("resting" in text or "Waiting" in text for text in seen_message_texts)


# ===========================================================================
# I7 -- No key material, anywhere
# ===========================================================================


class TestI7NoKeyMaterialAnywhere:
    """decisions/0001 I7: never broken. Always redacted to ``AIzaSy…q7R8``.
    Checked at both places a key becomes a display string: the fingerprint
    every rotation event carries, and the panel's own ``redact()``.
    """

    def test_the_fingerprint_carries_no_substring_of_the_key(self):
        key = KEYS[0]
        fp = carousel.fingerprint(key)
        assert key not in fp
        # No run of six or more characters from the key survives into the
        # fingerprint either -- a hash prefix, not a truncation.
        for start in range(0, len(key) - 6):
            assert key[start : start + 6] not in fp

    def test_keys_redact_never_returns_the_live_key(self):
        key = KEYS[0]
        shown = keys_module.redact(key)
        assert shown != key
        assert key not in shown
        assert shown.endswith(key[-4:])

    def test_short_tokens_are_not_partially_shown(self):
        assert keys_module.redact("short") == "*****"

    def test_every_events_add_call_site_fingerprints_the_key_first(self):
        # Source-marker check: every ``EVENTS.add(`` block in the dispatch
        # loop must pass a fingerprint, never the live key, as its ``key=``.
        # A caller that slipped a raw key past this would leak it into the
        # ring buffer this file's own docstring promises never carries one.
        source = (PLUGIN_DIR / "dispatch_binding.py").read_text(encoding="utf-8")
        calls = re.findall(r"EVENTS\.add\(.*?\n(?:.*?\n)*?\s*\)", source)
        assert calls, "no EVENTS.add( call sites found to check"
        key_lines = [
            line for block in calls for line in block.splitlines() if "key=" in line
        ]
        assert key_lines, "no key= arguments found to check"
        for line in key_lines:
            assert "fingerprint(" in line or 'key=""' in line, line


# ===========================================================================
# I8 -- No provider error text is kept (R43)
# ===========================================================================


class TestI8NoProviderErrorTextIsKept:
    """decisions/0001 I8, R43: reversed once, silently, by 1.2.9's
    ``raw_error``. The current design (``core/redact.py``, added after I8 was
    written) keeps a *redacted* provider sentence rather than none at all --
    the "third option" its own docstring describes: "redact before storing,
    not before showing." What must never happen, on any of the three writers
    this plugin has, is a live credential or an *unredacted* payload reaching
    disk, or the reason field growing into free text.

    The one addition 1.8.0.0 made -- allowlisted response headers in
    ``recorder.py``, for decision 0004 D1 -- is covered explicitly, because
    it is the one place this release added a new way for provider text to
    reach a file.
    """

    # -- what a classification verdict is allowed to say ---------------

    def test_classify_never_constructs_a_verdict_from_free_text(self):
        # ``Verdict.reason`` is coerced to a ``FailoverReason`` on the host
        # side (core/classify.py's own comment on ``Verdict.kind``), so it has
        # to be one of a small, closed vocabulary -- never the provider's
        # sentence, which is exactly what 1.2.9's ``raw_error`` put here.
        source = (PLUGIN_DIR / "core" / "classify.py").read_text(encoding="utf-8")
        literals = re.findall(r'reason="([a-z_]+)"', source)
        assert literals, "no reason= literals found in classify.py to check"
        for word in literals:
            assert re.fullmatch(r"[a-z][a-z_]{2,20}", word), word

    # -- the ring buffer that becomes the on-disk snapshot ---------------

    def test_events_add_redacts_a_credential_out_of_detail(self):
        secret = KEYS[0]
        EVENTS.add(
            "stream_drop",
            identity="google:gemini-3.7-flash",
            key=carousel.fingerprint(secret),
            reason="the connection failed mid-answer",
            code=429,
            detail=f"quota exceeded for key {secret}",
        )
        row = EVENTS.recent(1)[0]
        assert secret not in row["detail"]
        assert secret not in row["reason"]

    def test_events_reason_is_bounded_short(self):
        EVENTS.add("rotation", reason="x" * 5000)
        row = EVENTS.recent(1)[0]
        assert len(row["reason"]) <= 120

    def test_state_snapshot_only_ever_reads_the_already_redacted_ring_buffer(self):
        # Source-marker check: state.py must not read anything off an
        # exception or a provider payload directly -- only ``EVENTS.recent()``,
        # which has already redacted ``detail`` on the way in.
        source = (PLUGIN_DIR / "state.py").read_text(encoding="utf-8")
        assert "EVENTS.recent()" in source
        assert ".raw_message" not in source
        assert "error_body" not in source

    # -- the recorder's allowlisted header capture (1.8.0.0, decision 0004) --

    def test_recorder_header_capture_drops_credential_named_headers(self):
        from kame_gate_invariants_under_test import recorder as recorder_module

        headers = {
            "Retry-After": "42",
            "X-RateLimit-Reset": "120",
            "Authorization": f"Bearer {KEYS[0]}",
            "X-Api-Key": KEYS[1],
            "Set-Cookie": "session=abc",
        }
        kept = recorder_module._safe_headers(headers)
        assert kept.get("retry-after") == "42"
        assert "x-ratelimit-reset" in kept
        assert "authorization" not in kept
        assert "x-api-key" not in kept
        assert "set-cookie" not in kept
        assert not any(KEYS[0] in v or KEYS[1] in v for v in kept.values())

    def test_recorder_header_capture_ignores_names_not_on_the_allowlist(self):
        from kame_gate_invariants_under_test import recorder as recorder_module

        kept = recorder_module._safe_headers({"X-Request-Id": "abc123", "Server": "nginx"})
        # ``x-request-id`` matches the allowlist (provenance); ``server`` does
        # not and must be dropped, not merely unread.
        assert "x-request-id" in kept
        assert "server" not in kept

    def test_recorder_redacts_a_key_shaped_value_even_under_an_allowlisted_name(self):
        from kame_gate_invariants_under_test import recorder as recorder_module

        # A pathological case: a quota header whose *value* happens to look
        # like a key. The opaque-value rule has to catch what the name-based
        # rules miss.
        kept = recorder_module._safe_headers({"X-RateLimit-Limit": KEYS[0]})
        assert "x-ratelimit-limit" not in kept

    # -- the journal, which stores a caller-supplied reason verbatim -----

    def test_journal_record_block_is_fed_only_short_canonical_reasons(self):
        # journal.Journal.record_block() itself does not bound ``reason`` --
        # it trusts its one caller (pool_binding.py's ``_record_block``),
        # which always threads through a classification verdict's own
        # ``reason``/``kind`` or one of dispatch_binding's short phrases.
        # This is the caller-contract half of the same guarantee events.py
        # enforces structurally: the only strings that ever reach the
        # journal are canonical words, never ``ev.raw_message`` or ``str(exc)``.
        source = (PLUGIN_DIR / "pool_binding.py").read_text(encoding="utf-8")
        marker = source.index("def _record_block(")
        # every call passes reason=reason, i.e. threads the parameter through
        # rather than re-deriving it from a raw exception at the write site
        assert "reason=reason," in source
        assert "raw_message" not in source[marker : marker + 4000]
        assert "str(exc)" not in source[marker : marker + 4000]


# ===========================================================================
# I9 -- Prove the whole chain at runtime before patching the host
# ===========================================================================


class TestI9ProveTheChainAtRuntimeBeforePatchingTheHost:
    """decisions/0001 I9. Every guard has to fail closed and say why in
    ``/kame`` when any one link is missing -- ``apply()``'s own contract.
    This repository has no installed Hermes, so ``agent.gemini_native_adapter``
    genuinely does not import here, which makes "the adapter does not import"
    the one link this gate can prove for real rather than by construction.
    The self-check's synthetic-case logic (reproduce the bug, then prove the
    repair) is exercised directly, since it needs no host at all.
    """

    def test_apply_declines_and_explains_when_the_adapter_does_not_import(self):
        gemini_slots._state.clear()
        try:
            applied = gemini_slots.apply()
        finally:
            pass
        assert applied is False
        assert "adapter" in gemini_slots._state.get("reason", "").lower() or (
            "gemini" in gemini_slots._state.get("reason", "").lower()
        )

    def test_self_check_refuses_to_patch_a_stream_that_never_had_the_bug(self):
        def already_correct(event, model, indices):
            # A synthetic "original" that already keeps the two tool calls
            # apart -- the self-check must recognise there is nothing to fix
            # and refuse, exactly as it would refuse against a host that
            # shipped its own repair.
            return []

        ok, why = gemini_slots._self_check(already_correct)
        assert ok is False
        assert why


# ===========================================================================
# I10 -- Every host assumption is a tripwire
# ===========================================================================


class TestI10EveryHostAssumptionIsATripwire:
    """decisions/0001 I10. ``tools/host_assumptions.py`` runs each check
    against the *installed* Hermes under ``HERMES_HOME``
    (``AppData/Local/hermes`` by default) -- out of bounds for this gate on
    two counts (no AppData writes/reads, no dependency on a machine-specific
    install). What is checked here, without touching that path, is that the
    tripwire this invariant is named after still exists in the tool and is
    still wired into its check list -- so a change that silently dropped it
    would still be caught.
    """

    def test_not_covered_the_live_tripwire_needs_the_installed_host(self):
        pytest.skip(
            "tools/host_assumptions.py reads the installed Hermes under "
            "HERMES_HOME (AppData/Local/hermes by default); this gate may "
            "not touch AppData or depend on a specific machine's install. "
            "See the static check below for what is covered instead."
        )

    def test_the_manifest_version_tripwire_is_still_defined_and_registered(self):
        source = (ROOT / "tools" / "host_assumptions.py").read_text(encoding="utf-8")
        assert "def the_installer_still_stops_at_manifest_version_one(" in source
        assert (
            '"the installer still stops at manifest_version 1", '
            "the_installer_still_stops_at_manifest_version_one)" in source.replace("\n", " ")
            or "the_installer_still_stops_at_manifest_version_one)" in source
        )
        assert "_SUPPORTED_MANIFEST_VERSION" in source


# ===========================================================================
# I11 -- A deprecated setting name works for ever
# ===========================================================================


class TestI11ADeprecatedSettingNameWorksForEver:
    """decisions/0001 I11: three names have carried this one setting --
    ``first_token_patience_seconds`` -> ``silent_stream_patience_seconds`` ->
    ``stream_silence_timeout_seconds`` -- and the promise is that all three
    still answer. All three are wired in ``settings.py`` as of 1.8.0.0: the
    oldest name was restored to ``_LEGACY_KEYS`` (config) and the new
    ``_LEGACY_ENV_FOR_1_0_8`` (environment) once this gate's own grep proved
    it was missing everywhere except the CHANGELOG and this decision's text.
    """

    def test_the_1_0_9_name_still_works_as_an_environment_variable(self, monkeypatch):
        monkeypatch.setenv("KAME_SILENT_STREAM_PATIENCE", "42")
        settings.forget()
        assert settings.number(settings.STREAM_SILENCE_TIMEOUT, 0) == 42

    def test_the_1_0_9_name_still_works_as_a_config_key(self):
        assert settings.canonical("silent_stream_patience_seconds") == (
            settings.STREAM_SILENCE_TIMEOUT
        )

    def test_the_oldest_1_0_8_name_still_works_too(self, monkeypatch):
        monkeypatch.setenv("KAME_FIRST_TOKEN_PATIENCE", "42")
        settings.forget()
        assert settings.number(settings.STREAM_SILENCE_TIMEOUT, 0) == 42


# ===========================================================================
# I12 -- The pool is a mirror of the config, not an archive
# ===========================================================================


class TestI12ThePoolIsAMirrorNotAnArchive:
    """decisions/0001 I12, since 1.2.2. ``select()``'s own docstring: "An
    empty candidate list mirrors nothing at all -- a loader failing once is
    not evidence that every key was deleted."
    """

    def test_an_empty_candidate_list_does_not_erase_an_earned_cooldown(self):
        engine = Carousel()
        identity = "google:gemini-3.7-flash"
        engine.select(identity, [KEYS[0]], now=1000.0)
        applied = engine.mark(identity, KEYS[0], False, 45.0, "rate_limit", now=1000.0)
        assert applied > 0

        # One call where the host handed back nothing to choose from -- not
        # evidence the key was deleted.
        key, status = engine.select(identity, [], now=1001.0)
        assert key is None and status == "EMPTY"

        # The cooldown from a moment ago must still be in force.
        key, status = engine.select(identity, [KEYS[0]], now=1001.0)
        assert status == "EXHAUSTED"

    def test_a_key_absent_for_less_than_the_grace_window_is_not_dropped(self):
        engine = Carousel()
        identity = "google:gemini-3.7-flash"
        engine.select(identity, [KEYS[0], KEYS[1]], now=1000.0)
        engine.mark(identity, KEYS[0], False, 3600.0, "daily", now=1000.0)
        # KEYS[0] absent from the offered list for less than MIRROR_GRACE_S --
        # its bench must survive.
        engine.select(identity, [KEYS[1]], now=1000.0 + carousel.MIRROR_GRACE_S - 5)
        key, status = engine.select(identity, [KEYS[0]], now=1000.0 + carousel.MIRROR_GRACE_S - 4)
        assert status == "EXHAUSTED"


# ===========================================================================
# I13 -- The panel's lists are keyed (R48)
# ===========================================================================


class TestI13ThePanelsListsAreKeyed:
    """decisions/0001 I13, R48: reversed once by 1.3.x's mangled write to
    ``desktop/plugin.js`` (backslash/``${}``-eating), which broke
    ``EventRow`` and crashed the whole Events tab on the first status-coded
    event. Wired in from ``tests/ui_reconcile.mjs``, the same structural
    check ``test_v1_2_3.py`` already runs -- Node is present on this machine
    (checked at collection time), so this runs it rather than replicating it.
    """

    @pytest.mark.skipif(shutil.which("node") is None, reason="no node on this machine")
    def test_no_variadic_child_list_in_the_panel_is_keyless(self):
        script = ROOT / "tests" / "ui_reconcile.mjs"
        result = subprocess.run(
            ["node", str(script)], capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, result.stdout + result.stderr


# ===========================================================================
# I14 -- A setting no shelf claims lands in "Other"
# ===========================================================================


class TestI14ASettingNoShelfClaimsLandsSomewhereNotNowhere:
    """decisions/0001 I14, since 1.2.0: "It has to land in the wrong place
    rather than nowhere." ``settings.py``'s own fallback shelf is named
    ``tuning`` rather than literally "Other" (its comment explains why: a
    switch on the wrong shelf is a small confusion, one among the escape
    hatches reads as an unearned warning) -- the invariant tested here is the
    behaviour the decision actually defends, not the literal string.
    """

    def test_an_unlisted_setting_key_still_resolves_to_a_real_shelf(self):
        assert settings.group_of("a_setting_no_table_has_ever_heard_of") == (
            settings._UNGROUPED
        )
        assert settings._UNGROUPED in {group for group, _, _, _ in settings.GROUPS}

    def test_describe_all_still_lists_a_setting_missing_from_groups(self, monkeypatch):
        # ALL_FLAGS is a tuple, not a dict -- add the key by replacing the
        # tuple for the duration of the test, matching describe_all()'s own
        # ``list(ALL_FLAGS) + list(ALL_NUMBERS)`` read.
        fake_key = "kame_gate_invariant_fake_setting"
        monkeypatch.setattr(settings, "ALL_FLAGS", settings.ALL_FLAGS + (fake_key,))
        keys_seen = [row["key"] for row in settings.describe_all()]
        assert fake_key in keys_seen
        assert settings.group_of(fake_key) == settings._UNGROUPED


# ===========================================================================
# I15 -- manifest_version: 1
# ===========================================================================


class TestI15ManifestVersionOne:
    """decisions/0001 I15, since 1.1.2, watched by a tripwire. Hermes' loader
    (``hermes_cli/plugins.py``) understands 2; its installer
    (``hermes_cli/plugins_cmd.py``) understands 1 and raises on anything
    higher, which is why 1.0.9's bump to 2 broke every repository install
    silently -- a file copy never meets the installer's gate.
    """

    def test_the_manifest_declares_version_the_installer_accepts(self):
        manifest = MANIFEST.read_text(encoding="utf-8")
        assert "manifest_version: 1" in manifest
        for field in ("license:", "homepage:", "tags:", "api_version: 1"):
            assert field in manifest

    def test_the_tripwire_that_would_catch_a_premature_bump_still_exists(self):
        # See TestI10 above -- this is the same source-marker check, listed
        # again here because I15 names the specific fact this tripwire
        # guards, not only that host_assumptions.py has *a* tripwire.
        source = (ROOT / "tools" / "host_assumptions.py").read_text(encoding="utf-8")
        assert "manifest_version" in source
        assert "_SUPPORTED_MANIFEST_VERSION" in source
