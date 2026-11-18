"""1.9.0.3 - the page half of C1, C3, C4 and C5 is checked with node in ``tests/ui_1903.mjs`` (skipped without node)."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="no node on this machine")
def test_the_page_says_what_1903_says():
    script = Path(__file__).with_name("ui_1903.mjs")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
