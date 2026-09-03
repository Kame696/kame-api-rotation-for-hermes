"""Which call is in flight — the fact the credential pool is never told.

``CredentialPool`` is scoped to a provider and knows nothing about models.
Every decision it makes about a key is therefore provider-wide, while several
providers meter quota per key *per model*. The missing input is simply the
model of the call being attempted, and the host does announce it: the
``pre_api_request`` hook carries ``provider`` and ``model`` on every request.

This module is the wire between the two. It holds the announcement in a
``ContextVar`` so concurrent conversations in the same process — a chat turn
and a background task — each see their own model rather than the last one
written by whoever ran most recently.

Two rules make it safe to consult from a hot path:

* **The provider must match.** A model name means nothing outside the
  provider that issued it, and applying one pool's model to another's keys
  would release benches on evidence that does not apply. A mismatch reports
  "unknown", never a guess.
* **Unknown is a valid answer.** Callers that get ``""`` fall back to the
  host's own behaviour. Nothing here ever needs to invent a model to keep
  the plugin working; it just stops adding value for that call.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

from .core import tally
from .core.ledger import normalize_model

# (provider, model), both already normalised. A tuple rather than two vars so
# a call can never be observed half-updated: a reader that sees the provider
# sees the model that came with it.
_IN_FLIGHT: ContextVar[Tuple[str, str]] = ContextVar("kame_in_flight", default=("", ""))

# Hermes labels custom endpoints ``custom`` on the agent and ``custom:<name>``
# on the pool. The host reconciles the two by resolving the agent's base_url;
# KAME cannot see the base_url from a hook, so it accepts the family match and
# relies on the bench fingerprint to catch a wrong-endpoint release.
_CUSTOM = "custom"
_CUSTOM_PREFIX = "custom:"


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def note_call(provider: object, model: object) -> None:
    """Record the provider and model of the request about to be sent."""
    _IN_FLIGHT.set((_norm(provider), _norm(model)))


def forget_call() -> None:
    """Clear the announcement — used by tests and by explicit teardown."""
    _IN_FLIGHT.set(("", ""))


@contextmanager
def scoped_call(provider: object, model: object) -> Iterator[None]:
    """Announce a call for exactly as long as it is being made.

    ``note_call`` is right for the main conversation loop, where every request
    announces itself and the next one overwrites the last. Auxiliary calls —
    summarisation, titling, compression — run *inside* a main turn, so an
    announcement left standing afterwards would attribute the main model's
    next failure to the auxiliary model and file a bench against the wrong
    quota. Restoring the previous value keeps each nesting level honest.
    """
    token = _IN_FLIGHT.set((_norm(provider), _norm(model)))
    try:
        yield
    finally:
        _IN_FLIGHT.reset(token)


def in_flight() -> Tuple[str, str]:
    return _IN_FLIGHT.get()


def providers_match(pool_provider: object, call_provider: object) -> bool:
    """Whether a pool's provider and a call's provider are the same scope.

    Mirrors the host's own guard in ``_recover_with_credential_pool``: exact
    match, with the ``custom`` / ``custom:<name>`` pair accepted as one
    family. An empty side is never a match — the host treats an unscoped pool
    as applying to everything, but KAME needs positive identification before
    it acts on a bench.
    """
    pool = _norm(pool_provider)
    call = _norm(call_provider)
    if not pool or not call:
        return False
    if pool == call:
        return True
    if call == _CUSTOM and pool.startswith(_CUSTOM_PREFIX):
        return True
    if pool == _CUSTOM and call.startswith(_CUSTOM_PREFIX):
        return True
    return False


def model_for(pool_provider: object) -> str:
    """The in-flight model, but only if it belongs to this pool's provider.

    Returns ``""`` when nothing has been announced, when the announcement
    carried no model, or when it came from a different provider. All three
    mean the same thing to a caller: decide as the host would.
    """
    provider, model = _IN_FLIGHT.get()
    if not model:
        return ""
    if not providers_match(pool_provider, provider):
        return ""
    return model


def describe(pool_provider: Optional[str] = None) -> str:
    """One short line for logs — never includes credential material."""
    provider, model = _IN_FLIGHT.get()
    if not provider and not model:
        return "no call announced"
    if pool_provider is not None and not providers_match(pool_provider, provider):
        return f"in flight {provider}/{model}, pool is {_norm(pool_provider)}"
    return f"{provider}/{model}"


# ── which model a bench about to be written belongs to ────────────────────
# The announcement above is scoped to the *call*, and unwinds the moment the
# call returns or raises. For the main lane that is exactly right: the bench
# is written from inside the same request, with the announcement still up.
#
# The auxiliary lane is the exception, and it is the reason this exists.
# ``agent/auxiliary_client`` lets the relay raise, catches it *outside* the
# relay, and only then calls ``_recover_provider_pool`` -> the pool
# (``:4560``, ``:4572``). By then ``scoped_call`` has already restored the
# main model's announcement — so the bench earned by a summarisation call on
# a small model was being written against the conversation's model, on a key
# the small model had not spent. That is the per-model regression this plugin
# exists to fix, running in the one lane that fires no hooks.
#
# The announcement itself is deliberately *not* held open past the call: an
# announcement that outlived its scope would mis-attribute the next main-lane
# failure, which is a worse error in the other direction. This is a separate,
# narrower fact — "the next bench on this provider belongs to that model" —
# with the same provider match and the same short expiry as a verdict, set
# only where a lane is known to bench outside its own call.

_BENCH_MODEL: ContextVar[Tuple[str, str, float]] = ContextVar(
    "kame_bench_model", default=("", "", 0.0)
)


def note_bench_model(provider: object, model: object, *, now: float) -> None:
    """Say which model owns the bench the host is about to write."""
    _BENCH_MODEL.set((_norm(provider), normalize_model(model), float(now)))


def bench_model_for(pool_provider: object, *, now: float) -> str:
    """That model, if the hint is fresh and belongs to this pool.

    Returns ``""`` in every other case, which puts the caller straight back
    on ``model_for`` — the announcement — exactly as before this existed.
    """
    provider, model, at = _BENCH_MODEL.get()
    if not model:
        return ""
    if now - at > JUDGEMENT_TTL_SECONDS:
        _BENCH_MODEL.set(("", "", 0.0))
        return ""
    if not providers_match(pool_provider, provider):
        return ""
    return model


def forget_bench_model() -> None:
    _BENCH_MODEL.set(("", "", 0.0))


# ── the verdict that is about to become a bench ───────────────────────────
# KAME classifies a failure roughly fifty lines before the pool writes the
# cooldown that classification produced. The classifier knows *why* — which
# quota window, and from which piece of evidence — and the pool knows the
# deadline that actually got stored. Neither knows the other's half, and the
# journal wants both on one row.
#
# Passing it through a ContextVar rather than a module variable is the same
# reasoning as the announcement above: the hook and the bench run in one
# thread, one after the other, so the value is delivered exactly where it was
# produced and two concurrent turns cannot swap verdicts.


@dataclass(frozen=True)
class Judgement:
    """What KAME concluded about one failure, waiting to be filed."""

    provider: str
    model: str
    window: str
    source: str
    reset_at: Optional[float]
    at: float
    # Whether the refusal reaches past the model that earned it. Defaulted so
    # a caller written against the older signature still constructs.
    scope: str = "unknown"
    # The window the provider's own counter named, which is not the same
    # question as ``window`` above: that one is what KAME decided to act on,
    # this one is what the provider said. Filed side by side so a disagreement
    # between them can be counted later instead of being argued about.
    stated: str = "unknown"


_JUDGEMENT: ContextVar[Optional[Judgement]] = ContextVar("kame_judgement", default=None)

# How long a verdict stays claimable. The pool writes the bench in the same
# call stack, microseconds later; anything still sitting here after this long
# belongs to a failure that never became a bench — not retryable, or rotated
# without exhausting — and must not be attached to the next one.
JUDGEMENT_TTL_SECONDS = 30.0


def note_judgement(
    provider: object,
    model: object,
    *,
    window: str,
    source: str,
    reset_at: Optional[float],
    now: float,
    scope: str = "unknown",
    stated: str = "unknown",
) -> None:
    _JUDGEMENT.set(
        Judgement(
            provider=_norm(provider),
            model=normalize_model(model),
            window=str(window or "unknown"),
            source=str(source or ""),
            reset_at=reset_at,
            at=float(now),
            scope=str(scope or "unknown"),
            stated=str(stated or "unknown"),
        )
    )


def _matching_judgement(
    pool_provider: object, model: object, *, now: float
) -> Optional[Judgement]:
    """The pending verdict if it describes this call, without claiming it."""
    judgement = _JUDGEMENT.get()
    if judgement is None:
        return None
    if now - judgement.at > JUDGEMENT_TTL_SECONDS:
        _JUDGEMENT.set(None)
        return None
    if not providers_match(pool_provider, judgement.provider):
        return None
    if judgement.model and judgement.model != normalize_model(model):
        return None
    return judgement


def take_judgement(pool_provider: object, model: object, *, now: float) -> Optional[Judgement]:
    """Claim the pending verdict for this pool and model, once.

    Returns ``None`` unless the verdict is fresh and describes the same call
    the pool is about to bench. Claimed verdicts are cleared so a second
    bench — a different key failing later in the same turn — is recorded as
    what it is rather than inheriting the first one's reasoning.
    """
    judgement = _matching_judgement(pool_provider, model, now=now)
    if judgement is None:
        return None
    _JUDGEMENT.set(None)
    return judgement


def peek_judgement(pool_provider: object, model: object, *, now: float) -> Optional[Judgement]:
    """The same verdict, read without consuming it.

    There are two readers now and they run in this order, both inside one
    call to the pool's ``_mark_exhausted``:

    1. the **bench**, on the way in, which needs the deadline *before* the
       entry is written — that is the only moment at which the host will
       accept it;
    2. the **journal**, on the way out, which needs the same verdict to say
       whether the deadline that got stored is the one KAME asked for.

    ``take_judgement`` clears as it reads, which is right for a single reader
    and wrong for two: the first would have swallowed the verdict and the
    second would have recorded ``sized_by: host`` for a bench KAME had just
    sized itself. So the writer peeks and the recorder claims.
    """
    return _matching_judgement(pool_provider, model, now=now)


def forget_judgement() -> None:
    _JUDGEMENT.set(None)


# ── which key the pool last handed out ────────────────────────────────────
# The success side of the journal needs a credential, and the hook that
# reports a successful call carries only the provider and the model. The pool
# does know — it sets ``_current_id`` on every selection — but the hook has no
# pool in reach, so the selection is mirrored here as it happens.
#
# A module-level dict, not a ContextVar, because selection and use are often
# in different contexts: the main conversation picks a key once and then makes
# hundreds of calls from wherever the turn happens to run.
#
# **Best-effort by construction, and used for nothing but statistics.** Two
# agents on the same provider in parallel can each overwrite the other's
# entry, so an occasional observation lands against the wrong key of the right
# provider. That is tolerable in a sample of many and would not be tolerable
# in a release decision, which is why no release decision reads this.

_SELECTED: Dict[str, str] = {}
_SELECTED_LOCK = threading.Lock()

# Providers are a closed, small set; the bound only exists so a pathological
# caller inventing pool names cannot grow this without limit.
_MAX_TRACKED_PROVIDERS = 64


def note_selection(provider: object, credential_id: object) -> None:
    name = _norm(provider)
    identifier = str(credential_id or "").strip()
    if not name or not identifier:
        return
    with _SELECTED_LOCK:
        if name not in _SELECTED and len(_SELECTED) >= _MAX_TRACKED_PROVIDERS:
            return
        _SELECTED[name] = identifier


def selected_for(provider: object) -> str:
    """The last credential this provider's pool handed out, or ``""``."""
    with _SELECTED_LOCK:
        return _SELECTED.get(_norm(provider), "")


