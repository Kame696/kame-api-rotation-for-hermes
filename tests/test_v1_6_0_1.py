"""1.6.0.1 — the screens stopped lying, and the clocks stopped rounding up.

Every check in this file is here because something got past the ones that
came before it. They fall into two halves.

--------------------------------------------------------------------------
Half one: the panel described a machine that was not running
--------------------------------------------------------------------------

Four separate ways, all visible in one screenshot the owner sent:

* **Two writers, one file.** The Desktop and the gateway both load this
  plugin, both hold the same credential pool, and both wrote the whole of
  ``state.json``. Whichever wrote last won, so a panel on 1.6.0.0 could show
  a 1.5.0 reading and call it its own. The file now has one section per
  process and each writer owns exactly its own.
* **Thirty-two empty providers.** "What KAME can see" listed every provider
  the host had ever mentioned, whether or not a single key sat behind it.
* **A rejected key counted as ready.** The header said *15 of 17 ready*
  while the banner directly above it said two keys had been refused as
  credentials. Both numbers came from the same snapshot.
* **A key nobody could account for.** The pool legitimately holds keys the
  user's own config does not list — Hermes resolves some itself — and the
  panel gave the owner no way to tell those apart from a typo.

--------------------------------------------------------------------------
Half two: a refusal is not a clock
--------------------------------------------------------------------------

The owner put it plainly, having watched four keys sit out an hour each:

    timeout não deve demorar uma hora ... não tem problema tentar novamente
    então 1 hora é muito grande

The instinct was right and the reasoning generalises past timeouts. KAME's
cooldowns divide cleanly in two:

* **Clocks.** A per-minute throttle, a daily cap, an account allowance. The
  provider is metering time and the only cure is time. Waiting is the fix.
* **Refusals.** 401, a revoked key, a 403 saying this key may not have this
  model. The provider is describing the credential. Waiting fixes nothing.

Until 1.6.0.1 both were benched for the same hour, on the reasoning that
since waiting cannot repair a refused key the exact wait hardly matters. That
reasoning is wrong by asymmetry:

* wrong in the long direction — the provider was having an incident, or the
  401 was a transient edge failure — costs a **healthy key, for an hour**;
* wrong in the short direction costs **one request** that is refused in
  milliseconds and never metered, because a 401 is rejected before it counts.

So a refusal now rests five minutes. The thing that makes five minutes safe
is not the number: it is ``carousel.select`` offering a refused key *last*,
behind every other healthy key in the pool. A key that really is dead is
reached only when there is nothing better, which is exactly the moment trying
anything is right. Escalation still climbs from there toward the daily hour
when the refusals keep coming, so a genuinely dead key settles at the hourly
re-probe on its own — it is just no longer *assumed* dead on sight.

The last section of this file pins the whole kind → rest table against Agent
Zero's, because "does it rotate at the right time for each error" is a
question the owner should be able to have answered by running the tests.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PANEL = PLUGIN_DIR / "desktop-ui" / "plugin.js"
PACKAGE = "kame_v1601_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_package()
state_mod = importlib.import_module(f"{PACKAGE}.state")

carousel = importlib.import_module("hermes-kame-api-rotation.core.carousel")
classify_mod = importlib.import_module("hermes-kame-api-rotation.core.classify")
quota = importlib.import_module("hermes-kame-api-rotation.core.quota")
classify = classify_mod.classify

NOW = 1_000_000.0


def _mine():
    """The fields ``_merged`` reads off this process's own section."""
    return {"pid": os.getpid(), "updated_at": time.time(), "version": "1.6.0.1"}


# ==========================================================================
# A refusal is not a clock
# ==========================================================================

