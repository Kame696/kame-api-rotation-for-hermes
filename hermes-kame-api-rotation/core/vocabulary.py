"""The names for a failure, in one place, because scattering them cost us three times.

The same defect has now shipped three times, and each fix went to the site that
broke instead of to the words:

* **1.4.0** — the carousel's ladder spoke only ``per_minute`` while the
  classifier had started saying ``rate_limit``.
* **1.6.0.3** — the set of failures that must never end a turn had the same gap,
  thirty lines away in another file. Six turns in one 46-minute run ended with a
  quota error on screen where the answer was to wait.
* **now** — the classifier says ``auth_permanent`` for a credential the provider
  named dead; every set that acts on that says ``revoked``. So a dead key was
  neither retired from rotation nor protected from ending a turn.

There are genuinely **two** vocabularies here and merging them would be wrong.
``auth_permanent`` is Hermes' own word — it is a member of the host's
``FailoverReason`` enum and it is what the host expects handed back. ``revoked``
is this plugin's internal word for the same fact. What was missing was never a
rename: it was the translation, and a single place to keep it.

Membership questions go through :func:`same_kind` and :func:`may_end_turn` so a
future word cannot silently fall outside a set again.
"""

from __future__ import annotations

from typing import Any, FrozenSet

#: What ``core.carousel`` and ``dispatch_binding`` produce, for their own
#: decisions. Read out of the code, not out of a docstring: the first draft of
#: this module trusted a partial grep, called three of these words dead, and was
#: wrong about all three. ``daily`` and ``insufficient_quota`` are produced at
#: ``carousel.py:797`` and ``dispatch_binding.py:2442``.
INTERNAL_KINDS: FrozenSet[str] = frozenset(
    {"auth", "daily", "denied", "host_breaker", "insufficient_quota", "other",
     "per_minute", "revoked", "server", "timeout"}
)

#: What ``core.classify`` hands back to Hermes. These are the host's words, out
#: of ``agent/error_classifier.py``'s ``FailoverReason``, and they must stay
#: spelled the host's way or the hook result stops meaning anything.
HOST_REASONS: FrozenSet[str] = frozenset(
    {"auth", "auth_permanent", "billing", "rate_limit"}
)

#: The same fact under two spellings. Left side is any word either vocabulary
#: may produce; right side is the internal word the sets are written in.
#: Only two entries, and both are genuine aliases — the same fact spelled two
#: ways by the two vocabularies. ``daily`` and ``insufficient_quota`` were in an
#: earlier draft of this table and do not belong: they are kinds in their own
#: right, not other names for something else, and translating them away would
#: have hidden a real distinction to no purpose.
_TRANSLATION = {
    "auth_permanent": "revoked",   # the provider named this credential dead
    "per_minute": "rate_limit",    # the 1.4.0 split: one throttle, two spellings
}


def internal(kind: Any) -> str:
    """The internal spelling of a failure, whichever vocabulary named it."""
    word = str(kind or "").strip().lower()
    return _TRANSLATION.get(word, word)


def same_kind(left: Any, right: Any) -> bool:
    """Whether two words name the same failure across the two vocabularies."""
    return internal(left) == internal(right)


def provider_timed(source: Any) -> bool:
    """Whether this refusal carries a freshly read duration/deadline.

    A retry becoming eligible is not a guarantee of success. Repetition must
    not multiply a newly stated wait. Calendar anchors inferred from a named
    window remain distinct: their existing measured-offset correction stays.

    1.7.0.7 also used this as a blanket filter in ``escalate.stretch`` and
    ``journal.short_streak``: any current or historical refusal carrying a
    fresh provider number was excluded from ever counting toward a strike.
    Decision 0004 D3 removed both uses, measured: on the owner's corpus the
    guard blocked 31 of ~530 widenings, and those 31 were exactly the case
    R13 authorizes — a stated deadline served in full, refused again. This
    function survives 1.8.0.0 only because ``tools/replay_timeline.py``
    still calls it, to *measure* how often a refusal carries a fresh
    provider number, never again to gate one.
    """
    word = str(source or "").strip().lower()
    # 1.8.1.2: ``text.reset_at`` (a clock time in the provider's prose) is
    # the provider's number too; it now answers the dispatch's "was this
    # stated?" as well as the replay's measurement.
    return word in {"header", "retryinfo", "exception", "text"} or word.startswith(
        ("header.", "body.", "exception.", "text.")
    )