def forget_selections() -> None:
    with _SELECTED_LOCK:
        _SELECTED.clear()


# ── the key KAME itself put back on the table ─────────────────────────────
# Refuting a bench is a release decision, and the mirror above is explicitly
# not good enough for one: it records whichever key the pool most recently
# handed out, which two concurrent agents can overwrite for each other.
#
# A probe does not have that problem, and the difference is not a matter of
# degree. The escape hatch reaches the point of issuing one only when the
# model in flight has *nothing else usable at all*, and what it returns is a
# single entry it chose by name. There is no second candidate for the next
# successful call on that provider to have come from — not because a race is
# unlikely, but because the list the pool was given had one item in it.
#
# Claimed once and expiring quickly, for the same reason the verdict above is:
# a probe that was issued and never answered must not be credited to whatever
# succeeds later.


@dataclass(frozen=True)
class Probe:
    """A benched credential handed back on purpose, to test its deadline."""

    provider: str
    credential_id: str
    model: str
    at: float


# Generous next to ``JUDGEMENT_TTL_SECONDS`` because this one spans a real
# network call rather than fifty lines of the same call stack — a slow model
# answering a long prompt is still answering the probe. Beyond it the attempt
# is treated as unanswered, which costs a refutation and never invents one.
PROBE_TTL_SECONDS = 300.0

