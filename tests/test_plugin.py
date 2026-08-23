"""Tests for the Hermes adapter, against a fake plugin context.

Nothing here imports Hermes. The point is to prove the adapter obeys the hook
contract — right hook name, right return shape, never raises — without the
installed agent being involved.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
sys.path.insert(0, str(ROOT))

from tests.test_core import PER_DAY_BODY, PER_MINUTE_BODY  # noqa: E402


def _load_plugin():
    """Import the plugin the way Hermes does — by path, as a package.

    The directory name contains hyphens (matching the bundled plugin naming
    convention), so it is not importable by name; the real loader uses a file
    location spec and so does this.
    """
    spec = importlib.util.spec_from_file_location(
        "kame_rotation_under_test",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin_mod = _load_plugin()
_on_api_error_classification = plugin_mod._on_api_error_classification
register = plugin_mod.register


class FakeContext:
    """Stand-in for Hermes' PluginContext — records what got registered."""

    def __init__(self) -> None:
        self.hooks: dict[str, object] = {}
        self.commands: dict[str, object] = {}

    def register_hook(self, name: str, callback) -> None:
        self.hooks[name] = callback

    def register_command(self, name: str, handler, description="", args_hint="") -> None:
        self.commands[name] = handler


class HookOnlyContext:
    """A context with no register_command, standing in for an older Hermes."""

    def __init__(self) -> None:
        self.hooks: dict[str, object] = {}

    def register_hook(self, name: str, callback) -> None:
        self.hooks[name] = callback


class TestRegistration:
    def test_registers_exactly_the_four_hooks_it_needs(self):
        # Three, and no more. Every extra hook is surface the plugin has to
        # keep working across Hermes releases, and the classifier must come
        # first: if hook registration ever fails partway, correct cooldowns
        # are the half worth keeping.
        ctx = FakeContext()
        register(ctx)
        assert list(ctx.hooks) == [
            "transform_api_error_classification",
            "on_session_reset",
            "pre_api_request",
            "post_api_request",
        ]

    def test_registers_all_three_slash_commands(self):
        # register() wraps command registration in try/except so a failure
        # there cannot cost us the rotation hook. That guard would also hide
        # a command that silently stopped registering — this is the assertion
        # that keeps the guard honest.
        ctx = FakeContext()
        register(ctx)
        assert list(ctx.commands) == ["kame-keys", "kame-quota", "kame"]

    def test_hook_survives_a_context_without_commands(self):
        ctx = HookOnlyContext()
        register(ctx)
        assert "transform_api_error_classification" in ctx.hooks

    def test_manifest_hooks_match_registration(self):
        # A manifest that promises hooks the code never registers is a silent
        # contract break; this pins them together. Both manifest keys matter:
        # `hooks` is what the loader reads, `provides_hooks` is what
        # `hermes plugins doctor` validates registrations against, and a
        # mismatch between the two is exactly the drift worth catching.
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")

        declared: dict[str, list[str]] = {}
        current: str | None = None
        for line in manifest.splitlines():
            if not line.startswith((" ", "-", "#")) and line.rstrip().endswith(":"):
                current = line.rstrip()[:-1]
                declared[current] = []
            elif current and line.strip().startswith("- "):
                declared[current].append(line.strip()[2:].strip())

        ctx = FakeContext()
        register(ctx)
        registered = list(ctx.hooks)

        assert declared.get("hooks") == registered
        assert declared.get("provides_hooks") == registered

    def test_core_version_matches_the_manifest(self):
        # ``core`` exists to be lifted into another host, so the number that
        # travels with it has to name the rules that came along. Left to a
        # human it drifted four releases without anyone noticing, which is
        # what makes this a test rather than a note in a checklist.
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        declared = ""
        for line in manifest.splitlines():
            if line.startswith("version:"):
                declared = line.split(":", 1)[1].strip().strip('"').strip("'")
                break
        core = importlib.import_module(f"{plugin_mod.__name__}.core")
        assert declared and core.__version__ == declared


