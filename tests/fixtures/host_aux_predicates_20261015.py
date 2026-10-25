"""A TEXT copy of the host's auxiliary-call error predicates (Hermes ``agent/auxiliary_client.py``
and ``agent/error_classifier.py`` as read on 2026-10-15 and again 2026-10-24).

The real host is never imported by a test of this project (a suite with the real hermes-agent on
PYTHONPATH once rewrote the launcher). ``tools/host_assumptions.py`` re-reads the real files as text
and fails when the lines this copy mirrors change, so a drift is noticed there, not here.
"""
BILLING_PATTERNS = (
    "insufficient credits", "insufficient_quota", "insufficient balance", "credit balance",
    "credits exhausted", "credits have been exhausted", "requires available credits",
    "account balance is too low", "no usable credits", "top up your credits", "payment required",
    "billing hard limit", "exceeded your current quota", "account is deactivated", "plan does not include",
    "out of extra usage", "out of funds", "run out of funds", "balance_depleted",
    "budget limit exceeded", "hard billing limit",
    "not available on the free tier", "free tier",
)
PAYMENT_KEYWORDS = BILLING_PATTERNS + (
    "credits", "insufficient funds", "can only afford", "billing",
    "isn't available on the free tier", "key limit exceeded", "budget limit",
    "requires a subscription", "upgrade for access", "upgrade for higher limits",
    "reached your session usage limit", "quota exceeded", "quota_exceeded",
    "too many tokens per day", "daily limit", "tokens per day", "daily quota", "resource exhausted",
    "resource_exhausted", "resource-exhausted", "resourceexhausted",
    "weekly usage limit", "weekly limit",
)
RATE_LIMIT_KEYWORDS = ("rate limit", "rate_limit", "too many requests", "try again", "retry after", "resets in")
RATE_LIMIT_BILLING_KEYWORDS = (
    "credits", "insufficient funds", "billing", "payment required", "can only afford",
    "out of funds", "run out of funds", "balance_depleted", "no usable credits",
    "model_not_supported_on_free_tier", "not available on the free tier", "isn't available on the free tier",
)
TIMEOUT_NO_RETRY_TASKS = frozenset({"compression", "vision", "title_generation"})


def contains_any(text, needles):
    return any(kw in text for kw in needles)


def is_payment_error(exc):
    status = getattr(exc, "status_code", None)
    return status == 402 or (status in {403, 404, 429, None} and contains_any(str(exc).lower(), PAYMENT_KEYWORDS))


def is_rate_limit_error(exc):
    if type(exc).__name__ == "RateLimitError":
        return True
    if getattr(exc, "status_code", None) != 429:
        return False
    err = str(exc).lower()
    return contains_any(err, RATE_LIMIT_KEYWORDS) or not contains_any(err, RATE_LIMIT_BILLING_KEYWORDS)


def is_timeout_error(exc):
    return "Timeout" in type(exc).__name__ or "timed out" in str(exc).lower()


def is_connection_error(exc):
    if contains_any(type(exc).__name__, ("Connection", "Timeout", "DNS", "SSL")):
        return True
    return contains_any(str(exc).lower(), (
        "connection refused", "name or service not known", "no route to host",
        "network is unreachable", "timed out", "connection reset",
        "incomplete chunked read", "peer closed connection", "response ended prematurely",
        "unexpected eof", "remoteprotocolerror", "localprotocolerror",
    ))


def exc_http_status(exc):
    return getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)


def is_transient_transport_error(exc):
    if is_connection_error(exc):
        return True
    status = exc_http_status(exc)
    return isinstance(status, int) and (status == 408 or 500 <= status < 600)


def is_auth_error(exc):
    status = getattr(exc, "status_code", None)
    if status == 401:
        return True
    err = str(exc).lower()
    if "error code: 401" in err or "authenticationerror" in type(exc).__name__.lower():
        return True
    return "bad-credentials" in err and (status == 403 or "unauthenticated" in err)


def credential_rung_accepts(exc):
    return is_auth_error(exc) or is_payment_error(exc) or is_rate_limit_error(exc)


def should_skip_same_provider_retry(task, exc):
    return task in TIMEOUT_NO_RETRY_TASKS and is_timeout_error(exc) and "no-progress timeout" not in str(exc)
