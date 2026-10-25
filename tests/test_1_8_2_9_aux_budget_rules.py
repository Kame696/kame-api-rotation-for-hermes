"""1.8.2.9 / decisions/0032 A1 - what a background job may spend.

The measured defect: one ``title_generation`` call on the best model made 167 attempts over 744 s
(2026-10-23 22:41-22:53) because a background call inherited the chat's unlimited wait. The rules
that decide the budget are pure and live in ``core/aux_budget.py``.
"""
import importlib.util
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "kame_aux_rules_pkg"


def _module():
    if PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE, ROOT / "hermes-kame-api-rotation" / "__init__.py",
            submodule_search_locations=[str(ROOT / "hermes-kame-api-rotation")])
        module = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = module
        spec.loader.exec_module(module)
    return __import__(PACKAGE + ".core.aux_budget", fromlist=["x"])


budget_for = lambda *a, **k: _module().budget_for(*a, **k)  # noqa: E731


def test_the_cosmetic_list_is_closed_and_small():
    m = _module()
    assert m.COSMETIC == frozenset({"title_generation", "profile_describer", "background_review"})


def test_a_title_is_cosmetic_three_requests_no_waiting():
    b = budget_for("title_generation", True, None, 0.0)
    assert (b.klass, b.max_requests, b.may_wait) == ("cosmetic", 3, False)
    assert b.total_s == 30.0


def test_compression_is_important_and_may_wait():
    b = budget_for("compression", True, None, 0.0)
    assert (b.klass, b.max_requests, b.may_wait) == ("important", 6, True)
    assert b.total_s == 120.0


def test_an_unknown_or_guessed_task_is_never_cosmetic():
    assert budget_for("title_generation", False, None, 0.0).klass == "important"
    assert budget_for("", True, None, 0.0).klass == "important"
    assert budget_for("auxiliary", True, None, 0.0).klass == "important"
    assert budget_for("something_new", True, None, 0.0).klass == "important"


def test_the_task_name_is_normalised():
    assert budget_for("  Title_Generation ", True, None, 0.0).klass == "cosmetic"


def test_the_host_timeout_is_the_budget():
    assert budget_for("title_generation", True, 12, 0.0).total_s == 12.0
    assert budget_for("approval", True, 45.5, 0.0).total_s == 45.5


def test_the_host_timeout_is_capped_by_the_class():
    assert budget_for("compression", True, 600, 0.0).total_s == 300.0
    assert budget_for("title_generation", True, 600, 0.0).total_s == 30.0


def test_a_missing_or_odd_timeout_falls_back_to_the_class_default():
    for odd in (None, 0, -3, float("nan"), "30", object(), True, float("inf")):
        b = budget_for("title_generation", True, odd, 0.0)
        assert b.total_s == 30.0, odd
    assert budget_for("approval", True, None, 0.0).total_s == 120.0


def test_the_floor_is_five_seconds():
    assert budget_for("title_generation", True, 1, 0.0).total_s == 5.0


def test_a_users_own_bound_only_tightens():
    assert budget_for("approval", True, None, 10.0).total_s == 10.0
    assert budget_for("approval", True, None, 0.0).total_s == 120.0      # 0 = no bound = ignored
    assert budget_for("approval", True, None, 900.0).total_s == 120.0    # never loosens
    assert budget_for("title_generation", True, 12, 2.0).total_s == 5.0  # floor still wins


def test_the_budget_is_finite_whatever_goes_in():
    for task in ("title_generation", "compression", "x"):
        for timeout in (None, 1e12, 5):
            for cap in (0.0, 1e9, 3.0):
                assert math.isfinite(budget_for(task, True, timeout, cap).total_s)


def test_the_unbounded_switch_is_off_unless_asked(monkeypatch):
    m = _module()
    monkeypatch.delenv("KAME_AUX_UNBOUNDED", raising=False)
    assert m.unbounded() is False
    monkeypatch.setenv("KAME_AUX_UNBOUNDED", "1")
    assert m.unbounded() is True
    monkeypatch.setenv("KAME_AUX_UNBOUNDED", "0")
    assert m.unbounded() is False
