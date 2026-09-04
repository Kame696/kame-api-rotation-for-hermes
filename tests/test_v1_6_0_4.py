"""A thinking token is not an answer.

Measured on the owner's machine, 04/09/2026, build ``1076cd6664c1``. Two turns
out of a twelve-minute session ended with a provider error while thirteen
healthy keys sat in the pool. Both were 503s, both arrived on ``gemini-3.8-flash``
— a thinking model — and both were handed back to Hermes with the sentence
*"dropped mid-answer and cannot be continued"*. The host's own log for the same
two moments says:

    Streaming failed before delivery: Gemini HTTP 503 (UNAVAILABLE)

*Before* delivery. Nothing had reached the screen. KAME believed otherwise, and
believing it cost the rotation: a drop after visible text cannot be retried
plainly (the text would print twice), so KAME stops rotating and gives the turn
to the host. The host then ran its own backoff — 2.6s, then 5.5s — and
recovered, which is why the owner saw no error and still waited.

The flag that said "the user has seen part of the answer" was ``_Progress.any``,
and it was set by three things that show no answer at all:

* ``on_first_delta`` — the host fires it on the first *reasoning* delta
  (``chat_completion_helpers.py:3937``) and on the first *tool name*
  (``:4039``), not only on text. On a thinking model it fires on every turn,
  before the answer exists.
* ``thinking_callback`` — the host's spinner. It is called with a kawaii face
  and a verb (``conversation_loop.py:2483``) and with ``""`` to clear it. KAME's
  own rotation notice goes through the same channel (``run_agent.py:1088``), so
  KAME could set the flag by announcing that it was rotating.
* ``_fire_stream_delta(None)`` — the end-of-stream sentinel
  (``conversation_loop.py:6911``).

So 1.6.0.4 splits the flag in two. ``any`` means *answer text reached the user*
and only a delivery callback carrying a non-empty string can set it.
``last_activity`` means *the stream is alive* and everything still sets it, so
the silence timeout is unchanged.

The one case that must not regress is the reason the flag was coarse to begin
with: Hermes has a path that delivers real text through
``stream_delta_callback`` directly, bypassing ``_fire_stream_delta``, while tool
calls accumulate (``chat_completion_helpers.py:3852``). That text is on the
screen and KAME cannot capture it, so it must still forbid a plain retry.
``TestTextKameCannotCaptureStillForbidsAReplay`` pins it.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1604_under_test"


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
core = importlib.import_module(f"{PACKAGE}.core")
dispatch_binding = importlib.import_module(f"{PACKAGE}.dispatch_binding")
events_module = importlib.import_module(f"{PACKAGE}.core.events")
settings = importlib.import_module(f"{PACKAGE}.settings")

DispatchBinding = dispatch_binding.DispatchBinding
Carousel = carousel.Carousel
EVENTS = events_module.EVENTS

KEYS = [f"AIzaSyTHINK{i}" + "7" * 28 for i in range(4)]
IDENTITY = "google:gemini-3.8-flash"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    settings.forget()
    for name in list(settings._ENV_FOR.values()) + list(settings._NUMBER_ENV_FOR.values()):
        monkeypatch.delenv(name, raising=False)
    carousel.ENGINE.forget()
    EVENTS.clear()
    dispatch_binding._NO_PREFILL.clear()
    yield
    settings.forget()
    carousel.ENGINE.forget()
    EVENTS.clear()
    dispatch_binding._NO_PREFILL.clear()


# --- the stand-in host ------------------------------------------------------


class Boom(Exception):
    def __init__(self, message="", status_code=None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


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
    """Carries every channel the real host carries, and records each one apart.

    ``shown`` is the answer. ``spinner`` is the status line. ``first_deltas``
    counts the one-shot the host fires on whatever arrives first. Keeping them
    in three lists rather than one is the whole subject of this file.
    """

    def __init__(self, keys=KEYS):
        self.provider = "google"
        self.model = "gemini-3.8-flash"
        self.api_mode = "chat_completions"
        self.api_key = keys[0] if keys else ""
        self._credential_pool = Pool(keys) if keys else None
        self._client_kwargs = {"api_key": self.api_key}
        self.client = Client(self.api_key)
        self._credential_pool_entry_id = None
        self._interrupt_requested = False
        self.stream_delta_callback = None
        self.shown = []
        self.spinner = []

    def _fire_stream_delta(self, text):
        self.shown.append(text)

    def thinking_callback(self, text):
        self.spinner.append(text)

    @property
    def screen(self):
        return "".join(t for t in self.shown if isinstance(t, str))


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
    return {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "tell me"}]}


def _binding(sleep=None):
    return DispatchBinding(engine=Carousel(), sleep=sleep)


def content_of(result):
    return str(getattr(result.choices[0].message, "content", "") or "")


def _kinds():
    return [row["kind"] for row in EVENTS.recent()]


UNAVAILABLE = (
    "Gemini HTTP 503 (UNAVAILABLE): This model is currently experiencing "
    "high demand. Spikes in demand are usually temporary."
)


# --- 1. the defect, from the log --------------------------------------------


class TestAReasoningTokenIsNotAnAnswer:
    """``on_first_delta`` fires on thinking. The turn must still rotate."""

    def test_a_503_after_reasoning_rotates_instead_of_ending_the_turn(self):
        # 02:03:02 and 02:03:37 on 04/09/2026, in one test. Before 1.6.0.4 the
        # first `on_first_delta` marked the answer as delivered, so the 503 was
        # read as a mid-answer cut and handed to Hermes with three untouched
        # keys in the pool.
        attempts = []
        slept = []

        def host(agent_, api_kwargs, **kwargs):
            attempts.append(agent_.api_key)
            kwargs["on_first_delta"]()  # the model started thinking
            if len(attempts) < 3:
                raise Boom(UNAVAILABLE, status_code=503)
            agent_._fire_stream_delta("recovered")
            return answer("recovered")

        binding = _binding(sleep=slept.append)
        result = binding.run(
            host, Agent(), conversation(), (), {"on_first_delta": lambda: None}
        )
        assert content_of(result) == "recovered"
        assert len(set(attempts)) == 3
        # Never mistaken for a cut answer, and never handed back.
        assert binding.stream_drops == 0
        assert binding.mid_stream_cuts == 0
        assert binding.surfaced == 0
        assert "surfaced" not in _kinds()

    def test_the_answer_is_printed_once(self):
        # The fear that made the flag coarse: a retry that reprints. There was
        # nothing to reprint, and the screen proves it.
        attempts = []

        def host(agent_, api_kwargs, **kwargs):
            attempts.append(agent_.api_key)
            kwargs["on_first_delta"]()
            if len(attempts) == 1:
                raise Boom(UNAVAILABLE, status_code=503)
            agent_._fire_stream_delta("The cat sat on the mat.")
            return answer("The cat sat on the mat.")

        agent = Agent()
        binding = _binding(sleep=lambda _s: None)
        binding.run(host, agent, conversation(), (), {"on_first_delta": lambda: None})
        assert agent.screen == "The cat sat on the mat."
        assert agent.screen.count("The cat sat") == 1

    def test_a_tool_name_is_not_an_answer_either(self):
        # The host fires the same one-shot when a tool call's name becomes
        # known (`chat_completion_helpers.py:4039`), before any text exists.
        attempts = []

        def host(agent_, api_kwargs, **kwargs):
            attempts.append(agent_.api_key)
            kwargs["on_first_delta"]()  # _fire_tool_gen_started("write_file")
            if len(attempts) == 1:
                raise Boom(UNAVAILABLE, status_code=503)
            return answer("done")

        binding = _binding(sleep=lambda _s: None)
        result = binding.run(
            host, Agent(), conversation(), (), {"on_first_delta": lambda: None}
        )
        assert content_of(result) == "done"
        assert binding.mid_stream_cuts == 0


class TestTheSpinnerIsNotAnAnswer:
    """``thinking_callback`` carries a face and a verb, never the reply."""

    def test_a_503_after_a_spinner_frame_rotates(self):
        attempts = []

        def host(agent_, api_kwargs, **kwargs):
            attempts.append(agent_.api_key)
            agent_.thinking_callback("(o_o) Pondering...")
            if len(attempts) < 3:
                raise Boom(UNAVAILABLE, status_code=503)
            return answer("recovered")

        binding = _binding(sleep=lambda _s: None)
        result = binding.run(host, Agent(), conversation(), (), {})
        assert content_of(result) == "recovered"
        assert binding.mid_stream_cuts == 0
        assert binding.surfaced == 0

    def test_the_spinner_still_reaches_the_host(self):
        # Wrapped, not swallowed. The user keeps their status line.
        agent = Agent()

        def host(agent_, api_kwargs, **kwargs):
            agent_.thinking_callback("(o_o) Pondering...")
            return answer()

        _binding().run(host, agent, conversation(), (), {})
        assert agent.spinner == ["(o_o) Pondering..."]

    def test_kames_own_rotation_notice_cannot_set_the_flag(self):
        # ``_emit_wait_notice`` routes through ``thinking_callback``, so before
        # 1.6.0.4 KAME could mark the answer as seen by announcing a rotation.
        progress = dispatch_binding._Progress()
        agent = Agent()
        dispatch_binding._install_shims(agent, progress, {})
        agent.thinking_callback("KAME: resting a key for 20s")
        assert progress.any is False


class TestTheEndOfStreamSentinelIsNotAnAnswer:
    def test_none_through_the_funnel_shows_nothing(self):
        progress = dispatch_binding._Progress()
        delivery = dispatch_binding._Delivery(progress, lambda text: None)
        delivery(None)
        delivery("")
        assert progress.any is False
        assert delivery.text == ""

    def test_text_through_the_funnel_shows_something(self):
        progress = dispatch_binding._Progress()
        delivery = dispatch_binding._Delivery(progress, lambda text: None)
        delivery("hello")
        assert progress.any is True
        assert delivery.text == "hello"


# --- 2. what must not regress -----------------------------------------------


class TestTextKameCannotCaptureStillForbidsAReplay:
    """``chat_completion_helpers.py:3852`` — real text, past the funnel."""

    def test_a_drop_after_uncaptured_text_is_handed_back(self):
        # Hermes fires ``stream_delta_callback`` directly while tool calls
        # accumulate. KAME never sees that text, so it cannot continue the
        # answer — but it must not retry either, or the user reads it twice.
        agent = Agent()
        agent.stream_delta_callback = lambda text: None

        def host(agent_, api_kwargs, **kwargs):
            agent_.stream_delta_callback("I'll use the tool")
            raise Boom(UNAVAILABLE, status_code=503)

        binding = _binding(sleep=lambda _s: None)
        with pytest.raises(Boom):
            binding.run(host, agent, conversation(), (), {})
        assert binding.stream_drops == 1
        assert binding.mid_stream_cuts == 1

    def test_an_empty_call_on_that_channel_is_not_text(self):
        # ``stream_delta_callback(None)`` at ``conversation_loop.py:6911``.
        agent = Agent()
        agent.stream_delta_callback = lambda text: None
        attempts = []

        def host(agent_, api_kwargs, **kwargs):
            attempts.append(agent_.api_key)
            agent_.stream_delta_callback(None)
            if len(attempts) == 1:
                raise Boom(UNAVAILABLE, status_code=503)
            return answer("recovered")

        binding = _binding(sleep=lambda _s: None)
        assert content_of(binding.run(host, agent, conversation(), (), {})) == "recovered"
        assert binding.mid_stream_cuts == 0


class TestACutAnswerIsStillContinued:
    def test_visible_text_then_a_cut_is_stitched_not_replayed(self):
        agent = Agent()
        calls = []

        def host(agent_, api_kwargs, **kwargs):
            calls.append(api_kwargs)
            if len(calls) == 1:
                agent_._fire_stream_delta("The cat sat")
                return cut("The cat sat")
            agent_._fire_stream_delta(" on the mat.")
            return answer("The cat sat on the mat.")

        binding = _binding(sleep=lambda _s: None)
        result = binding.run(host, agent, conversation(), (), {})
        assert content_of(result) == "The cat sat on the mat."
        assert binding.stitched == 1
        assert agent.screen == "The cat sat on the mat."


class TestTheSilenceTimeoutStillSeesEverything:
    def test_stir_moves_the_clock_without_claiming_delivery(self):
        progress = dispatch_binding._Progress()
        before = progress.last_activity
        time.sleep(0.01)
        progress.stir()
        assert progress.last_activity > before
        assert progress.any is False

    def test_touch_moves_the_clock_and_claims_delivery(self):
        progress = dispatch_binding._Progress()
        before = progress.last_activity
        time.sleep(0.01)
        progress.touch()
        assert progress.last_activity > before
        assert progress.any is True

    def test_a_thinking_model_that_never_speaks_is_not_called_silent(self):
        # The reason ``stir`` exists rather than "shim nothing": a model that
        # reasons for ninety seconds before its first word is working, not
        # wedged, and the silence timeout has to be able to tell.
        progress = dispatch_binding._Progress()
        agent = Agent()
        call_kwargs = {"on_first_delta": lambda: None}
        dispatch_binding._install_shims(agent, progress, call_kwargs)
        before = progress.last_activity
        time.sleep(0.01)
        call_kwargs["on_first_delta"]()
        assert progress.last_activity > before
        assert progress.any is False


# --- 3. the wiring is the rule ----------------------------------------------


class TestWhichChannelCountsAsDelivery:
    def test_carries_text_truth_table(self):
        carries = dispatch_binding._carries_text
        assert carries(("hello",)) is True
        assert carries(("",)) is False
        assert carries((None,)) is False
        assert carries(()) is False
        assert carries((123,)) is False

    def test_the_delivery_channels_set_the_flag(self):
        for name in ("stream_delta_callback", "_stream_callback"):
            progress = dispatch_binding._Progress()
            agent = Agent()
            setattr(agent, name, lambda text: None)
            dispatch_binding._install_shims(agent, progress, {})
            getattr(agent, name)("real text")
            assert progress.any is True, name

    def test_the_liveness_channels_do_not(self):
        progress = dispatch_binding._Progress()
        agent = Agent()
        call_kwargs = {"on_first_delta": lambda: None}
        dispatch_binding._install_shims(agent, progress, call_kwargs)
        agent.thinking_callback("(o_o) Pondering...")
        call_kwargs["on_first_delta"]()
        assert progress.any is False

    def test_the_originals_come_back(self):
        agent = Agent()
        agent.stream_delta_callback = lambda text: None
        before = (agent.stream_delta_callback, agent.thinking_callback)
        progress = dispatch_binding._Progress()
        restore = dispatch_binding._install_shims(agent, progress, {})
        assert agent.stream_delta_callback is not before[0]
        assert agent.thinking_callback is not before[1]
        dispatch_binding._remove_shims(agent, restore)
        assert (agent.stream_delta_callback, agent.thinking_callback) == before

    def test_the_return_value_is_passed_through(self):
        # Hermes reads these to control early stopping. A shim that swallowed
        # the answer would stop the stream.
        progress = dispatch_binding._Progress()
        agent = Agent()
        agent.stream_delta_callback = lambda text: "keep going"
        agent.thinking_callback = lambda text: "keep going"
        dispatch_binding._install_shims(agent, progress, {})
        assert agent.stream_delta_callback("x") == "keep going"
        assert agent.thinking_callback("x") == "keep going"


class TestTheRuleIsWrittenWhereItIsRead:
    """A mutation guard: the split must survive somebody tidying the loop."""

    def test_the_spinner_is_not_wired_to_the_delivery_shim(self):
        source = inspect.getsource(dispatch_binding._install_shims)
        spinner_line = next(
            line for line in source.splitlines() if "thinking_callback" in line
        )
        # Named in its own loop, so it cannot be swept back into the delivery
        # tuple by a refactor that only reads the names.
        assert "stream_delta_callback" not in spinner_line

    def test_the_one_shot_is_not_wired_to_the_delivery_shim(self):
        source = inspect.getsource(dispatch_binding._install_shims)
        line = next(
            line for line in source.splitlines() if "on_first_delta" in line and "=" in line
        )
        assert "_stir_shim" in line
        # ``_stir_shim`` ends in ``_shim(`` too, so the mutation this pins is
        # the assignment reverting to the bare delivery wrapper.
        assert "= _shim(" not in line

    def test_progress_still_has_both_verbs(self):
        assert hasattr(dispatch_binding._Progress, "touch")
        assert hasattr(dispatch_binding._Progress, "stir")


class TestTheVersionSaysWhatShipped:
    def test_the_manifest_and_the_core_agree(self):
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        assert f'version: "{core.__version__}"' in manifest

    def test_the_manifest_version_the_installer_accepts_is_unchanged(self):
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        assert "manifest_version: 1" in manifest
