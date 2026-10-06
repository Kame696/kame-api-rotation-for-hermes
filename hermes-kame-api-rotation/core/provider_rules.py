"""Small endpoint-scoped exceptions to the shared vocabulary.

Meaning is attached to the actual request route, not a user-facing provider
alias or a URL embedded in an error message. Unknown routes remain unknown.
Sources and limitations: knowledge_base/provider-surfaces.md.
"""
from urllib.parse import urlsplit

from . import catalog
from .quota import QuotaScope, QuotaWindow


SCOPED_RULES = (
    {"surface": "alibaba_chat", "path": ("error", "code"), "value": "insufficient_quota",
     "messages": ("You exceeded your current quota, please check your plan and billing details.",
                  "Allocated quota exceeded, please increase your quota limit."),
     "source": "https://www.alibabacloud.com/help/en/model-studio/error-code",
     "reading": catalog.Reading(catalog.THROTTLE,
         billing_context_only=True,
         why="Exact documented token-rate message; no fixed second/minute window inferred")},
    *({"surface": "zai_chat", "path": ("error", "code"), "value": code,
       "source": "https://docs.z.ai/api-reference/api-code",
       "reading": catalog.Reading(catalog.THROTTLE,
           billing_context_only=True,
           why="Subscription limit can recover at its supplied reset despite unavailable extra billing")}
      for code in ("1308", "1310", "1316", "1317", "1318", "1319", "1320", "1321")),
    *({"surface": "zai_chat", "path": ("error", "code"), "value": code,
       "source": "https://docs.z.ai/api-reference/api-code",
       "reading": catalog.Reading(catalog.BILLING,
           why="Provider requires balance or subscription renewal, not ordinary rate recovery")}
      for code in ("1113", "1309", "1314")),
    {"surface": "google_vertex", "path": ("error", "status"), "value": "UNAUTHENTICATED",
     "source": "https://cloud.google.com/vertex-ai/generative-ai/docs/model-reference/api-errors",
     "reading": catalog.Reading(catalog.AUTH_REFRESH,
         why="Vertex authentication can require OAuth refresh rather than key replacement")},
    {"surface": "google_interactions", "path": ("error", "code"), "value": "quota_exceeded",
     "source": "https://ai.google.dev/gemini-api/docs/api-errors",
     "reading": catalog.Reading(catalog.THROTTLE, window=QuotaWindow.PER_DAY,
         why="Google Interactions explicitly names a daily allowance")},
    {"surface": "anthropic_messages", "path": ("error", "details", "error_code"),
     "value": "enforced_spend_limit_reached",
     "source": "https://platform.claude.com/docs/en/api/rate-limits",
     "reading": catalog.Reading(catalog.THROTTLE, window=QuotaWindow.PER_MONTH,
         scope=QuotaScope.ACCOUNT, why="Messages API names the organization monthly spend cap")},
)

TIMING_RULES = (
    {"surface": "groq_chat", "window": QuotaWindow.PER_DAY,
     "sources": ("header.retry-after", "exception.retry_after", "header.x-ratelimit-reset-requests"),
     "source": "https://console.groq.com/docs/rate-limits"},
)


