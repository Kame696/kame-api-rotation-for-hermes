"""What a background (auxiliary) job may spend: requests, seconds, waiting.

1.8.2.9 (decisions/0032 A). Measured on the owner's gateway, 2026-10-23 22:41-22:53: one
``title_generation`` call on the best model made 167 attempts over 744.7 s, 60 quarantines among
them a 3,600 s daily refusal, because a background call inherited the chat's wait. Nobody waits on a
title, and every attempt spends the per-model daily quota the chat needs.

Pure functions; the transport applies them to calls whose channel is "secondary" and never to the chat.
"""
from __future__ import annotations

import math
import os
import time
from typing import Any, NamedTuple, Optional

#: A closed list. Anything else - an unknown task, the host's generic "auxiliary" label, a name guessed
#: from the model - is "important": a wrong guess toward cosmetic would starve a job that matters.
COSMETIC = frozenset({"title_generation", "profile_describer", "background_review"})

FLOOR_S = 5.0
#: After a stop, the same model and class answer "stopped" for this long without a request: the host's
#: own same-provider retry (1 s, 2 s backoff) or the next title attempt costs nothing.
MEMO_S = 30.0


#: Stems of the words the host matches in an error text to decide "payment", "rate limit" or "auth"
#: (``agent/auxiliary_client.py``, ``agent/error_classifier.py``). A name that contains one is shown as "?".
_HOST_WORDS = ("credit", "quota", "billing", "payment", "fund", "balance", "limit", "free", "tier",
               "retry", "again", "reset", "exhaust", "unauthor", "apikey", "api_key", "subscription",
               "budget", "upgrade", "daily", "weekly", "tokens", "usage", "401", "402", "403", "404",
               "429", "afford", "deactivat", "plan", "spending", "overload", "capacity")


class Limit(NamedTuple):
    cap_s: float
    max_requests: int
    may_wait: bool
    default_host_s: float


LIMITS = {
    "cosmetic": Limit(cap_s=30.0, max_requests=3, may_wait=False, default_host_s=30.0),
    "important": Limit(cap_s=300.0, max_requests=6, may_wait=True, default_host_s=120.0),
}


class Budget(NamedTuple):
    klass: str
    total_s: float
    max_requests: int
    may_wait: bool


def _seconds(value: Any) -> float:
    """A positive finite number of seconds, or 0.0 when ``value`` is not one (``bool`` is not)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    value = float(value)
    return value if math.isfinite(value) and value > 0 else 0.0


def budget_for(task: str, task_known: bool, host_timeout: Any, user_cap: float) -> Budget:
    """The budget of one background call.

    ``task_known`` is true only when the host (or the client) named the task; a label guessed from the
    model name never decides the class. ``host_timeout`` is the ``timeout`` Hermes put in the request
    (30 s by default, more for compression); ``user_cap`` is the user's own total bound (0 = none).
    """
    name = str(task or "").strip().lower()
    klass = "cosmetic" if task_known and name in COSMETIC else "important"
    limit = LIMITS[klass]
    host = _seconds(host_timeout) or limit.default_host_s
    total = min(host, limit.cap_s)
    cap = _seconds(user_cap)
    if cap:
        total = min(total, cap)
    return Budget(klass, max(FLOOR_S, total), limit.max_requests, limit.may_wait)


def stop_message(task: str, model: str, tries: int, seconds: float) -> str:
    """The text of the error a stopped background job raises.

    It must hold none of the host's payment, rate-limit or auth words (``agent/auxiliary_client.py``
    matches them as substrings), or the host would retry the call and rotate its own credentials.
    Only identifier characters of the task and model are kept for the same reason."""
    def clean(text: Any) -> str:
        kept = "".join(c for c in str(text or "") if c.isalnum() or c in "._-/:")[:60]
        if not kept or any(word in kept.lower() for word in _HOST_WORDS):
            return "?"
        return kept

    return (f"kame: background job {clean(task)} on {clean(model)} timed out after {int(tries)} "
            f"tries in {max(0, int(seconds))}s - its keys are kept for your chat")


#: The suggestion appears only when background jobs are a real share of the best model's requests.
SUGGEST_MIN_SHARE = 0.15
SUGGEST_MIN_SIDE = 20


def suggest(models: Any, primary: Any, chain: Any, written: Any, since: float) -> "Optional[str]":
    """A read-only sentence: which background job to move off the best model, and to which model.

    ``models`` is the transport's per-identity tally (``{identity: {"main", "side", "tasks"}}``);
    ``primary`` and ``chain`` are the user's own list from Hermes' config; ``written(entry)`` gives a
    model's name as written there. The named model is ALWAYS a member of that list, on the same
    provider as the best one - a cosmetic task goes to the last such entry, an important one to the
    first fallback - and the sentence says KAME changes nothing. ``None`` when there is nothing worth
    saying (under 15% of requests or under 20 background requests; no same-provider model to name).
    """
    try:
        if not primary or not chain:
            return None
        main = side = 0
        tasks: dict = {}
        for identity, row in dict(models or {}).items():
            if identity == primary or str(identity).startswith(str(primary) + ":"):
                main += int(row.get("main", 0))
                side += int(row.get("side", 0))
                for name, count in dict(row.get("tasks") or {}).items():
                    tasks[name] = tasks.get(name, 0) + int(count)
        total = main + side
        if side < SUGGEST_MIN_SIDE or total <= 0 or side / total < SUGGEST_MIN_SHARE or not tasks:
            return None
        task = max(sorted(tasks), key=lambda name: tasks[name])
        provider = str(primary).split(":", 1)[0]
        same = [entry for entry in chain if str(entry).split(":", 1)[0] == provider]
        if not same:
            return None
        cosmetic = task.strip().lower() in COSMETIC
        target = same[-1] if cosmetic else same[0]
        number = ([primary] + list(chain)).index(target) + 1
        name = str(written(target) or str(target).split(":", 1)[-1])
        best = str(written(primary) or str(primary).split(":", 1)[-1])
        clock = time.strftime("%H:%M", time.localtime(since))
        text = (f"Background jobs used {round(100.0 * side / total)}% of {best}'s requests since {clock} "
                f"({task}: {tasks[task]}). To keep your best model for your chats you could move {task} to "
                f"{name} (number {number} of your list): in Hermes' config.yaml set "
                f"`auxiliary.{task}.provider: {provider}` and `auxiliary.{task}.model: {name}`.")
        if task == "title_generation":
            text += " Or turn titles off with `auxiliary.title_generation.enabled: false`."
        return text + " KAME never changes this for you."
    except Exception:
        return None


def unbounded() -> bool:
    """The expert switch ``KAME_AUX_UNBOUNDED=1`` restores 1.8.2.8: background calls wait like the chat."""
    return os.environ.get("KAME_AUX_UNBOUNDED", "").strip().lower() in ("1", "true", "yes", "on")
