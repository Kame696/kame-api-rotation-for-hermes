"""1.8.2.4: the speed features, driven through the real transport path.

The 1.8.2.3 tests called each piece alone — ``should_hedge`` with a prompt
size the test chose, the pacer with a label the test chose — and every piece
passed. The defects lived in the seams: the transport waited for the slow
leg, passed the size of a dict, looked groups up by the raw key. These tests
go through ``KameTransport.complete`` / the pacer's own settings sync, so a
seam that breaks fails here.

Hedge tests use real time (the race runs on real threads) and stay under a
couple of seconds each.

Run:  uv run --with pytest python -m pytest tests/test_1_8_2_4_speed_paths.py -q
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_speed_1824_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
transport = importlib.import_module(f"{PACKAGE}.transport")
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
hedging_mod = importlib.import_module(f"{PACKAGE}.core.hedging")
pacing_mod = importlib.import_module(f"{PACKAGE}.core.pacing")
latency_mod = importlib.import_module(f"{PACKAGE}.core.latency")
region_mod = importlib.import_module(f"{PACKAGE}.core.region")
settings = importlib.import_module(f"{PACKAGE}.settings")

KEYS = [f"AIzaSyKEY{i}" + "0" * 29 for i in range(3)]
IDENTITY = "gemini:gemini-test"
REQUEST = {"model": "gemini-test", "messages": [{"role": "user", "content": "hi"}]}
THRESHOLD = 0.2


class Refusal(Exception):
    def __init__(self, status: int, message: str, details=()):
        super().__init__(message)
        self.status_code = status
        self.body = {"error": {"code": status, "message": message,
                               "status": "RESOURCE_EXHAUSTED" if status == 429 else "UNAVAILABLE",
                               "details": list(details)}}


def per_minute(retry_s: int = 30) -> Refusal:
    return Refusal(429, "Quota exceeded for metric: generate_content_free_tier_requests", [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{retry_s}s"},
    ])


def answer(text: str):
    message = SimpleNamespace(role="assistant", content=text, tool_calls=None)
    return SimpleNamespace(id="c", model="m", usage=None,
                           choices=[SimpleNamespace(index=0, message=message, finish_reason="stop")])


class Plan:
    """Per key: ``(seconds to take, answer text or exception)``, one per request."""

    def __init__(self, plan):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.asked = []

    def run(self, key):
        self.asked.append(key)
        delay, outcome = self.plan[key].pop(0)
        time.sleep(delay)
        if isinstance(outcome, BaseException):
            raise outcome
        return answer(outcome)


class FakeCall(transport.Call):
    def __init__(self, plan, keys=KEYS, kwargs=REQUEST):
        self.plan = plan
        self._keys = list(keys)
        self.kwargs = dict(kwargs)
        self.provider = "gemini"
        self.identity = IDENTITY

    def keys(self):
        return list(self._keys)

    def entry(self, key):
        return None

    def entry_id(self, key):
        return ""

    def client_for(self, key, attempt):
        def create(**request):
            return self.plan.run(key)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def cancelled(self):
        return False


def text_of(result) -> str:
    return result.choices[0].message.content


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(__import__("os").environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    settings.forget()
    hedging_mod.HEDGE_TRACKER.clear()
    pacing_mod.PACER.clear()
    pacing_mod.PACER._synced_at.clear()
    monkeypatch.setattr(hedging_mod.HEDGE_TRACKER, "threshold", lambda *a, **k: THRESHOLD)
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    yield t
    settings.forget()
    hedging_mod.HEDGE_TRACKER.clear()
    pacing_mod.PACER.clear()


def _settle(t, seconds=1.2):
    """Let the losing leg finish on its thread, then account for it."""
    time.sleep(seconds)
    t._drain_hedge_losers()


class TestTheRaceReturnsAtTheFirstAnswer:
    def test_the_backup_answer_comes_back_without_waiting_for_the_slow_leg(self, fresh):
        plan = Plan({KEYS[0]: [(3.0, "slow")], KEYS[1]: [(0.05, "backup")], KEYS[2]: [(0.05, "backup")]})
        started = time.monotonic()
        result = fresh.complete(FakeCall(plan))
        elapsed = time.monotonic() - started
        assert text_of(result) == "backup"
        # 1.8.2.3 returned after ~3.0 s here (it joined the slow leg).
        assert elapsed < 1.5, elapsed

    def test_an_empty_answer_does_not_win_while_the_other_leg_is_working(self, fresh):
        # Seen live on the owner's Gemini pool: a thinking model answered one
        # leg with nothing while the other was still working.
        plan = Plan({KEYS[0]: [(0.4, "")], KEYS[1]: [(0.8, "useful")], KEYS[2]: [(0.8, "useful")]})
        result = fresh.complete(FakeCall(plan))
        assert text_of(result) == "useful"
        assert len(plan.asked) == 2  # no third attempt was needed

    def test_two_empty_legs_fall_back_to_the_empty_answer_rule(self, fresh):
        class TwoEmpty(Plan):
            def __init__(self):
                super().__init__({})

            def run(self, key):
                self.asked.append(key)
                if len(self.asked) <= 2:
                    time.sleep(0.4 if key == KEYS[0] else 0.0)
                    return answer("")
                return answer("third")

        plan = TwoEmpty()
        result = fresh.complete(FakeCall(plan))
        assert text_of(result) == "third"
        assert len(plan.asked) == 3

    def test_a_fast_primary_never_sends_a_backup(self, fresh):
        plan = Plan({KEYS[0]: [(0.01, "first")], KEYS[1]: [], KEYS[2]: []})
        result = fresh.complete(FakeCall(plan))
        assert text_of(result) == "first"
        assert plan.asked == [KEYS[0]]


class TestEveryLegIsBlamedOnItsOwnKey:
    def test_a_slow_primary_that_fails_after_losing_rests_its_own_key(self, fresh):
        plan = Plan({KEYS[0]: [(0.6, per_minute())], KEYS[1]: [(0.05, "backup")], KEYS[2]: [(0.05, "backup")]})
        result = fresh.complete(FakeCall(plan))
        assert text_of(result) == "backup"
        _settle(fresh)
        assert fresh.engine.healthy_count(IDENTITY, [KEYS[0]]) == 0
        backup = [k for k in plan.asked if k != KEYS[0]][0]
        assert fresh.engine.healthy_count(IDENTITY, [backup]) == 1

    def test_a_backup_refused_per_minute_rests_the_backup_and_cools_hedging(self, fresh):
        plan = Plan({KEYS[0]: [(0.6, "primary")], KEYS[1]: [(0.0, per_minute())], KEYS[2]: [(0.0, per_minute())]})
        result = fresh.complete(FakeCall(plan))
        assert text_of(result) == "primary"
        backup = [k for k in plan.asked if k != KEYS[0]][0]
        assert fresh.engine.healthy_count(IDENTITY, [backup]) == 0
        assert fresh.engine.healthy_count(IDENTITY, [KEYS[0]]) == 1
        assert hedging_mod.HEDGE_TRACKER._cool_until.get(IDENTITY, 0) > time.monotonic()

    def test_when_both_legs_fail_each_key_rests_and_the_third_answers(self, fresh):
        class BothFail(Plan):
            """The primary refuses late; whichever key is the backup refuses;
            the key left over answers the next attempt."""

            def __init__(self):
                super().__init__({})
                self.backup_refused = False

            def run(self, key):
                self.asked.append(key)
                if key == KEYS[0]:
                    time.sleep(0.4)
                    raise per_minute()
                if not self.backup_refused:
                    self.backup_refused = True
                    raise per_minute()
                return answer("third")

        plan = BothFail()
        result = fresh.complete(FakeCall(plan))
        assert text_of(result) == "third"
        rested = [k for k in KEYS if fresh.engine.healthy_count(IDENTITY, [k]) == 0]
        assert KEYS[0] in rested and len(rested) == 2


class TestThePromptSizeGuardSeesThePrompt:
    def test_the_estimate_counts_text_not_keyword_arguments(self):
        big = {"model": "m", "messages": [{"role": "user", "content": "x" * 40_000}]}
        # 40 000 characters of prompt plus the role word: about 10 000 tokens.
        assert 10_000 <= transport._estimate_prompt_tokens(big) <= 10_005
        assert transport._estimate_prompt_tokens({"model": "m", "messages": []}) == 0

    def test_a_huge_prompt_is_never_doubled(self, fresh, monkeypatch):
        monkeypatch.setenv("KAME_HEDGE_MAX_PROMPT_TOKENS", "1000")
        settings.forget()
        huge = {"model": "gemini-test", "messages": [{"role": "user", "content": "y" * 40_000}]}
        plan = Plan({KEYS[0]: [(0.5, "only")], KEYS[1]: [], KEYS[2]: []})
        result = fresh.complete(FakeCall(plan, kwargs=huge))
        assert text_of(result) == "only"
        assert plan.asked == [KEYS[0]]


class TestQuotaGroupsMatchWhatThePanelShows:
    def _sync(self, monkeypatch, value):
        monkeypatch.setenv("KAME_KEY_QUOTA_GROUPS", value)
        settings.forget()
        pacing_mod.PACER._synced_at.clear()
        pacing_mod.PACER.sync_identity_settings(IDENTITY)

    def test_the_label_is_the_fingerprint_the_events_tab_shows(self):
        for k in KEYS:
            assert carousel_mod.fingerprint(k) in pacing_mod.key_labels(k)

    def test_both_spellings_group_keys_and_deleting_the_entry_ungroups_them(self, fresh, monkeypatch):
        fp0 = carousel_mod.fingerprint(KEYS[0])          # "key:abc123"
        fp1 = carousel_mod.fingerprint(KEYS[1])[4:]      # "abc123"
        self._sync(monkeypatch, f"{fp0}=proj1, {fp1}=proj1")
        assert pacing_mod.PACER.same_scope(KEYS[0], KEYS[1])
        assert not pacing_mod.PACER.same_scope(KEYS[0], KEYS[2])
        self._sync(monkeypatch, "")
        assert not pacing_mod.PACER.same_scope(KEYS[0], KEYS[1])

    def test_a_backup_never_goes_to_the_primarys_group(self, fresh, monkeypatch):
        fp0, fp1 = (carousel_mod.fingerprint(k) for k in KEYS[:2])
        self._sync(monkeypatch, f"{fp0}=proj1, {fp1}=proj1")
        plan = Plan({KEYS[0]: [(0.6, "primary")], KEYS[1]: [(0.0, "same-project")],
                     KEYS[2]: [(0.0, "other-project")]})
        result = fresh.complete(FakeCall(plan))
        assert KEYS[1] not in plan.asked
        assert text_of(result) == "other-project"

    def test_region_override_accepts_both_spellings(self):
        fp = carousel_mod.fingerprint(KEYS[0])
        assert region_mod.region_from_overrides(f"{fp[4:]}=us-east5", fp) == "us-east5"
        assert region_mod.region_from_overrides(f"{fp}=us-east5", fp) == "us-east5"


class TestALearnedLimitCanRecover:
    def test_answers_beyond_the_learned_limit_raise_it(self):
        p = pacing_mod.Pacer()
        now = 1000.0
        for i in range(3):
            p.stamp(IDENTITY, KEYS[0], now=now + i)
        p.on_minute_429(IDENTITY, KEYS[0])
        learned = p._rpm[(IDENTITY, KEYS[0])]
        for i in range(6):
            p.stamp(IDENTITY, KEYS[0], now=now + 10 + i)
        assert p._rpm[(IDENTITY, KEYS[0])] > learned

    def test_never_above_the_owners_override(self):
        p = pacing_mod.Pacer()
        p._identity_override_rpm = {IDENTITY: 4}
        p.note_limit(IDENTITY, KEYS[0], 2)
        for i in range(8):
            p.stamp(IDENTITY, KEYS[0], now=2000.0 + i)
        assert p._rpm[(IDENTITY, KEYS[0])] <= 4


class TestThinkingIsRecognised:
    @pytest.mark.parametrize("model,kwargs,expected", [
        ("gemini-3.8-flash", None, True),
        ("gemini-2.5-flash", None, True),
        ("gemini-2.5-flash-lite", None, False),
        ("gemini-2.0-flash", None, False),
        ("gemma-3-27b-it", None, False),
        ("llama-3.3-70b", {"reasoning_effort": "high"}, True),
        ("llama-3.3-70b", {"reasoning_effort": "none"}, False),
        ("claude-x", {"extra_body": {"thinking": {"type": "enabled", "budget_tokens": 2000}}}, True),
        ("claude-x", {"extra_body": {"thinking": {"type": "disabled"}}}, False),
    ])
    def test_reasoning_model(self, model, kwargs, expected):
        assert hedging_mod.reasoning_model(model, kwargs) is expected


class TestMaxModeFiresFromDispatch:
    def test_max_fires_at_once_until_a_backup_is_refused(self, fresh, monkeypatch):
        monkeypatch.setenv("KAME_HEDGE_MODE", "max")
        settings.forget()
        tracker = hedging_mod.HEDGE_TRACKER
        assert tracker.fires_from_dispatch(IDENTITY)
        tracker.note_hedge_429(IDENTITY)
        assert not tracker.fires_from_dispatch(IDENTITY)

    def test_adaptive_waits(self, fresh):
        assert not hedging_mod.HEDGE_TRACKER.fires_from_dispatch(IDENTITY)


class TestLatencyMeansFirstSignForStreams:
    def test_a_stream_is_timed_to_its_first_sign_not_its_end(self, fresh):
        t = fresh
        started = time.monotonic() - 10.0  # the stream ran for ten seconds
        progress = SimpleNamespace(first_text_at=started + 0.5, first_sign_at=started + 0.2)
        call = FakeCall(Plan({}))
        latency_mod.LATENCY.clear() if hasattr(latency_mod.LATENCY, "clear") else None
        t._learn_latency(call, IDENTITY, KEYS[0], True, started=started, progress=progress)
        assert latency_mod.LATENCY.get(IDENTITY, KEYS[0]) == pytest.approx(0.5, abs=0.05)

    def test_a_stream_with_no_sign_teaches_nothing(self, fresh):
        progress = SimpleNamespace(first_text_at=None, first_sign_at=None)
        before = dict(hedging_mod.HEDGE_TRACKER.snapshot())
        fresh._learn_latency(FakeCall(Plan({})), IDENTITY, KEYS[1], True,
                             started=time.monotonic(), progress=progress)
        assert hedging_mod.HEDGE_TRACKER.snapshot() == before
