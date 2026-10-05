# KAME — API Key Rotation for Hermes

**1.8.1.9 — the carousel through a provider profile, with no runtime
overrides.** Plugin catalog rule 9 asks listed plugins not to replace, wrap or
rebind Hermes core; 1.8.1.8 did, and its catalog update was closed for it
([#131918](https://github.com/NousResearch/hermes-agent/pull/131918)). 1.8.1.9
removes every rebind and keeps the carousel: Hermes asks a provider profile for
its model client (`ProviderProfile.create_client`, the documented way to bring
a transport), and KAME's companion package `hermes-kame-provider` answers with
a client that rotates the keys. Every rotation, wait, refusal-sizing and
continuation decision is 1.8.1.8's, moved unchanged. `hermes plugins validate`
passes both packages with no warnings.

Measured on real Hermes turns against local fake providers, in six scenarios on
the native Gemini wire and the OpenAI wire: the same turns answered and the
same requests per key as 1.8.1.8, steady turns faster on the OpenAI wire
(0.2 s instead of 0.6 s), and no repeated text after a cut answer. KAME steps
aside — Hermes builds its own client and KAME still sizes every refusal — on
Anthropic-Messages and Responses-API endpoints, where Hermes would wrap a
client for another wire. Full notes: [CHANGELOG](https://github.com/Kame696/kame-api-rotation-for-hermes/blob/main/CHANGELOG.md).

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
hermes plugins install Kame696/kame-api-rotation-for-hermes/hermes-kame-provider
hermes plugins enable hermes-kame-api-rotation
```

Both are needed: `hermes-kame-api-rotation` is the carousel, the commands and
the panel; `hermes-kame-provider` (a `model-provider` plugin) is how Hermes asks
it for a client. Either one alone changes nothing — Hermes builds its own client.

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
