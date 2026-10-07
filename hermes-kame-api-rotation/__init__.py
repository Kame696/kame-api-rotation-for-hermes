"""Hermes KAME API Rotation — every refusal read for what it actually says,
so Hermes' own credential pool rotates on the truth.

**What changed in 1.8.1.9.** Every release from 1.0.0 to 1.8.1.8 wrapped Hermes
internals — the dispatch functions, the credential pool's methods, the auxiliary
relays, the runtime resolver, a FastAPI route and the Gemini stream translator.
That is exactly what the plugin catalog's rule 9 forbids, and
NousResearch/hermes-agent#131918 was closed for it. This release removes every
one of them. Nothing in this package replaces, wraps or rebinds a Hermes
function, method or module attribute; ``hermes plugins validate`` reports no
core override.

What is left is the part of KAME that was always the reason for it: the
judgement. Hermes already rotates its pool — for free, without spending its
retry budget — whenever a refusal is classified as a credential problem. What it
cannot do is *read* the refusal: a per-minute throttle and a daily cap arrive as
the same 429, a Gemini ``QuotaFailure`` names its own window and model scope in
a structured body the built-in classifier does not parse, and a 503 says nothing
about the key at all. KAME reads all of it and answers through the public hook:

* ``transform_api_error_classification`` — the verdict. ``reason`` decides
  whether Hermes rotates, retries or gives up; ``error_context.reset_at`` says
  how long the refused key rests; ``error_context.quota_scope`` says whether the
  refusal was about one model (``"model"``) or the whole key. KAME declines any
  payload it cannot size, so Hermes' own pipeline decides those.
* ``post_api_request`` — counts answers that came back empty, which a squeezed
  free-tier key returns instead of a refusal. A readout, never a decision.
* ``/kame-keys`` — bulk key intake through the pool's public ``add_entry``.
* ``/kame-quota`` and ``/kame`` — what KAME classified and what the native pool
  looks like right now, read through ``load_pool()``.
* The Desktop panel (``desktop/plugin.js``) — the same readout, through the
  Desktop SDK only.

**Two of the three fields above need a Hermes that reads them.** Hermes' pool
recovery today uses the context it scrapes off the exception itself, so a
hook's ``reset_at`` and ``quota_scope`` are accepted and then not consulted —
see ``PLAN_1.8.1.9.md`` for the small core change that makes them count. On a
Hermes without it, KAME's ``reason`` still steers rotation, and the extra
fields are inert rather than wrong.

**No provider allowlist, deliberately.** An allowlist is a promise to do
nothing for whichever provider is not on it. KAME acts on *evidence* — a retry
attribute, a ``Retry-After`` header, a structured body, a sentence naming a
window — and declines when it has none. The hook is a cold path, fires only on
failure, and runs inside Hermes' isolation wrapper, so a fault here degrades to
the built-in behaviour rather than breaking a call.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from . import integrity, recorder, runtime, settings
from .core import Verdict, answer, classify
from .core.quota import QuotaScope

logger = logging.getLogger(__name__)


PLUGIN_NAME = "hermes-kame-api-rotation"

#: What `integrity.verify()` said at register time, carried so every readout
#: can repeat it. A dict rather than a flag because "incomplete" is only useful
#: alongside *what* is missing — "KAME did not start" and "KAME started without
#: its engine" look identical from a chat window and have opposite fixes.
_INTEGRITY: Dict[str, Any] = {"complete": True, "fingerprint": "", "missing_required": []}

#: The reasons whose bench can be narrowed to one model. Only a throttle: a
#: spent account, a dead key and a refused credential are about the key itself,
#: whatever model asked.
_SCOPABLE_REASONS = frozenset({"rate_limit"})

#: ``max_hold_seconds`` when nothing configures it — the same hour the engine's
#: ``core.carousel.MAX_HOLD_S`` and the setting's own declaration use.
MAX_HOLD_DEFAULT_S = 3600.0

# Escape hatch: KAME_ROTATION_DISABLED=1 turns the plugin into a no-op without
# uninstalling it, so a suspected regression can be ruled out in one restart.
# Also settable as `disabled` under this plugin's own config entry, which is
# where Hermes keeps plugin switches — see ``settings``. The environment wins,
# because an escape hatch that can be overridden by a file is not one.
_DISABLED_ENV = "KAME_ROTATION_DISABLED"


def _is_disabled() -> bool:
    return settings.is_on(settings.ROTATION_DISABLED)


def _to_hook_result(verdict: Verdict) -> Dict[str, Any]:
    """Translate a core verdict into the dict shape the hook expects.

    ``reset_at`` rides inside ``error_context`` because that is the field the
    credential pool normalises into ``last_error_reset_at`` — the value that
    overrides the default TTL when the entry's cooldown is computed.

    Every recovery hint is sent explicitly, including the ones that look like
    defaults. Hermes expands the returned dict into ``ClassifiedError``, whose
    own defaults are ``False`` — an omitted hint is a disabled one, not an
    inherited one.
    """
    # 1.6.0.0. The one place a user can say "do not switch models on me".
    #
    # Hermes reads ``should_fallback`` as permission to answer a spent
    # credential with a different model, and then a different provider. That
    # is a sensible default and a wrong one for a pool whose whole purpose is
    # to wait out a quota and come back on the model that was asked for. The
    # switch is off unless the user turns it on, because the behaviour it
    # overrides is the host's own.
    fallback = verdict.should_fallback
    if fallback and settings.is_on(settings.NO_MODEL_FALLBACK):
        fallback = False
    # A key the pool can rotate past is never a reason to end the turn.
    #
    # ``retryable=False`` tells Hermes to stop. That is the right answer for a
    # request no key could answer, and the wrong one for anything the pool can
    # step around — a spent account, a credential the provider named dead, a
    # refused key. Those verdicts already set ``should_rotate_credential``, and
    # asking to rotate while saying "do not retry" is a contradiction the host
    # resolves by stopping.
    #
    # Measured on the corpus: the two of them together ended turns on refusals
    # where thirteen other keys were healthy. So the invariant is written here,
    # once, instead of being remembered at each of the verdicts that construct
    # it — which is how the last three vocabulary defects got in.
    retryable = bool(verdict.retryable) or bool(verdict.should_rotate_credential)

    result: Dict[str, Any] = {
        "reason": verdict.reason,
        "retryable": retryable,
        "should_rotate_credential": verdict.should_rotate_credential,
        "should_fallback": fallback,
    }
    context: Dict[str, Any] = {}
    if verdict.reset_at is not None:
        # The owner's ceiling (decisions/0004): no credential sits out longer
        # than ``max_hold_seconds``, whatever the provider or the calendar said.
        # Past it the key goes back to selection and the provider is asked
        # again; a refusal costs one request, an over-long hold costs a healthy
        # key for its whole duration, silently. Applied here because the pool
        # stores exactly the number it is handed.
        ceiling = time.time() + settings.number(settings.MAX_HOLD, MAX_HOLD_DEFAULT_S)
        context["reset_at"] = min(float(verdict.reset_at), ceiling)
    # 1.8.1.9. Which models the refusal reaches. Silence is read as one model,
    # as it has been since 0.0.8: Google's free-tier counters are per project
    # *per model*, and the only scope words that widen a bench to the whole key
    # are explicit ones, which ``core.quota`` reports as ``ACCOUNT``. Widening
    # on silence is the 0.0.3 regression — a daily cap on one model benching
    # the key for every other model the conversation and its auxiliary tasks use.
    if verdict.reason in _SCOPABLE_REASONS and verdict.quota_scope != QuotaScope.ACCOUNT:
        context["quota_scope"] = "model"
    if context:
        result["error_context"] = context
    return result




def _headers_from(error: Any) -> Any:
    """Dig the response headers out of whatever exception shape arrived.

    SDKs disagree about where they live — ``exc.headers`` (litellm),
    ``exc.response.headers`` (httpx-based clients), ``exc.response_headers``.
    Rate-limit reset headers are the single richest source of timing the
    host does not read, so it is worth checking all three rather than
    picking one and being right two thirds of the time.

    Every access is guarded: these are attributes on an arbitrary object
    handed to us by whichever SDK failed, and ``getattr`` on a property that
    raises would propagate. Losing the headers costs precision; letting the
    exception escape would cost the body evidence too, and this runs on the
    host's error path.
    """
    if error is None:
        return None
    for attribute in ("headers", "response_headers"):
        try:
            headers = getattr(error, attribute, None)
        except Exception:
            continue
        if headers:
            return headers
    try:
        response = getattr(error, "response", None)
        return getattr(response, "headers", None) if response is not None else None
    except Exception:
        return None


def _count(provider: object, status_code: object, *, sized: bool) -> None:
    """Count one classification, and never fail because of it.

    A counter is worth less than the call it is counting: this runs on the
    host's error path, where an exception would turn a recoverable API error
    into a crash.
    """
    try:
        runtime.note_classification(provider, status_code, sized=sized)
    except Exception:  # pragma: no cover — a bounded dict write does not fail
        logger.debug("%s: could not count the classification", PLUGIN_NAME, exc_info=True)


def _count_empty(provider: object) -> None:
    """Count one answer that carried nothing, and never fail because of it.

    Same rule as ``_count``: this runs on the host's successful path, where
    an exception would turn a completed API call into a crash.
    """
    try:
        runtime.note_empty_answer(provider)
    except Exception:  # pragma: no cover — a bounded dict write does not fail
        logger.debug("%s: could not count the empty answer", PLUGIN_NAME, exc_info=True)


#: Hermes' own parser for a refusal it has a dedicated contract for, looked up
#: once. ``_UNRESOLVED`` until the first refusal asks; ``None`` on a Hermes
#: that has no such parser, which is every version before 0.21.3.
_UNRESOLVED = object()
_WELCOME_PARSER: Any = _UNRESOLVED


def _host_owns(error_body: Any) -> bool:
    """Whether Hermes has its own contract for this exact payload.

    Hermes 0.21.3 added an anonymous, single-credential welcome tier whose 429
    carries a structured body the host parses itself and turns into a message,
    a ``retry_after`` and alternates for the user. Plugin hooks run *before*
    that pipeline and the first verdict wins, so a verdict here would hide the
    host's reading - and there is no pool on that route to rotate anyway.

    Decided on the payload, by the host's own parser, never on a provider's
    name: if Hermes recognises the body as its contract, Hermes classifies it.
    """
    global _WELCOME_PARSER
    if not isinstance(error_body, dict):
        return False
    if _WELCOME_PARSER is _UNRESOLVED:
        try:
            from hermes_cli.anon_auth import parse_welcome_refusal as parser  # type: ignore
        except Exception:
            parser = None
        _WELCOME_PARSER = parser if callable(parser) else None
    if _WELCOME_PARSER is None:
        return False
    try:
        return _WELCOME_PARSER(error_body) is not None
    except Exception:
        return False


def _on_api_error_classification(
    *,
    provider: str = "",
    model: str = "",
    status_code: Optional[int] = None,
    error_message: str = "",
    error_body: Optional[Dict[str, Any]] = None,
    error: Any = None,
    error_type: str = "",
    error_code: str = "",
    **_ignored: Any,
) -> Optional[Dict[str, Any]]:
    """Classify one failure, or decline so the host pipeline runs.

    ``error_type`` and ``error_code`` were in ``**_ignored`` until 1.5.0, with
    a comment explaining that they were discarded. They should not have been:

    * ``error_type`` is literally ``type(error).__name__``
      (``agent/error_classifier.py:680``). It is the only evidence a transport
      failure carries at all — no status, no body, ever — and the only evidence
      left for any SDK class the host's ``RateLimitError -> 429`` repair
      (``:684``) does not cover.
    * ``error_code`` comes from ``_extract_error_code`` (``:1808``), which walks
      the exception's cause chain five deep, parses JSON nested inside
      ``error.message``, and knows spellings this plugin's own path list does
      not. Strictly more than KAME could dig out for itself.

    Both stay keyword-with-default, and the remaining payload still lands in
    ``**_ignored``, so a future Hermes adding a field cannot break dispatch.
    """
    if _is_disabled():
        return None

    # Write down what the provider actually sent, before anything reads it.
    #
    # The corpus this plugin is measured against holds thirteen thousand real
    # refusals and not one carries ``quotaId``, because no version before
    # 1.7.0.0 read the structured body and so no version ever wrote it. The
    # gate can therefore only test the case where nothing names a wait. This
    # line is what ends that: it records the payload as it arrives and returns.
    #
    # ``recorder.record`` never raises and never decides anything. Deleting the
    # module changes no behaviour at all — which is the point of it existing.
    recorder.record(
        provider=provider, model=model, status_code=status_code,
        error_message=error_message, error_body=error_body, error=error,
        error_type=error_type, error_code=error_code,
    )


    if _host_owns(error_body):
        _count(provider, status_code, sized=False)
        return None

    try:
        verdict = classify(
            provider=provider,
            model=model,
            status_code=status_code,
            error_message=error_message or "",
            error_body=error_body,
            headers=_headers_from(error),
            error=error,
            error_type=error_type or "",
            error_code=error_code or "",
        )
    except Exception:
        # Never let a classifier bug turn a recoverable API error into a crash.
        logger.debug("%s: classification failed, deferring to host", PLUGIN_NAME, exc_info=True)
        _count(provider, status_code, sized=False)
        return None

    # Counted before the verdict is used, both ways. Declining is the common
    # path and the safe one, which is exactly why the plugin needs to say how
    # often it happens: an install that reads every refusal and an install
    # that has been inert since the provider changed a payload look the same
    # from every other angle. Provider and status only — never the text.
    _count(provider, status_code, sized=verdict is not None)

    if verdict is None:
        return None


    # Deliberately logs the decision and never the error text — the hook
    # contract warns that error_message/error_body may carry an unredacted
    # provider dump, which for an auth failure can include key material.
    logger.info(
        "%s: %s/%s -> %s [%s via %s] (%s)",
        PLUGIN_NAME,
        provider or "?",
        model or "?",
        verdict.reason,
        verdict.quota_window,
        verdict.source or "-",
        verdict.rationale,
    )
    return _to_hook_result(verdict)


def _on_session_reset(payload=None, **kwargs):
    """Forget the status line, keep the keys' rests.

    A reset clears the chat, not the calendar: cooldowns describe a quota that
    is still spent, so they stay. The spinner line's throttle is what goes, so
    the next line KAME draws in the new conversation is drawn at once. The
    throttle is kept per client, not per session (a client does not know its
    session), so every conversation's is cleared — the cost is at most one
    extra redraw elsewhere, never a line held back.
    """
    session_id = None
    if isinstance(payload, dict):
        session_id = payload.get("session_id")
    if session_id is None:
        session_id = kwargs.get("session_id")
    try:
        from .transport import _Spinner

        _Spinner.reset(session_id)
    except Exception:  # pragma: no cover - defensive only
        logger.debug("%s: could not reset the status line", PLUGIN_NAME, exc_info=True)
    return None


def _on_post_api_request(
    *,
    provider: str = "",
    assistant_content_chars: Any = None,
    assistant_tool_call_count: Any = None,
    **_ignored: Any,
) -> None:
    """Count an answer that carried nothing. Return value is unused by design.

    A squeezed free-tier key can return 200 and no content instead of refusing.
    Hermes treats that as a success, so nothing rotates and nothing is said;
    ``/kame-quota`` is where the count shows up. See ``core.answer``.
    """
    if _is_disabled():
        return
    if answer.carried_nothing(
        content_chars=assistant_content_chars,
        tool_calls=assistant_tool_call_count,
    ):
        _count_empty(provider)


#: The ``ctx`` this namespace registered with, so a second ``register()`` on the
#: same context is a no-op and one on a new context replaces the old resources
#: instead of stacking a second heartbeat.
_REGISTERED_CTX: Any = None

#: What ``/kame``, ``/kame-quota``, the panel and ``control`` read, under the
#: names they have always read: the quota journal's keeper and the transport.
#: ``None`` until ``register`` runs.
_binding: Any = None
_dispatch_binding: Any = None


def register(ctx) -> None:
    global _REGISTERED_CTX
    if _REGISTERED_CTX is ctx:
        return
    if _REGISTERED_CTX is not None:
        _cleanup_runtime()
    _REGISTERED_CTX = ctx
    unload = getattr(ctx, "on_unload", None)
    if callable(unload):
        try:
            unload(_cleanup_runtime)
        except Exception:  # pragma: no cover - a host without disposal still loads
            logger.debug("%s: could not register the unload callback", PLUGIN_NAME, exc_info=True)

    # Before anything else, and loudly: is this a whole plugin?
    #
    # An install that lost `core/` to a non-recursive copy registers perfectly
    # and decides nothing. The check says what is absent, and the answer
    # travels with every readout afterwards. It does not stop registration:
    # the slash commands and the panel are how a person finds out what is
    # wrong, and refusing to register would take away the screen that says so.
    try:
        _INTEGRITY.update(integrity.verify())
        if not _INTEGRITY.get("complete"):
            logger.error("%s: %s", PLUGIN_NAME, integrity.describe(_INTEGRITY))
        else:
            logger.info("%s: %s", PLUGIN_NAME, integrity.describe(_INTEGRITY))
    except Exception:  # pragma: no cover — verify() swallows its own failures
        logger.debug("%s: could not verify the install", PLUGIN_NAME, exc_info=True)

    # First, because everything below asks whether it is switched off, and a
    # switch read after the thing it switches has already run is not a switch.
    try:
        settings.load(ctx)
    except Exception:  # pragma: no cover — ``load`` swallows its own failures
        logger.debug("%s: could not read the plugin settings", PLUGIN_NAME, exc_info=True)

    ctx.register_hook("transform_api_error_classification", _on_api_error_classification)

    # Separately guarded: a readout must never cost the verdict above.
    try:
        ctx.register_hook("post_api_request", _on_post_api_request)
    except Exception:
        logger.debug("%s: post_api_request unavailable", PLUGIN_NAME, exc_info=True)
    try:
        ctx.register_hook("on_session_reset", _on_session_reset)
    except Exception:
        logger.debug("%s: on_session_reset unavailable", PLUGIN_NAME, exc_info=True)

    # The quota journal: refusals and answers, filed by the carousel. Kept under
    # the name the readouts have always looked for (``_binding``).
    globals()["_binding"] = _install_journal(ctx)

    # The carousel itself. ``hermes-kame-provider`` finds this package through
    # the registry and asks ``facade.make_client`` for every API-key provider's
    # client; without that companion installed nothing here is ever asked, and
    # the hook above still sizes and scopes every refusal Hermes sees.
    try:
        import sys as _sys

        from . import facade

        from .transport import configure

        configure(facade.TRANSPORT)
        facade.publish(_sys.modules[__name__])
        globals()["_dispatch_binding"] = facade.TRANSPORT
    except Exception:
        logger.warning("%s: the rotating client is unavailable; refusals are still classified",
                       PLUGIN_NAME, exc_info=True)
        globals()["_dispatch_binding"] = None

    # The slash commands are registered separately and defensively: if command
    # registration ever breaks (a name collision with a future built-in, a
    # Hermes API change), the rotation above must still be in place.
    try:
        from .commands import register_command

        register_command(ctx)
    except Exception:
        logger.warning("%s: /kame-keys unavailable, rotation still active",
                       PLUGIN_NAME, exc_info=True)

    # 1.8.1.8 split ``GOOGLE_API_KEY=k1,k2,k3`` inside Hermes' resolver. With
    # no wrap left to do that, the pool is kept with one row per key instead,
    # at every start, following the variable as it changes (``envsync``).
    try:
        from . import envsync

        envsync.sync()
    except Exception:
        logger.debug("%s: could not sync multi-key variables", PLUGIN_NAME, exc_info=True)

    try:
        from .status import register_command as register_status_command

        register_status_command(ctx, binding=globals().get("_binding"))
    except Exception:
        logger.warning("%s: /kame-quota unavailable, rotation still active",
                       PLUGIN_NAME, exc_info=True)

    try:
        from .menu import register_command as register_menu_command

        register_menu_command(ctx, binding=globals().get("_dispatch_binding"))
    except Exception:
        logger.warning("%s: /kame unavailable, rotation still active",
                       PLUGIN_NAME, exc_info=True)

    # The Desktop half ships at desktop/plugin.js, Hermes' own unified-package
    # door; nothing is copied anywhere. The user turns the panel on in
    # Settings > Plugins. This is the snapshot it reads. Everything above works
    # without it -- it is a readout, and a readout that can break a turn is not
    # worth having, so every failure inside it is logged and swallowed.
    try:
        from . import state

        state.set_pool_binding(globals().get("_binding"))
        state.attach(globals().get("_dispatch_binding"))
        state.publish(force=True)
        _start_state_heartbeat()
    except Exception:
        logger.debug("%s: could not publish the desktop snapshot", PLUGIN_NAME, exc_info=True)


def _install_journal(ctx) -> Any:
    """The journal writer, registered on the two ``runtime`` seams. Never raises."""
    try:
        from .journal_keeper import JournalKeeper
        from .store import JournalStore, LedgerStore

        state = getattr(ctx, "state", None)
        keeper = JournalKeeper(LedgerStore(state), journal=JournalStore(state))
        runtime.set_rotation_recorder(keeper.note_rotation)
        runtime.set_answer_recorder(keeper.note_answered)
        return keeper
    except Exception:
        logger.debug("%s: the quota journal is unavailable", PLUGIN_NAME, exc_info=True)
        return None


#: How often the heartbeat looks for a request from the panel, and how often it
#: republishes the snapshot. The first has to feel immediate — it is the delay
#: between clicking a switch and seeing it move — and the second only has to
#: prove the file is not stale and follow the pool, so one thread does both.
_CONTROL_TICK_S = 1.0
_SNAPSHOT_TICK_S = 5.0


def _env_stamp() -> Any:
    """``(mtime_ns, size)`` of this profile's ``.env``, or ``None``."""
    try:
        from . import envfile

        target = envfile.path()
        if target is None:
            return None
        info = target.stat()
        return (info.st_mtime_ns, info.st_size)
    except OSError:
        return None
    except Exception:  # pragma: no cover
        return None


