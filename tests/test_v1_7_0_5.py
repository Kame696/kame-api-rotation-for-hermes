"""1.7.0.5 — the panel and the engine had drifted, and nothing could tell.

Every Events row carries a small chip saying **where the rest came from**: a
field the provider filled in, a header, the retry hint, or KAME's own default.
The panel renders it with

    const sized = SIZED_BY_LABELS[event.sized_by] ?? null
    ...
    sized && h(...)

so a value the map has never heard of does not render an unknown chip — it
renders **nothing**, and the row looks like every other row.

Two values were reachable and unlabelled:

``window``
    KAME's own default, applied because the payload supplied nothing to size
    by. This is the single most important source a reader can be shown,
    because it is the one that says *do not believe this number, we made it
    up* — and it was the one showing no source at all.

``text``
    A duration read out of the error sentence.

The owner found it by asking whether the Events tab might have a problem. It
did, it had been there since the chip was introduced, and no test crossed the
two sides — which is why the fix is not two lines in a map but a set in
``core.vocabulary`` that both sides are checked against.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PANEL = PLUGIN_DIR / "desktop" / "plugin.js"
PACKAGE = "kame_v1_7_0_5_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
vocabulary = importlib.import_module(f"{PACKAGE}.core.vocabulary")


def panel_labels() -> dict[str, str]:
    """The keys of ``SIZED_BY_LABELS``, read out of the panel source.

    Parsed rather than imported because the panel is JavaScript and there is
    no runtime here to evaluate it. The block is found by its opening brace
    and read to the matching close, so a key added anywhere inside it counts
    and a key in some other object does not.
    """
    source = PANEL.read_text(encoding="utf-8")
    start = source.index("const SIZED_BY_LABELS = {")
    end = source.index("\n}", start)
    block = source[start:end]
    return {
        match.group(1): match.group(2)
        for match in re.finditer(r"^\s*(\w+):\s*\['([^']*)'", block, re.M)
    }


def test_every_source_the_engine_can_emit_has_a_label():
    """The check that did not exist, and the reason the chip could vanish."""
    labels = panel_labels()
    missing = sorted(vocabulary.SIZED_BY_SOURCES - set(labels))
    assert not missing, (
        "these can reach an Events row and the panel would show no source "
        f"chip for them: {missing}"
    )


def test_the_two_that_were_missing_are_named_here():
    """Pinned by name, so the regression is recognisable and not just absent.

    A set-difference test passes again the moment somebody adds the keys back;
    it does not say which two mattered or why. These do.
    """
    labels = panel_labels()
    assert "window" in labels, "the 'this number is ours' chip"
    assert "text" in labels, "the 'read from the message' chip"
    # And `window` must not be presented as though a provider supplied it.
    # `pattern`, `table` and `reprobe` are the other weak ones; a 'good' tone
    # here would tell the reader the opposite of the truth.
    source = PANEL.read_text(encoding="utf-8")
    window_line = next(
        line for line in source.splitlines() if line.strip().startswith("window:")
    )
    assert "'weak'" in window_line, "our own default must not read as evidence"


def test_the_vocabulary_covers_what_dispatch_actually_writes():
    """The set is only worth having if it is complete on the Python side too.

    ``dispatch_binding`` writes four literals of its own and otherwise passes
    ``verdict.source`` through. Those four are checked here by reading the
    file, so adding a fifth without adding it to the vocabulary fails.
    """
    dispatch = (PLUGIN_DIR / "dispatch_binding.py").read_text(encoding="utf-8")
    written = set(re.findall(r'sized_by = "(\w+)"', dispatch))
    written |= set(re.findall(r'"(\w+)" if window_number_declined', dispatch))
    unknown = sorted(written - vocabulary.SIZED_BY_SOURCES)
    assert not unknown, f"dispatch_binding writes these and nothing knows them: {unknown}"


@pytest.mark.parametrize("source", sorted({"window", "text", "reprobe", "table"}))
def test_the_weak_sources_are_marked_weak(source):
    """A guess must never be drawn like a fact.

    These four are all KAME's own reading — a default, a sentence, a re-probe,
    a lookup table — and the chip's second element drives the colour. Marking
    one of them 'good' would put a provider's authority behind a number no
    provider gave.
    """
    labels_with_tone = {}
    block_source = PANEL.read_text(encoding="utf-8")
    start = block_source.index("const SIZED_BY_LABELS = {")
    end = block_source.index("\n}", start)
    for match in re.finditer(
        r"^\s*(\w+):\s*\['([^']*)',\s*'(\w+)'\]",
        block_source[start:end],
        re.M,
    ):
        labels_with_tone[match.group(1)] = match.group(3)
    assert labels_with_tone.get(source) == "weak"
