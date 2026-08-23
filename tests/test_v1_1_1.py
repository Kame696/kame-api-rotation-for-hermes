"""What 1.1.1 promised, pinned one promise per class.

1.1.0 put a panel on screen and could only *show* things. The work order for
this release is nine items long, and eight of them are variations on one
sentence: a person who has never read this source should be able to run the
plugin, understand it, change it, and see why it did what it did — without a
log file and without asking anyone.

The ninth is the one that matters most, and it is not a UI item at all: when a
provider closes the stream in the middle of an answer, the user must never see
the answer cut. That is the first section below, and it is the longest, because
a seam that reads wrong is worse than no seam at all.

1. The stream seam: one continuous answer, across keys, with nothing repeated.
2. The chip names the whole pool and never outgrows the status bar.
3. Every setting is editable from the panel, and only KAME_ lines are written.
4. The events screen exists, is bounded, and carries no key material.
5. The renamed timeout answers to both names, forever.
6. The manifest declares an integer api_version.
7. Uninstalling takes the Desktop half back out.
8. A first run says what to do; an invalid key says to replace it.
9. Everything visible is in English, and the version is 1.1.1 everywhere.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
DESKTOP_PLUGIN = PLUGIN_DIR / "desktop-ui/plugin.js"
MANIFEST = PLUGIN_DIR / "plugin.yaml"
PACKAGE = "kame_v111_under_test"


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
control = importlib.import_module(f"{PACKAGE}.control")
core = importlib.import_module(f"{PACKAGE}.core")
desktop_ui = importlib.import_module(f"{PACKAGE}.desktop_ui")
dispatch_binding = importlib.import_module(f"{PACKAGE}.dispatch_binding")
envfile = importlib.import_module(f"{PACKAGE}.envfile")
events_module = importlib.import_module(f"{PACKAGE}.core.events")
menu = importlib.import_module(f"{PACKAGE}.menu")
settings = importlib.import_module(f"{PACKAGE}.settings")
state = importlib.import_module(f"{PACKAGE}.state")
stitch = importlib.import_module(f"{PACKAGE}.core.stitch")

DispatchBinding = dispatch_binding.DispatchBinding
Carousel = carousel.Carousel
EVENTS = events_module.EVENTS
Stitcher = stitch.Stitcher

KEYS = [f"AIzaSySEAM{i}" + "7" * 29 for i in range(4)]

UI = DESKTOP_PLUGIN.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    settings.forget()
    for name in list(settings._ENV_FOR.values()) + list(settings._NUMBER_ENV_FOR.values()):
        monkeypatch.delenv(name, raising=False)
    for name in settings._LEGACY_ENV_FOR.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("HERMES_STREAM_READ_TIMEOUT", raising=False)
    carousel.ENGINE.forget()
    EVENTS.clear()
    control.forget()
    state.clear()
    yield
    settings.forget()
    carousel.ENGINE.forget()
    EVENTS.clear()
    control.forget()
    state.clear()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_hermes_home", lambda: tmp_path)
    return tmp_path


# --- the stand-in host ------------------------------------------------------


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
    """A Hermes agent, reduced to what the seam touches.

    ``_fire_stream_delta`` is defined on the CLASS rather than on the instance,
    which is not a detail: the binding installs its own delivery on the
    instance and removes it with ``delattr``, so a host that carried the funnel
    as an instance attribute would lose it after the first attempt — and every
    later attempt would deliver nothing. Hermes carries it on the class; this
    stand-in has to as well, or the test would prove the wrong thing.
    """

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
        #: Every string the user was shown, in order.
        self.shown = []

    def _fire_stream_delta(self, text):
        self.shown.append(text)

    @property
    def screen(self):
        return "".join(self.shown)


def answer(content="hello"):
    message = SimpleNamespace(content=content, tool_calls=None, role="assistant")
    choice = SimpleNamespace(index=0, message=message, finish_reason="stop")
    return SimpleNamespace(id="chatcmpl-1", model="m", choices=[choice], usage=None)


def cut(content, *, tool_names=None):
    """The stub Hermes RETURNS — not raises — when a text stream is dropped."""
    message = SimpleNamespace(content=content, tool_calls=None, role="assistant")
    choice = SimpleNamespace(index=0, message=message, finish_reason="length")
    result = SimpleNamespace(
        id=dispatch_binding.PARTIAL_STUB_ID, model="m", choices=[choice], usage=None
    )
    if tool_names:
        result._dropped_tool_names = list(tool_names)
    return result


def conversation():
    return {"model": "gemini-3.7-flash", "messages": [{"role": "user", "content": "tell me"}]}


def _binding():
    return DispatchBinding(engine=Carousel())


#: Long enough that a continuation repeating it from the top cannot be caught
#: by the tail comparison — which is what forces the restart branch.
LONG = " ".join(f"sentence{index}" for index in range(80))


# --- 1. the seam ------------------------------------------------------------


class TestTheStitcherDecidesWhatToTrim:
    """The pure half: three shapes of continuation, one decision each."""

    def test_a_continuation_that_repeats_a_few_words_drops_the_repeat(self):
        assert stitch.stitch_text("The cat sat on the", "sat on the mat.") == " mat."

    def test_a_continuation_that_repeats_nothing_is_forwarded_whole(self):
        assert stitch.stitch_text("The cat sat on the", " mat.") == " mat."

    def test_the_repeat_is_matched_across_different_spacing_and_case(self):
        # A model rarely reproduces the spacing of the text it continues. A
        # comparison that insisted on it would leave the repetition on screen.
        assert stitch.stitch_text("The cat sat on the", "SAT  ON   THE mat.") == " mat."

    def test_a_short_coincidence_is_not_treated_as_a_repeat(self):
        # "tion" and " the " end English sentences all day. Trimming on one of
        # those would eat real text at the seam.
        assert stitch.stitch_text("an explanation", "tion is over") == "tion is over"

    def test_an_answer_started_again_from_the_top_is_dropped_to_the_new_part(self):
        fresh = LONG + " and finally the end."
        assert stitch.stitch_text(LONG, fresh) == " and finally the end."

    def test_a_restart_that_diverges_starts_showing_at_the_divergence(self):
        # The model repeats a while, then says something else. Everything from
        # the disagreement on is text the user has not read.
        fresh = LONG[:200] + "but actually no."
        shown = stitch.stitch_text(LONG, fresh)
        assert shown.endswith("but actually no.")
        assert "sentence0" not in shown

    def test_nothing_is_held_back_once_the_continuation_ends(self):
        # A continuation shorter than the probe never fills the buffer, so
        # without the flush the last words of an answer would be lost.
        stitcher = Stitcher("The cat sat on the")
        assert stitcher.feed("sat on the mat.") == ""
        assert stitcher.flush() == " mat."

    def test_a_stitcher_with_no_history_is_a_pass_through(self):
        stitcher = Stitcher("")
        assert stitcher.feed("anything at all") == "anything at all"


class TestACutAnswerIsContinuedOnAnotherKey:
    """The whole point of the release, end to end through ``run``."""

    def test_the_user_sees_one_continuous_answer(self):
        agent = Agent()
        attempts = []

        def host(agent_, api_kwargs, **kwargs):
            attempts.append(api_kwargs)
            if len(attempts) == 1:
                agent_._fire_stream_delta("The cat sat on the")
                return cut("The cat sat on the")
            agent_._fire_stream_delta("sat on the mat and purred.")
            return answer("sat on the mat and purred.")

        binding = _binding()
        result = binding.run(host, agent, conversation(), (), {})

        assert agent.screen == "The cat sat on the mat and purred."
        assert result.choices[0].message.content == "The cat sat on the mat and purred."

    def test_the_response_is_not_the_stub_so_hermes_never_explains_a_cut(self):
        # The host appends "[System: The previous response was cut off...]"
        # when it sees the stub. A stitched turn must never hand it one.
        agent = Agent()
        attempts = []

        def host(agent_, api_kwargs, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                agent_._fire_stream_delta("half an ")
                return cut("half an ")
            agent_._fire_stream_delta("half an answer.")
            return answer("half an answer.")

        result = _binding().run(host, agent, conversation(), (), {})
        assert result.id != dispatch_binding.PARTIAL_STUB_ID

    def test_the_continuation_is_asked_for_with_the_answer_so_far(self):
        agent = Agent()
        sent = []

        def host(agent_, api_kwargs, **kwargs):
            sent.append(api_kwargs)
            if len(sent) == 1:
                agent_._fire_stream_delta("The cat sat")
                return cut("The cat sat")
            return answer(" on the mat.")

        _binding().run(host, agent, conversation(), (), {})
        assert sent[1]["messages"][-1] == {"role": "assistant", "content": "The cat sat"}
        # Built from the ORIGINAL request, so a third attempt would carry one
        # trailing assistant message and not three.
        assert len(sent[1]["messages"]) == len(sent[0]["messages"]) + 1

    def test_a_model_that_rewrites_the_whole_answer_does_not_print_it_twice(self):
        agent = Agent()
        attempts = []

        def host(agent_, api_kwargs, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                for word in LONG.split(" "):
                    agent_._fire_stream_delta(word + " ")
                return cut(LONG + " ")
            for word in (LONG + " and finally the end.").split(" "):
                agent_._fire_stream_delta(word + " ")
            return answer(LONG + " and finally the end.")

        _binding().run(host, agent, conversation(), (), {})
        assert agent.screen.count("sentence0 ") == 1
        assert agent.screen.rstrip().endswith("the end.")

    def test_the_counters_separate_the_drop_from_the_repair(self):
        agent = Agent()
        attempts = []

        def host(agent_, api_kwargs, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                agent_._fire_stream_delta("one ")
                return cut("one ")
            agent_._fire_stream_delta("two.")
            return answer("two.")

        binding = _binding()
        binding.run(host, agent, conversation(), (), {})
        assert binding.stream_drops == 1
        assert binding.resumes == 1
        assert binding.stitched == 1
        # The diagnostic that meant "a cut the user actually saw" still does.
        assert binding.mid_stream_cuts == 0

    def test_the_events_screen_records_the_drop_and_the_repair(self):
        agent = Agent()
        attempts = []

        def host(agent_, api_kwargs, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                agent_._fire_stream_delta("one ")
                return cut("one ")
            return answer("two.")

        _binding().run(host, agent, conversation(), (), {})
        kinds = [row["kind"] for row in EVENTS.recent()]
        assert "stream_drop" in kinds
        assert "stitch" in kinds


class TestWhatIsNeverStitched:
    """Three cases where continuing would be a guess, and none is taken."""

    def test_a_drop_inside_a_tool_call_is_handed_back_untouched(self):
        # Half-written JSON arguments are not something a second model call can
        # be asked to finish. Hermes tags this one; KAME leaves it alone.
        agent = Agent()

        def host(agent_, api_kwargs, **kwargs):
            agent_._fire_stream_delta("thinking ")
            return cut("thinking ", tool_names=["read_file"])

        binding = _binding()
        result = binding.run(host, agent, conversation(), (), {})
        assert result.id == dispatch_binding.PARTIAL_STUB_ID
        assert binding.stitched == 0
        assert binding.resumes == 0

    def test_the_flag_turns_the_whole_thing_off(self, monkeypatch):
        monkeypatch.setenv("KAME_STREAM_STITCH_DISABLED", "1")
        agent = Agent()
        attempts = []

        def host(agent_, api_kwargs, **kwargs):
            attempts.append(1)
            agent_._fire_stream_delta("half ")
            return cut("half ")

        binding = _binding()
        result = binding.run(host, agent, conversation(), (), {})
        assert attempts == [1]
        assert binding.resumes == 0
        assert binding.mid_stream_cuts == 1
        assert result.choices[0].message.content == "half "

    def test_the_budget_is_a_ceiling_and_the_rest_goes_back_in_one_piece(
        self, monkeypatch
    ):
        monkeypatch.setenv("KAME_STREAM_RESUME_LIMIT", "1")
        agent = Agent()
        attempts = []

        def host(agent_, api_kwargs, **kwargs):
            attempts.append(1)
            agent_._fire_stream_delta(f"part{len(attempts)} ")
            return cut(f"part{len(attempts)} ")

        binding = _binding()
        result = binding.run(host, agent, conversation(), (), {})
        assert len(attempts) == 2
        assert binding.resumes == 1
        assert binding.mid_stream_cuts == 1
        # Everything that did arrive is returned as one response, so Hermes
        # continues from the whole answer rather than from half of it.
        assert result.choices[0].message.content == "part1 part2 "

    def test_a_host_with_no_delivery_funnel_never_stitches(self):
        # Without ``_fire_stream_delta`` there is no record of what the user
        # saw, and a continuation without that record is the answer twice.
        agent = Agent()
        del type(agent)._fire_stream_delta
        try:
            assert DispatchBinding._resume_budget(agent, conversation()) == 0
        finally:
            type(agent)._fire_stream_delta = Agent.__dict__.get("_fire_stream_delta") or (
                lambda self, text: self.shown.append(text)
            )

    def test_a_connection_that_dies_after_visible_text_is_not_replayed(self):
        # The drop that arrives as an exception rather than as a stub, on a
        # host that shows text through a path KAME cannot capture. Nothing was
        # recorded, so nothing can be continued — and rotating would print the
        # part the user already read a second time. It goes back to Hermes.
        agent = Agent()
        del type(agent)._fire_stream_delta
        attempts = []

        def host(agent_, api_kwargs, on_first_delta=None, **kwargs):
            attempts.append(1)
            if on_first_delta:
                on_first_delta()
            raise RuntimeError("connection reset mid-stream")

        binding = _binding()
        try:
            with pytest.raises(RuntimeError):
                binding.run(host, agent, conversation(), (), {"on_first_delta": lambda: None})
        finally:
            type(agent)._fire_stream_delta = lambda self, text: self.shown.append(text)
        assert attempts == [1]
        assert binding.resumes == 0
        assert binding.mid_stream_cuts == 1

    def test_a_request_shape_kame_does_not_recognise_is_never_rewritten(self):
        assert DispatchBinding._resume_budget(Agent(), {"prompt": "no messages here"}) == 0
        assert stitch.resumable({"messages": "not a list"}) is None


# --- 2. the chip ------------------------------------------------------------


class TestTheChipNamesThePoolAndFitsTheStatusBar:
    """1.1.0 showed the model. Two pools of two different keys looked like one."""

    def test_the_chip_renders_the_whole_identity(self):
        assert "pool.identity" in UI
        assert "`${pool.healthy}/${pool.keys}`" in UI

    def test_only_two_pools_are_shown_in_full(self):
        assert re.search(r"const CHIP_POOLS = 2\b", UI)
        assert "`+${hidden}`" in UI

    def test_a_pool_nothing_has_used_is_left_off(self):
        assert "pool.idle_for_s !== null" in UI
        assert re.search(r"const CHIP_IDLE_AFTER_S = \d+", UI)

    def test_the_pool_in_use_comes_first(self):
        assert "snap?.activity?.identity" in UI

    def test_a_long_model_name_truncates_rather_than_pushing_the_bar_around(self):
        assert "truncate" in UI
        assert "max-w-[22rem]" in UI
        assert "overflow-hidden" in UI
        assert "whitespace-nowrap" in UI

    def test_an_invalid_key_shows_a_red_dot(self):
        assert "bg-destructive" in UI
        assert "refused as credentials" in UI

    def test_the_eta_is_only_rendered_when_there_is_one(self):
        assert "eta === null ? null" in UI

    def test_the_countdown_is_computed_against_the_snapshots_age(self):
        # The file carries seconds measured at ``updated_at``. A chip that
        # printed the number as written would freeze between publishes.
        assert "value - ageSeconds(snap, now)" in UI

    def test_the_pool_snapshot_carries_what_the_chip_needs(self):
        engine = Carousel()
        engine.select("google:gemini-3.7-flash", KEYS[:2])
        row = engine.snapshot()["google:gemini-3.7-flash"]
        assert row["idle_for"] is not None
        assert row["invalid"] == 0
        assert row["invalid_keys"] == []

    def test_an_unused_pool_reports_no_idle_time_at_all(self):
        engine = Carousel()
        engine.mark("google:gemini-3.7-flash", KEYS[0], False, 60.0, "rate")
        row = engine.snapshot()["google:gemini-3.7-flash"]
        assert row["idle_for"] is None


# --- 3. the settings panel --------------------------------------------------


class TestEverySettingIsEditableFromThePanel:
    def test_every_flag_and_number_is_described(self):
        described = {row["key"] for row in settings.describe_all()}
        assert described == set(settings.ALL_FLAGS) | set(settings.ALL_NUMBERS)

    def test_each_description_carries_what_a_control_needs(self):
        for row in settings.describe_all():
            assert row["title"] and row["help"], row["key"]
            assert row["kind"] in {"flag", "number"}
            assert row["source"] in {"default", "environment", "config"}
            if row["kind"] == "number":
                assert row["min"] is not None and row["max"] is not None
                assert row["units"]

    def test_the_source_of_the_value_is_visible(self, monkeypatch):
        assert settings.provenance(settings.DAILY_COOLDOWN) == "default"
        monkeypatch.setenv("KAME_DAILY_COOLDOWN", "120")
        assert settings.provenance(settings.DAILY_COOLDOWN) == "environment"
        assert "sourceLabel" in UI

    def test_only_the_two_switches_that_stop_the_plugin_ask_first(self):
        assert settings.CONSEQUENTIAL == {
            settings.ROTATION_DISABLED,
            settings.CAROUSEL_DISABLED,
        }
        assert "ConfirmDialog" in UI

    def test_a_number_outside_its_range_is_refused_with_a_reason(self):
        value, error = settings.parse(settings.STREAM_RESUME_LIMIT, "500")
        assert value is None
        assert "accepts 0 to 10" in error

    def test_a_counted_setting_refuses_a_fraction(self):
        value, error = settings.parse(settings.STREAM_RESUME_LIMIT, "2.5")
        assert value is None
        assert "whole" in error

    def test_the_panel_validates_with_the_same_bounds_it_was_given(self):
        # The JS repeats the rules, so it has to repeat them from the snapshot
        # rather than from constants written into the file.
        assert "setting.min" in UI and "setting.max" in UI
        assert "setting.off_or_at_least" in UI
        assert "setting.step === 1" in UI


class TestWhatThePanelIsAllowedToAskFor:
    def test_the_action_list_is_closed(self):
        assert control.ACTIONS == ("set", "reset", "reset_all", "clear_pool", "clear_events")

    def test_a_request_naming_something_else_is_refused(self, home, monkeypatch):
        monkeypatch.setattr(envfile, "path", lambda: home / ".env")
        control.control_path().parent.mkdir(parents=True, exist_ok=True)
        control.control_path().write_text(
            json.dumps({"schema": 1, "id": "a", "action": "delete_keys"}), encoding="utf-8"
        )
        control.poll()
        assert control.last_result()["ok"] is False
        assert "is not something KAME does" in control.last_result()["detail"]

    def test_a_request_is_applied_once_and_the_file_is_removed(self, home, monkeypatch):
        monkeypatch.setattr(envfile, "path", lambda: home / ".env")
        path = control.control_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"schema": 1, "id": "b", "action": "set", "key": settings.DAILY_COOLDOWN, "value": "120"}
            ),
            encoding="utf-8",
        )
        assert control.poll() is True
        assert not path.is_file()
        assert control.last_result()["ok"] is True
        assert settings.effective(settings.DAILY_COOLDOWN) == 120.0

    def test_the_same_request_arriving_twice_is_applied_once(self, home, monkeypatch):
        monkeypatch.setattr(envfile, "path", lambda: home / ".env")
        path = control.control_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"schema": 1, "id": "c", "action": "clear_events"})
        path.write_text(body, encoding="utf-8")
        assert control.poll() is True
        path.write_text(body, encoding="utf-8")
        assert control.poll() is False

    def test_a_refusal_still_carries_the_id_the_panel_is_waiting_on(self, home):
        path = control.control_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 99, "id": "d", "action": "set"}), encoding="utf-8")
        control.poll()
        assert control.last_result()["id"] == "d"
        assert control.last_result()["ok"] is False

    def test_reset_removes_the_variable_rather_than_writing_the_default(
        self, home, monkeypatch
    ):
        env = home / ".env"
        env.write_text("OPENAI_API_KEY=sk-not-ours\nKAME_DAILY_COOLDOWN=120\n", encoding="utf-8")
        monkeypatch.setattr(envfile, "path", lambda: env)
        monkeypatch.setenv("KAME_DAILY_COOLDOWN", "120")
        ok, _ = control._apply("reset", settings.DAILY_COOLDOWN, None)
        assert ok is True
        assert "KAME_DAILY_COOLDOWN" not in env.read_text(encoding="utf-8")
        assert settings.provenance(settings.DAILY_COOLDOWN) == "default"

    def test_clearing_the_pool_forgets_health_and_no_key(self):
        carousel.ENGINE.mark("google:gemini-3.7-flash", KEYS[0], False, 3600.0, "daily")
        assert carousel.ENGINE.snapshot()
        ok, detail = control._apply("clear_pool", "", None)
        assert ok is True
        assert carousel.ENGINE.snapshot() == {}
        assert "delete" not in detail


class TestTheSecretsFileIsTouchedSurgically:
    def test_only_kame_lines_are_written(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        monkeypatch.setattr(envfile, "path", lambda: env)
        ok, _ = envfile.write("OPENAI_API_KEY", "sk-hijack")
        assert ok is False
        assert not env.exists()

    def test_every_other_byte_of_the_file_survives(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        original = "# my keys\nOPENAI_API_KEY=sk-secret\n\nGOOGLE_API_KEY=k1,k2\n"
        env.write_text(original, encoding="utf-8")
        monkeypatch.setattr(envfile, "path", lambda: env)
        envfile.write("KAME_DAILY_COOLDOWN", "120")
        after = env.read_text(encoding="utf-8")
        for line in original.splitlines():
            assert line in after.splitlines()
        assert "KAME_DAILY_COOLDOWN=120" in after

    def test_a_second_assignment_is_not_left_behind(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("KAME_DAILY_COOLDOWN=1\nKAME_DAILY_COOLDOWN=2\n", encoding="utf-8")
        monkeypatch.setattr(envfile, "path", lambda: env)
        envfile.write("KAME_DAILY_COOLDOWN", "3")
        assert env.read_text(encoding="utf-8").count("KAME_DAILY_COOLDOWN=") == 1

    def test_nothing_from_the_file_is_ever_logged(self, tmp_path, monkeypatch, caplog):
        env = tmp_path / ".env"
        env.write_text("OPENAI_API_KEY=sk-secret-canary\n", encoding="utf-8")
        monkeypatch.setattr(envfile, "path", lambda: env)
        with caplog.at_level("DEBUG"):
            envfile.write("KAME_DAILY_COOLDOWN", "120")
            envfile.forget("KAME_DAILY_COOLDOWN")
        assert "sk-secret-canary" not in caplog.text


# --- 4. the events screen ---------------------------------------------------


class TestTheEventsScreen:
    def test_it_is_bounded(self):
        for index in range(120):
            EVENTS.add("rotation", identity="x", key=f"f{index}")
        assert len(EVENTS.recent()) == events_module.MAX_EVENTS

    def test_the_newest_is_first(self):
        EVENTS.add("rotation", reason="older")
        EVENTS.add("rotation", reason="newer")
        assert EVENTS.recent()[0]["reason"] == "newer"

    def test_a_key_passed_by_mistake_cannot_survive_whole(self):
        EVENTS.add("rotation", key=KEYS[0])
        assert KEYS[0] not in json.dumps(EVENTS.recent())

    def test_the_snapshot_carries_the_events_and_no_raw_log(self, home):
        EVENTS.add("quarantine", identity="google:x", key="abcd1234", reason="rate limited", code=429)
        document = state.snapshot(binding=None)
        assert document["events"][0]["code"] == 429
        assert "traceback" not in json.dumps(document).lower()

    def test_the_panel_has_a_screen_for_them(self):
        assert "EventsPage" in UI
        assert "EVENT_LABELS" in UI
        for kind in ("rotation", "quarantine", "invalid_key", "stream_drop", "stitch"):
            assert kind in UI

    def test_the_menu_can_print_them_too(self):
        EVENTS.add("quarantine", identity="google:x", key="abcd1234", reason="rate limited", code=429)
        text = menu.MenuCommand(_binding()).handle("events")
        assert "rate limited" in text
        assert "429" in text


# --- 5. the renamed timeout -------------------------------------------------


class TestTheRenamedTimeout:
    def test_the_new_name_is_what_the_plugin_reads(self):
        assert settings.STREAM_SILENCE_TIMEOUT == "stream_silence_timeout_seconds"

    def test_the_old_name_still_works_in_the_environment(self, monkeypatch):
        monkeypatch.setenv("KAME_SILENT_STREAM_PATIENCE", "30")
        assert settings.number(settings.STREAM_SILENCE_TIMEOUT, 0.0) == 30.0

    def test_the_new_name_outranks_the_old_one(self, monkeypatch):
        monkeypatch.setenv("KAME_SILENT_STREAM_PATIENCE", "30")
        monkeypatch.setenv("KAME_STREAM_SILENCE_TIMEOUT", "45")
        assert settings.number(settings.STREAM_SILENCE_TIMEOUT, 0.0) == 45.0

    def test_the_old_name_still_works_in_the_config_file(self):
        ctx = SimpleNamespace(
            get_config=lambda key, default=None: (
                20 if key == "silent_stream_patience_seconds" else default
            )
        )
        settings.load(ctx)
        assert settings.number(settings.STREAM_SILENCE_TIMEOUT, 0.0) == 20.0

    def test_the_old_constant_still_points_at_the_same_setting(self):
        assert settings.SILENT_STREAM_PATIENCE == settings.STREAM_SILENCE_TIMEOUT

    def test_the_old_name_is_accepted_by_the_command(self, monkeypatch, tmp_path):
        # The compatibility promise has to hold where a person types, not only
        # where a file is read: somebody who wrote the old name into a config a
        # year ago will type that same name at /kame set.
        monkeypatch.setattr(envfile, "path", lambda: tmp_path / ".env")
        try:
            out = menu.MenuCommand().handle("set silent_stream_patience_seconds 30")
            assert "Unknown setting" not in out
            assert settings.number(settings.STREAM_SILENCE_TIMEOUT, 0.0) == 30.0
            # And it is written down under the name this release uses.
            assert "KAME_STREAM_SILENCE_TIMEOUT=30" in (tmp_path / ".env").read_text(
                encoding="utf-8"
            )
        finally:
            dispatch_binding.os.environ.pop("KAME_STREAM_SILENCE_TIMEOUT", None)

    def test_the_old_name_is_accepted_by_the_panel(self, monkeypatch, tmp_path):
        monkeypatch.setattr(envfile, "path", lambda: tmp_path / ".env")
        try:
            ok, _ = control._apply("set", "silent_stream_patience_seconds", 30)
            assert ok is True
            assert settings.number(settings.STREAM_SILENCE_TIMEOUT, 0.0) == 30.0
        finally:
            dispatch_binding.os.environ.pop("KAME_STREAM_SILENCE_TIMEOUT", None)

    def test_it_leaves_the_key_before_the_cut_rather_than_after(self, monkeypatch):
        monkeypatch.setenv("KAME_STREAM_SILENCE_TIMEOUT", "30")
        agent = Agent()
        with dispatch_binding._SilenceTimeout(agent):
            assert dispatch_binding.os.environ["HERMES_STREAM_READ_TIMEOUT"] == "30"

    def test_the_host_variable_is_put_back_afterwards(self, monkeypatch):
        monkeypatch.setenv("KAME_STREAM_SILENCE_TIMEOUT", "30")
        with dispatch_binding._SilenceTimeout(Agent()):
            pass
        assert "HERMES_STREAM_READ_TIMEOUT" not in dispatch_binding.os.environ

    def test_a_number_the_user_set_themselves_is_never_overruled(self, monkeypatch):
        monkeypatch.setenv("KAME_STREAM_SILENCE_TIMEOUT", "30")
        monkeypatch.setenv("HERMES_STREAM_READ_TIMEOUT", "600")
        with dispatch_binding._SilenceTimeout(Agent()):
            assert dispatch_binding.os.environ["HERMES_STREAM_READ_TIMEOUT"] == "600"

    def test_a_local_model_is_left_alone(self, monkeypatch):
        monkeypatch.setenv("KAME_STREAM_SILENCE_TIMEOUT", "30")
        agent = Agent()
        agent.base_url = "http://localhost:11434/v1"
        with dispatch_binding._SilenceTimeout(agent):
            assert "HERMES_STREAM_READ_TIMEOUT" not in dispatch_binding.os.environ

    def test_off_by_default_changes_nothing(self):
        with dispatch_binding._SilenceTimeout(Agent()):
            assert "HERMES_STREAM_READ_TIMEOUT" not in dispatch_binding.os.environ


# --- 6. the manifest --------------------------------------------------------


class TestTheManifestLoadsWithoutAWarning:
    """Hermes logged ``'0.20' is not an integer; ignoring`` on every boot."""

    def test_api_version_is_an_integer(self):
        raw = MANIFEST.read_text(encoding="utf-8")
        found = re.search(r"^api_version:\s*(\S+)\s*(?:#.*)?$", raw, re.MULTILINE)
        assert found, "the manifest declares no api_version"
        assert found.group(1) == "1"

    def test_the_version_is_quoted_and_the_api_version_is_not(self):
        # The point of the pair is the *quoting*: an unquoted 1.1.1 is a
        # YAML float that loses its last component, and an api_version in
        # quotes is the string Hermes refuses. Neither is about which release
        # this is, so the version itself is read from the code.
        raw = MANIFEST.read_text(encoding="utf-8")
        assert re.search(rf'^version:\s*"{re.escape(core.__version__)}"', raw, re.MULTILINE)

    def test_every_new_setting_is_declared(self):
        raw = MANIFEST.read_text(encoding="utf-8")
        for key in (
            settings.STREAM_SILENCE_TIMEOUT,
            settings.STREAM_STITCH_DISABLED,
            settings.STREAM_RESUME_LIMIT,
        ):
            assert key in raw

    def test_the_old_name_is_still_declared_so_an_old_config_still_validates(self):
        assert "silent_stream_patience_seconds" in MANIFEST.read_text(encoding="utf-8")


# --- 7. uninstalling --------------------------------------------------------


class TestTheDesktopHalfCanBeTakenBackOut:
    def test_install_then_uninstall_leaves_nothing_behind(self, home):
        assert desktop_ui.install() is True
        target = desktop_ui.target()
        assert target.is_file()
        assert desktop_ui.uninstall() is True
        assert not target.exists()
        assert not target.parent.exists()

    def test_a_directory_holding_somebody_elses_file_is_left_alone(self, home):
        desktop_ui.install()
        target = desktop_ui.target()
        (target.parent / "notes.txt").write_text("mine", encoding="utf-8")
        desktop_ui.uninstall()
        assert target.parent.is_dir()
        assert (target.parent / "notes.txt").read_text(encoding="utf-8") == "mine"

    def test_uninstalling_what_was_never_installed_is_not_an_error(self, home):
        assert desktop_ui.uninstall() is False
        assert desktop_ui.report()["reason"] == "was not installed"

    def test_the_plugin_registers_the_removal_with_the_host(self):
        source = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
        assert "on_unload" in source
        assert "desktop_ui.uninstall" in source


# --- 8. first run, and a key that will never work ---------------------------


class TestTheFirstRunAndTheInvalidKey:
    def test_a_fresh_install_says_so_rather_than_showing_zeroes(self):
        assert state.snapshot(binding=None)["first_run"] is True

    def test_it_stops_being_a_first_run_once_a_key_is_used(self):
        carousel.ENGINE.select("google:gemini-3.7-flash", KEYS[:2])
        assert state.snapshot(binding=None)["first_run"] is False

    def test_the_panel_has_something_to_say_on_a_first_run(self):
        assert "FirstRun" in UI
        assert "Getting started" in UI
        assert "comma" in UI

    def test_an_invalid_key_is_reported_as_replaceable_not_as_resting(self):
        binding = _binding()
        error = type("AuthError", (Exception,), {})("API key not valid. Please pass a valid API key.")
        error.status_code = 400
        binding._on_failure("google:gemini-3.7-flash", KEYS[0], error, "label", 1, False)
        rows = [row for row in EVENTS.recent() if row["kind"] == "invalid_key"]
        assert rows, [row["kind"] for row in EVENTS.recent()]
        assert "replace this key" in rows[0]["reason"]

    def test_the_snapshot_counts_invalid_keys_apart_from_resting_ones(self):
        # Through the process-wide engine, because that is the one the snapshot
        # reads — a pool nobody but this test can see would prove nothing.
        binding = DispatchBinding(engine=carousel.ENGINE)
        error = type("AuthError", (Exception,), {})("API key not valid. Please pass a valid API key.")
        error.status_code = 400
        binding._on_failure("google:gemini-3.7-flash", KEYS[0], error, "label", 1, False)
        row = state.snapshot(binding=binding)["pools"][0]
        assert row["invalid"] == 1
        assert row["invalid_keys"] and KEYS[0] not in row["invalid_keys"]

    def test_the_panel_tells_the_user_to_replace_it(self):
        assert "replace" in UI.lower()
        assert "Waiting will not repair one" in UI


# --- 9. the release itself --------------------------------------------------


class TestTheReleaseIsConsistent:
    def test_the_version_is_the_same_everywhere_it_is_written_down(self):
        # One number in four places. Which number it is belongs to the release
        # that sets it — this file only insists that nothing disagrees, and
        # that 1.1.1 is still on the record it was written for.
        version = core.__version__
        assert tuple(int(part) for part in version.split(".")) >= (1, 1, 1)
        assert state.__version__ == version
        assert f'"{version}"' in MANIFEST.read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert f"## [{version}]" in changelog
        assert "## [1.1.1]" in changelog

    def test_the_snapshot_schema_moved_with_the_document(self):
        # The two halves are a pair: the panel refuses a document it does not
        # understand. Which number they are on belongs to the release that
        # moves them — this file only insists they moved together, and that
        # nothing went back below the shape 1.1.1 shipped.
        assert state.SCHEMA >= 2
        assert re.search(rf"const SCHEMA = {state.SCHEMA}\b", UI)

    def test_the_sidebar_says_the_whole_product_name(self):
        assert "const PRODUCT = 'KAME API Rotation'" in UI
        assert "label: PRODUCT" in UI

    def test_the_panel_imports_nothing_the_loader_would_refuse(self):
        specifiers = set(re.findall(r"""from\s+['"]([^'"]+)['"]""", UI))
        assert specifiers <= {
            "@hermes/plugin-sdk",
            "react",
            "react/jsx-runtime",
            "react/jsx-dev-runtime",
        }

    def test_nothing_visible_is_written_in_another_language(self):
        # The plugin ships in English, every string, warnings included.
        for path in list(PLUGIN_DIR.rglob("*.py")) + [DESKTOP_PLUGIN, MANIFEST]:
            if "__pycache__" in str(path):
                continue
            text = path.read_text(encoding="utf-8")
            if path.name == "resolver_binding.py":
                # Quotes the provider's own Portuguese error message as
                # evidence of the bug it fixes. That is a quotation, not a
                # string this plugin prints.
                continue
            found = re.findall(r"[áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇñ]\w*", text)
            assert not found, f"{path.name}: {found[:5]}"

    def test_the_readme_says_how_to_install_it_and_under_what_licence(self):
        # Asserted by content and not by heading text: 1.2.0 retitled the
        # sections ("Install" -> "Install - three lines", "Screenshots" ->
        # "What you see") and a test that fails on a better heading is a test
        # that teaches you to leave the heading alone.
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "hermes plugins install" in readme
        assert "MIT" in readme
        # The two views a reader needs before installing: the status chip and
        # the panel. Both are shown as layout blocks, so look for their content.
        assert "KAME  gemini:" in readme
        assert "POOL HEALTH" in readme

    def test_the_licence_is_a_file_and_not_a_claim(self):
        licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
        assert "MIT License" in licence
        assert "WITHOUT WARRANTY OF ANY KIND" in licence