def _start_state_heartbeat() -> None:
    """Refresh the snapshot on a slow tick, and take the panel's requests on a fast one.

    The rotation path publishes on every change, which is what makes the chip
    move. The slow tick exists for the opposite case: a pool that has been
    healthy and idle for an hour writes nothing, and a reader cannot tell a
    settled Hermes from a dead one except by the age of the file.

    The fast tick is 1.1.1's half. The panel has no way to call into this
    process — see ``control`` — so it leaves a request in a file, and something
    has to look. One second is the whole latency of a switch in the panel, and
    a stat of one path is cheap enough to do every second for ever.

    A daemon thread, so it can never hold the process open, and started once
    per interpreter even if register() runs again.
    """
    import contextvars
    import threading

    if globals().get("_state_heartbeat") is not None:
        return

    def _tick() -> None:
        from . import control, state

        ticks = 0
        env_stamp = _env_stamp()
        while not stop.wait(_CONTROL_TICK_S):
            ticks += 1
            try:
                # Publishes by itself when it applies something, so the panel
                # sees the new value and the outcome in the same read.
                control.poll()
            except Exception:  # pragma: no cover — ``poll`` swallows its own
                logger.debug("%s: control poll failed", PLUGIN_NAME, exc_info=True)
            # 1.8.2.1. A panel request is applied by whichever Hermes process
            # on this profile polls first (Desktop backend, gateway, a second
            # window); it writes .env, but only that process's environment
            # changed. Every other process now picks the edit up from the file
            # within a second, so a setting saved in the panel is in force in
            # the process actually serving the chat.
            stamp = _env_stamp()
            if stamp != env_stamp:
                env_stamp = stamp
                try:
                    from . import settings as _settings

                    changed = _settings.reread_environment()
                    if changed:
                        logger.info("%s: .env changed — now in force here: %s",
                                    PLUGIN_NAME, ", ".join(changed))
                except Exception:  # pragma: no cover - a daemon that cannot die
                    logger.debug("%s: could not reread .env", PLUGIN_NAME, exc_info=True)
            if ticks * _CONTROL_TICK_S < _SNAPSHOT_TICK_S:
                continue
            ticks = 0
            try:
                state.publish(force=True)
            except Exception:  # pragma: no cover - a daemon that cannot die
                logger.debug("%s: snapshot heartbeat failed", PLUGIN_NAME, exc_info=True)

    stop = threading.Event()
    context = contextvars.copy_context()
    thread = threading.Thread(target=context.run, args=(_tick,), name="kame-state", daemon=True)
    globals()["_state_heartbeat_stop"] = stop
    globals()["_state_heartbeat"] = thread
    thread.start()


