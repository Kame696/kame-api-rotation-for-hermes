"""The field the user actually types into, driven as a real HTTP request.

``live_multikey.py`` watches fifteen keys in one variable become fifteen
credentials — but it starts from the value already being *stored*. For a
desktop user there is exactly one way to store it: the key field in Settings.
Before the SPA saves that field it asks ``POST /api/providers/validate``, and
the shipped handler sends the field's whole contents to the provider as one
key. A comma-joined paste comes back as one invalid key and is never written,
which is how the owner of fifteen keys ends up looking at "API inválida".

``tests/test_field.py`` proves the wrapper's rules against a stand-in handler.
This harness proves them against the real one: the real gateway app, the real
route table, the installed plugin registered by Hermes' own plugin manager,
and a real request through the whole middleware stack. Then the value that
survives the check is saved through the real ``PUT /api/env`` and handed to
the real ``load_pool``, so the claim being made — *paste them all, comma
separated, and rotation has something to rotate* — is watched end to end.

What this is not: the provider. ``_CREDENTIAL_PROBES`` is pointed at a stub
on this process's own socket, so the handler runs its real httpx call against
a server that answers like Google's models endpoint and no quota is spent.
The keys are obvious fakes and ``HERMES_HOME`` is a throwaway directory, so
the real profile is neither read nor written.

    "$HERMES_HOME/hermes-agent/venv/Scripts/python.exe" tools/live_field.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERMES_ROOT = Path(os.environ.get("KAME_HERMES_ROOT", Path.home() / "AppData/Local/hermes"))
AGENT = HERMES_ROOT / "hermes-agent"
INSTALLED_PLUGIN = HERMES_ROOT / "plugins" / "hermes-kame-api-rotation"

PROVIDER = "gemini"
PROBED_VAR = "GEMINI_API_KEY"     # the variable that has a probe configured
UNPROBED_VAR = "GOOGLE_API_KEY"   # the one that does not, and saves anything

# Fakes of the right length: the split refuses tokens under 16 characters, so
# a shorter test key would be dropped for the right reason and make this
# harness say the wrong thing.
GOOD = [f"AIzaSyFAKE-live-good-key-0000000{n:04d}" for n in (1, 2)]
DEAD = [f"AIzaSyFAKE-live-dead-key-0000000{n:04d}" for n in (1, 2)]
MANY_GOOD = [f"AIzaSyFAKE-live-many-key-0000000{n:04d}" for n in range(1, 14)]

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
    """Google's models endpoint, reduced to the one thing it decides."""

    accepted: set = set()
    seen: list = []

    def do_GET(self):  # noqa: N802 - name fixed by the base class
        key = (parse_qs(urlparse(self.path).query).get("key") or [""])[0]
        type(self).seen.append(key)
        if key in type(self).accepted:
            payload = json.dumps({"models": []}).encode("utf-8")
            self.send_response(200)
        else:
            payload = json.dumps({"error": {"code": 400}}).encode("utf-8")
            # 401 is how the handler recognises a rejected key. A real Google
            # 400 lands on the generic branch, and both are refusals; 401 is
            # the one whose message the user is shown.
            self.send_response(401)
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
    return server, f"http://127.0.0.1:{server.server_port}/v1beta/models"


