"""Turn per-model memory into a list of edits for a provider-scoped pool.

The host keeps one cooldown per credential. The ledger knows that cooldown
was earned on a particular model. This module compares the two and says what
would have to change for the pool to be right *for the model about to be
called* — nothing more. It performs no edits and imports nothing from a
framework; the binding layer applies the plan.

Two edits exist:

``release``
    The host has a key benched, the fingerprint proves KAME wrote it, and the
    ledger says the key is not actually spent here — either because the bench
    was earned on a different model, or because the bench was tested and the
    key worked anyway. The second reason is the stronger one: a prediction
    that has been contradicted by an observation is finished, and continuing
    to hold a key against it is holding it against the evidence. That is also
    why a refuted bench is kept rather than deleted — the deadline in it is
    what proves the host's cooldown is KAME's to unwind, and a released key
    needs that proof on every selection until the host's own clock runs out.

``hold``
    The ledger says the key is spent on the model now in play and the host
    does not know it — normally because KAME released it for a different
    model earlier and the agent has come back. Restore the fact.

A bench the provider described as covering the whole key is never released
for another model, whichever model earned it. Per-model release is right only
where the quota is per-model, and the provider is the one who says so: Google
meters ``PerProjectPerModel`` and OpenRouter meters the free tier per account.
Applying one provider's shape to the other is the same class of error as the
host benching provider-wide — the direction of the mistake changes, the cost
does not. Silence is read as per-model, because that is what every version
before v0.0.8 assumed and it is the reading that cannot lock anyone out.

**Every write is gated on the fingerprint.** A bench KAME did not create is
never released. If the host's stored deadline does not match one this ledger
recorded, some other writer owns that bench — a host-side classification, a
second process, a hand edit — and un-benching it would resurrect a key that
another subsystem deliberately retired. Releasing somebody else's bench is
the one failure mode of this design that costs more than it saves, so the
gate is the first thing checked, not the last.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence

from .ledger import Ledger, normalize_model

# The host writes ``last_error_reset_at`` from the ``reset_at`` KAME returned,
# so an exact match is expected. The tolerance absorbs a float that has been
# through a JSON round-trip; it is far below any real cooldown, so it cannot
# make two genuinely different deadlines look like one.
FINGERPRINT_TOLERANCE_SECONDS = 1.0

RELEASE = "release"
HOLD = "hold"

# Host status values. Mirrored rather than imported so the decision rules stay
# runnable without Hermes; the binding layer maps the real constants onto
# these when it builds the entry views.
STATUS_EXHAUSTED = "exhausted"
STATUS_DEAD = "dead"


@dataclass(frozen=True)
class EntryView:
    """What the planner needs to know about one pooled credential.

    A deliberately narrow view: an id, whether the host considers it spent,
    and until when. No key material crosses this boundary — the binding layer
    reads the pool and passes ids, so a bug here cannot leak a credential.
    """

    credential_id: str
    status: Optional[str] = None
    reset_at: Optional[float] = None

    @property
    def is_dead(self) -> bool:
        return (self.status or "").strip().lower() == STATUS_DEAD

    @property
    def is_benched(self) -> bool:
        return (self.status or "").strip().lower() == STATUS_EXHAUSTED


@dataclass(frozen=True)
class Action:
    """One edit to apply to the pool, with the reason it was decided."""

    kind: str
    credential_id: str
    reset_at: Optional[float]
    why: str


def _fingerprint_matches(entry: EntryView, ledger: Ledger, now: float) -> Optional[str]:
    """Return the model whose bench explains the host's deadline, if ours.

    Matching on the deadline is what makes ownership provable without the
    host storing a plugin marker. KAME chose that number from the provider's
    own words; the odds of an unrelated writer landing within a second of it
    are negligible, and the consequence of a miss is only that KAME declines
    to act.
    """
    host_reset = entry.reset_at
    if host_reset is None:
        # A bench with no explicit deadline is a default-TTL bench. KAME never
        # writes one of those, so it cannot be ours.
        return None
    for bench in ledger.live_benches_for(entry.credential_id, now):
        if abs(bench.reset_at - host_reset) <= FINGERPRINT_TOLERANCE_SECONDS:
            return bench.model
    return None


def plan(
    entries: Iterable[EntryView],
    ledger: Ledger,
    *,
    model: Any,
    now: float,
) -> List[Action]:
    """Decide what the pool should look like for ``model`` right now.

    Returns an empty list when nothing needs to change, which is the common
    case and the one worth being cheap: the binding layer skips its write
    entirely on an empty plan, so an untouched pool costs no disk I/O.
    """
    model_name = normalize_model(model)
    if not model_name:
        # Without a model there is no per-model question to answer. Every
        # bench is then simply the host's, and the host is right.
        return []

    actions: List[Action] = []
    for entry in entries:
        if entry.is_dead:
            # A revoked or invalid credential is not a quota problem and never
            # comes back on a timer. Releasing one would put a key that cannot
            # authenticate back into rotation, to fail again immediately.
            continue

        # Not ``benched_until``: a refusal the provider said covers the whole
        # key is spent here too, whichever model earned it. Releasing on that
        # evidence is the same mistake as benching provider-wide without it,
        # pointed the other way.
        spent_here = ledger.spent_until(entry.credential_id, model_name, now)
        shared = ledger.shared_bench_for(entry.credential_id, now)

        # Status alone is not the question — the host counts an entry as
        # usable once its stored deadline has passed, whether or not anything
        # has got round to clearing the status yet. Reading the stale status as
        # "still benched" would send the whole decision down the ownership
        # branch and skip the hold, handing back a key the ledger knows is
        # spent here.
        still_held = entry.is_benched and (
            entry.reset_at is None or entry.reset_at > now
        )
        if still_held:
            owner_model = _fingerprint_matches(entry, ledger, now)
            if owner_model is None:
                # Not ours. Leave it exactly as found.
                continue
            if spent_here is None:
                tested = ledger.find(entry.credential_id, model_name)
                actions.append(
                    Action(
                        kind=RELEASE,
                        credential_id=entry.credential_id,
                        reset_at=None,
                        why=(
                            f"tested on {model_name} and it worked"
                            if tested is not None and tested.is_refuted
                            else f"benched on {owner_model}, unspent on {model_name}"
                        ),
                    )
                )
            elif abs(spent_here - (entry.reset_at or 0.0)) > FINGERPRINT_TOLERANCE_SECONDS:
                # Ours, spent here too, but the stored deadline is not the one
                # that governs this model. Two ways that happens, and they are
                # different sentences: the number belongs to another model's
                # bench, or it belongs to this model's and KAME is holding the
                # key past it (v0.1.0). Saying "the deadline was X's" when X
                # *is* the model in flight reads as a per-model correction that
                # did not happen, in the one log a reader consults to find out
                # why a key is missing.
                actions.append(
                    Action(
                        kind=HOLD,
                        credential_id=entry.credential_id,
                        reset_at=spent_here,
                        why=(
                            f"held past the host's deadline on {model_name}"
                            if owner_model == model_name
                            else f"deadline was {owner_model}'s, {model_name} resets later"
                        ),
                    )
                )
            continue

        if spent_here is not None:
            # The host has it available; the ledger knows better. This is the
            # return leg — KAME released this key for another model and the
            # agent has switched back to the one it is actually spent on.
            actions.append(
                Action(
                    kind=HOLD,
                    credential_id=entry.credential_id,
                    reset_at=spent_here,
                    why=(
                        f"{shared.model}'s limit covers every model"
                        if shared is not None and shared.model != model_name
                        else f"still spent on {model_name}"
                    ),
                )
            )

    return actions


def would_leave_available(entries: Sequence[EntryView], actions: Sequence[Action]) -> int:
    """How many credentials remain usable once ``actions`` are applied.

    The binding layer logs this. A plan that benches the last key is not
    wrong — if every key really is spent on this model, saying so is what
    lets the host fall back to a different model instead of hammering a wall
    — but it is worth being able to see in a log after the fact.
    """
    held = {action.credential_id for action in actions if action.kind == HOLD}
    released = {action.credential_id for action in actions if action.kind == RELEASE}
    available = 0
    for entry in entries:
        if entry.is_dead:
            continue
        if entry.credential_id in held:
            continue
        if entry.credential_id in released or not entry.is_benched:
            available += 1
    return available
