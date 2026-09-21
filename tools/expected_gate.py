"""Grade a KAME-Hermes plugin version against the independently-written
answer key in ``research/1.8.0.0/expected/verdicts.jsonl``.

Two artifacts feed this gate, and neither is produced here:

* ``research/1.8.0.0/expected/verdicts.jsonl`` — the answer key, written by a
  reviewer who never opened this plugin's code (see the file's own header and
  ``research/1.8.0.0/expected/vocabulary.md``). Read-only, never edited by
  this tool.
* ``research/1.8.0.0/expected/matchers.json`` — a machine-readable predicate
  per answer-key row, derived ONLY from that row's own
  ``status``/``structured_code``/``distinguishing_fields``/``message_template``/
  ``provider`` fields (never from its ``family``/``window``/``scope``/
  ``action``/``rest_rule``, which is the *expected answer*, not evidence to
  match on). Generated once by a human-reviewed script; committed as a static
  file so every predicate is inspectable without re-deriving it.

Usage
-----

    python tools/expected_gate.py --plugin-dir <hermes-kame-api-rotation dir> \\
        [--calls PATH] [--refusals PATH] [--matchers PATH] \\
        --out research/1.8.0.0/gate/<name>.json

        Loads the real filtered refusals/calls exactly as
        ``tools/replay_timeline.py`` does (contamination window excluded,
        ``some-gateway`` excluded, pre-``MIN_AT`` garbage excluded — see that
        module's constants), assigns each failing record to at most one
        answer-key shape via ``matchers.json`` (first match in file order
        wins; zero-match and multi-match records are both reported, never
        hidden), then re-runs that plugin version's *real* decision path
        (``DispatchBinding._on_failure`` + ``core.carousel.Carousel.mark``)
        once per matched record, with a FRESH engine each time so no shape's
        outcome can leak carousel learning into another shape's. Writes the
        per-shape and overall agreement report to ``--out``.

    python tools/expected_gate.py --compare A.json B.json

        Prints what moved between two gate runs (agreement % by field, and
        per-shape disagreement-count deltas).

Isolation: identical guard to ``replay_timeline.py`` — ``HERMES_HOME`` is
redirected to a fresh temp directory and the two record switches are
disabled *before* the plugin package is ever imported, so nothing here ever
touches the owner's real AppData or makes a network call. No raw API key is
ever read into an output field: every place a key is needed (``_on_failure``,
``engine.mark``) takes it from the evidence file directly and this module
never logs, journals or prints it.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import time as time_module
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay_timeline as rt  # noqa: E402  (see module docstring — reused, not duplicated)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MATCHERS = REPO_ROOT / "research" / "1.8.0.0" / "expected" / "matchers.json"

# A tolerance floor so a 1s-scale rest (flat server rest) is not held to an
# absurdly tight absolute bound, and a 3600s-scale rest is not held to an
# absurdly tight relative one. Matches the task's "±5% or ±2s" instruction.


def _tolerance(expected: float) -> float:
    return max(2.0, 0.05 * abs(expected))


# ===========================================================================
# SECTION 1 — the ONE vocabulary-mapping table (kind/verdict -> family/
# window/scope/action), documented. Every citation below is a file:line in
# hermes-kame-api-rotation/ read while building this table, not a guess.
# ===========================================================================
#
# ``DispatchBinding._on_failure`` (dispatch_binding.py:2560) returns
# ``(verdict, kind, status)``. ``verdict`` is one of "rotate", "stitch",
# "raise" (its own docstring, dispatch_binding.py:2573), and the outer loop
# (dispatch_binding.py:2055-2070) shows exactly what each one does: "raise"
# re-raises the exception to Hermes (ends the turn / surfaces), "stitch"
# returns the answer delivered so far, "rotate" falls through to try the
# next credential.
#
# ``kind`` is drawn from three places:
#
#   (a) The four words ``core.classify.classify()`` ever constructs a
#       ``Verdict.reason`` from — grepped directly (every ``Verdict(reason=``
#       call site in core/classify.py): "auth", "auth_permanent", "billing",
#       "rate_limit". These are exactly ``core.vocabulary.HOST_REASONS``.
#   (b) ``dispatch_binding``'s own reassignment of those four words, done
#       immediately after ``classify()`` returns (dispatch_binding.py:
#       2700-2800): "billing"->"insufficient_quota", "auth_permanent"->
#       "revoked", "auth" with ``Verdict.kind == "denied"``->"denied" (a 403
#       naming a model, not the credential — classify.py:989/1084 set
#       ``Verdict.kind="denied"`` alongside ``reason="auth"`` for exactly
#       this), "rate_limit" with ``quota_window == "per_day"``->"daily".
#       Otherwise "auth" and "rate_limit" pass through unchanged.
#   (c) Five kinds handed back before ``classify()`` is even called
#       (dispatch_binding.py:2642-2697): "content_filter" (421 on the
#       xiaomi surface), "upstream_error" (``looks_like_upstream_wrapper``),
#       "model_not_ready" (Bedrock 429), "auth_refresh" (a verified Vertex
#       401 with no ``API_KEY_INVALID`` reason), "host_breaker" (Hermes' own
#       cross-turn breaker).
#   (d) ``core.carousel.classify()``'s own nine words, used only when (a)
#       returned ``None`` ("the evidence-first classifier declined" —
#       dispatch_binding.py:2805-2820): "host_breaker", "timeout", "server",
#       "revoked", "denied", "auth", "insufficient_quota", "daily",
#       "per_minute", "other" (core/carousel.py:833-943, read end to end).
#
# FAMILY. Every kind above maps to exactly one answer-key family EXCEPT
# "other", which the plugin uses for both a genuine "request_fault" (no key
# helps) and a genuine "unknown" (nothing recognised) — it does not spell
# them differently. The plugin's OWN disambiguator for that split is
# ``is_terminal()`` (core/carousel.py:571,795-820: a fixed status set —
# {400,404,405,410,413,415,422,451,501} — plus a content-policy text match),
# which is exactly the check that turns "rotate" into "raise" upstream of
# ``kind`` ever being read. So: kind "other" + verdict "raise" (the status
# WAS terminal) -> "request_fault"; kind "other" + verdict "rotate"/"stitch"
# (the status was NOT terminal, e.g. a bare 418) -> "unknown". This mirrors
# the answer key's own real-06b (400, request_fault) vs real-23 (418,
# unknown) split — both would reach the plugin as kind="other".
_KIND_TO_FAMILY: Dict[str, str] = {
    "server": "server",
    "timeout": "timeout",
    "auth": "auth_dead",
    "revoked": "auth_dead",
    "denied": "denial",
    "insufficient_quota": "billing",
    "daily": "throttle",
    "per_minute": "throttle",
    "rate_limit": "throttle",
    "host_breaker": "unknown",
    "content_filter": "request_fault",
    "upstream_error": "upstream",
    "model_not_ready": "model_not_ready",
    "auth_refresh": "auth_refresh",
}


def map_family(kind: Any, verdict: Any) -> str:
    kind = str(kind or "").strip()
    if kind == "other":
        return "request_fault" if verdict == "raise" else "unknown"
    return _KIND_TO_FAMILY.get(kind, "unknown")


# ACTION. "rotate"->"rotate_key" and "stitch"->"stitch_or_return_partial" are
# unambiguous (dispatch_binding.py:2055-2070). "raise" is surfaced to Hermes
# for every terminal kind, but the answer key's own vocabulary (vocabulary.md
# §4) splits that single plugin behaviour into two different *meanings*:
# "raise_to_host" (no key helps — request_fault, retired model, unanimous
# refusal) and "defer_host_retry" (hand back to the HOST's own recovery —
# OAuth refresh, host-owned transport retries). Only "auth_refresh" and
# "host_breaker" are host-owned recovery paths in this plugin's own kind
# vocabulary; every other terminal kind is a genuine "nothing can help"
# surface. NOTE (see GATE.md "disagreements"): the plugin's "timeout" kind
# does NOT reach verdict=="raise" at all — ``is_terminal()`` explicitly
# returns False for a timeout-matched text (core/carousel.py:811-812), so a
# timeout always returns "rotate" with a real bench (``TIMEOUT_S = 3.0``,
# core/carousel.py:261/864). The answer key expects "defer_host_retry" with
# NO bench for timeout (real-10, real-12a, corpus-07). That gap is a finding
# this gate reports, not something this mapping table papers over.
_DEFER_HOST_KINDS = frozenset({"auth_refresh", "host_breaker"})


def map_action(verdict: Any, kind: Any) -> str:
    verdict = str(verdict or "")
    if verdict == "rotate":
        return "rotate_key"
    if verdict == "stitch":
        return "stitch_or_return_partial"
    if verdict == "raise":
        return "defer_host_retry" if str(kind or "") in _DEFER_HOST_KINDS else "raise_to_host"
    return f"unrecognised_verdict:{verdict}"


# WINDOW / SCOPE. When the rich ``core.classify.classify()`` path ran (kind
# came from (a)/(b)/(c) above), its ``Verdict.quota_window`` /
# ``quota_scope`` (core/quota.py:159-181, ``QuotaScope``/``QuotaWindow``) are
# captured directly off the real object (see ``install_classify_capture``
# below) — translated, not re-derived. NOTE: ``QuotaWindow`` has no separate
# "tokens per minute" member — the TPM markers in ``_PER_MINUTE_MARKERS``
# (core/quota.py:212-215) collapse to the SAME ``"per_minute"`` value an RPM
# refusal gets, so the plugin cannot distinguish real-04 (expected window
# ``tokens_per_minute``) from real-21 (expected window ``per_minute``) even
# in principle. Reported as a disagreement, not hidden by a lenient mapping.
# ``QuotaScope`` likewise has no "credential" or "project" member — only
# ``per_model``/``account``/``unknown`` — so the answer key's "credential"
# and "project" scopes have no plugin equivalent; mapped to "unknown" below
# and flagged in GATE.md rather than silently forced to "account".
_QUOTA_WINDOW_TO_KEY = {
    "per_minute": "per_minute",
    # 1.8.0.0 taught the plugin the difference the answer key already made:
    # every real per-minute refusal on this machine is a token limit, not a
    # request limit. Before this line the gate translated the new word to
    # "unknown" and reported the plugin as wrong for having become right.
    "tokens_per_minute": "tokens_per_minute",
    "per_day": "per_day",
    "per_week": "per_week",
    "per_month": "per_month",
    "per_hour": "per_hour",
    "account": "unknown",  # QuotaWindow.ACCOUNT is a scope-shaped fallback, not a real period
    "unknown": "unknown",
}
_QUOTA_SCOPE_TO_KEY = {
    "per_model": "model",
    "account": "account",
    "unknown": "unknown",
}

# When ``classify()`` returned None (the legacy ``core.carousel.classify()``
# path, no ``Verdict`` object exists at all), window/scope are inferred from
# ``kind`` alone — this is a real limitation of that older code path, not a
# gate simplification: it genuinely tracks neither dimension.
_KIND_DEFAULT_WINDOW = {"daily": "per_day"}  # everything else not in this map -> "none", except throttle kinds -> "unknown"
_KIND_THROTTLE_LIKE = frozenset({"per_minute", "rate_limit"})
_KIND_DEFAULT_SCOPE = {
    "daily": "model", "per_minute": "model", "rate_limit": "model",
    "denied": "model", "model_not_ready": "model",
    "auth": "credential", "revoked": "credential", "auth_refresh": "credential",
    "insufficient_quota": "account", "host_breaker": "account",
}


def map_window(kind: Any, quota_window: Any) -> str:
    if quota_window:
        return _QUOTA_WINDOW_TO_KEY.get(str(quota_window), "unknown")
    kind = str(kind or "")
    if kind in _KIND_DEFAULT_WINDOW:
        return _KIND_DEFAULT_WINDOW[kind]
    if kind in _KIND_THROTTLE_LIKE:
        return "unknown"
    return "none"


def window_for_family(family: str, raw_window: str) -> str:
    """The answer key's own family table (vocabulary.md §2) states window is
    conceptually N/A outside the throttle family: "none (no counter —
    server, timeout, request_fault)". Cross-checked directly against every
    row in verdicts.jsonl: EVERY non-throttle family's window is "none",
    except family "unknown" itself, which the answer key also spells
    "unknown" for window (real-23/24/26 — an unresolved family leaves the
    window unresolved too, not "none"). So the family the plugin actually
    produced, not just its raw captured/derived window, decides which of
    the two applies; a stray non-"unknown" quota_window captured on a
    non-throttle Verdict (the classifier sets SOME default even when it
    never really evaluated a counter) must not leak through as a false
    "the plugin named a real window" signal.
    """
    if family == "throttle":
        return raw_window
    if family == "unknown":
        return "unknown"
    return "none"


def map_scope(kind: Any, quota_scope: Any) -> str:
    # A captured ``quota_scope`` of exactly "unknown" (``QuotaScope.UNKNOWN``)
    # is the classifier's own catch-all, not evidence — vocabulary.py's R18
    # ("silence defaults to per-model; only explicit evidence may widen") is
    # the answer key's OWN default rule for exactly this situation, and the
    # kind-based table below already encodes it per kind. Letting a merely
    # non-empty-but-generic "unknown" short-circuit past that default (a
    # non-empty string is truthy) would silently throw the R18 default away
    # every time the rich classifier ran but found nothing — which is most
    # of the time. Only a REAL captured scope (per_model/account) wins here.
    if quota_scope and str(quota_scope) != "unknown":
        return _QUOTA_SCOPE_TO_KEY.get(str(quota_scope), "unknown")
    return _KIND_DEFAULT_SCOPE.get(str(kind or ""), "unknown")


# APPLICABILITY. ``window``/``scope`` name a counter and its owner — a fact
# that exists only where the family has a counter at all. Cross-checked
# directly against every "real"/"corpus" row in verdicts.jsonl (the only
# origins that ever reach the scoring loop below — a "documented" row has
# ``count: 0`` and is ``not_exercised``, never scored): every ``server``,
# ``timeout``, ``request_fault``, ``auth_dead``, ``auth_refresh``,
# ``denial``, ``model_not_ready`` and ``upstream`` row's own ``window`` is
# "none" — there is no counter to name — and its ``scope`` is either "model"
# or "unknown", a filler value nobody asked the row for (the message names
# the model incidentally, or it names nothing at all), never something a
# behaviour actually depends on. Grading those as if the row had answered a
# real question manufactures agreement or disagreement over a question the
# family never posed — see GATE.md for the measured shapes this was doing it
# to (real-03/144 records, real-08a/b, real-14, real-28, real-31).
#
# Only ``throttle`` and ``billing`` are different: a throttle's whole
# identity IS its window (per-minute vs. per-day changes the rest by two
# orders of magnitude) and its scope (one model vs. the whole key changes
# which other identities stay usable) — see the answer key's own
# distribution (billing rows carry real "account"/"project" scopes, never
# filler; throttle rows carry every scope value the vocabulary has). Gated on
# the answer key's OWN expected family (ground truth about which real-world
# scenario this is), not on whatever family the plugin happened to produce —
# a family disagreement is already scored on its own field; asking a
# structurally inapplicable question on top of it would not add information.
_SCOPE_WINDOW_FAMILIES = frozenset({"throttle", "billing"})


def field_applies(field: str, family: str) -> bool:
    if field in ("window", "scope"):
        return family in _SCOPE_WINDOW_FAMILIES
    return True


# ===========================================================================
# SECTION 2 — matchers.json: loading, evidence extraction, matching
# ===========================================================================


def load_matchers(path: Path = DEFAULT_MATCHERS) -> List[dict]:
    matchers = json.loads(Path(path).read_text(encoding="utf-8"))
    seen = set()
    for m in matchers:
        sid = m["shape_id"]
        if sid in seen:
            raise SystemExit(f"duplicate shape_id in matchers.json: {sid}")
        seen.add(sid)
    return matchers


def _safe_json_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def build_evidence(call_row: dict, refusal: Optional[dict]) -> dict:
    """The evidence view a matcher predicate is checked against.

    Deliberately narrow: only what a real ``calls.jsonl``/``refusals.jsonl``
    pair actually carries (see ``replay_timeline.build_exception_from_refusal``'s
    own docstring on why headers can never be reconstructed here). Never
    includes ``call_row["key"]`` — nothing in this function, or anything
    downstream of it, may put the raw credential into an output field.
    """
    status = call_row.get("status")
    if status is None and refusal is not None:
        status = refusal.get("status")
    identity = str(call_row.get("identity") or "")
    provider = identity.split(":", 1)[0] if ":" in identity else identity
    message = ""
    body_text = ""
    if refusal is not None:
        message = str(refusal.get("message") or "")
        body_text = _safe_json_text(refusal.get("body"))
        if not body_text:
            body_text = str(refusal.get("response") or "")
    else:
        message = f"(no refusal payload matched; call kind={call_row.get('kind')!r})"
    haystack = f"{message} {body_text}".lower()
    return {
        "status": status,
        "provider": provider.lower(),
        "identity": identity,
        "message": message,
        "haystack": haystack,
    }


_PER_DAY_TOKENS = ("perday", "requestsperday", "tokensperday", "dailylimit")
_PER_MINUTE_TOKENS = ("perminute", "requestsperminute", "tokensperminute", "rpm", "tpm")
_RETRY_HINT_TOKENS = (
    "retrydelay", "retry-after", "retry_after", "resets_in_seconds", "resets_at",
    "x-ratelimit-reset", "retry in ", "please retry in", "try again in",
)


def detect_quota_family(haystack: str) -> str:
    if any(tok in haystack for tok in _PER_DAY_TOKENS):
        return "PerDay"
    if any(tok in haystack for tok in _PER_MINUTE_TOKENS):
        return "PerMinute"
    return "none"


def detect_retry_hint(haystack: str) -> bool:
    return any(tok in haystack for tok in _RETRY_HINT_TOKENS)


def matcher_matches(matcher: dict, ev: dict) -> bool:
    m = matcher["match"]
    if "status" in m and m["status"] != ev["status"]:
        return False
    if "status_in" in m and ev["status"] not in m["status_in"]:
        return False
    if "provider_in" in m and not any(p.lower() in ev["provider"] for p in m["provider_in"]):
        return False
    if "route_host" in m and m["route_host"].lower() not in ev["haystack"]:
        return False
    if "body_contains" in m and not all(tok.lower() in ev["haystack"] for tok in m["body_contains"]):
        return False
    if "body_absent" in m and any(tok.lower() in ev["haystack"] for tok in m["body_absent"]):
        return False
    if "message_regex" in m and not re.search(m["message_regex"], ev["message"], re.IGNORECASE):
        return False
    if "quota_family" in m and detect_quota_family(ev["haystack"]) != m["quota_family"]:
        return False
    if "has_retry_hint" in m and detect_retry_hint(ev["haystack"]) != m["has_retry_hint"]:
        return False
    return True


def assign_shapes(records: Sequence[dict], matchers: Sequence[dict]) -> List[dict]:
    """One row per failing record: ``{"record", "evidence", "matched", "assigned"}``.

    ``matched`` lists EVERY shape_id whose predicate is true (for multi-match
    reporting); ``assigned`` is the first one in file order, or ``None``.
    """
    out = []
    for rec in records:
        ev = build_evidence(rec, rec.get("_refusal"))
        matched = [m["shape_id"] for m in matchers if matcher_matches(m, ev)]
        out.append({
            "record": rec, "evidence": ev, "matched": matched,
            "assigned": matched[0] if matched else None,
        })
    return out


# ===========================================================================
# SECTION 3 — running the plugin's real decision path, fresh engine/record
# ===========================================================================

_NO_MARK = object()
_NO_CLASSIFY = object()


def install_classify_capture(dispatch_binding_mod) -> Optional[Dict[str, Any]]:
    """Wrap ``dispatch_binding_mod.classify`` (== ``core.classify.classify``,
    imported by name — dispatch_binding.py:160) so the ``Verdict`` object it
    returns can be read after ``_on_failure`` runs, exactly the same pattern
    ``replay_timeline._install_mark_capture`` uses for ``engine.mark``.
    Returns ``None`` (capture stays permanently empty) when this plugin
    version has no such attribute — an older shape this gate still grades,
    just without window/scope precision beyond the kind-based fallback.
    """
    original = getattr(dispatch_binding_mod, "classify", None)
    if original is None:
        return None
    capture: Dict[str, Any] = {"last": _NO_CLASSIFY}

    def _wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        capture["last"] = result
        return result

    dispatch_binding_mod.classify = _wrapped
    return capture


def load_plugin_context(plugin_dir: Path, package_name: str) -> dict:
    hermes_home = rt.isolate_hermes_home()
    rt.load_plugin(plugin_dir, package_name=package_name)
    dispatch_binding_mod = importlib.import_module(f"{package_name}.dispatch_binding")
    carousel_mod = importlib.import_module(f"{package_name}.core.carousel")
    guidance_patched = rt.patch_guidance_blocks(package_name)
    adaptations = rt._introspect_adaptations(dispatch_binding_mod, carousel_mod)
    classify_capture = install_classify_capture(dispatch_binding_mod)
    return {
        "hermes_home": hermes_home,
        "dispatch_binding_mod": dispatch_binding_mod,
        "carousel_mod": carousel_mod,
        "adaptations": adaptations,
        "accepts_credential_id": adaptations.get("_on_failure_has_credential_id", True),
        "classify_capture": classify_capture,
        "classify_capturable": classify_capture is not None,
        "guidance_patched": guidance_patched,
        "clock": {"now": time_module.time()},
    }


def run_decision(ctx: dict, record: dict) -> dict:
    """One fresh ``Carousel`` + ``DispatchBinding`` per call, at the record's
    own timestamp — deliberately NOT the stateful, continuous-process replay
    ``replay_timeline.run_replay`` performs; the task this gate answers is
    "what would THIS shape alone produce", not "what did the owner's whole
    session produce", so no learning may leak from one matched record to the
    next, even within the same shape.
    """
    carousel_mod = ctx["carousel_mod"]
    dispatch_binding_mod = ctx["dispatch_binding_mod"]
    engine = carousel_mod.Carousel()
    binding = dispatch_binding_mod.DispatchBinding(engine=engine)

    mark_capture = {"last": _NO_MARK}
    original_mark = engine.mark

    def _wrapped_mark(*args, **kwargs):
        result = original_mark(*args, **kwargs)
        mark_capture["last"] = result
        return result

    engine.mark = _wrapped_mark

    identity = record.get("identity") or "?:?"
    key = record.get("key") or ""
    ctx["clock"]["now"] = record["at"]

    refusal = record.get("_refusal")
    exc = rt.build_exception_from_refusal(refusal) if refusal is not None else rt.build_minimal_exception(record)
    attempt = record.get("attempt") or 1

    classify_capture = ctx["classify_capture"]
    if classify_capture is not None:
        classify_capture["last"] = _NO_CLASSIFY

    try:
        if ctx["accepts_credential_id"]:
            verdict, kind_returned, status = binding._on_failure(
                identity, key, exc, identity, attempt, False, credential_id=key,
            )
        else:
            verdict, kind_returned, status = binding._on_failure(
                identity, key, exc, identity, attempt, False,
            )
    except TypeError:
        verdict, kind_returned, status = binding._on_failure(
            identity, key, exc, identity, attempt, False,
        )

    applied = mark_capture["last"]
    hold_s = float(applied) if applied is not _NO_MARK else None

    quota_window = None
    quota_scope = None
    if classify_capture is not None:
        cls_verdict = classify_capture["last"]
        if cls_verdict is not None and cls_verdict is not _NO_CLASSIFY:
            quota_window = getattr(cls_verdict, "quota_window", None)
            quota_scope = getattr(cls_verdict, "quota_scope", None)

    return {
        "verdict": verdict, "kind": kind_returned, "status": status,
        "hold_s": hold_s, "quota_window": quota_window, "quota_scope": quota_scope,
    }


# ===========================================================================
# SECTION 4 — rest-DSL comparison
# ===========================================================================

_STATED_PATTERNS = (
    # Evidence text is a mix of real JSON (double-quoted) and Python dict
    # repr (single-quoted, e.g. "{'resets_in_seconds': 12660}" — exactly
    # what ``str(exc)``/refusal.jsonl's own ``message`` field carries for a
    # body that was python-repr'd, not json.dumps'd, before being logged).
    # Both quote styles, and no quotes at all, must be accepted.
    re.compile(r'[\'"]?retrydelay[\'"]?\s*:\s*[\'"]?(\d+(?:\.\d+)?)\s*s?[\'"]?'),
    re.compile(r'[\'"]?resets_in_seconds[\'"]?\s*:\s*[\'"]?(\d+(?:\.\d+)?)'),
    re.compile(r'retry[- ]after[\'"]?\s*[:=]\s*[\'"]?(\d+(?:\.\d+)?)'),
    re.compile(r'retry in (\d+(?:\.\d+)?)\s*s'),
    re.compile(r'try again in (\d+(?:\.\d+)?)\s*s'),
)


def extract_stated_seconds(haystack: str) -> Optional[float]:
    """Best-effort recovery of a provider-stated wait from the redacted
    evidence text, for checking the ``obey_stated`` rest DSL. This is a
    regex approximation of what the plugin's own ``extract_delay`` does over
    the parsed body/headers (core/carousel.py:673) — headers were never part
    of this evidence (see ``replay_timeline.build_exception_from_refusal``),
    so this can under-detect; documented as a gate limitation in GATE.md,
    not silently treated as authoritative.
    """
    for pattern in _STATED_PATTERNS:
        match = pattern.search(haystack)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return None


def rest_agrees(dsl: dict, hold_s: Optional[float], haystack: str) -> Optional[bool]:
    """``True``/``False``, or ``None`` when this rest rule cannot be checked
    numerically (an ``unclear`` row, or an ``obey_stated`` row where no
    stated number could be recovered from the evidence)."""
    kind = dsl.get("kind")
    if kind == "unclear":
        return None
    if kind == "none":
        # "No bench" is a real, checkable claim when the plugin actually
        # produced a number — a request_fault/timeout path that mistakenly
        # rests a key is exactly the bug this branch exists to catch, so a
        # non-``None`` hold outside tolerance stays a genuine disagreement.
        # But when the plugin produced NO number at all, that is not a
        # measurement, it is the structural consequence of an action that
        # never calls ``engine.mark`` (``raise_to_host``/``defer_host_retry``
        # never reach it) — the same fact the ``action`` field already
        # scores. Counting it again here as an "agreement" manufactures
        # information a family with ``rest_rule: {"kind": "none"}`` never had
        # to offer; reported ``not_applicable`` instead, same treatment as
        # ``scope``/``window`` on a family with no counter (see
        # ``field_applies`` above and GATE.md).
        if hold_s is None:
            return None
        return abs(hold_s) <= 2.0
    if kind in ("fixed", "flat_base"):
        expected = float(dsl["seconds"])
        if hold_s is None:
            return False
        return abs(hold_s - expected) <= _tolerance(expected)
    if kind == "obey_stated":
        stated = extract_stated_seconds(haystack)
        cap = float(dsl.get("cap", 3600))
        if hold_s is None:
            return False
        if dsl.get("ignore_stated"):
            if stated is None:
                return None
            return abs(hold_s - stated) > _tolerance(stated)
        if stated is None:
            return None
        expected = min(stated, cap)
        return abs(hold_s - expected) <= _tolerance(expected)
    if kind == "probe_ladder":
        if hold_s is None:
            return False
        return any(abs(hold_s - step) <= _tolerance(step) for step in dsl.get("steps", []))
    return None


# ===========================================================================
# SECTION 5 — the gate driver
# ===========================================================================

FIELDS = ("family", "window", "scope", "action", "rest")


def run_gate(
    plugin_dir: Path,
    calls_path: Path,
    refusals_path: Path,
    matchers_path: Path,
    out_path: Path,
) -> dict:
    matchers = load_matchers(matchers_path)

    calls, calls_filter_report = rt.load_calls(calls_path)
    refusals, refusals_filter_report = rt.load_refusals(refusals_path)
    calls.sort(key=lambda r: r["at"])
    match_counts = rt.match_refusals(calls, refusals)

    failing = [r for r in calls if r.get("outcome") != "answered" and r.get("key")]
    assignments = assign_shapes(failing, matchers)

    unmatched = [a for a in assignments if not a["matched"]]
    multi = [a for a in assignments if len(a["matched"]) > 1]

    by_shape: Dict[str, List[dict]] = defaultdict(list)
    for a in assignments:
        if a["assigned"]:
            by_shape[a["assigned"]].append(a)

    package_name = f"kame_expected_gate_{os.getpid()}_{id(str(plugin_dir))}"
    ctx = load_plugin_context(Path(plugin_dir), package_name)

    real_time_fn = time_module.time
    real_monotonic_fn = time_module.monotonic
    time_module.time = lambda: ctx["clock"]["now"]
    time_module.monotonic = lambda: ctx["clock"]["now"]

    field_agree = Counter()
    field_total = Counter()
    field_na = Counter()
    per_shape_rows: List[dict] = []

    try:
        for matcher in matchers:
            sid = matcher["shape_id"]
            recs = by_shape.get(sid, [])
            row = {
                "shape_id": sid, "origin": matcher["origin"], "records": len(recs),
            }
            if not recs:
                row["not_exercised"] = True
                per_shape_rows.append(row)
                continue

            agree = Counter()
            disagree = Counter()
            not_applicable = Counter()
            examples: List[dict] = []
            exp = matcher["expected"]
            dsl = matcher["rest"]

            for a in recs:
                rec, ev = a["record"], a["evidence"]
                decision = run_decision(ctx, rec)
                produced_family = map_family(decision["kind"], decision["verdict"])
                produced = {
                    "family": produced_family,
                    "window": window_for_family(
                        produced_family, map_window(decision["kind"], decision["quota_window"])
                    ),
                    "scope": map_scope(decision["kind"], decision["quota_scope"]),
                    "action": map_action(decision["verdict"], decision["kind"]),
                }
                # Applicability is gated on the answer key's OWN expected
                # family — the ground truth about which real-world scenario
                # this refusal is — never on whatever family the plugin
                # happened to produce (see ``field_applies``'s own comment).
                exp_family = str(exp.get("family") or "")
                field_ok: Dict[str, Optional[bool]] = {
                    f: (produced[f] == exp.get(f)) if field_applies(f, exp_family) else None
                    for f in ("family", "window", "scope", "action")
                }
                rest_ok = rest_agrees(dsl, decision["hold_s"], ev["haystack"])

                for f in ("family", "window", "scope", "action"):
                    if field_ok[f] is None:
                        not_applicable[f] += 1
                        field_na[f] += 1
                        continue
                    field_total[f] += 1
                    (agree if field_ok[f] else disagree)[f] += 1
                    if field_ok[f]:
                        field_agree[f] += 1
                if rest_ok is None:
                    not_applicable["rest"] += 1
                    field_na["rest"] += 1
                else:
                    field_total["rest"] += 1
                    (agree if rest_ok else disagree)["rest"] += 1
                    if rest_ok:
                        field_agree["rest"] += 1

                disagreed = any(v is False for v in field_ok.values()) or (rest_ok is False)
                if disagreed and len(examples) < 3:
                    examples.append({
                        "at": rec.get("at"),
                        "status": ev["status"],
                        "message": rt._redact_message(ev["message"], limit=160),
                        "produced": {**produced, "rest_s": decision["hold_s"],
                                     "plugin_kind": decision["kind"], "plugin_verdict": decision["verdict"]},
                        "expected": {**exp, "rest": dsl},
                    })

            row.update({
                "agree": dict(agree), "disagree": dict(disagree),
                "not_applicable": dict(not_applicable), "examples": examples,
            })
            per_shape_rows.append(row)
    finally:
        time_module.time = real_time_fn
        time_module.monotonic = real_monotonic_fn

    agreement_pct = {
        f: (round(100.0 * field_agree[f] / field_total[f], 2) if field_total[f] else None)
        for f in FIELDS
    }

    not_exercised = [r["shape_id"] for r in per_shape_rows if r.get("not_exercised")]

    def _example(a: dict) -> dict:
        ev = a["evidence"]
        return {
            "at": a["record"].get("at"), "identity": ev["identity"], "status": ev["status"],
            "message": rt._redact_message(ev["message"], limit=160), "matched": a["matched"],
        }

    result = {
        "plugin_dir": str(plugin_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "matchers_path": str(matchers_path),
        "adaptations": ctx["adaptations"],
        "classify_capturable": ctx["classify_capturable"],
        "input": {
            "calls_read": calls_filter_report["total_read"],
            "calls_kept": len(calls),
            "refusals_read": refusals_filter_report["total_read"],
            "refusals_kept": len(refusals),
            "failing_records": len(failing),
        },
        "filtering": {"calls": calls_filter_report, "refusals": refusals_filter_report},
        "matching": {
            "matched_to_a_shape": len(assignments) - len(unmatched),
            "unmatched": len(unmatched),
            "multi_match": len(multi),
        },
        "agreement_pct": agreement_pct,
        # Printed for every field, never swallowed — ``family``/``action``
        # apply everywhere so their count is always 0; ``window``/``scope``
        # are 0 outside throttle/billing rows, ``rest`` is 0 wherever a
        # ``{"kind":"none"}`` rule met an actual number instead of silence.
        "not_applicable": {f: field_na.get(f, 0) for f in FIELDS},
        "rest_not_applicable": field_na.get("rest", 0),  # kept: pre-1.8.0.0 readers of this key
        "shapes_not_exercised": not_exercised,
        "shapes_exercised_count": len(matchers) - len(not_exercised),
        "per_shape": per_shape_rows,
        "unmatched_examples": [_example(a) for a in unmatched[:20]],
        "multi_match_examples": [_example(a) for a in multi[:20]],
    }

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


# ===========================================================================
# SECTION 6 — compare mode
# ===========================================================================


def compare_mode(path_a: Path, path_b: Path) -> str:
    a = json.loads(Path(path_a).read_text(encoding="utf-8"))
    b = json.loads(Path(path_b).read_text(encoding="utf-8"))
    lines = [
        f"Compare: A={Path(path_a).name} ({a.get('plugin_dir')})",
        f"         B={Path(path_b).name} ({b.get('plugin_dir')})",
        "",
        "Agreement % by field (A -> B):",
    ]
    for f in FIELDS:
        av, bv = a["agreement_pct"].get(f), b["agreement_pct"].get(f)
        delta = "n/a" if (av is None or bv is None) else f"{round(bv - av, 2):+.2f}"
        lines.append(f"  {f:8s}  A={av!s:>7s}  B={bv!s:>7s}  delta={delta}")

    lines.append("")
    lines.append(f"Matching: A unmatched={a['matching']['unmatched']} multi={a['matching']['multi_match']}"
                  f"  ->  B unmatched={b['matching']['unmatched']} multi={b['matching']['multi_match']}")
    lines.append(f"Shapes not exercised: A={len(a['shapes_not_exercised'])} B={len(b['shapes_not_exercised'])}")

    a_shapes = {r["shape_id"]: r for r in a["per_shape"]}
    b_shapes = {r["shape_id"]: r for r in b["per_shape"]}
    lines.append("")
    lines.append("Per-shape disagreement-count changes (only shapes with >=1 record on either side):")
    for sid in sorted(set(a_shapes) | set(b_shapes)):
        ra, rb = a_shapes.get(sid, {}), b_shapes.get(sid, {})
        ra_dis, rb_dis = ra.get("disagree", {}), rb.get("disagree", {})
        if not ra_dis and not rb_dis and not ra.get("records") and not rb.get("records"):
            continue
        if ra_dis == rb_dis and ra.get("records") == rb.get("records"):
            continue
        lines.append(
            f"  {sid}: A records={ra.get('records', 0)} disagree={ra_dis}  "
            f"->  B records={rb.get('records', 0)} disagree={rb_dis}"
        )
    return "\n".join(lines)


# ===========================================================================
# CLI
# ===========================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plugin-dir", help="path to a hermes-kame-api-rotation folder")
    parser.add_argument("--calls", help="path to calls.jsonl (default: the real installed evidence)")
    parser.add_argument("--refusals", help="path to refusals.jsonl (default: the real installed evidence)")
    parser.add_argument("--matchers", help="path to matchers.json (default: research/1.8.0.0/expected/matchers.json)")
    parser.add_argument("--out", help="output gate JSON path")
    parser.add_argument("--compare", nargs=2, metavar=("A_JSON", "B_JSON"), help="compare two gate runs")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.compare:
        print(compare_mode(Path(args.compare[0]), Path(args.compare[1])))
        return 0

    if not args.plugin_dir or not args.out:
        print("error: --plugin-dir and --out are required unless --compare is used", file=sys.stderr)
        return 2

    calls_path = Path(args.calls) if args.calls else rt.DEFAULT_CALLS
    refusals_path = Path(args.refusals) if args.refusals else rt.DEFAULT_REFUSALS
    matchers_path = Path(args.matchers) if args.matchers else DEFAULT_MATCHERS
    if not calls_path.is_file():
        print(f"error: calls file not found: {calls_path}", file=sys.stderr)
        return 2
    if not refusals_path.is_file():
        print(f"error: refusals file not found: {refusals_path}", file=sys.stderr)
        return 2

    result = run_gate(Path(args.plugin_dir), calls_path, refusals_path, matchers_path, Path(args.out))
    summary = {k: v for k, v in result.items() if k != "per_shape"}
    summary["per_shape_exercised"] = [r for r in result["per_shape"] if not r.get("not_exercised")]
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
