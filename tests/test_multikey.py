"""The rules for reading one credential that holds several keys.

Pure: no Hermes, no pool, no filesystem. What these establish is that the
identity of a split key is the key, that the marker naming a derived part
cannot be confused with anything a host writes, and that a value which is
not a list is left alone.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent / "hermes-kame-api-rotation"


def _load():
    spec = importlib.util.spec_from_file_location(
        "kame_multikey_under_test",
        PLUGIN / "core" / "multikey.py",
        submodule_search_locations=[str(PLUGIN / "core")],
    )
    # ``core.keys`` is imported relatively, so the package has to exist first.
    package_spec = importlib.util.spec_from_file_location(
        "kame_core_pkg", PLUGIN / "core" / "__init__.py",
        submodule_search_locations=[str(PLUGIN / "core")],
    )
    package = importlib.util.module_from_spec(package_spec)
    sys.modules["kame_core_pkg"] = package
    package_spec.loader.exec_module(package)
    spec = importlib.util.spec_from_file_location(
        "kame_core_pkg.multikey", PLUGIN / "core" / "multikey.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["kame_core_pkg.multikey"] = module
    spec.loader.exec_module(module)
    return module


multikey = _load()

# Long enough to look like credentials, which is what the parser requires.
K1 = "AIzaSyTestKeyNumberOneAAAAAAAAAAAAAAAAAA"
K2 = "AIzaSyTestKeyNumberTwoBBBBBBBBBBBBBBBBBB"
K3 = "AIzaSyTestKeyNumberThreeCCCCCCCCCCCCCCCC"


class TestSplittingAValue:
    def test_a_comma_list_is_several_keys(self):
        keys, rejected = multikey.split_value(f"{K1},{K2},{K3}")
        assert keys == [K1, K2, K3]
        assert rejected == 0

    def test_one_key_is_not_a_list(self):
        assert multikey.split_value(K1) == ([], 0)

    def test_nothing_is_not_a_list(self):
        assert multikey.split_value("") == ([], 0)
        assert multikey.split_value(None) == ([], 0)

    def test_a_trailing_separator_does_not_invent_a_second_key(self):
        assert multikey.split_value(f"{K1},") == ([], 0)

    def test_the_other_separators_work_too(self):
        """Whatever the paste used. The rules live in ``parse_keys``; this is
        only the promise that this module did not quietly narrow them."""
        for joined in (f"{K1};{K2}", f"{K1}|{K2}", f"{K1}\n{K2}", f"{K1} {K2}"):
            assert multikey.split_value(joined)[0] == [K1, K2]

    def test_a_fragment_is_counted_and_not_returned(self):
        """A URL is the fragment that actually turns up — pasted along with
        the keys from whatever page they were copied off."""
        keys, rejected = multikey.split_value(
            f"{K1},https://aistudio.google.com/apikey,{K2}"
        )
        assert keys == [K1, K2]
        assert rejected == 1

    def test_a_repeat_collapses(self):
        assert multikey.split_value(f"{K1},{K2},{K1}")[0] == [K1, K2]


class TestNamingAPart:
    def test_a_derived_source_is_recognisable(self):
        source = multikey.child_source("env:GOOGLE_API_KEY", 2)
        assert multikey.is_child_source(source)

    def test_and_a_real_one_is_not(self):
        for source in ("manual", "env:GOOGLE_API_KEY", "", None, "/home/x/creds.json"):
            assert not multikey.is_child_source(source)

    def test_the_part_number_is_in_the_label(self):
        assert multikey.child_label("GOOGLE_API_KEY", 2, 7) == "GOOGLE_API_KEY (2/7)"

    def test_a_missing_label_still_produces_one(self):
        assert multikey.child_label(None, 1, 2) == "? (1/2)"


class TestIdentityIsTheKey:
    def test_the_same_key_always_gets_the_same_id(self):
        assert multikey.child_id("row", K1) == multikey.child_id("row", K1)

    def test_different_keys_get_different_ids(self):
        assert multikey.child_id("row", K1) != multikey.child_id("row", K2)

    def test_moving_a_key_in_the_list_does_not_rename_it(self):
        """The reason identity is not the position.

        Delete the first key of a list and every key after it shifts up one.
        If ids were positional, the ledger — which remembers what is spent by
        id — would read all of them as credentials it has never tried, and a
        pool that is out of quota would look fresh.
        """
        first = multikey.plan_children(
            parent_id="row", parent_source="env:X", parent_label="X",
            keys=[K1, K2, K3],
        )
        after_deletion = multikey.plan_children(
            parent_id="row", parent_source="env:X", parent_label="X",
            keys=[K2, K3],
        )
        surviving = {plan["access_token"]: plan["id"] for plan in first}
        assert [p["id"] for p in after_deletion] == [surviving[K2], surviving[K3]]

    def test_the_id_does_not_contain_the_key(self):
        assert K1 not in multikey.child_id("row", K1)
        assert K1[:12] not in multikey.child_id("row", K1)


class TestPlanningTheParts:
    def test_one_plan_per_key_in_order(self):
        plans = multikey.plan_children(
            parent_id="row", parent_source="env:GOOGLE_API_KEY",
            parent_label="GOOGLE_API_KEY", keys=[K1, K2],
        )
        assert [p["access_token"] for p in plans] == [K1, K2]
        assert [p["label"] for p in plans] == [
            "GOOGLE_API_KEY (1/2)", "GOOGLE_API_KEY (2/2)",
        ]

    def test_every_part_is_marked_as_derived(self):
        plans = multikey.plan_children(
            parent_id="row", parent_source="manual", parent_label="pasted",
            keys=[K1, K2, K3],
        )
        assert all(multikey.is_child_source(p["source"]) for p in plans)

    def test_every_part_has_its_own_identity(self):
        plans = multikey.plan_children(
            parent_id="row", parent_source="manual", parent_label="pasted",
            keys=[K1, K2, K3],
        )
        assert len({p["id"] for p in plans}) == 3
        assert len({p["source"] for p in plans}) == 3
