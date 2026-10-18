"""V3 retry hint tests: the three families, and the loop bug killed.

The load-bearing test is ``test_quota_daily_never_says_retry`` — the exact
v1/v2 defect: a "retrying will very likely succeed" hint on a spent daily
quota turned a clean failure into an agent retry loop. A daily quota must
now say the opposite, in the same breath.
"""
import os
import sys
import unittest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hermes-kame-api-rotation"))
sys.path.insert(0, _SRC)

import importlib
import importlib.util as _ilu

_pkg_spec = _ilu.spec_from_file_location(
    "hermes_kame_api_rotation", os.path.join(_SRC, "__init__.py"),
    submodule_search_locations=[_SRC],
)
_pkg = _ilu.module_from_spec(_pkg_spec)
sys.modules.setdefault("hermes_kame_api_rotation", _pkg)
_pkg_spec.loader.exec_module(_pkg)
transport = importlib.import_module("hermes_kame_api_rotation.transport")
_with_retry_hint = transport._with_retry_hint


class TestHintFamilies(unittest.TestCase):
    def _hinted(self, kind, window="", msg="provider refused"):
        exc = RuntimeError(msg)
        return _with_retry_hint(exc, kind=kind, quota_window=window), exc

    def test_transient_kinds_get_retry_hint(self):
        for kind in ("timeout", "server", "server_overload", "connection", "rate_limit"):
            hinted, exc = self._hinted(kind)
            self.assertIn("Retrying the same tool call", str(hinted))
            self.assertIs(hinted, exc, "the original object must be preserved")

    def test_quota_daily_never_says_retry(self):
        """THE regression test."""
        hinted, _ = self._hinted("quota", window="per_day")
        text = str(hinted)
        self.assertIn("Do NOT keep retrying", text)
        self.assertNotIn("very likely succeed", text)

    def test_quota_kind_without_window_is_the_wall(self):
        # A quota refusal the classifier could not window defaults to the
        # wall hint: "do not retry now" on a per-minute throttle costs one
        # manual retry, while "do retry" on a daily wall costs an agent
        # loop — the asymmetry picks the safe side (documented decision).
        hinted, _ = self._hinted("quota", window="")
        self.assertIn("Do NOT keep retrying", str(hinted))

    def test_rate_limit_with_daily_window_is_the_wall(self):
        # the kind says per-minute throttle but the window says daily: the
        # window wins — this is the exact shape Gemini's quotaId confusion
        # produces when the metric name is identical
        hinted, _ = self._hinted("rate_limit", window="per_day")
        self.assertIn("Do NOT keep retrying", str(hinted))

    def test_permanent_kinds_say_do_not_retry(self):
        for kind in ("auth", "billing", "denied", "permission"):
            hinted, _ = self._hinted(kind)
            self.assertIn("Retrying will not help", str(hinted))
            self.assertNotIn("very likely succeed", str(hinted))

    def test_unknown_kind_no_hint(self):
        hinted, exc = self._hinted("something_new")
        self.assertEqual(str(hinted), "provider refused")

    def test_empty_kind_no_hint(self):
        # empty kind = unknown: silence, never a confident wrong hint
        hinted, _ = self._hinted("")
        self.assertEqual(str(hinted), "provider refused")

    def test_idempotent_no_double_hint(self):
        exc = RuntimeError("provider refused")
        once = _with_retry_hint(exc, kind="timeout")
        twice = _with_retry_hint(once, kind="timeout")
        self.assertEqual(str(once).count("Retrying the same tool call"), 1)
        self.assertIs(once, twice)

    def test_family_does_not_migrate_on_resurface(self):
        # surfaced as transient, re-surfaced with a daily window: the
        # first (correct at the time) hint stays; the error never carries
        # two contradicting hints
        exc = RuntimeError("provider refused")
        first = _with_retry_hint(exc, kind="rate_limit")
        second = _with_retry_hint(first, kind="quota", quota_window="per_day")
        text = str(second)
        self.assertTrue(
            ("Retrying the same tool call" in text) != ("Do NOT keep retrying" in text),
            f"both families present: {text}",
        )

    def test_type_and_traceback_preserved(self):
        class ProviderError(Exception):
            status_code = 429
        exc = ProviderError("429 too many requests")
        hinted = _with_retry_hint(exc, kind="rate_limit")
        self.assertIs(type(hinted), ProviderError)
        self.assertEqual(hinted.status_code, 429)
        self.assertTrue(str(hinted).startswith("429 too many requests"),
                        "the provider's own text must lead")

    def test_args_surgery_keeps_extra_args(self):
        exc = ValueError("msg", "extra")
        hinted = _with_retry_hint(exc, kind="timeout")
        self.assertEqual(hinted.args[1:], ("extra",))


class TestSurfacedHintWiring(unittest.TestCase):
    """The hint constants exist and the surface path uses the families."""

    def test_constants(self):
        self.assertIn("rate_limit", transport._TRANSIENT_KINDS)
        self.assertIn("quota", transport._QUOTA_WALL_KINDS)
        self.assertIn("auth", transport._PERMANENT_KINDS)
        self.assertNotIn("quota", transport._TRANSIENT_KINDS,
                          "THE BUG: quota must never be transient")
        self.assertNotIn("insufficient_quota", transport._TRANSIENT_KINDS)


if __name__ == "__main__":
    unittest.main()
