# KAME — API Key Rotation for Hermes

**Paste several API keys. KAME picks the healthiest one for every call, reads every refusal, and never lets a rate limit end your turn.**

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
hermes plugins install hermes-kame-api-rotation
hermes plugins enable hermes-kame-api-rotation
```

(Or straight from GitHub: `hermes plugins install Kame696/kame-api-rotation-for-hermes/hermes-kame-api-rotation`.)

Restart Hermes once. Then paste your keys, comma separated, into one provider field:

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
- `/kame-keys` — add and inspect keys in bulk

MIT licence. Issues: <https://github.com/Kame696/kame-api-rotation-for-hermes/issues>
