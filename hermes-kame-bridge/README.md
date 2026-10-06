# hermes-kame-bridge

An optional companion to **hermes-kame-api-rotation 1.8.2.0**. It is not part
of the Hermes plugin catalog entry.

KAME reaches Hermes only through documented seams. Two of those seams are
proposed upstream and not merged yet. This plugin adds them, copied from the
pull requests, to a Hermes that lacks them:

| Seam | Hermes PR | What it gives you |
|---|---|---|
| `agent.status_output.notify_turn_status` | [#133474](https://github.com/NousResearch/hermes-agent/pull/133474) | KAME's live line (key health, "trying the next key", "next key in 2m 10s") on the spinner in the CLI, the TUI and the Desktop. Without this seam, the line appears only above the Desktop composer. |
| `ProviderProfile.create_messages_client` | [#133461](https://github.com/NousResearch/hermes-agent/pull/133461) | Per-call key rotation on the Anthropic Messages wire (Anthropic, MiniMax, Kimi coding). Without this seam, that wire uses Hermes' own client: KAME sizes each refusal, but does not pick the key. |

- **A seam Hermes already ships is left alone.** The log says `not needed` for
  it, so the bridge is safe to keep after updating Hermes. Remove it once both
  PRs are in your Hermes.
- **A seam Hermes lacks is added by patching Hermes**
  (`perform_api_call`, `build_anthropic_client`). The catalog does not allow
  that, so the bridge is a separate plugin.

## Install

Copy the folder next to `hermes-kame-api-rotation` in `~/.hermes/plugins/`,
then enable it:

```bash
hermes plugins enable hermes-kame-bridge
```

Restart Hermes afterwards.