def _stop_state_heartbeat() -> None:
    """Stop and join this namespace's worker without touching another profile."""
    import threading
    stop = globals().pop("_state_heartbeat_stop", None)
    thread = globals().pop("_state_heartbeat", None)
    if stop is not None:
        stop.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=2.0)


def _cleanup_runtime() -> None:
    """Stop this namespace's heartbeat and take its snapshot down. Idempotent."""
    global _REGISTERED_CTX
    _REGISTERED_CTX = None
    _stop_state_heartbeat()
    try:
        import sys as _sys

        from . import facade

        # Clients already handed out keep working to the end of their request;
        # new ones go back to Hermes' own as soon as this package is gone.
        facade.retract(_sys.modules[__name__])
    except Exception:  # pragma: no cover - a registry pop
        logger.debug("%s: could not retract the rotating client", PLUGIN_NAME, exc_info=True)
    runtime.set_rotation_recorder(None)
    runtime.set_answer_recorder(None)
    globals()["_binding"] = None
    globals()["_dispatch_binding"] = None
    try:
        from . import state

        state.attach(None)
        state.set_pool_binding(None)
        state.publish(force=True)
    except Exception:  # pragma: no cover - a readout on the way out
        logger.debug("%s: could not retract the desktop snapshot", PLUGIN_NAME, exc_info=True)
