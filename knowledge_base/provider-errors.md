# Provider error catalogue

> **What this is.** Every failure shape KAME has to read, per provider, with the
> exact field it lives in. Compiled 2026-08-28 from vendor documentation and
> from nine days of this machine's own production telemetry.
>
> **Why it exists.** KAME's classifier was matching prose. Prose is the weakest
> evidence a provider gives you and the only one that is ambiguous *across*
> providers — Google's per-minute throttle and OpenAI's out-of-credits refusal
> are, word for word, the same sentence. Every row below is a machine-readable
> field that says the same thing without the ambiguity.
>
> **Encoded in:** `hermes-kame-api-rotation/core/catalog.py`. This document is
> the reasoning; that file is the table. They change together.

---

## 0. The rule this catalogue serves

**Evidence, never identity.** Nothing here is a list of blessed providers. Each
row is a *shape* — a field name and a value — that any provider may produce.
A provider that appears nowhere in this document is classified by the same
rules as one that appears twice; the names are here because that is how a human
verifies the row, not because the code branches on them.

The ordering rule that follows from it:

```
structured type/code   >   status code   >   headers   >   prose
```

Prose is last because it is the only one two providers can share while meaning
opposite things.

---

## 1. The ambiguity that cost the most

Google's **per-minute** free-tier 429:

> `You exceeded your current quota, please check your plan and billing details.`

OpenAI's **out-of-credits** 429 (`insufficient_quota`):

> `You exceeded your current quota, please check your plan and billing details.`

Identical. One clears in sixty seconds; the other clears when somebody pays.
KAME's legacy table listed both `exceeded your current quota` and `billing` as
daily/account markers, so **every Gemini throttle was read as a daily cap** and
benched for an hour. Measured on this machine: **1,088 occurrences** of that
Google sentence in nine days, **79** resulting `daily [429] — resting 1h 0m`
lines, across a pool of fourteen keys.

What separates them, always:

| | Google per-minute | OpenAI out of credits |
|---|---|---|
| `error.status` | `RESOURCE_EXHAUSTED` | — |
| `error.type` | — | `insufficient_quota` |
| `error.code` | — | `insufficient_quota` |
| `details[].@type` | `google.rpc.QuotaFailure` + `RetryInfo` | — |
| `quotaId` | `...PerMinute...` / `...PerDay...` | — |
| `retryDelay` | present | absent |

Two fields, and the question is settled without reading a word of the sentence.

---

## 2. Google Gemini (native adapter)

Body shape:

```json
{"error": {
  "code": 429,
  "message": "You exceeded your current quota, ...",
  "status": "RESOURCE_EXHAUSTED",
  "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
     "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                     "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                     "quotaDimensions": {"location": "global", "model": "gemini-3.7-flash"}}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "21s"},
    {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "RATE_LIMIT_EXCEEDED",
     "metadata": {"service": "generativelanguage.googleapis.com"}}
  ]}}
```

| HTTP | `status` | Means | KAME verdict |
|---|---|---|---|
| 400 | `INVALID_ARGUMENT` | malformed request | terminal — **unless** the message says the key is invalid |
| 400 | `FAILED_PRECONDITION` | **free tier unavailable in your country; enable billing** | **billing — NOT terminal.** A 400 that is an account problem |
| 403 | `PERMISSION_DENIED` | key lacks permission / tuned-model auth | denial, bench and re-probe |
| 404 | `NOT_FOUND` | model or file missing | terminal for the *request*, never the key |
| 429 | `RESOURCE_EXHAUSTED` | RPM, TPM, RPD or spend | **read `quotaId` for the window** |
| 500 | `INTERNAL` | provider fault | server, short rest |
| 503 | `UNAVAILABLE` | overloaded | server, short rest |
| 504 | `DEADLINE_EXCEEDED` | too long | timeout, short rest |

**The `quotaId` is the whole answer** and it is a single string:

