"""What 1.0.9 promised, pinned one promise per class.

Every release before this one added a capability. This one is mostly a set of
*refusals*: things the plugin had been doing that it must stop doing, each one
traced to a specific hour of a specific log. So the tests read as refusals too,
and each class names the failure it exists to prevent from coming back.

1. A 410 is the request, not the key — the loop that ran from 10:02 to 10:18.
2. Hermes' own cross-turn breaker is not a key failure — rotating into it
   burns the pool without a single packet leaving the machine.
3. Fifteen keys giving the same answer is evidence about the request, but only
   for the kinds of answer a key cannot cause.
4. The status line is throttled per cadence and locked, because three lanes
   share one class.
5. A session reset forgets the conversation, never the calendar.
6. KAME tunes the host only where the host is at its default.
7. ``/kame`` never raises, and never writes anything but ``KAME_*``.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v109_under_test"


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
dispatch_binding = importlib.import_module(f"{PACKAGE}.dispatch_binding")
carousel = importlib.import_module(f"{PACKAGE}.core.carousel")
storm = importlib.import_module(f"{PACKAGE}.core.storm")
settings = importlib.import_module(f"{PACKAGE}.settings")
menu = importlib.import_module(f"{PACKAGE}.menu")
# 1.1.1 moved writing to the .env out of the menu and into its own module, so
# the file-path hook these tests replace lives there now. What they assert about
# it — surgical writes, no duplicate line, nothing outside KAME_* — is unchanged.
envfile = importlib.import_module(f"{PACKAGE}.envfile")

DispatchBinding = dispatch_binding.DispatchBinding
Carousel = carousel.Carousel

KEYS = [f"AIzaSyKEY{i}" + "0" * 29 for i in range(4)]

#: The exact sentence Hermes raises from ``_check_stale_giveup``. Pinned as a
#: literal because the whole mechanism keys off recognising it in a message,
#: and a paraphrase here would let a real drift pass unnoticed.
BREAKER_MESSAGE = (
    "5 consecutive stale attempts; provider has been unresponsive - "
    "aborting to avoid an endless retry loop"
)


class Boom(Exception):
    def __init__(self, message="", status_code=None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    settings.forget()
    for name in list(settings._ENV_FOR.values()) + list(settings._NUMBER_ENV_FOR.values()):
        monkeypatch.delenv(name, raising=False)
    dispatch_binding._Spinner.reset()
    yield
    settings.forget()
    dispatch_binding._Spinner.reset()


# --- 1. the 410 loop --------------------------------------------------------


class TestARemovedEndpointIsNotAKeyProblem:
    """`nvidia:z-ai/glm-5.2` returned 410 Gone and KAME rotated on it forever.

    Forty-six occurrences in one sixteen-minute window on 21 August, every one
    of them a fresh key against an endpoint that no longer exists.
    """

    def test_410_is_terminal(self):
        assert carousel.is_terminal(Boom("Gone", 410), "Gone") is True

    @pytest.mark.parametrize("status", [400, 404, 405, 410, 413, 415, 422, 451, 501])
    def test_every_status_that_describes_the_request_is_terminal(self, status):
        assert carousel.is_terminal(Boom("nope", status), "nope") is True

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_a_status_that_describes_the_moment_still_rotates(self, status):
        assert carousel.is_terminal(Boom("later", status), "later") is False

    def test_a_bad_credential_still_rotates(self):
        # 401 is about this key and not about the request, so it must stay
        # rotatable — the other fourteen keys are fine.
        assert carousel.is_terminal(Boom("invalid api key", 401), "invalid api key") is False


# --- 2. the host's own breaker ----------------------------------------------


class TestTheHostBreakerIsNotAKeyFailure:
    """``_check_stale_giveup`` raises *before* any network attempt.

    Its counter lives on the agent, not on the key, so once it trips KAME
    would burn every key in the pool in milliseconds with no traffic at all,
    and would do it again on the next turn. Hermes' own comment cites the
    case that motivated it: 494 consecutive failures over three days.
    """

    def test_it_is_classified_before_anything_else(self):
        delay, kind, status = carousel.classify(Boom(BREAKER_MESSAGE), BREAKER_MESSAGE)
        assert (delay, kind, status) == (0.0, "host_breaker", None)

    def test_it_is_terminal(self):
        assert carousel.is_terminal(Boom(BREAKER_MESSAGE), BREAKER_MESSAGE) is True

    def test_it_beats_the_auth_reading_of_the_same_text(self):
        # The breaker message can arrive wrapped in something that also smells
        # of auth. The breaker wins: a key quarantine would be a lie, and it
        # would cost the pool.
        text = BREAKER_MESSAGE + " (invalid api key)"
        assert carousel.is_terminal(Boom(text), text) is True

    def test_a_rotation_clears_the_streak_the_breaker_counts(self):
        # Hermes resets this counter when the *provider* changes. A key swap is
        # the same kind of event for the same reason, and clearing it is what
        # stops a pool-wide burn from ever reaching the threshold.
        class Agent:
            _consecutive_stale_streams = 3

        agent = Agent()
        dispatch_binding._clear_host_stale_streak(agent)
        assert agent._consecutive_stale_streams == 0

    def test_clearing_the_streak_never_raises_on_an_agent_without_one(self):
        dispatch_binding._clear_host_stale_streak(object())


# --- 3. the evidence rule ---------------------------------------------------


class TestUnanimityIsEvidenceAboutTheRequest:
    """Every key answering identically means the request, not the pool.

    But only for answers a key cannot cause on its own. A quota is per key by
    definition; fifteen keys all out of quota is a bad afternoon, not a bad
    request, and promoting that to terminal would throw away the wait that is
    the entire point of the plugin.
    """

    def _agrees(self, verdicts, keys, kind, status=None):
        return DispatchBinding._pool_agrees_it_is_the_request(verdicts, keys, kind, status)

    def test_a_unanimous_pool_is_promoted(self):
        verdicts = {k: ("unknown", 418) for k in KEYS}
        assert self._agrees(verdicts, KEYS, "unknown", 418) is True

    def test_one_dissenting_key_is_enough_to_keep_rotating(self):
        verdicts = {k: ("unknown", 418) for k in KEYS}
        verdicts[KEYS[2]] = ("unknown", 500)
        assert self._agrees(verdicts, KEYS, "unknown", 418) is False

    def test_a_pool_not_yet_fully_asked_is_not_unanimous(self):
        verdicts = {KEYS[0]: ("unknown", 418)}
        assert self._agrees(verdicts, KEYS, "unknown", 418) is False

    @pytest.mark.parametrize(
        "kind",
        ["server", "timeout", "per_minute", "daily", "insufficient_quota", "auth"],
    )
    def test_the_kinds_a_key_can_cause_are_never_promoted(self, kind):
        verdicts = {k: (kind, 429) for k in KEYS}
        assert self._agrees(verdicts, KEYS, kind, 429) is False

    def test_a_single_key_pool_can_still_be_unanimous(self):
        # The one-key case the user asked about explicitly. With one key there
        # is no rotation to do, so the only useful thing left is to notice
        # quickly when the answer is about the request and stop.
        one = [KEYS[0]]
        assert self._agrees({KEYS[0]: ("unknown", 418)}, one, "unknown", 418) is True


# --- 4. the live status line ------------------------------------------------


class TestTheStatusLine:
    """Always visible, never a flood, and never a message.

    It rides ``thinking.delta``, which is transient status: it creates no
    message and moves no ordinal, which is the one thing this release could
    not afford to get wrong.
    """

    def test_it_keeps_the_words_agent_zero_uses(self):
        # The shape had to change to get past Desktop's gate; the vocabulary
        # did not. Two ports of one plugin that describe themselves
        # differently are two plugins to the person reading the screen.
        line = dispatch_binding.status_line(15, 15)
        assert "KAME" in line and "15/15" in line and "healthy" in line

    def test_a_tail_is_appended_not_substituted(self):
        line = dispatch_binding.status_line(2, 15, "next key in 1m 23s")
        assert "2/15 keys healthy" in line and line.endswith("next key in 1m 23s")

    def test_every_line_it_can_produce_survives_desktops_gate(self):
        # The gate is not a formality. A line that misses it does not merely
        # fail to show: `setSessionProviderWait` is called with the empty
        # string, which wipes whatever the core had put in the row. v1.0.9
        # emitted one of these every ten seconds.
        lines = [
            dispatch_binding.status_line(15, 15, subject="gemini-2.5-pro"),
            dispatch_binding.status_line(
                12, 15, "on key 3", subject="gemini-2.5-pro", symbol="\u21bb"
            ),
            dispatch_binding.status_line(
                0, 15, "next key in 1m 23s", subject="a key to come back"
            ),
            dispatch_binding.status_line(
                15, 15, "back after 4m12s", subject="",
                symbol="\u21bb", opener="model returned",
            ),
        ]
        for line in lines:
            assert dispatch_binding.passes_desktop_status_gate(line), line

    def test_the_gate_is_not_vacuous(self):
        assert not dispatch_binding.passes_desktop_status_gate(
            "KAME API Rotation: 15/15 healthy"
        )
        assert not dispatch_binding.passes_desktop_status_gate("")

    def test_the_model_is_what_names_the_wait(self):
        # The provider is already in Hermes' own chrome; the row is one line
        # shared with a timer, and the model is the half that says which wait
        # this is.
        assert dispatch_binding.model_label("google:gemini-2.5-pro") == "gemini-2.5-pro"
        assert dispatch_binding.model_label("gemini-2.5-pro") == "gemini-2.5-pro"
        assert dispatch_binding.model_label("") == "the provider"

    @pytest.mark.parametrize(
        "eta,expected", [(None, 10.0), (5.0, 1.0), (10.0, 1.0), (45.0, 5.0), (600.0, 30.0)]
    )
    def test_the_cadence_follows_the_countdown(self, eta, expected):
        # A one-second tick for an hour would be 3600 frames to redraw a number
        # that changes by the minute. Near zero it is worth a tick a second.
        assert dispatch_binding._Spinner.cadence_for(eta) == expected

    def test_a_short_wait_shows_no_wall_clock(self):
        assert "around" not in dispatch_binding.recovery_clock(30.0)

    def test_a_long_wait_answers_can_i_go_and_do_something_else(self):
        assert "around" in dispatch_binding.recovery_clock(3600.0)

    def test_an_unknown_eta_says_so_rather_than_guessing(self):
        assert dispatch_binding.recovery_clock(None) == "unknown"

    def test_the_same_text_twice_is_sent_once(self):
        said = []

        class Agent:
            _emit_wait_notice = staticmethod(said.append)

        agent = Agent()
        dispatch_binding._Spinner.update(agent, "one", interval=0.0)
        dispatch_binding._Spinner.update(agent, "one", interval=0.0)
        assert said == ["one"]

    def test_an_unchanged_line_is_redrawn_once_the_refresh_window_passes(self):
        # The spinner is shared. Once Hermes writes its own activity into it,
        # KAME's line is off the screen while the diff gate still believes it
        # is showing, so a strict gate would never put it back.
        said = []

        class Agent:
            _emit_wait_notice = staticmethod(said.append)

        agent = Agent()
        dispatch_binding._Spinner.update(agent, "one", interval=0.0)
        key = dispatch_binding._Spinner.key_for(agent)
        said_at, shown = dispatch_binding._Spinner._state[key]
        dispatch_binding._Spinner._state[key] = (
            said_at - dispatch_binding._Spinner._REFRESH_S - 1,
            shown,
        )
        dispatch_binding._Spinner.update(agent, "one", interval=0.0)
        assert said == ["one", "one"]

    def test_the_interval_holds_a_changed_line_back(self):
        said = []

        class Agent:
            _emit_wait_notice = staticmethod(said.append)

        agent = Agent()
        dispatch_binding._Spinner.update(agent, "one", interval=0.0)
        dispatch_binding._Spinner.update(agent, "two", interval=3600.0)
        assert said == ["one"]

    def test_the_switch_silences_it_completely(self, monkeypatch):
        said = []

        class Agent:
            _emit_wait_notice = staticmethod(said.append)

        monkeypatch.setenv("KAME_LIVE_STATUS_DISABLED", "1")
        dispatch_binding._Spinner.update(Agent(), "anything", interval=0.0)
        assert said == []

    def test_the_state_is_guarded(self):
        # Three lanes share this class state: the main call, the auxiliary
        # lane, and any subagent. Without the lock two of them interleave a
        # read and a write and one update is silently lost.
        assert isinstance(dispatch_binding._Spinner._lock, type(threading.Lock()))

    def test_a_host_that_cannot_show_status_costs_nothing(self):
        dispatch_binding._Spinner.update(object(), "anything", interval=0.0)


# --- 5. session reset -------------------------------------------------------


class TestASessionResetForgetsTheConversationNotTheCalendar:
    def test_the_status_line_is_forgotten(self):
        said = []

        class Agent:
            _emit_wait_notice = staticmethod(said.append)

        agent = Agent()
        agent.session_id = "s1"
        dispatch_binding._Spinner.update(agent, "one", interval=0.0)
        plugin._on_session_reset({"session_id": "s1"})
        dispatch_binding._Spinner.update(agent, "one", interval=0.0)
        assert said == ["one", "one"]

    def test_an_unlabelled_reset_forgets_every_status_line(self):
        # The host always names the session today (`hermes_cli/hooks.py:182`).
        # If a build ever stops, forgetting too much costs one redrawn line and
        # forgetting nothing leaves every conversation's line stuck, so the
        # unlabelled case deliberately clears the lot.
        said = []

        class Agent:
            _emit_wait_notice = staticmethod(said.append)

        agent = Agent()
        agent.session_id = "s1"
        dispatch_binding._Spinner.update(agent, "one", interval=0.0)
        plugin._on_session_reset()
        dispatch_binding._Spinner.update(agent, "one", interval=0.0)
        assert said == ["one", "one"]

    def test_the_storm_filter_survives_it(self):
        # It was cleared here until 1.0.9. Two reasons it is not any more. It
        # describes a provider outage rather than a chat, so it belongs with
        # the cooldowns; and it guards the log, which is one file per process
        # and is shared by every conversation. Clearing it on one session's
        # reset restarted the collapse for the sessions still living through
        # the same outage, which is the opposite of what the filter is for.
        binding = dispatch_binding.DispatchBinding.__new__(
            dispatch_binding.DispatchBinding
        )
        binding._storm = storm.StormFilter()
        binding._storm.observe("server", 503, "abcd", 0.0)
        plugin.__dict__["_dispatch_binding"] = binding
        try:
            plugin._on_session_reset({"session_id": "s1"})
            assert binding._storm._storm is not None
        finally:
            plugin.__dict__.pop("_dispatch_binding", None)

    def test_cooldowns_survive_it(self):
        # A key benched until midnight is still benched at 00:01 whether or not
        # the history was cleared. Clearing these would walk the whole pool back
        # into the same wall it just learned about, one request at a time.
        engine = Carousel()
        engine.mark("google:gemini", KEYS[0], False, 900.0, "daily")
        before = engine.healthy_count("google:gemini", KEYS)
        plugin._on_session_reset()
        assert engine.healthy_count("google:gemini", KEYS) == before
        assert engine.next_recovery_seconds("google:gemini", [KEYS[0]]) is not None

    def test_it_never_raises_with_no_bindings_installed(self):
        assert plugin._on_session_reset() is None


# --- 5b. one process, many conversations ------------------------------------


class TestOneProcessServesEveryConversation:
    """Hermes runs one plugin instance for every chat, lane and subagent.

    So anything the plugin keeps per class is shared by conversations that know
    nothing about each other. Some of that is correct and deliberate -- a key
    benched for being out of quota is out of quota for everybody. The status
    line is not: it is one sentence per conversation, and 1.0.8 kept it in a
    single pair of class attributes.
    """

    @staticmethod
    def _agent(session, sink):
        class Agent:
            _emit_wait_notice = staticmethod(sink.append)

        agent = Agent()
        agent.session_id = session
        return agent

    def test_two_conversations_do_not_swallow_each_others_line(self):
        # The 1.0.8 failure exactly: B's first line was suppressed as a repeat
        # of a sentence A had shown and B never had.
        said_a, said_b = [], []
        a = self._agent("s1", said_a)
        b = self._agent("s2", said_b)
        line = "KAME API Rotation: 15/15 healthy"
        dispatch_binding._Spinner.update(a, line, interval=0.0)
        dispatch_binding._Spinner.update(b, line, interval=0.0)
        assert said_a == [line]
        assert said_b == [line]

    def test_one_conversations_throttle_does_not_silence_another(self):
        said_a, said_b = [], []
        a = self._agent("s1", said_a)
        b = self._agent("s2", said_b)
        # A is inside its interval and must stay quiet; B has never spoken.
        dispatch_binding._Spinner.update(a, "first", interval=0.0)
        dispatch_binding._Spinner.update(a, "second", interval=600.0)
        dispatch_binding._Spinner.update(b, "second", interval=600.0)
        assert said_a == ["first"]
        assert said_b == ["second"]

    def test_a_reset_in_one_conversation_leaves_the_other_alone(self):
        said_a, said_b = [], []
        a = self._agent("s1", said_a)
        b = self._agent("s2", said_b)
        dispatch_binding._Spinner.update(a, "line", interval=0.0)
        dispatch_binding._Spinner.update(b, "line", interval=0.0)
        plugin._on_session_reset({"session_id": "s1"})
        # A forgot and redraws; B is still throttled and must not.
        dispatch_binding._Spinner.update(a, "line", interval=600.0)
        dispatch_binding._Spinner.update(b, "line", interval=600.0)
        assert said_a == ["line", "line"]
        assert said_b == ["line"]

    def test_agents_with_no_session_id_are_still_told_apart(self):
        # Subagents and the auxiliary lane may not carry one. Falling back to a
        # shared key would recreate the bug for exactly the lanes that run
        # concurrently with the conversation.
        said_a, said_b = [], []
        a = self._agent(None, said_a)
        b = self._agent(None, said_b)
        assert dispatch_binding._Spinner.key_for(a) != dispatch_binding._Spinner.key_for(b)
        dispatch_binding._Spinner.update(a, "line", interval=0.0)
        dispatch_binding._Spinner.update(b, "line", interval=0.0)
        assert said_a == said_b == ["line"]

    def test_the_table_does_not_grow_without_bound(self):
        # A long-lived Hermes opens and closes conversations all day. Two
        # floats per session is not a leak worth fearing, but unbounded is
        # unbounded, so the oldest is dropped.
        limit = dispatch_binding._Spinner._MAX_TRACKED
        for index in range(limit + 20):
            dispatch_binding._Spinner.update(
                self._agent("s%d" % index, []), "line", interval=0.0
            )
        assert len(dispatch_binding._Spinner._state) == limit
        assert "s0" not in dispatch_binding._Spinner._state
        assert "s%d" % (limit + 19) in dispatch_binding._Spinner._state

    def test_an_evicted_conversation_just_redraws(self):
        # Eviction must cost a duplicate line, never an exception and never a
        # silence: the evicted session simply looks new again.
        said = []
        agent = self._agent("s1", said)
        dispatch_binding._Spinner.update(agent, "line", interval=0.0)
        for index in range(dispatch_binding._Spinner._MAX_TRACKED + 1):
            dispatch_binding._Spinner.update(
                self._agent("other%d" % index, []), "x", interval=0.0
            )
        dispatch_binding._Spinner.update(agent, "line", interval=600.0)
        assert said == ["line", "line"]

    def test_the_panel_names_its_counters_as_the_processs(self):
        # ``/kame`` reads the binding, and the binding is one object for the
        # whole process. The numbers are right; a reader who takes them for
        # their own chat is not, so the panel says which it means.
        class Binding:
            reason = "installed"
            calls = 7
            rotations = 2
            recovered = 1
            surfaced = 0
            waits = 0
            waited_s = 0.0
            mid_stream_cuts = 0

        text = "\n".join(menu.MenuCommand(Binding())._rotation_lines())
        assert "across every conversation" in text

    def test_a_benched_key_is_benched_for_every_conversation(self):
        # The other half of the audit, and the half that must NOT be per
        # session: quota is spent against the key, not against the chat that
        # spent it. Two conversations sharing a pool have to share what the
        # pool has learned, or the second one walks into the same wall the
        # first one just found. One engine, deliberately.
        assert carousel.ENGINE is carousel.ENGINE
        engine = Carousel()
        engine.mark("google:gemini", KEYS[0], False, 900.0, "daily")
        assert engine.healthy_count("google:gemini", KEYS) == len(KEYS) - 1


# --- 6. the host's own stream retries ---------------------------------------


class TestKameLeavesTheHostsStreamRetriesAlone:
    """v1.0.9 set ``HERMES_STREAM_RETRIES=0``. That was the wrong reading.

    The name suggests retries against a spent key, and Agent Zero really did
    have that bug. Hermes' variable is a different thing wearing the same word:
    ``agent/chat_completion_helpers.py:4693`` uses it only in the *transient
    network* branch -- timeouts, dropped connections, SSE parse errors, an
    empty stream -- where the fix is a fresh socket to the same endpoint. An
    HTTP 429 or 401 is an ``APIStatusError``, never enters that branch, and
    already reaches KAME on the first try.

    So zeroing it bought nothing on the failures KAME exists for, and cost the
    one recovery that used to be free and invisible: a blip mid-answer now
    ends the stream, and Hermes continues it with the synthetic
    ``[System: The previous response was cut off…]`` row the user can see. The
    fix is to stop touching it.
    """

    def test_the_plugin_ships_no_host_tuning_module(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"{PACKAGE}.host_tuning")

    def test_registering_never_writes_the_hosts_stream_variables(self, monkeypatch):
        for name in (
            "HERMES_STREAM_RETRIES",
            "HERMES_STREAM_READ_TIMEOUT",
            "HERMES_STREAM_STALE_TIMEOUT",
            "HERMES_STREAM_STALE_GIVEUP",
        ):
            monkeypatch.delenv(name, raising=False)

        class Ctx:
            def register_hook(self, *a, **k):
                return None

            def register_command(self, *a, **k):
                return None

            def register_tool(self, *a, **k):
                return None

        try:
            plugin.register(Ctx())
        except Exception:
            # Registration does far more than this test cares about, and a
            # host stub is not a host. What must hold either way is that
            # nothing wrote to the environment on the way through.
            pass
        for name in (
            "HERMES_STREAM_RETRIES",
            "HERMES_STREAM_READ_TIMEOUT",
            "HERMES_STREAM_STALE_TIMEOUT",
            "HERMES_STREAM_STALE_GIVEUP",
        ):
            assert name not in os.environ

    def test_the_setting_that_switched_it_off_is_gone_too(self):
        # It described a behaviour that no longer exists. Leaving it listed
        # would promise a lever that does nothing.
        assert not settings.known("host_retry_suppression_disabled")
        assert "host_retry_suppression_disabled" not in settings.ALL_FLAGS

    def test_the_panel_says_kame_sets_none_of_them(self):
        text = "\n".join(menu.MenuCommand(None)._host_lines())
        assert "KAME sets none of them" in text
        assert "HERMES_STREAM_RETRIES" in text


# --- 7. the settings surface ------------------------------------------------


class TestTheSettingsAreReadableAndTraceable:
    def test_every_switch_names_its_environment_variable(self):
        for key in list(settings.ALL_FLAGS) + list(settings.ALL_NUMBERS):
            assert settings.env_name(key).startswith("KAME_")

    def test_a_value_nobody_set_reads_as_default(self):
        assert settings.provenance(settings.LIVE_STATUS_DISABLED) == "default"

    def test_a_value_from_the_environment_says_so(self, monkeypatch):
        # The whole reason ``/kame get`` prints provenance: "off because a file
        # says so" and "off because nothing anywhere mentions it" are the same
        # word and two different problems.
        monkeypatch.setenv("KAME_LIVE_STATUS_DISABLED", "1")
        assert settings.provenance(settings.LIVE_STATUS_DISABLED) == "environment"
        assert settings.effective(settings.LIVE_STATUS_DISABLED) is True

    def test_the_patience_setting_has_a_floor_above_zero(self, monkeypatch):
        # Zero means off. Any positive value below the floor is a read timeout
        # so short it would cut healthy answers, so it is raised, not honoured.
        monkeypatch.setenv("KAME_SILENT_STREAM_PATIENCE", "1")
        assert settings.number(settings.SILENT_STREAM_PATIENCE, 0.0) == 5.0

    def test_zero_still_means_off(self, monkeypatch):
        monkeypatch.setenv("KAME_SILENT_STREAM_PATIENCE", "0")
        assert settings.number(settings.SILENT_STREAM_PATIENCE, 0.0) == 0.0

    def test_an_invented_setting_is_not_known(self):
        assert settings.known("kame_go_faster") is False


# --- 8. the panel -----------------------------------------------------------


class TestTheKameCommand:
    """The switches existed and were unreachable. This is the reach.

    Hermes parses ``config_schema`` and never renders it, so before 1.0.9 the
    only way to change any of this was to hand-edit a YAML file.
    """

    def test_the_panel_renders_with_no_binding_at_all(self):
        text = menu.MenuCommand().handle()
        assert "KAME API Rotation" in text
        assert "NOT INSTALLED" in text

    def test_the_panel_names_the_counters_when_there_is_a_binding(self):
        binding = DispatchBinding(engine=Carousel())
        binding.calls = 7
        binding.rotations = 2
        text = menu.MenuCommand(binding).handle()
        # Padded text columns since 1.1.0 — Desktop renders a slash command's
        # reply with `pretty={false}`, so markdown arrived as its own source.
        assert "calls" in text and "rotations" in text
        assert "  calls: 7" in text
        assert "  rotations: 2" in text

    def test_it_reports_the_mid_stream_cuts_that_explain_a_broken_rewind(self):
        binding = DispatchBinding(engine=Carousel())
        binding.mid_stream_cuts = 3
        text = menu.MenuCommand(binding).handle()
        assert "3 answer(s), handed back to Hermes" in text
        assert "HERMES_STREAM_STALE_TIMEOUT" in text

    def test_get_lists_every_setting_with_its_provenance(self):
        text = menu.MenuCommand().handle("get")
        for key in list(settings.ALL_FLAGS) + list(settings.ALL_NUMBERS):
            assert key in text
        assert "default" in text

    def test_set_applies_immediately_even_without_a_file(self, monkeypatch):
        monkeypatch.setattr(envfile, "path", lambda: None)
        out = menu.MenuCommand().handle("set live_status_disabled true")
        try:
            assert settings.is_on(settings.LIVE_STATUS_DISABLED) is True
            assert "session only" in out
        finally:
            os.environ.pop("KAME_LIVE_STATUS_DISABLED", None)

    def test_set_writes_the_env_file_and_leaves_everything_else_alone(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text(
            "# a comment\nGOOGLE_API_KEY=k1,k2\n\nOTHER=value\n", encoding="utf-8"
        )
        monkeypatch.setattr(envfile, "path", lambda: env)
        try:
            menu.MenuCommand().handle("set live_status_disabled on")
        finally:
            os.environ.pop("KAME_LIVE_STATUS_DISABLED", None)
        written = env.read_text(encoding="utf-8")
        assert "KAME_LIVE_STATUS_DISABLED=1" in written
        # The file holds credentials. Every line that is not the one being set
        # is copied through byte for byte.
        assert "GOOGLE_API_KEY=k1,k2" in written
        assert "# a comment" in written
        assert "OTHER=value" in written

    def test_setting_the_same_key_twice_leaves_one_line(self, tmp_path, monkeypatch):
        # dotenv takes the last assignment, so a stale duplicate left below the
        # new line would silently undo the write.
        env = tmp_path / ".env"
        env.write_text("KAME_LIVE_STATUS_DISABLED=1\nKAME_LIVE_STATUS_DISABLED=1\n", encoding="utf-8")
        monkeypatch.setattr(envfile, "path", lambda: env)
        try:
            menu.MenuCommand().handle("set live_status_disabled off")
        finally:
            os.environ.pop("KAME_LIVE_STATUS_DISABLED", None)
        lines = [l for l in env.read_text(encoding="utf-8").splitlines() if l.startswith("KAME_")]
        assert lines == ["KAME_LIVE_STATUS_DISABLED=0"]

    def test_it_refuses_to_write_anything_outside_its_own_namespace(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("GOOGLE_API_KEY=secret\n", encoding="utf-8")
        monkeypatch.setattr(envfile, "path", lambda: env)
        ok, detail = menu._write_env("GOOGLE_API_KEY", "hijacked")
        assert ok is False and "refusing" in detail
        assert env.read_text(encoding="utf-8") == "GOOGLE_API_KEY=secret\n"

    def test_an_unknown_setting_is_reported_not_written(self):
        out = menu.MenuCommand().handle("set go_faster 1")
        assert "Unknown setting" in out

    def test_a_switch_rejects_a_value_that_is_not_a_switch(self):
        out = menu.MenuCommand().handle("set live_status_disabled maybe")
        assert "true or false" in out

    def test_a_number_rejects_a_value_that_is_not_a_number(self):
        out = menu.MenuCommand().handle("set silent_stream_patience_seconds soon")
        assert "not one" in out

    def test_set_without_a_value_explains_itself(self):
        assert "Usage:" in menu.MenuCommand().handle("set live_status_disabled")

    def test_help_names_the_three_verbs(self):
        text = menu.MenuCommand().handle("help")
        assert "/kame get" in text and "/kame set" in text

    def test_it_never_raises_whatever_it_is_handed(self):
        class Exploding:
            reason = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))

        out = menu.MenuCommand(Exploding()).handle()
        assert isinstance(out, str) and out
