"""Host scenario for 1.9.0.5 (A1, B1): runs inside Hermes' venv. Prints one JSON line.

usage: h1905_host_one_package.py <plugin dir> <home A (with the plugin)> [<home B (without)>]
"""
import json
import os
import sys
from pathlib import Path

plugin_dir, home_a = Path(sys.argv[1]), Path(sys.argv[2])
home_b = Path(sys.argv[3]) if len(sys.argv) > 3 else None
os.environ["HERMES_HOME"] = str(home_a)

from hermes_cli import plugins as hp  # noqa: E402
import hermes_constants  # noqa: E402
import providers  # noqa: E402

out = {}
hp.discover_plugins(force=True)
prof = providers.get_provider_profile("gemini")
out["profile_class"] = type(prof).__name__
out["provider_source"] = providers.provider_source("gemini")
FAKE = "AIzaFAKE_probe_key_0000000000000000000"
cli = prof.create_client(api_key=FAKE, base_url=prof.base_url)
out["a_client_class"] = type(cli).__name__
out["a_client_mro"] = [c.__name__ for c in type(cli).__mro__]
out["a_has_chat_completions"] = hasattr(getattr(cli, "chat", None), "completions")
out["a_base_url"] = str(getattr(cli, "base_url", ""))

# a second install() in the same process registers nothing new
pkg = next((m for n, m in sys.modules.items() if n.endswith("provider_profiles") and hasattr(m, "install")), None)
out["second_install"] = pkg.install() if pkg else "no-module"
out["other_bundled_still_bundled"] = sorted(
    n for n in ("openai-codex", "anthropic") if providers.get_provider_profile(n) is not None
    and providers.provider_source(n) == "bundled")

if home_b is not None:
    tok = hermes_constants.set_hermes_home_override(str(home_b))
    try:
        profb = providers.get_provider_profile("gemini")
        clib = profb.create_client(api_key=FAKE, base_url=profb.base_url)
        out["b_profile_class"] = type(profb).__name__
        out["b_client_class"] = type(clib).__name__
        out["b_client_mro"] = [c.__name__ for c in type(clib).__mro__]
    finally:
        hermes_constants.reset_hermes_home_override(tok)
print(json.dumps(out))
