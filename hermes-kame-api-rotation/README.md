# KAME — API Key Rotation for Hermes

**1.8.1.8 serial reliability release.** Rotation and
classification intelligence are retained. The prior guarded integration was
reviewed in [#117966](https://github.com/NousResearch/hermes-agent/pull/117966#issuecomment-5762488795)
and version 1.8.1.0 landed through [#118402](https://github.com/NousResearch/hermes-agent/pull/118402).
The new pin still needs review: current catalog rule 9 is stricter than that
historical ruling. A green syntactic scan does not remove the runtime wrappers
or establish a new policy exception; the update discloses them explicitly.

### Changes in 1.8.1.8

Optional provider-request parallelism is deferred to a future version requiring
new owner approval. This package contains no racing transport module, activation
flags or panel controls; stale experimental settings cannot enable it. Normal
model-generated tool calls and their existing Gemini stream repair are retained.
The experimental code and failed cancellation evidence remain outside the package.

Recovery validation now exercises actual EOF and truncated HTTP bodies, repeated
productive continuations beyond ten, and preamble plus incomplete tool recovery.
Default continuation is progress-based (-1); explicit 0..10 ceilings remain.
Tool replay keeps the original request and deduplicates only its displayed
preamble: it never synthesizes tool arguments or executes an incomplete call.
Only exact helper-owned diagnostics are deferred during recovery; terminal
notices, other model text, cancellation and warning preferences remain intact.
A synchronous snapshot cache avoids rereading the process's own unchanged
atomic write, with foreign replacement/in-place/delete/home/race controls.
Actual counts, quality checks and narrowly scoped speed results are recorded in
the repository validation report; finite tests are not a universal uptime guarantee.

The frozen runtime passes 3,157 tests, with six platform/isolation skips and
four documented expected failures. Against current Hermes source
`476268f16732e09b93bc7202ee38f773b913707e`, unchanged admission passes all 15
checks; 12 runtime contracts plus 12 mutation controls, four native Gemini
contracts plus four mutations, 15 actual SDK/native TCP stream cases, and 11
three-profile manager/unload/reload/heartbeat checks pass. Pool comparisons
activate the binding in 99 fixture homes, retain the intended hold-ceiling
divergence, and independently prove load spreading and its off switch. The
host's 200-case classifier corpus has only five documented intentional verdict
differences. No new unexpected compatibility failure was found in those gates.

Balanced, disk-inclusive snapshot benchmarks passed same-source and slowdown
controls: measured local plugin cost fell by about 28 milliseconds per tested
call (63-65 percent), not 65 percent off an entire agent answer. The final
runtime answered the live Gemini arithmetic check correctly; an earlier NVIDIA
arithmetic error and provider 429/503/deadline failures remain recorded, not
discarded. A later paired trial's two selected Gemini credentials were daily
spent, so it establishes quota handling, not response quality or speed.
No representative provider-quality improvement or p95 agent-speed guarantee
is claimed. Known quota waits are not bypassed to manufacture a faster result.

Restart Hermes after installing or updating to load the new runtime. The public
GitHub release does not update the catalog's reviewed immutable pin by itself;
the catalog update requires a new PR and maintainer review. Detailed validation
and limitations: <https://github.com/Kame696/kame-api-rotation-for-hermes/blob/main/VALIDATION.md>.
Timing rows describe logical attempts; do not add nested rescue as extra elapsed
time. No universal provider-quality or response-speed improvement is promised.

**Paste several API keys. KAME picks the healthiest one for every call, reads every refusal, and keeps recoverable rate limits from prematurely ending your turn.**

Smart API key rotation, 429 / `RESOURCE_EXHAUSTED` recovery and rate-limit failover for Hermes — Gemini, OpenAI, OpenRouter, Anthropic, or any provider. No third-party package, no telemetry, no key ever printed.

Full documentation and screenshots: <https://github.com/Kame696/kame-api-rotation-for-hermes>

## In 30 seconds

- **One field, many keys.** Put `key1,key2,key3` in the provider field you already use.
- **The healthiest key, every call** — fewest requests in the last 60 seconds, least recently used on a tie.
- **It reads the error instead of guessing.** Throttle, daily quota, outage and dead key each get their own wait — the provider's own number whenever it states one.
- **No key sits out longer than an hour**, and KAME never silently switches you to another model.
- **When every key is resting, it waits** for the first one back and tells you how long.

## Install

```bash
hermes plugins install Kame696/kame-api-rotation-for-hermes/hermes-kame-api-rotation
hermes plugins enable hermes-kame-api-rotation
```

The catalog name `hermes-kame-api-rotation` currently resolves to its previously reviewed 1.8.1.0 pin until the update PR is merged. Use the repository route above for this release; enable the optional Desktop half in the host's Plugins settings.

Restart Hermes once. The Desktop panel ships in `desktop/plugin.js`; turn it on in Desktop Settings → Plugins. Then paste your keys, comma separated, into one provider field:

```
GOOGLE_API_KEY=AIzaSy...aaa,AIzaSy...bbb,AIzaSy...ccc
```

## How KAME reads every error

| The provider says | KAME does |
|---|---|
| 429 with a stated wait (`retryDelay`, `Retry-After`, a reset header) | rests that key exactly that long, up to the 1-hour ceiling |
| Gemini `429 RESOURCE_EXHAUSTED` with no number | 1s, then 2, 4, 8, 16, 32, 64s on each repeat; back to 1s when it answers |
| Any other throttle with no number | a short flat rest, 20–30s |
| A daily quota | a 5-minute re-probe while other keys answer; 1 hour once the whole pool has been silent 20 minutes |
| An account-wide limit | that credential rests on every model of the provider, up to the ceiling |
| Out of credit | 1 hour |
| 5xx / overloaded | 1 second, never escalates |
| Timeout / dropped connection | next key; no rest |
| "This key is invalid / revoked" | out of rotation until you replace it |
| 403 for one model only | that key skips that model only |
| An answer cut mid-stream | continued on another key, joined into one reply |
| Anything it cannot size | left to Hermes' own classifier |

## Commands

- `/kame` — pool health, counters, what is resting and the build fingerprint
- `/kame doctor` — is it rotating correctly, and what only a person can fix
- `/kame get` · `/kame set <key> <value>` · `/kame reset <key>` — settings, live
- `/kame-quota` — the quota picture per key and per model
- `/kame-keys` — add and inspect keys in bulk. `add` and `import` write to `~/.hermes/auth.json`, after saving a plaintext backup `auth.json.kame-<timestamp>.bak` beside it (last 5 kept)

MIT licence. Issues: <https://github.com/Kame696/kame-api-rotation-for-hermes/issues>
