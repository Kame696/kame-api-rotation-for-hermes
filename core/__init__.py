"""KAME core — framework-agnostic rotation logic.

Nothing in this package imports Hermes, Agent Zero, or any HTTP client. That is
deliberate: the decision rules are the asset, the host bindings are disposable.
"""

from . import answer, carousel, escalate, events, probe, reconcile, report, stitch, storm
from .carousel import Carousel
from .events import EVENTS, Events
from .stitch import Stitcher, prefill_message, resumable, stitch_text
from .classify import Verdict, classify, looks_like_upstream_wrapper
from .journal import Block, Journal, Recovery, WindowStat, short_streak, summarize
from .probe import Probe
from .storm import StormFilter, Verdict as StormVerdict
from .ledger import Bench, Ledger, Refutation, normalize_model
from .keys import (
    ImportPlan,
    build_labels,
    format_plan_lines,
    group_status,
    looks_like_api_key,
    parse_keys,
    plan_import,
    redact,
)
from .quota import (
    QuotaScope,
    QuotaWindow,
    ResetDecision,
    compute_reset_at,
    detect_quota_scope,
    detect_quota_window,
    extract_from_body,
    extract_from_exception,
    extract_from_headers,
    extract_from_text,
    extract_retry_delay_seconds,
    parse_absolute_timestamp,
    parse_duration_to_seconds,
)

# Kept in step with ``plugin.yaml`` by ``test_core_version_matches_the_manifest``.
# It drifted four releases before anything noticed, which is what a version
# string nobody reads is worth: this package is meant to be lifted into
# another host, and the number that travels with it has to say which rules
# came along.
__version__ = "1.3.1"

__all__ = [
    "__version__",
    "answer",
    "Carousel",
    "carousel",
    "EVENTS",
    "Events",
    "events",
    "Stitcher",
    "stitch",
    "stitch_text",
    "prefill_message",
    "resumable",
    "Verdict",
    "classify",
    "looks_like_upstream_wrapper",
    "Bench",
    "Ledger",
    "Refutation",
    "normalize_model",
    "Block",
    "Journal",
    "Recovery",
    "WindowStat",
    "short_streak",
    "summarize",
    "escalate",
    "Probe",
    "probe",
    "storm",
    "StormFilter",
    "StormVerdict",
    "reconcile",
    "report",
    "QuotaScope",
    "QuotaWindow",
    "ResetDecision",
    "compute_reset_at",
    "detect_quota_scope",
    "detect_quota_window",
    "extract_from_body",
    "extract_from_exception",
    "extract_from_headers",
    "extract_from_text",
    "extract_retry_delay_seconds",
    "parse_absolute_timestamp",
    "parse_duration_to_seconds",
    "ImportPlan",
    "build_labels",
    "format_plan_lines",
    "group_status",
    "looks_like_api_key",
    "parse_keys",
    "plan_import",
    "redact",
]
