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
- **Voice:** an optional speech provider `kame-gemini` speaks with your rotated Gemini keys (`tts.provider: kame-gemini`).
- **Optional bridge:** `hermes-kame-bridge` (separate, not in the catalog) adds the spinner line early. It patches
  Hermes at runtime and is not supported by Hermes.

Full documentation, screenshots and FAQ: <https://github.com/Kame696/kame-api-rotation-for-hermes> · MIT licence ·
Issues: <https://github.com/Kame696/kame-api-rotation-for-hermes/issues>
