"""1.8.1.7: coarse clocks retain liveness and never imply text delivery."""
import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "hermes-kame-api-rotation"
NAME = "kame_v1817_under_test"
spec = importlib.util.spec_from_file_location(NAME, PLUGIN / "__init__.py",
                                            submodule_search_locations=[str(PLUGIN)])
mod = importlib.util.module_from_spec(spec)
sys.modules[NAME] = mod
spec.loader.exec_module(mod)
dispatch = importlib.import_module(NAME + ".dispatch_binding")
core = importlib.import_module(NAME + ".core")


@pytest.mark.parametrize("step", [0.015625, 0.001, 1.0])
@pytest.mark.parametrize("delivery", [False, True])
def test_quantized_clock_tracks_activity_without_inventing_delivery(monkeypatch, step, delivery):
    clock = [100.0]
    monkeypatch.setattr(dispatch.time, "monotonic", lambda: clock[0])
    progress = dispatch._Progress()
    before = progress.last_activity
    method = progress.touch if delivery else progress.stir
    method()
    assert progress.last_activity == before  # a real clock can repeat an instant
    assert progress.any is delivery
    clock[0] += step
    method()
    assert progress.last_activity == clock[0]
    assert progress.any is delivery


def test_reasoning_liveness_on_a_coarse_clock_does_not_claim_text(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(dispatch.time, "monotonic", lambda: clock[0])
    progress = dispatch._Progress()
    agent = SimpleNamespace()
    callbacks = {"on_first_delta": lambda: None}
    dispatch._install_shims(agent, progress, callbacks)
    clock[0] += 0.015625
    callbacks["on_first_delta"]()
    assert progress.last_activity == clock[0]
    assert progress.any is False


def test_current_manifest_and_core_version_agree():
    assert 'version: "1.8.1.8"' in (PLUGIN / "plugin.yaml").read_text(encoding="utf-8")
    assert core.__version__ == "1.8.1.8"


def test_archive_history_is_not_rewritten():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [1.8.1.7]" in text
    assert "## [1.8.1.6]" in text
