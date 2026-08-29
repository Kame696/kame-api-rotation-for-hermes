"""The sentences Hermes adds to a provider's error, so KAME can take them off.

Hermes' Gemini adapter appends its own guidance to some provider failures — a
free-tier 429 gains a paragraph about the free tier, a legacy-key 401 gains one
about key types. That is good product behaviour and it is poison to a
classifier, because the appended paragraph contains words the classifier reads
as evidence about the failure:

    "...a few hundred requests/day for Gemini Flash models..."

``quota._PER_DAY_MARKERS`` matches ``/day``. So a sixty-second per-minute
throttle arrived carrying, in the host's own handwriting, the phrase that means
"this key is done for the day" — and was benched for an hour. Across fourteen
keys that is the whole pool gone, repeatedly, for a limit that would have
cleared by itself. Ninety-two log lines in nine days.

**The blocks are imported from the host, not copied from it.** A literal pasted
here is right on the day it is written and silently wrong the day Hermes
rewords its own paragraph — and the failure is invisible, because a stale
literal simply stops matching and the poison flows again. Importing means KAME
follows the host automatically; the literals below are a floor for the case
where the import fails, and ``tools/host_assumptions.py`` fails loudly when the
names move so this file gets updated on purpose rather than by accident.

1.2.6 fixed the same bug by string-splitting on one hardcoded prefix and
*mutating the exception's ``args``* on the way past. This does neither: the
host's exception is left exactly as it arrived, because something downstream
may want to show a human the sentence Hermes wrote for them.
"""

from __future__ import annotations

from typing import List, Tuple

#: Used only when the import below fails. Deliberately the opening clause of
#: each block rather than the whole paragraph: an opening survives a URL
#: changing at the end, and matching less is the safe direction here — a block
#: that is not stripped is the bug we already understand, while a block that
#: over-matches would eat the provider's own words.
_FALLBACK_BLOCKS: Tuple[str, ...] = (
    "Your Google API key is on the free tier",
    "Google Gemini rejected this API key's type",
)

#: Where the blocks live upstream, as ``(module, attribute)``. Private names on
#: purpose — they are private to Hermes and this is a deliberate coupling, which
#: is why it is asserted by a tripwire rather than assumed.
_HOST_BLOCK_SOURCES: Tuple[Tuple[str, str], ...] = (
    ("agent.gemini_native_adapter", "_FREE_TIER_GUIDANCE"),
    ("agent.gemini_native_adapter", "_STANDARD_KEY_GUIDANCE"),
)

_cache: List[str] = []
_resolved = False
_source = ""


def _load() -> Tuple[List[str], str]:
    """Import the host's guidance blocks, falling back to the literals.

    Never raises. This is called from the error path, and a plugin that turns a
    recoverable API failure into an ImportError has done more damage than the
    misclassification it was trying to prevent.
    """
    blocks: List[str] = []
    imported = 0
    for module_name, attribute in _HOST_BLOCK_SOURCES:
        try:
            from importlib import import_module

            value = getattr(import_module(module_name), attribute, None)
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            blocks.append(value)
            imported += 1

    if not blocks:
        return list(_FALLBACK_BLOCKS), "fallback"

    # Both roads at once. The imported paragraph is the exact text this Hermes
    # appends; the fallback opening also catches a message that was recorded
    # earlier — a journal entry, a test fixture, an error captured before an
    # upgrade — where the wording is the previous release's.
    for literal in _FALLBACK_BLOCKS:
        if not any(literal in block for block in blocks):
            blocks.append(literal)
    return blocks, f"host:{imported}"


def guidance_blocks() -> List[str]:
    """The text blocks Hermes appends, cached for the life of the process.

    Cached because this runs on every classified failure and the answer cannot
    change without a restart — Hermes' module constants are bound at import.
    """
    global _resolved, _cache, _source
    if not _resolved:
        _cache, _source = _load()
        _resolved = True
    return _cache


def guidance_source() -> str:
    """``host:N`` or ``fallback`` — reported in ``/kame`` and in the panel.

    A plugin silently running on fallback literals is a plugin one Hermes
    reword away from the bug it was built to prevent, so the state is shown
    rather than kept.
    """
    guidance_blocks()
    return _source


def reset_cache() -> None:
    """Forget the resolved blocks. Tests only."""
    global _resolved, _cache, _source
    _resolved = False
    _cache = []
    _source = ""