def _attr(obj, name):
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def api_surface(error):
    """Infer only from attached request metadata; never read credentials/network."""
    response = _attr(error, "response")
    for request in (_attr(response, "request"), _attr(error, "request")):
        value = _attr(request, "url")
        if value is None:
            continue
        try:
            url = urlsplit(str(value))
            if url.scheme != "https" or url.username or url.password or url.port not in (None, 443):
                continue
            host = (url.hostname or "").lower()
            path = url.path
            if host == "generativelanguage.googleapis.com":
                pieces = path.strip("/").split("/")
                if len(pieces) >= 2 and pieces[0] in ("v1", "v1beta"):
                    if pieces[1] == "interactions":
                        return "google_interactions"
                    if pieces[1] == "openai":
                        return "google_openai"
                    if pieces[1] == "models":
                        return "google_native"
            if host == "aiplatform.googleapis.com" or host.endswith("-aiplatform.googleapis.com"):
                return "google_vertex"
            if host == "api.anthropic.com" and path.rstrip("/") == "/v1/messages":
                return "anthropic_messages"
            if host == "api.xiaomimimo.com" and path.rstrip("/") == "/v1/chat/completions":
                return "xiaomi_chat"
            if host == "dashscope-intl.aliyuncs.com" and path.rstrip("/") == "/compatible-mode/v1/chat/completions":
                return "alibaba_chat"
            if host == "api.groq.com" and path.rstrip("/") == "/openai/v1/chat/completions":
                return "groq_chat"
            if host == "api.z.ai" and path.rstrip("/") in (
                    "/api/paas/v4/chat/completions", "/api/coding/paas/v4/chat/completions"):
                return "zai_chat"
        except Exception:
            continue
    return ""


def reading(surface, body):
    """Read only an exact documented code on its identified API surface."""
    for rule in SCOPED_RULES:
        if surface != rule["surface"]:
            continue
        value = body
        for field in rule["path"]:
            value = value.get(field) if isinstance(value, dict) else None
        if value == rule["value"]:
            if "messages" in rule:
                inner = body.get("error") if isinstance(body, dict) else None
                message = inner.get("message") if isinstance(inner, dict) else None
                if not isinstance(message, str) or message.strip() not in rule["messages"]:
                    continue
            if rule["reading"].family == catalog.AUTH_REFRESH:
                inner = body.get("error", {}) if isinstance(body, dict) else {}
                details = inner.get("details") if isinstance(inner, dict) else None
                # An explicit invalid-key ErrorInfo is stronger than a generic
                # OAuth-capable status (Vertex Express can also use API keys).
                if isinstance(details, list) and any(isinstance(item, dict)
                    and str(item.get("@type", "")).endswith("/google.rpc.ErrorInfo")
                    and item.get("reason") == "API_KEY_INVALID" for item in details):
                    return None
            return rule["reading"]
    return None


def timing_headers(error, headers, window):
    """Discard only a demonstrably unrelated Groq RPD telemetry header."""
    if api_surface(error) != "groq_chat":
        return headers
    from .quota import _header_items
    import math
    values = {name.strip().lower(): value for name, value in _header_items(headers)}
    try:
        remaining = float(values.get("x-ratelimit-remaining-requests", "nan"))
    except (TypeError, ValueError, OverflowError):
        remaining = float("nan")
    if (window == QuotaWindow.PER_MINUTE and not (math.isfinite(remaining) and remaining <= 0)) or (window == QuotaWindow.UNKNOWN
            and math.isfinite(remaining) and remaining >= 1):
        values.pop("x-ratelimit-reset-requests", None)
    # Positive token balance says nothing about the size of the rejected prompt.
    return values


def trusts_window_reset(error, window, source, headers=None, now_epoch=0):
    """A documented retry instruction for this surface/counter, not a guarantee."""
    surface = api_surface(error)
    if (surface == "groq_chat" and window == QuotaWindow.PER_DAY
            and source == "header.x-ratelimit-reset-tokens"):
        # The selected token deadline can cover the daily deadline only when
        # the same response supplied a valid daily reset. Token timing alone
        # is not evidence of daily recovery. The header reader selected the max.
        from .quota import _header_items, extract_from_headers
        values = {name.strip().lower(): value for name, value in _header_items(headers)}
        raw = values.get("x-ratelimit-reset-requests")
        if isinstance(raw, bool) or (isinstance(raw, str) and raw.lstrip().startswith("-")):
            return False
        daily, _ = extract_from_headers({"x-ratelimit-reset-requests":
            raw}, now_epoch)
        return daily is not None
    return any(surface == rule["surface"] and window == rule["window"]
               and source in rule["sources"] for rule in TIMING_RULES)
