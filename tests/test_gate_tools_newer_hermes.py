"""Gate tools against Hermes after 0.21.5 (main, 2026-09-25).

That host asks git for its own version while importing, so the runtime
contracts' offline guard must let read-only git through -- and nothing else.
"""
import importlib.util
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "host_runtime_contracts.py"


def _tool():
    spec = importlib.util.spec_from_file_location("hrc_newer_hermes", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("argv", [
    ["git", "-C", "C:/h/hermes-agent", "rev-parse", "HEAD"],
    ["git", "describe", "--tags", "--long", "--match", "v2[0-9][0-9][0-9].*", "HEAD"],
    ["git", "branch", "--show-current"],
    ["git", "tag", "--merged", "HEAD", "--list", "v[0-9]*"],
    # Windows audits the joined command line, not the list.
    'git -C "C:\\Users\\x\\hermes-agent" rev-parse HEAD',
])
def test_read_only_git_is_allowed(argv):
    assert _tool()._read_only_git((None, argv, None, None)) is True


@pytest.mark.parametrize("argv", [
    ["git", "fetch", "origin"],
    ["git", "checkout", "main"],
    ["git", "branch", "new-branch"],
    ["git", "tag", "v9"],
    ["python", "-c", "print(1)"],
    "cmd /c del x",
])
def test_anything_else_stays_blocked(argv):
    assert _tool()._read_only_git((None, argv, None, None)) is False
