"""1.9.0.6 (decisions/0037 C12) - the manifest declares the hook KAME registers, and the host knows it."""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "hermes-kame-api-rotation" / "plugin.yaml"
DEV_VENV = Path.home() / "dev" / "hermes-venv" / "Scripts" / "python.exe"   # a local Hermes checkout and its venv, when present
DEV_CLONE = Path.home() / "dev" / "hermes-agent"


def test_pre_llm_call_is_declared_in_both_lists():
    import re
    text = MANIFEST.read_text(encoding="utf-8")
    for key in ("hooks", "provides_hooks"):
        block = re.search(rf"^{key}:\s*\n((?:\s*(?:-.*|#.*)\n)+)", text.replace("\r\n", "\n"), re.M).group(1)
        assert "- pre_llm_call" in block, key


@pytest.mark.skipif(not (DEV_VENV.exists() and DEV_CLONE.exists()), reason="the dev clone of Hermes is not on this machine")
def test_the_hosts_own_manifest_reader_and_hook_list_accept_the_package():
    import yaml
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    hooks, provides = list(data["hooks"]), list(data["provides_hooks"])
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(DEV_CLONE)!r})
        from hermes_cli.plugins import VALID_HOOKS
        hooks, provides = {hooks!r}, {provides!r}
        assert hooks == provides, (hooks, provides)
        unknown = [h for h in hooks if h not in VALID_HOOKS]
        assert not unknown, unknown
        assert "pre_llm_call" in hooks
        print("MANIFEST-OK")
    """)
    done = subprocess.run([str(DEV_VENV), "-B", "-c", script], capture_output=True, text=True, timeout=120)
    assert "MANIFEST-OK" in done.stdout, done.stdout + done.stderr[-1500:]
