"""The provider that will not be handed its own turn back.

1.1.1 continued a cut answer by appending the text so far as a trailing
assistant message. Every OpenAI-compatible endpoint and the Anthropic Messages
API accept that. Gemini's native API does not::

    Gemini HTTP 400 (INVALID_ARGUMENT): Requests ending with a model turn are
    not supported.

Which made 1.1.1 strictly worse than 1.1.0 for that provider, in the worst
possible way: a 400 is a refusal of the request, so the carousel surfaced it,
and a cut answer — something the user had already begun reading — became a
failed turn. The feature that existed to stop the user seeing a broken answer
was breaking the whole turn instead.

Three promises are pinned here, in the order they matter:

1. **The turn survives.** Whatever a continuation attempt runs into, the text
   the user has already read comes back as the answer. Nothing KAME does to
   recover an answer may ever cost more than not trying.
2. **The refusal is learned, not guessed.** The continuation is re-shaped to
   end in a user turn, once, from what the provider said — never from its name.
3. **It is paid once.** The second cut answer in the same process is continued
   in the shape that works, without spending a request to rediscover the 400.
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
PACKAGE = "kame_v112_under_test"


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
stitch = importlib.import_module(f"{PACKAGE}.core.stitch")

DispatchBinding = dispatch_binding.DispatchBinding
Carousel = carousel.Carousel
EVENTS = events_module.EVENTS

KEYS = [f"AIzaSyTURN{i}" + "5" * 29 for i in range(4)]

#: The message the provider actually sends back, quoted exactly. The whole
#: recovery keys off recognising this sentence, so a paraphrase here would let
#: a real drift pass unnoticed.
GEMINI_REFUSAL = (
    "Gemini HTTP 400 (INVALID_ARGUMENT): Requests ending with a model turn "
    "are not supported."
)


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


class RateLimited(Exception):
    """A refusal the provider timed itself. Rests the key whatever else exists."""

    def __init__(self, message, status_code=429):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class BadRequest(Exception):
    """What the host raises for a 400. Terminal: no key can fix a bad request."""

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


# --- 1. the sentence itself -------------------------------------------------


class TestTheRefusalIsRecognisedByWhatItSays:
    def test_the_gemini_sentence_is_read_as_a_refused_prefill(self):
        assert stitch.refuses_prefill(GEMINI_REFUSAL) is True

    def test_an_ordinary_bad_request_is_not(self):
        # A 400 is also what an unknown parameter looks like, and re-shaping the
        # request would not help that one — it has to keep being raised.
        assert stitch.refuses_prefill("400 unknown field 'temperture'") is False
        assert stitch.refuses_prefill("") is False

    def test_no_provider_is_named_anywhere_in_the_decision(self):
        # Evidence, not identity: the plugin took a provider allowlist back out
        # in 0.0.3 and this is the same mistake wearing a different hat.
        source = (PLUGIN_DIR / "dispatch_binding.py").read_text(encoding="utf-8")
        marker = source.index("def _prefill_refused")
        body = source[marker : source.index("def _resume_kwargs")]
        assert "gemini" not in body.lower().replace("gemini's", "")
        assert dispatch_binding._prefill_refused("google:gemini-3.7-flash") is False


# --- 2. the two shapes of a continuation ------------------------------------


class TestTheContinuationHasTwoShapes:
    def test_by_default_it_is_a_prefill_and_nothing_else(self):
        messages = stitch.continuation("The cat sat")
        assert messages == [{"role": "assistant", "content": "The cat sat"}]

    def test_the_other_shape_ends_in_a_user_turn(self):
        messages = stitch.continuation("The cat sat", trailing_user=True)
        assert messages[0] == {"role": "assistant", "content": "The cat sat"}
        assert messages[-1]["role"] == "user"
        # The answer so far is still in the conversation, which is what the
        # seam is computed against. Only the last turn changed hands.
        assert "continue" in messages[-1]["content"].lower()

    def test_the_instruction_asks_for_no_preamble_and_no_repeat(self):
        instruction = stitch.CONTINUE_INSTRUCTION.lower()
        assert "do not repeat" in instruction
        assert "preamble" in instruction


# --- 3. the recovery, end to end --------------------------------------------


class TestAProviderThatRefusesThePrefill:
    def test_the_answer_still_arrives_in_one_piece(self):
        agent = Agent()
        sent = []

        def host(agent_, api_kwargs, **kwargs):
            sent.append(api_kwargs)
            if len(sent) == 1:
                agent_._fire_stream_delta("The cat sat")
                return cut("The cat sat")
            if sent[-1]["messages"][-1]["role"] == "assistant":
                raise BadRequest(GEMINI_REFUSAL)
            agent_._fire_stream_delta(" on the mat.")
            return answer("The cat sat on the mat.")

        binding = _binding()
        result = binding.run(host, agent, conversation(), (), {})
        assert content_of(result) == "The cat sat on the mat."
        assert agent.screen == "The cat sat on the mat."
        # Three requests: the cut one, the refused prefill, the one that worked.
        assert len(sent) == 3
        assert sent[2]["messages"][-1]["role"] == "user"

    def test_the_refusal_is_not_counted_against_the_resume_budget(self):
        # It is the same continuation, asked for in a shape the provider will
        # take. Spending a resume on it would make the budget mean "attempts
        # KAME made" rather than "times the answer was continued".
        agent = Agent()
        sent = []

        def host(agent_, api_kwargs, **kwargs):
            sent.append(api_kwargs)
            if len(sent) == 1:
                agent_._fire_stream_delta("half ")
                return cut("half ")
            if sent[-1]["messages"][-1]["role"] == "assistant":
                raise BadRequest(GEMINI_REFUSAL)
            agent_._fire_stream_delta("and the rest.")
            return answer("half and the rest.")

        binding = _binding()
        binding.run(host, agent, conversation(), (), {})
        assert binding.resumes == 1
        assert binding.stitched == 1

    def test_it_is_learned_once_and_not_paid_for_again(self):
        agent = Agent()
        shapes = []

        def host(agent_, api_kwargs, **kwargs):
            last = api_kwargs["messages"][-1]["role"]
            shapes.append(last)
            if last == "assistant":
                raise BadRequest(GEMINI_REFUSAL)
            if shapes.count("user") == 1:
                agent_._fire_stream_delta("first ")
                return cut("first ")
            agent_._fire_stream_delta("second.")
            return answer("first second.")

        binding = _binding()
        binding.run(host, agent, conversation(), (), {})
        # The first cut costs one refused request; every continuation after it
        # is asked for in the shape that works.
        assert shapes == ["user", "assistant", "user"]

        again = _binding()
        shapes.clear()
        again.run(host, Agent(), conversation(), (), {})
        assert "assistant" not in shapes

    def test_the_events_screen_says_what_happened(self):
        agent = Agent()
        sent = []

        def host(agent_, api_kwargs, **kwargs):
            sent.append(api_kwargs)
            if len(sent) == 1:
                agent_._fire_stream_delta("half ")
                return cut("half ")
            if sent[-1]["messages"][-1]["role"] == "assistant":
                raise BadRequest(GEMINI_REFUSAL)
            return answer("half done.")

        _binding().run(host, agent, conversation(), (), {})
        reasons = [row["reason"] for row in EVENTS.recent(20)]
        assert any("user turn" in reason for reason in reasons)
        # And never the provider's message itself, which can quote the request.
        assert not any("INVALID_ARGUMENT" in reason for reason in reasons)


# --- 4. a continuation that cannot be saved ---------------------------------


class TestARefusedContinuationNeverCostsTheTurn:
    def test_a_fatal_error_returns_the_answer_so_far(self):
        agent = Agent()
        sent = []

        def host(agent_, api_kwargs, **kwargs):
            sent.append(api_kwargs)
            if len(sent) == 1:
                agent_._fire_stream_delta("The cat sat")
                return cut("The cat sat")
            raise BadRequest("400 unknown field 'temperture'")

        binding = _binding()
        result = binding.run(host, agent, conversation(), (), {})
        # Not raised: the user is holding half an answer, and half an answer is
        # worth more than a traceback.
        assert content_of(result) == "The cat sat"
        assert binding.mid_stream_cuts == 1
        assert binding.surfaced == 0

    def test_a_second_refusal_of_the_new_shape_does_not_loop(self):
        agent = Agent()
        sent = []

        def host(agent_, api_kwargs, **kwargs):
            sent.append(api_kwargs)
            if len(sent) == 1:
                agent_._fire_stream_delta("The cat sat")
                return cut("The cat sat")
            raise BadRequest(GEMINI_REFUSAL)

        binding = _binding()
        result = binding.run(host, agent, conversation(), (), {})
        assert content_of(result) == "The cat sat"
        # The cut request, the prefill, the user-turn shape. Then it stops.
        assert len(sent) == 3

    def test_a_bad_request_on_the_first_call_is_still_raised(self):
        # Nothing has been shown, nothing is being continued: this is the
        # user's request being refused, and hiding it would be a lie.
        def host(agent_, api_kwargs, **kwargs):
            raise BadRequest("400 unknown field 'temperture'")

        with pytest.raises(BadRequest):
            _binding().run(host, Agent(), conversation(), (), {})


class TestNoWayOutOfTheLoopLosesTheAnswer:
    """The other four exits, which 1.1.2 found by reading rather than by failing.

    The refusal branch above covers a continuation that is *refused*. A carousel
    has other ways to end: the user presses stop, every key goes to rest, the
    pool agrees the request itself is at fault, or there is no key to pick. Each
    of those either raised the last error or handed the call back to Hermes —
    and once text is on screen both are wrong. Raising throws away an answer the
    user is reading; letting Hermes make the call itself prints that answer
    twice, because its request carries no record of what was already delivered.
    """

    def test_an_interrupt_after_a_cut_keeps_what_was_shown(self):
        agent = Agent()

        def host(agent_, api_kwargs, **kwargs):
            agent_._fire_stream_delta("The cat sat")
            agent_._interrupt_requested = True
            return cut("The cat sat")

        binding = _binding()
        result = binding.run(host, agent, conversation(), (), {})
        assert content_of(result) == "The cat sat"
        assert binding.mid_stream_cuts == 1

    def test_a_pool_that_is_all_resting_keeps_what_was_shown(self):
        # One key, cut mid-answer, and then a refusal the provider itself
        # timed — which benches the key whatever else is in the pool, so the
        # continuation runs out of keys with text already on screen. The wait
        # that follows is unbounded by design, so the only thing simulated here
        # is it ending without a recovery.
        #
        # Until 1.1.3 the cut alone was enough to empty this pool: the key was
        # rested for thirty seconds purely to make the next selection pick a
        # different one, on a pool that had no different one. That rest is gone
        # (see ``test_v1_1_3.py``), so reaching this exit now takes a real
        # refusal — which is the only way it was ever reached in the field.
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
        assert len(calls) == 2
        assert binding.mid_stream_cuts == 1

    def test_a_pool_that_agrees_about_the_request_keeps_what_was_shown(
        self, monkeypatch
    ):
        # The unanimity rule is about the request that was sent — and after a
        # cut, the request that was sent is KAME's own continuation. One resume
        # allowed, so the pool runs out of new things to say on the second call
        # rather than after three rounds of resting and recovering.
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

        binding = _binding()
        result = binding.run(host, agent, conversation(), (), {})
        assert content_of(result) == "The cat sat"
        assert binding.surfaced == 0

    def test_nothing_shown_still_ends_the_way_it_always_did(self):
        # The guard is about text on screen. With none, an interrupt is still
        # an interrupt and the host still gets the call.
        agent = Agent()
        agent._interrupt_requested = True
        calls = []

        def host(agent_, api_kwargs, **kwargs):
            calls.append(api_kwargs)
            return answer("fresh")

        result = _binding().run(host, agent, conversation(), (), {})
        assert content_of(result) == "fresh"
        assert len(calls) == 1


class TestTheDeployHasASecondRoad:
    """A refusal is the right answer to "this would land in a shadow" — and the
    wrong answer to "this machine cannot be deployed to today".

    The container redirects a path, not a volume, so the same directory reached
    through the local administrative share is the real one. ``deploy.py`` probes
    each route separately and uses whichever one a write provably leaves the
    container by; only the translation is pinned here, because the probing part
    is a fact about the machine and not about the code.
    """

    @staticmethod
    def _deploy_module():
        spec = importlib.util.spec_from_file_location(
            "kame_deploy_under_test", ROOT / "tools/deploy.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_drive_letter_becomes_an_administrative_share(self):
        deploy = self._deploy_module()
        view = deploy.unc_view(Path(r"C:\Users\someone\AppData\Local\hermes"))
        assert str(view).replace("/", "\\") == r"\\localhost\C$\Users\someone\AppData\Local\hermes"

    def test_a_path_with_no_drive_has_no_second_road(self):
        deploy = self._deploy_module()
        assert deploy.unc_view(Path(r"\\localhost\C$\Users\someone")) is None


# --- 5. the release ---------------------------------------------------------


class TestTheVersionSaysOneThing:
    def test_the_code_is_at_least_1_1_2(self):
        # Which number it is belongs to whichever release is current; this file
        # only insists that 1.1.2 is still on the record it was written for,
        # and that nothing here is older than the fix it pins.
        assert tuple(int(part) for part in core.__version__.split(".")) >= (1, 1, 2)

    def test_the_manifest_agrees(self):
        assert f'version: "{core.__version__}"' in MANIFEST.read_text(encoding="utf-8")

    def test_the_changelog_has_an_entry(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert "## [1.1.2]" in changelog
        assert "model turn" in changelog

    def test_the_manifest_declares_a_version_the_installer_accepts(self):
        """``hermes plugins install`` raises above 1, whatever the loader reads.

        Hermes' loader (``hermes_cli/plugins.py``) understands manifest
        version 2 and its installer (``hermes_cli/plugins_cmd.py``) does not:
        the installer raises ``requires manifest_version 2, but this installer
        only supports up to 1`` and stops. A file copy — how this plugin was
        deployed while it was being written — never meets that gate, so the
        declaration cost nothing until the day somebody installs it from a
        repository, which is the whole point of publishing one.

        ``tools/host_assumptions.py`` watches the installer's own constant and
        fails when it catches up, which is when this line may go back to 2.
        """
        manifest = MANIFEST.read_text(encoding="utf-8")
        assert "manifest_version: 1" in manifest
        # And the fields that were the reason for saying 2 are still declared:
        # the loader reads them at either version.
        for field in ("license:", "homepage:", "tags:", "api_version: 1"):
            assert field in manifest