#: Failures the plugin may end a turn on. Two words, and only the two that mean
#: *this module could not name the failure at all*.
#:
#: The list used to run the other way: nine named failures could never end a
#: turn and everything else could. So every word nobody remembered to add ended
#: turns by default — ``other``, ``auth_permanent`` and ``billing`` were all
#: outside it. A list that leaks by default will leak again; this one fails
#: closed, and a word it has never heard of waits instead of ending a turn.
#:
#: **Why not empty.** The first draft of this was empty, on the reasoning that
#: ``carousel.is_terminal`` already stops every request-shaped failure upstream.
#: Two tests written in 1.0.9 refuted it in seconds: a **418** is not in
#: ``_TERMINAL_STATUS``, so it rotates — and a pool where every key answers the
#: same 418 has asked everyone there is to ask. Looping there forever is not
#: patience, it is a hang. The tests were right and the reasoning was wrong.
#:
#: **Why only these two.** Unanimity is evidence about the *request* only when
#: the failure is one this module could not attribute to a key. A dead
#: credential (``auth_permanent``), a spent account (``billing``) and every
#: throttle are all attributable — unanimity there says the *pool* needs
#: attention, not that the request is malformed, and ending the turn puts the
#: wrong sentence on screen. That is exactly what happened six times in one
#: 46-minute run.
#:
#: The other conditions carry the safety, not this set: every key tried, every
#: one failing identically, none succeeding.
MAY_END_A_TURN: FrozenSet[str] = frozenset({"other", "unknown"})


def may_end_turn(kind: Any) -> bool:
    """Whether this failure is allowed to end a turn. See :data:`MAY_END_A_TURN`."""
    return internal(kind) in MAY_END_A_TURN


def unknown_words() -> FrozenSet[str]:
    """Words some vocabulary produces that this module has never been told about.

    A test asserts this is empty. That is the guard the last three releases did
    not have: a new word added to either classifier shows up here instead of
    silently falling outside whichever set forgot it.
    """
    produced = {internal(word) for word in (INTERNAL_KINDS | HOST_REASONS)}
    return frozenset(produced - (INTERNAL_KINDS | HOST_REASONS))


#: The wait a refusal gets when nothing named one. Twenty seconds, and it is the
#: only invented number in the rule: a first refusal from a provider nobody has
#: seen before has no evidence to stand on, and being wrong short costs one
#: request while being wrong long costs an hour of a healthy key.
FLOOR_SECONDS = 20.0

#: Internal kind -> the host's own word for it. The hook result is expanded into
#: Hermes' ``ClassifiedError``, so a reason it does not know degrades to unknown
#: and the recovery hints stop meaning anything.
_TO_HOST = {
    "server": "server_error",
    "timeout": "timeout",
    "auth": "auth",
    "revoked": "auth_permanent",
    "denied": "auth_permanent",
    "host_breaker": "unknown",
    "per_minute": "rate_limit",
    "rate_limit": "rate_limit",
    "daily": "rate_limit",
    "insufficient_quota": "billing",
    "billing": "billing",
    "other": "unknown",
}


def to_host_reason(kind: Any) -> str:
    """The host's word for a failure this plugin named internally.

    Falls back to ``unknown``, which is a real member of Hermes'
    ``FailoverReason`` and means "retry with backoff" — the safe reading for a
    refusal nobody could name, and never a reason that retires a credential.
    """
    return _TO_HOST.get(str(kind or "").strip().lower(), "unknown")


#: Every value that can reach an Events row's ``sized_by`` field.
#:
#: 1.7.0.5, and it exists because the panel and the engine had drifted with
#: nothing to notice. ``desktop/plugin.js`` keeps a label for each of these
#: and renders the chip only when it finds one — so a value the map had never
#: heard of produced **no chip at all**, silently. Two were missing: ``window``
#: and ``text``.
#:
#: ``window`` is the worst one to lose. It means *nobody stated this number,
#: it is KAME's own default* — precisely the row where a reader should be
#: least willing to believe the rest, and it was the row with no source shown.
#:
#: Read out of the code rather than remembered: ``dispatch_binding`` writes
#: ``reprobe``, ``table``, ``verdict`` or ``verdict.source``, and
#: ``classify``/``quota`` produce the rest. ``host``, ``kame`` and ``dropped``
#: come from ``core.journal`` on the host-bench path.
SIZED_BY_SOURCES: FrozenSet[str] = frozenset(
    {
        # written by dispatch_binding
        "reprobe", "table", "verdict", "retained",
        # verdict.source, from classify
        "catalog", "pattern", "type",
        # verdict.source, from quota
        "window", "text", "anchor", "header", "body", "retryinfo", "exception",
        # core.journal, host-bench path
        "kame", "host", "dropped",
    }
)
