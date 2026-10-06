"""KAME bridge — two Hermes seams, until Hermes ships its own.

KAME API Rotation 1.8.2.0 reaches Hermes only through documented seams. Two of
them are proposed upstream and not merged yet:

* ``agent.status_output.notify_turn_status`` (NousResearch/hermes-agent#133474)
  — the live rotation line on the spinner (CLI, TUI, Desktop thinking line).
  Without it the line shows only above the Desktop composer.
* ``ProviderProfile.create_messages_client`` (#133461) — Hermes asking the
  provider profile for its Anthropic Messages client, the way it already asks
  for the chat client. Without it the Messages wire (Anthropic, MiniMax, Kimi
  coding) uses Hermes' own client: KAME still sizes every refusal there, but
  does not pick the key per call.

This plugin installs both, copied from the PRs, on a Hermes that lacks them:

* ``notify_turn_status`` plus a wrapper around ``perform_api_call`` that binds
  the running turn's status rail for the duration of the provider call;
* a wrapper around ``agent.anthropic_adapter.build_anthropic_client`` that asks
  the provider's profile for its Messages client first. The PR passes
  ``provider=`` from each caller; here the callers are unchanged, so the
  provider is read from the caller's own frame (its ``agent``, ``self``, or a
  ``provider`` name it holds).

It rebinds Hermes code, which is why it is a separate plugin and never part of
the catalog entry. Each seam is installed only where Hermes does not already
have the real one, so leaving the bridge installed after updating Hermes is
harmless. The log says which seams it installed and which it found already
present.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import logging
import sys
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)

TURN_STATUS_MAX_CHARS = 300
TURN_STATUS_KINDS = ("lifecycle", "warn", "activity")
_MARK = "_kame_bridge"


# --- #133474: notify_turn_status ------------------------------------------------


def _sink_for(agent: Any) -> Callable[[str, str], bool]:
    """The PR's ``_turn_status_sink``, for one agent."""

    def sink(kind: str, message: str) -> bool:
        if kind == "activity":
            wait_notice = getattr(agent, "_emit_wait_notice", None)
            if callable(wait_notice):
                wait_notice(message)
                return True
            thinking = getattr(agent, "thinking_callback", None)
            if not callable(thinking):
                return False
            thinking(message)
            return True
        emit = getattr(agent, "_emit_status_kind", None)
        if callable(emit):
            emit(kind, message, origin="notify_turn_status")
            return True
        return False

    return sink


def _install_seam(status_output: Any) -> contextvars.ContextVar:
    sink_var: contextvars.ContextVar = contextvars.ContextVar("hermes_turn_status_sink", default=None)

    def notify_turn_status(message: str, *, kind: str = "lifecycle") -> bool:
        """Show ``message`` on the status rail of the turn whose provider call is in flight
        (installed by hermes-kame-bridge; same contract as #133474)."""
        sink = sink_var.get()
        if sink is None or not isinstance(message, str):
            return False
        text = " ".join(message.split())
        if not text:
            return False
        if len(text) > TURN_STATUS_MAX_CHARS:
            text = text[: TURN_STATUS_MAX_CHARS - 1] + "…"
        try:
            return sink(kind if kind in TURN_STATUS_KINDS else "lifecycle", text) is not False
        except Exception:
            logger.debug("kame bridge: turn status sink failed", exc_info=True)
            return False

    setattr(notify_turn_status, _MARK, True)
    status_output._TURN_STATUS_SINK = sink_var
    status_output.TURN_STATUS_MAX_CHARS = TURN_STATUS_MAX_CHARS
    status_output.TURN_STATUS_KINDS = TURN_STATUS_KINDS
    status_output.notify_turn_status = notify_turn_status
    return sink_var


def _wrap_call(original: Callable[..., Any], sink_var: contextvars.ContextVar) -> Callable[..., Any]:
    if getattr(original, _MARK, False):
        return original

    @functools.wraps(original)
    def perform_api_call(agent: Any, *args: Any, **kwargs: Any) -> Any:
        token = sink_var.set(_sink_for(agent))
        try:
            return original(agent, *args, **kwargs)
        finally:
            sink_var.reset(token)

    setattr(perform_api_call, _MARK, True)
    return perform_api_call


def install_status_seam() -> str:
    try:
        import agent.status_output as status_output
    except Exception:
        return "status line: skipped (agent.status_output is not importable)"
    existing = getattr(status_output, "notify_turn_status", None)
    if existing is not None and not getattr(existing, _MARK, False):
        return "status line: not needed (this Hermes has notify_turn_status, #133474)"
    if existing is not None:
        return "status line: already installed"
    try:
        import agent.turn_api_call as turn_api_call
    except Exception:
        return "status line: skipped (agent.turn_api_call is not importable)"
    original = getattr(turn_api_call, "perform_api_call", None)
    if not callable(original):
        return "status line: skipped (perform_api_call not found)"
    sink_var = _install_seam(status_output)
    wrapped = _wrap_call(original, sink_var)
    turn_api_call.perform_api_call = wrapped
    # conversation_loop imports the function by name, so its reference is the
    # one the turn actually calls.
    try:
        import agent.conversation_loop as conversation_loop

        if getattr(conversation_loop, "perform_api_call", None) is original:
            conversation_loop.perform_api_call = wrapped
    except Exception:
        logger.debug("kame bridge: conversation_loop not patched", exc_info=True)
    return "status line: installed (notify_turn_status bound around perform_api_call)"


