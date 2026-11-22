"""1.9.0.5 - shared helpers for the one-package and profile-client tests.

* ``load()`` the real plugin package (no Hermes needed);
* ``FakeProviders`` - a stand-in for Hermes' ``providers`` module (list / register / source);
* ``run_host()`` - run a scenario script in Hermes' own venv against a source checkout, with a temp
  ``HERMES_HOME`` (never the real one). Tests that use it skip cleanly when the venv or checkout is absent.
"""
from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_1905_under_test"

HERMES_SRC = Path(os.environ.get("KAME_HERMES_SRC", str(Path.home() / "dev" / "hermes-agent")))
HERMES_PY = Path(os.environ.get("KAME_HERMES_PY", str(Path.home() / "dev" / "hermes-venv" / "Scripts" / "python.exe")))


def load():
    """The real plugin package, loaded once under a private name."""
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sub(name):
    load()
    return importlib.import_module(f"{PACKAGE}.{name}")


# --- a stand-in for Hermes' `providers` module ------------------------------------------------------


@dataclasses.dataclass
class FProfile:
    name: str
    auth_type: str = "api_key"
    api_mode: str = "chat_completions"
    base_url: str = "https://example.invalid/v1"

    def create_client(self, **kw):
        return None


class FakeProviders(types.ModuleType):
    """``list_providers`` / ``register_provider`` / ``provider_source`` / ``get_provider_profile``."""

    def __init__(self, *profiles):
        super().__init__("providers")
        self.calls = []
        self._profiles = {p.name: p for p in profiles}
        self._source = {p.name: "bundled" for p in profiles}
        self.fail_register = False

    def list_providers(self):
        self.calls.append("list_providers")
        return list(self._profiles.values())

    def get_provider_profile(self, name):
        return self._profiles.get(name)

    def provider_source(self, name):
        return self._source.get(name, "")

    def register_provider(self, profile):
        self.calls.append(("register", profile.name))
        if self.fail_register:
            raise RuntimeError("boom")
        self._profiles[profile.name] = profile
        self._source[profile.name] = "runtime"


# --- the real Hermes, in its own venv --------------------------------------------------------------


def host_available() -> bool:
    return HERMES_PY.exists() and (HERMES_SRC / "providers" / "__init__.py").exists()


needs_host = pytest.mark.skipif(not host_available(), reason="Hermes venv / source checkout not present")


def run_host(script: str, home: Path, *args: str, timeout: int = 240) -> dict:
    """Run ``tests/<script>`` with Hermes' venv python; the script prints one JSON object on its last line."""
    env = dict(os.environ)
    env.update({"HERMES_HOME": str(home), "KAME_RECORDER_DISABLED": "1", "KAME_CALL_TIMINGS_DISABLED": "1",
                "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(HERMES_SRC)})
    env.pop("HERMES_PLUGIN_HOST_PROCESS", None)
    done = subprocess.run([str(HERMES_PY), str(ROOT / "tests" / script), str(PLUGIN_DIR), str(home), *args],
                          cwd=str(HERMES_SRC), env=env, capture_output=True, text=True, timeout=timeout)
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    assert lines, f"no result from {script}: rc={done.returncode}\n{done.stdout[-1500:]}\n{done.stderr[-1500:]}"
    return json.loads(lines[-1])


def make_home(root: Path, name: str, plugin: bool, enabled: bool = True) -> Path:
    home = root / name
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    if plugin:
        shutil.copytree(PLUGIN_DIR, home / "plugins" / "hermes-kame-api-rotation",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    names = ["hermes-kame-api-rotation"] if (plugin and enabled) else []
    listing = "".join(f"    - {n}\n" for n in names) or "    []\n"
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n" + (listing if names else "    []\n"), encoding="utf-8")
    return home
