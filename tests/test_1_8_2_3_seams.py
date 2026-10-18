"""V3 seam check tests: presence, bind-check, drift, never-raises.

The v2 check only proved "the import works" — which is exactly why a host
update that renames a parameter sailed through as OK while the plugin
silently stopped rotating. The v3 check binds the plugin's real call shape
against the host signature and reports DRIFT by name.
"""
import os
import sys
import unittest
from types import ModuleType, FunctionType

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
integrity = importlib.import_module("hermes_kame_api_rotation.integrity")
check_host_seams = integrity.check_host_seams


class TestSeamCheck(unittest.TestCase):
    def setUp(self):
        # a fake host that satisfies every seam, with the real shapes
        for name in list(sys.modules):
            if name.startswith(("hermes_cli", "agent", "providers", "hermes_kame_provider")):
                del sys.modules[name]
        plugins = ModuleType("hermes_cli.plugins")
        plugins.PluginManager = type("PluginManager", (), {})
        sys.modules["hermes_cli.plugins"] = plugins

        cred = ModuleType("agent.credential_pool")
        def load_pool(provider):  # real shape: one positional
            return {}
        cred.load_pool = load_pool
        cred.CredentialPool = type("CredentialPool", (), {})
        sys.modules["agent.credential_pool"] = cred

        prov = ModuleType("providers")
        def get_provider_profile(name):  # real host shape: one "name" kwarg
            return None
        prov.get_provider_profile = get_provider_profile
        sys.modules["providers"] = prov

        kame_provider = ModuleType("hermes_kame_provider")
        sys.modules["hermes_kame_provider"] = kame_provider

    def tearDown(self):
        for name in list(sys.modules):
            if name.startswith(("hermes_cli", "agent", "providers", "hermes_kame_provider")):
                del sys.modules[name]

    def test_all_ok_on_matching_host(self):
        report = check_host_seams()
        self.assertTrue(report["ok"], report["summary"])
        self.assertEqual(report["required_missing"], [])
        self.assertEqual(report["drift"], {})

    def test_missing_module_is_reported_not_raised(self):
        del sys.modules["agent.credential_pool"]
        sys.modules["agent.credential_pool"] = None  # forces ImportError on import
        report = check_host_seams()
        self.assertFalse(report["ok"])
        self.assertIn("agent.credential_pool.load_pool", report["required_missing"])

    def test_signature_drift_is_drift_not_ok(self):
        """THE new capability: present but no longer binding."""
        cred = sys.modules["agent.credential_pool"]
        def load_pool(pool_name, extra_required):  # shape changed by host update
            return {}
        cred.load_pool = load_pool
        report = check_host_seams()
        self.assertFalse(report["ok"])
        self.assertIn("agent.credential_pool.load_pool", report["required_missing"])
        self.assertIn("agent.credential_pool.load_pool", report["drift"])
        self.assertIn("no longer binds", report["drift"]["agent.credential_pool.load_pool"])
        # and the summary names it — diagnosable from the panel
        self.assertIn("DRIFT", report["summary"])

    def test_optional_absent_is_ok(self):
        # Block the real Hermes "providers" package from leaking into the
        # test env (the installed tree is on this machine's sys.path): a
        # meta_path finder that raises keeps the module genuinely absent.
        import importlib.abc

        class _Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name == "providers":
                    raise ImportError(f"blocked for test: {name}")
                return None
        blocker = _Block()
        sys.modules.pop("providers", None)
        sys.meta_path.insert(0, blocker)
        try:
            report = check_host_seams()
            self.assertTrue(report["ok"], "an optional seam missing must not fail the check")
            self.assertIn("providers.get_provider_profile", report["optional_absent"])
        finally:
            sys.meta_path.remove(blocker)

    def test_never_raises(self):
        sys.modules["hermes_cli.plugins"] = None  # import of None explodes
        report = check_host_seams()  # must return, not raise
        self.assertIsInstance(report, dict)


if __name__ == "__main__":
    unittest.main()
