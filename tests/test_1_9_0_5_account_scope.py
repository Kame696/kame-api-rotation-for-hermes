"""1.9.0.5 (E2, decisions/0036): an account-wide hold needs a second model to say the same.

Pure carousel, explicit ``now``. The old rule (one ACCOUNT-scope refusal holds the key on every model of the
provider) is what let one wrongly labelled refusal take a whole provider out for up to an hour.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_account_scope_1905_under_test"


def _load():
    if PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{PACKAGE}.core.carousel"), importlib.import_module(f"{PACKAGE}.core.quota")


carousel, quota = _load()
ACCOUNT = quota.QuotaScope.ACCOUNT

KEY = "AIzaSyACCOUNT" + "0" * 26
OTHER = "AIzaSyOTHERKEY" + "0" * 25
KEYS = [KEY, OTHER]
A, B, C = "gemini:model-a", "gemini:model-b", "gemini:model-c"
T0 = 1_800_000_000.0


def _engine():
    return carousel.Carousel()


def _refuse(engine, identity, key, now, kind="rate_limit", delay=0.0, scope=ACCOUNT, stated=False):
    """Default: a LABEL-only refusal (an account marker, no time from the provider)."""
    return engine.mark(identity, key, False, delay, kind, now=now, stated=stated, scope=scope)


def _ready(engine, identity, now):
    return engine.healthy_count(identity, KEYS, now=now)


def test_one_account_scope_refusal_holds_only_this_models_key():
    engine = _engine()
    _refuse(engine, A, KEY, T0)
    assert _ready(engine, A, T0 + 1) == 1          # KEY rests on A
    assert _ready(engine, B, T0 + 1) == 2          # and is still offered on B
    assert _ready(engine, C, T0 + 1) == 2
    assert (("gemini", KEY)) not in engine._account_hold


def test_a_provider_stated_deadline_is_proof_and_account_wide_at_once():
    engine = _engine()
    _refuse(engine, A, KEY, T0, kind="daily", delay=12660.0, stated=True)     # e.g. Codex: a real reset time
    assert ("gemini", KEY) in engine._account_hold
    assert _ready(engine, B, T0 + 1) == 1 and _ready(engine, C, T0 + 1) == 1
    engine2 = _engine()
    _refuse(engine2, A, KEY, T0, delay=60.0, stated=True)                      # stated seconds
    assert ("gemini", KEY) in engine2._account_hold


def test_a_label_only_refusal_waits_for_a_second_model_even_with_a_hold_on_this_one():
    engine = _engine()
    _refuse(engine, A, KEY, T0, kind="daily")
    assert ("gemini", KEY) not in engine._account_hold
    assert _ready(engine, B, T0 + 1) == 2


def test_a_second_model_within_ten_minutes_makes_the_hold_account_wide():
    engine = _engine()
    _refuse(engine, A, KEY, T0)
    _refuse(engine, B, KEY, T0 + 599.0)
    assert ("gemini", KEY) in engine._account_hold
    # a third model, which never refused, now finds the key resting too
    assert _ready(engine, C, T0 + 600.0) == 1


def test_a_second_model_after_ten_minutes_starts_a_new_suspicion_instead():
    engine = _engine()
    _refuse(engine, A, KEY, T0)
    _refuse(engine, B, KEY, T0 + 601.0)
    assert ("gemini", KEY) not in engine._account_hold
    assert _ready(engine, C, T0 + 602.0) == 2
    # and B's refusal is the pending one now: C within the window confirms it
    _refuse(engine, C, KEY, T0 + 700.0)
    assert ("gemini", KEY) in engine._account_hold


def test_the_same_model_refusing_twice_is_not_a_second_model():
    engine = _engine()
    _refuse(engine, A, KEY, T0)
    _refuse(engine, A, KEY, T0 + 30.0)
    assert ("gemini", KEY) not in engine._account_hold
    assert _ready(engine, B, T0 + 31.0) == 2


def test_another_key_is_its_own_suspicion():
    engine = _engine()
    _refuse(engine, A, KEY, T0)
    _refuse(engine, B, OTHER, T0 + 10.0)
    assert ("gemini", KEY) not in engine._account_hold and ("gemini", OTHER) not in engine._account_hold


def test_money_is_account_wide_at_once():
    for kind in ("insufficient_quota", "billing"):
        engine = _engine()
        _refuse(engine, A, KEY, T0, kind=kind, delay=3600.0)
        assert ("gemini", KEY) in engine._account_hold, kind
        assert _ready(engine, B, T0 + 1) == 1, kind


def test_a_scope_that_is_not_account_never_spans_models():
    for scope in (quota.QuotaScope.UNKNOWN, quota.QuotaScope.MODEL if hasattr(quota.QuotaScope, "MODEL")
                  else quota.QuotaScope.UNKNOWN):
        engine = _engine()
        _refuse(engine, A, KEY, T0, scope=scope)
        _refuse(engine, B, KEY, T0 + 5.0, scope=scope)
        assert ("gemini", KEY) not in engine._account_hold


def test_an_answer_clears_the_suspicion():
    engine = _engine()
    _refuse(engine, A, KEY, T0)
    engine.mark(B, KEY, True, now=T0 + 30.0)            # the key answered on another model
    _refuse(engine, B, KEY, T0 + 60.0)                  # a later account-scope refusal starts over
    assert ("gemini", KEY) not in engine._account_hold


def test_a_confirmed_hold_is_still_released_by_an_answer_and_by_reset():
    engine = _engine()
    _refuse(engine, A, KEY, T0)
    _refuse(engine, B, KEY, T0 + 5.0)
    assert ("gemini", KEY) in engine._account_hold
    engine.mark(C, KEY, True, now=T0 + 6.0)
    assert ("gemini", KEY) not in engine._account_hold
    _refuse(engine, A, KEY, T0 + 10.0)
    assert engine._account_pending
    engine.forget()
    assert not engine._account_pending and not engine._account_hold


def _hold_on_two_models(engine, **kwargs):
    _refuse(engine, A, KEY, T0, **kwargs)
    _refuse(engine, B, KEY, T0 + 5, **kwargs)
    engine._pool_for(C, KEYS, T0 + 6)   # model C's pool exists (select builds it), with no history of its own


def test_an_account_hold_remembers_why_and_only_a_label_hold_is_re_tested_on_other_models():
    label = _engine()
    _hold_on_two_models(label, kind="daily")
    assert label._account_hold_why[("gemini", KEY)] == "label"
    assert [k for _u, k in label._label_held(C, KEYS, T0 + 7)] == [KEY]

    for why, kwargs in (("money", dict(kind="insufficient_quota")),
                        ("auth", dict(kind="auth")),
                        ("stated", dict(kind="daily", delay=12660.0, stated=True))):
        engine = _engine()
        _hold_on_two_models(engine, **kwargs)
        assert engine._account_hold_why[("gemini", KEY)] == why, why
        assert engine._label_held(C, KEYS, T0 + 7) == [], why


def test_a_short_stated_refusal_does_not_turn_a_label_hold_into_a_stated_one():
    engine = _engine()
    _hold_on_two_models(engine, kind="daily")
    _refuse(engine, A, KEY, T0 + 10, kind="per_minute", delay=30.0, stated=True)
    assert engine._account_hold_why[("gemini", KEY)] == "label"
    # ...but a deadline the provider stated for the quota does
    _refuse(engine, A, KEY, T0 + 20, kind="daily", delay=12660.0, stated=True)
    assert engine._account_hold_why[("gemini", KEY)] == "stated"


def test_an_answer_and_a_reset_forget_why():
    engine = _engine()
    _hold_on_two_models(engine, kind="daily")
    engine.mark(C, KEY, True, now=T0 + 8)
    assert ("gemini", KEY) not in engine._account_hold_why
    _hold_on_two_models(engine, kind="daily")
    engine.forget()
    assert not engine._account_hold_why
