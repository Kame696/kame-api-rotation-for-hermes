"""Independent-account inference and measured, provenance-aware escalation."""
import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hermes-kame-api-rotation"))
from core.carousel import Carousel
from core.escalate import stretch
from core.journal import Journal, short_streak


def load_test_helpers(filename, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("count", [1, 2, 14])
def test_independent_keys_do_not_inherit_daily(count):
    engine = Carousel()
    identity = "gemini:model"
    keys = [f"independent-{i}" for i in range(count)]
    engine.select(identity, keys, now=1000)
    assert engine.mark(identity, keys[0], False, 0, "daily", now=1000) == 300
    for key in keys[1:]:
        # 1.8.0.2: 20 -> 30 (not 45 -- that curve double-counted retries).
        # See core.carousel.UNSIZED_THROTTLE_REST_S.
        assert engine.mark(identity, key, False, 0, "rate_limit", now=1000) == 30
    # Same-key evidence survives, but a fresh stated wait is obeyed.
    assert engine.mark(identity, keys[0], False, 0, "rate_limit", now=1300) == 300
    assert engine.mark(identity, keys[0], False, 7, "rate_limit", now=1600, stated=True) == 7


def test_success_clears_own_inference_not_other_account():
    e = Carousel()
    e.mark("p:m", "A", False, 0, "daily", now=1000)
    e.mark("p:m", "B", True, now=1001)
    assert e.mark("p:m", "A", False, 0, "rate_limit", now=1300) == 300
    e.mark("p:m", "A", True, now=1301)
    # 1.8.0.2: 20 -> 30 (not 45 -- that curve double-counted retries).
    # See core.carousel.UNSIZED_THROTTLE_REST_S.
    assert e.mark("p:m", "A", False, 0, "rate_limit", now=1302) == 30


@pytest.mark.parametrize("identity", [None, "p:m"])
def test_reset_discards_named_window_memory(identity):
    e = Carousel()
    e.mark("p:m", "A", False, 0, "daily", now=1000)
    e.mark("other:m", "B", False, 0, "daily", now=1000)
    e.forget(identity)
    # 1.8.0.2: 20 -> 30 (not 45 -- that curve double-counted retries).
    # See core.carousel.UNSIZED_THROTTLE_REST_S.
    assert e.mark("p:m", "A", False, 0, "rate_limit", now=1300) == 30
    expected = 30 if identity is None else 300
    assert e.mark("other:m", "B", False, 0, "rate_limit", now=1300) == expected


def test_removed_key_does_not_keep_an_orphan_daily_memory():
    e = Carousel()
    e.select("p:m", ["A", "B"], now=1000)
    e.mark("p:m", "A", False, 0, "daily", now=1000)
    e.select("p:m", ["B"], now=1301)
    assert ("p:m", "A") not in e._named_window
    # 1.8.0.2: 20 -> 30 (not 45 -- that curve double-counted retries).
    # See core.carousel.UNSIZED_THROTTLE_REST_S.
    assert e.mark("p:m", "A", False, 0, "rate_limit", now=1302) == 30


def test_subscription_reset_reaches_classifier_as_recoverable_wait():
    from core import classify
    verdict = classify(provider="openai-codex", status_code=429,
        error_message="You have hit your usage limit.",
        error_body={"error": {"type": "usage_limit_reached",
                              "resets_at": 1788909203, "resets_in_seconds": 12660}},
        now_epoch=1788896543)
    assert verdict.reason == "rate_limit"
    assert verdict.retryable and verdict.should_rotate_credential
    assert verdict.reset_at == 1788909203
    assert verdict.source == "body.resets_at"


@pytest.mark.parametrize("derived", [False, True])
@pytest.mark.parametrize("raise_on_write", [False, True])
def test_persist_guard_preserves_host_reset_options(derived, raise_on_write):
    import threading
    from types import SimpleNamespace
    helpers = load_test_helpers("test_binding.py", "kame_706_persist_helpers")
    binding = helpers.PoolBinding.__new__(helpers.PoolBinding)
    binding._originals = {}
    binding.splitting_multikey = False
    observed = []
    parent = SimpleNamespace(source="env:GOOGLE_API_KEY")
    child = SimpleNamespace(source="env:GOOGLE_API_KEY#kame-key-1")
    class HostPool:
        def _persist(self, *, removed_ids=None, status_cleared_ids=None):
            observed.append((list(self._entries), removed_ids, status_cleared_ids))
            if raise_on_write:
                raise RuntimeError("write failed")
            return "written"
    binding._guard_persist(HostPool)
    pool = HostPool()
    pool._lock = threading.RLock()
    entries = [parent, child] if derived else [parent]
    pool._entries = entries
    removed, cleared = ["old"], ["refreshed"]
    if raise_on_write:
        with pytest.raises(RuntimeError, match="write failed"):
            pool._persist(removed_ids=removed, status_cleared_ids=cleared)
    else:
        assert pool._persist(removed_ids=removed, status_cleared_ids=cleared) == "written"
    assert observed == [([parent], removed, cleared)]
    assert observed[0][1] is removed and observed[0][2] is cleared
    assert pool._entries == entries


@pytest.mark.parametrize("source", ["header", "retryinfo", "exception", "text",
                                   "body.resets_at", "header.retry-after", "exception.retry_delay"])
def test_fresh_provider_wait_can_still_be_stretched_by_measured_strikes(source):
    """Old name/behaviour (pre-1.8.0.0): ``test_fresh_provider_wait_never_
    stretched`` — asserted ``is None`` for every one of these, because
    1.7.0.7's blanket ``if provider_timed(source): return None`` refused
    escalation outright on a provider-timed source, whatever ``strikes``
    said. Decision 0004 D3 removed that guard, measured: on the owner's
    corpus it was blocking exactly the case R13 authorizes — a stated
    deadline served in full, refused again. ``strikes`` decides now, the
    same as for any other source.
    """
    assert stretch(reset_at=1007, now=1000, strikes=20, window="per_minute", source=source) == 1056.0


def test_estimated_wait_can_still_learn():
    assert stretch(reset_at=1020, now=1000, strikes=2, window="per_minute", source="window") == 1040


def book(source="window", reason="rate_limit"):
    j = Journal()
    for at in (1000, 1021):
        j.record_block(at=at, provider="gemini", model="m", credential_id="A",
                       reset_at=at+20, window="per_minute", source=source, reason=reason)
    return j


def streak(j, **kwargs):
    return short_streak(j, credential_id="A", model="m", window="per_minute", at=1042, **kwargs)


def test_chain_requires_same_reason_and_provenance():
    assert streak(book(), source="window", reason="rate_limit") == 2
    assert streak(book(reason="auth"), source="window", reason="rate_limit") == 0
    # Old assertion (pre-1.8.0.0): == 0. 1.7.0.7's blanket
    # `if provider_timed(previous.source): break` treated a *previous*
    # block's own fresh source as disqualifying evidence, wherever it sat
    # in the chain. Decision 0004 D3 removed it: a previous block that was
    # itself provider-timed is not evidence against the chain — it is the
    # shape of the case R13 authorizes, a stated deadline served in full
    # and refused again.
    assert streak(book(source="header"), source="window", reason="rate_limit") == 2
    assert streak(book(source="anchor"), source="window", reason="rate_limit") == 0
    # Old assertion (pre-1.8.0.0): == 0. 1.7.0.7's blanket
    # `if provider_timed(source): return 0` at the top of short_streak
    # zeroed the whole streak whenever the CURRENT refusal carried a fresh
    # provider number, whatever its own history showed. Removed by D3 for
    # the same reason as above.
    assert streak(book(), source="header", reason="rate_limit") == 2


def test_timing_preserves_legacy_elapsed_but_separates_pool_wait(tmp_path, monkeypatch):
    helpers = load_test_helpers("test_v1_7_0_5.py", "kame_706_timing_helpers")
    pkg = helpers._load_package()
    timings = importlib.import_module(pkg.__name__ + ".timings")
    target = tmp_path / "calls.jsonl"
    monkeypatch.setattr(timings, "_destination", lambda: str(target))
    monkeypatch.setattr(timings, "_off", lambda: False)
    monkeypatch.setattr(timings, "_silenced", False)
    timings.record(started_at=40, ended_at=42, waited_before_s=30,
                   pool_waited_before_s=5, call_id="one-call")
    row = json.loads(target.read_text(encoding="utf-8"))
    assert row["ms_waited_before"] == row["ms_elapsed_before"] == 30000
    assert row["ms_pool_waited_before"] == 5000
    assert row["ms_total"] == 2000
    assert row["call_id"] == "one-call"


def test_dispatch_records_actual_recovery_wait_separately(monkeypatch):
    from types import SimpleNamespace
    helpers = load_test_helpers("test_dispatch.py", "kame_706_dispatch_helpers")
    dispatch = helpers.dispatch_binding
    helpers.settings.forget()
    for name in list(helpers.settings._ENV_FOR.values()) + list(helpers.settings._NUMBER_ENV_FOR.values()):
        monkeypatch.delenv(name, raising=False)
    clock = [100.0]
    monkeypatch.setattr(dispatch, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: 10000+clock[0], time_ns=lambda: 123456,
        sleep=lambda seconds: clock.__setitem__(0, clock[0]+seconds)))
    binding = helpers._binding()
    agent = helpers.Agent()
    selections = iter([(helpers.KEYS[0], "EXHAUSTED"), (helpers.KEYS[0], "READY")])
    monkeypatch.setattr(binding.engine, "select", lambda *a, **kw: next(selections))
    def recover(*args):
        clock[0] += 5
        return True
    monkeypatch.setattr(binding, "_wait_for_recovery", recover)
    captured = []
    monkeypatch.setattr(dispatch.timings, "record", lambda **kw: captured.append(kw))
    def host(*args, **kwargs):
        clock[0] += 30
        return helpers.Answer("done")
    binding.run(host, agent, {}, (), {})
    assert len(captured) == 1
    assert captured[0]["pool_waited_before_s"] == 5
    assert captured[0]["waited_before_s"] == 5
    assert captured[0]["ended_at"]-captured[0]["started_at"] == 30
    assert captured[0]["call_id"]
    helpers.settings.forget()
