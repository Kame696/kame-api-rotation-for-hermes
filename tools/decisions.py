"""What KAME decides, for every refusal shape it has ever met — v1.6.0.2.

Every other tool in here answers *does it still work*. This one answers the
question that actually goes wrong: **what number comes out, and where did it
come from?**

The gap it closes is the one that produced 1.6.0.2. The unit tests assert
individual branches, the corpus asserts KAME does not disturb the host, and the
live harnesses prove a socket. None of them puts the whole decision table on one
screen, so a payload that started answering differently — because a provider
reworded a sentence, or because the *host* appended one — changed nothing that
anybody was looking at.

Run it before and after any classifier change:

    python tools/decisions.py                 # the table
    python tools/decisions.py --json  > a.json
    ... make the change ...
    python tools/decisions.py --check a.json  # what moved, and by how much

`--check` is the point. A diff of this table is a diff of the plugin's opinions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
sys.path.insert(0, str(PLUGIN_DIR))

from core.classify import classify  # noqa: E402
from core.quota import (  # noqa: E402
    DEFAULT_UNSIZED_THROTTLE_BENCH_SECONDS as POOL_FLOOR_S,
)

NOW = 1_000_000.0

# --- the host's own appended guidance ---------------------------------------
# Copied **verbatim** from the installed host, not paraphrased: these are
# ``_FREE_TIER_GUIDANCE`` and ``_STANDARD_KEY_GUIDANCE`` in
# ``agent/gemini_native_adapter.py`` (:156 and :189), appended to the error
# message itself at :907 and :913 — so they arrive at this plugin's hook as
# part of ``error_message``, indistinguishable from what Google sent.
#
# A paraphrase here is worse than nothing. The first draft of this file
# shortened the free-tier text and lost the sentence "Enable billing on your
# Google Cloud project", which is the whole defect: that sentence alone, on any
# status, is a complete ``billing`` verdict. ``tools/host_prose.py`` checks
# these against the installed host so a reworded footer is a failing gate
# rather than a silent miss.
HERMES_FREE_TIER_FOOTER = (
    "\n\nYour Google API key is on the free tier (a few hundred requests/day "
    "for Gemini Flash models). Hermes typically makes 3-10 API calls per user turn, "
    "so the free tier is exhausted in a handful of messages and cannot sustain "
    "an agent session. Enable billing on your Google Cloud project and "
    "regenerate the key in a billing-enabled project: "
    "https://aistudio.google.com/apikey"
)

HERMES_STANDARD_KEY_FOOTER = (
    "\n\nGoogle Gemini rejected this API key's type — you do NOT need OAuth. "
    "Google began rejecting legacy 'Standard' Google Cloud keys for the "
    "Gemini API on June 19, 2026, and all Standard keys stop working in "
    "September 2026. Open https://aistudio.google.com/api-keys, check the "
    "key's type and status, and create a replacement Gemini API key (or, as "
    "a temporary bridge, restrict the Standard key to "
    "generativelanguage.googleapis.com). Then update GEMINI_API_KEY / "
    "GOOGLE_API_KEY in ~/.hermes/.env and restart your session. "
    "Details: https://ai.google.dev/gemini-api/docs/api-key"
)

#: The two shapes the owner's 2026-09-03 14:39 run actually produced, copied
#: from ``logs/agent.log.1``. Neither states a delay, and both are the case
#: this release exists for: a verdict with no number is not a short bench, it
#: is the host's one-hour default (``credential_pool.py:125``).
RUN_429 = ("Gemini HTTP 429 (RESOURCE_EXHAUSTED): Resource has been exhausted "
           "(e.g. check quota).")
RUN_503 = ("Gemini HTTP 503 (UNAVAILABLE): This model is currently "
           "experiencing high demand. Spikes in demand are usually temporary. "
           "Please try again later.")

GEMINI_429 = (
    "Gemini HTTP 429 (RESOURCE_EXHAUSTED): You exceeded your current quota, "
    "please check your plan and billing details. For more information on this "
    "error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To "
    "monitor your current usage, head to: https://ai.dev/rate-limit. \n"
    "* Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_input_token_count, limit: 250000, model: "
    "gemini-3.7-flash\nPlease retry in {delay}s."
)


class Err(Exception):
    def __init__(self, msg, *, status=None, details=None, code="", body=None):
        super().__init__(msg)
        if status is not None:
            self.status_code = status
        if details is not None:
            self.details = details
        if code:
            self.code = code
        if body is not None:
            self.body = body


#: Each case is (group, name, kwargs-for-classify, what a human expects).
#: The expectation column is prose on purpose — it is what a reader checks the
#: number against, and a number that only agrees with another number proves
#: nothing.
CASES = [
    # ---- the one that produced this release -------------------------------
    ("google/free-tier", "429, provider states 6.89s",
     dict(status_code=429, error_message=GEMINI_429.format(delay="6.89161299"),
          error_type="GeminiAPIError", error_code="gemini_rate_limited"),
     "~7s — the provider said so"),
    ("google/free-tier", "429, states 6.89s, + HOST footer",
     dict(status_code=429,
          error_message=GEMINI_429.format(delay="6.89161299")
                        + HERMES_FREE_TIER_FOOTER,
          error_type="GeminiAPIError", error_code="gemini_rate_limited"),
     "~7s — the footer is Hermes' text, not evidence"),
    ("google/free-tier", "429, states 46.83s, + HOST footer",
     dict(status_code=429,
          error_message=GEMINI_429.format(delay="46.834848218")
                        + HERMES_FREE_TIER_FOOTER,
          error_type="GeminiAPIError", error_code="gemini_rate_limited"),
     "~47s — the provider said so"),
    ("google/free-tier", "HOST free-tier footer ALONE, on a 500",
     dict(status_code=500, error_message="Internal error." + HERMES_FREE_TIER_FOOTER),
     "declined — the host's advice is not a provider verdict"),
    ("google/free-tier", "HOST standard-key footer ALONE, on a 500",
     dict(status_code=500, error_message="Internal error." + HERMES_STANDARD_KEY_FOOTER),
     "declined — the host's advice is not a provider verdict"),

    # ---- the two shapes the owner's real run produced ----------------------
    ("the 14:39 run", "429 bare RESOURCE_EXHAUSTED (19x)",
     dict(status_code=429, error_message=RUN_429,
          error_type="GeminiAPIError", error_code="gemini_rate_limited"),
     "seconds — nothing was stated, rotating costs one request"),
    ("the 14:39 run", "429 bare, no error_code either",
     dict(status_code=429, error_message=RUN_429, error_type="GeminiAPIError"),
     "declined — 429 alone does not say throttle; Anthropic's is billing"),
    ("the 14:39 run", "503 high demand (8x)",
     dict(status_code=503, error_message=RUN_503, error_type="GeminiAPIError"),
     "None — congestion; the host is already right"),
    ("google/free-tier", "429 with per-minute quota id",
     dict(status_code=429, error_message="Quota exceeded",
          error_type="GeminiAPIError", error_code="gemini_rate_limited",
          error=Err("Quota exceeded", status=429, details={
              "status": "RESOURCE_EXHAUSTED", "reason": "RATE_LIMIT_EXCEEDED",
              "metadata": {"quota_limit":
                           "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}})),
     "~1 minute — a per-minute window"),
    ("google/free-tier", "429 with per-DAY quota id",
     dict(status_code=429, error_message="Quota exceeded",
          error_type="GeminiAPIError", error_code="gemini_rate_limited",
          error=Err("Quota exceeded", status=429, details={
              "status": "RESOURCE_EXHAUSTED", "reason": "RATE_LIMIT_EXCEEDED",
              "metadata": {"quota_limit":
                           "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}})),
     "long — a daily cap really is a clock"),

    # ---- the ambiguous sentence -------------------------------------------
    ("openai", "429 out of credits",
     dict(status_code=429,
          error_message="You exceeded your current quota, please check your "
                        "plan and billing details.",
          error_type="RateLimitError", error_code="insufficient_quota"),
     "billing — waiting does not refill an account"),
    ("openai", "429 plain rate limit with Retry-After",
     dict(status_code=429, error_message="Rate limit reached for gpt-4o",
          headers={"retry-after": "21"}, error_type="RateLimitError"),
     "21s — the header is the provider's own number"),

    # ---- refusals ---------------------------------------------------------
    ("auth", "401 bare",
     dict(status_code=401, error_message="Error code: 401 - Unauthorized",
          error_type="AuthenticationError"),
     "short refusal, not a dead key"),
    ("auth", "400 API key not valid (Google)",
     dict(status_code=400,
          error_message="API key not valid. Please pass a valid API key.",
          error_type="BadRequestError"),
     "revoked — the provider used the words"),
    ("auth", "401 Anthropic wording",
     dict(status_code=401, error_message="Your API key is invalid.",
          error_type="AuthenticationError"),
     "revoked"),
    ("auth", "403 model not authorised",
     dict(status_code=403,
          error_message="PERMISSION_DENIED: model not authorized for this key",
          error_type="PermissionDeniedError"),
     "denied — the pairing, not the key"),

    # ---- other providers --------------------------------------------------
    ("other", "Alibaba Throttling.RateQuota",
     dict(status_code=429, error_message="Throttling.RateQuota: request denied",
          error_type="RateLimitError"),
     "a spent counter"),
    ("other", "Z.AI usage limit reached",
     dict(status_code=429, error_message="Usage limit reached for your plan",
          error_type="RateLimitError"),
     "a spent counter"),
    ("other", "402 try again in 5 minutes",
     dict(status_code=402,
          error_message="Usage limit reached, try again in 5 minutes",
          error_type="APIStatusError"),
     "~5 minutes — an empty balance that named a time"),
    ("other", "429 Monthly quota reached.",
     dict(status_code=429, error_message="Monthly quota reached.",
          error_type="RateLimitError"),
     "an allowance that is gone; only another key helps"),

    # ---- transport / server ----------------------------------------------
    ("transport", "APIConnectionError, no status, no body",
     dict(status_code=None, error_message="Connection error.",
          error_type="APIConnectionError"),
     "~3s — the socket never opened"),
    ("server", "503 upstream",
     dict(status_code=503, error_message="Service Unavailable",
          error_type="APIStatusError"),
     "a few seconds; the key is fine"),
    ("server", "503 whose body mentions quota",
     dict(status_code=503,
          error_message="503 UNAVAILABLE: resource exhausted, check quota",
          error_type="APIStatusError"),
     "still a server error — a real quota is never a 5xx"),

    # ---- must be left alone ----------------------------------------------
    ("hands off", "404 model not found",
     dict(status_code=404, error_message="model `gemini-99` not found",
          error_type="NotFoundError"),
     "None — try another model, not another key"),
    ("hands off", "400 bad request",
     dict(status_code=400, error_message="Invalid JSON payload",
          error_type="BadRequestError"),
     "None — no key on earth fixes this"),
]


#: What the **host** benches a credential for when KAME hands it a verdict with
#: no deadline on it. Read from ``agent/credential_pool.py`` — 401 at :124, 429
#: at :125, everything else at :126, selected by ``_exhausted_ttl`` (:333) and
#: applied by ``_exhausted_until`` (:426) only *after* ``last_error_reset_at``
#: comes back empty.
#:
#: This is the column that matters, and the one this file did not have when
#: 1.6.0.1 shipped. ``classify.py`` says of an unsized throttle: "bench it for
#: nothing". There is no "bench for nothing" in this host — silence is an hour.
HOST_TTL_401_S = 300.0
HOST_TTL_429_S = 3600.0
HOST_TTL_DEFAULT_S = 3600.0


def host_bench(status, reason, seconds):
    """Seconds the credential is actually held, and who decided it.

    Three answers, and the middle one is 1.6.0.2's whole subject:

    ``kame``
        The verdict named a deadline. The host honours it verbatim.
    ``kame floor``
        The verdict is a throttle with no deadline. ``classify`` leaves it
        unset on purpose — that is what gives the *dispatch* rest its
        one-second floor — and ``PoolBinding._floor_for_unsized`` supplies a
        short one for the *pool*, which would otherwise take the host's.
    ``HOST FALLBACK``
        Nothing was supplied and nothing supplies it. ``_exhausted_ttl``
        applies, and for a 429 that is an hour.
    """
    if reason is None:
        return None, "host decides alone"
    if seconds is not None:
        return seconds, "kame"
    if reason == "rate_limit":
        return POOL_FLOOR_S, "kame floor"
    if status == 401:
        return HOST_TTL_401_S, "HOST FALLBACK"
    if status == 429:
        return HOST_TTL_429_S, "HOST FALLBACK"
    return HOST_TTL_DEFAULT_S, "HOST FALLBACK"


def decide(kwargs):
    """One classification, flattened to the few fields worth comparing."""
    call = dict(provider="gemini", model="gemini-3.7-flash", now_epoch=NOW)
    call.update(kwargs)
    try:
        verdict = classify(**call)
    except Exception as exc:  # a classifier that raises is the worst outcome
        return {"error": f"{type(exc).__name__}: {exc}"}
    status = kwargs.get("status_code")
    if verdict is None:
        held, by = host_bench(status, None, None)
        return {"reason": None, "held_s": held, "held_by": by}
    reset_at = getattr(verdict, "reset_at", None)
    seconds = None if reset_at is None else round(float(reset_at) - NOW, 1)
    held, by = host_bench(status, getattr(verdict, "reason", None), seconds)
    return {
        "reason": getattr(verdict, "reason", None),
        "kind": getattr(verdict, "kind", None),
        "seconds": seconds,
        "held_s": held,
        "held_by": by,
        "source": getattr(verdict, "source", "") or "",
        "window": str(getattr(verdict, "quota_window", "") or ""),
        "scope": str(getattr(verdict, "quota_scope", "") or ""),
    }


def collect():
    return [
        {"group": group, "name": name, "expect": expect, **decide(kwargs)}
        for group, name, kwargs, expect in CASES
    ]


def _fmt(seconds):
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:g}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _secs(row):
    """What KAME said."""
    if row.get("error"):
        return "RAISED"
    if row.get("reason") is None:
        return "declined"
    seconds = row.get("seconds")
    return "unsized" if seconds is None else _fmt(seconds)


def _held(row):
    """What the credential actually gets — the number the user feels."""
    if row.get("error"):
        return "-"
    held = _fmt(row.get("held_s"))
    if row.get("held_by") == "HOST FALLBACK":
        return f"{held}!"
    return f"{held}*" if row.get("held_by") == "kame floor" else held


def render(rows):
    group = None
    print(f"    {'shape':44} {'KAME':>9} {'HELD':>7}  {'reason':18} "
          f"{'':10} what a human expects")
    for row in rows:
        if row["group"] != group:
            group = row["group"]
            print(f"\n  {group}")
        print(f"    {row['name']:44} {_secs(row):>9} {_held(row):>7}  "
              f"{str(row.get('reason') or '-'):18} "
              f"{('via ' + row['source']) if row.get('source') else '':10} "
              f"{row['expect']}")


def check(rows, baseline_path):
    with open(baseline_path, encoding="utf-8") as fh:
        old = {(r["group"], r["name"]): r for r in json.load(fh)}
    moved = []
    for row in rows:
        was = old.get((row["group"], row["name"]))
        if was is None:
            moved.append((row["name"], "NEW", _secs(row)))
            continue
        # ``held_s`` is in here on purpose. A fix can leave the verdict
        # untouched and still change what the credential is held for — that is
        # exactly what 1.6.0.2's pool floor does, and a diff that compared only
        # the verdict reported "nothing moved" for the release's headline
        # change.
        keys = ("reason", "seconds", "source", "held_s", "held_by")
        if tuple(was.get(k) for k in keys) != tuple(row.get(k) for k in keys):
            moved.append((row["name"],
                          f"{_secs(was)} / {_held(was)}",
                          f"{_secs(row)} / {_held(row)}"))
    if not moved:
        print("\n  nothing moved — every decision is the one it was\n")
        return 0
    print(f"\n  {len(moved)} decision(s) moved:\n")
    print(f"    {'shape':44} {'KAME / HELD':>17}      {'KAME / HELD'}")
    for name, before, after in moved:
        print(f"    {name:44} {before:>17}  ->  {after}")
    print()
    return 0


def main():
    # This machine's console is cp1252 and every expectation column contains
    # an em-dash. Without this the table prints "?" where it means to explain
    # itself, which for a tool whose whole output is prose is a real defect.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover — older or redirected streams
        pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable")
    parser.add_argument("--check", metavar="BASELINE.json",
                        help="diff against a saved run")
    args = parser.parse_args()

    rows = collect()
    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.check:
        return check(rows, args.check)

    print(f"\nKAME decision table — {len(rows)} refusal shapes")
    print("  KAME = what the classifier said.  HELD = what the credential "
          "actually gets.")
    print("  '!' = nobody named a number and the HOST's fixed TTL applied.  "
          "'*' = KAME's pool floor.\n")
    render(rows)
    raised = [r for r in rows if r.get("error")]
    fell_back = [r for r in rows if r.get("held_by") == "HOST FALLBACK"]
    print(f"\n  {len(rows)} shapes, {len(raised)} raised, "
          f"{len(fell_back)} fell through to the host's fixed TTL\n")
    return 1 if raised else 0


if __name__ == "__main__":
    sys.exit(main())
