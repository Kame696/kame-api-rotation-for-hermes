"""Do not write into the owner's evidence.

Every test module here loads the **real** plugin package, and the real plugin
writes beside the *installed* state file rather than beside the test that called
it: `recorder` appends to `refusals.jsonl`, `timings` to `calls.jsonl`, and
`state` publishes `state.json`, all under `state.state_dir()`. On this machine
that resolves through `HERMES_HOME` to the owner's live Hermes install.

Measured on 2026-09-07: one run of this suite wrote **116 synthetic attempts and
80 synthetic refusals** into his real logs, and twenty-five minutes of iterating
on 1.7.0.2 had put 1,131 of them there. It had happened before with the offline
tools, guarded on 2026-09-06 (`bf32143`); the tests were missed, so the same
file was lost the same way twice.

That is not cosmetic. `refusals.jsonl` has an 8 MB ceiling and stops writing
when it is reached, and the last time it filled with synthetic rows the raw body
of the one refusal he asked me to explain had never been captured.

Two guards, because one was not enough:

* `HERMES_HOME` is redirected to a throwaway directory. This is the
  load-bearing one — it moves the *destination*, so a test that legitimately
  turns the recorder on (there are several) still cannot reach his files. The
  switches below were tried first on their own and 5 of 45 test modules wrote
  anyway.
* The two record switches are set off, so the common case does not even open a
  file.

Set at import time rather than in a fixture: pytest imports this before any test
module, and therefore before the plugin reads either value. Nothing about the
installed plugin changes — this only affects this process.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

#: Kept for the life of the process and deliberately not cleaned up: a test that
#: fails while writing here leaves its evidence behind, which is the only reason
#: any of it is worth reading afterwards. The OS clears its own temp.
SANDBOX_HOME = Path(tempfile.mkdtemp(prefix="kame-tests-home-"))

os.environ["HERMES_HOME"] = str(SANDBOX_HOME)
os.environ.setdefault("KAME_RECORDER_DISABLED", "1")
os.environ.setdefault("KAME_CALL_TIMINGS_DISABLED", "1")
# 1.8.1.2: tools/clock_gate.py and tools/continuity_gate.py write their
# reports here instead of over the committed research/1.8.0.0 evidence.
os.environ.setdefault("KAME_GATE_OUT_DIR", str(SANDBOX_HOME / "gate-out"))


import pytest


@pytest.fixture(autouse=True)
def _hermes_home_stays_redirected():
    """Put the redirect back after every test, whatever the test did to it.

    Setting it once at import was enough while nothing in the suite moved it
    again. `tools/replay_timeline.py` does move it — it has to, it loads a
    whole plugin version of its own — and when a test drove that code the
    variable stayed moved for every test that ran afterwards. Nothing wrote
    to the owner's install (the replacement is another temp directory), but
    the guarantee this file exists to give had quietly stopped being true,
    and `test_v1_7_0_2::test_the_suite_cannot_reach_the_installed_state_directory`
    is what noticed: green alone, red in the full run.
    """
    yield
    if os.environ.get("HERMES_HOME") != str(SANDBOX_HOME):
        os.environ["HERMES_HOME"] = str(SANDBOX_HOME)
