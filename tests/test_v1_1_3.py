"""The rest that bought nothing, and the drop that said nothing.

Two defects with the same shape: KAME doing something reasonable-looking on a
path nobody had measured.

1. **A cooldown with nowhere to route to.** Most of this plugin's rests are the
   provider's own words and bind whatever else is in the pool. Two of them are
   not: the thirty seconds a key serves after cutting a stream exists for one
   reason, which is to make the *next* selection pick a different key. On a
   pool with one key there is no different key — so the carousel found nothing
   usable, waited out the full thirty seconds, and then continued the answer on
   the same key it would have used immediately. The install least able to
   afford a wait was the only one paying it.

2. **A drop that was recorded as an answer.** A stream that stops while a tool
   call's arguments are being written cannot be continued by anything: half a
   JSON payload is not something a second model call can finish. KAME correctly
   refuses to try — and then, until 1.1.3, handed the stub back through the
   *success* path. The key that did it was marked healthy, its failure count
   never moved, no event was written, and the next selection treated it as the
   freshest key in the pool. A key that could not hold a long stream was
   invisible for as long as it kept failing that way.

Plus the panel order, which is cosmetic and is pinned here anyway: a hand-kept
order list that silently drops a setting is worse than no order list at all.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
MANIFEST = PLUGIN_DIR / "plugin.yaml"
PACKAGE = "kame_v113_under_test"


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
dispatch_binding = importlib.import_module(f"{PACKAGE}.dispatch_binding")
events_module = importlib.import_module(f"{PACKAGE}.core.events")
settings = importlib.import_module(f"{PACKAGE}.settings")
state = importlib.import_module(f"{PACKAGE}.state")

DispatchBinding = dispatch_binding.DispatchBinding
Carousel = carousel.Carousel
EVENTS = events_module.EVENTS

KEYS = [f"AIzaSyCUT{i}" + "7" * 30 for i in range(3)]


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

    def _fire_stream_delta(self, text):
        self.shown.append(text)

    @property
    def screen(self):
        return "".join(self.shown)


def answer(content="hello"):
    message = SimpleNamespace(content=content, tool_calls=None, role="assistant")
    choice = SimpleNamespace(index=0, message=message, finish_reason="stop")
    return SimpleNamespace(id="chatcmpl-1", model="m", choices=[choice], usage=None)


def cut(content):
    """A stream that stopped mid-sentence. Continuable."""
    message = SimpleNamespace(content=content, tool_calls=None, role="assistant")
    choice = SimpleNamespace(index=0, message=message, finish_reason="length")
    return SimpleNamespace(
        id=dispatch_binding.PARTIAL_STUB_ID, model="m", choices=[choice], usage=None
    )


def cut_in_a_tool_call(content="", names=("write_file",)):
    """The stub Hermes builds when the stream died inside a tool call.

    ``_dropped_tool_names`` is the host's own tag for it, and it is the whole
    reason this case is distinguishable from a cut sentence.
    """
    message = SimpleNamespace(content=content, tool_calls=None, role="assistant")
    choice = SimpleNamespace(index=0, message=message, finish_reason="tool_calls")
    result = SimpleNamespace(
        id=dispatch_binding.PARTIAL_STUB_ID, model="m", choices=[choice], usage=None
    )
    result._dropped_tool_names = list(names)
    return result


def conversation():
    return {"model": "gemini-3.7-flash", "messages": [{"role": "user", "content": "tell me"}]}


def _binding(sleep=None):
    # ``sleep`` is the binding's own injection point, and a test that wants to
    # prove nothing waited must use it rather than patch ``time.sleep``.
    # ``dispatch_binding.time`` *is* the stdlib module, so patching it there
    # reaches every thread in the interpreter — including the plugin's state
    # heartbeat, a ``while True: time.sleep(...)`` daemon that ``register()``
    # starts and never stops. Whether that daemon is already running when this
    # file executes depends on whether ``test_plugin.py`` happened to be
    # ordered earlier, which under ``pytest-randomly`` is a coin flip: the
    # recorder would then collect the daemon's ticks and a "did KAME wait?"
    # assertion would fail for something KAME never did.
    return DispatchBinding(engine=Carousel(), sleep=sleep)


def content_of(result):
    return str(getattr(result.choices[0].message, "content", "") or "")


def _kinds():
    return [row["kind"] for row in EVENTS.recent()]


def _reasons():
    return " | ".join(row["reason"] for row in EVENTS.recent())


# --- 1. the rest that only exists to route away -----------------------------


class TestAKeyIsNotRestedWhenItIsTheOnlyOne:
    """``_rest_unless_it_is_the_only_one``, stated against the engine directly."""

    def test_with_company_the_key_rests(self):
        engine = Carousel()
        applied = dispatch_binding._rest_unless_it_is_the_only_one(
            engine, "google:g", KEYS, KEYS[0], 30.0, "timeout"
        )
        assert applied == 30.0
        assert engine.healthy_count("google:g", KEYS) == len(KEYS) - 1

    def test_alone_it_is_not_rested(self):
        engine = Carousel()
        only = [KEYS[0]]
        applied = dispatch_binding._rest_unless_it_is_the_only_one(
            engine, "google:g", only, only[0], 30.0, "timeout"
        )
        assert applied == 0.0
        # Still usable — which is the whole point. A pool of one that rests its
        # one key has not routed around anything, it has only stopped.
        assert engine.healthy_count("google:g", only) == 1

    def test_the_failure_is_recorded_either_way(self):
        # Only the sentence is dropped, never the history. A key that keeps
        # dropping streams has to keep looking like one.
        engine = Carousel()
        only = [KEYS[0]]
        dispatch_binding._rest_unless_it_is_the_only_one(
            engine, "google:g", only, only[0], 30.0, "timeout"
        )
        row = engine.snapshot()["google:g"]
        assert row["failures"] == 1

    def test_a_pool_whose_other_keys_are_already_resting_counts_as_alone(self):
        engine = Carousel()
        for spent in KEYS[1:]:
            engine.mark("google:g", spent, False, 600.0, "daily")
        applied = dispatch_binding._rest_unless_it_is_the_only_one(
            engine, "google:g", KEYS, KEYS[0], 30.0, "timeout"
        )
        assert applied == 0.0
        assert engine.healthy_count("google:g", KEYS) == 1


class TestASingleKeyContinuesItsAnswerAtOnce:
    def test_the_only_key_is_not_benched_after_a_cut(self):
        # Before 1.1.3 this run rested the key for DROP_REST_S and then waited
        # it out inside `_wait_for_recovery` before continuing the same answer
        # on the same key. Any sleep at all here is that bug coming back.
        slept = []

        only = [KEYS[0]]
        agent = Agent(only)
        calls = []

        def host(agent_, api_kwargs, **kwargs):
            calls.append(api_kwargs)
            if len(calls) == 1:
                agent_._fire_stream_delta("The cat sat")
                return cut("The cat sat")
            agent_._fire_stream_delta(" on the mat.")
            return answer("The cat sat on the mat.")

        binding = _binding(sleep=slept.append)
        result = binding.run(host, agent, conversation(), (), {})
        assert content_of(result) == "The cat sat on the mat."
        assert binding.stitched == 1
        assert slept == []

    def test_a_pool_with_company_still_rests_the_key_that_cut(self):
        slept = []

        agent = Agent()
        calls = []

        def host(agent_, api_kwargs, **kwargs):
            calls.append(api_kwargs)
            if len(calls) == 1:
                agent_._fire_stream_delta("The cat sat")
                return cut("The cat sat")
            agent_._fire_stream_delta(" on the mat.")
            return answer("The cat sat on the mat.")

        binding = _binding(sleep=slept.append)
        binding.run(host, agent, conversation(), (), {})
        # Rested, and no wait: there were other keys to go to, which is exactly
        # when a rest is worth something.
        assert binding.engine.healthy_count("google:gemini-3.7-flash", KEYS) == len(KEYS) - 1
        assert slept == []


# --- 2. the drop that cannot be continued -----------------------------------


class TestAStreamThatStoppedInsideAToolCall:
    def test_the_stub_goes_back_untouched(self):
        # KAME does not try to continue it and does not rewrite it. The host
        # has its own handling for this stub and it must arrive intact.
        agent = Agent()
        stub = cut_in_a_tool_call()

        binding = _binding()
        result = binding.run(lambda *a, **k: stub, agent, conversation(), (), {})
        assert result is stub
        assert result._dropped_tool_names == ["write_file"]

    def test_it_is_counted_as_a_drop_and_not_as_an_answer(self):
        agent = Agent()
        binding = _binding()
        binding.run(lambda *a, **k: cut_in_a_tool_call(), agent, conversation(), (), {})
        assert binding.tool_call_cuts == 1
        assert binding.stream_drops == 1
        # Not a continuation and not a completed answer.
        assert binding.resumes == 0
        assert binding.stitched == 0

    def test_the_key_is_not_marked_healthy(self):
        # The defect in one line: before 1.1.3 this key came out of the turn
        # with a success recorded against it.
        agent = Agent()
        binding = _binding()
        binding.run(lambda *a, **k: cut_in_a_tool_call(), agent, conversation(), (), {})
        rows = binding.engine.snapshot()["google:gemini-3.7-flash"]
        assert rows["failures"] == 1
        assert rows["successes"] == 0

    def test_the_events_screen_names_the_tool(self):
        agent = Agent()
        binding = _binding()
        binding.run(
            lambda *a, **k: cut_in_a_tool_call(names=("read_file",)),
            agent,
            conversation(),
            (),
            {},
        )
        assert "stream_drop" in _kinds()
        assert "tool call" in _reasons()
        assert "read_file" in _reasons()

    def test_the_key_rests_when_there_is_another_to_use(self):
        agent = Agent()
        binding = _binding()
        binding.run(lambda *a, **k: cut_in_a_tool_call(), agent, conversation(), (), {})
        assert binding.engine.healthy_count("google:gemini-3.7-flash", KEYS) == len(KEYS) - 1

    def test_the_only_key_is_not_rested(self):
        only = [KEYS[0]]
        agent = Agent(only)
        binding = _binding()
        binding.run(lambda *a, **k: cut_in_a_tool_call(), agent, conversation(), (), {})
        assert binding.tool_call_cuts == 1
        assert binding.engine.healthy_count("google:gemini-3.7-flash", only) == 1

    def test_text_delivered_before_the_tool_call_is_not_lost(self):
        # A cut sentence continued onto a second key, which then dropped inside
        # a tool call. What the user read still has to come back as the answer.
        agent = Agent()
        calls = []

        def host(agent_, api_kwargs, **kwargs):
            calls.append(api_kwargs)
            if len(calls) == 1:
                agent_._fire_stream_delta("Reading the file")
                return cut("Reading the file")
            return cut_in_a_tool_call()

        binding = _binding()
        result = binding.run(host, agent, conversation(), (), {})
        assert content_of(result) == "Reading the file"
        assert binding.tool_call_cuts == 1

    def test_a_plain_cut_is_still_continued(self):
        # The guard is on the tool-call shape only. A cut sentence must keep
        # taking the 1.1.1 path.
        agent = Agent()
        calls = []

        def host(agent_, api_kwargs, **kwargs):
            calls.append(api_kwargs)
            if len(calls) == 1:
                agent_._fire_stream_delta("half ")
                return cut("half ")
            agent_._fire_stream_delta("and the rest.")
            return answer("half and the rest.")

        binding = _binding()
        result = binding.run(host, agent, conversation(), (), {})
        assert content_of(result) == "half and the rest."
        assert binding.tool_call_cuts == 0
        assert binding.stitched == 1


class TestTheCounterIsReadable:
    def test_the_snapshot_carries_it(self):
        binding = _binding()
        binding.tool_call_cuts = 4
        document = state.snapshot(binding)
        assert document["counters"]["tool_call_cuts"] == 4

    def test_the_panel_has_a_field_for_it(self):
        source = (PLUGIN_DIR / "desktop-ui" / "plugin.js").read_text(encoding="utf-8")
        assert "counters.tool_call_cuts" in source


# --- 3. the order the panel shows -------------------------------------------


class TestThePanelOrder:
    def test_the_silence_timeout_comes_first(self):
        rows = settings.describe_all()
        assert rows[0]["key"] == settings.STREAM_SILENCE_TIMEOUT

    def test_the_numbers_come_before_the_switches(self):
        rows = [row["key"] for row in settings.describe_all()]
        last_number = max(rows.index(key) for key in settings.ALL_NUMBERS)
        first_flag = min(rows.index(key) for key in settings.ALL_FLAGS)
        assert last_number < first_flag

    def test_nothing_is_lost_by_the_ordering(self):
        # The failure mode of a hand-kept order list: a setting added later
        # falls out of the panel entirely. It has to be late, never missing.
        rows = [row["key"] for row in settings.describe_all()]
        assert sorted(rows) == sorted(list(settings.ALL_FLAGS) + list(settings.ALL_NUMBERS))
        assert len(rows) == len(set(rows))

    def test_every_row_still_describes_itself(self):
        for row in settings.describe_all():
            assert row["title"]
            assert row["help"]


# --- 4. the version ---------------------------------------------------------


class TestTheVersion:
    def test_the_manifest_and_the_core_agree(self):
        text = MANIFEST.read_text(encoding="utf-8")
        assert f'version: "{core.__version__}"' in text
        assert tuple(int(part) for part in core.__version__.split(".")) >= (1, 1, 3)

    def test_the_manifest_version_the_installer_accepts_is_unchanged(self):
        # 1.1.2 found that Hermes' installer refuses anything above 1. Nothing
        # in 1.1.3 has any business moving it back.
        text = MANIFEST.read_text(encoding="utf-8")
        assert "manifest_version: 1" in text