_PROBES: Dict[str, Probe] = {}
_PROBES_LOCK = threading.Lock()


def note_probe_issued(
    provider: object, credential_id: object, model: object, *, now: float
) -> None:
    name = _norm(provider)
    identifier = str(credential_id or "").strip()
    if not name or not identifier:
        return
    with _PROBES_LOCK:
        if name not in _PROBES and len(_PROBES) >= _MAX_TRACKED_PROVIDERS:
            return
        _PROBES[name] = Probe(
            provider=name,
            credential_id=identifier,
            model=normalize_model(model),
            at=float(now),
        )


def take_probe(provider: object, *, now: float) -> Optional[Probe]:
    """Claim the outstanding probe for this provider, once."""
    name = _norm(provider)
    with _PROBES_LOCK:
        pending = _PROBES.get(name)
        if pending is None:
            return None
        del _PROBES[name]
    if now - pending.at > PROBE_TTL_SECONDS:
        return None
    return pending


def forget_probes() -> None:
    with _PROBES_LOCK:
        _PROBES.clear()


# ── what the classifier was asked, and what it could answer ───────────────
# Lives here rather than on the binding because the classification half of
# the plugin runs whether or not the pool binding installed — an install that
# refused the pool still sizes cooldowns, and that is exactly the install
# whose numbers are hardest to see. Module state, like everything else above,
# so `/kame-quota` can read it without being handed a reference.

_TALLY = tally.Tally()


def note_classification(provider: object, status_code: object, *, sized: bool) -> None:
    _TALLY.note(provider, status_code, sized=sized)


def classifications() -> List[tally.Seen]:
    return _TALLY.snapshot()


def forget_classifications() -> None:
    _TALLY.clear()


# ── answers that carried nothing ──────────────────────────────────────────
# A second tally rather than a row in the first one. What the first counts is
# failures the host asked KAME about; these are successes KAME declined to
# read anything into. Filing them together would need a fake status code to
# key them under and would put "declined to size" next to "declined to
# believe", which are opposite decisions about opposite events.
#
# Same container, so the properties that matter are the same ones already
# tested: bounded, thread-safe, and holding integers and a provider name.

_QUIET = tally.Tally()


def note_empty_answer(provider: object) -> None:
    """Count a call that returned with no content and no tool call."""
    _QUIET.note(provider, None, sized=False)


def empty_answers() -> List[tally.Seen]:
    return _QUIET.snapshot()


def forget_empty_answers() -> None:
    _QUIET.clear()
