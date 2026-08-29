"""Hermes KAME API Rotation — a key chosen for every call, and a call that
rotates instead of failing.

**What changed in 1.0.0.** Every release before this one acted *after* a
refusal: read the provider's timing out of the error, size the cooldown, bench
the key for as long as it actually asked for. All of that was true and none of
it chose the key a request carried, so a user with fifteen keys still watched
one key get used until it refused — and watched a 503 end a turn while
fourteen untouched keys sat in the pool, because Hermes retries three times
(``agent._api_max_retries``) and rotates the credential pool only for billing,
rate-limit and auth failures.

The carousel closes both gaps, and it is the piece this plugin was named
after. It is a port of the Agent Zero engine's v1.0.9 rules — the same
selection, the same escalation ladders, the same refusal to believe a daily
quota's ``retryDelay`` — expressed against Hermes' own dispatch point rather
than re-implementing the request. See ``dispatch_binding`` for where it
attaches and ``core.carousel`` for the rules themselves.

Wires nine things:

* The dispatch binding (``dispatch_binding``) — the carousel. Wraps the two
  functions every model call in the process goes through, picks the healthiest
  key by requests-in-the-last-60-seconds then least-recently-used, and on a
  failure that is not terminal rests that key and takes the next one without
  the error ever reaching the conversation loop. This is what "rotating on
  every message, not only on errors" means, and it is the only binding here
  that changes what a *healthy* call does.

* ``transform_api_error_classification`` — fired once per failed API call,
  before Hermes' built-in classifier. KAME reads the quota window and the
  retry timing out of the response and hands back a precise ``reset_at``, so
  the credential pool benches the key for as long as the provider actually
  asked instead of a flat default.
* ``pre_api_request`` — an observer, used for the one fact the credential
  pool is never told: which model the request in flight is for. Hermes
  discards this hook's return value by design; KAME uses it only for that
  side effect.
* ``post_api_request`` — the other side of the same coin: a call that came
  back clean. Used only to close an open question in the journal, which is
  why the common case of a healthy call writes nothing at all.
* The per-model binding (``pool_binding``) — wraps the two pool methods that
  write and read a bench, so a key spent on one model keeps its allowance on
  another. See that module for why this is required rather than a bonus.
* The journal (``core.journal``) — every real refusal, with the prediction
  that was made about it, so the predictions can be checked against what
  happened instead of against a fixture. It informs nothing in this version
  and is read only by ``/kame-quota``; a rule tuned on data gathered after
  the rule existed is not evidence.
* The field binding (``field_binding``) — the desktop checks a pasted
  credential before it saves it, by sending the whole field to the provider
  as one key. That refuses the several-keys-in-one-field shape the pool
  binding exists to read, so the keys are probed one at a time instead.
  Installed only where a gateway is running and only once the splitting is
  actually on.
* The resolver binding (``resolver_binding``) — the two above make a
  multi-key variable *storable* and *splittable*, and neither reaches the key
  a request carries: Hermes resolves an API-key provider straight off the
  environment variable, so the comma-joined list went out as one Bearer
  token and no pool was ever attached to the agent. This makes the first key
  of a turn one key, chosen by the pool.
* ``/kame-keys`` and ``/kame-quota`` — bulk key intake and the report, in CLI
  and gateway sessions alike.

Why this matters. Hermes sizes a 429 cooldown at one hour
(``EXHAUSTED_TTL_429_SECONDS``) unless it recognises a reset time in the
response. Its scan is text-only, so it misses the three richest sources:
SDK exception attributes, HTTP rate-limit headers, and structured error
bodies. Two failures then collapse onto the same hour while needing
opposite treatment:

* A per-minute throttle asks for ~20 seconds and gets benched for an hour.
  With a pool of N keys a burst can bench the whole pool over throttles that
  would have cleared on their own.
* A daily cap gets that same hour, so the key returns to rotation while
  still spent, fails, and repeats — hourly, all day.

**No provider allowlist, deliberately.** An allowlist is a promise to do
nothing for whichever provider is not on it, including every provider that
does not exist yet. KAME acts on *evidence* — a retry attribute, a
``Retry-After`` header, a structured body, a sentence naming a window — and
declines when it has none, which leaves the host's own classifier in charge.
The hook is a cold path, fires only on failure, and runs inside Hermes'
isolation wrapper, so a fault here degrades to the built-in behaviour rather
than breaking a call.

**Sizing a cooldown correctly is only half of it.** Hermes keys its
credential pool by provider alone, while several providers meter quota per
key *per model* — Google says so in the error body it returns. Without the
model dimension, an accurate 24-hour bench for a daily cap locks the key out
of every other model for 24 hours, where the old inaccurate one-hour default
would have released it in an hour. Precision without scope is a regression,
so the two ship together and the binding is not optional.

If the binding cannot install — a Hermes release moved what it wraps — the
plugin keeps running with cooldown sizing alone and says so once in the log.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

from . import host_text, integrity, runtime, settings
from .core import Verdict, answer, classify

if TYPE_CHECKING:  # pragma: no cover
    from .aux_binding import AuxBinding
    from .dispatch_binding import DispatchBinding
    from .field_binding import FieldBinding
    from .pool_binding import PoolBinding
    from .resolver_binding import ResolverBinding

logger = logging.getLogger(__name__)


PLUGIN_NAME = "hermes-kame-api-rotation"

#: What `integrity.verify()` said at register time, carried so every readout
#: can repeat it. A dict rather than a flag because "incomplete" is only useful
#: alongside *what* is missing — "KAME did not start" and "KAME started without
#: its engine" look identical from a chat window and have opposite fixes.
_INTEGRITY: Dict[str, Any] = {"complete": True, "fingerprint": "", "missing_required": []}

# The live bindings, or None when the plugin is running with cooldown sizing
# alone. Module-level so a reload or a test can tear them down; assigned in
# register(). The auxiliary one is only installed when the pool one succeeded —
# announcing a model no reader consults would be pure overhead.
_binding: Optional["PoolBinding"] = None
_aux_binding: Optional["AuxBinding"] = None
_field_binding: Optional["FieldBinding"] = None
_resolver_binding: Optional["ResolverBinding"] = None
_dispatch_binding: Optional["DispatchBinding"] = None

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
    result: Dict[str, Any] = {
        "reason": verdict.reason,
        "retryable": verdict.retryable,
        "should_rotate_credential": verdict.should_rotate_credential,
        "should_fallback": verdict.should_fallback,
    }
    if verdict.reset_at is not None:
        result["error_context"] = {"reset_at": verdict.reset_at}
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


def _on_api_error_classification(
    *,
    provider: str = "",
    model: str = "",
    status_code: Optional[int] = None,
    error_message: str = "",
    error_body: Optional[Dict[str, Any]] = None,
    error: Any = None,
    **_ignored: Any,
) -> Optional[Dict[str, Any]]:
    """Classify one failure, or decline so the host pipeline runs.

    Accepts and ignores the rest of the hook payload (``error_type``,
    ``error_code``, token counts) via ``**_ignored`` so a future Hermes
    release adding a field cannot break dispatch.
    """
    if _is_disabled():
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

    # Hand the reasoning forward to whoever writes the bench. The pool is
    # about fifty lines away in the same call stack and knows the deadline
    # that actually got stored; this is the only place that knows why.
    try:
        runtime.note_judgement(
            provider,
            model,
            window=verdict.quota_window,
            source=verdict.source,
            reset_at=verdict.reset_at,
            now=time.time(),
            scope=verdict.quota_scope,
        )
    except Exception:  # pragma: no cover — a ContextVar set does not fail
        logger.debug("%s: could not stage the verdict", PLUGIN_NAME, exc_info=True)

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


def _on_pre_api_request(*, provider: str = "", model: str = "", **_ignored: Any) -> None:
    """Note which call is about to go out. Return value is unused by design.

    Hermes documents ``pre_api_request`` as an observer — the dispatcher
    discards whatever it returns and swallows exceptions. That makes it the
    right and only place to learn the model, and it means this function must
    earn its keep purely through the side effect below.
    """
    if _is_disabled():
        return
    try:
        runtime.note_call(provider, model)
    except Exception:  # pragma: no cover — a ContextVar set does not fail
        logger.debug("%s: could not note the call in flight", PLUGIN_NAME, exc_info=True)


def _on_post_api_request(
    *,
    provider: str = "",
    model: str = "",
    assistant_content_chars: Any = None,
    assistant_tool_call_count: Any = None,
    **_ignored: Any,
) -> None:
    """Note that a call came back clean. Almost always writes nothing.

    This is the only signal that says a benched key started working again,
    which is the only way to catch a cooldown that was sized too *long* —
    nothing fails when KAME over-holds a key, so without this the mistake is
    invisible. The journal ignores the call unless it closes an open
    question, so the healthy path costs a dictionary lookup.

    An answer with nothing in it is not that signal. A squeezed free-tier key
    can return 200 and no content instead of refusing, so treating "the call
    returned" as "the key works" would retire a bench permanently on an event
    that proved nothing. It is counted and dropped — see ``core.answer``.
    """
    if _is_disabled():
        return
    if answer.carried_nothing(
        content_chars=assistant_content_chars,
        tool_calls=assistant_tool_call_count,
    ):
        _count_empty(provider)
        return
    binding = globals().get("_binding")
    if binding is None:
        return
    try:
        binding.note_success(str(provider or "").strip().lower(), model)
    except Exception:
        logger.debug("%s: could not note a successful call", PLUGIN_NAME, exc_info=True)


def _install_pool_binding(ctx) -> Optional["PoolBinding"]:
    """Give the pool a model dimension, or explain in one line why not.

    Returns the live binding so a caller (tests, ``/kame-keys``, a future
    status command) can inspect or remove it. Returns ``None`` when the
    plugin is running without per-model memory, which is a supported mode,
    not a failure — cooldown sizing is unaffected.
    """
    if _is_disabled():
        return None
    try:
        from agent import credential_pool

        from .pool_binding import PoolBinding
        from .store import JournalStore, LedgerStore

        state = getattr(ctx, "state", None)
        binding = PoolBinding(LedgerStore(state), journal=JournalStore(state))
        return binding if binding.install(credential_pool) else None
    except ImportError:
        # Running outside a Hermes install — the doctor, a unit test, a
        # packaging check. Nothing to bind to and nothing to warn about.
        logger.debug("%s: no credential pool to bind", PLUGIN_NAME)
        return None
    except Exception:
        logger.warning(
            "%s: per-model memory unavailable, cooldown sizing still active",
            PLUGIN_NAME,
            exc_info=True,
        )
        return None


def _install_field_binding() -> Optional["FieldBinding"]:
    """Let the Settings key field hold several keys, or say why it cannot.

    Returns ``None`` in every session that has no gateway — the CLI, the TUI,
    a test — which is not a failure: there is no field there to widen.
    """
    try:
        from .field_binding import install as _install

        return _install()
    except Exception:
        logger.warning(
            "%s: the settings field still takes one key",
            PLUGIN_NAME,
            exc_info=True,
        )
        return None


def _install_dispatch_binding() -> Optional["DispatchBinding"]:
    """Make every API call choose its own key, or say why it still cannot.

    This is the binding that turns the plugin from a cooldown sizer into a
    rotation engine, so its absence is worth a warning rather than a debug
    line: without it the plugin still sizes benches correctly, but a pool of
    fifteen keys is used the way Hermes uses it — one key until it refuses.
    """
    try:
        from .dispatch_binding import install as _install

        return _install()
    except Exception:
        logger.warning(
            "%s: every call still carries the key Hermes resolved",
            PLUGIN_NAME,
            exc_info=True,
        )
        return None


def _install_resolver_binding() -> Optional["ResolverBinding"]:
    """Make the first key of a turn one key, or say why it is still the list.

    Returns ``None`` outside a Hermes install and on a build whose resolver
    has moved — in both cases the plugin behaves exactly as v0.3.6 did, which
    for a user with one key per variable is no difference at all.
    """
    try:
        from .resolver_binding import install as _install

        return _install()
    except Exception:
        logger.warning(
            "%s: a multi-key variable still resolves to the whole list",
            PLUGIN_NAME,
            exc_info=True,
        )
        return None


def _on_session_reset(payload=None, **kwargs):
    """Forget what belonged to the conversation, keep what belongs to the keys.

    A reset clears the chat, not the calendar. The status line describes a
    conversation that no longer exists, so it goes. Cooldowns describe a quota
    that is still spent, and a key benched until midnight is still benched at
    00:01 whether or not the history was cleared; clearing those would send the
    whole pool back into the same wall it just learned about, one request at a
    time.

    The storm filter stays for the same reason the cooldowns do, and for one
    more. It describes a provider outage rather than a chat, and it guards the
    *log*, which is one file per process and is not cleared by a reset either.
    It is also shared by every conversation this process is serving: clearing
    it here would restart the collapse for sessions that never asked for a
    reset and are still living through the same outage.
    """
    # Scoped to the session that was reset, when the host says which one it
    # was. It always does today (`hermes_cli/hooks.py:182`), but a reset that
    # arrived unlabelled clearing every conversation's status line would be a
    # bug in the other direction, so the argument is read rather than assumed.
    session_id = None
    if isinstance(payload, dict):
        session_id = payload.get("session_id")
    if session_id is None:
        session_id = kwargs.get("session_id")
    try:
        from .dispatch_binding import _Spinner

        _Spinner.reset(session_id)
    except Exception:  # pragma: no cover — defensive only
        logger.debug("%s: could not reset the status line", PLUGIN_NAME, exc_info=True)
    return None


def register(ctx) -> None:
    # Before anything else, and loudly: is this a whole plugin?
    #
    # An install that lost `core/` to a non-recursive copy registers perfectly,
    # publishes `installed: true, reason: "active"`, and rotates nothing — for
    # nine days, in this user's Hermes, while the panel showed a version number
    # taken from a manifest that the same partial copy had faithfully updated.
    # The guards below are all `except Exception: log at debug`, which is right
    # for a hook a host may not offer and exactly wrong for a package that is
    # missing its own engine.
    #
    # So the check runs first, says what is absent, and the answer travels with
    # every readout afterwards. It does not stop registration: the slash
    # commands and the panel are how a person finds out what is wrong, and
    # refusing to register would take away the screen that explains it.
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

    # Separate try: a Hermes without this hook still rotates, it just carries a
    # storm filter and a status line across a /reset.
    try:
        ctx.register_hook("on_session_reset", _on_session_reset)
    except Exception:
        logger.debug("%s: on_session_reset unavailable", PLUGIN_NAME, exc_info=True)

    # Registered after the classifier and separately guarded: the observers
    # only improve scoping and record-keeping, so a Hermes build that does not
    # offer them must still get correct cooldowns.
    try:
        ctx.register_hook("pre_api_request", _on_pre_api_request)
        # Declared here rather than after the binding so the manifest can
        # promise exactly what gets registered. It costs a returned-None
        # callback per successful call and does nothing at all until there is
        # a binding to complete a record for.
        ctx.register_hook("post_api_request", _on_post_api_request)
    except Exception:
        logger.warning(
            "%s: cannot observe the model in flight; benches will be provider-wide",
            PLUGIN_NAME,
            exc_info=True,
        )
    else:
        globals()["_binding"] = _install_pool_binding(ctx)
        # Only worth wiring once the pool can act on it: an announcement with
        # nothing reading it changes nothing, and the auxiliary lane is the
        # place the per-model gap hurts most — it runs a smaller model on the
        # same keys the conversation just spent.
        if globals()["_binding"] is not None:
            from .aux_binding import install as _install_aux

            globals()["_aux_binding"] = _install_aux()

        # Only once the pool can actually read a multi-key value as several
        # keys. Accepting the paste while the splitting is off would trade a
        # refusal the user can act on for a credential that fails every call:
        # the host would store the whole comma-joined string as one key, and
        # the field's own check is the last place anything says so.
        if getattr(globals()["_binding"], "splitting_multikey", False):
            globals()["_field_binding"] = _install_field_binding()

    # Outside the block above on purpose: this one does not need the hooks, the
    # pool, or the splitting. Its worst case — no pool to ask — still replaces
    # a comma-joined list that no provider accepts with one key that they do,
    # and that is worth having on a host where everything else declined.
    globals()["_resolver_binding"] = _install_resolver_binding()

    # Last, and outside every guard above, because it depends on none of them.
    # The resolver decides the key a *session* starts with; this decides the
    # key every *call* carries and what happens when one of them refuses. It
    # works with a pool, with a comma-separated variable, and with a single
    # key — in the last case there is nothing to rotate to, and what remains
    # (backoff sized by the evidence, and a failure that waits instead of
    # ending the turn) is still the difference between a 503 costing a pause
    # and a 503 costing the answer.
    globals()["_dispatch_binding"] = _install_dispatch_binding()

    # The slash commands are registered separately and defensively: if command
    # registration ever breaks (a name collision with a future built-in, a
    # Hermes API change), the rotation hook above must still install. Key
    # intake and reporting are conveniences; correct cooldowns are the reason
    # the plugin exists.
    try:
        from .commands import register_command

        register_command(ctx)
    except Exception:
        logger.warning(
            "%s: /kame-keys unavailable, rotation hook still active",
            PLUGIN_NAME,
            exc_info=True,
        )

    try:
        from .status import register_command as register_status_command

        register_status_command(ctx, binding=globals().get("_binding"))
    except Exception:
        logger.warning(
            "%s: /kame-quota unavailable, rotation hook still active",
            PLUGIN_NAME,
            exc_info=True,
        )

    # Since 1.0.9. The panel is last of the three because it is the one whose
    # absence costs least: it reads state the other two already expose in
    # pieces, and it writes nothing but this plugin's own settings. It is
    # handed the dispatch binding rather than the pool one because the
    # counters worth reading -- rotations, waits, answers cut mid-stream --
    # live on the carousel.
    try:
        from .menu import register_command as register_menu_command

        register_menu_command(ctx, binding=globals().get("_dispatch_binding"))
    except Exception:
        logger.warning(
            "%s: /kame unavailable, rotation hook still active",
            PLUGIN_NAME,
            exc_info=True,
        )

    # 1.1.0. Repairs the host's Gemini stream translator, which merges two
    # parallel tool calls into one unparseable argument string and surfaces it
    # as "Response truncated due to output length limit" -- an error about
    # length that has nothing to do with length. Guarded and self-checking:
    # see gemini_slots.py for what it verifies before touching anything, and
    # why a key-rotation plugin is the thing that ships it.
    try:
        from . import gemini_slots

        gemini_slots.apply()
    except Exception:
        logger.warning(
            "%s: the Gemini tool-call repair could not be applied, "
            "rotation hook still active",
            PLUGIN_NAME,
            exc_info=True,
        )

    # 1.1.0. The Desktop half of this package, moved into the door Desktop
    # loads default-on. Shipped inside the package as desktop-ui/plugin.js,
    # which neither runtime door scans, so this copy is the only install and
    # there is never a second, stale one racing it. See desktop_ui.py.
    try:
        from . import desktop_ui

        desktop_ui.install()
        # 1.1.1. Whatever writes a file outside its own directory has to be
        # able to take it back: without this, removing the plugin leaves a
        # sidebar entry and a status chip reading a snapshot nothing writes.
        # ``on_unload`` fires on uninstall, disable and reload alike; after a
        # reload the copy above simply puts it back.
        try:
            ctx.on_unload(desktop_ui.uninstall)
        except Exception:
            logger.debug(
                "%s: this Hermes offers no unload hook; the desktop panel "
                "will have to be removed by hand",
                PLUGIN_NAME,
                exc_info=True,
            )
    except Exception:
        logger.debug("%s: could not install the desktop panel", PLUGIN_NAME, exc_info=True)

    # 1.1.0. The snapshot the Desktop plugin reads. Everything above this line
    # works without it -- it is a readout, and a readout that can break a turn
    # is not worth having, so every failure inside it is logged and swallowed.
    try:
        from . import state

        # Remembered once, so the control poller and the heartbeat can publish
        # the truth without being handed the binding again each time.
        state.attach(globals().get("_dispatch_binding"))
        state.publish(force=True)
        _start_state_heartbeat()
    except Exception:
        logger.debug("%s: could not publish the desktop snapshot", PLUGIN_NAME, exc_info=True)


#: How often the heartbeat looks for a request from the panel, and how often it
#: republishes an unchanged snapshot. The first has to feel immediate — it is
#: the delay between clicking a switch and seeing it move — and the second only
#: has to prove the file is not stale, so one thread does both on the fast tick
#: and counts to twenty for the slow one.
_CONTROL_TICK_S = 1.0
_SNAPSHOT_TICK_S = 20.0


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
    import threading

    if globals().get("_state_heartbeat") is not None:
        return

    def _tick() -> None:
        from . import control, state

        ticks = 0
        while True:
            time.sleep(_CONTROL_TICK_S)
            ticks += 1
            try:
                # Publishes by itself when it applies something, so the panel
                # sees the new value and the outcome in the same read.
                control.poll()
            except Exception:  # pragma: no cover — ``poll`` swallows its own
                logger.debug("%s: control poll failed", PLUGIN_NAME, exc_info=True)
            if ticks * _CONTROL_TICK_S < _SNAPSHOT_TICK_S:
                continue
            ticks = 0
            try:
                state.publish(force=True)
            except Exception:  # pragma: no cover - a daemon that cannot die
                logger.debug("%s: snapshot heartbeat failed", PLUGIN_NAME, exc_info=True)

    thread = threading.Thread(target=_tick, name="kame-state", daemon=True)
    globals()["_state_heartbeat"] = thread
    thread.start()