| Fragment | Window |
|---|---|
| `...PerMinute...` | per-minute — seconds, not hours |
| `...PerDay...` | per-day |
| `...PerProjectPerModel...` | scope: this model only |
| `...PerProject...` (no model) | scope: shared across models |
| `-FreeTier` | tier marker, **not** a window |

⚠️ **Two host-side traps, both live in Hermes.**

1. `agent/gemini_native_adapter.py` appends `_FREE_TIER_GUIDANCE` to every
   free-tier 429, and that paragraph contains *"a few hundred **requests/day**"*.
   Any day-marker table matches `/day` and converts a 60-second throttle into a
   1-hour bench. Strip the block before matching — `host_text.py`.
2. The same adapter harvests **only** `google.rpc.ErrorInfo` from `details[]`
   into `exc.details`, discarding `QuotaFailure` and `RetryInfo`. So
   `exc.retry_after` is populated only from a `Retry-After` header, **and Gemini
   does not send one**. Result on this machine: `reset_at` null on **276 of 276**
   recorded blocks. The raw body has to be read off `exc.response`.

Invalid key arrives as **400**, not 401: `INVALID_ARGUMENT`, reason
`API_KEY_INVALID`, message `API key not valid. Please pass a valid API key.`
Checking status before text aborts the whole run over one dead key in a pool of
fourteen.

Legacy "Standard" Google Cloud keys have been rejected since 2026-06-19 and stop
working entirely in September 2026 — a 401 whose message misleadingly suggests
OAuth. Hermes appends `_STANDARD_KEY_GUIDANCE` for this one.

---

## 3. NVIDIA NIM (`integrate.api.nvidia.com`)

OpenAI-compatible endpoint, **RFC 7807 problem+json error body**:

```json
{"status": 429, "title": "Too Many Requests"}
```

| Fact | Consequence for KAME |
|---|---|
| `status` is an **int in the body**, and the exception often carries `status_code=None` | the status must be read from the body, or a 429 is invisible |
| The field is `title`, not `type` or `code` | `structured_error_tokens` read `type`/`code` only — NVIDIA said "Too Many Requests" in a field nobody looked at |
| **No `Retry-After`. No `X-RateLimit-*`.** | nothing to size from; a fixed short rest is the only honest answer |
| Sometimes **429 with no body at all** | reported repeatedly on the vendor forum |
| Free tier ≈ 40 RPM, and 429s occur well below it | the limit is account-wide, not per-key |

Measured here: **15 of 38** NVIDIA blocks arrived with `status_code=None`, and
every `rate_limit` was immediately followed by a `401 api_error` on the same
identity in the same second — the signature of a comma-joined key list being
sent as one credential.

**The right treatment is a short flat rest, not the escalating ladder.** NVIDIA
burst limits clear in seconds; the log shows `resting 20s` → `40s` → `1m 20s`
and then a successful answer on attempt 6, i.e. the escalation was pure waste.

---

## 4. OpenAI and OpenAI-compatible

```json
{"error": {"message": "...", "type": "insufficient_quota",
           "param": null, "code": "insufficient_quota"}}
```

| `type` / `code` | HTTP | Verdict |
|---|---|---|
| `invalid_request_error` | 400 | terminal (request) |
| `invalid_api_key` | 401 | key is dead |
| `insufficient_quota` | 429 | **billing** — waiting achieves nothing |
| `billing_hard_limit_reached` | 429 | billing |
| `rate_limit_exceeded` | 429 | throttle — rotate |
| `model_not_found` | 404 | terminal (request), key is fine |
| `context_length_exceeded` | 400 | terminal (request) |
| `server_error` | 5xx | server |

The **only** thing separating `insufficient_quota` from `rate_limit_exceeded` is
the `type`/`code` field. The messages are interchangeable.

---

## 5. Anthropic

```json
{"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
```

