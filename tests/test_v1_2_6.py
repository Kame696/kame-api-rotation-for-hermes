"""1.2.4 — parity with Agent Zero: daily cooldowns floor at 1 hour max, no Pacific midnight lockout.

In 1.2.0 through 1.2.3, Google Gemini daily quotas calculated seconds until
midnight in US/Pacific, locking pools out for 12-18 hours on burst errors.
1.2.4 restores full parity with Agent Zero: daily quotas floor at 1 hour
(3600s), and burst limits (RPM/TPM) use the standard short cooldown.
"""

from __future__ import annotations

import importlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
MANIFEST = PLUGIN_DIR / "plugin.yaml"

core_mod = importlib.import_module("hermes-kame-api-rotation.core")
quota_mod = importlib.import_module("hermes-kame-api-rotation.core.quota")
classify_mod = importlib.import_module("hermes-kame-api-rotation.core.classify")


def test_gemini_daily_cap_floors_at_one_hour_max():
    """Daily quotas for Gemini floor at 3600s, never midnight US/Pacific."""
    now = time.time()
    morning = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    decision = quota_mod.compute_reset_at(
        now_epoch=now,
        provider="gemini",
        body={
            "error": {
                "code": 429,
                "message": "Resource has been exhausted (e.g. check quota).",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "subject": "project:my-proj",
                                "description": "Quota exceeded for quota metric 'GenerateRequestsPerDayPerProjectPerModel'",
                            }
                        ],
                    }
                ],
            }
        },
        now=morning,
    )
    assert decision.window == quota_mod.QuotaWindow.PER_DAY
    assert decision.reset_at - now == pytest.approx(3600, abs=1)
    assert decision.source == "window"


def test_gemini_per_minute_burst_uses_short_cooldown():
    """Per-minute throttles are not confused with daily caps."""
    now = time.time()
    decision = quota_mod.compute_reset_at(
        now_epoch=now,
        provider="gemini",
        body={
            "error": {
                "code": 429,
                "message": "Resource has been exhausted (e.g. check quota).",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "subject": "project:my-proj",
                                "description": "Quota exceeded for quota metric 'GenerateRequestsPerMinutePerProjectPerModel'",
                            }
                        ],
                    }
                ],
            }
        },
    )
    assert decision.window == quota_mod.QuotaWindow.PER_MINUTE
    assert decision.reset_at - now == pytest.approx(65, abs=1)


def test_nvidia_daily_cap_floors_at_one_hour_max():
    """OpenAI-compatible daily quotas floor at 1 hour max."""
    now = time.time()
    decision = quota_mod.compute_reset_at(
        now_epoch=now,
        provider="nvidia",
        message="Daily request limit reached for model kimi-k3",
    )
    assert decision.window == quota_mod.QuotaWindow.PER_DAY
    assert decision.reset_at - now == pytest.approx(3600, abs=1)


def test_the_manifest_the_core_and_the_changelog_agree():
    manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert tuple(int(part) for part in core_mod.__version__.split(".")) >= (1, 2, 6)
    assert f'version: "{core_mod.__version__}"' in manifest
    assert f"## [{core_mod.__version__}]" in changelog
    assert "manifest_version: 1" in manifest
    assert "api_version: 1" in manifest
