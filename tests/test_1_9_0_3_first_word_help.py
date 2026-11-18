"""1.9.0.3 (C9) - the help of "Wait for the first token" says what the code does (backlog L2)."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "kame_fwh_pkg"


def _load():
    if PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE, ROOT / "hermes-kame-api-rotation" / "__init__.py",
            submodule_search_locations=[str(ROOT / "hermes-kame-api-rotation")])
        module = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = module
        spec.loader.exec_module(module)
    return (__import__(PACKAGE + ".settings", fromlist=["x"]),
            __import__(PACKAGE + ".transport", fromlist=["x"]))


def test_help_numbers_are_the_constants_of_the_code():
    settings, transport = _load()
    help_text = settings.META[settings.FIRST_TOKEN_WAIT][1]
    plain, thinking = transport._AUTO_SILENCE_S[False][0], transport._AUTO_SILENCE_S[True][0]
    assert f"{plain:.0f} s at least" in help_text
    assert f"{thinking:.0f} s for thinking models" in help_text
    assert "2,000" in help_text
    assert "20 s at least" not in help_text and "60 s for thinking" not in help_text
