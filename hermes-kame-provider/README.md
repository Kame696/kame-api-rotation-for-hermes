# hermes-kame-provider

The provider half of [KAME API Rotation](https://github.com/Kame696/kame-api-rotation-for-hermes).

Hermes asks a provider profile for its model client before it builds its own
(`ProviderProfile.create_client`, and `create_messages_client` for Anthropic
Messages endpoints). This `model-provider` plugin registers, for every bundled
API-key provider, a profile that inherits everything from Hermes' own —
endpoints, models, auth, request shaping — and adds only those two client
factories, which ask `hermes-kame-api-rotation` for a client that rotates the
provider's keys on every request: Chat Completions, Responses API and
Anthropic Messages.

- Nothing in Hermes is replaced, wrapped or rebound: the bundled profile
  objects are copied into new ones, and Hermes' own registry keeps them as the
  documented per-home override.
- Without `hermes-kame-api-rotation` installed and loaded for the same profile
  home, or with KAME switched off (`KAME_ROTATION_DISABLED=1`), both answer
  `None` and Hermes builds its own client exactly as it always does. A Hermes
  that does not yet ask `create_messages_client` builds its own Messages
  client; the other wires still rotate.

```bash
hermes plugins install Kame696/kame-api-rotation-for-hermes/hermes-kame-api-rotation
hermes plugins install Kame696/kame-api-rotation-for-hermes/hermes-kame-provider
hermes plugins enable hermes-kame-api-rotation
```

Restart Hermes once after installing. MIT licence.