| HTTP | `error.type` | Verdict |
|---|---|---|
| 400 | `invalid_request_error` | terminal — **but also returned on an org/workspace spend limit** |
| 401 | `authentication_error` | key dead |
| **402** | **`billing_error`** | **billing** — a status code most rotation engines ignore |
| 403 | `permission_error` | denial |
| 404 | `not_found_error` | terminal (request) |
| 413 | `request_too_large` | terminal (request) |
| 429 | `rate_limit_error` | throttle — **unless** it is a tier spend cap |
| 500 | `api_error` | server |
| 504 | `timeout_error` | timeout |
| **529** | `overloaded_error` | server/busy, **not** a spent key |

⚠️ Two traps:

- **A 400 can mean "you hit your spend limit"**, not "your request is malformed".
  A 400-is-always-terminal rule ends a turn that another key could have served.
- **A tier spend-cap 429 carries no `retry-after` and keeps failing.** The
  absence of a retry hint on a 429 is itself evidence: a genuine throttle almost
  always names a wait; a spend cap cannot.

`overloaded_error` is why `_BUSY_PATTERNS` must stay separate from
`_QUOTA_PATTERNS`: 529 "Overloaded" is a server hiccup, and folding it into the
quota family makes a busy endpoint look like a spent key.

---

## 6. OpenRouter (and any aggregator of its shape)

OpenRouter normalises every upstream error into a **stable `error_type`**. This
is the single most useful field any aggregator exposes.

| `error_type` | Verdict |
|---|---|
| `authentication` | key dead |
| `permission_denied` | denial (also: guardrail / moderation block) |
| `payment_required` | billing |
| `rate_limit_exceeded` | throttle — honour `Retry-After` |
| `context_length_exceeded`, `max_tokens_exceeded`, `token_limit_exceeded`, `string_too_long` | **transformed into a *successful* completion with `finish_reason: length`** — never an error at all |
| `server` | server (upstream message is masked) |
| `timeout` | timeout |
| `unmapped` | unknown — `error.metadata.provider_code` carries the original |

⚠️ **The envelope is not about your key.** When an aggregator relays an upstream
failure, `metadata.raw` contains somebody else's error, and an
`API key not valid` in there refers to *their* credential. Reading it marks the
user's healthy aggregator key permanently dead. Detected by shape:
`error.message` opening with "Provider returned error", or `metadata` carrying
`raw` / `provider_name`.

⚠️ **Mid-stream errors arrive in-band as SSE events**, after HTTP 200 is already
committed. They cannot be distinguished by status code — only by the event body.

---

## 7. Groq

Plain HTTP semantics, plus two custom codes worth knowing:

| HTTP | Meaning | Verdict |
|---|---|---|
| 413 | request entity too large | terminal (request) |
| 422 | unprocessable / model hallucination | terminal (request) |
| 424 | failed dependency (remote MCP auth) | terminal (request) |
| **498** | **flex tier at capacity** | **server/busy — retryable.** Not in any standard status set |
| 499 | request cancelled by caller | not a failure |
| 503 | maintenance or overload | server |

---

## 8. DeepSeek

| HTTP | Meaning | Verdict |
|---|---|---|
| 400 | invalid format | terminal (request) |
| 401 | authentication fails | key dead |
| **402** | **insufficient balance** | **billing** |
| 422 | invalid parameters | terminal (request) |
| 429 | rate limit reached | throttle |
| 500 | server error | server |
| 503 | server overloaded | server |

Invalid-key wording: `Your api key: ****0000 is invalid` — the words the other
way round from every "invalid key" pattern. Anthropic phrases it the same way
(`API key is invalid.`).

---

## 9. Z.AI / Zhipu

Numeric codes in the body. The ones that matter:

| Code | Meaning | Verdict |
|---|---|---|
| 1308, 1310 | usage limit reached (`Weekly/Monthly Limit Exhausted`) | quota, long window |
| 1309, 1314 | coding/enterprise package expired | billing |
| 1311 | `Your current subscription plan does not yet include access to ${model}` | denial, **per-model** |
| 1316–1321 | reset moments quoted in the message (`resets at 2026-08-21`) | quota with an absolute anchor |

---

## 10. Alibaba Model Studio / Qwen

