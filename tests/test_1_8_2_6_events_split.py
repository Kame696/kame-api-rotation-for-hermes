"""1.8.2.6: the Events tab shows the chat, the Background tab shows Hermes' side jobs.

Found on the owner's real use (2026-10-13): through 1.8.2.5 every auxiliary
call landed on the main Events tab. ``set_aux_slot`` was never called, and the
model heuristic never fired because Hermes does not pass a model to
``create_client``. The fix reads Hermes' own per-call marker
(``agent.auxiliary_client._RELAY_AUX_CALL_CONTEXT``), which Hermes binds for
every auxiliary call and copies into the thread that runs the provider call.

Also here: the task is written on each Background row, the list scrolls
inside itself, and a call that has waited long says so in one line.
"""
import contextvars
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from types import SimpleNamespace

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hermes-kame-api-rotation"))
sys.path.insert(0, _SRC)

import importlib
import importlib.util as _ilu

if "hermes_kame_api_rotation" not in sys.modules:
    _spec = _ilu.spec_from_file_location(
        "hermes_kame_api_rotation", os.path.join(_SRC, "__init__.py"), submodule_search_locations=[_SRC]
    )
    _pkg = _ilu.module_from_spec(_spec)
    sys.modules["hermes_kame_api_rotation"] = _pkg
    _spec.loader.exec_module(_pkg)
facade = importlib.import_module("hermes_kame_api_rotation.facade")
transport = importlib.import_module("hermes_kame_api_rotation.transport")
state = importlib.import_module("hermes_kame_api_rotation.state")
integrity = importlib.import_module("hermes_kame_api_rotation.integrity")
events = importlib.import_module("hermes_kame_api_rotation.core.events")

PLUGIN_JS = os.path.join(_SRC, "desktop", "plugin.js")


class _FakeKeys:
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _call(request_model, main_model=""):
    client = SimpleNamespace(
        _kame_provider="gemini", _kame_keys=_FakeKeys(), _kame_main_model=main_model,
        _kame_is_aux_client=False, _kame_aux_task="",
    )
    return facade._FacadeCall(client, {"model": request_model, "messages": []})


class _HermesAuxModule:
    """A stand-in for ``agent.auxiliary_client`` with the real marker's shape."""

    def __enter__(self):
        self._saved = {name: sys.modules.get(name) for name in ("agent", "agent.auxiliary_client")}
        self.var = contextvars.ContextVar("auxiliary_relay_call", default=None)
        module = types.ModuleType("agent.auxiliary_client")
        module._RELAY_AUX_CALL_CONTEXT = self.var
        sys.modules["agent.auxiliary_client"] = module
        sys.modules.setdefault("agent", types.ModuleType("agent"))
        return self

    def __exit__(self, *exc):
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def scope(self, task):
        """What ``_relay_aux_call_scope`` binds for one auxiliary call."""
        return self.var.set({"task": task, "request_id": "aux-test", "attempt_count": 0})


class TestHermesMarkerDecidesTheTab(unittest.TestCase):
    def test_no_marker_is_the_chat(self):
        with _HermesAuxModule():
            call = _call("gemini-3.5-flash-lite")
        self.assertEqual((call.channel, call.task), ("main", "chat"))

    def test_marked_call_goes_to_background_with_its_task(self):
        with _HermesAuxModule() as hermes:
            token = hermes.scope("title_generation")
            try:
                call = _call("gemini-3.5-flash-lite")
            finally:
                hermes.var.reset(token)
        self.assertEqual((call.channel, call.task), ("secondary", "title_generation"))

    def test_same_model_as_the_chat_still_splits_by_task(self):
        # The owner's point: an auxiliary on the chat's own model shares the
        # pool — and must still be shown as background work.
        with _HermesAuxModule() as hermes:
            token = hermes.scope("compression")
            try:
                call = _call("moonshotai/kimi-k3", main_model="moonshotai/kimi-k3")
            finally:
                hermes.var.reset(token)
        self.assertEqual((call.channel, call.task), ("secondary", "compression"))
        self.assertEqual(call.identity, _call("moonshotai/kimi-k3").identity)  # same pool

    def test_unknown_task_is_still_background(self):
        with _HermesAuxModule() as hermes:
            token = hermes.scope("unknown")
            try:
                call = _call("gemini-3.5-flash-lite")
            finally:
                hermes.var.reset(token)
        self.assertEqual((call.channel, call.task), ("secondary", "auxiliary"))

    def test_marker_crosses_into_hermes_protected_call_thread(self):
        # Hermes runs the provider callback in a daemon thread with
        # contextvars.copy_context() — the call is built there.
        with _HermesAuxModule() as hermes:
            token = hermes.scope("vision")
            try:
                ctx = contextvars.copy_context()
                out = {}
                worker = threading.Thread(target=lambda: out.update(call=ctx.run(_call, "gemini-3.8-flash")))
                worker.start()
                worker.join(5)
            finally:
                hermes.var.reset(token)
        self.assertEqual((out["call"].channel, out["call"].task), ("secondary", "vision"))

    def test_marker_gone_after_the_aux_call(self):
        with _HermesAuxModule() as hermes:
            hermes.var.reset(hermes.scope("title_generation"))
            call = _call("gemini-3.5-flash-lite")
        self.assertEqual(call.channel, "main")

    def test_hermes_without_the_module_degrades_to_main(self):
        saved = sys.modules.pop("agent.auxiliary_client", None)
        try:
            self.assertEqual(events.host_aux_task(), "")
            self.assertEqual(_call("x").channel, "main")
        finally:
            if saved is not None:
                sys.modules["agent.auxiliary_client"] = saved

    def test_a_broken_marker_never_raises(self):
        with _HermesAuxModule():
            sys.modules["agent.auxiliary_client"]._RELAY_AUX_CALL_CONTEXT = object()
            self.assertEqual(events.host_aux_task(), "")
            self.assertEqual(_call("x").channel, "main")


