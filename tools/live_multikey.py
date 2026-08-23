"""The shape the user actually has: all the keys in one provider field.

Hermes reads ``GOOGLE_API_KEY`` and stores whatever it finds as one
credential. Somebody who owns seven Google keys types them where the
provider asks for a key, separated by commas - which is how Agent Zero's key
pool takes them, how this plugin's own ``/kame-keys add`` takes them, and the
only way Hermes offers to say "here are my keys" at all. The pool then holds
one credential whose key is the whole comma-joined string: rejected by the
provider, and with exactly one entry to rotate to, nothing to rotate.

``tests/test_multikey.py`` proves the split rules and ``sandbox_binding.py``
proves them against a pool this project built. Neither goes through
``load_pool`` - the real function that reads the real environment variable
through the real provider registry and builds the pool a real Hermes run
uses. That path is what the user is about to exercise by hand, so it is the
one worth watching.

So: three keys in one variable, loaded by Hermes' own loader with the
installed plugin behind it, then two real 429s off a real socket to make the
rotation happen, then a look at what reached the disk - and finally fifteen
keys in that same field, walked to the end, because there is no cap anywhere
and a claim about the code is not a claim about the pool.

What this is not: the provider. The keys below are obvious fakes of the right
length and the only endpoint contacted is this process's own socket. No quota
is spent, no credential is used, and ``HERMES_HOME`` points at a throwaway
directory, so the real profile is neither read nor written.

    "$HERMES_HOME/hermes-agent/venv/Scripts/python.exe" tools/live_multikey.py
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERMES_ROOT = Path(os.environ.get("KAME_HERMES_ROOT", Path.home() / "AppData/Local/hermes"))
AGENT = HERMES_ROOT / "hermes-agent"
INSTALLED_PLUGIN = HERMES_ROOT / "plugins" / "hermes-kame-api-rotation"

PROVIDER = "gemini"
MODEL = "gemini-3.6-flash"
ENV_VAR = "GOOGLE_API_KEY"

# Fakes, but the right length: the split refuses tokens under 16 characters
# because a short fragment in a pasted list is punctuation, not a key. A test
# key shorter than that would be dropped for the right reason and make this
# harness say the wrong thing.
KEYS = [f"AIzaSyFAKE-not-a-real-key-00000000{n:04d}" for n in (1, 2, 3)]

PER_MINUTE = {
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
                        "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                    }
                ],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "21s"},
        ],
    }
}

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
        payload = json.dumps(PER_MINUTE).encode("utf-8")
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


def real_rate_limit_error(base_url: str):
    """One real request, one real SDK exception.

    ``trust_env=False`` because importing Hermes normalises the proxy
    environment variables and a proxy in front of 127.0.0.1 turns this into a
    connection error. One client for the whole run, because a fresh one per
    call leaks a connection pool and eventually fails on ephemeral ports.
    """
    import httpx
    from openai import OpenAI, RateLimitError

    if "client" not in _client_cache:
        _client_cache["client"] = OpenAI(
            api_key="sk-not-a-real-key-0000",
            base_url=base_url,
            max_retries=0,
            http_client=httpx.Client(trust_env=False),
        )
    try:
        _client_cache["client"].chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": "ping"}]
        )
    except RateLimitError as exc:
        return exc
    return None


def keys_of(entries) -> list[str]:
    return [str(getattr(entry, "runtime_api_key", "")) for entry in entries]


def main() -> int:
    if not AGENT.is_dir():
        print(f"Hermes not found at {AGENT}")
        return 2
    if not INSTALLED_PLUGIN.is_dir():
        print(f"the installed plugin is not at {INSTALLED_PLUGIN}")
        return 2

    home = Path(tempfile.mkdtemp(prefix="kame-multikey-"))
    shutil.copytree(
        INSTALLED_PLUGIN,
        home / "plugins" / "hermes-kame-api-rotation",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - hermes-kame-api-rotation\n", encoding="utf-8"
    )
    os.environ["HERMES_HOME"] = str(home)
    # The whole point: one variable, several keys, exactly as typed into the
    # provider field. Nothing else about the environment is arranged.
    os.environ[ENV_VAR] = ",".join(KEYS)
    os.environ.pop("GEMINI_API_KEY", None)
    sys.path.insert(0, str(AGENT))
    print(f"throwaway home: {home}")
    print(f"{ENV_VAR} holds {len(KEYS)} keys separated by commas")

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

        from agent import credential_pool as cp

        check_true(
            "the pool is wrapped at construction",
            getattr(cp.CredentialPool.__init__, "__kame_wrapped__", False),
        )
        if failures:
            return 1

        print("\n[2] Hermes' own loader reads that variable")
        pool = cp.load_pool(PROVIDER)
        offered, _pending = pool._available_entries()
        check("three usable credentials, not one", len(offered), len(KEYS))
        check("each carrying one of the keys typed", sorted(keys_of(offered)), sorted(KEYS))
        check(
            "none of them carrying the whole list",
            [key for key in keys_of(offered) if "," in key],
            [],
        )
        # The parent is still there - it is the row on disk and the one the
        # variable maps to - but it is not a key any provider would accept, so
        # it never takes a turn.
        parent = next(
            entry
            for entry in pool.entries()
            if "#kame-key-" not in str(getattr(entry, "source", ""))
        )
        check("the list itself is never offered", parent.id in {e.id for e in offered}, False)
        for entry in offered:
            print(f"        {entry.label}  id {entry.id}")

        first = pool.select()
        check_true("and one of them is picked to start", first is not None)
        if first is None:
            return 1

        from agent.error_classifier import classify_api_error
        from run_agent import AIAgent

        agent = AIAgent.__new__(AIAgent)
        agent._credential_pool = pool
        agent.provider = PROVIDER
        agent.model = MODEL
        agent.base_url = ""
        agent.log_prefix = ""
        swapped: list = []
        agent._swap_credential = swapped.append

        def refuse(current, *, quiet: bool = False) -> object:
            """One real 429 for the key in hand, classified and recovered from
            exactly the way a live run does it.

            ``quiet`` for the walk in phase 8, which repeats this fifteen
            times and would otherwise print thirty lines saying the same two
            things; the walk asserts on its own result instead.
            """
            agent.api_key = current.runtime_api_key
            agent._credential_pool_entry_id = current.id
            plugin_system.invoke_hook("pre_api_request", provider=PROVIDER, model=MODEL)
            error = real_rate_limit_error(base_url)
            if not quiet:
                check("status off the wire", getattr(error, "status_code", None), 429)
            classified = classify_api_error(
                error,
                provider=PROVIDER,
                model=MODEL,
                approx_tokens=1200,
                context_length=1_000_000,
                num_messages=2,
            )
            swapped.clear()
            recovered, _ = agent._recover_with_credential_pool(
                status_code=429,
                # The host retries the same key once before rotating; this is
                # the second consecutive refusal, the one that benches it.
                has_retried_429=True,
                classified_reason=classified.reason,
                error_context=classified.error_context,
            )
            if not quiet:
                check("the host rotated", recovered, True)
            return swapped[0] if swapped else None

        print("\n[3] a real 429 on the first key moves onto the second")
        second = refuse(first)
        check_true("a different key came back", second is not None and second.id != first.id)
        if second is None:
            return 1
        stored = {entry.id: entry for entry in pool.entries()}
        check("the spent key is benched", stored[first.id].last_status, cp.STATUS_EXHAUSTED)
        check_true("with a deadline of its own", stored[first.id].last_error_reset_at)
        check("and the others are untouched", stored[second.id].last_status, None)
        print(f"        {first.label} -> {second.label}")

        print("\n[4] and again onto the third")
        third = refuse(second)
        check_true(
            "the third key came back",
            third is not None and third.id not in {first.id, second.id},
        )
        offered, _pending = pool._available_entries()
        check(
            "only the key that has not refused is left",
            [entry.id for entry in offered],
            [] if third is None else [third.id],
        )
        if third is not None:
            print(f"        {second.label} -> {third.label}")

        print("\n[5] what reached the disk is still the one row that was typed")
        # The parts are derived on load and recomputed on the next one. If one
        # ever reached auth.json it would be a second copy of a key, stored
        # somewhere the user never put it and left behind when the variable
        # changes.
        written = json.loads((home / "auth.json").read_text(encoding="utf-8"))
        text = json.dumps(written)
        check("no derived part was written", "#kame-key-" in text, False)
        check("no split key was written", any(key in text for key in KEYS[1:]), False)

        print("\n[6] editing the list leaves the other keys as the same keys")
        ids_before = {
            entry.id: entry.runtime_api_key
            for entry in pool.entries()
            if "#kame-key-" in str(getattr(entry, "source", ""))
        }
        os.environ[ENV_VAR] = ",".join([KEYS[0], KEYS[2]])
        reloaded = cp.load_pool(PROVIDER)
        parts = {
            entry.id: entry.runtime_api_key
            for entry in reloaded.entries()
            if "#kame-key-" in str(getattr(entry, "source", ""))
        }
        check("the removed key is gone", len(parts), 2)
        check(
            "and the survivors kept their identity",
            sorted(parts),
            sorted(i for i, key in ids_before.items() if key in (KEYS[0], KEYS[2])),
        )

        print("\n[7] without KAME, the same variable is one unusable credential")
        # Every check above would also pass if Hermes had been splitting the
        # list all along. Taking the binding out puts the host back exactly as
        # it ships, on the same variable, so the difference is visible.
        binding = getattr(plugin_module, "_binding", None)
        check_true("the live binding is reachable", binding is not None)
        if binding is None:
            return 1
        os.environ[ENV_VAR] = ",".join(KEYS)
        binding.uninstall()
        try:
            bare = cp.load_pool(PROVIDER)
            bare_offered, _pending = bare._available_entries()
            check("one credential, not three", len(bare_offered), 1)
            check(
                "and its key is the whole comma-joined list",
                [key.count(",") for key in keys_of(bare_offered)],
                [len(KEYS) - 1],
            )
        finally:
            binding.install(cp)
        check_true(
            "the binding is back",
            getattr(cp.CredentialPool.__init__, "__kame_wrapped__", False),
        )

        print("\n[8] and the number of keys is not a number this plugin has")
        # Asked out loud because the owner asked out loud: is fifteen fine?
        # There is no cap anywhere - the only per-key rule is a length floor -
        # but "no cap in the code I wrote" is a claim about the code, and the
        # question is about the pool. So: fifteen in one field, walked to the
        # end, one refusal at a time.
        # Fifteen keys this run has never seen. Reusing the three above would
        # measure something else entirely: the ledger remembers a spent key by
        # the hash of the key, so the two benched in phases 3 and 4 come back
        # still benched even from a freshly loaded pool - correct, and not what
        # this phase is asking.
        many = [f"AIzaSyFAKE-scale-key-000000000000{n:04d}" for n in range(1, 16)]
        os.environ[ENV_VAR] = ",".join(many)
        started = time.perf_counter()
        big = cp.load_pool(PROVIDER)
        built = time.perf_counter() - started
        offered, _pending = big._available_entries()
        check("fifteen keys, fifteen credentials", len(offered), len(many))
        check("all of them distinct", len(set(keys_of(offered))), len(many))
        print(f"        the pool was built in {built * 1000:.0f} ms")

        agent._credential_pool = big
        current = big.select()
        walked = [current.id]
        for _ in range(len(many)):
            nxt = refuse(current, quiet=True)
            if nxt is None:
                break
            walked.append(nxt.id)
            current = nxt
        check("the pool walks all fifteen", len(set(walked)), len(many))
        offered, _pending = big._available_entries()
        check("and stops when every one has refused", len(offered), 0)
        os.environ[ENV_VAR] = ",".join(KEYS)

        print("\n[9] and the keys are spread before any of them is refused")
        # Every phase above rotates on a refusal, which is the part that was
        # already there. This is the v0.2.6 part: five keys, no refusal at
        # all, and the pool hands out a different one each time instead of
        # hammering the first until the provider says stop.
        five = [f"AIzaSyFAKE-spread-key-00000000000{n:04d}" for n in range(1, 6)]
        os.environ[ENV_VAR] = ",".join(five)
        spread_pool = cp.load_pool(PROVIDER)
        picked = [spread_pool.select().id for _ in range(len(five))]
        check("five selections, five different keys", len(set(picked)), len(five))
        offered, _pending = spread_pool._available_entries()
        check("and none of them was benched to do it", len(offered), len(five))

        # Decision 42: a check that cannot fail is not a check. The shipped
        # switch turns the feature off, and the same five keys must go back to
        # answering the way stock Hermes does.
        os.environ["KAME_SPREAD_DISABLED"] = "1"
        binding.uninstall()
        binding.install(cp)
        try:
            flat = cp.load_pool(PROVIDER)
            flat_picked = {flat.select().id for _ in range(len(five))}
            check("with the switch off, one key takes them all", len(flat_picked), 1)
        finally:
            os.environ.pop("KAME_SPREAD_DISABLED", None)
            binding.uninstall()
            binding.install(cp)
        os.environ[ENV_VAR] = ",".join(KEYS)

        print("\n[10] and /kame-quota shows that spread instead of promising it")
        # v0.2.8. Everything phase 9 proves is invisible to the person running
        # the agent: the pool rotating and the pool hammering one key produce
        # the same silence. This renders the real command off the real binding
        # and reads the section back.
        status = importlib.import_module(f"{plugin_module.__name__}.status")
        fresh = cp.load_pool(PROVIDER)
        seen = [fresh.select().id for _ in range(len(KEYS))]
        text = status.QuotaCommand(binding).handle("")
        check_true(
            "the report has a spread section",
            "How the requests are spread" in text,
        )
        section = text.split("How the requests are spread", 1)[1]
        section = section.split("What KAME was asked", 1)[0]
        check_true(
            "under the model the keys were spent on",
            f"{PROVIDER} · {MODEL}" in section or f"{PROVIDER} " in section,
        )
        counted = 0
        for line in section.splitlines():
            words = line.split()
            for index, word in enumerate(words):
                if word.startswith("request") and index and words[index - 1].isdigit():
                    counted += int(words[index - 1])
        check_true(
            "counting at least the selections just made",
            counted >= len(seen),
        )
        check_true(
            "and naming no key",
            not any(key in text for key in KEYS + five),
        )

        # Decision 42. Every check above would also pass against a section
        # printed from thin air, so the same command is asked again through a
        # stand-in that has the two stores and no dispersion: the numbers have
        # to disappear with it.
        class WithoutTheOrdering:
            _store = binding._store
            _journal = binding._journal

        blind = status.QuotaCommand(WithoutTheOrdering()).handle("")
        check_true(
            "and with the ordering taken away, the numbers are gone",
            "nothing has been handed out yet" in blind,
        )

        print()
        if failures:
            print(f"{len(failures)} FAILED: {', '.join(failures)}")
            return 1
        print("one provider field holding several keys is several keys:")
        print("        Hermes' own loader builds them, the pool rotates through")
        print("        them on real refusals, and the disk keeps the single row.")
        return 0
    finally:
        server.shutdown()
        os.environ.pop(ENV_VAR, None)
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
