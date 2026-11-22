"""1.9.0.5 (F1) - every Hermes symbol the package imports still exists in the Hermes checkout."""
from __future__ import annotations

import importlib.util
import subprocess
import sys

import pytest

from tests.h1905 import HERMES_PY, HERMES_SRC, ROOT, needs_host


def _tool():
    spec = importlib.util.spec_from_file_location("host_symbols_tool", ROOT / "tools" / "host_symbols.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_list_is_built_from_the_source_and_finds_the_known_imports(tmp_path):
    tool = _tool()
    found = tool.collect()
    assert "providers.list_providers" in found
    assert "agent.credential_pool.load_pool" in found
    assert "hermes_constants.get_hermes_home" in found
    # nothing from KAME's own relative imports
    assert not any(s.startswith(("kame", ".")) for s in found)


def test_collect_sees_imports_inside_functions_and_try_blocks(tmp_path):
    tool = _tool()
    (tmp_path / "m.py").write_text(
        "import tools.thing\n"
        "def f():\n    try:\n        from agent.x import (a,\n            b)\n    except ImportError:\n        pass\n"
        "from . import local\nimport os\n", encoding="utf-8")
    assert sorted(tool.collect(tmp_path)) == ["agent.x.a", "agent.x.b", "tools.thing"]


def test_a_missing_symbol_is_a_fail_and_an_optional_one_is_not(tmp_path, monkeypatch):
    tool = _tool()
    ok, why = tool.check("os.path.join")
    assert ok
    ok, why = tool.check("os.path.no_such_thing")
    assert not ok and "no attribute" in why
    ok, why = tool.check("no_such_hermes_module.thing")
    assert not ok


@needs_host
def test_every_symbol_imports_on_the_host_checkout():
    done = subprocess.run([str(HERMES_PY), str(ROOT / "tools" / "host_symbols.py"), "--host", str(HERMES_SRC)],
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-1500:]
    assert " 0 FAIL" in done.stdout.splitlines()[-1]