class TestRowsCarryTheirTask(unittest.TestCase):
    def setUp(self):
        events.EVENTS.clear()
        events.SECONDARY_EVENTS.clear()

    def test_background_row_names_its_task(self):
        call = SimpleNamespace(channel="secondary", task="title_generation")
        transport._emit(call, events.SENT, identity="gemini:x", key="key:abc", reason="")
        rows = events.SECONDARY_EVENTS.recent()
        self.assertEqual(rows[0]["task"], "title_generation")
        self.assertEqual(events.EVENTS.recent(), [])

    def test_main_row_shape_unchanged(self):
        call = SimpleNamespace(channel="main", task="chat")
        transport._emit(call, events.SENT, identity="gemini:x", key="key:abc")
        row = events.EVENTS.recent()[0]
        self.assertNotIn("task", row)
        self.assertEqual(events.SECONDARY_EVENTS.recent(), [])

    def test_panel_gets_the_whole_background_buffer(self):
        call = SimpleNamespace(channel="secondary", task="compression")
        for _ in range(180):
            transport._emit(call, events.SENT, identity="gemini:x")
        self.assertEqual(len(state._secondary_event_rows()), 180)


class TestStillWaiting(unittest.TestCase):
    def setUp(self):
        events.INFLIGHT.clear()

    def test_running_call_is_listed_and_counts_only_refusals(self):
        call = SimpleNamespace(channel="secondary", task="title_generation", identity="gemini:flash-lite")
        events.INFLIGHT.start(call)
        transport._emit(call, events.SENT, identity="gemini:flash-lite")
        transport._emit(call, "rotation", identity="gemini:flash-lite", reason="server")
        transport._emit(call, "rotation", identity="gemini:flash-lite", reason="rate_limit")
        row = state._inflight_rows()[0]
        self.assertEqual((row["channel"], row["task"], row["failures"], row["reason"]),
                         ("secondary", "title_generation", 2, "rate_limit"))
        events.INFLIGHT.end(call)
        self.assertEqual(state._inflight_rows(), [])

    def test_abandoned_entry_expires(self):
        call = SimpleNamespace(channel="main", task="chat", identity="x")
        events.INFLIGHT.start(call)
        self.assertEqual(events.INFLIGHT.rows(now=time.time() + events.InFlight.STALE_S + 1), [])

    def test_transport_registers_and_releases_both_paths(self):
        seen = []
        call = SimpleNamespace(channel="main", task="chat", identity="x",
                               wire=SimpleNamespace(result=lambda value: value))

        def fake_drive(_call, *, stream):
            seen.append(len(events.INFLIGHT.rows()))
            yield transport._Done("answer")

        t = transport.KameTransport()
        t._drive = fake_drive
        self.assertEqual(t.complete(call), "answer")
        self.assertEqual(list(t._views(call)), [])
        self.assertEqual(seen, [1, 1])
        self.assertEqual(events.INFLIGHT.rows(), [])

    def test_stream_closed_early_still_releases(self):
        call = SimpleNamespace(channel="main", task="chat", identity="x")

        def fake_drive(_call, *, stream):
            yield "chunk"
            yield "chunk"

        t = transport.KameTransport()
        t._drive = fake_drive
        views = t._views(call)
        next(views)
        self.assertEqual(len(events.INFLIGHT.rows()), 1)
        views.close()
        self.assertEqual(events.INFLIGHT.rows(), [])


class TestPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PLUGIN_JS, encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_both_lists_scroll_inside(self):
        self.assertIn("overflow-y-auto", self.src.split("const EVENT_LIST_CLASS", 1)[1].split("\n", 1)[0])
        self.assertEqual(self.src.count("className: EVENT_LIST_CLASS"), 2)

    def test_background_filters_by_task(self):
        page = self.src.split("function SecondaryPage", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("setTask(name)", page)
        self.assertIn("filter(event => (event.task || 'other') === active)", page)

    def test_still_waiting_on_both_tabs(self):
        self.assertIn("h(StillWaiting, { channel: 'main'", self.src)
        self.assertIn("h(StillWaiting, { channel: 'secondary'", self.src)

    def test_clear_words_for_400_and_bare_429(self):
        self.assertIn("the provider rejected the request itself", self.src)
        self.assertIn("usually the provider is busy, not your quota", self.src)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_panel_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "plugin.mjs")
            shutil.copy(PLUGIN_JS, target)
            done = subprocess.run(["node", "--check", target], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)


class TestSeamIsWatched(unittest.TestCase):
    def test_marker_is_in_the_seam_check(self):
        with open(integrity.__file__, encoding="utf-8") as fh:
            self.assertIn('"_RELAY_AUX_CALL_CONTEXT"', fh.read())


if __name__ == "__main__":
    unittest.main()
