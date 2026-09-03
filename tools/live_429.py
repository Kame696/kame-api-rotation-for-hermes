"""Drive real 429s through the whole installed chain and read the result.

Every other proof stops short of the wire. The sandbox builds the exception
by hand; the corpus feeds payloads to the classifier; ``verify_installed``
proves the wrappers are attached but never fires one. So the single thing
this project's own definition of done demands - a 429 arriving and a cooldown
coming out - had never been observed end to end.

This observes it. A local server answers with a verbatim Google free-tier
429; the real OpenAI SDK turns that into a real ``RateLimitError`` off a real
socket; Hermes' own ``classify_api_error`` runs with the installed plugin
behind its hook; the host's own ``recover_with_credential_pool`` benches and
rotates a real ``CredentialPool``; and then the deadline on the spent key and
the text of ``/kame-quota`` are read back.

Several refusals, not one, because every claim worth checking is a
distinction:

* the same sentence and the same 21-second RetryInfo arrive for a per-minute
  throttle and for a daily cap, and only the counter named in the body tells
  them apart - the first is held 21 seconds, the second for the daily floor
  in ``core/quota.py``;
* the auxiliary lane fires no hooks at all, so a real relay call asks, from
  inside the call, whether the key the main model spent is available to the
  smaller model - and whether it goes back to being withheld the moment the
  call ends;
* a sole credential benched by KAME's own number is offered again for a test
  five minutes later, because a bench of ours must never be the thing that
  locks a model out. Only the clock is moved there; the bench, the pool and
  the plugin are live;
* and with KAME switched off through its own kill switch, the same payload on
  the same socket produces no deadline at all.

What this is not: the provider. No quota is spent and no credential is used -
the keys below are obvious fakes and the only endpoint contacted is this
process's own socket. Everything between the wire and the pool is the real
thing; the sentence on the wire is a capture rather than a live refusal.

``HERMES_HOME`` is redirected to a throwaway directory holding a copy of the
**installed** plugin, so the artifact under test is the deployed one and
nothing is written to the real profile.

    "$HERMES_HOME/hermes-agent/venv/Scripts/python.exe" tools/live_429.py
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("KAME_HERMES_ROOT", Path.home() / "AppData/Local/hermes"))
AGENT = HERMES_ROOT / "hermes-agent"
INSTALLED_PLUGIN = HERMES_ROOT / "plugins" / "hermes-kame-api-rotation"

PROVIDER = "gemini"
MODEL = "gemini-3.6-flash"
# The lane that spends nothing on the main model and gets locked out anyway.
AUX_MODEL = "gemini-3.5-flash-lite"
RETRY_DELAY_SECONDS = 21


def google_429(quota_id: str) -> dict:
    """Verbatim, from the capture in tests/test_core.py: the sentence Google
    sends on every free-tier 429, the quota id naming the counter that blew,
    and the RetryInfo it sends either way."""
    return {
        "error": {
            "code": 429,
            "message": (
                "You exceeded your current quota, please check your plan and billing "
                "details. For more information on this error, read the docs: "
                "https://ai.google.dev/gemini-api/docs/rate-limits."
            ),
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {
                            "quotaMetric": (
                                "generativelanguage.googleapis.com/"
                                "generate_content_free_tier_requests"
                            ),
                            "quotaId": quota_id,
                        }
                    ],
                },
                {
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": f"{RETRY_DELAY_SECONDS}s",
                },
            ],
        }
    }


PER_MINUTE = google_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
PER_DAY = google_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier")

_serving = {"body": PER_MINUTE}

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got  {got}")
        print(f"        want {want}")
        failures.append(label)


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - name fixed by the base class
        # Drain the request body first. Leaving it in the socket buffer makes
        # the connection unusable for the client's next request on the same
        # pool, which arrives as WinError 10053 - a flake that reads like the
        # stub server died mid-run.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        payload = json.dumps(_serving["body"]).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


def serve() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/v1"


_client_cache: dict = {}


def openai_client(base_url: str):
    """``max_retries=0`` because the SDK retries a 429 by default and these are
    the calls that must not. ``trust_env=False`` because importing Hermes
    normalises the proxy environment variables, and a proxy in front of
    127.0.0.1 turns this into a connection error that looks like the stub
    server died."""
    import httpx
    from openai import OpenAI

    # One client for the whole run. A fresh one per call leaks a connection
    # pool each time and, on Windows, eventually draws an ephemeral-port
    # failure that surfaces as APIConnectionError - a flake that reads like
    # the stub server died.
    if "client" not in _client_cache:
        _client_cache["client"] = OpenAI(
            api_key="sk-not-a-real-key-0000",
            base_url=base_url,
            max_retries=0,
            http_client=httpx.Client(trust_env=False),
        )
    return _client_cache["client"]


def real_rate_limit_error(base_url: str):
    """One real request, one real SDK exception."""
    from openai import RateLimitError

    client = openai_client(base_url)
    try:
        client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "ping"}],
        )
    except RateLimitError as exc:
        return exc
    return None


def main() -> int:
    if not AGENT.is_dir():
        print(f"Hermes not found at {AGENT}")
        return 2
    if not INSTALLED_PLUGIN.is_dir():
        print(f"the installed plugin is not at {INSTALLED_PLUGIN}")
        return 2

    home = Path(tempfile.mkdtemp(prefix="kame-live-"))
    shutil.copytree(
        INSTALLED_PLUGIN,
        home / "plugins" / "hermes-kame-api-rotation",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    # Plugins are opt-in and the allow-list lives in the home's own
    # config.yaml, so the throwaway home needs the one line that says this
    # plugin is on. Nothing else is taken from the real profile - no
    # credential, no model config, no history.
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - hermes-kame-api-rotation\n", encoding="utf-8"
    )
    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(AGENT))
    print(f"throwaway home: {home}")

    server, base_url = serve()
    try:
        from hermes_cli import plugins as plugin_system

        print("\n[1] the installed plugin loads under the real manager")
        plugin_system.discover_plugins(force=True)
        manager = plugin_system.get_plugin_manager()
        loaded = [key for key in manager._plugins if "kame" in key]
        check("one kame plugin", len(loaded), 1)
        if not loaded:
            return 1
        plugin_module = manager._plugins[loaded[0]].module
        quota_module = importlib.import_module(plugin_module.__name__ + ".core.quota")
        check_true(
            "hook registered", plugin_system.has_hook("transform_api_error_classification")
        )

        from agent import credential_pool as cp

        check_true(
            "pool wrapped",
            getattr(cp.CredentialPool._mark_exhausted, "__kame_wrapped__", False),
        )
        if failures:
            return 1

        from agent.error_classifier import FailoverReason, classify_api_error

        def one_refusal(body: dict):
            """Announce the call the way the host announces it, take the
            refusal off the wire, and let Hermes classify it with the plugin
            behind the hook. Returns (classified, seconds until the deadline).
            """
            _serving["body"] = body
            plugin_system.invoke_hook("pre_api_request", provider=PROVIDER, model=MODEL)
            error = real_rate_limit_error(base_url)
            if error is None:
                return None, None
            check("status off the wire", getattr(error, "status_code", None), 429)
            before = time.time()
            classified = classify_api_error(
                error,
                provider=PROVIDER,
                model=MODEL,
                approx_tokens=1200,
                context_length=1_000_000,
                num_messages=2,
            )
            reset_at = (classified.error_context or {}).get("reset_at")
            return classified, (None if reset_at is None else float(reset_at) - before)

        print("\n[2] a per-minute 429, off a real socket, through the real classifier")
        classified, waited = one_refusal(PER_MINUTE)
        check_true("the SDK raised and Hermes classified it", classified is not None)
        if classified is None:
            return 1
        check("reason", classified.reason, FailoverReason.rate_limit)
        check_true("rotates the credential", classified.should_rotate_credential)
        check_true("a deadline came back", waited is not None)
        if waited is None:
            return 1
        check(
            "the deadline is the provider's own 21s",
            RETRY_DELAY_SECONDS <= waited <= RETRY_DELAY_SECONDS + 5,
            True,
        )
        print(f"        {waited:.0f}s out")

        print("\n[3] the same 21s on a daily cap is not taken at face value")
        classified, waited = one_refusal(PER_DAY)
        check_true("still classified", classified is not None)
        if classified is None or waited is None:
            return 1
        # Same sentence, same RetryInfo, different counter. The 21 seconds is
        # the provider's generic retry hint, not the answer to "when does a
        # spent daily allowance come back", so the daily cap has to be held
        # far longer than the throttle above.
        #
        # *How much* longer stopped being "until midnight US/Pacific" in
        # 1.2.4, which removed that calculation on purpose: it locked keys for
        # 12-18 hours on a burst error, and the whole of
        # ``seconds_until_pacific_midnight`` was deleted in 1.2.5. The
        # contract since then is the flat floor in ``core/quota.py``, and this
        # check asserted the deleted behaviour until it was first run.
        daily_floor = quota_module.DEFAULT_PER_DAY_BENCH_SECONDS
        check_true(
            "not the provider's 21 seconds",
            waited > RETRY_DELAY_SECONDS * 10,
        )
        check(
            "held for the daily floor instead",
            abs(waited - daily_floor) <= 120,
            True,
        )
        print(f"        {waited / 60:.0f}m out; the daily floor is {daily_floor / 60:.0f}m")
        daily_reset_at = (classified.error_context or {}).get("reset_at")

        print("\n[4] the host's own recovery benches that key and rotates")
        # Hermes announces the model before a call goes out, and KAME learns it
        # there and nowhere else: with nothing in flight, `_adjust` returns the
        # host's answer untouched and every per-model claim below would be
        # measuring a plugin that had been told nothing. Phase 1 checks this
        # hook is registered; until this line nothing ever fired it. Fired the
        # way the dispatcher fires it — once, for the main model.
        plugin_module._on_pre_api_request(provider=PROVIDER, model=MODEL)
        entries = [
            cp.PooledCredential(
                provider=PROVIDER,
                id=uuid.uuid4().hex[:6],
                label=f"key-{index}",
                auth_type=cp.AUTH_TYPE_API_KEY,
                priority=index,
                source="manual",
                access_token=f"AIza-not-a-real-key-{index}",
            )
            for index in range(2)
        ]
        pool = cp.CredentialPool(PROVIDER, entries)

        from run_agent import AIAgent

        agent = AIAgent.__new__(AIAgent)
        agent._credential_pool = pool
        agent.provider = PROVIDER
        agent.model = MODEL
        agent.api_key = entries[0].access_token
        agent._credential_pool_entry_id = entries[0].id
        agent.base_url = ""
        agent.log_prefix = ""
        swapped: list = []
        agent._swap_credential = swapped.append

        recovered, _ = agent._recover_with_credential_pool(
            status_code=429,
            # The host retries the same key once before rotating; this is the
            # second consecutive refusal, which is the one that benches it.
            has_retried_429=True,
            classified_reason=classified.reason,
            error_context=classified.error_context,
        )
        check("the host rotated", recovered, True)
        check("onto the other key", [getattr(e, "id", None) for e in swapped], [entries[1].id])

        # Read back from the pool rather than from the objects handed to it:
        # the pool keeps its own records, and the entry a caller still holds
        # is a stale copy of what was written.
        stored = {entry.id: entry for entry in pool.entries()}
        spent, healthy = stored[entries[0].id], stored[entries[1].id]
        check("the spent key is benched", spent.last_status, cp.STATUS_EXHAUSTED)
        check("with the deadline KAME read", spent.last_error_reset_at, daily_reset_at)
        check("the other key is untouched", healthy.last_status, None)

        print("\n[5] and the pool stops handing that key out")
        available, _pending = pool._available_entries()
        check("only the healthy key is offered", [e.id for e in available], [entries[1].id])

        print("\n[6] /kame-quota can say what happened")
        commands = getattr(manager, "_plugin_commands", None) or {}
        entry = commands.get("kame-quota")
        check_true("the command resolved", entry is not None)
        if entry is None:
            return 1
        handler = entry.get("handler") if isinstance(entry, dict) else getattr(entry, "handler", entry)
        check_true("and is callable", callable(handler))
        if not callable(handler):
            return 1
        text = handler("")
        text = text if isinstance(text, str) else str(text)
        check("it no longer says nothing happened", "nothing recorded yet" in text, False)
        check_true("the model that spent the quota is named", MODEL in text)
        for line in text.splitlines():
            print(f"        {line}")

        print("\n[7] the auxiliary lane, on the same key, keeps its smaller model")
        # The auxiliary lane fires no hooks at all - not pre_api_request, not
        # the classifier - so without the relay wrapper every summarisation
        # and titling call looks unannounced and inherits the bench the main
        # model earned. This asks the question from inside a real relay call:
        # while a smaller model is on the wire, is the key the main model
        # spent available to it?
        from agent import auxiliary_client as aux

        check_true(
            "the relay is wrapped",
            getattr(aux._relay_sync_completion, "__kame_wrapped__", False),
        )
        seen: dict = {}

        def during_the_call(request):
            offered, _ = pool._available_entries()
            seen["offered"] = sorted(entry.id for entry in offered)
            return openai_client(base_url).chat.completions.create(**request)

        from openai import RateLimitError

        try:
            aux._relay_sync_completion(
                None,
                {"model": AUX_MODEL, "messages": [{"role": "user", "content": "ping"}]},
                provider=PROVIDER,
                create=during_the_call,
            )
        except RateLimitError:
            pass
        check(
            "the spent key is offered to the smaller model",
            seen.get("offered"),
            sorted(entry.id for entry in entries),
        )
        after, _ = pool._available_entries()
        check(
            "and withheld again once the call is over",
            [entry.id for entry in after],
            [entries[1].id],
        )

        print("\n[8] a bench of KAME's own never locks a model out")
        # One key, spent on the main model, no alternative. A provider-supplied
        # reset_at disarms the host's own sole-credential escape hatch, so KAME
        # puts one back: the bench is offered for a test on a widening
        # schedule. Everything here is live except the reading of the clock.
        only = cp.PooledCredential(
            provider=PROVIDER,
            id=uuid.uuid4().hex[:6],
            label="sole-key",
            auth_type=cp.AUTH_TYPE_API_KEY,
            priority=0,
            source="manual",
            access_token="AIza-not-a-real-sole-key",
        )
        sole = cp.CredentialPool(PROVIDER, [only])
        classified, _ = one_refusal(PER_DAY)
        sole._mark_exhausted(
            next(entry for entry in sole.entries() if entry.id == only.id),
            429,
            classified.error_context,
        )
        offered, _ = sole._available_entries()
        check("nothing is offered while the bench stands", [e.id for e in offered], [])

        binding = getattr(plugin_module, "_binding", None)
        check_true("the live binding is reachable", binding is not None)
        if binding is None:
            return 1
        probe = importlib.import_module(plugin_module.__name__ + ".core.probe")
        real_clock = binding._clock
        binding._clock = lambda: real_clock() + probe.FIRST_PROBE_SECONDS
        try:
            offered, _ = sole._available_entries()
            check(
                "after five minutes it is offered for a test",
                [e.id for e in offered],
                [only.id],
            )
        finally:
            binding._clock = real_clock

        print("\n[9] with KAME switched off, the same 429 says nothing about timing")
        # Every check above would also pass if the numbers came from somewhere
        # else, so the last thing this does is take the plugin out of the path
        # - through its own kill switch, on the same socket, same payload -
        # and confirm the deadline disappears. A proof that cannot fail is not
        # measuring the thing it names.
        os.environ["KAME_ROTATION_DISABLED"] = "1"
        try:
            classified, waited = one_refusal(PER_DAY)
        finally:
            os.environ.pop("KAME_ROTATION_DISABLED", None)
        check_true("the host still classifies it", classified is not None)
        check("but no deadline comes back", waited, None)
        check(
            "and the host keeps its own judgement",
            None if classified is None else classified.reason,
            FailoverReason.rate_limit,
        )

        print("\n[10] and the report says how much of all that KAME could read")
        # v0.2.9. Every refusal above went through the classification hook,
        # including the one made with the plugin switched off. The counter is
        # the only thing that can tell an install reading its provider from
        # one that stopped recognising it, so it is worth proving against real
        # traffic rather than against a call made by hand.
        status = importlib.import_module(plugin_module.__name__ + ".status")
        text = status.QuotaCommand(binding).handle("")
        section = text.split("What KAME was asked", 1)[-1].split("What KAME has seen", 1)[0]
        check_true("the report has an asked section", "What KAME was asked" in text)
        check_true(
            "counting the refusals that just went through it",
            f"{PROVIDER}" in section and "429" in section,
        )
        check_true(
            "and at least one of them as read",
            "sized" in section and "none sized" not in section.splitlines()[1],
        )
        check_true(
            "with no flag, because these were read",
            "a wait KAME could not read" not in section,
        )
        # Decision 42: the same section, asked about a provider whose payloads
        # KAME has never sized, has to say so out loud.
        plugin_module.runtime.note_classification("nowhere", 429, sized=False)
        flagged = status.QuotaCommand(binding).handle("")
        check_true(
            "and a provider it never read is pointed at",
            "a wait KAME could not read" in flagged,
        )
        check_true("and still no error text anywhere", "AIzaSy" not in flagged)

        print("\n[11] an answer that carried nothing does not unbench the key")
        # v0.3.1. The bench standing on the sole key above is real, written by
        # KAME from a real refusal. The host announces a completed call through
        # post_api_request, and until this version any such announcement was
        # read as proof the key recovered - which retires a bench for good. A
        # squeezed free-tier key can answer 200 with nothing in it, so that is
        # the exact shape tested here, against the installed plugin.
        plugin_module.runtime.forget_empty_answers()
        plugin_module.runtime.note_probe_issued(
            PROVIDER, only.id, MODEL, now=real_clock()
        )
        plugin_module._on_post_api_request(
            provider=PROVIDER,
            model=MODEL,
            assistant_content_chars=0,
            assistant_tool_call_count=0,
        )
        still_held, _ = sole._available_entries()
        check("the bench is still standing", [e.id for e in still_held], [])
        check_true(
            "and the empty answer is counted where it can be seen",
            "answered with nothing" in status.QuotaCommand(binding).handle(""),
        )
        # Decision 42: the same path, given a real answer, has to release it -
        # otherwise this phase would pass just as happily on a plugin whose
        # release path is broken outright. The probe is re-issued because an
        # empty answer leaving it outstanding is the point: nothing about that
        # call was allowed to consume the question.
        check_true(
            "the question it was asked is still outstanding",
            plugin_module.runtime.take_probe(PROVIDER, now=real_clock()) is not None,
        )
        plugin_module.runtime.note_probe_issued(
            PROVIDER, only.id, MODEL, now=real_clock()
        )
        plugin_module._on_post_api_request(
            provider=PROVIDER,
            model=MODEL,
            assistant_content_chars=64,
            assistant_tool_call_count=0,
        )
        released, _ = sole._available_entries()
        check(
            "while a real answer puts the same key back",
            [e.id for e in released],
            [only.id],
        )

        print()
        if failures:
            print(f"{len(failures)} FAILED: {', '.join(failures)}")
            return 1
        print("429s went in off the wire and a per-model bench came out")
        print("        classification, deadline, window, rotation, bench and")
        print("        report all came from the installed plugin, end to end.")
        return 0
    finally:
        server.shutdown()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
