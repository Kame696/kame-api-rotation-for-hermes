"""The carousel against the real Hermes, with a real agent, sending nothing.

``tests/test_dispatch.py`` proves the rules against a fake agent shaped the way
the real one looks. That is the right place for the rules and the wrong place
for one question it cannot answer: **is the real ``AIAgent`` actually shaped
that way?** A fake that drifts from the host passes forever while the plugin
does nothing.

So this builds a real ``AIAgent`` from the installed Hermes, gives it a real
``CredentialPool`` holding several fake credentials, installs the real binding
into the real ``agent.chat_completion_helpers``, and then swaps *only the two
dispatch functions* for stubs that record which key they were handed and raise
the failures we want to see. No socket is opened, no provider is contacted, and
no real key is read: the keys below are obvious fakes.

Run it with the Hermes venv's interpreter::

    ~/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe tools/live_carousel.py

What it proves, in order:

1. the binding installs into the module the forwarders import;
2. a real agent's keys are found by ``candidates`` — pool first, and the id of
   the entry that sent the request is written back where Hermes reads it;
3. four consecutive healthy calls use four different keys, which is the
   sentence in the manifest;
4. a 503 rotates instead of ending the call, which is the failure this release
   exists for;
5. Google's ``400 API key not valid`` quarantines that one key and the others
   carry the answer;
6. the rotation itself reaches the Events buffer — the `switch` and
   `recovery` rows 1.6.0.1 added, which no unit test can prove are written
   because every one of them reads the module that only *declares* the kinds;
7. a genuinely bad request still stops at once.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HERMES = Path.home() / "AppData/Local/hermes/hermes-agent"
INSTALLED = Path.home() / "AppData/Local/hermes/plugins/hermes-kame-api-rotation"

# A throwaway profile. Nothing here reads or writes the real one — the point of
# the harness is that it can be run on a live machine without touching it.
os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp(prefix="kame-carousel-"))
sys.path.insert(0, str(HERMES))

FAKE_KEYS = [f"AIzaSyFAKEKEY{i}" + "0" * 26 for i in range(4)]

_failures = 0


def check(label: str, got, want) -> None:
    global _failures
    ok = got == want
    if not ok:
        _failures += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got {got!r}, want {want!r}")


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "kame_live_carousel",
        INSTALLED / "__init__.py",
        submodule_search_locations=[str(INSTALLED)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Answer:
    """The response shape the loop reads, reduced to what the binding inspects."""

    class _Message:
        def __init__(self, content):
            self.content = content
            self.tool_calls = None

    class _Choice:
        def __init__(self, content):
            self.message = Answer._Message(content)

    def __init__(self, content="ok"):
        self.choices = [Answer._Choice(content)]


class Boom(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code


def build_agent():
    """A real ``AIAgent``, carrying a real pool of obviously-fake credentials."""
    import run_agent
    from agent.credential_pool import CredentialPool

    agent = run_agent.AIAgent.__new__(run_agent.AIAgent)
    agent.provider = "gemini"
    agent.model = "gemini-3.7-flash"
    agent.api_mode = "chat_completions"
    agent.api_key = FAKE_KEYS[0]
    agent.base_url = "https://generativelanguage.googleapis.com/v1beta"
    agent._client_kwargs = {"api_key": FAKE_KEYS[0], "base_url": agent.base_url}
    agent.client = type("C", (), {"api_key": FAKE_KEYS[0]})()
    agent._credential_pool_entry_id = None
    agent._interrupt_requested = False

    pool = CredentialPool.__new__(CredentialPool)
    entries = []
    for index, key in enumerate(FAKE_KEYS):
        entry = type(
            "Entry",
            (),
            {
                "id": f"fake{index}",
                "runtime_api_key": key,
                "access_token": "",
                "runtime_base_url": agent.base_url,
                "base_url": agent.base_url,
                "last_status": None,
            },
        )()
        entries.append(entry)
    pool.entries = lambda: list(entries)
    pool.provider = "gemini"
    agent._credential_pool = pool
    return agent


def main() -> int:
    print(f"interpreter: {sys.executable}")
    print(f"hermes     : {HERMES}")
    print(f"plugin     : {INSTALLED}")

    plugin = load_plugin()
    dispatch_binding = importlib.import_module("kame_live_carousel.dispatch_binding")
    carousel = importlib.import_module("kame_live_carousel.core.carousel")
    host = importlib.import_module("agent.chat_completion_helpers")

    print("\n[1] the binding installs where the forwarders look")
    binding = dispatch_binding.DispatchBinding(
        engine=carousel.Carousel()
    )
    check("install", binding.install(host), True)
    for name in dispatch_binding._FUNCTIONS:
        check(name, getattr(getattr(host, name), "__kame_carousel__", False), True)

    print("\n[2] a real agent's keys are found, and attribution is written back")
    agent = build_agent()
    keys, entry_by_key = dispatch_binding.candidates(agent)
    check("keys found on a real AIAgent", sorted(keys), sorted(FAKE_KEYS))
    check("each key maps to a pool entry", len(entry_by_key), len(FAKE_KEYS))

    seen = []

    def healthy(a, api_kwargs, **kwargs):
        seen.append(a.api_key)
        return Answer()

    binding.run(healthy, agent, {}, (), {})
    check(
        "the entry id Hermes benches by is the one that sent it",
        agent._credential_pool_entry_id,
        entry_by_key[agent.api_key].id,
    )

    print("\n[3] four healthy calls use four different keys")
    seen.clear()
    for _ in range(4):
        binding.run(healthy, agent, {}, (), {})
    check("distinct keys", sorted(set(seen)), sorted(FAKE_KEYS))
    check("no key used twice", len(seen), len(set(seen)))

    print("\n[4] a 503 rotates instead of ending the call")
    attempts = []

    def flaky(a, api_kwargs, **kwargs):
        attempts.append(a.api_key)
        if len(attempts) < 3:
            raise Boom("503 Service Unavailable", status_code=503)
        return Answer("recovered")

    engine = carousel.Carousel()
    fresh = dispatch_binding.DispatchBinding(engine=engine)
    result = fresh.run(flaky, build_agent(), {}, (), {})
    check("the call returned", result.choices[0].message.content, "recovered")
    check("three different keys were tried", len(set(attempts)), 3)

    print("\n[5] Google's 400 for a dead key quarantines the key, not the run")
    tried = []
    dead = FAKE_KEYS[0]

    def one_bad_key(a, api_kwargs, **kwargs):
        tried.append(a.api_key)
        if a.api_key == dead:
            raise Boom("400 INVALID_ARGUMENT: API key not valid.", status_code=400)
        return Answer("carried by the others")

    engine = carousel.Carousel()
    fresh = dispatch_binding.DispatchBinding(engine=engine)
    live = build_agent()
    for _ in range(4):
        answer = fresh.run(one_bad_key, live, {}, (), {})
    check("the turn still finished", answer.choices[0].message.content, "carried by the others")
    check("the dead key was tried exactly once", tried.count(dead), 1)

    print("\n[6] the rotation itself reaches the Events buffer")
    # 1.6.0.1. `switch`, `recovery` and `wait` had been in the event
    # vocabulary since 1.1.1 and not one of them was ever written, which no
    # unit test could have caught: every one of them reads the module that
    # *declares* the kinds. This runs the real dispatch loop against the real
    # host and then reads what the buffer actually holds afterwards.
    EVENTS = importlib.import_module("kame_live_carousel.core.events").EVENTS

    EVENTS.clear()
    turns = []

    def one_bad_then_good(a, api_kwargs, **kwargs):
        turns.append(a.api_key)
        if len(turns) < 2:
            raise Boom("503 Service Unavailable", status_code=503)
        return Answer("answered on the second key")

    fresh = dispatch_binding.DispatchBinding(engine=carousel.Carousel())
    fresh.run(one_bad_then_good, build_agent(), {}, (), {})
    rows = EVENTS.recent()
    kinds = [row.get("kind") for row in rows]
    check("the refusal was recorded", "rotation" in kinds or "quarantine" in kinds, True)
    check("so was the key that took over", "switch" in kinds, True)
    check("and so was the answer that ended it", "recovery" in kinds, True)
    took_over = next(r for r in rows if r.get("kind") == "switch")
    check("the switch names a key", bool(took_over.get("key")), True)
    check("as a fingerprint, not a key", took_over["key"].startswith("key:"), True)
    answered = next(r for r in rows if r.get("kind") == "recovery")
    check("the recovery says which attempt", "attempt" in (answered.get("reason") or ""), True)

    print("\n[7] a genuinely bad request still stops at once")
    stops = []

    def bad_request(a, api_kwargs, **kwargs):
        stops.append(1)
        raise Boom("400 Unknown name \"temperture\"", status_code=400)

    fresh = dispatch_binding.DispatchBinding(engine=carousel.Carousel())
    try:
        fresh.run(bad_request, build_agent(), {}, (), {})
        raised = False
    except Boom:
        raised = True
    check("it was raised", raised, True)
    check("it was tried once", stops, [1])

    binding.uninstall()
    print(
        "\nthe carousel is live against the real host"
        if not _failures
        else f"\n{_failures} check(s) failed"
    )
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