class TestHookResult:
    def test_per_minute_returns_reset_at(self):
        result = _on_api_error_classification(
            provider="gemini", model="gemini-3.6-flash",
            status_code=429, error_body=PER_MINUTE_BODY,
        )
        assert result["reason"] == "rate_limit"
        assert result["should_rotate_credential"] is True
        assert "reset_at" in result["error_context"]

    def test_per_day_benches_until_the_quota_resets(self):
        import time

        from core import seconds_until_pacific_midnight

        result = _on_api_error_classification(
            provider="gemini", status_code=429, error_body=PER_DAY_BODY,
        )
        # The whole point: the deadline is the quota's own reset, not the 1h
        # EXHAUSTED_TTL_429_SECONDS the host would otherwise apply. That is
        # usually much longer and, in the hour before Pacific midnight,
        # correctly shorter — so the assertion is the reset, not a duration.
        assert result["error_context"]["reset_at"] - time.time() == pytest.approx(
            seconds_until_pacific_midnight(), abs=5
        )

    def test_serves_any_provider_that_supplies_evidence(self):
        # v0.0.2 had a Gemini allowlist here and it was a regression: an
        # allowlist promises to do nothing for every provider not on it,
        # including every provider that does not exist yet.
        for name in ("openrouter", "groq", "deepseek", "a-provider-from-2029"):
            result = _on_api_error_classification(
                provider=name, status_code=429,
                error_message="rate limit exceeded, try again in 45s",
            )
            assert result is not None, name
            assert result["reason"] == "rate_limit"

    def test_declines_when_the_response_says_nothing_useful(self):
        # The decline is what keeps the plugin honest: no evidence means the
        # host's own classifier is the better answer.
        assert _on_api_error_classification(
            provider="openrouter", status_code=429,
            error_message="Too Many Requests",
        ) is None

    def test_reads_headers_off_the_exception(self):
        # The richest source Hermes does not read. Every SDK hides it
        # somewhere different, so all three shapes are exercised.
        class WithHeaders(Exception):
            headers = {"retry-after": "42"}

        class WithResponse(Exception):
            class response:  # noqa: N801 - mimics an httpx response attribute
                headers = {"retry-after": "42"}

        class WithResponseHeaders(Exception):
            response_headers = {"retry-after": "42"}

        import time

        for shape in (WithHeaders, WithResponse, WithResponseHeaders):
            result = _on_api_error_classification(
                provider="anthropic", status_code=429,
                error_message="rate limit", error=shape(),
            )
            assert result is not None, shape.__name__
            delay = result["error_context"]["reset_at"] - time.time()
            assert 37 <= delay <= 43, shape.__name__

    def test_a_hostile_exception_costs_only_its_own_evidence(self):
        # getattr on a raising property propagates. Absorbing it here means a
        # badly-behaved SDK object loses us the headers and nothing else —
        # the body is still read.
        class Hostile(Exception):
            @property
            def headers(self):
                raise RuntimeError("nope")

        result = _on_api_error_classification(
            provider="gemini", status_code=429,
            error_body=PER_MINUTE_BODY, error=Hostile(),
        )
        assert result is not None
        assert result["reason"] == "rate_limit"

    def test_permanent_auth_carries_no_reset_at(self):
        result = _on_api_error_classification(
            provider="gemini", status_code=400,
            error_message="API key not valid. Please pass a valid API key.",
        )
        assert result["reason"] == "auth_permanent"
        assert result["retryable"] is False
        assert "error_context" not in result

    def test_reason_is_a_valid_failover_reason_name(self):
        # Hermes coerces `reason` to a FailoverReason member and drops the
        # result if it cannot. These are the names that exist in that enum.
        valid = {
            "auth", "auth_permanent", "billing", "rate_limit", "upstream_rate_limit",
            "overloaded", "server_error", "timeout", "ssl_cert_verification",
            "context_overflow", "payload_too_large", "image_too_large",
        }
        cases = [
            dict(provider="gemini", status_code=429, error_body=PER_MINUTE_BODY),
            dict(provider="gemini", status_code=429, error_body=PER_DAY_BODY),
            dict(provider="gemini", status_code=400, error_message="API key not valid."),
            dict(provider="gemini", status_code=429,
                 error_message="Billing account not enabled."),
        ]
        for case in cases:
            result = _on_api_error_classification(**case)
            assert result["reason"] in valid, result


class TestSafety:
    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("KAME_ROTATION_DISABLED", "1")
        assert _on_api_error_classification(
            provider="gemini", status_code=429, error_body=PER_MINUTE_BODY,
        ) is None

    @pytest.mark.parametrize("value", ["0", "", "off", "no"])
    def test_kill_switch_stays_off_for_falsey_values(self, monkeypatch, value):
        monkeypatch.setenv("KAME_ROTATION_DISABLED", value)
        assert _on_api_error_classification(
            provider="gemini", status_code=429, error_body=PER_MINUTE_BODY,
        ) is not None

    def test_tolerates_unknown_future_kwargs(self):
        # Hermes passes error_type/error_code/error/token counts today and may
        # add more. Extra keys must not break dispatch.
        result = _on_api_error_classification(
            provider="gemini", model="gemini-3.6-flash", status_code=429,
            error_type="RateLimitError", error_code="429", error_body=PER_MINUTE_BODY,
            error=RuntimeError("boom"), approx_tokens=1000, context_length=100000,
            num_messages=12, some_future_field="whatever",
        )
        assert result is not None

    def test_never_raises_on_garbage(self):
        for body in (None, {}, "not a dict", [1, 2, 3], {"error": {"details": None}}):
            _on_api_error_classification(
                provider="gemini", status_code=429, error_body=body,  # type: ignore[arg-type]
            )

    def test_declines_when_core_raises(self, monkeypatch):
        def _boom(**_kwargs):
            raise RuntimeError("core exploded")

        monkeypatch.setattr(plugin_mod, "classify", _boom)
        assert _on_api_error_classification(
            provider="gemini", status_code=429, error_body=PER_MINUTE_BODY,
        ) is None

    def test_does_not_log_error_text(self, caplog):
        # error_message/error_body can carry an unredacted provider dump,
        # including key material on an auth failure.
        secret = "AIzaSyTOTALLY_SECRET_KEY_MATERIAL"
        with caplog.at_level("DEBUG"):
            _on_api_error_classification(
                provider="gemini", status_code=400,
                error_message=f"API key not valid: {secret}",
            )
        assert secret not in caplog.text


