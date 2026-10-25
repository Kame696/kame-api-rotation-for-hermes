"""1.8.2.9 - the idle gateway must not re-read config.yaml every few seconds.

Measured on the owner's gateway (2026-10-24, py sampler inside pid 9000): the gateway loads the plugin
once per profile (four copies), and each copy parsed the 42 KB ``config.yaml`` three times every five
seconds (``read``, ``entry_source``, ``switch_limit``), about 120 ms each with the pure-Python reader
the real Hermes ships. That was roughly 30% of a core while nothing was happening. The file is parsed
once per change now, whoever asks, and a half-saved file keeps the last good reading.
"""
import importlib.util
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "kame_cfg_once_pkg"


def _load():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE + ".core.fallback_chain"]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, ROOT / "hermes-kame-api-rotation" / "__init__.py",
        submodule_search_locations=[str(ROOT / "hermes-kame-api-rotation")])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return __import__(PACKAGE + ".core.fallback_chain", fromlist=["x"])


CHAIN = ("model:\n  provider: gemini\n  default: best\n  base_url: https://example.test/v1\n"
         "fallback_providers:\n  - provider: gemini\n    model: second\n  - provider: gemini\n    model: third\n"
         "agent:\n  api_max_retries: 5\n")


@pytest.fixture()
def chain(tmp_path, monkeypatch):
    module = _load()
    module.forget()
    (tmp_path / "config.yaml").write_text(CHAIN, encoding="utf-8")
    calls = {"n": 0}
    real = module._load_yaml

    def counting(text):
        calls["n"] += 1
        return real(text)

    monkeypatch.setattr(module, "_load_yaml", counting)
    yield module, tmp_path, calls
    module.forget()


def _everything(module, home):
    module.read(home)
    module.entry_source("gemini:second", home)
    module.switch_limit(home)
    module.later_entries("gemini:best", home)


def test_three_readers_one_parse_even_over_a_long_idle(chain, monkeypatch):
    module, home, calls = chain
    _everything(module, home)
    assert calls["n"] == 1, "the three readers share one parse of the file"
    clock = [time.monotonic()]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    for _ in range(600):                      # ten idle minutes, one look per second
        clock[0] += 1.0
        _everything(module, home)
    assert calls["n"] == 1, "an unchanged file is never parsed again, however long Hermes idles"


def test_a_changed_file_is_read_again_by_all_three(chain, monkeypatch):
    module, home, calls = chain
    clock = [time.monotonic()]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    assert module.switch_limit(home) == 5
    assert module.read(home)[1] == ("gemini:second", "gemini:third")
    (home / "config.yaml").write_text(CHAIN.replace("api_max_retries: 5", "api_max_retries: 7")
                                      + "# a longer file\n", encoding="utf-8")
    clock[0] += 1.5                           # the file is looked at once a second
    assert module.switch_limit(home) == 7
    assert module.read(home)[0] == "gemini:best"
    assert module.entry_source("gemini:second", home) == ("second", "")
    assert calls["n"] == 2


def test_a_half_saved_file_keeps_the_last_good_reading(chain, monkeypatch):
    module, home, calls = chain
    good = module.read(home)
    assert good[1]
    clock = [time.monotonic()]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    (home / "config.yaml").write_text("model: [unclosed\n  - : :\n", encoding="utf-8")
    clock[0] += 30.0
    assert module.read(home) == good, "a file Hermes is saving right now is not 'no chain'"
    assert module.switch_limit(home) == 5


def test_two_profile_homes_do_not_share_a_reading(chain, tmp_path_factory):
    module, home, _calls = chain
    other = tmp_path_factory.mktemp("other")
    (other / "config.yaml").write_text(CHAIN.replace("second", "other-second"), encoding="utf-8")
    assert module.read(home)[1][0] == "gemini:second"
    assert module.read(other)[1][0] == "gemini:other-second"


def test_forget_clears_the_reading(chain):
    module, home, calls = chain
    module.read(home)
    module.forget()
    module.read(home)
    assert calls["n"] == 2


def test_a_file_that_stays_broken_is_parsed_once(chain, monkeypatch):
    # Review finding 3: a typo left in config.yaml must not bring the idle CPU back.
    module, home, calls = chain
    good = module.read(home)
    assert calls["n"] == 1
    clock = [time.monotonic()]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    (home / "config.yaml").write_text("model: [unclosed\n  - : :\n", encoding="utf-8")
    clock[0] += 1.5
    for _ in range(60):                                        # three seconds of polling every 50 ms
        clock[0] += 0.05
        _everything(module, home)
    for _ in range(40):
        clock[0] += 1.0                                        # and forty more seconds of it
        _everything(module, home)
    assert calls["n"] == 2, "one parse of the broken text, never again while it is unchanged"
    assert module.read(home) == good, "the last good reading stays"
    (home / "config.yaml").write_text(CHAIN.replace("api_max_retries: 5", "api_max_retries: 9"), encoding="utf-8")
    clock[0] += 1.5
    assert module.switch_limit(home) == 9
    assert calls["n"] == 3