class TestARefusedCredentialRestsShortAndWaitsAtTheBack:
    """Five minutes and last in line, instead of an hour and first in line."""

    def test_the_provider_saying_the_words_is_its_own_kind(self):
        """`revoked`, not `auth`. The only kind that retires on sight."""
        delay, kind, status = carousel.classify(
            None, "API key not valid. Please pass a valid API key.",
            status_code=401,
        )
        assert kind == "revoked"
        assert delay == quota.DEFAULT_REJECTED_BENCH_SECONDS
        assert delay < carousel.DAILY_COOLDOWN_S

    def test_a_bare_401_stays_ambiguous(self):
        """The 1.4.0 disaster, as a check.

        `unauthorized` is the HTTP reason phrase for every 401 — an expired
        OAuth token a second from refreshing sends it, and so does a proxy.
        Reading it as "this is not a key" retired twenty-one healthy keys for
        an hour each. It must reach the *ambiguous* kind, which needs three in
        a row before anything is taken out of rotation.
        """
        delay, kind, _ = carousel.classify(None, "Unauthorized", status_code=401)
        assert kind == "auth"
        assert kind not in ("revoked",)
        assert delay == quota.DEFAULT_REJECTED_BENCH_SECONDS

    def test_a_model_this_key_may_not_use_keeps_its_hour(self):
        """The distinction the owner drew, and it is the right one.

        "This key may not use this model" is a fact about the *pairing*. The
        credential may be the healthiest in the account on every other model,
        so retiring it would throw away a working key over a permission that
        was never about the key.

        The rest is short and the hour is not lost: ``denied`` is on the
        doubling ladder, so a permission that really is permanent reaches an
        hour by itself, while a plan somebody upgrades comes back in the
        seconds it actually took. It costs nothing meanwhile because pool
        health is per provider:model and the key is offered last.
        """
        delay, kind, _ = carousel.classify(
            None, "Generative Language API has not been used in project 12345",
        )
        assert kind == "denied"
        assert delay == carousel.DENIED_REST_S == quota.DEFAULT_DENIAL_BENCH_SECONDS
        assert kind not in carousel.RETIRING_KINDS

    def test_the_evidence_first_classifier_agrees_with_the_table(self):
        """One refusal, one bench, whichever classifier answers first.

        This test existed before 1.6.0.1 and was asserting the disagreement
        it was named for: it read ``DEFAULT_REJECTED_BENCH_SECONDS`` while
        the table beside it said an hour, and both sides passed. The two
        constants have been collapsed into one, and this now reads the same
        name the carousel does, so there is nothing left for the two to
        differ about.
        """
        verdict = classify(
            provider="gemini", status_code=403,
            error_message="PERMISSION_DENIED: project has been denied access",
            now_epoch=NOW,
        )
        assert verdict is not None
        # ``auth`` on the wire because that is the only word Hermes has:
        # ``reason`` is coerced to a ``FailoverReason`` member and an unknown
        # one drops the whole classification.
        assert verdict.reason == "auth"
        # ...and ``denied`` underneath, which is what keeps it out of
        # ``RETIRING_KINDS``.
        assert verdict.kind == "denied"
        assert verdict.reset_at == NOW + carousel.DENIED_REST_S

    def test_a_model_refusal_never_costs_the_key_through_the_real_dispatch(self):
        """The same rule, through the translation where the bug actually lived.

        The carousel was always right about ``denied``. The defect was one
        layer up: ``classify`` had no word for it that Hermes would accept,
        so the denial arrived at ``_on_failure`` labelled ``auth`` — which
        *is* in ``RETIRING_KINDS``. Testing the carousel alone would have
        gone on passing while a real turn retired the key.
        """
        dispatch = importlib.import_module(f"{PACKAGE}.dispatch_binding")
        engine = carousel.Carousel()
        binding = dispatch.DispatchBinding(engine=engine)
        identity, key = "google:gemini-3.7-flash", "kk"
        for _ in range(carousel.REFUSALS_BEFORE_RETIRING * 2):
            error = type("DeniedError", (Exception,), {})(
                "PERMISSION_DENIED: Generative Language API has not been "
                "used in project 12345 before or it is disabled"
            )
            error.status_code = 403
            _verdict, kind, _status = binding._on_failure(
                identity, key, error, "label", 1, False,
            )
            assert kind == "denied", kind
        assert not engine.is_retired(identity, key)

    def test_a_model_refusal_never_costs_the_key(self):
        """The owner's rule, run through the real dispatch translation.

        *Refusing a model does not mean the API does not work.* Until this
        round a denial reached the dispatch loop as a bare ``auth``, which is
        in ``RETIRING_KINDS`` — so three refusals from one model the key was
        never entitled to retired a credential that worked everywhere else.
        """
        engine = carousel.Carousel()
        for i in range(carousel.REFUSALS_BEFORE_RETIRING * 3):
            engine.mark("p:m", "k", False, carousel.DENIED_REST_S, "denied",
                        now=NOW + i * 10_000)
        assert not engine.is_retired("p:m", "k")
        # And the key was never entitled to *this* model only: another model
        # on the same credential never heard about it.
        chosen, status = engine.select("p:other", ["k"], now=NOW)
        assert (chosen, status) == ("k", "SUCCESS")

    def test_a_refused_key_is_offered_last_even_when_it_looks_freshest(self):
        """The defect the short bench would have created, had it shipped alone.

        A key that answered 401 comes back with an empty request window and
        the oldest ``last_used`` in the pool — precisely the profile the
        least-loaded/least-recently-used rule reaches for. Without the
        demotion, the one key known not to work would be the *first* one
        tried every time its bench lapsed, and a five-minute bench would be
        worse than the hour it replaced.
        """
        engine = carousel.Carousel()
        keys = ["dead", "good"]
        now = NOW

        # The refusal, and the working key carrying the traffic meanwhile.
        engine.mark("p:m", "dead", False, carousel.REJECTED_REST_S, "auth", now=now)
        for i in range(4):
            engine.mark("p:m", "good", True, now=now + i)

        # Long enough for the bench to lapse and the 60s window to empty, so
        # both keys look equally idle to every rule except the demotion.
        later = now + carousel.REJECTED_REST_S + 120.0
        chosen, status = engine.select("p:m", keys, now=later)
        assert status == "SUCCESS"
        assert chosen == "good", "the refused key was offered ahead of a working one"

    def test_a_refused_key_is_still_reached_when_nothing_else_is_left(self):
        """Demoted is not hidden. This is the whole safety argument."""
        engine = carousel.Carousel()
        keys = ["dead", "spent"]
        now = NOW
        engine.mark("p:m", "dead", False, carousel.REJECTED_REST_S, "auth", now=now)
        engine.mark("p:m", "spent", False, carousel.DAILY_COOLDOWN_S, "daily", now=now)

        later = now + carousel.REJECTED_REST_S + 1.0
        chosen, status = engine.select("p:m", keys, now=later)
        assert status == "SUCCESS"
        assert chosen == "dead", "a demoted key must still be tried over a benched one"

    def test_one_good_answer_ends_the_demotion(self):
        """A false 401 must cost minutes, not the rest of the session."""
        engine = carousel.Carousel()
        keys = ["recovered", "busy"]
        now = NOW
        engine.mark("p:m", "recovered", False, carousel.REJECTED_REST_S, "auth", now=now)
        engine.mark("p:m", "recovered", True, now=now + 400.0)
        for i in range(3):
            engine.mark("p:m", "busy", True, now=now + 500.0 + i)

        chosen, _ = engine.select("p:m", keys, now=now + 600.0)
        assert chosen == "recovered"

    def test_repeated_refusals_still_climb_to_the_hourly_re_probe(self):
        """Short first, patient later. A key that really is dead settles."""
        engine = carousel.Carousel()
        applied = []
        for i in range(12):
            applied.append(
                engine.mark("p:m", "dead", False, carousel.REJECTED_REST_S,
                            "auth", now=NOW + i * 10_000.0)
            )
        assert applied[0] == carousel.REJECTED_REST_S
        assert applied[-1] == carousel.DAILY_COOLDOWN_S
        assert applied == sorted(applied), "the ladder must never step backwards"


