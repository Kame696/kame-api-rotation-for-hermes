"""1.8.2.7: the provider answer-key gate is a regression floor, not a one-off.

``tools/provider_doc_gate.py`` grades the plugin's real failure path
(``KameTransport._on_failure``) against an independent answer key of documented
provider errors. The numbers below are what 1.8.2.7 reached; the floor stops a
later change from quietly losing them. Raise the floor when the plugin learns
more; never lower it to make a change pass - the disagreeing row names what
broke.

Known, intended divergences that keep the numbers under 100%: the timeout rows
(policy C, decisions/0024: the key expects `defer_host_retry`, the gate grades
one failure alone and sees `rotate_key`), upstream-vs-server and
model_not_ready-vs-request_fault labels (same behaviour), quota 429s filed as
billing by Cohere/SambaNova (same 3600 s hold).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "tools" / "provider_doc_gate.py"
KEYS = ROOT / "research" / "1.8.2.7" / "expected"

#: (key file, minimum rows agreeing on family, minimum on action)
FLOORS = [
    ("verdicts-providers.jsonl", 120, 124),
    ("verdicts-nvidia.jsonl", 17, 15),
    ("verdicts-batch-a.jsonl", 117, 113),
    ("verdicts-batch-b.jsonl", 96, 97),
    ("verdicts-batch-c.jsonl", 128, 128),
    ("verdicts-batch-d.jsonl", 108, 119),
    ("verdicts-batch-e.jsonl", 80, 72),
]


def _score(key_file: str):
    proc = subprocess.run(
        [sys.executable, "-I", str(GATE), "--key", str(KEYS / key_file)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240, cwd=str(ROOT),
    )
    out = proc.stdout
    got = {}
    for field in ("family", "action"):
        match = re.search(rf"^\s*{field}\s+agree=\s*(\d+)", out, re.M)
        assert match, f"gate printed no {field} line:\n{out[-600:]}"
        got[field] = int(match.group(1))
    return got


@pytest.mark.parametrize("key_file,family_floor,action_floor", FLOORS)
def test_the_plugin_still_agrees_with_the_answer_key(key_file, family_floor, action_floor):
    got = _score(key_file)
    assert got["family"] >= family_floor, f"{key_file}: family {got['family']} < {family_floor}"
    assert got["action"] >= action_floor, f"{key_file}: action {got['action']} < {action_floor}"
