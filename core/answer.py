"""Did the call that came back actually carry an answer?

Everywhere else in this plugin a *failure* is the event. This module is about
the one success that is not evidence of anything.

``post_api_request`` fires whenever a call returns without raising, and this
plugin reads that as proof: a key that answers while KAME says it is spent has
disproved KAME's deadline, and the bench is retired **for good** rather than
re-tested every few minutes (v0.0.9, "believe the answer"). That rule is right
and it stays. It rests on a premise, though — that a call which returned is a
call that produced something.

The Agent Zero engine this rotation was ported from stopped believing that
premise in its v1.0.9, and the reason is the same free-tier Google traffic this
plugin was built for: a squeezed key does not always answer with a 429. It
answers 200 with nothing in it. ``_kame_result_is_empty`` (kame_engine.py:1533)
exists for exactly that, and there the engine owns the call loop, so an empty
answer is a failure and the next key is tried.

Here the plugin owns no call loop and cannot retry — ``post_api_request`` is
handed metrics and no way to change the outcome (``run_agent.py:2687``: "Token
buckets for ``post_api_request`` plugins (no raw ``response`` object)", and
``moa_loop.py:1633``: "Read-only: a MoA turn's post_api_request hook must not
disturb the accounting"). What it can do is refuse to *count* it. An empty
answer is not proof the key works, so it settles no probe, closes no bench and
is filed as nothing observed. The bench stands and the escape hatch offers the
key again on its own schedule, which is what would have happened had the call
never been made — the honest outcome for an event that said nothing.

Deliberately not treated as a failure either. Nothing refused anything: there
is no status, no deadline, no window, and inventing a refusal the provider
never sent would put a number in the journal that no provider ever said. It is
counted and shown, and that is all.

Pure and framework-free like the rest of ``core``: two integers in, one
decision out.
"""

from __future__ import annotations

from typing import Optional


def _count(value: object) -> Optional[int]:
    """``value`` as a non-negative count, or ``None`` when it says nothing.

    ``None`` is the important return. It means the host did not report this
    number — an older Hermes, a different dispatch site, a build that renames
    the field — and it is *not* the same as zero. Read as zero, a Hermes that
    stopped passing the field would turn every successful call in the process
    into an empty one, and the release path that lets a wrongly benched key
    back into rotation would go quiet everywhere at once. Unknown has to stay
    unknown for that reason.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def carried_nothing(*, content_chars: object, tool_calls: object) -> bool:
    """True only when the host reported an answer with no content and no calls.

    Both numbers have to be present and both have to be zero. Either one
    missing means the question cannot be answered, and the answer to a question
    that cannot be answered is that this is an ordinary success — the behaviour
    of every version before this one, which is the safe direction to fail in.

    A tool call with no prose is a full answer and the commonest shape of one:
    the model chose to act instead of talk. Only the turn that produced neither
    is empty.
    """
    chars = _count(content_chars)
    calls = _count(tool_calls)
    if chars is None or calls is None:
        return False
    return chars == 0 and calls == 0