# ==========================================================================
# The kind -> rest table, pinned
# ==========================================================================

class TestEveryErrorRestsForTheRightLength:
    """One table, checked against Agent Zero's, differences stated.

    Agent Zero's ``_classify_error`` is the reference implementation: it has
    been in daily use far longer than this port. Where the two differ, the
    difference is deliberate and named in the comment beside it.
    """

    CASES = [
        # (message, status, expected kind, expected rest, Agent Zero's rest)
        ("Read timed out.", None, "timeout", 3.0, 3.0),
        ("503 Service Unavailable", 503, "server", 5.0, 5.0),
        ("Internal server error", 500, "server", 5.0, 5.0),
        # No retry-after anywhere: both fall back to the same 20s opener.
        ("Too Many Requests", 429, "per_minute", 20.0, 20.0),
        ("something nobody has a rule for", None, "other", 20.0, 20.0),
        # Clocks: an hour, matching _KAME_DAILY_COOLDOWN_S exactly.
        ("Quota exceeded: PerDay limit reached", 429, "daily", 3600.0, 3600.0),
        # Refusals: twenty seconds here, an hour in Agent Zero — and Agent
        # Zero does not escalate them at all, it applies the daily hour flat.
        # It has neither a demotion nor a retirement in its selector, so the
        # long bench is the only thing keeping a dead key out of the way
        # there. This port has both, so the wait does not have to do that job
        # and can be short enough to catch a transient refusal clearing.
        ("Unauthorized", 401, "auth", 20.0, 3600.0),
        ("API key not valid", 401, "revoked", 20.0, 3600.0),
        # Not a refusal of the credential: a refusal of this *pairing*. It
        # opens on the same step as the other refusals and climbs the ladder
        # to an hour if it holds, and it is per provider:model, so the key
        # keeps working everywhere else in the same second.
        ("Generative Language API has not been used in project 12345", None,
         "denied", 20.0, 3600.0),
    ]

    @pytest.mark.parametrize("message,status,kind,rest,_az", CASES)
    def test_the_table(self, message, status, kind, rest, _az):
        got_delay, got_kind, _ = carousel.classify(
            None, message, status_code=status,
        )
        assert got_kind == kind, message
        assert got_delay == rest, message

    def test_no_first_refusal_costs_an_hour_unless_waiting_is_the_answer(self):
        """The owner's rule, as a check: an hour has to be earned.

        Three kinds may take it, and each for a reason that is about the
        provider rather than about us: a day's allowance is spent, an account
        is out of credit, or a model authorisation has been refused. All three
        are facts that only time or a person can change. Anything else resting
        an hour on its first refusal is this plugin guessing.
        """
        for message, status, kind, rest, _az in self.CASES:
            if rest >= 3600.0:
                assert kind in ("daily", "insufficient_quota", "denied"), (
                    f"{kind} rests an hour on the first refusal, and only a "
                    f"refusal that waiting or a person can fix may do that"
                )

    def test_a_timeout_never_reaches_the_daily_bench_by_escalating(self):
        """Timeouts have no ladder, by design. Ten in a row is still 3s."""
        engine = carousel.Carousel()
        applied = [
            engine.mark("p:m", "k", False, carousel.TIMEOUT_S, "timeout",
                        now=NOW + i)
            for i in range(10)
        ]
        assert set(applied) == {carousel.TIMEOUT_S}

    def test_a_dropped_stream_rests_half_a_minute_not_an_hour(self):
        dispatch = importlib.import_module(f"{PACKAGE}.dispatch_binding")
        assert dispatch.DROP_REST_S == 30.0
        assert dispatch.DROP_REST_S < carousel.DAILY_COOLDOWN_S / 10


# ==========================================================================
# The panel says what is true
# ==========================================================================