class TestCountingWhatItWasAsked:
    """The hook has to say how often it declines, or inert looks like quiet.

    Declining is the common path (decision 39). That is precisely why the
    count matters: without it, a plugin reading every refusal and a plugin
    that stopped recognising a provider's payload six weeks ago produce the
    same silent, healthy-looking install.
    """

    def _counts(self):
        return {
            (row.provider, row.status_code): (row.total, row.sized)
            for row in plugin_mod.runtime.classifications()
        }

    def setup_method(self):
        plugin_mod.runtime.forget_classifications()

    def teardown_method(self):
        plugin_mod.runtime.forget_classifications()

    def test_a_payload_it_reads_is_counted_as_read(self):
        _on_api_error_classification(
            provider="gemini", status_code=429, error_body=PER_MINUTE_BODY,
        )
        assert self._counts()[("gemini", 429)] == (1, 1)

    def test_a_payload_it_declines_is_counted_as_declined(self):
        _on_api_error_classification(
            provider="gemini", status_code=404, error_message="model not found",
        )
        assert self._counts()[("gemini", 404)] == (1, 0)

    def test_a_classifier_that_explodes_counts_as_a_decline_not_as_nothing(self, monkeypatch):
        # The worst version of the invisible failure: the plugin is not merely
        # silent, it is throwing on every error, and the count is the only
        # place that would show it without a debug log turned on.
        def _boom(**_kwargs):
            raise RuntimeError("core exploded")

        monkeypatch.setattr(plugin_mod, "classify", _boom)
        _on_api_error_classification(
            provider="gemini", status_code=429, error_body=PER_MINUTE_BODY,
        )
        assert self._counts()[("gemini", 429)] == (1, 0)

    def test_a_disabled_plugin_counts_nothing(self, monkeypatch):
        # The kill switch means "as if it were not installed", and a counter
        # that keeps running would make the switch look ignored.
        monkeypatch.setenv("KAME_ROTATION_DISABLED", "1")
        _on_api_error_classification(
            provider="gemini", status_code=429, error_body=PER_MINUTE_BODY,
        )
        assert self._counts() == {}

    def test_a_counter_that_fails_never_costs_the_call(self, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise RuntimeError("counter exploded")

        monkeypatch.setattr(plugin_mod.runtime, "note_classification", _boom)
        verdict = _on_api_error_classification(
            provider="gemini", status_code=429, error_body=PER_MINUTE_BODY,
        )
        assert verdict is not None and verdict["reason"]

    def test_the_count_holds_no_part_of_the_error(self):
        secret = "AIzaSyTOTALLY_SECRET_KEY_MATERIAL"
        _on_api_error_classification(
            provider="gemini", status_code=400,
            error_message=f"API key not valid: {secret}",
            error_body={"error": {"message": secret}},
        )
        assert secret not in repr(plugin_mod.runtime.classifications())


class TestAnAnswerThatCarriedNothing:
    """A call that returns is normally proof; an empty one is not.

    ``post_api_request`` is what lets a wrongly benched key back into
    rotation for good — the plugin believes the answer (v0.0.9). The premise
    is that a call which returned produced something, and a squeezed
    free-tier key breaks that premise by answering 200 with nothing in it.
    Believing that answer retires a bench permanently on no evidence at all.

    The rule is *refuse to count it*, not *treat it as a failure*: no
    provider refused anything, so nothing here writes a deadline.
    """

    def setup_method(self):
        plugin_mod.runtime.forget_empty_answers()
        self._seen = []
        self._previous = plugin_mod.__dict__.get("_binding")
        plugin_mod.__dict__["_binding"] = self

    def teardown_method(self):
        plugin_mod.__dict__["_binding"] = self._previous
        plugin_mod.runtime.forget_empty_answers()

    # stands in for the pool binding: the only method the hook calls
    def note_success(self, provider, model):
        self._seen.append((provider, model))

    def _counts(self):
        return {row.provider: row.total for row in plugin_mod.runtime.empty_answers()}

    def test_an_ordinary_answer_is_still_believed(self):
        plugin_mod._on_post_api_request(
            provider="gemini", model="gemini-2.5-pro",
            assistant_content_chars=120, assistant_tool_call_count=0,
        )
        assert self._seen == [("gemini", "gemini-2.5-pro")]
        assert self._counts() == {}

    def test_a_tool_call_with_no_prose_is_still_believed(self):
        plugin_mod._on_post_api_request(
            provider="gemini", model="gemini-2.5-pro",
            assistant_content_chars=0, assistant_tool_call_count=1,
        )
        assert self._seen == [("gemini", "gemini-2.5-pro")]

    def test_an_empty_answer_settles_nothing(self):
        plugin_mod._on_post_api_request(
            provider="gemini", model="gemini-2.5-pro",
            assistant_content_chars=0, assistant_tool_call_count=0,
        )
        assert self._seen == []

    def test_an_empty_answer_is_counted_so_it_can_be_seen(self):
        # Decision 47: a path taken by design still needs a number, or it is
        # indistinguishable from a path that is broken. Here the visible
        # consequence is a bench that is not being released.
        for _ in range(3):
            plugin_mod._on_post_api_request(
                provider="gemini", model="gemini-2.5-pro",
                assistant_content_chars=0, assistant_tool_call_count=0,
            )
        assert self._counts() == {"gemini": 3}

    def test_a_host_that_reports_neither_number_changes_nothing(self):
        # Every Hermes before the fields existed, and every dispatch site that
        # does not pass them. Read as zero this would silence the release path
        # for every provider at once, which is far worse than the bug it fixes.
        plugin_mod._on_post_api_request(provider="gemini", model="gemini-2.5-pro")
        assert self._seen == [("gemini", "gemini-2.5-pro")]
        assert self._counts() == {}

    def test_a_disabled_plugin_counts_nothing(self, monkeypatch):
        monkeypatch.setenv("KAME_ROTATION_DISABLED", "1")
        plugin_mod._on_post_api_request(
            provider="gemini", model="gemini-2.5-pro",
            assistant_content_chars=0, assistant_tool_call_count=0,
        )
        assert self._counts() == {}
        assert self._seen == []

    def test_a_counter_that_fails_never_costs_the_call(self, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise RuntimeError("counter exploded")

        monkeypatch.setattr(plugin_mod.runtime, "note_empty_answer", _boom)
        plugin_mod._on_post_api_request(
            provider="gemini", model="gemini-2.5-pro",
            assistant_content_chars=0, assistant_tool_call_count=0,
        )
        assert self._seen == []

    def test_it_is_decided_before_the_binding_is_even_looked_at(self):
        # The classification half of the plugin runs without a pool binding,
        # and so does this: an install whose binding refused still counts what
        # it saw. Nothing here may depend on the binding existing.
        plugin_mod.__dict__["_binding"] = None
        plugin_mod._on_post_api_request(
            provider="gemini", model="gemini-2.5-pro",
            assistant_content_chars=0, assistant_tool_call_count=0,
        )
        assert self._counts() == {"gemini": 1}


class TestReadingTheSwitchesAtRegistration:
    """The config is read by ``register``, or it is not read at all.

    ``settings`` can be perfect on its own and still never be consulted. The
    wiring is the part that can silently go missing — nothing else in the
    plugin fails when a switch stops being read; it just quietly stays on.
    """

    def setup_method(self):
        plugin_mod.settings.forget()

    def teardown_method(self):
        plugin_mod.settings.forget()

    class _ConfiguredContext(FakeContext):
        def __init__(self, values):
            super().__init__()
            self._values = values

        def get_config(self, key, default=None):
            return self._values.get(key, default)

    def test_a_config_that_disables_the_plugin_is_obeyed(self):
        register(self._ConfiguredContext({"disabled": True}))
        assert plugin_mod._is_disabled() is True

    def test_a_config_that_says_nothing_leaves_it_running(self):
        register(self._ConfiguredContext({}))
        assert plugin_mod._is_disabled() is False

    def test_the_other_switch_is_read_at_the_same_time(self):
        register(self._ConfiguredContext({"spread_disabled": True}))
        assert plugin_mod.settings.is_on(plugin_mod.settings.SPREAD_DISABLED) is True

    def test_a_context_with_no_config_surface_still_registers(self):
        ctx = FakeContext()
        register(ctx)
        assert "transform_api_error_classification" in ctx.hooks
        assert plugin_mod._is_disabled() is False