def main() -> int:
    if not AGENT.is_dir():
        print(f"Hermes not found at {AGENT}")
        return 2
    if not INSTALLED_PLUGIN.is_dir():
        print(f"the installed plugin is not at {INSTALLED_PLUGIN}")
        return 2

    home = Path(tempfile.mkdtemp(prefix="kame-field-"))
    shutil.copytree(
        INSTALLED_PLUGIN,
        home / "plugins" / "hermes-kame-api-rotation",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - hermes-kame-api-rotation\n", encoding="utf-8"
    )
    os.environ["HERMES_HOME"] = str(home)
    os.environ.pop(PROBED_VAR, None)
    os.environ.pop(UNPROBED_VAR, None)
    sys.path.insert(0, str(AGENT))
    print(f"throwaway home: {home}")

    server, probe_url = serve()
    try:
        from fastapi.testclient import TestClient

        # Imported before the plugin is discovered, which is the real order:
        # the gateway builds its route table at import and the plugin looks
        # for that module rather than importing one of its own.
        from hermes_cli import web_server

        web_server._CREDENTIAL_PROBES[PROBED_VAR] = (probe_url, "query")
        _Handler.accepted = set(GOOD) | set(MANY_GOOD)

        from hermes_cli import plugins as plugin_system

        print("\n[1] the installed plugin wraps the real validate route")
        plugin_system.discover_plugins(force=True)
        manager = plugin_system.get_plugin_manager()
        loaded = [key for key in manager._plugins if "kame" in key]
        check("one kame plugin", len(loaded), 1)
        if not loaded:
            return 1
        plugin_module = manager._plugins[loaded[0]].module
        field = getattr(plugin_module, "_field_binding", None)
        check_true("the field binding is live", field is not None and field.installed)
        if field is None or not field.installed:
            print(f"        reason: {getattr(field, 'reason', 'not installed at all')}")
            return 1
        route = next(
            r for r in web_server.app.routes
            if getattr(r, "path", "") == "/api/providers/validate"
        )
        check_true(
            "on the route a request actually reaches",
            getattr(route.dependant.call, "__kame_wrapped__", False),
        )

        client = TestClient(web_server.app)
        headers = {"X-Hermes-Session-Token": web_server._SESSION_TOKEN}

        def validate(value: str, key: str = PROBED_VAR) -> dict:
            _Handler.seen = []
            response = client.post(
                "/api/providers/validate",
                json={"key": key, "value": value},
                headers=headers,
            )
            check(f"HTTP 200 for {key}", response.status_code, 200)
            return response.json()

        print("\n[2] one key is answered exactly as before")
        answer = validate(GOOD[0])
        check("a working key passes", (answer["ok"], answer["reachable"]), (True, True))
        check("and was sent once, as itself", _Handler.seen, [GOOD[0]])
        answer = validate(DEAD[0])
        check("a dead key is still refused", (answer["ok"], answer["reachable"]), (False, True))

        print("\n[3] several keys are probed as several keys")
        blob = ",".join([DEAD[0], GOOD[0], DEAD[1]])
        answer = validate(blob)
        check("the paste is accepted", answer["ok"], True)
        check("and says what it found", answer["message"], "1 of 3 keys accepted, 2 rejected.")
        check("every key was probed on its own", sorted(_Handler.seen), sorted([DEAD[0], GOOD[0], DEAD[1]]))
        check("the whole field was never sent", [k for k in _Handler.seen if "," in k], [])

        print("\n[4] a paste where nothing works is still refused")
        answer = validate(",".join(DEAD))
        check("refused", (answer["ok"], answer["reachable"]), (False, True))
        check_true("and says so in words", "None of all 2 keys" in answer["message"])

        print("\n[5] the shape the owner actually pastes: fifteen at once")
        many = MANY_GOOD + DEAD
        answer = validate(",".join(many))
        check("accepted", answer["ok"], True)
        check("all fifteen probed", len(set(_Handler.seen)), len(many))
        check(
            "with the two revoked ones named as a count",
            answer["message"],
            f"{len(MANY_GOOD)} of {len(many)} keys accepted, {len(DEAD)} rejected.",
        )

        print("\n[6] without KAME, the same paste is one invalid key")
        # Every check above would also pass if the host had been splitting the
        # field all along. Taking the wrapper off puts the gateway back exactly
        # as it ships, on the same route, so the difference is visible.
        field.uninstall()
        try:
            answer = validate(blob)
            check("stock Hermes refuses it", (answer["ok"], answer["reachable"]), (False, True))
            check("having sent the commas to the provider", len(_Handler.seen), 1)
            check_true("as one key", "," in (_Handler.seen or [""])[0])
        finally:
            field.install(web_server.app)
        check_true(
            "the wrapper is back",
            getattr(route.dependant.call, "__kame_wrapped__", False),
        )

        print("\n[7] the variable with no probe still answers the way it did")
        answer = validate(",".join(GOOD), key=UNPROBED_VAR)
        check(
            "unchecked, and not blocked",
            (answer["ok"], answer["reachable"]),
            (True, False),
        )
        check("nothing was asked of the provider", _Handler.seen, [])

        print("\n[8] and the value the check passed is the value that is saved")
        typed = ",".join(MANY_GOOD + DEAD)
        saved = client.put(
            "/api/env", json={"key": UNPROBED_VAR, "value": typed}, headers=headers
        )
        check("the save is accepted", saved.status_code, 200)
        from hermes_cli.config import load_env

        on_disk = load_env().get(UNPROBED_VAR, "")
        check("byte for byte, commas included", on_disk, typed)

        # And the pool the whole plugin exists for reads it as the keys it is.
        os.environ[UNPROBED_VAR] = on_disk
        os.environ.pop(PROBED_VAR, None)
        from agent import credential_pool as cp

        pool = cp.load_pool(PROVIDER)
        offered, _pending = pool._available_entries()
        check("one field, fifteen credentials", len(offered), len(MANY_GOOD) + len(DEAD))
        check(
            "none of them holding the list",
            [e for e in offered if "," in str(e.runtime_api_key)],
            [],
        )

        print("\n[9] and the switch gives the host's own check back")
        field.uninstall()
        os.environ["KAME_FIELD_PROBE_DISABLED"] = "1"
        try:
            from importlib import import_module

            settings = import_module(f"{plugin_module.__name__}.settings")
            binding = import_module(f"{plugin_module.__name__}.field_binding")
            check("the switch reads as on", settings.is_on(settings.FIELD_PROBE_DISABLED), True)
            check("so nothing installs", binding.install(web_server.app), None)
            answer = validate(blob)
            check("and the paste is refused again", answer["ok"], False)
        finally:
            os.environ.pop("KAME_FIELD_PROBE_DISABLED", None)
            field.install(web_server.app)

        print()
        if failures:
            print(f"{len(failures)} FAILED: {', '.join(failures)}")
            return 1
        print("the provider field takes the whole list:")
        print("        the real route probes the keys instead of the field,")
        print("        the save stores what was typed, and the pool reads it")
        print("        back as the fifteen credentials it is.")
        return 0
    finally:
        server.shutdown()
        os.environ.pop(UNPROBED_VAR, None)
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