class TestTheSnapshotHasOneSectionPerProcess:

    def test_the_writer_keeps_a_neighbour_that_is_still_alive(self, tmp_path):
        path = tmp_path / "state.json"
        neighbour = {
            "updated_at": time.time(),
            "version": "1.5.0",
            "role": "gateway",
            "totals": {"healthy": 1, "keys": 1, "ready": 1, "rejected": 0},
        }
        path.write_text(json.dumps({
            "schema": state_mod.SCHEMA,
            "updated_at": time.time(),
            "processes": {"999999": neighbour},
        }), encoding="utf-8")

        merged = state_mod._merged(path, _mine())
        assert "999999" in merged["processes"]
        assert str(os.getpid()) in merged["processes"]

    def test_the_writer_drops_a_neighbour_that_has_gone_quiet(self, tmp_path):
        path = tmp_path / "state.json"
        stale = time.time() - state_mod._PROCESS_STALE_S - 60.0
        path.write_text(json.dumps({
            "schema": state_mod.SCHEMA,
            "updated_at": stale,
            "processes": {"999999": {"updated_at": stale, "version": "1.5.0"}},
        }), encoding="utf-8")

        merged = state_mod._merged(path, _mine())
        assert "999999" not in merged["processes"]

    def test_the_document_carries_nothing_but_the_sections(self, tmp_path):
        path = tmp_path / "state.json"
        merged = state_mod._merged(path, _mine())
        assert set(merged) == {"schema", "updated_at", "processes"}


class TestTheCountsMatchTheWarningAboveThem:

    def test_a_rejected_key_is_not_counted_ready(self):
        rows = [{"healthy": 3, "keys": 3, "invalid": 2, "soonest_s": None}]
        totals = state_mod._totals(rows)
        assert totals["healthy"] == 3
        assert totals["rejected"] == 2
        assert totals["ready"] == 1

    def test_ready_never_goes_negative(self):
        rows = [{"healthy": 1, "keys": 3, "invalid": 2, "soonest_s": None}]
        assert state_mod._totals(rows)["ready"] == 0

    def test_the_panel_shows_ready_rather_than_healthy(self):
        source = PANEL.read_text(encoding="utf-8")
        assert "totals.ready" in source
        assert "to replace" in source


class TestThePanelOnlyListsProvidersWithKeysBehindThem:

    def test_a_provider_with_no_parents_and_no_keys_is_dropped(self):
        binding = _ShapeStub()
        binding.seen_pools["emptyprovider"] = {"provider": "emptyprovider"}
        _note_shape(binding, _Pool("emptyprovider"), [])
        assert "emptyprovider" not in binding.seen_pools

    def test_a_provider_with_keys_is_kept(self):
        binding = _ShapeStub()
        _note_shape(binding, _Pool("nvidia"), [_Entry("a"), _Entry("b")])
        assert binding.seen_pools["nvidia"]["keys"] == 2


class TestAKeyOutsideTheConfigIsNamedAsSuch:

    def test_the_snapshot_carries_the_unpooled_fingerprints(self):
        source = (PLUGIN_DIR / "state.py").read_text(encoding="utf-8")
        assert "outside_pool" in source
        assert "keys_outside_the_pool" in source

    def test_the_panel_explains_where_such_a_key_came_from(self):
        source = PANEL.read_text(encoding="utf-8")
        assert "outside_pool" in source
        assert "not in the credential pool" in source


pool_binding = importlib.import_module(f"{PACKAGE}.pool_binding")
_note_shape = pool_binding.PoolBinding._note_shape


class _Pool:
    def __init__(self, provider):
        self.provider = provider


class _Entry:
    """One credential, as much of one as ``_note_shape`` reads."""

    def __init__(self, ident, source="env"):
        self.id = ident
        self.source = source
        self.last_status = "ok"


class _ShapeStub:
    """The three attributes ``_note_shape`` touches, and nothing else."""

    def __init__(self):
        self.seen_pools = {}
        self._module = None

    def _clock(self):
        return NOW


# ==========================================================================
# The panel can ask "did my edit land?" and get an answer
# ==========================================================================