The whole 429 family is spelled `Throttling` and **never** "rate limit":
`Throttling`, `Throttling.RateQuota`, `Throttling.BurstRate`,
`Throttling.AllocationQuota`. The word arrives in the **code**, which is why a
bare stem `throttl` is worth matching.

| Code | Verdict |
|---|---|
| `AllocationQuota.FreeTierOnly` — *"The free tier of the model has been exhausted"*, on a **403** | **billing.** The one refusal meaning "this key's free allowance is gone" arrives on the status that is normally handed back untouched |
| `Arrearage` — *"please make sure your account is in good standing"*, on a **400** | billing |

---

## 11. Moonshot / Kimi

OpenAI-compatible. The trap is a **contradiction**: HTTP 429 with
`error.type: "rate_limit_error"` and the message *"The engine is currently
overloaded, please try again later."*

Read as congestion → do not rotate → loop, because the next key would have
worked. **The type outranks the sentence.** Confirming evidence (a type that
says overloaded beside prose that says overloaded) must never flip a verdict;
only a contradiction may.

---

## 12. HuggingFace Inference Providers

`You have exceeded your monthly included credits for Inference Providers` — a
monthly allowance, not a throttle. Hourly re-probing achieves nothing; the daily
cadence is right for a human topping it up.

---

## 13. Cross-provider summary — what to read, in order

| Rank | Source | Fields |
|---|---|---|
| 1 | structured type/code | `error.type`, `error.code`, `error.metadata.error_type`, `error.metadata.provider_code`, `title` (RFC 7807), `error.status` (Google) |
| 2 | structured details | `details[].@type` → `QuotaFailure.violations[].quotaId`, `RetryInfo.retryDelay`, `ErrorInfo.reason` |
| 3 | HTTP status | incl. the unusual ones: **402** billing, **498** capacity, **529** overloaded |
| 4 | headers | `Retry-After`, `X-RateLimit-Reset*`, `RateLimit-Reset` |
| 5 | prose | last, and never alone when 1–4 said anything |

### Statuses a rotation engine gets wrong by default

| Status | Naive reading | Actually |
|---|---|---|
| 400 | terminal request | can be an invalid key (Google), a spend limit (Anthropic), or `Arrearage` (Alibaba) |
| 402 | *not handled at all* | billing — Anthropic, DeepSeek |
| 403 | denial | can be free-tier exhaustion (Alibaba) or a moderation block (OpenRouter) |
| 404 | terminal request | correct — but never touch the credential |
| 429 | throttle | can be billing (`insufficient_quota`, spend cap) |
| 498 | *unknown* | Groq flex capacity — retryable |
| 503 | server | can carry quota text in the body; **status wins** |
| 529 | *unknown* | Anthropic overloaded — retryable |

---

## 14. Sources

- [Gemini API troubleshooting](https://ai.google.dev/gemini-api/docs/troubleshooting)
- [Anthropic API errors](https://docs.anthropic.com/en/api/errors)
- [OpenRouter error handling](https://openrouter.ai/docs/api-reference/errors)
- [Groq API error codes](https://console.groq.com/docs/errors)
- [DeepSeek error codes](https://api-docs.deepseek.com/quick_start/error_codes)
- [NVIDIA NIM 429 reports](https://forums.developer.nvidia.com/t/getting-429-too-many-request-for-nim-cloud-api/335755) ·
  [no-body 429](https://forums.developer.nvidia.com/t/too-many-api-error-429-status-code-no-body/369151) ·
  [account-level 429](https://forums.developer.nvidia.com/t/api-key-rate-limited-429-on-every-request-despite-low-rpm-account-level-issue/377111)
- [OpenAI usage/spend limit errors](https://help.openai.com/en/articles/6614457-why-am-i-getting-an-error-message-stating-that-ive-reached-my-usage-limit)
- This machine's telemetry: `plugin-data/agent-plugin-hermes-kame-api-rotation-d6fbd8bc/state.json`
  (276 blocks, 30 recoveries, 2026-08-17 → 2026-08-26) and
  `logs/agent.log.{1,2,3}`, `logs/errors.log.{1,2}`.
