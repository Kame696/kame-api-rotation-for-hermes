"""Run the binding against the REAL installed Hermes, in a throwaway home.

The unit tests state the rules against a stand-in pool. This states that the
rules meet the actual thing: the real ``CredentialPool``, the real
``PooledCredential``, the real availability logic with its OAuth syncs, DEAD
pruning and cooldown clearing. A stand-in that drifts from the host is worse
than no test, and this is what catches that drift.

**Touches nothing real.** ``HERMES_HOME`` is redirected to a temporary
directory before Hermes is imported, so every pool file, every persist, and
every plugin-state write lands there and is deleted on the way out. No
credential is read, none is written, and the user's install is not modified.
The keys below are obvious fakes.

Run it directly:

    python tools/sandbox_binding.py
"""

from __future__ import annotations

import codecs
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
HERMES = Path(os.environ.get("KAME_HERMES_ROOT", Path.home() / "AppData/Local/hermes/hermes-agent"))

NOW = time.time()
HOUR = 3600.0
MAIN = "gemini-3.6-flash"
AUX = "gemini-3.5-flash-lite"

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got  {got}")
        print(f"        want {want}")
        failures.append(label)


def load_plugin_package():
    spec = importlib.util.spec_from_file_location(
        "kame_sandbox",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MemoryState:
    """``ctx.state`` without the disk — the store's own file handling is
    covered by its unit tests; what is under test here is the pool."""

    def __init__(self) -> None:
        self.data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


def main() -> int:
    if not HERMES.is_dir():
        print(f"Hermes not found at {HERMES}; set KAME_HERMES_ROOT")
        return 2

    home = Path(tempfile.mkdtemp(prefix="kame-sandbox-"))
    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(HERMES))
    print(f"sandbox home: {home}")

    try:
        from agent import credential_pool as cp

        load_plugin_package()
        runtime = importlib.import_module("kame_sandbox.runtime")
        pool_binding = importlib.import_module("kame_sandbox.pool_binding")
        store_module = importlib.import_module("kame_sandbox.store")

        print("\n[1] the shape check accepts the installed Hermes")
        try:
            pool_binding.inspect_module(cp)
            print("  PASS  inspect_module accepted agent.credential_pool")
        except pool_binding.Incompatible as exc:
            print(f"  FAIL  inspect_module refused: {exc}")
            failures.append("inspect_module")
            return 1

        state = MemoryState()
        binding = pool_binding.PoolBinding(
            store_module.LedgerStore(state, ttl_seconds=0.0),
            journal=store_module.JournalStore(state, ttl_seconds=0.0),
        )
        check("install", binding.install(cp), True)
        check("watching selection", binding.watching_selection, True)

        entries = [
            cp.PooledCredential(
                provider="gemini",
                id=uuid.uuid4().hex[:6],
                label=f"key-{i}",
                auth_type=cp.AUTH_TYPE_API_KEY,
                priority=i,
                source="manual",
                access_token=f"AIza-sandbox-fake-{i}",
            )
            for i in range(3)
        ]
        pool = cp.CredentialPool("gemini", entries)
        labels = {e.id: e.label for e in entries}

        def usable(**kwargs):
            got, _pending = pool._available_entries(**kwargs)
            return sorted(labels[e.id] for e in got)

        print("\n[2] baseline: three healthy keys")
        check("all available", usable(), ["key-0", "key-1", "key-2"])

        print("\n[3] a daily cap on the main model, sized correctly at 24h")
        runtime.note_call("gemini", MAIN)
        pool._mark_exhausted(
            entries[0], 429, {"reason": "rate_limit", "reset_at": NOW + 24 * HOUR}
        )
        check("main model loses the spent key", usable(), ["key-1", "key-2"])

        print("\n[4] the regression: the auxiliary model never spent anything")
        runtime.note_call("gemini", AUX)
        check("auxiliary keeps all three", usable(), ["key-0", "key-1", "key-2"])

        print("\n[5] a selection on the auxiliary model must not erase the bench")
        pool._select_unlocked(refresh=False)
        stored = next(e for e in pool.entries() if e.id == entries[0].id)
        check("still benched", stored.last_status, cp.STATUS_EXHAUSTED)
        check("deadline intact", stored.last_error_reset_at, NOW + 24 * HOUR)

        print("\n[6] back on the main model, the key is still spent")
        runtime.note_call("gemini", MAIN)
        check("main model still without it", usable(), ["key-1", "key-2"])

        print("\n[7] a bench KAME did not write is never released")
        from dataclasses import replace

        foreign = replace(
            next(e for e in pool.entries() if e.id == entries[1].id),
            last_status=cp.STATUS_EXHAUSTED,
            last_status_at=NOW,
            last_error_code=429,
            last_error_reset_at=NOW + 9 * HOUR,
        )
        pool._replace_entry(next(e for e in pool.entries() if e.id == entries[1].id), foreign)
        runtime.note_call("gemini", AUX)
        check("foreign bench held", usable(), ["key-0", "key-2"])

        print("\n[8] an unannounced call gets the host's own answer")
        runtime.forget_call()
        check("host behaviour", usable(), ["key-2"])

        print("\n[9] a freshly loaded pool object sees the same memory")
        # The auxiliary path calls load_pool() and gets a new object every
        # time; a binding that only reached the long-lived pool would leave
        # that path with the regression it was built to fix.
        fresh = cp.CredentialPool("gemini", [e for e in pool.entries()])
        runtime.note_call("gemini", AUX)
        got, _ = fresh._available_entries()
        check("fresh pool, same answer", sorted(labels[e.id] for e in got), ["key-0", "key-2"])

        print("\n[10] the auxiliary lane announces its own model")
        # auxiliary_client fires no hooks, so without this wrapper every
        # summarisation and titling call looks unannounced and inherits the
        # main model's bench — the lane that spent nothing losing the key.
        from agent import auxiliary_client as aux

        aux_binding = importlib.import_module("kame_sandbox.aux_binding")
        try:
            aux_binding.inspect_module(aux)
            print("  PASS  inspect_module accepted agent.auxiliary_client")
        except aux_binding.Incompatible as exc:
            print(f"  FAIL  inspect_module refused: {exc}")
            failures.append("aux inspect_module")

        aux_bound = aux_binding.AuxBinding()
        check("aux install", aux_bound.install(aux), True)

        # The real relay accepts a ``create`` callback and invokes it in place
        # of the provider SDK. That runs the genuine relay body — routing,
        # metadata, protection wrapper — with no network and no credentials,
        # and reports what the announcement looked like from inside it.
        seen: list = []

        def _observe(_request):
            seen.append(runtime.model_for("gemini"))
            return None

        runtime.note_call("gemini", MAIN)
        aux._relay_sync_completion(
            None, {"model": AUX}, provider="gemini", create=_observe
        )
        check("aux call sees its own model", seen, [AUX])
        check("main announcement restored", runtime.model_for("gemini"), MAIN)

        print("\n[10b] an auxiliary refusal is read, and benched on its own model")
        # The lane that fires no hooks. Its refusals reached `core.classify`
        # nowhere, and `_recover_provider_pool` benches *after* the relay has
        # unwound — so the cooldown earned by a titling call on a small model
        # was written against the conversation's model, with the host's fixed
        # TTL, on a key the small model had barely touched.
        from agent.gemini_native_adapter import GeminiAPIError as _AuxError

        aux_pool = cp.CredentialPool(
            "gemini",
            [
                cp.PooledCredential(
                    provider="gemini",
                    id=uuid.uuid4().hex[:6],
                    label="aux-key",
                    auth_type=cp.AUTH_TYPE_API_KEY,
                    priority=0,
                    source="manual",
                    access_token="AIza-sandbox-fake-aux",
                )
            ],
        )
        aux_entry = aux_pool.entries()[0]
        runtime.forget_judgement()
        runtime.forget_bench_model()
        runtime.note_call("gemini", MAIN)

        def _refuse(_request):
            raise _AuxError(
                "Gemini HTTP 429 (RESOURCE_EXHAUSTED): quota exceeded",
                code="gemini_rate_limited",
                status_code=429,
                details={
                    "status": "RESOURCE_EXHAUSTED",
                    "reason": "RATE_LIMIT_EXCEEDED",
                    "metadata": {
                        "quota_limit":
                            "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                    },
                    "message": "quota exceeded",
                },
            )

        try:
            aux._relay_sync_completion(
                None, {"model": AUX}, provider="gemini", create=_refuse
            )
            check("the refusal still reaches the caller", "swallowed", "raised")
        except Exception:
            check("the refusal still reaches the caller", "raised", "raised")

        # `scoped_call` has unwound; the conversation's model is announced
        # again, exactly as the host left it.
        check("the announcement is back on the main model", runtime.model_for("gemini"), MAIN)

        # This is what `_recover_provider_pool` does next, in the same stack.
        aux_pool.mark_exhausted_and_rotate(status_code=429, error_context={})
        benched = next(e for e in aux_pool.entries() if e.id == aux_entry.id)
        check("the key was benched", benched.last_status, cp.STATUS_EXHAUSTED)
        check(
            "and with a deadline, where the host had none",
            benched.last_error_reset_at is not None,
            True,
        )
        aux_row = binding._journal.load(force=True).last_block(aux_entry.id, AUX)
        check("recorded against the auxiliary model", aux_row is not None, True)
        if aux_row is not None:
            check("sized by KAME", aux_row.sized_by, "kame")
            check("as a per-minute counter", aux_row.window, "per_minute")
        check(
            "and never against the conversation's model",
            binding._journal.load(force=True).last_block(aux_entry.id, MAIN),
            None,
        )
        runtime.forget_bench_model()
        runtime.forget_judgement()

        print("\n[10c] and that note stops being true at the next main-lane refusal")
        # Found by ``tools/live_429.py`` against the installed build. The note
        # above is how an auxiliary bench finds its model, and it outlives the
        # call that wrote it by design — the host benches after the relay has
        # unwound. What it must not outlive is the *next* refusal: a titling
        # call that fails seconds before the conversation's own 429 would
        # otherwise make KAME file the conversation's bench under the
        # auxiliary model, and per-model release would then hand the key
        # straight back to the model that had just spent it.
        #
        # ``agent/auxiliary_client.py`` never calls ``classify_api_error``, so
        # reaching the classification hook is proof the refusal is the main
        # lane's.
        kame = importlib.import_module("kame_sandbox")
        leak_pool = cp.CredentialPool(
            "gemini",
            [
                cp.PooledCredential(
                    provider="gemini",
                    id=uuid.uuid4().hex[:6],
                    label="leak-key",
                    auth_type=cp.AUTH_TYPE_API_KEY,
                    priority=0,
                    source="manual",
                    access_token="AIza-sandbox-fake-leak",
                )
            ],
        )
        leak_entry = leak_pool.entries()[0]
        runtime.note_call("gemini", MAIN)
        runtime.note_bench_model("gemini", AUX, now=binding._clock())
        check(
            "the auxiliary note is standing",
            runtime.bench_model_for("gemini", now=binding._clock()),
            AUX,
        )

        main_error = _AuxError(
            "Gemini HTTP 429 (RESOURCE_EXHAUSTED): quota exceeded",
            code="gemini_rate_limited",
            status_code=429,
            details={
                "status": "RESOURCE_EXHAUSTED",
                "reason": "RATE_LIMIT_EXCEEDED",
                "metadata": {
                    "quota_limit":
                        "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                },
                "message": "quota exceeded",
            },
        )
        sized = kame._on_api_error_classification(
            provider="gemini",
            model=MAIN,
            status_code=429,
            error_message=str(main_error),
            error_body=None,
            error=main_error,
            error_type="GeminiAPIError",
            error_code="gemini_rate_limited",
        )
        check("the main lane's refusal was sized", sized is not None, True)
        check(
            "and the auxiliary note is gone",
            runtime.bench_model_for("gemini", now=binding._clock()),
            "",
        )

        leak_pool.mark_exhausted_and_rotate(status_code=429, error_context={})
        book = binding._journal.load(force=True)
        leak_row = book.last_block(leak_entry.id, MAIN)
        check(
            "so the bench is filed under the conversation's model",
            leak_row is not None,
            True,
        )
        check(
            "and not under the auxiliary one",
            book.last_block(leak_entry.id, AUX),
            None,
        )
        # 1.6.0.0, gap 12. The refusal above named
        # ``GenerateRequestsPerMinutePerProjectPerModel-FreeTier`` in its
        # metadata, so the provider's own counter said per-minute. The row has
        # to carry that beside KAME's conclusion, or the journal holds a
        # verdict with nothing able to contradict it — which is what it held
        # for every one of the 295 blocks that produced this release.
        if leak_row is not None:
            check("the provider's own counter reached the row",
                  leak_row.stated_window, "per_minute")
            check("beside the window KAME acted on", leak_row.window, "per_minute")
            check("which agree here, so no contradiction is claimed",
                  leak_row.contradicted, False)
        runtime.forget_bench_model()
        runtime.forget_judgement()
        aux_bound.uninstall()

        print("\n[11] the journal recorded the real refusals")
        # Sections 3 and 7 both benched a key. Only the first went through
        # ``_mark_exhausted``; the second was written straight onto the entry
        # to stand in for another writer, and must not appear here.
        book = binding._journal.load(force=True)
        rows = [r for r in book.blocks() if r.credential_id in {e.id for e in entries}]
        check("one block recorded", len(rows), 1)
        if rows:
            check("against the model that spent it", rows[0].model, MAIN)
            check("with the deadline the pool stored", rows[0].reset_at, NOW + 24 * HOUR)
            check("and the failing key named", rows[0].credential_id, entries[0].id)

        print("\n[12] a real selection names the key for the success side")
        runtime.forget_selections()
        runtime.note_call("gemini", AUX)
        chosen, _pending = pool._select_unlocked(refresh=False)
        check(
            "selection mirrored",
            runtime.selected_for("gemini"),
            getattr(chosen, "id", ""),
        )

        print("\n[13] a success closes the question, and only the first one")
        runtime.note_selection("gemini", entries[0].id)
        binding.note_success("gemini", MAIN)
        recovery = binding._journal.load(force=True).recovery_for(entries[0].id, MAIN)
        check("recovery recorded", recovery is not None, True)
        if recovery is not None:
            # The bench was for 24h and the key answered within seconds of
            # being benched, so the prediction was long — which is exactly the
            # mistake nothing else in the system can see.
            check("marked early", recovery.was_early, True)
        binding.note_success("gemini", MAIN)
        check(
            "second success adds nothing",
            len(binding._journal.load(force=True).recoveries()),
            1,
        )

        print("\n[14] the report reads back without touching a token")
        report = importlib.import_module("kame_sandbox.core.report")
        text = report.render(
            binding._store.load(force=True),
            binding._journal.load(force=True),
            now=NOW,
            labels=labels,
        )
        check("names the model", MAIN in text, True)
        check("names the key by label", "key-0" in text, True)
        check("leaks no token", "AIza-sandbox-fake" in text, False)

        print("\n[15] a total lockout is tested, not simply obeyed")
        # The failure this guards against is the expensive one: KAME reads a
        # daily cap correctly, benches the last key for 24 hours, and the
        # host's own sole-credential shortening does not apply because a
        # provider-supplied reset_at overrides it. Without the escape hatch
        # that is a day with no agent, and no way to ever discover the
        # deadline was wrong.
        lockout_home = cp.CredentialPool(
            "gemini",
            [
                cp.PooledCredential(
                    provider="gemini",
                    id=uuid.uuid4().hex[:6],
                    label="last-key",
                    auth_type=cp.AUTH_TYPE_API_KEY,
                    priority=0,
                    source="manual",
                    access_token="AIza-sandbox-fake-last",
                )
            ],
        )
        only = lockout_home.entries()[0]
        probe = importlib.import_module("kame_sandbox.core.probe")

        clock = {"now": NOW}
        binding._clock = lambda: clock["now"]

        runtime.note_call("gemini", MAIN)
        lockout_home._mark_exhausted(
            only, 429, {"reason": "rate_limit", "reset_at": NOW + 24 * HOUR}
        )

        def usable_now():
            got, _pending = lockout_home._available_entries()
            return [e.label for e in got]

        check("locked out at first", usable_now(), [])
        clock["now"] = NOW + probe.FIRST_PROBE_SECONDS
        check("the last key is tried again", usable_now(), ["last-key"])
        bench = binding._store.load(force=True).find(only.id, MAIN)
        check("the attempt was counted", bench.probes, 1)
        clock["now"] = NOW + probe.FIRST_PROBE_SECONDS + probe.PROBE_WINDOW_SECONDS
        check("and the lockout resumes", usable_now(), [])
        stored = next(e for e in lockout_home.entries() if e.id == only.id)
        check("the pool was never written to", stored.last_status, cp.STATUS_EXHAUSTED)
        check("deadline untouched", stored.last_error_reset_at, NOW + 24 * HOUR)
        print("\n[16] a limit the provider says covers the key is held everywhere")
        # The other half of the same coin as [15]. Per-model release is right
        # for Google's free tier and wrong for an account-wide ceiling; the
        # provider says which, and the pool has no field for either. Run
        # against the real CredentialPool so the hold is proved on the host's
        # own answer, not on a stand-in.
        wide = cp.CredentialPool(
            "gemini",
            [
                cp.PooledCredential(
                    provider="gemini",
                    id=uuid.uuid4().hex[:6],
                    label="shared-key",
                    auth_type=cp.AUTH_TYPE_API_KEY,
                    priority=0,
                    source="manual",
                    access_token="AIza-sandbox-fake-wide",
                )
            ],
        )
        shared = wide.entries()[0]
        clock["now"] = NOW

        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini", MAIN, window="per_day", source="body",
            reset_at=NOW + 12 * HOUR, now=NOW, scope="account",
        )
        wide._mark_exhausted(
            shared, 429, {"reason": "rate_limit", "reset_at": NOW + 12 * HOUR}
        )

        def usable_for(model):
            runtime.note_call("gemini", model)
            got, _pending = wide._available_entries()
            return [e.label for e in got]

        stored_bench = binding._store.load(force=True).find(shared.id, MAIN)
        check("the scope was recorded", stored_bench.covers_every_model, True)
        check("held for the model that spent it", usable_for(MAIN), [])
        check("held for the auxiliary model too", usable_for(AUX), [])
        clock["now"] = NOW + probe.FIRST_PROBE_SECONDS
        check("and still tested, not obeyed forever", usable_for(AUX), ["shared-key"])
        check(
            "counted against the bench that blocks",
            binding._store.load(force=True).find(shared.id, MAIN).probes,
            1,
        )
        check(
            "no bench invented for the model in flight",
            binding._store.load(force=True).find(shared.id, AUX),
            None,
        )
        stored = next(e for e in wide.entries() if e.id == shared.id)
        check("the pool was never written to", stored.last_error_reset_at, NOW + 12 * HOUR)
        binding._clock = lambda: NOW

        print("\n[17] a key that answers is believed, against the real pool")
        # The hole v0.0.7 left open: the escape hatch asked the question and
        # threw the answer away, so a key that worked was withheld again on
        # the very next selection. Run against the real CredentialPool because
        # the release has to survive the host's own availability rules, with
        # the host still holding a twelve-hour cooldown on the entry.
        tested = cp.CredentialPool(
            "gemini",
            [
                cp.PooledCredential(
                    provider="gemini",
                    id=uuid.uuid4().hex[:6],
                    label="proved-key",
                    auth_type=cp.AUTH_TYPE_API_KEY,
                    priority=0,
                    source="manual",
                    access_token="AIza-sandbox-fake-proved",
                )
            ],
        )
        proved = tested.entries()[0]
        clock["now"] = NOW
        binding._clock = lambda: clock["now"]

        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini", MAIN, window="per_day", source="body",
            reset_at=NOW + 12 * HOUR, now=NOW, scope="per_model",
        )
        tested._mark_exhausted(
            proved, 429, {"reason": "rate_limit", "reset_at": NOW + 12 * HOUR}
        )

        def usable_proved():
            runtime.note_call("gemini", MAIN)
            got, _pending = tested._available_entries()
            return [e.label for e in got]

        check("locked out at first", usable_proved(), [])
        clock["now"] = NOW + probe.FIRST_PROBE_SECONDS
        check("the deadline gets tested", usable_proved(), ["proved-key"])

        # The probe came back clean.
        binding.note_success("gemini", MAIN)
        refuted = binding._store.load(force=True).find(proved.id, MAIN)
        check("the bench is marked, not deleted", refuted.is_refuted, True)
        check("its deadline is kept as the proof of ownership", refuted.reset_at, NOW + 12 * HOUR)

        clock["now"] = NOW + probe.FIRST_PROBE_SECONDS + probe.PROBE_WINDOW_SECONDS + 1
        check("the key stays in rotation", usable_proved(), ["proved-key"])
        clock["now"] = NOW + 6 * HOUR
        check("hours later, still in rotation", usable_proved(), ["proved-key"])
        stored = next(e for e in tested.entries() if e.id == proved.id)
        check("the pool was never written to", stored.last_status, cp.STATUS_EXHAUSTED)
        check("deadline untouched", stored.last_error_reset_at, NOW + 12 * HOUR)
        binding._clock = lambda: NOW

        print("\n[18] a deadline measured short twice is widened, against the real pool")
        # The one number KAME learns instead of reading. The sequence below is
        # the only thing the journal can read as "too short" without knowing
        # anything about the provider: held until X, handed back, refused again
        # within minutes — twice over.
        #
        # It is staged in the past on purpose. What has to be proved is the
        # awkward moment the unit tests can only simulate: the *host's* own
        # cooldown has already lapsed and the real pool considers the key
        # usable again, while KAME is still holding it on a deadline of its
        # own. Nothing here moves the host's stored number.
        #
        # Two keys, and only one of them spent. A widened bench on the *only*
        # key for a model is not a lockout — the escape hatch tests it, which
        # is [15]'s subject and is proved again below — so a pool of one would
        # measure that instead of the hold.
        widened = cp.CredentialPool(
            "gemini",
            [
                cp.PooledCredential(
                    provider="gemini",
                    id=uuid.uuid4().hex[:6],
                    label=label,
                    auth_type=cp.AUTH_TYPE_API_KEY,
                    priority=index,
                    source="manual",
                    access_token=f"AIza-sandbox-fake-{label}",
                )
                for index, label in enumerate(("short-key", "spare-key"))
            ],
        )
        short_key = widened.entries()[0]
        binding._clock = lambda: clock["now"]

        def refuse_at(moment: float, seconds: float = 600.0) -> None:
            clock["now"] = moment
            runtime.note_call("gemini", MAIN)
            runtime.note_judgement(
                "gemini", MAIN, window="per_hour", source="headers",
                reset_at=moment + seconds, now=moment, scope="per_model",
            )
            widened._mark_exhausted(
                widened.entries()[0], 429,
                {"reason": "rate_limit", "reset_at": moment + seconds},
            )

        refuse_at(NOW - 2000.0)                     # held until NOW-1400
        refuse_at(NOW - 1399.0)                     # refused a second later — strike 1
        refuse_at(NOW - 798.0)                      # and again — strike 2, so it widens

        clock["now"] = NOW
        bench = binding._store.load(force=True).find(short_key.id, MAIN)
        check("the bench is extended", bench.is_extended, True)
        check("the host's deadline is the fingerprint", bench.reset_at, NOW - 198.0)
        check("KAME holds it twice as long", bench.until, NOW + 402.0)
        stored = widened.entries()[0]
        check("the pool was never written to", stored.last_error_reset_at, NOW - 198.0)

        def usable_widened(model: str):
            runtime.note_call("gemini", model)
            got, _pending = widened._available_entries()
            return [e.label for e in got]

        check("the host would hand it back now", stored.last_error_reset_at < NOW, True)
        check(
            "KAME keeps holding it for the model that spent it",
            usable_widened(MAIN), ["spare-key"],
        )
        check(
            "every other model still has it",
            usable_widened(AUX), ["short-key", "spare-key"],
        )

        clock["now"] = NOW + 403.0
        check(
            "and it returns when the longer deadline lapses",
            usable_widened(MAIN), ["short-key", "spare-key"],
        )
        binding._clock = lambda: NOW

        print("\n[18b] the deadline reaches the host when the host derived none")
        # The measurement that made 1.6.0.0 necessary: 295 benches over 13.9
        # days, `last_error_reset_at` unset on every one, `sized_by: kame` a
        # standing zero. Every check above hands `_mark_exhausted` a context
        # that already contains a deadline — which is the one thing the real
        # caller never does.
        #
        # `agent/conversation_loop.py:4280` throws away
        # `ClassifiedError.error_context` and passes
        # `extract_api_error_context(api_error)` instead. So this section
        # calls the host's own extractor on the host's own Gemini exception
        # and feeds the pool exactly what it produces — nothing.
        from agent.agent_runtime_helpers import extract_api_error_context
        from agent.gemini_native_adapter import GeminiAPIError

        carried = cp.CredentialPool(
            "gemini",
            [
                cp.PooledCredential(
                    provider="gemini",
                    id=uuid.uuid4().hex[:6],
                    label="carried-key",
                    auth_type=cp.AUTH_TYPE_API_KEY,
                    priority=0,
                    source="manual",
                    access_token="AIza-sandbox-fake-carried",
                )
            ],
        )
        carried_entry = carried.entries()[0]
        clock["now"] = NOW
        binding._clock = lambda: clock["now"]

        refusal = GeminiAPIError(
            "Gemini HTTP 429 (RESOURCE_EXHAUSTED): You exceeded your current "
            "quota, please check your plan and billing details.",
            code="gemini_rate_limited",
            status_code=429,
            details={
                "status": "RESOURCE_EXHAUSTED",
                "reason": "RATE_LIMIT_EXCEEDED",
                "metadata": {
                    "quota_limit":
                        "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                },
                "message": "You exceeded your current quota.",
            },
        )
        host_context = extract_api_error_context(refusal)
        check(
            "the host derives no deadline from a Gemini refusal",
            host_context.get("reset_at"),
            None,
        )

        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini", MAIN, window="per_minute", source="catalog",
            reset_at=NOW + 21.0, now=NOW, scope="per_model",
        )
        carried._mark_exhausted(carried_entry, 429, host_context)

        stored = next(e for e in carried.entries() if e.id == carried_entry.id)
        check("KAME's deadline reached the entry", stored.last_error_reset_at, NOW + 21.0)
        check(
            "and it is what the host's own cooldown now reads",
            cp._exhausted_until(stored),
            NOW + 21.0,
        )
        row = binding._journal.load(force=True).last_block(carried_entry.id, MAIN)
        check("the journal can finally say who sized it", getattr(row, "sized_by", None), "kame")

        # And the ledger claims it, which it can only do when the number it
        # proposed is the number on the entry — that match is the fingerprint
        # the release path checks before it ever lets a key back.
        owned = binding._store.load(force=True).find(carried_entry.id, MAIN)
        check("the bench is provably KAME's", getattr(owned, "reset_at", None), NOW + 21.0)

        def usable_carried():
            runtime.note_call("gemini", MAIN)
            got, _pending = carried._available_entries()
            return [e.label for e in got]

        check("benched while the counter is spent", usable_carried(), [])

        # And the third thing the journal could not do. A recovery inherits
        # `predicted_reset_at` from `block.reset_at`, which is the host's
        # stored deadline — `None` on all 295 recorded blocks, so **0 of 30**
        # recoveries carried the prediction they were testing and the plugin
        # could not grade itself. It is not a separate defect: it is this one,
        # measured from the other end.
        runtime.note_selection("gemini", carried_entry.id)
        binding.note_success("gemini", MAIN)
        recovered = binding._journal.load(force=True).recovery_for(carried_entry.id, MAIN)
        check("a recovery is recorded", recovered is not None, True)
        if recovered is not None:
            check(
                "carrying the prediction it tests",
                recovered.predicted_reset_at,
                NOW + 21.0,
            )

        print("\n[18c] a deadline the provider stated is never overwritten")
        # The rule that keeps [18b] from ever being a regression. A
        # `Retry-After` header is the provider's own number; KAME adds where
        # the host found nothing and never argues with what it did find.
        stated = cp.CredentialPool(
            "gemini",
            [
                cp.PooledCredential(
                    provider="gemini",
                    id=uuid.uuid4().hex[:6],
                    label="stated-key",
                    auth_type=cp.AUTH_TYPE_API_KEY,
                    priority=0,
                    source="manual",
                    access_token="AIza-sandbox-fake-stated",
                )
            ],
        )
        stated_entry = stated.entries()[0]
        clock["now"] = NOW
        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini", MAIN, window="per_minute", source="catalog",
            reset_at=NOW + 21.0, now=NOW, scope="per_model",
        )
        stated._mark_exhausted(
            stated_entry, 429, {"reason": "rate_limit", "reset_at": NOW + 90.0}
        )
        stored = next(e for e in stated.entries() if e.id == stated_entry.id)
        check("the provider's number stands", stored.last_error_reset_at, NOW + 90.0)
        dropped_row = binding._journal.load(force=True).last_block(stated_entry.id, MAIN)
        check(
            "and the journal says KAME's was dropped",
            getattr(dropped_row, "sized_by", None),
            "dropped",
        )

        print("\n[18d] a verdict from another call cannot attach itself")
        # `peek_judgement` matches on provider and on the model in flight and
        # expires after 30 seconds. Without that, a verdict left over from a
        # failure that never became a bench would size the next key's
        # cooldown — a wrong deadline is worse than none, because the ledger
        # would then claim it.
        drifting = cp.CredentialPool(
            "gemini",
            [
                cp.PooledCredential(
                    provider="gemini",
                    id=uuid.uuid4().hex[:6],
                    label="drifting-key",
                    auth_type=cp.AUTH_TYPE_API_KEY,
                    priority=0,
                    source="manual",
                    access_token="AIza-sandbox-fake-drifting",
                )
            ],
        )
        drifting_entry = drifting.entries()[0]
        clock["now"] = NOW
        runtime.forget_judgement()
        runtime.note_call("gemini", MAIN)
        runtime.note_judgement(
            "gemini", AUX, window="per_minute", source="catalog",
            reset_at=NOW + 21.0, now=NOW, scope="per_model",
        )
        drifting._mark_exhausted(drifting_entry, 429, {})
        stored = next(e for e in drifting.entries() if e.id == drifting_entry.id)
        check(
            "a verdict for another model is not borrowed",
            stored.last_error_reset_at,
            None,
        )
        runtime.forget_judgement()
        binding._clock = lambda: NOW

        print("\n[18e] the row holding several keys is never handed out as a key")
        # The owner's NVIDIA report, reproduced. `_available_entries` and
        # `_select_unlocked` have excluded the parent of a split since the
        # feature shipped; `current()` never went through either, and it is
        # what `_current_id` resolves through. That pointer is restored from
        # auth.json, where only the parent is ever written — so after a
        # restart the live credential was the comma-joined list, sent whole,
        # and refused as an invalid key.
        joined = "AIza-sandbox-fake-one" + "," + "AIza-sandbox-fake-two"
        container = cp.CredentialPool(
            "gemini",
            [
                cp.PooledCredential(
                    provider="gemini",
                    id="parent01",
                    label="env",
                    auth_type=cp.AUTH_TYPE_API_KEY,
                    priority=0,
                    source="env:GOOGLE_API_KEY",
                    access_token=joined,
                )
            ],
        )
        parts = [e for e in container.entries() if e.id != "parent01"]
        check("the row was split into its keys", len(parts), 2)
        check(
            "and the container is not offered",
            [e.id for e in container._available_entries()[0]],
            [e.id for e in parts],
        )

        # A restart, exactly as auth.json produces it.
        container._current_id = "parent01"
        live = container.current()
        check("current() no longer answers with the container", live.id != "parent01", True)
        check("what it answers with is one key, not the list", live.runtime_api_key != joined, True)
        check("and it is one of the split parts", live.id in {e.id for e in parts}, True)
        check("the pointer itself was repaired", container._current_id, live.id)

        # And the key KAME actually handed out is the one it stands in for,
        # so requests in flight are not attributed to the other part.
        runtime.forget_selections()
        chosen, _pending = container._select_unlocked(refresh=False)
        container._current_id = "parent01"
        check("it stands in for the key that was selected", container.current().id, chosen.id)

        print("\n[18f] the panel can say what KAME is looking at")
        # The other half of the same report. "The new profile doesn't detect
        # the key separation" and "this provider field really does hold one
        # key" are indistinguishable from outside the process, and neither
        # number was anywhere on screen. Read here, before the single-key
        # pool below replaces it: the record is one row per provider and says
        # what KAME last saw, which is what a live panel wants.
        shape = binding.seen_pools.get("gemini") or {}
        check("the container is counted as a container", shape.get("rows"), 1)
        check("and its keys are counted as keys", shape.get("keys"), 2)
        check("which is what 'split' means", shape.get("split"), True)
        check("named by where they came from", shape.get("origin"), "env")
        leaked = [
            value for value in shape.values()
            if isinstance(value, str) and "AIza" in value
        ]
        check("and nothing key-shaped is kept", leaked, [])

        # A pool with nothing to split is untouched.
        single = cp.CredentialPool(
            "gemini",
            [
                cp.PooledCredential(
                    provider="gemini",
                    id="solo01",
                    label="one",
                    auth_type=cp.AUTH_TYPE_API_KEY,
                    priority=0,
                    source="manual",
                    access_token="AIza-sandbox-fake-solo",
                )
            ],
        )
        single._current_id = "solo01"
        check("a row that is a key is still returned", single.current().id, "solo01")

        print("\n[19] uninstall leaves Hermes exactly as found")
        names = (
            "_mark_exhausted", "_available_entries", "_select_unlocked",
            # Wrapped since v0.1.8, and the two whose restoration matters
            # most: one decides what reaches auth.json, the other runs for
            # every pool the process builds.
            "_persist", "__init__",
        )
        before = tuple(getattr(cp.CredentialPool, name) for name in names)
        binding.uninstall()
        after = tuple(getattr(cp.CredentialPool, name) for name in names)
        check("methods restored", after != before, True)
        check(
            "no wrapper left behind",
            any(getattr(method, "__kame_wrapped__", False) for method in after),
            False,
        )
        runtime.note_call("gemini", AUX)
        got, _ = pool._available_entries()
        check("host answer only", sorted(labels[e.id] for e in got), ["key-2"])

        print("\n[20] /kame-keys writes through the real pool, in this throwaway home")
        commands = importlib.import_module("kame_sandbox.commands")
        keys_mod = importlib.import_module("kame_sandbox.core.keys")
        multikey = importlib.import_module("kame_sandbox.core.multikey")
        fakes = [
            "AIzaSy" + letter * 33
            for letter in ("Q", "R", "S")
        ]

        first = commands.handle(f"add gemini {','.join(fakes)}")
        check("three added", "Added 3 key(s)" in first, True)
        check("a backup was written first", "Backup:" in first, True)
        check(
            "no key is ever echoed",
            any(fake in first for fake in fakes),
            False,
        )

        written = cp.load_pool("gemini")
        stored = {str(getattr(e, "access_token", "") or "") for e in written.entries()}
        # Deltas, not totals: earlier sections seeded this same pool, and a
        # count that only holds when nothing ran before it is a test of the
        # ordering, not of the write.
        check("the real pool holds them", sorted(stored & set(fakes)), sorted(fakes))
        added = [e for e in written.entries() if str(getattr(e, "access_token", "")) in fakes]
        check(
            "and they are real API-key entries",
            {getattr(e, "auth_type", None) for e in added},
            {cp.AUTH_TYPE_API_KEY},
        )
        before = len(written.entries())

        again = commands.handle(f"add gemini {','.join(fakes)}")
        check("re-running adds nothing", "Nothing to add" in again, True)
        check("and says why", "3 already in pool" in again, True)
        check("the pool is unchanged", len(cp.load_pool("gemini").entries()), before)

        listing = commands.handle("")
        check(
            "the listing names every key it holds",
            all(keys_mod.redact(fake) in listing for fake in fakes),
            True,
        )
        check(
            "and never a whole key",
            any(fake in listing for fake in fakes),
            False,
        )

        # The file the user is told to point `import` at, in the encoding
        # Windows tools actually write. A mark left in place costs the first
        # key of a UTF-8 file and every key of a UTF-16 one.
        marked = home / "keys-from-notepad.txt"
        marked.write_bytes(
            codecs.BOM_UTF8 + ("AIzaSy" + "T" * 33 + "\r\n").encode("utf-8")
        )
        imported = commands.handle(f"import gemini {marked}")
        check("a marked file imports whole", "Added 1 key(s)" in imported, True)

        utf16 = home / "keys-from-powershell.txt"
        utf16.write_bytes(("AIzaSy" + "U" * 33 + "\r\n").encode("utf-16"))
        imported16 = commands.handle(f"import gemini {utf16}")
        check("so does a UTF-16 one", "Added 1 key(s)" in imported16, True)
        check("two more entries than before", len(cp.load_pool("gemini").entries()), before + 2)

        print("\n[21] a row the real pool will never pick is not called a key")
        # The state an env source that resolved to nothing leaves behind, and
        # the one the user's own Gemini pool is in. The host skips it before
        # it looks at status or cooldown:
        #   if entry.auth_type == AUTH_TYPE_API_KEY and not entry.runtime_api_key:
        #       continue
        # so its stored status describes a request it will never make. Written
        # through auth.json rather than add_entry because that is where such a
        # row comes from — nothing would ever *add* an empty credential.
        store_path = home / "auth.json"
        raw = json.loads(store_path.read_text(encoding="utf-8"))
        pooled = raw.get("credential_pool", {}).get("gemini")
        rows = pooled["entries"] if isinstance(pooled, dict) else pooled
        rows.append({
            "id": "envrow", "provider": "gemini", "label": "GOOGLE_API_KEY",
            "auth_type": "api_key", "priority": 99, "source": "env:GOOGLE_API_KEY",
            "access_token": "",
        })
        store_path.write_text(json.dumps(raw), encoding="utf-8")

        reloaded = cp.load_pool("gemini")
        empty = [e for e in reloaded.entries() if e.id == "envrow"]
        check("the row survives a real load", len(empty), 1)
        available, _ = reloaded._available_entries()
        check(
            "and the real pool refuses to pick it",
            "envrow" in {e.id for e in available},
            False,
        )

        report = commands.handle("status gemini")
        line = [ln for ln in report.splitlines() if "GOOGLE_API_KEY" in ln]
        check("status names it", len(line), 1)
        check("and does not call it ok", "[ok]" in line[0], False)
        check("it says what is wrong instead", "[no key]" in line[0], True)
        check(
            "and the count agrees with the pool",
            f"{before + 2} of {before + 3} key(s) usable" in report,
            True,
        )

        print("\n[22] a provider env var holding several keys, through the real seeder")
        # The whole point of this section is that nothing here is simulated.
        # `_seed_from_env` reads the variable, `load_pool` builds the pool,
        # `_available_entries` decides what can be picked and
        # `write_credential_pool` is what disk sees. The host does
        #   token = _get_env_prefer_dotenv(env_var)
        # and stores that whole string as one credential — there is no split
        # anywhere on that path, which is why a comma-separated list has only
        # ever been one malformed key.
        fakes = ["AIzaSandboxKey" + chr(ord("A") + n) * 25 for n in range(4)]
        os.environ["GOOGLE_API_KEY"] = ",".join(fakes)
        # Section 19 removed the binding on purpose. Splitting is a wrapper,
        # so it needs one installed — put a fresh one on for this section and
        # take it off again, which also leaves the module as section 19 found it.
        splitter = pool_binding.PoolBinding(
            store_module.LedgerStore(MemoryState(), ttl_seconds=0.0)
        )
        check("a binding that can split", splitter.install(cp), True)
        check("and knows it may", splitter.splitting_multikey, True)
        try:
            seeded = cp.load_pool("gemini")
            derived = [
                e for e in seeded.entries()
                if multikey.is_child_source(getattr(e, "source", ""))
            ]
            check("the real seeder's one row became four keys", len(derived), 4)
            check(
                "each carries exactly one of them",
                sorted(e.runtime_api_key for e in derived) == sorted(fakes),
                True,
            )
            picked, _ = seeded._available_entries()
            check(
                "and the comma-joined row is not among what it will pick",
                any("," in e.runtime_api_key for e in picked),
                False,
            )

            # The guard that matters. An `env:` source is borrowed, so the
            # host would strip the secret at the disk boundary anyway — but a
            # manual multi-key row is persistable, and a stripped row is still
            # a row. Neither is acceptable, so neither happens.
            seeded._persist()
            on_disk = store_path.read_text(encoding="utf-8")
            check("no derived key reached disk", any(f in on_disk for f in fakes), False)
            check("no derived row reached disk", multikey.CHILD_SOURCE_MARK in on_disk, False)
            check(
                "and the pool still holds them afterwards",
                len([e for e in seeded.entries()
                     if multikey.is_child_source(getattr(e, "source", ""))]),
                4,
            )

            report = commands.handle("status gemini")
            check("the report counts keys, not rows", "[list]" in report, True)
            check("and leaks none of them", any(f in report for f in fakes), False)

            # Belt and braces, and worth stating because it is the host's
            # promise rather than this plugin's: a derived source is not in
            # `_PERSISTABLE_PROVIDER_SOURCES`, so `is_borrowed_credential_source`
            # calls it borrowed and `sanitize_borrowed_credential_payload`
            # strips its secret at the disk boundary. That holds for a part cut
            # from a `manual` row too — `manual#kame-key-1` is neither `manual`
            # nor `manual:`-prefixed. So even with the wrapper removed and the
            # parts still in a live pool, the key cannot be written.
            splitter.uninstall()
            seeded._persist()
            after = store_path.read_text(encoding="utf-8")
            check(
                "with the plugin gone the host still refuses the key",
                any(f in after for f in fakes),
                False,
            )
        finally:
            splitter.uninstall()
            os.environ.pop("GOOGLE_API_KEY", None)

    finally:
        shutil.rmtree(home, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all sandbox checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
