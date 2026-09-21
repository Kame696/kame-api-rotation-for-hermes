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
*mutating the exception's ``args``* on the way past. This does neither on the
way past: an error that is being rotated on is handed onward exactly as it
arrived, and only the copy the classifier reads has the paragraph taken off.

1.7.0.1 adds the one place the text is also removed for a *reader* —
``take_off_the_advice``, called where KAME has given up and is surfacing the
failure. This file used to say the sentence was kept "because something
downstream may want to show a human the sentence Hermes wrote for them", and
on an install rotating fourteen keys that sentence is wrong advice: it tells
the owner the free tier cannot sustain a session, under a refusal the pool
answered on the next key. Nothing else changed — mid-rotation errors still
carry the host's words.
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

#: A block shorter than this is not specific enough to truncate a message at.
#: The two literals above are 38 and 41 characters; the host's real constants
#: are paragraphs. Nothing shorter is trusted to name where the advice starts.
_MIN_ADVICE_LEN = 24

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


def take_off_the_advice(error: object) -> object:
    """Strip Hermes' appended guidance from an error the user is about to read.

    Until 1.7.0.1 this module took the host's paragraph off only the *copy*
    the classifier reads, and said so in this file's own opening: the
    exception was left exactly as it arrived, "because something downstream
    may want to show a human the sentence Hermes wrote for them". That was
    right for a Hermes without a rotation plugin and wrong here, and the owner
    is the one who caught it. The paragraph reads:

        "...the free tier is exhausted in a handful of messages and cannot
        sustain an agent session. Enable billing..."

    On an install rotating fourteen keys that is advice about a situation the
    user is not in, printed underneath a refusal KAME has already answered. It
    was on screen at 19:28 on 2026-09-05 while the pool went on to answer the
    turn on the next key.

    Only reached where KAME has decided to surface — the end of the road, when
    every key has refused. Anything the host still wants to reason about is
    untouched: the type, the status code, the body and every other attribute
    are the ones that arrived. Only the human-readable text changes, and only
    by deletion.

    Returns the same object, so a caller can ``raise take_off_the_advice(e)``.
    Never raises: an error that reaches a user with one paragraph too many is
    a blemish, and an error that cannot be raised at all is a lost turn.
    """
    blocks = guidance_blocks()
    if not blocks:
        return error
    try:
        said = getattr(error, "message", None)
        if isinstance(said, str) and said:
            cut = _cut_at_the_advice(said, blocks)
            if cut is not None:
                error.message = cut  # type: ignore[attr-defined]

        args = getattr(error, "args", None)
        if isinstance(args, tuple) and args and isinstance(args[0], str):
            cut = _cut_at_the_advice(args[0], blocks)
            if cut is not None:
                error.args = (cut,) + args[1:]  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - defensive
        pass
    return error


def without_advice(message: str) -> str:
    """The provider's own words, for a surface that claims to show them.

    The Events tab opens a refusal under the heading *"What the provider
    actually sent"*, and until 1.7.0.1 it also showed the paragraph **Hermes**
    appends underneath — the one about the free tier not sustaining an agent
    session. On an install rotating fourteen credentials that is advice about
    a situation the owner is not in, printed beside a refusal the pool
    answered on the next key. He reported seeing it there after the release
    that was supposed to have removed it: ``take_off_the_advice`` covers the
    error that is *raised*, and this panel is fed from a different string.

    Deliberately not applied to ``recorder`` — a recording that has already
    been edited cannot answer a question about the edit, and the recorder
    exists to answer exactly those.

    Returns the message unchanged when the host appended nothing.
    """
    if not message:
        return message
    try:
        cut = _cut_at_the_advice(message, guidance_blocks())
    except Exception:  # pragma: no cover - defensive
        return message
    return message if cut is None else cut


def _cut_at_the_advice(message: str, blocks: List[str]) -> "str | None":
    """Everything before the host's paragraph starts, or ``None`` if absent.

    Truncation rather than the deletion ``evidence.strip_trailing_blocks``
    does, and the difference matters exactly here. That function removes the
    block it was given, which is enough for the classifier: it only needs the
    phrase *"a few hundred requests/day"* gone before ``quota`` reads ``/day``
    as a daily cap.

    A reader needs the whole paragraph gone, and the fallback literals in this
    file are opening clauses on purpose — cutting only the clause would leave
    "...cannot sustain an agent session. Enable billing..." on screen under a
    refusal KAME has already answered, which is the sentence the owner
    objected to. Since Hermes appends its guidance and never prepends it,
    everything from the opening clause onward is the appended block.

    The provider's own words are always before the cut, which is the half that
    is kept.
    """
    earliest = None
    for block in blocks:
        if not block or len(block) < _MIN_ADVICE_LEN:
            continue
        found = message.find(block)
        if found >= 0 and (earliest is None or found < earliest):
            earliest = found
    if earliest is None:
        return None
    return message[:earliest].rstrip()


def reset_cache() -> None:
    """Forget the resolved blocks. Tests only."""
    global _resolved, _cache, _source
    _resolved = False
    _cache = []
    _source = ""
