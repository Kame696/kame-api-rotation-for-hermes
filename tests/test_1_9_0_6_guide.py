"""1.9.0.6 - the panel's round "i" opens "How to install or configure" (checked with node in ``tests/ui_1906_guide.mjs``)."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="no node on this machine")
def test_the_info_button_opens_the_install_and_configure_guide():
    script = Path(__file__).with_name("ui_1906_guide.mjs")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "GUIDE-OK" in result.stdout, result.stdout + result.stderr