# --- #133461: ProviderProfile.create_messages_client ----------------------------

#: Names a caller of ``build_anthropic_client`` keeps the provider under, in the
#: order they are trusted: an explicit switch target first, then a name, then
#: the object that carries one.
_PROVIDER_NAMES = ("new_provider", "provider", "provider_id")
_PROVIDER_HOLDERS = ("req", "agent", "self")


def _caller_provider(depth: int = 2) -> Optional[str]:
    """The provider the code calling ``build_anthropic_client`` is working for."""
    try:
        frame = sys._getframe(depth)
    except ValueError:
        return None
    try:
        for _ in range(3):
            if frame is None:
                return None
            local = frame.f_locals
            for name in _PROVIDER_NAMES:
                value = local.get(name)
                if isinstance(value, str) and value:
                    return value
            for name in _PROVIDER_HOLDERS:
                try:
                    value = getattr(local.get(name), "provider", None)
                except Exception:  # a half-built object's __getattr__ may raise anything
                    value = None
                if isinstance(value, str) and value:
                    return value
            frame = frame.f_back
        return None
    finally:
        del frame


def _profile_messages_client(provider: str, **client_kwargs: Any) -> Any:
    """``provider``'s registered profile's own Messages-wire client, or ``None`` (#133461)."""
    try:
        from providers import get_provider_profile

        profile = get_provider_profile(provider)
    except Exception:
        return None
    create = getattr(profile, "create_messages_client", None)
    if not callable(create):
        return None
    try:
        return create(**client_kwargs)
    except Exception:
        logger.warning("kame bridge: provider profile %r failed to create a Messages client; "
                       "building the standard one", provider, exc_info=True)
        return None


#: Set while a profile builds its Messages client. A rotating client builds one
#: plain Hermes client per key through this same function, and those inner
#: calls must reach Hermes' builder, not the profile again.
_BUILDING: contextvars.ContextVar = contextvars.ContextVar("kame_bridge_building_messages", default=False)


def _wrap_builder(original: Callable[..., Any]) -> Callable[..., Any]:
    if getattr(original, _MARK, False):
        return original

    @functools.wraps(original)
    def build_anthropic_client(api_key: Any, base_url: Any = None, timeout: Any = None, *,
                               drop_context_1m_beta: bool = False, **extra: Any) -> Any:
        provider = extra.pop("provider", None)
        if _BUILDING.get():
            return original(api_key, base_url, timeout, drop_context_1m_beta=drop_context_1m_beta, **extra)
        provider = provider or _caller_provider()
        if provider:
            token = _BUILDING.set(True)
            try:
                supplied = _profile_messages_client(
                    provider, api_key=api_key, base_url=base_url, timeout=timeout,
                    drop_context_1m_beta=drop_context_1m_beta,
                )
            finally:
                _BUILDING.reset(token)
            if supplied is not None:
                logger.info("%s Messages client created from provider profile (kame bridge)", provider)
                return supplied
        return original(api_key, base_url, timeout, drop_context_1m_beta=drop_context_1m_beta, **extra)

    setattr(build_anthropic_client, _MARK, True)
    return build_anthropic_client


def install_messages_seam() -> str:
    try:
        import agent.anthropic_adapter as anthropic_adapter
    except Exception:
        return "messages client: skipped (agent.anthropic_adapter is not importable)"
    original = getattr(anthropic_adapter, "build_anthropic_client", None)
    if not callable(original):
        return "messages client: skipped (build_anthropic_client not found)"
    if getattr(original, _MARK, False):
        return "messages client: already installed"
    try:
        if "provider" in inspect.signature(original).parameters:
            return "messages client: not needed (this Hermes has create_messages_client, #133461)"
    except (TypeError, ValueError):
        return "messages client: skipped (build_anthropic_client has no readable signature)"
    # Every caller imports the function from the module at call time, so the
    # module attribute is the one they all reach.
    anthropic_adapter.build_anthropic_client = _wrap_builder(original)
    return "messages client: installed (profiles are asked for their Messages client first)"


def install() -> List[str]:
    """Install whichever seams this Hermes lacks. Returns what happened, for the log."""
    return [install_status_seam(), install_messages_seam()]


def register(ctx: Any) -> None:
    for outcome in install():
        logger.info("hermes-kame-bridge: %s", outcome)
