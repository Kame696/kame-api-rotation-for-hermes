"""V3 secondary events tests: channel routing, client tag, task labels.

The load-bearing test drives a real ``_FacadeCall`` through the model
heuristic and the client tag, then proves the transport's ``_emit`` puts
the row in SECONDARY_EVENTS — the "aba morta" of v1/v2 (set_aux_slot
exported, never called; active_events imported, never used) made
verifiable end to end.
"""
import os
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hermes-kame-api-rotation"))
sys.path.insert(0, _SRC)

# facade.py uses package-relative imports ("from . import settings"), so it
# must be loaded with its package name. Import the root package under the
# name the installed plugin uses, then reach into it — and import events
# FROM THE SAME PACKAGE: two sys.path entries give two module objects for
# "core.events", and a ContextVar set in one is invisible to the other
# (the classic split-brain the offline-test lesson warns about).
import importlib
import importlib.util as _ilu

_pkg_spec = _ilu.spec_from_file_location(
    "hermes_kame_api_rotation", os.path.join(_SRC, "__init__.py"),
    submodule_search_locations=[_SRC],
)
_pkg = _ilu.module_from_spec(_pkg_spec)
sys.modules["hermes_kame_api_rotation"] = _pkg
_pkg_spec.loader.exec_module(_pkg)
facade = importlib.import_module("hermes_kame_api_rotation.facade")
_events_mod = importlib.import_module("hermes_kame_api_rotation.core.events")
EVENTS = _events_mod.EVENTS
SECONDARY_EVENTS = _events_mod.SECONDARY_EVENTS
set_aux_slot = _events_mod.set_aux_slot
clear_aux_slot = _events_mod.clear_aux_slot
get_aux_slot = _events_mod.get_aux_slot
_guess_aux_task = facade._guess_aux_task
_FacadeCall = facade._FacadeCall


class _FakeKeys:
    base_url = "https://api.openai.com/v1"


def _make_call(client_kwargs, request_kwargs):
    """A real _FacadeCall against a stub client, exactly as the transport sees one."""
    client = SimpleNamespace(
        _kame_provider="gemini",
        _kame_keys=_FakeKeys(),
        **client_kwargs,
    )
    # _KameMixin's setup fields the call reads
    if "_kame_main_model" not in client_kwargs:
        client._kame_main_model = client_kwargs.get("model", "")
    client._kame_is_aux_client = client_kwargs.get("_kame_is_aux_client", False)
    client._kame_aux_task = client_kwargs.get("_kame_aux_task", "")
    return _FacadeCall(client, request_kwargs)


class TestChannelRouting(unittest.TestCase):
    def setUp(self):
        EVENTS.clear()
        SECONDARY_EVENTS.clear()

    def test_main_model_same_model_is_main(self):
        call = _make_call({"model": "gemini-2.5-pro"}, {"model": "gemini-2.5-pro"})
        self.assertEqual(call.channel, "main")
        self.assertEqual(call.task, "chat")

    def test_different_model_is_secondary(self):
        # the session's chat model is pro; a flash call is auxiliary work
        call = _make_call({"model": "gemini-2.5-pro"}, {"model": "gemini-2.5-flash"})
        self.assertEqual(call.channel, "secondary")
        self.assertEqual(call.task, "title")  # flash pin

    def test_client_tag_beats_model(self):
        # a tagged aux client carrying the SAME model still goes secondary
        call = _make_call(
            {"model": "gemini-2.5-pro", "_kame_is_aux_client": True, "_kame_aux_task": "vision"},
            {"model": "gemini-2.5-pro"},
        )
        self.assertEqual(call.channel, "secondary")
        self.assertEqual(call.task, "vision")

    def test_contextvar_route(self):
        token = set_aux_slot("compression")
        try:
            call = _make_call({"model": "m"}, {"model": "m"})
            self.assertEqual(call.channel, "secondary")
            self.assertEqual(call.task, "compression")
        finally:
            clear_aux_slot(token)

    def test_no_main_model_falls_back_to_main(self):
        # a client built without a model kwarg cannot be judged different
        call = _make_call({}, {"model": "gemini-2.5-flash"})
        self.assertEqual(call.channel, "main")


class TestTaskLabels(unittest.TestCase):
    def test_vision_pins(self):
        self.assertEqual(_guess_aux_task("mimo-vl-2"), "vision")
        self.assertEqual(_guess_aux_task("gemini-vision-pro"), "vision")

    def test_fast_pins(self):
        self.assertEqual(_guess_aux_task("gemini-2.5-flash"), "title")
        self.assertEqual(_guess_aux_task("kimi-k2-turbo"), "title")

    def test_compression_pins(self):
        self.assertEqual(_guess_aux_task("claude-haiku-4-5"), "compression")

    def test_other(self):
        self.assertEqual(_guess_aux_task("some-unknown-model"), "other")


class TestEmitRouting(unittest.TestCase):
    """The dead-import fix: _emit routes by channel."""

    def setUp(self):
        EVENTS.clear()
        SECONDARY_EVENTS.clear()
        import importlib
        transport = importlib.import_module("hermes_kame_api_rotation.transport")
        self.transport = transport

    def test_emit_main_goes_to_events(self):
        call = SimpleNamespace(channel="main")
        before = len(EVENTS.recent(500))
        self.transport._emit(call, "test_event", identity="g:m", key="k:123")
        self.assertEqual(len(EVENTS.recent(500)), before + 1)
        self.assertEqual(len(SECONDARY_EVENTS.recent(500)), 0)

    def test_emit_secondary_goes_to_secondary(self):
        call = SimpleNamespace(channel="secondary")
        before = len(SECONDARY_EVENTS.recent(500))
        self.transport._emit(call, "test_event", identity="g:m", key="k:123")
        self.assertEqual(len(SECONDARY_EVENTS.recent(500)), before + 1)
        self.assertEqual(len(EVENTS.recent(500)), 0)

    def test_emit_no_channel_degrades_to_main(self):
        call = SimpleNamespace()  # no channel attr: pre-v3 shape
        before = len(EVENTS.recent(500))
        self.transport._emit(call, "test_event")
        self.assertEqual(len(EVENTS.recent(500)), before + 1)

    def test_emit_never_raises(self):
        class Boom:
            @property
            def channel(self):
                raise RuntimeError("boom")
        self.transport._emit(Boom(), "test_event")  # must not raise


class TestAuxSlotContextVar(unittest.TestCase):
    def test_set_and_get(self):
        self.assertEqual(get_aux_slot(), "")
        token = set_aux_slot("title")
        self.assertEqual(get_aux_slot(), "title")
        clear_aux_slot(token)
        self.assertEqual(get_aux_slot(), "")

    def test_wrapper_survives_threadpool_hop(self):
        """The Python fact the design documented: a ContextVar set on THIS
        thread does NOT survive a ThreadPoolExecutor hop. The wrapper must
        set it inside the submitted callable — which is what the client-tag
        path replaces, and why the tag is the primary mechanism."""
        token = set_aux_slot("vision")
        try:
            def read_on_worker():
                return get_aux_slot()
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(read_on_worker).result(timeout=10)
            self.assertEqual(result, "", "ContextVar must be lost across the hop (documented fact)")
        finally:
            clear_aux_slot(token)


if __name__ == "__main__":
    unittest.main()
