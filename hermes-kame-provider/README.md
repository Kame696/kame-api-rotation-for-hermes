# hermes-kame-provider

The provider half of [KAME API Rotation](https://github.com/Kame696/kame-api-rotation-for-hermes).

Hermes asks a provider profile for its model client before it builds its own
(`ProviderProfile.create_client`). This `model-provider` plugin registers, for
every bundled API-key chat-completions provider, a profile that inherits
everything from Hermes' own — endpoints, models, auth, request shaping — and
adds only `create_client`, which asks `hermes-kame-api-rotation` for a client
that rotates the provider's keys on every request.

- Nothing in Hermes is replaced, wrapped or rebound: the bundled profile
  objects are copied into new ones, and Hermes' own registry keeps them as the
  documented per-home override.
- Without `hermes-kame-api-rotation` installed and loaded for the same profile
  home, with KAME switched off (`KAME_ROTATION_DISABLED=1`), or for a request
  that may need a wire other than chat completions (Anthropic Messages,
  Responses API), `create_client` answers `None` and Hermes builds its own
  client exactly as it always does.

```bash
hermes plugins install Kame696/kame-api-rotation-for-hermes/hermes-kame-api-rotation
hermes plugins install Kame696/kame-api-rotation-for-hermes/hermes-kame-provider
hermes plugins enable hermes-kame-api-rotation
```

Restart Hermes once after installing. MIT licence.