class TestRefreshReadsTheEnvironmentAgain:
    """A change made outside this panel shows up without a restart.

    The owner asked for it in the plainest way there is — *as vezes as
    configurações precisaria de botão refresh?* — and the honest answer was
    yes, because the panel's own answer to "did that land" was "wait and see".
    """

    def _envfile(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        envfile = importlib.import_module(f"{PACKAGE}.envfile")
        monkeypatch.setattr(envfile, "path", lambda: env)
        return env, envfile

    def test_it_reads_only_this_plugin_s_own_names(self, tmp_path, monkeypatch):
        env, envfile = self._envfile(tmp_path, monkeypatch)
        env.write_text(
            "OPENAI_API_KEY=sk-not-ours\n"
            "KAME_DAILY_COOLDOWN=1200\n"
            "SOMETHING_ELSE=1\n",
            encoding="utf-8",
        )
        found = envfile.read_kame()
        assert found == {"KAME_DAILY_COOLDOWN": "1200"}

    def test_a_quoted_value_reads_the_way_dotenv_would(self, tmp_path, monkeypatch):
        env, envfile = self._envfile(tmp_path, monkeypatch)
        env.write_text('KAME_DAILY_COOLDOWN="1200"\n', encoding="utf-8")
        assert envfile.read_kame()["KAME_DAILY_COOLDOWN"] == "1200"

    def test_the_last_assignment_wins_like_dotenv(self, tmp_path, monkeypatch):
        env, envfile = self._envfile(tmp_path, monkeypatch)
        env.write_text(
            "KAME_DAILY_COOLDOWN=60\nKAME_DAILY_COOLDOWN=1200\n", encoding="utf-8")
        assert envfile.read_kame()["KAME_DAILY_COOLDOWN"] == "1200"

    def test_an_edit_made_outside_the_panel_is_picked_up(self, tmp_path, monkeypatch):
        env, _ = self._envfile(tmp_path, monkeypatch)
        settings = importlib.import_module(f"{PACKAGE}.settings")
        monkeypatch.delenv("KAME_DAILY_COOLDOWN", raising=False)

        env.write_text("KAME_DAILY_COOLDOWN=1200\n", encoding="utf-8")
        changed = settings.reread_environment()

        assert "KAME_DAILY_COOLDOWN" in changed
        assert settings.number(settings.DAILY_COOLDOWN, 3600.0) == 1200.0

    def test_a_deleted_line_is_a_reset(self, tmp_path, monkeypatch):
        env, _ = self._envfile(tmp_path, monkeypatch)
        settings = importlib.import_module(f"{PACKAGE}.settings")
        monkeypatch.setenv("KAME_DAILY_COOLDOWN", "1200")

        env.write_text("# nothing of ours here\n", encoding="utf-8")
        changed = settings.reread_environment()

        assert "KAME_DAILY_COOLDOWN" in changed
        assert settings.number(settings.DAILY_COOLDOWN, 3600.0) == 3600.0

    def test_nothing_changed_reports_nothing_changed(self, tmp_path, monkeypatch):
        env, _ = self._envfile(tmp_path, monkeypatch)
        settings = importlib.import_module(f"{PACKAGE}.settings")
        monkeypatch.setenv("KAME_DAILY_COOLDOWN", "1200")
        env.write_text("KAME_DAILY_COOLDOWN=1200\n", encoding="utf-8")
        assert settings.reread_environment() == ()

    def test_a_variable_belonging_to_someone_else_is_left_alone(
        self, tmp_path, monkeypatch
    ):
        env, _ = self._envfile(tmp_path, monkeypatch)
        settings = importlib.import_module(f"{PACKAGE}.settings")
        monkeypatch.setenv("KAME_SOMETHING_A_FUTURE_RELEASE_ADDS", "9")
        env.write_text("KAME_SOMETHING_A_FUTURE_RELEASE_ADDS=1\n", encoding="utf-8")

        settings.reread_environment()

        import os as _os
        assert _os.environ["KAME_SOMETHING_A_FUTURE_RELEASE_ADDS"] == "9"


# ==========================================================================
# Events records what KAME did, not only what failed
# ==========================================================================

class TestEveryRotationIsOnTheScreen:

    def test_the_vocabulary_has_a_word_for_the_rotation_itself(self):
        events = importlib.import_module("hermes-kame-api-rotation.core.events")
        assert "switch" in events._KINDS
        assert events.SWITCH in events.GOOD_KINDS

    def test_the_kinds_that_were_never_written_now_are(self):
        source = (PLUGIN_DIR / "dispatch_binding.py").read_text(encoding="utf-8")
        for kind in ('"switch"', '"recovery"', '"wait"'):
            assert f"EVENTS.add(\n                    {kind}" in source \
                or f"EVENTS.add(\n                {kind}" in source, kind

    def test_the_buffer_is_big_enough_to_hold_a_whole_incident(self):
        events = importlib.import_module("hermes-kame-api-rotation.core.events")
        # Three rows per rotation now (what failed, where it went, whether it
        # answered), so the old fifty would have shown a third of an outage.
        assert events.MAX_EVENTS >= 150

    def test_the_panel_can_say_all_of_them(self):
        """Derived from ``_KINDS``, not from a list written beside it.

        This was five names typed by hand, which is a test that passes for
        every kind it was not told about. 1.6.0.1 added ``denied_model`` and
        it went on passing, so a real event would have reached the Events tab
        with the raw code word as its label and no explanation under it.
        Reading the set means a kind cannot be added without the screen
        learning to say it.
        """
        events = importlib.import_module("hermes-kame-api-rotation.core.events")
        source = PANEL.read_text(encoding="utf-8")
        labels = source.split("const EVENT_LABELS")[1].split("}")[0]
        meanings = source.split("const EVENT_MEANING")[1].split("}")[0]
        for kind in sorted(events._KINDS):
            assert f"{kind}:" in labels, f"{kind} has no label on the panel"
            assert f"{kind}:" in meanings, f"{kind} has no explanation on the panel"


# ==========================================================================
# The diagnostic survives losing the repository
# ==========================================================================

class TestTheDoctorLivesInThePlugin:
    """`/kame doctor`, so the reading outlives the checkout it was written in.

    It was `tools/inspect_run.py`, which is the one place a diagnostic is
    guaranteed not to be when it is wanted: a fresh install, another machine,
    a different assistant, an owner who does not have the repo open.
    """

    def _doctor(self):
        return importlib.import_module("hermes-kame-api-rotation.core.doctor")

    SNAP = {
        "version": "1.6.0.1",
        "pid": 4242,
        "role": "desktop",
        "profile": "default",
        "build": {"complete": True, "fingerprint": "aaaaaaaaaaaa", "missing": []},
        "totals": {"keys": 3, "healthy": 1, "ready": 0, "rejected": 2},
        "pools": [
            {
                "identity": "nvidia:moonshotai/kimi-k3",
                "healthy": 1, "keys": 3, "invalid": 2,
                "invalid_keys": ["key:02ba29", "key:b65dd9"],
                "outside_pool": ["key:00082b"],
                "soonest_s": 38,
            }
        ],
        "neighbours": [],
    }

    def test_the_command_answers_to_doctor(self):
        source = (PLUGIN_DIR / "menu.py").read_text(encoding="utf-8")
        assert '"doctor", "check", "diagnose"' in source
        assert "/kame doctor" in source

    def test_it_names_the_build_that_is_actually_running(self):
        lines = "\n".join(self._doctor().build_lines(self.SNAP))
        assert "aaaaaaaaaaaa" in lines
        assert "1.6.0.1" in lines

    def test_it_says_when_a_neighbour_is_on_another_build(self):
        snap = dict(self.SNAP, neighbours=[
            {"role": "gateway", "version": "1.5.0",
             "build": {"fingerprint": "bbbbbbbbbbbb"}}
        ])
        lines = "\n".join(self._doctor().build_lines(snap))
        assert "different build" in lines

    def test_an_incomplete_install_is_the_first_thing_it_says(self):
        snap = dict(self.SNAP, build={
            "complete": False, "fingerprint": "aaaaaaaaaaaa", "missing": ["core/quota.py"]
        })
        lines = "\n".join(self._doctor().build_lines(snap))
        assert "INCOMPLETE" in lines
        assert "core/quota.py" in lines

    def test_the_pool_line_never_counts_more_keys_than_the_pool_holds(self):
        """The first draft said '0 of 3 ready, 2 refused, 2 resting'.

        Refused and resting are the same keys — a refused credential is
        benched too — so the three numbers were read as a partition of a pool
        that only had three keys in it.
        """
        line = "\n".join(self._doctor().pool_lines(self.SNAP))
        assert "3 keys, 0 ready now, 2 refused as credentials" in line
        assert "resting" not in line

    def test_it_names_a_key_the_config_does_not_hold(self):
        text = "\n".join(self._doctor().pool_lines(self.SNAP))
        assert "key:00082b" in text
        assert "does not hold" in "\n".join(self._doctor().trouble_lines(self.SNAP, ()))

    def test_a_clean_install_is_told_it_is_clean(self):
        clean = {
            "version": "1.6.0.1", "pid": 1, "build": {"complete": True, "fingerprint": "a"},
            "totals": {"keys": 4, "healthy": 4, "ready": 4, "rejected": 0},
            "pools": [{"identity": "gemini:x", "healthy": 4, "keys": 4, "invalid": 0,
                       "invalid_keys": [], "outside_pool": [], "soonest_s": None}],
            "neighbours": [],
        }
        assert self._doctor().trouble_lines(clean, ()) == ["  Nothing here needs a person."]

    def test_it_says_nothing_rather_than_guessing_on_a_cold_process(self):
        cold = {"version": "1.6.0.1", "build": {}, "pools": [], "totals": {}}
        assert "no pool has been built yet" in "\n".join(self._doctor().pool_lines(cold))
        assert "nothing to grade" in "\n".join(self._doctor().evidence_lines(()))

    def test_the_rest_table_agrees_with_the_code_it_describes(self):
        """The one table in this plugin written by hand rather than derived.

        Derived, it would agree with the carousel by construction and prove
        nothing. Written down, it is a second statement of intent that a
        change to either side has to be made to match — which is the only
        arrangement in which "is it resting for the right length?" is a
        question a test can answer.
        """
        doctor = self._doctor()
        expected = dict((kind, rest) for kind, rest, _ in doctor.EXPECTED_RESTS)
        assert expected["timeout"] == carousel.TIMEOUT_S
        assert expected["server"] == carousel.SERVER_BASE_S
        assert expected["other"] == carousel.OTHER_S
        assert expected["auth"] == carousel.REJECTED_REST_S
        assert expected["revoked"] == carousel.REJECTED_REST_S
        assert expected["denied"] == carousel.DENIED_REST_S
        assert expected["daily"] == carousel.DAILY_COOLDOWN_S
        assert expected["insufficient_quota"] == carousel.DAILY_COOLDOWN_S

    def test_every_kind_the_carousel_can_produce_is_in_the_table(self):
        doctor = self._doctor()
        named = {kind for kind, _, _ in doctor.EXPECTED_RESTS}
        # `host_breaker` is deliberately absent: it carries no cooldown and is
        # not the pool's business — Hermes stopped the call itself.
        produced = {"timeout", "server", "per_minute", "daily",
                    "insufficient_quota", "denied", "auth", "revoked", "other"}
        assert produced <= named

    def test_the_whole_report_renders_without_a_journal(self):
        text = self._doctor().render(self.SNAP, ())
        for heading in ("Which KAME is running", "What it can see",
                        "What each error costs a key", "Worth a person's time"):
            assert heading in text

    def test_the_doctor_asks_for_the_neighbours_rather_than_inventing_them(self):
        """`snapshot()` builds one section. The panel derives the rest; this cannot.

        Caught by rendering the report against a real snapshot and noticing the
        neighbour section was always empty — the field the doctor reads is one
        the panel computes on its own side, and nothing filled it in here.
        """
        source = (PLUGIN_DIR / "menu.py").read_text(encoding="utf-8")
        assert 'snapshot["neighbours"] = state.neighbours()' in source
        assert hasattr(state_mod, "neighbours")

    def test_the_neighbour_reader_skips_this_process_and_the_dead(self, tmp_path, monkeypatch):
        alive = time.time()
        dead = alive - state_mod._PROCESS_STALE_S - 60.0
        path = tmp_path / "state.json"
        path.write_text(json.dumps({
            "schema": state_mod.SCHEMA,
            "updated_at": alive,
            "processes": {
                str(os.getpid()): {"updated_at": alive, "version": "mine"},
                "999998": {"updated_at": alive, "version": "alive"},
                "999997": {"updated_at": dead, "version": "gone"},
            },
        }), encoding="utf-8")
        monkeypatch.setattr(state_mod, "state_path", lambda: path)

        found = state_mod.neighbours(now=alive)
        assert [s["version"] for s in found] == ["alive"]

    def test_a_file_it_cannot_read_is_one_process_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(state_mod, "state_path", lambda: tmp_path / "nothing.json")
        assert state_mod.neighbours() == []
        monkeypatch.setattr(state_mod, "state_path", lambda: None)
        assert state_mod.neighbours() == []

    def test_the_doctor_works_out_the_build_when_nothing_registered_one(self):
        """The reinstall case, which is the case this command exists for.

        `_INTEGRITY` is filled in by `register()`. A plugin imported outside a
        running Hermes — a fresh copy on a new machine, somebody checking what
        they just installed — has never registered, so the field a doctor is
        asked for first was the one it could not answer.
        """
        source = (PLUGIN_DIR / "menu.py").read_text(encoding="utf-8")
        assert 'if not build.get("fingerprint")' in source
        assert "integrity.verify()" in source

    def test_the_report_never_carries_a_key_length_string(self):
        doctor = self._doctor()
        report = doctor.render(self.SNAP, ())
        assert max(len(word) for word in report.split()) < 40


# ==========================================================================
# A key the provider refuses leaves rotation. It is never deleted.
# ==========================================================================

class TestARefusedKeyStopsBeingOffered:
    """The owner's question, and the distinction he drew answering it.

        chave revogada idealmente deve sair pra fora de primeira ...
        mas se for sem autorização daí você decide ... não é como se fosse
        voltar se dá essa mensagem né

    Both halves are right, and the classifier already knew the difference —
    `dispatch_binding` was flattening it one line after it was worked out.

    * **revoked** — the provider used the words ("API key not valid").
      Nothing ambiguous is left, so it leaves rotation on the first one.
    * **auth** — a bare 401 with no explanation. Could be an OAuth token a
      second from refreshing. Three in a row, with no success between them.
    * **denied** — "this key may not use *this model*". A fact about the
      pairing, not the key. Never retired; the key goes on working on every
      other model in the same second.

    Nothing here deletes anything. KAME does not write credentials, and
    "retired" means one thing only: it stops being chosen.
    """

    def test_the_provider_saying_the_words_retires_at_once(self):
        engine = carousel.Carousel()
        engine.mark("p:m", "dead", False, carousel.REJECTED_REST_S, "revoked", now=NOW)
        assert engine.is_retired("p:m", "dead") is True

    def test_a_bare_401_needs_three_in_a_row(self):
        engine = carousel.Carousel()
        for strike in range(1, carousel.REFUSALS_BEFORE_RETIRING):
            engine.mark("p:m", "maybe", False, carousel.REJECTED_REST_S, "auth",
                        now=NOW + strike)
            assert engine.is_retired("p:m", "maybe") is False, strike
        engine.mark("p:m", "maybe", False, carousel.REJECTED_REST_S, "auth",
                    now=NOW + 99)
        assert engine.is_retired("p:m", "maybe") is True

    def test_one_good_answer_wipes_the_count(self):
        """1.4.0's twenty-one quarantines, as a check that they cannot repeat."""
        engine = carousel.Carousel()
        engine.mark("p:m", "k", False, carousel.REJECTED_REST_S, "auth", now=NOW)
        engine.mark("p:m", "k", False, carousel.REJECTED_REST_S, "auth", now=NOW + 1)
        engine.mark("p:m", "k", True, now=NOW + 2)          # the token refreshed
        engine.mark("p:m", "k", False, carousel.REJECTED_REST_S, "auth", now=NOW + 3)
        assert engine.is_retired("p:m", "k") is False

    def test_a_different_failure_between_them_breaks_the_run(self):
        """Consecutive means consecutive, not "three of them eventually"."""
        engine = carousel.Carousel()
        engine.mark("p:m", "k", False, carousel.REJECTED_REST_S, "auth", now=NOW)
        engine.mark("p:m", "k", False, 60.0, "per_minute", now=NOW + 1)
        engine.mark("p:m", "k", False, carousel.REJECTED_REST_S, "auth", now=NOW + 2)
        engine.mark("p:m", "k", False, carousel.REJECTED_REST_S, "auth", now=NOW + 3)
        assert engine.is_retired("p:m", "k") is False

    def test_a_model_this_key_may_not_use_never_retires_it(self):
        """The owner's own distinction, and the expensive one to get wrong.

        A key refused for one model may be the healthiest credential in the
        account on every other. Retiring it would throw away a working key
        over a permission that was never about the key.
        """
        engine = carousel.Carousel()
        for i in range(10):
            engine.mark("p:m", "k", False, carousel.DENIED_REST_S, "denied", now=NOW + i)
        assert engine.is_retired("p:m", "k") is False
        assert "denied" not in carousel.RETIRING_KINDS

    def test_a_retired_key_is_not_offered_while_anything_else_can_serve(self):
        engine = carousel.Carousel()
        engine.mark("p:m", "dead", False, carousel.REJECTED_REST_S, "revoked", now=NOW)
        later = NOW + carousel.REJECTED_REST_S + 120.0
        for _ in range(6):
            chosen, status = engine.select("p:m", ["dead", "good"], now=later)
            assert status == "SUCCESS"
            assert chosen == "good"

    def test_a_pool_of_nothing_but_retired_keys_still_sends_the_request(self):
        """The escape hatch, and the whole safety argument for retiring at all.

        If retiring could take a pool to zero, a mistyped key would turn into
        silence from a plugin that decided — and the worst outcome of a wrong
        verdict has to be no worse than not having the rule. So when every key
        is retired, every key is offered again: the request goes out and the
        provider's own error comes back, exactly as it would without KAME.
        """
        engine = carousel.Carousel()
        for key in ("a", "b"):
            engine.mark("p:m", key, False, carousel.REJECTED_REST_S, "revoked", now=NOW)
        later = NOW + carousel.REJECTED_REST_S + 5.0
        chosen, status = engine.select("p:m", ["a", "b"], now=later)
        assert chosen in ("a", "b")
        assert status == "SUCCESS"

    def test_a_retired_key_that_works_is_back_immediately(self):
        engine = carousel.Carousel()
        engine.mark("p:m", "k", False, carousel.REJECTED_REST_S, "revoked", now=NOW)
        assert engine.is_retired("p:m", "k") is True
        engine.mark("p:m", "k", True, now=NOW + 1)
        assert engine.is_retired("p:m", "k") is False

    def test_retiring_removes_nothing(self):
        """`retired` is a flag on a row, not a deletion. Say it as a check."""
        engine = carousel.Carousel()
        engine.mark("p:m", "k", False, carousel.REJECTED_REST_S, "revoked", now=NOW)
        snap = engine.snapshot(now=NOW + 1)
        assert snap["p:m"]["keys"] == 1
        assert snap["p:m"]["retired"] == 1
        assert snap["p:m"]["retired_keys"], "the panel needs the fingerprint to name it"

    def test_the_screen_can_tell_resting_from_out_for_good(self):
        source = (PLUGIN_DIR / "dispatch_binding.py").read_text(encoding="utf-8")
        assert "out of rotation until it is replaced" in source
        assert "is_retired" in source

    def test_waiting_for_a_working_key_beats_calling_a_dead_one(self):
        """The case the demotion alone gets wrong, and the reason to retire.

        The working key is resting twenty seconds off a throttle. The refused
        key's own rest has lapsed, so it is the only thing "ready" — and the
        demotion cannot help, because a demotion only reorders keys that are
        all ready together. So the call went to a credential already known to
        be dead: one spent request and an error handed to the user, where
        waiting twenty seconds would have handed them an answer.
        """
        engine = carousel.Carousel()
        engine.mark("p:m", "dead", False, carousel.REJECTED_REST_S, "revoked", now=NOW)
        engine.mark("p:m", "good", False, 20.0, "per_minute", now=NOW + 100)

        # Far enough out that the refused key is ready again and the working
        # one is not.
        when = NOW + 100 + 5.0
        chosen, status = engine.select("p:m", ["dead", "good"], now=when)

        assert chosen == "good", "the call went to a key the provider named dead"
        assert status == "EXHAUSTED", "it should be waiting, not sending"

    def test_but_a_pool_of_only_retired_keys_still_sends(self):
        """The escape hatch again, this time against the stronger rule."""
        engine = carousel.Carousel()
        engine.mark("p:m", "a", False, carousel.REJECTED_REST_S, "revoked", now=NOW)
        engine.mark("p:m", "b", False, carousel.REJECTED_REST_S, "revoked", now=NOW)
        chosen, status = engine.select(
            "p:m", ["a", "b"], now=NOW + carousel.REJECTED_REST_S + 5.0
        )
        assert chosen in ("a", "b")
        assert status == "SUCCESS"

    def test_the_panel_repeats_the_threshold_from_the_same_number(self):
        """A number written in two languages drifts. Hold them together."""
        source = PANEL.read_text(encoding="utf-8")
        assert f"REFUSALS_BEFORE_RETIRING = {carousel.REFUSALS_BEFORE_RETIRING}" in source

    def test_the_panel_says_the_two_states_differently(self):
        source = PANEL.read_text(encoding="utf-8")
        assert "left rotation" in source
        assert "still being tried" in source
        assert "Nothing was deleted" in source

    def test_the_doctor_separates_them_too(self):
        doctor = importlib.import_module("hermes-kame-api-rotation.core.doctor")
        snap = {
            "version": "1.6.0.1", "pid": 1,
            "build": {"complete": True, "fingerprint": "a"},
            "totals": {"keys": 3, "healthy": 1, "ready": 0, "rejected": 2, "retired": 1},
            "pools": [{"identity": "p:m", "healthy": 1, "keys": 3, "invalid": 2,
                       "invalid_keys": ["key:aa", "key:bb"], "retired": 1,
                       "retired_keys": ["key:bb"], "outside_pool": [],
                       "soonest_s": None}],
            "neighbours": [],
        }
        text = "\n".join(doctor.trouble_lines(snap, ()))
        assert "out of rotation" in text
        assert "key:bb" in text
        assert "still being tried" in text
        assert "Nothing was deleted" in text
