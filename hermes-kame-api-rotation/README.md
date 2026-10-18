# KAME — API Key Rotation for Hermes

**1.8.2.0 — no runtime overrides, and nothing given up for it.** Hermes asks a
provider profile for its model client (`ProviderProfile.create_client`, the
documented way to bring a transport), and KAME's companion package
`hermes-kame-provider` answers with a client that rotates the keys. No Hermes
module, class or dict is rebound. The Desktop panel uses only
`@hermes/plugin-sdk`, through its own backend route (`dashboard/plugin_api.py`).
`hermes plugins validate` passes both packages.

The rotation, wait, refusal-sizing and continuation decisions are the same as
in 1.8.1.8. The Events tab now shows every call, and settings live under
Desktop Settings ▸ Plugins. Full notes: [CHANGELOG](https://github.com/Kame696/kame-api-rotation-for-hermes/blob/main/CHANGELOG.md).

**Two Hermes seams that are not merged yet.** KAME uses each one as soon as
your Hermes has it:

- **`notify_turn_status`
  ([#133474](https://github.com/NousResearch/hermes-agent/pull/133474)):** the
  live status line ("next key in 2m 10s") on the spinner. Without it, the line
  appears only above the Desktop composer.
- **`create_messages_client`
  ([#133461](https://github.com/NousResearch/hermes-agent/pull/133461)):**
  per-call key rotation on the Anthropic Messages wire. Without it, KAME sizes
  each refusal there but does not pick the key.

Both seams arrive with those Hermes pull requests. Until then, the author's
repository also has an optional `hermes-kame-bridge` package that adds them
early. It **patches Hermes core at runtime**, is **not supported by Hermes**,
and is not part of this catalog entry.

**Paste several API keys. KAME picks the healthiest one for every call, reads every refusal, and keeps recoverable rate limits from prematurely ending your turn.**

Smart API key rotation, 429 / `RESOURCE_EXHAUSTED` recovery and rate-limit failover for Hermes — Gemini, OpenAI, OpenRouter, Anthropic, or any provider. No third-party package, no telemetry, no key ever printed.

Full documentation and screenshots: <https://github.com/Kame696/kame-api-rotation-for-hermes>

## In 30 seconds

- **One field, many keys.** Put `key1,key2,key3` in the provider field you already use.
- **The healthiest key, every call** — fewest requests in the last 60 seconds, least recently used on a tie.
- **It reads the error instead of guessing.** Throttle, daily quota, outage and dead key each get their own wait — the provider's own number whenever it states one.
- **No key sits out longer than an hour**, and KAME never silently switches you to another model.
- **When every key is resting, it waits** for the first one back and tells you how long. For cron or an unattended gateway, `max_total_wait_seconds` (off by default) bounds that wait. Past the bound, the provider's refusal goes to Hermes.

## Install

```bash
hermes plugins install Kame696/kame-api-rotation-for-hermes/hermes-kame-api-rotation
hermes plugins install Kame696/kame-api-rotation-for-hermes/hermes-kame-provider
hermes plugins enable hermes-kame-api-rotation
```

Both are needed: `hermes-kame-api-rotation` is the carousel, the commands and
the panel; `hermes-kame-provider` (a `model-provider` plugin) is how Hermes asks
it for a client. Either one alone changes nothing — Hermes builds its own client.

Restart Hermes once. This also mounts the panel's backend route. The Desktop panel ships in `desktop/plugin.js`. Turn it on in Desktop Settings → Plugins, and you will find KAME's settings there too. Then paste your keys, comma separated, into one provider field:

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
