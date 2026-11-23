# 🐢⚡ API Rotation (KAME) for Hermes

**Many API keys and models as one pool. Rate limits and quota errors never stop your agent again.**

Free, open source, no telemetry. Gemini, OpenAI, OpenRouter, Anthropic, NVIDIA, DeepSeek or any provider.

## What it does

- 🔑 **Many keys, one field.** Paste `key1,key2,key3` where your key already is. Every call goes to the healthiest key.
- 🧠 **Reads every error.** A 429, a spent daily quota, an outage or a dead key each get their own wait: the provider's own number when it gives one.
- 🔁 **Keeps going.** When one model is busy, your next model answers. When every key rests, it asks one again on its own, so a wrong "daily limit" label no longer needs Reset pool.
- 👀 **You see it live.** The status bar and the chat say what it is doing, like "↻ gemini-3-flash: trying another key".

## Install (1 minute)

1. **Hermes app:** Capabilities → Plugins → **API Rotation** → **Install**. (Command line: `hermes plugins install Kame696/kame-api-rotation-for-hermes/hermes-kame-api-rotation` then `hermes plugins enable hermes-kame-api-rotation`.)
2. **Add your keys:** in the field where your provider's key already is (dashboard or `~/.hermes/.env`), paste all of them separated by commas:
   ```
   GOOGLE_API_KEY=AIza...key1,AIza...key2,AIza...key3
   ```
3. **Restart Hermes once.** Done. The API Rotation settings page has a step-by-step keys guide and checks each key you add.

## Commands

| Command | What for |
|---|---|
| `/kame` | pool health: which keys answer, which rest and until when |
| `/kame doctor` | is everything working, and what only you can fix |
| `/kame-keys add <keys>` | add keys (each one checked) |
| `/kame-keys remove-all` | take API Rotation's keys out before you remove the plugin |
| `/kame-quota` | quota per key and per model |

## Good to know

- **Extra requests:** a slow reply may be raced by your next model, and while every key rests one of them is asked
  again after 20 s, 40 s, 80 s, 160 s, then every 5 minutes. Providers do not bill refused requests.
- **Privacy:** keys stay in Hermes' own files; nothing is sent anywhere but to your provider; no key is ever printed.
- **What it writes:** with `/kame-keys add|import`, or a key field holding several keys, one pool row per key in
  `auth.json` through Hermes' pool API (a backup first, last 5 kept); settings as `KAME_*` lines in `.env`; small
  local logs, a status file and per-model health counters in the profile (no key, prompt or text). The Desktop page
  reads them through the plugin's own route and, after a fallback answered, sets that chat's model back
  (`config.set --session`, never global).
- **Where it connects:** only your provider's own endpoints. `/kame-keys add` checks each new Gemini key with one
  request to Google.
- **Waiting:** when every key rests and no model of your list can take over, chats keep waiting (each key at most
  an hour) and cron gives up after 300 s; `max_total_wait_seconds` bounds it.
- **Voice:** an optional speech provider `kame-gemini` speaks with your rotated Gemini keys (`tts.provider: kame-gemini`).

Full documentation, screenshots and FAQ: <https://github.com/Kame696/kame-api-rotation-for-hermes> · MIT licence ·
Issues: <https://github.com/Kame696/kame-api-rotation-for-hermes/issues>
