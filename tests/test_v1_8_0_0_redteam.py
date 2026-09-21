"""Red-first tests for the release-blocking findings in
``research/1.8.0.0/RED_TEAM.md`` (2026-09-19, adversarial review of
``git diff 17aaf07..HEAD``), reproduced against the exact shapes the report
measured, before the fix each one names. F1-F5 were the release-blocking
first pass; F6/F7/F9/F10 were added in a second pass once fixing F3 made two
of them newly live rather than latent; F8/F13 close a third pass, and F11's
gate/release mismatch is closed alongside them (see each class's own
docstring).

* **F1** — ``core/catalog.py:229``: a Gemini 401 stopped rotating and ended
  the turn instead, because the generic catalog table (not only
  ``provider_rules.py``'s route-scoped Vertex rule) produced ``AUTH_REFRESH``.
* **F2** — ``core/classify.py:1046``: an uncertain-TERMINAL 400 could fall
  through to the permanent-auth/denial prose tables and retire a credential
  on nothing but the user's own prompt text echoed back by the provider.
* **F3** — ``dispatch_binding.py:3222``: ``settings.MAX_HOLD``
  (``KAME_MAX_HOLD``) was never pushed into the one ``Carousel`` the dispatch
  path actually marks and selects against.
* **F4** — ``core/shared_health.py:159`` / ``settings.py:627``: the config
  default for ``share_pool_health`` (on since G4 passed) never reached
  ``shared_health.enabled()``, which read only the environment.
* **F5** — ``core/shared_health.py:333``: an unremovable stale lock spun the
  write path at full CPU instead of honouring its own deadline.
* **F9** — ``core/carousel.py:1703``: the account-wide hold took a plain
  ``max()`` against its own history and was never re-trimmed by a tightened
  ``max_hold_s``, unlike ``sick_until`` right above it.
* **F6** — ``core/carousel.py:1271``/``1286``: a deadline read from another
  profile's shared entry was trusted verbatim, never re-bounded by the
  reader's own ceiling.
* **the future-dated write** — ``core/shared_health.py:558``: one event whose
  ``at`` was implausibly far ahead of wall-clock time set the pruning horizon
  for every other row, deleting them all.
* **F10** — ``integrity.py:62``: ``core/shared_health.py`` was importable
  unconditionally by ``core/carousel.py`` yet absent from
  ``REQUIRED_MODULES``, so a copy that lost the file passed the integrity
  check and only failed later, as an unwitnessed traceback.
* **F8** — ``core/shared_health.py:642``: ``if at >= existing_at`` discarded
  a genuinely later success stamped earlier than what was stored, after a
  backwards (or previously forward) clock step -- silently keeping a healthy
  key benched.
* **F13** — ``control.py:251``: ``/kame reset`` built its legacy-name list
  from ``_LEGACY_ENV_FOR`` only, never ``_LEGACY_ENV_FOR_1_0_8`` -- the
  oldest alias restored in 1.8.0.0 for I11 -- so resetting a setting reported
  success while leaving ``KAME_FIRST_TOKEN_PATIENCE`` set.

Each class below starts with the exact measured shape from ``RED_TEAM.md``
and asserts the fix; each also carries at least one regression guard proving
the narrow, intentional exception each fix preserves did not move.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1_8_0_0_redteam_under_test"


def _load_package():
    """Import the plugin as a package, the way the Hermes loader does.

    Self-contained, like every other test module in this suite: its own
    ``PACKAGE`` name, its own load, so this file can be read and run without
    knowing what any other test file did to the module cache, and so the
    module-level ``core.carousel.ENGINE`` singleton this file pokes at (F3)
    is this file's own copy, not one shared with any other test module.
    """
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
catalog = importlib.import_module(f"{PACKAGE}.core.catalog")
classify_mod = importlib.import_module(f"{PACKAGE}.core.classify")
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
shared_health_mod = importlib.import_module(f"{PACKAGE}.core.shared_health")
provider_rules = importlib.import_module(f"{PACKAGE}.core.provider_rules")
quota_mod = importlib.import_module(f"{PACKAGE}.core.quota")
dispatch_binding = importlib.import_module(f"{PACKAGE}.dispatch_binding")
settings = importlib.import_module(f"{PACKAGE}.settings")
integrity = importlib.import_module(f"{PACKAGE}.integrity")
control = importlib.import_module(f"{PACKAGE}.control")
envfile = importlib.import_module(f"{PACKAGE}.envfile")

classify = classify_mod.classify
Carousel = carousel_mod.Carousel
fingerprint = carousel_mod.fingerprint
SharedHealth = shared_health_mod.SharedHealth
QuotaScope = quota_mod.QuotaScope

NOW = 1_800_000_000.0
IDENTITY = "gemini:gemini-3.6-flash"
HOUR = 3600.0


@pytest.fixture(autouse=True)
def _clean_settings_and_shared_health_state():
    """Every test in this file starts from the same blank slate.

    ``settings._FROM_CONFIG``/``_NUMBERS_FROM_CONFIG`` and ``shared_health.
    _CONFIG_DEFAULT`` are module-level state pushed in once at registration
    (F3/F4's whole subject) — without this, one test's ``settings.load``/
    ``set_config_default`` call would leak into the next.
    """
    settings.forget()
    shared_health_mod.set_config_default(None)
    yield
    settings.forget()
    shared_health_mod.set_config_default(None)


# ---------------------------------------------------------------------------
# F1 — a Gemini 401 must not end the turn
# ---------------------------------------------------------------------------


class TestF1AGemini401Rotates:
    """core/catalog.py:229, dispatch_binding.py:2698.

    Before the fix, ``catalog.look_up`` filed these five tokens under the new
    ``AUTH_REFRESH`` family regardless of surface, so ``classify()`` returned
    ``None`` and ``dispatch_binding`` exited ``"raise", "auth_refresh"`` —
    the turn ends with every other key in the pool untried. The fix: only
    ``provider_rules.py``'s route-scoped ``google_vertex`` rule may produce
    ``AUTH_REFRESH``; the generic table goes back to ``AUTH_DEAD``, exactly
    as it read before 1.8.0.0's diff.
    """

    @pytest.mark.parametrize("token", [
        "gemini_unauthorized", "UNAUTHENTICATED", "authentication_error",
        "authentication", "invalid_authentication",
    ])
    def test_the_generic_catalog_table_reads_these_as_auth_dead(self, token):
        reading = catalog.look_up(token)
        assert reading is not None
        assert reading.family == catalog.AUTH_DEAD
        assert reading.family != catalog.AUTH_REFRESH

    def test_googles_own_standard_unauthenticated_wording_rotates_not_ends_the_turn(self):
        # RED_TEAM.md F1's exact measured shape: Google's own standard
        # UNAUTHENTICATED wording, on a bare (non-Vertex) Gemini surface.
        body = {"error": {"code": 401, "status": "UNAUTHENTICATED",
                           "message": "Request had invalid authentication credentials."}}
        verdict = classify(
            provider="gemini", status_code=401,
            error_message="Request had invalid authentication credentials.",
            error_body=body, now_epoch=NOW,
        )
        assert verdict is not None, (
            "classify() returned None -- the turn would end at "
            "dispatch_binding's auth_refresh exit with every other key untried"
        )
        assert verdict.reason == "auth_permanent"
        assert verdict.should_rotate_credential is True

    def test_hermes_own_gemini_unauthorized_name_rotates(self):
        # Hermes' own adapter name for the same 401 (see catalog.py's
        # comment on this token).
        body = {"error": {"code": "gemini_unauthorized"}}
        verdict = classify(
            provider="gemini", status_code=401,
            error_message="Gemini API key not valid.",
            error_body=body, now_epoch=NOW,
        )
        assert verdict is not None
        assert verdict.reason == "auth_permanent"

    def test_dispatch_rotates_instead_of_raising_auth_refresh(self):
        exc = Exception("Request had invalid authentication credentials.")
        exc.status_code = 401
        exc.body = {"error": {"code": 401, "status": "UNAUTHENTICATED",
                               "message": str(exc)}}
        engine = Carousel()
        binding = dispatch_binding.DispatchBinding(engine=engine)
        result = binding._on_failure("gemini:m", "synthetic-key", exc, "test", 1, False)
        assert result[0] == "rotate"
        assert result[0:2] != ("raise", "auth_refresh")
        assert engine.is_retired("gemini:m", "synthetic-key")

    def test_vertex_oauth_refresh_is_unaffected_the_narrow_case_survives(self):
        # Regression guard: the ONE case AUTH_REFRESH exists for --
        # provider_rules.py's route-scoped google_vertex rule -- must still
        # return to host refresh rather than retiring. api_surface() reads
        # this off the request URL, which a bare "gemini" call never
        # attaches, so this and the tests above cannot collide.
        exc = Exception("Missing, invalid, or expired OAuth token")
        exc.status_code = 401
        exc.request = SimpleNamespace(
            url="https://aiplatform.googleapis.com/v1/projects/p/locations/"
                "global/publishers/google/models/m:generateContent")
        exc.body = {"error": {"code": 401, "status": "UNAUTHENTICATED",
                               "message": str(exc)}}
        verdict = classify(provider="vertex", status_code=401, error=exc,
                            error_body=exc.body, now_epoch=NOW)
        assert verdict is None
        engine = Carousel()
        binding = dispatch_binding.DispatchBinding(engine=engine)
        result = binding._on_failure("vertex:m", "synthetic-key", exc, "test", 1, False)
        assert result[:2] == ("raise", "auth_refresh")
        assert engine.snapshot() == {}

    def test_explicit_invalid_key_evidence_still_outranks_everything(self):
        # Regression guard: the row this table has always had (invalid_api_key
        # / API_KEY_INVALID / ACCESS_TOKEN_TYPE_UNSUPPORTED) is untouched.
        assert catalog.look_up("invalid_api_key").family == catalog.AUTH_DEAD
        assert catalog.look_up("API_KEY_INVALID").family == catalog.AUTH_DEAD
        assert catalog.look_up("ACCESS_TOKEN_TYPE_UNSUPPORTED").family == catalog.AUTH_DEAD


# ---------------------------------------------------------------------------
# F2 — prose must not retire a credential
# ---------------------------------------------------------------------------


class TestF2ProseCannotRetireACredential:
    """core/classify.py:1046.

    The uncertain-TERMINAL wastebasket (``invalid_request_error``,
    ``certain=False``) falling through to prose checks was meant to let
    Anthropic's own spend-limit sentence answer a bare 400 (_BILLING_
    PATTERNS). Before the fix, the SAME fall-through also reached
    _PERMANENT_AUTH_PATTERNS and _DENIAL_PATTERNS -- tables that read the
    request's own text, which a provider can echo back from the user's own
    prompt (R43). Four of RED_TEAM.md F2's six measured phrases, replayed
    verbatim.
    """

    @staticmethod
    def _wastebasket_400(phrase: str):
        body = {"error": {"type": "invalid_request_error",
                           "message": f"Invalid request: could not parse: '{phrase}'"}}
        return classify(provider="anthropic", status_code=400,
                         error_message=body["error"]["message"],
                         error_body=body, now_epoch=NOW)

    def test_the_users_own_prompt_text_cannot_reach_auth_permanent(self):
        verdict = self._wastebasket_400("our account suspended")
        assert verdict is None, (
            "an ordinary English phrase inside the user's own prompt "
            "retired a credential for a fault that was never about one"
        )

    def test_a_key_no_longer_valid_phrase_in_a_wastebasket_400_defers_to_host(self):
        verdict = self._wastebasket_400("your key is no longer valid")
        assert verdict is None

    def test_an_ip_range_denial_phrase_in_a_wastebasket_400_defers_to_host(self):
        verdict = self._wastebasket_400("key allowed only from approved IP ranges")
        assert verdict is None

    def test_no_wastebasket_400_ever_reaches_auth_permanent_or_denied(self):
        for phrase in ("our account suspended", "your key is no longer valid",
                       "key allowed only from approved IP ranges",
                       "we have exceeded your current quota",
                       "nothing special at all"):
            verdict = self._wastebasket_400(phrase)
            if verdict is not None:
                assert verdict.reason not in ("auth_permanent",)
                assert getattr(verdict, "kind", "") != "denied"

    def test_the_anthropic_spend_limit_case_this_branch_exists_for_still_works(self):
        # Regression guard: the intended use of the fall-through --
        # Anthropic's own spend-limit sentence on a bare 400 -- must still
        # reach billing. This is _BILLING_PATTERNS, not the permanent-auth
        # or denial tables, so it is untouched by the new gate.
        verdict = self._wastebasket_400("reached your specified API usage limits")
        assert verdict is not None
        assert verdict.reason == "billing"

    def test_a_bare_wastebasket_with_nothing_specific_still_defers(self):
        # Regression guard: unaffected either way -- no pattern matches at
        # all, so this was already None before the fix.
        assert self._wastebasket_400("nothing special at all") is None

    def test_a_certain_terminal_reading_still_ends_the_turn_unchanged(self):
        # Regression guard: the gate only concerns the UNCERTAIN wastebasket.
        # A certain TERMINAL family (model_not_found etc) must still return
        # None immediately, never reaching any prose table.
        body = {"error": {"type": "not_found_error",
                           "message": "our account suspended -- models/x not found"}}
        verdict = classify(provider="anthropic", status_code=404,
                            error_message=body["error"]["message"],
                            error_body=body, now_epoch=NOW)
        assert verdict is None


# ---------------------------------------------------------------------------
# F3 — the owner's ceiling dial must reach the real dispatch engine
# ---------------------------------------------------------------------------


class TestF3TheCeilingDialReachesDispatch:
    """dispatch_binding.py:3222, core/carousel.py:2227.

    ``install()`` wired ``daily_cooldown_s`` from settings but never
    ``max_hold_s``, so the only ``Carousel`` this plugin ever marks/selects
    against -- the module-level ``ENGINE`` singleton -- kept the hard-coded
    3600s default no matter what ``KAME_MAX_HOLD`` said. Proven through
    ``dispatch_binding.install()`` itself, not a hand-built ``Carousel`` --
    ``test_v1_8_0_0_ceiling.py`` already covers the pure-Carousel half.
    """

    @staticmethod
    def _fake_host_module():
        # Shaped exactly the way DispatchBinding.install() requires: both
        # wrapped functions present, callable, with (agent, api_kwargs, ...)
        # as their first two parameters (dispatch_binding.py's
        # _EXPECTED_SECOND_PARAM check).
        def interruptible_api_call(agent, api_kwargs, *a, **kw):
            return None

        def interruptible_streaming_api_call(agent, api_kwargs, *a, **kw):
            return None

        return SimpleNamespace(
            interruptible_api_call=interruptible_api_call,
            interruptible_streaming_api_call=interruptible_streaming_api_call,
        )

    def test_lowering_kame_max_hold_reaches_the_engine_install_actually_uses(self, monkeypatch):
        monkeypatch.setenv("KAME_MAX_HOLD", "120")
        monkeypatch.delenv("KAME_ROTATION_DISABLED", raising=False)
        monkeypatch.delenv("KAME_CAROUSEL_DISABLED", raising=False)
        original_max_hold = carousel_mod.ENGINE.max_hold_s
        try:
            binding = dispatch_binding.install(module=self._fake_host_module())
            assert binding is not None, "install() refused the fake module -- shape mismatch"
            assert binding.engine.max_hold_s == 120.0, (
                "KAME_MAX_HOLD=120 left the real dispatch engine's ceiling "
                "unchanged -- the exact bug measured in RED_TEAM.md F3"
            )
            # And the ceiling this settles is not just an attribute: a real
            # 9-hour stated hold through this SAME engine is bounded by it.
            applied = binding.engine.mark(
                IDENTITY, "key-a", False, 9 * HOUR, "daily", now=NOW, stated=True,
            )
            assert applied == 120.0
        finally:
            carousel_mod.ENGINE.max_hold_s = original_max_hold

    def test_the_default_still_applies_with_nothing_configured(self, monkeypatch):
        monkeypatch.delenv("KAME_MAX_HOLD", raising=False)
        monkeypatch.delenv("KAME_ROTATION_DISABLED", raising=False)
        monkeypatch.delenv("KAME_CAROUSEL_DISABLED", raising=False)
        original_max_hold = carousel_mod.ENGINE.max_hold_s
        try:
            binding = dispatch_binding.install(module=self._fake_host_module())
            assert binding is not None
            assert binding.engine.max_hold_s == 3600.0
        finally:
            carousel_mod.ENGINE.max_hold_s = original_max_hold


# ---------------------------------------------------------------------------
# F4 — sharing must not ship off while claiming on
# ---------------------------------------------------------------------------


class TestF4SharingShipsWhatItClaims:
    """core/shared_health.py:159, settings.py:627.

    ``enabled()`` read only ``os.environ``; ``settings.load()`` never
    exported the variable and nothing called ``settings.is_on`` from
    ``core``, so a fresh install's config default (on, since G4 passed) was
    inert. Fixed by pushing the resolved value in once, from the host layer,
    via ``set_config_default`` -- the environment variable still overrides.
    """

    def test_unset_environment_and_no_pushed_default_is_still_off(self, monkeypatch):
        # Regression guard: a process that never wires anything in (a bare
        # SharedHealth() in a test, or a caller that predates this fix)
        # reads the same False this module always defaulted to.
        monkeypatch.delenv(shared_health_mod.ENV_ENABLE, raising=False)
        assert shared_health_mod.enabled() is False

    def test_pushing_the_config_default_on_turns_it_on_with_no_environment_set(self, monkeypatch):
        monkeypatch.delenv(shared_health_mod.ENV_ENABLE, raising=False)
        shared_health_mod.set_config_default(True)
        assert shared_health_mod.enabled() is True, (
            "a fresh install with nothing in the environment shipped the "
            "feature off even though settings.DEFAULTS_ON says on -- the "
            "exact gap RED_TEAM.md F4 measured"
        )

    def test_the_environment_variable_still_overrides_the_pushed_default(self, monkeypatch):
        # Regression guard: the escape hatch must keep working even when the
        # config default disagrees with it, in both directions.
        shared_health_mod.set_config_default(True)
        monkeypatch.setenv(shared_health_mod.ENV_ENABLE, "0")
        assert shared_health_mod.enabled() is False

        shared_health_mod.set_config_default(False)
        monkeypatch.setenv(shared_health_mod.ENV_ENABLE, "1")
        assert shared_health_mod.enabled() is True

    def test_settings_defaults_on_resolves_true_with_nothing_configured(self, monkeypatch):
        # settings.is_on(SHARE_POOL_HEALTH) is the value dispatch_binding.
        # install() pushes in. Confirm it actually resolves True on a bare
        # process (no config loaded, no environment set) -- the G4-passed
        # default this whole fix exists to make real.
        monkeypatch.delenv("KAME_SHARE_POOL_HEALTH", raising=False)
        assert settings.is_on(settings.SHARE_POOL_HEALTH) is True

    def test_installing_through_the_dispatch_path_turns_sharing_on_with_nothing_configured(self, monkeypatch):
        # End-to-end: the same install() path F3 exercises also wires F4,
        # with no environment variable set at all.
        monkeypatch.delenv("KAME_SHARE_POOL_HEALTH", raising=False)
        monkeypatch.delenv("KAME_ROTATION_DISABLED", raising=False)
        monkeypatch.delenv("KAME_CAROUSEL_DISABLED", raising=False)

        def interruptible_api_call(agent, api_kwargs, *a, **kw):
            return None

        def interruptible_streaming_api_call(agent, api_kwargs, *a, **kw):
            return None

        fake_module = SimpleNamespace(
            interruptible_api_call=interruptible_api_call,
            interruptible_streaming_api_call=interruptible_streaming_api_call,
        )
        binding = dispatch_binding.install(module=fake_module)
        assert binding is not None
        assert shared_health_mod.enabled() is True


# ---------------------------------------------------------------------------
# F5 — the stale-lock branch must honour its own deadline
# ---------------------------------------------------------------------------


class TestF5StaleLockHonoursTheDeadline:
    """core/shared_health.py:326-341, ``_acquire_file_lock``.

    An old lock file this process cannot delete used to be retried forever
    -- ``continue`` with no deadline check and no sleep -- because the
    branch assumed removal either worked or the caller would eventually stop
    calling. Measured: still spinning after 3s against a 50ms contract.
    """

    def test_an_unremovable_stale_lock_gives_up_within_the_spin_budget(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "pool-health.json.lock"
        lock_path.write_bytes(b"")
        old = time.time() - 999.0
        os.utime(lock_path, (old, old))

        def _boom_unlink(self, *_a, **_kw):
            raise PermissionError("held by another process/user, no delete ACL")

        monkeypatch.setattr(Path, "unlink", _boom_unlink)

        started = time.time()
        result = shared_health_mod._acquire_file_lock(
            lock_path, spin_s=0.05, poll_s=0.005, stale_after_s=0.01,
        )
        elapsed = time.time() - started

        assert result is False
        assert elapsed < 1.0, (
            f"_acquire_file_lock spun for {elapsed:.2f}s against a 0.05s "
            "contract -- the exact hang RED_TEAM.md F5 measured at 3s+"
        )

    def test_a_removable_stale_lock_is_still_reclaimed(self, tmp_path):
        # Regression guard: decisions/0006's accepted, recoverable case (a
        # killed process's own lock) must keep working -- this fix only
        # bounds the UNREMOVABLE case, it must not make every stale lock
        # fail outright.
        lock_path = tmp_path / "pool-health.json.lock"
        lock_path.write_bytes(b"")
        old = time.time() - 999.0
        os.utime(lock_path, (old, old))

        result = shared_health_mod._acquire_file_lock(
            lock_path, spin_s=0.05, poll_s=0.005, stale_after_s=0.01,
        )
        assert result is True
        assert lock_path.exists()  # reclaimed and re-created by this process

    def test_a_genuinely_held_fresh_lock_still_gives_up_at_the_ordinary_deadline(self, tmp_path):
        # Regression guard: the ordinary (non-stale) path is untouched --
        # still bounded by `deadline`, still returns False rather than
        # blocking, for a lock that is simply busy rather than abandoned.
        lock_path = tmp_path / "pool-health.json.lock"
        lock_path.write_bytes(b"")  # fresh -- age is ~0, never "stale"

        started = time.time()
        result = shared_health_mod._acquire_file_lock(
            lock_path, spin_s=0.05, poll_s=0.005, stale_after_s=999.0,
        )
        elapsed = time.time() - started

        assert result is False
        assert elapsed < 1.0

    def test_end_to_end_a_stuck_lock_does_not_hang_a_real_record_call(self, tmp_path, monkeypatch):
        # Same failure, exercised through the public path RED_TEAM.md F5
        # names directly: SharedHealth.record(), called after every mark()
        # outcome, must not hang the turn that produced it.
        path = tmp_path / "pool-health.json"
        lock_path = tmp_path / "pool-health.json.lock"
        lock_path.write_bytes(b"")
        old = time.time() - 999.0
        os.utime(lock_path, (old, old))

        def _boom_unlink(self, *_a, **_kw):
            raise PermissionError("held by another process/user, no delete ACL")

        monkeypatch.setattr(Path, "unlink", _boom_unlink)
        monkeypatch.setattr(shared_health_mod, "_LOCK_STALE_S", 0.01)

        store = SharedHealth(path=path, profile="p1", enabled_fn=lambda: True)
        started = time.time()
        store.record(scope="model", subject=IDENTITY,
                      fingerprint_key=fingerprint("sk-test-key"),
                      until=NOW + 60.0, kind="rate_limit", at=NOW)
        elapsed = time.time() - started
        assert elapsed < 1.0


# ---------------------------------------------------------------------------
# F9 -- account-wide holds must be trimmed by the ceiling, same as sick_until
# ---------------------------------------------------------------------------


class TestF9AccountHoldsRespectTheCeiling:
    """core/carousel.py:1703.

    ``sick_until`` (the per-model bench) is re-derived and re-clamped to
    ``now + max_hold_s`` on every ``mark`` -- including one that did not
    itself produce a long hold -- which is what actually re-trims a hold
    stored under a looser dial. The account-wide hold a few lines below only
    ever took ``max(existing, sick_until)`` against its OWN stored value, so
    once it held a number bigger than a later, tighter ``max_hold_s``,
    nothing ever brought it back down. ``decisions/0005`` says the ceiling
    holds on every path; an account hold is a path.

    Escalated to a blocker alongside F6 once F3 was fixed: before that, every
    profile ran the hard-coded 3600s ceiling regardless of its own
    ``KAME_MAX_HOLD``, so no one could ever observe a tightened dial fail to
    shrink an old hold in production. F3 makes the dial real, which makes
    this reachable.
    """

    def test_an_account_hold_is_trimmed_when_the_dial_is_tightened(self):
        provider = "gemini"
        key = "acct-key-1"
        engine = Carousel(max_hold_s=HOUR)
        engine.mark(IDENTITY, key, False, HOUR, "daily", now=NOW,
                    stated=True, scope=QuotaScope.ACCOUNT)
        assert engine._account_hold[(provider, key)] == pytest.approx(NOW + HOUR)

        # The owner tightens the dial mid-run, the same move
        # test_v1_8_0_0_ceiling.py's retained-prior-hold test makes for
        # sick_until.
        engine.max_hold_s = 60.0
        engine.mark(IDENTITY, key, False, 1.0, "server", now=NOW + 10.0,
                    stated=True, scope=QuotaScope.ACCOUNT)

        assert engine._account_hold[(provider, key)] == pytest.approx(NOW + 10.0 + 60.0), (
            "the account hold ignored the tightened ceiling and kept the "
            "value recorded under the old, wider dial"
        )

    def test_the_never_shorten_rule_still_holds_when_the_ceiling_is_not_binding(self):
        # Regression guard: a softer refusal on a second model of the same
        # provider must not cut short a longer account-wide deadline a first
        # model already established -- the reason this was a plain max() in
        # the first place. The fix's min(max(...), ceiling) must keep that
        # half intact when the ceiling is not what is limiting.
        provider = "gemini"
        key = "acct-key-2"
        engine = Carousel(max_hold_s=HOUR)
        engine.mark(IDENTITY, key, False, HOUR, "daily", now=NOW,
                    stated=True, scope=QuotaScope.ACCOUNT)
        engine.mark("gemini:another-model", key, False, 1.0, "server",
                    now=NOW + 10.0, stated=True, scope=QuotaScope.ACCOUNT)
        assert engine._account_hold[(provider, key)] == pytest.approx(NOW + HOUR)


# ---------------------------------------------------------------------------
# F6 -- a shared deadline must be re-bounded by the READER's own ceiling
# ---------------------------------------------------------------------------


class TestF6SharedDeadlinesAreReBoundedByTheReader:
    """core/carousel.py:1271 (_model_deadline) and :1286 (_account_deadline).

    Two profiles may run different KAME_MAX_HOLD dials on purpose
    (decisions/0006: base/k/lo1 share one physical pool). Before this,
    whichever profile wrote the shared entry decided how long every OTHER
    profile honoured it -- a writer with a 9h ceiling and a reader with a 1h
    ceiling produced a 9h next_recovery_seconds in the reader, and a
    backwards clock step on the writer's machine could hold a key past the
    reader's ceiling indefinitely, since nothing re-derives an absolute
    until read off disk until the reader's own next mark.

    Same escalation as F9: latent while F3 was unfixed (every profile ran
    the same hard-coded ceiling), live the moment it was.
    """

    @staticmethod
    def _shared_store(path, profile):
        return SharedHealth(path=path, profile=profile, enabled_fn=lambda: True)

    def test_a_shared_model_hold_is_bounded_by_the_readers_own_ceiling(self, tmp_path):
        path = tmp_path / "pool-health.json"
        key = "shared-key-1"
        writer = Carousel(max_hold_s=9 * HOUR,
                           shared_health_store=self._shared_store(path, "base"))
        writer.mark(IDENTITY, key, False, 9 * HOUR, "daily", now=NOW, stated=True)

        reader = Carousel(max_hold_s=HOUR,
                           shared_health_store=self._shared_store(path, "k"))
        deadline = reader._model_deadline({}, IDENTITY, key, NOW + 1.0, True)
        assert deadline == pytest.approx(NOW + 1.0 + HOUR), (
            "the reader honoured the writer's 9h ceiling instead of its own 1h one"
        )

    def test_a_shared_account_hold_is_bounded_by_the_readers_own_ceiling(self, tmp_path):
        path = tmp_path / "pool-health.json"
        provider = "gemini"
        key = "shared-key-2"
        writer = Carousel(max_hold_s=9 * HOUR,
                           shared_health_store=self._shared_store(path, "base"))
        writer.mark(IDENTITY, key, False, 9 * HOUR, "daily", now=NOW, stated=True,
                    scope=QuotaScope.ACCOUNT)

        reader = Carousel(max_hold_s=HOUR,
                           shared_health_store=self._shared_store(path, "k"))
        deadline = reader._account_deadline(provider, key, NOW + 1.0, True)
        assert deadline == pytest.approx(NOW + 1.0 + HOUR)

    def test_a_shared_success_still_zeroes_a_local_bench_regardless_of_ceiling(self, tmp_path):
        # Regression guard: the freshness comparison (the whole reason
        # _model_deadline is not a max()) must survive re-bounding -- a
        # shared until:0 still beats a positive local bench.
        path = tmp_path / "pool-health.json"
        key = "shared-key-3"
        writer = Carousel(max_hold_s=HOUR,
                           shared_health_store=self._shared_store(path, "base"))
        reader = Carousel(max_hold_s=HOUR,
                           shared_health_store=self._shared_store(path, "k"))
        reader.mark(IDENTITY, key, False, HOUR, "daily", now=NOW, stated=True)
        writer.mark(IDENTITY, key, True, now=NOW + 5.0)
        deadline = reader._model_deadline(
            reader._pools[IDENTITY][key], IDENTITY, key, NOW + 10.0, True)
        assert deadline == 0.0


# ---------------------------------------------------------------------------
# the future-dated write must not prune every other entry
# ---------------------------------------------------------------------------


class TestFutureDatedWriteDoesNotEmptyTheFile:
    """core/shared_health.py:558, the _prune call inside _write_locked.

    _prune ages every OTHER row against the at of the event currently being
    written -- correct when at is an ordinary wall-clock moment, wrong the
    moment at is implausibly far ahead of it. A single write with a
    synthetic or clock-skewed future at used to set the pruning horizon for
    the whole document, dropping every live row in one write. Reachable via
    a forward clock/NTP step, a VM resume, or any tool writing a synthetic
    at.
    """

    def test_a_future_dated_write_does_not_delete_every_other_row(self, tmp_path):
        # Real wall-clock time, deliberately NOT this file's NOW constant --
        # NOW is fixed far enough ahead of "today" that it would itself look
        # implausible against a live wall clock, which would confound
        # exactly the comparison this test makes.
        base_now = time.time()
        path = tmp_path / "pool-health.json"
        store = SharedHealth(path=path, profile="base", enabled_fn=lambda: True)
        for i in range(3):
            store.record(scope="model", subject=f"gemini:model-{i}",
                         fingerprint_key=fingerprint(f"sk-key-{i}"),
                         until=base_now + 60.0, kind="rate_limit", at=base_now)

        # One event stamped two days ahead -- a forward clock/NTP step, a VM
        # resume, or any caller writing a synthetic at.
        store.record(scope="model", subject="gemini:model-99",
                     fingerprint_key=fingerprint("sk-key-99"),
                     until=base_now + 60.0, kind="rate_limit",
                     at=base_now + 2 * 86400.0)

        document = json.loads(path.read_text(encoding="utf-8"))
        model_rows = document.get("model", {})
        total_entries = sum(len(rows) for rows in model_rows.values())
        assert total_entries == 4, (
            f"expected all 4 entries to survive, found {total_entries} -- "
            "the future-dated write pruned the others"
        )

    def test_a_genuinely_old_entry_is_still_pruned(self, tmp_path):
        # Regression guard: the fix must not defeat ordinary pruning. An
        # entry that is actually a day+ old, with nothing implausible about
        # the WRITE that ages it out, must still go.
        base_now = time.time()
        path = tmp_path / "pool-health.json"
        store = SharedHealth(path=path, profile="base", enabled_fn=lambda: True)
        store.record(scope="model", subject="gemini:old-model",
                     fingerprint_key=fingerprint("sk-old-key"),
                     until=base_now - 90000.0, kind="rate_limit",
                     at=base_now - (shared_health_mod.PRUNE_AFTER_S + 3600.0))
        store.record(scope="model", subject="gemini:new-model",
                     fingerprint_key=fingerprint("sk-new-key"),
                     until=base_now + 60.0, kind="rate_limit", at=base_now)

        document = json.loads(path.read_text(encoding="utf-8"))
        model_rows = document.get("model", {})
        assert "gemini:old-model" not in model_rows
        assert "gemini:new-model" in model_rows


# ---------------------------------------------------------------------------
# F10 -- core/shared_health.py must be a REQUIRED module
# ---------------------------------------------------------------------------


class TestF10SharedHealthIsARequiredModule:
    """integrity.py:62.

    core/carousel.py imports core/shared_health.py unconditionally at module
    level, so a copy that shipped without it does not degrade -- it raises
    ImportError the moment the carousel loads, which is precisely what
    REQUIRED_MODULES's own docstring says the list exists to catch before it
    surfaces as a traceback nobody saw.
    """

    def test_shared_health_is_declared_required(self):
        assert "core/shared_health.py" in integrity.REQUIRED_MODULES

    def test_a_copy_missing_it_is_reported_incomplete(self, tmp_path):
        for name in integrity.REQUIRED_MODULES:
            if name == "core/shared_health.py":
                continue
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# stub\n", encoding="utf-8")
        report = integrity.verify(str(tmp_path))
        assert report["complete"] is False
        assert "core/shared_health.py" in report["missing_required"]


# ---------------------------------------------------------------------------
# F8 -- a backwards (or forward) clock step must not drop a releasing success
# ---------------------------------------------------------------------------


class TestF8AClockStepDoesNotDropARelease:
    """core/shared_health.py:642, ``_write_locked``.

    ``if at >= existing_at`` is the right guard for an out-of-order WRITE on
    a clock that never moves, and the wrong one across a clock STEP.
    RED_TEAM.md's measured shape: a bench is written at ``T``, then the SAME
    credential's success — genuinely later in wall time — is written at
    ``T-30`` because the machine's shared clock stepped backwards in
    between. Before the fix the success lost the comparison and the key
    stayed benched for up to the full ceiling: bounded damage (F6/F9 already
    cap it), but not the release F8 asked about.

    The fix favours a release unconditionally over the ``at`` comparison,
    because the two failure directions are not equally costly: losing a
    release keeps a healthy key benched for the whole ceiling span, which
    costs the pool that key's throughput; accepting a release that turns out
    to be stale costs at most one refused request the next time the key is
    tried. Only releases get this exemption, which the third test below
    pins.
    """

    def test_a_backwards_clock_step_does_not_drop_the_releasing_success(self, tmp_path):
        path = tmp_path / "pool-health.json"
        store = SharedHealth(path=path, profile="base", enabled_fn=lambda: True)
        key = fingerprint("sk-clock-key-backwards")

        store.record(scope="model", subject=IDENTITY, fingerprint_key=key,
                     until=NOW + HOUR, kind="rate_limit", at=NOW)
        # The success genuinely happened after the bench above, but the
        # shared clock stepped back 30s before it was stamped.
        store.record(scope="model", subject=IDENTITY, fingerprint_key=key,
                     until=0.0, kind="success", at=NOW - 30.0)

        until, _at = store.model_entry(IDENTITY, key, now=NOW)
        assert until <= 0.0, (
            f"the releasing success was dropped by the backwards clock step "
            f"-- model_entry still reports until={until}"
        )

    def test_a_forward_clock_step_on_the_bench_does_not_trap_a_later_release(self, tmp_path):
        # The other direction RED_TEAM.md asked to cover: the BENCH was
        # written under a clock that had already jumped forward (an
        # inflated `at`), and the release that should clear it arrives
        # afterwards, stamped with an ordinary, lower `at`.
        path = tmp_path / "pool-health.json"
        store = SharedHealth(path=path, profile="base", enabled_fn=lambda: True)
        key = fingerprint("sk-clock-key-forward")

        store.record(scope="model", subject=IDENTITY, fingerprint_key=key,
                     until=NOW + HOUR, kind="rate_limit", at=NOW + 2 * HOUR)
        store.record(scope="model", subject=IDENTITY, fingerprint_key=key,
                     until=0.0, kind="success", at=NOW + HOUR)

        until, _at = store.model_entry(IDENTITY, key, now=NOW)
        assert until <= 0.0, (
            f"the release lost to a bench stamped under a forward-jumped "
            f"clock -- model_entry still reports until={until}"
        )

    def test_a_genuinely_stale_bench_write_is_still_dropped(self, tmp_path):
        # Regression guard: the exemption is for RELEASES only. An ordinary
        # non-release write that lands out of order -- a slow writer for an
        # EARLIER bench event, arriving after a newer one is already
        # recorded -- must still lose the comparison. This is the
        # "genuinely stale write" the guard exists to catch, and F8 must not
        # remove it.
        path = tmp_path / "pool-health.json"
        store = SharedHealth(path=path, profile="base", enabled_fn=lambda: True)
        key = fingerprint("sk-clock-key-stale-bench")

        store.record(scope="model", subject=IDENTITY, fingerprint_key=key,
                     until=NOW + HOUR, kind="rate_limit", at=NOW)
        # A stale, non-release write for an event 10s in the past.
        store.record(scope="model", subject=IDENTITY, fingerprint_key=key,
                     until=NOW + 5.0, kind="rate_limit", at=NOW - 10.0)

        until, at = store.model_entry(IDENTITY, key, now=NOW)
        assert until == pytest.approx(NOW + HOUR)
        assert at == pytest.approx(NOW)


# ---------------------------------------------------------------------------
# F13 -- /kame reset must clear the oldest restored env name too
# ---------------------------------------------------------------------------


class TestF13ResetClearsTheOldestRestoredAlias:
    """control.py:251, ``_legacy_names``.

    ``_LEGACY_ENV_FOR_1_0_8`` (``KAME_FIRST_TOKEN_PATIENCE``, restored in
    1.8.0.0 for I11 -- see ``tests/test_v1_8_0_0_invariants.py``'s
    ``TestI11ADeprecatedSettingNameWorksForEver``) is a table
    ``_legacy_names`` never read; it only ever read ``_LEGACY_ENV_FOR``, one
    generation newer. ``/kame reset`` popped every OTHER spelling, reported
    success, and left this one set in both the environment and ``.env`` --
    exactly where ``settings._env_names`` still reads it from. A name that
    answers has to be one reset can clear too.

    ``envfile.path`` is monkeypatched to a real temp file, the same recipe
    ``test_v1_1_1.py``'s ``test_reset_removes_the_variable_rather_than_
    writing_the_default`` uses -- this process has no real Hermes to ask for
    an ``.env`` path, so ``envfile.forget`` would otherwise return ``False``
    on the very FIRST name in ``_forget_one``'s loop and never even reach
    the legacy names, which would make this test pass for the wrong reason
    (an early exit) both before and after the fix.
    """

    @pytest.fixture(autouse=True)
    def _clean_env(self):
        key = settings.STREAM_SILENCE_TIMEOUT
        names = settings._env_names(key)
        before = {name: os.environ.get(name) for name in names}
        for name in names:
            os.environ.pop(name, None)
        yield
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_reset_clears_the_oldest_restored_alias(self, tmp_path, monkeypatch):
        key = settings.STREAM_SILENCE_TIMEOUT
        env = tmp_path / ".env"
        env.write_text("KAME_FIRST_TOKEN_PATIENCE=42\n", encoding="utf-8")
        monkeypatch.setattr(envfile, "path", lambda: env)
        os.environ["KAME_FIRST_TOKEN_PATIENCE"] = "42"
        settings.forget()
        assert settings.number(key, 0.0) == 42.0  # sanity: the alias is read

        ok, detail = control._apply("reset", "stream_silence_timeout_seconds", None)
        assert ok, detail

        assert "KAME_FIRST_TOKEN_PATIENCE" not in os.environ, (
            "reset reported success but left the oldest restored alias set "
            "in the environment"
        )
        assert "KAME_FIRST_TOKEN_PATIENCE" not in env.read_text(encoding="utf-8"), (
            "reset reported success but left the oldest restored alias in .env"
        )
        settings.forget()
        assert settings.number(key, 0.0) == 0.0, (
            "the setting still reads the oldest alias after a reset that "
            "claimed to clear it"
        )

    def test_reset_still_clears_the_1_0_9_alias_too(self, tmp_path, monkeypatch):
        # Regression guard: fixing the oldest table must not disturb the
        # newer one control.py already read correctly.
        key = settings.STREAM_SILENCE_TIMEOUT
        env = tmp_path / ".env"
        env.write_text("KAME_SILENT_STREAM_PATIENCE=17\n", encoding="utf-8")
        monkeypatch.setattr(envfile, "path", lambda: env)
        os.environ["KAME_SILENT_STREAM_PATIENCE"] = "17"
        settings.forget()
        assert settings.number(key, 0.0) == 17.0

        ok, detail = control._apply("reset", "stream_silence_timeout_seconds", None)
        assert ok, detail
        assert "KAME_SILENT_STREAM_PATIENCE" not in os.environ
        assert "KAME_SILENT_STREAM_PATIENCE" not in env.read_text(encoding="utf-8")

    def test_reset_still_clears_the_current_name_when_all_three_are_set(self, tmp_path, monkeypatch):
        # Regression guard: all three generations set at once (an install
        # carrying every rename it ever survived) must all go.
        key = settings.STREAM_SILENCE_TIMEOUT
        env = tmp_path / ".env"
        env.write_text(
            "KAME_STREAM_SILENCE_TIMEOUT=5\n"
            "KAME_SILENT_STREAM_PATIENCE=17\n"
            "KAME_FIRST_TOKEN_PATIENCE=42\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(envfile, "path", lambda: env)
        os.environ["KAME_STREAM_SILENCE_TIMEOUT"] = "5"
        os.environ["KAME_SILENT_STREAM_PATIENCE"] = "17"
        os.environ["KAME_FIRST_TOKEN_PATIENCE"] = "42"
        settings.forget()

        ok, detail = control._apply("reset", "stream_silence_timeout_seconds", None)
        assert ok, detail
        after = env.read_text(encoding="utf-8")
        for variable in ("KAME_STREAM_SILENCE_TIMEOUT", "KAME_SILENT_STREAM_PATIENCE",
                          "KAME_FIRST_TOKEN_PATIENCE"):
            assert variable not in os.environ, variable
            assert variable not in after, variable
        settings.forget()
        assert settings.number(key, 0.0) == 0.0
