"""The switches, read from where Hermes keeps switches.

This plugin's knobs were environment variables and nothing else,
which made them correct and invisible. Hermes has a place for exactly this:
a manifest declares ``config_schema``, the user writes
``plugins.entries.<id>.settings.<key>`` in ``config.yaml``, and the plugin
reads it back through ``ctx.get_config`` (``hermes_cli/plugins.py:1422``).
A knob that does not appear there is a knob nobody finds.

**The environment still wins.** ``KAME_ROTATION_DISABLED`` is the escape
hatch — the thing somebody reaches for when they suspect this plugin of
breaking their agent, from a shell, without editing a YAML file they may
never have created. A config file that could override it would make the
emergency switch conditional on the state it exists to rule out. So the
order is: environment if it says anything at all, config otherwise, and the
built-in default when neither speaks.

**Read once, at registration.** ``ctx.get_config`` calls
``load_config_readonly()`` on every access, and these are consulted on the
classification path and on the selection path — every failed call and every
credential handed out. A config file is re-read when Hermes restarts, which
is what changing a config file already means everywhere else in the host.

Nothing here can prevent the plugin from loading. A context without
``get_config``, a config that will not parse, a value of an unexpected type:
each falls back to the environment, which falls back to the default. The
failure mode of a switch must never be worse than the feature it switches.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# What counts as "on" when the value arrives as text — from the environment
# always, and from YAML when somebody quoted it. Booleans arrive as booleans.
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

# key in config.yaml -> environment variable that outranks it
ROTATION_DISABLED = "disabled"
SPREAD_DISABLED = "spread_disabled"
FIELD_PROBE_DISABLED = "field_probe_disabled"
RESOLVER_DISABLED = "resolver_disabled"
CAROUSEL_DISABLED = "carousel_disabled"
# Every switch here turns a KAME behaviour *off*, which is why this one is
# named for the disabling rather than for the feature: collapsing is the
# default, as it is in Agent Zero, and ``is_on`` reads an absent setting as
# "off" — so a feature-named flag would default to not collapsing and the
# default would have to be spelled somewhere else to survive.
STORM_COLLAPSE_DISABLED = "storm_collapse_disabled"
# 1.0.9. Same naming rule: the live status line is on by default, so the switch
# is named for turning it off.
LIVE_STATUS_DISABLED = "live_status_disabled"
# 1.1.0. Repairs a host bug in the Gemini stream translator that merges two
# parallel tool calls into one broken argument string; see gemini_slots.py.
# Named for turning it off, like the rest: the repair is on by default.
GEMINI_TOOL_CALL_FIX_DISABLED = "gemini_tool_call_fix_disabled"

# 1.1.1. Continuing an answer the provider cut off, on another key, instead of
# handing the cut back to Hermes to paper over with a synthetic
# "[System: The previous response was cut off...]" row. On by default because
# the alternative is a visibly broken answer; named for turning it off like
# every other switch here.
STREAM_STITCH_DISABLED = "stream_stitch_disabled"

# 1.6.0.0, and the only switch here that turns something *on*.
#
# Hermes answers a spent credential two ways at once: rotate to another key,
# and — if that does not work — fall back to a different model, and then to a
# different provider. For most installs that is the right instinct. For the
# install this plugin was written for it is the wrong one, and the owner said
# why in a sentence: the point of the pool is to wait out a quota and come
# back *on the model that was asked for*, exactly as the Agent Zero plugin
# does. A silent switch to another provider mid-conversation is not a
# recovery, it is a different answer from a different model, and the user
# finds out afterwards.
#
# So this is opt-in and off by default: falling back is Hermes' own behaviour
# and this plugin does not get to decide that nobody wants it.
#
# **What it can and cannot reach.** ``should_fallback`` is a field on the
# classification, so it governs every refusal KAME classifies — which is the
# whole main conversation. The auxiliary lane fires no classification hook at
# all (see ``aux_binding``), and Hermes routes summarisation and titling by
# its own rules there; this switch does not reach it. That is a real limit
# and it is written in the help text rather than left to be discovered.
NO_MODEL_FALLBACK = "never_fall_back_to_another_model"

_ENV_FOR = {
    ROTATION_DISABLED: "KAME_ROTATION_DISABLED",
    SPREAD_DISABLED: "KAME_SPREAD_DISABLED",
    FIELD_PROBE_DISABLED: "KAME_FIELD_PROBE_DISABLED",
    RESOLVER_DISABLED: "KAME_RESOLVER_DISABLED",
    CAROUSEL_DISABLED: "KAME_CAROUSEL_DISABLED",
    STORM_COLLAPSE_DISABLED: "KAME_STORM_COLLAPSE_DISABLED",
    LIVE_STATUS_DISABLED: "KAME_LIVE_STATUS_DISABLED",
    GEMINI_TOOL_CALL_FIX_DISABLED: "KAME_GEMINI_TOOL_CALL_FIX_DISABLED",
    STREAM_STITCH_DISABLED: "KAME_STREAM_STITCH_DISABLED",
    NO_MODEL_FALLBACK: "KAME_NO_MODEL_FALLBACK",
}

# The settings that carry a number rather than a yes/no. Kept in a separate
# table because they need a separate reader: a switch that cannot be parsed
# falls back to "off", which is harmless, while a *number* that cannot be
# parsed must fall back to the built-in default rather than to zero.
#
# There is deliberately no knob for how long the carousel may rotate. 1.0.0
# had one; 1.0.1 removed it along with the ceiling itself, for the reason
# Agent Zero's ADR 0002 gives for rejecting the same knob: any non-null value
# reintroduces the failure it was meant to prevent, and the user who sets a
# "safe" number hits it on a hard prompt and blames the plugin. Hermes already
# offers the bound at the level where it belongs — per request, via
# ``HERMES_API_TIMEOUT`` and the per-provider ``request_timeout_seconds``.
DAILY_COOLDOWN = "daily_quota_cooldown_seconds"

# 1.0.9, renamed in 1.1.1, and off by default -- zero means "never give up on
# a silent stream", which is what every version before 1.0.9 did.
#
# The paragraph above rejects a ceiling on how long the carousel may ROTATE,
# and this is not one. It bounds one attempt, not the turn: how long a single
# call may deliver nothing at all before the socket read gives up and KAME
# takes the next key.
#
# It is implemented by lowering Hermes' own ``HERMES_STREAM_READ_TIMEOUT``
# rather than by a watchdog of KAME's own, and that choice is the whole
# safety argument. A watchdog would have to abort a call it does not own;
# the read timeout is the mechanism the host already uses, already surfaces
# as a ``TimeoutError``, and the carousel already knows how to rotate on.
# The variable is set around one attempt and put back afterwards, never at
# registration and never when the user has set it themselves -- see
# ``dispatch_binding._read_timeout_for``, and see the 1.0.10 changelog entry
# for what happens when this plugin sets a host knob and forgets to say so.
#
# **The name.** Until 1.1.1 this was ``silent_stream_patience_seconds``, which
# read like a virtue rather than a timeout and left every reader asking
# patience for *what*. It is the timeout on silence inside one stream, so it
# is now ``stream_silence_timeout_seconds``. The old name still works, in the
# config file and in the environment, for ever: a setting somebody wrote a
# year ago must not become a silent no-op because a maintainer preferred a
# different word.
#
# Off by default because the honest default is Hermes': 120s of silence from
# a cloud provider is unusual but not proof of anything, and a reasoning
# model on a slow day can spend longer than that before its first token.
STREAM_SILENCE_TIMEOUT = "stream_silence_timeout_seconds"

#: The 1.0.9 name, kept as an alias so old configuration keeps working. Points
#: at the same string as the new constant, so plugin code that still imports it
#: reads the same setting rather than a second, dead one.
SILENT_STREAM_PATIENCE = STREAM_SILENCE_TIMEOUT

#: 1.1.1. How many times one turn may continue an answer that was cut off
#: mid-stream. Zero switches stitching off as surely as the flag does, which
#: is why the range starts there.
#:
#: **1.6.0.0 demoted it.** It was the gate, at three, and three is a number
#: someone chose. The paragraph at the top of this file rejects exactly that
#: for the rotation loop — "any non-null value reintroduces the failure it
#: was meant to prevent, and the user who sets a safe number hits it on a
#: hard prompt and blames the plugin" — and there was never a reason the
#: stitching loop should be held to a weaker standard. The owner put it
#: plainly: *the agent should not stop because of errors.*
#:
#: ``dispatch_binding`` now ends the loop on evidence instead: every key in
#: the pool asked to continue the answer, and not one of them adding a word.
#: Unanimity, the same shape ``_pool_agrees_it_is_the_request`` uses. This
#: number stays as the guard rail it always claimed to be, defaulted to the
#: top of its own range so it never ends an answer that was still growing.
STREAM_RESUME_LIMIT = "stream_resume_limit"

_NUMBER_ENV_FOR = {
    DAILY_COOLDOWN: "KAME_DAILY_COOLDOWN",
    STREAM_SILENCE_TIMEOUT: "KAME_STREAM_SILENCE_TIMEOUT",
    STREAM_RESUME_LIMIT: "KAME_STREAM_RESUME_LIMIT",
}

#: Names this plugin used to answer to, and still does. Read only when the
#: current name says nothing at all, so a config that carries both is decided
#: by the one the user wrote most recently rather than by dictionary order.
_LEGACY_KEYS: Dict[str, str] = {
    "silent_stream_patience_seconds": STREAM_SILENCE_TIMEOUT,
}

_LEGACY_ENV_FOR: Dict[str, str] = {
    STREAM_SILENCE_TIMEOUT: "KAME_SILENT_STREAM_PATIENCE",
}

# Bounds, so a mistyped value cannot make the plugin worse than not having it.
# A daily cooldown of a second is a busy-loop against a key that is out until
# midnight; one over a day outlives the quota it describes.
_NUMBER_RANGE = {
    DAILY_COOLDOWN: (1.0, 86400.0),
    # Zero is the default and means off, so it has to be inside the range. The
    # floor above zero is 5s: anything shorter fires while a healthy provider
    # is still opening its stream.
    STREAM_SILENCE_TIMEOUT: (0.0, 3600.0),
    # Ten is not a considered maximum, it is a guard rail. Each resume is a
    # real request against a pool that has already lost one; somebody who
    # types 500 has mistaken this for a retry budget.
    STREAM_RESUME_LIMIT: (0.0, 10.0),
}

#: Values inside the range that are still refused, per setting. A silence
#: timeout of one second is not a configuration, it is a way to rotate the
#: whole pool before any provider has answered.
_NUMBER_FLOOR_ABOVE_ZERO = {
    STREAM_SILENCE_TIMEOUT: 5.0,
}

#: Numbers that must land on a whole number, because they count things. A
#: budget of 2.5 resumes is not a smaller budget, it is a typo.
_NUMBER_INTEGRAL = frozenset({STREAM_RESUME_LIMIT})

# What the config file said, filled in once at registration. Empty until then,
# and empty forever on a host that offers no config surface — which is the
# same behaviour every version before this one had.
_FROM_CONFIG: Dict[str, bool] = {}
_NUMBERS_FROM_CONFIG: Dict[str, float] = {}

# The host context ``load`` was given, kept only so the config can be read a
# second time — see ``pending_restart``. Nothing on the hot path touches it.
_CTX: object = None

# How often the config file may be re-read to look for an edit, and the last
# answer. ``ctx.get_config`` parses the file on every access, so this is
# throttled hard: a person who edits a YAML file and switches to the panel
# will wait a few seconds, and nobody will wait for a parse per second.
_DRIFT_EVERY_S = 15.0
_drift_checked_at = 0.0
_drift: Tuple[str, ...] = ()


def _as_flag(value: object) -> Optional[bool]:
    """A truthy/falsy reading of one setting, or ``None`` for "said nothing".

    ``None`` matters as much as the two answers. An unset variable and a
    value nobody can interpret both mean the next source down should decide,
    and a setting that silently reads as ``False`` would make a typo look
    exactly like a deliberate "off".
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def _as_number(value: object, key: str) -> Optional[float]:
    """A bounded reading of one numeric setting, or ``None`` for "said nothing".

    Out-of-range is clamped rather than rejected: somebody who wrote
    ``daily_quota_cooldown_seconds: 99999`` meant "a long time", and refusing
    the whole setting over it would give them the default, which is shorter
    than anything they asked for.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    low, high = _NUMBER_RANGE.get(key, (float("-inf"), float("inf")))
    number = max(low, min(number, high))
    if key in _NUMBER_INTEGRAL:
        number = float(round(number))
    # A floor that only applies above zero, because zero is how a setting is
    # turned off and "off" must not be rounded up into "on, briefly".
    floor = _NUMBER_FLOOR_ABOVE_ZERO.get(key)
    if floor is not None and 0.0 < number < floor:
        logger.warning(
            "kame: %s was %.1fs, raising it to the %.0fs floor — anything "
            "shorter fires while a healthy provider is still connecting",
            key, number, floor,
        )
        number = floor
    return number


def _from_legacy_config(getter, key: str) -> object:
    """What an older name for ``key`` says, or ``None``.

    Renaming a setting is free for the person who does it and expensive for
    everybody who wrote the old name down. Reading both is the cheap half of
    that bargain; the other half is that the old name is never *printed*, so
    nothing here teaches it to a new reader.
    """
    for old, new in _LEGACY_KEYS.items():
        if new != key:
            continue
        try:
            value = getter(old, None)
        except Exception:
            logger.debug("%s: could not read the %r setting", __name__, old, exc_info=True)
            continue
        if value is not None:
            logger.info(
                "kame: %s is the old name for %s and still works; rename it "
                "when convenient",
                old, new,
            )
            return value
    return None


def load(ctx) -> None:
    """Read every switch out of the host's config, once.

    Called from ``register``. Anything that goes wrong here leaves the
    plugin exactly as it was before config was a source at all.
    """
    global _CTX, _drift, _drift_checked_at
    _FROM_CONFIG.clear()
    _NUMBERS_FROM_CONFIG.clear()
    _CTX = ctx
    _drift = ()
    _drift_checked_at = 0.0
    getter = getattr(ctx, "get_config", None)
    if not callable(getter):
        return
    for key in _NUMBER_ENV_FOR:
        try:
            raw = getter(key, None)
            if raw is None:
                # The name this setting used to have. Only consulted when the
                # current one is absent, so a file carrying both is not
                # decided by which happens to be read first.
                raw = _from_legacy_config(getter, key)
        except Exception:
            logger.debug("%s: could not read the %r setting", __name__, key, exc_info=True)
            continue
        number = _as_number(raw, key)
        if number is None:
            if raw is not None:
                logger.warning(
                    "kame: ignoring plugins.entries.hermes-kame-api-rotation"
                    ".settings.%s — expected a number of seconds",
                    key,
                )
            continue
        _NUMBERS_FROM_CONFIG[key] = number
    for key in _ENV_FOR:
        try:
            raw = getter(key, None)
        except Exception:
            # Includes the host rejecting the key outright, which it does by
            # raising. One unusable setting must not cost the other one.
            logger.debug("%s: could not read the %r setting", __name__, key, exc_info=True)
            continue
        flag = _as_flag(raw)
        if flag is None:
            if raw is not None:
                # Worth a line: the user wrote something and got the default.
                # Silently ignoring it is how a switch is reported broken.
                logger.warning(
                    "kame: ignoring plugins.entries.hermes-kame-api-rotation"
                    ".settings.%s — expected true or false",
                    key,
                )
            continue
        _FROM_CONFIG[key] = flag


def forget() -> None:
    """Drop what was read. For tests and for a re-registration."""
    global _CTX, _drift, _drift_checked_at
    _FROM_CONFIG.clear()
    _NUMBERS_FROM_CONFIG.clear()
    _CTX = None
    _drift = ()
    _drift_checked_at = 0.0


def reread_environment() -> Tuple[str, ...]:
    """Pull Hermes' ``.env`` back into this process. Returns what changed.

    Settings are read out of ``os.environ`` on every access, so the only way
    the environment goes stale is when somebody edits the file directly — by
    hand, from a second Hermes, or with a tool that is not this panel. Until
    1.6.0.1 the answer to "did my edit land?" was to restart, and the panel
    could not even say whether there was anything to land.

    Only ``KAME_*`` names are touched, and only ones this build knows: a
    variable belonging to another plugin, or a KAME name from a future
    release, is left exactly where it is. A name that has been *deleted* from
    the file is dropped from the environment too, so removing a line has the
    same effect as pressing Reset — which is what a person deleting a line
    plainly means.
    """
    from . import envfile

    known_keys = list(ALL_FLAGS) + list(ALL_NUMBERS)
    mine = {name for key in known_keys for name in _env_names(key)}
    if not mine:
        return ()
    on_disk = {
        name: value for name, value in envfile.read_kame().items() if name in mine
    }
    changed = []
    for name in sorted(mine):
        before = os.environ.get(name)
        after = on_disk.get(name)
        if before == after:
            continue
        if after is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = after
        changed.append(name)
    return tuple(changed)


def _config_now(getter) -> Tuple[Dict[str, bool], Dict[str, float]]:
    """What ``config.yaml`` says right now, read the same way ``load`` reads it."""
    flags: Dict[str, bool] = {}
    numbers: Dict[str, float] = {}
    for key in _NUMBER_ENV_FOR:
        try:
            raw = getter(key, None)
            if raw is None:
                raw = _from_legacy_config(getter, key)
        except Exception:
            continue
        parsed = _as_number(raw, key)
        if parsed is not None:
            numbers[key] = parsed
    for key in _ENV_FOR:
        try:
            raw = getter(key, None)
        except Exception:
            continue
        flag = _as_flag(raw)
        if flag is not None:
            flags[key] = flag
    return flags, numbers


def pending_restart(now: Optional[float] = None) -> Tuple[str, ...]:
    """Settings whose config file entry has changed since Hermes started.

    The config is read once, at registration, because ``ctx.get_config``
    re-parses the file on every access and these are consulted on the
    classification and selection paths. That is the right trade and it has one
    cost: a person can edit ``config.yaml``, watch nothing happen, and have no
    way to tell an edit that did not take from one that did nothing. This is
    the way to tell — the file is re-read off the hot path, on the snapshot
    the panel already reads once a second, throttled to
    :data:`_DRIFT_EVERY_S`, and what comes back is compared with what was
    captured at registration.

    A key the environment owns is never listed. The environment outranks the
    config file, so an edit to the file changes nothing whether Hermes is
    restarted or not, and reporting it would send somebody to restart for a
    change that will still not apply.

    Returns the setting names, sorted. Empty is the normal answer, and the
    answer on any host that offers no config surface at all.
    """
    global _drift, _drift_checked_at
    getter = getattr(_CTX, "get_config", None)
    if not callable(getter):
        return ()
    now = time.time() if now is None else now
    if now - _drift_checked_at < _DRIFT_EVERY_S:
        return _drift
    _drift_checked_at = now
    try:
        flags, numbers = _config_now(getter)
    except Exception:
        logger.debug("%s: could not re-read the config", __name__, exc_info=True)
        return _drift
    changed = []
    for key in list(_ENV_FOR) + list(_NUMBER_ENV_FOR):
        if provenance(key) == "environment":
            continue
        if key in _ENV_FOR:
            before = _FROM_CONFIG.get(key)
            after = flags.get(key)
        else:
            before = _NUMBERS_FROM_CONFIG.get(key)
            after = numbers.get(key)
        if before != after:
            changed.append(key)
    _drift = tuple(sorted(changed))
    return _drift


def number(key: str, default: float) -> float:
    """A numeric setting, environment first, then config, then the default.

    Same precedence as ``is_on`` and for the same reason: the environment is
    what somebody reaches for when they are debugging a live gateway and do
    not want to edit — or create — a YAML file first.
    """
    for variable in _env_names(key):
        from_env = _as_number(os.environ.get(variable, None), key)
        if from_env is not None:
            return from_env
    return float(_NUMBERS_FROM_CONFIG.get(key, default))


def is_on(key: str) -> bool:
    """Whether the named switch is set, environment first.

    Defaults to ``False`` for each, which is the plugin doing its whole job:
    a switch that is not mentioned anywhere leaves every feature running.
    """
    for variable in _env_names(key):
        from_env = _as_flag(os.environ.get(variable, None))
        if from_env is not None:
            return from_env
    return bool(_FROM_CONFIG.get(key, False))


def _env_names(key: str) -> Tuple[str, ...]:
    """Every environment variable that speaks for one setting, best name first."""
    current = _ENV_FOR.get(key) or _NUMBER_ENV_FOR.get(key) or ""
    legacy = _LEGACY_ENV_FOR.get(key, "")
    return tuple(name for name in (current, legacy) if name)

# --- what /kame reads -------------------------------------------------------

#: Every switch and number KAME understands, with its default. The single
#: source ``/kame`` enumerates, so a setting added to the manifest and forgotten
#: here shows up as missing rather than as absent.
ALL_FLAGS = (
    ROTATION_DISABLED,
    SPREAD_DISABLED,
    FIELD_PROBE_DISABLED,
    RESOLVER_DISABLED,
    CAROUSEL_DISABLED,
    STORM_COLLAPSE_DISABLED,
    LIVE_STATUS_DISABLED,
    GEMINI_TOOL_CALL_FIX_DISABLED,
    STREAM_STITCH_DISABLED,
    NO_MODEL_FALLBACK,
)

#: The subset that turns a KAME behaviour *off*. Every one is named
#: ``*_disabled`` and every one hands a job back to Hermes, which is what the
#: "Turn parts of KAME off" card says about the settings it lists — so the
#: card is built from this rather than from ``ALL_FLAGS``, which since
#: 1.6.0.0 also carries a switch that turns something on.
DISABLE_FLAGS = tuple(flag for flag in ALL_FLAGS if flag.endswith("disabled") or flag == ROTATION_DISABLED)

ALL_NUMBERS = {
    DAILY_COOLDOWN: 3600.0,
    STREAM_SILENCE_TIMEOUT: 0.0,
    # 1.6.0.0: 3 -> 10. The number stopped being the thing that ends the
    # stitching loop; ``dispatch_binding`` now stops when every key in the
    # pool has been asked to continue the answer and none of them added a
    # word. This stays as the guard rail it always said it was, and is set to
    # the top of its own range so a working continuation is never cut off
    # mid-answer by an arbitrary count. A user who typed a number still gets
    # exactly that number, and zero still switches stitching off.
    STREAM_RESUME_LIMIT: 10.0,
}

#: What each number counts, for a UI that has to label a field and for a
#: reader who should not have to infer the unit from the name.
UNITS = {
    DAILY_COOLDOWN: "seconds",
    STREAM_SILENCE_TIMEOUT: "seconds",
    STREAM_RESUME_LIMIT: "times",
}

#: Switches whose "on" position stops KAME doing the thing it was installed
#: for. The panel asks before turning one of these on; nothing else about them
#: is special, and none of them is refused.
CONSEQUENTIAL = frozenset({ROTATION_DISABLED, CAROUSEL_DISABLED})

#: A short title and one sentence for every setting, so the panel can be read
#: by somebody who has never opened this file. Kept here rather than in the
#: UI because there must be exactly one description of what a switch does,
#: and this is the side that knows.
#:
#: Written for the person deciding whether to touch it: what changes, and what
#: it costs. Every one of them describes the effect of turning the setting
#: **on**, because that is the direction a switch named ``*_disabled`` is
#: read in.
META = {
    ROTATION_DISABLED: (
        "Turn KAME off",
        "Every call keeps the key Hermes resolved and failures follow Hermes' "
        "own retry rules. The plugin stays installed and does nothing.",
    ),
    SPREAD_DISABLED: (
        "Give back key selection",
        "Cooldown sizing stays, but Hermes chooses which key each call carries "
        "instead of KAME picking the least-loaded one.",
    ),
    FIELD_PROBE_DISABLED: (
        "Restore the one-key field check",
        "The Settings key field goes back to refusing a paste that holds "
        "several comma-separated keys.",
    ),
    RESOLVER_DISABLED: (
        "Send multi-key values whole",
        "A comma-separated key variable is sent to the provider exactly as "
        "Hermes resolves it — as one long key, which no provider accepts.",
    ),
    CAROUSEL_DISABLED: (
        "Stop rotating per call",
        "Hermes' own key, retry ceiling and rotation rules come back. Cooldown "
        "sizing stays. This is the switch that stops a failed call trying the "
        "next key.",
    ),
    STORM_COLLAPSE_DISABLED: (
        "Log every failure during an outage",
        "Repeated identical failures are written in full instead of being "
        "collapsed into a periodic count. Louder logs, same rotation.",
    ),
    LIVE_STATUS_DISABLED: (
        "Hide the status line",
        "Pool health and the recovery countdown stop appearing on the spinner "
        "line. Rotation is unchanged.",
    ),
    GEMINI_TOOL_CALL_FIX_DISABLED: (
        "Stop repairing Gemini tool calls",
        "Two parallel calls to one tool arrive merged into one unparseable "
        "argument string, which Hermes reports as 'Response truncated due to "
        "output length limit'. This switch turns the repair off.",
    ),
    STREAM_STITCH_DISABLED: (
        "Stop continuing cut answers",
        "An answer cut off mid-stream is handed back to Hermes, which restarts "
        "it behind a '[System: The previous response was cut off...]' note, "
        "instead of KAME continuing it on another key.",
    ),
    DAILY_COOLDOWN: (
        "Daily quota cooldown",
        "How long a key rests after a daily or account-level refusal — a "
        "quota that is genuinely spent. The provider's own retry hint is "
        "ignored for these on purpose: it routinely says a minute when the "
        "truth is midnight. It does not govern a key the provider *refused* "
        "(a 401, a revoked key): that rests twenty seconds and is then "
        "offered last, because waiting is not what repairs one — and a "
        "key the provider names dead leaves rotation instead of resting.",
    ),
    # 1.2.2 renames the *title* only. "Give up on a silent key after" named
    # the mechanism; this names the thing a person is deciding about, which is
    # how long they are willing to sit in front of a provider that has said
    # nothing yet. The key, the environment variable and the config name are
    # untouched — a title is read once and a name is typed for years.
    STREAM_SILENCE_TIMEOUT: (
        "Wait for the first token",
        "For a provider that accepts a request and then sends nothing. KAME "
        "waits this long for the first character — and for every character "
        "after it — before dropping that key and asking the next one. Zero, "
        "the default, means only Hermes' own 120s applies. Leave it at zero "
        "unless you have a provider that accepts a request and then hangs: a "
        "reasoning model can spend well over twenty seconds before its first "
        "token, and a number that short would abandon it mid-thought and "
        "rotate through the whole pool doing the same. Anything above zero is "
        "raised to at least 5s for the same reason. "
        "If you do have such a provider, 60 is the number to try: it is well "
        "clear of the slowest honest first token anyone has measured here, and "
        "half of what Hermes would otherwise spend before giving up — so a "
        "hung key costs a minute instead of two, and a slow one is still "
        "allowed to think.",
    ),
    NO_MODEL_FALLBACK: (
        "Stay on this model, always",
        "Hermes answers a spent key by rotating and, if that does not work, "
        "by quietly switching to another model or provider. Turn this on and "
        "KAME tells it not to: the pool waits out the quota and comes back on "
        "the model you asked for, however long that takes. Summarisation and "
        "titling are routed by Hermes on a lane this cannot reach, so those "
        "may still fall back.",
    ),
    STREAM_RESUME_LIMIT: (
        "Resume attempts per turn",
        "A ceiling, not the rule. KAME keeps continuing a cut answer while "
        "keys are still adding words to it, and stops on its own once every "
        "key has been asked and none of them added anything — so this only "
        "bites if you lower it. Zero switches stitching off entirely.",
    ),
}


def env_name(key: str) -> str:
    """The environment variable that outranks this setting, or ``""``."""
    return _ENV_FOR.get(key) or _NUMBER_ENV_FOR.get(key, "")


def title(key: str) -> str:
    """A short human name for one setting, or the key itself."""
    entry = META.get(key)
    return entry[0] if entry else key


def explain(key: str) -> str:
    """One sentence about what turning this on does, or ``""``."""
    entry = META.get(key)
    return entry[1] if entry else ""


def bounds(key: str) -> Optional[Tuple[float, float]]:
    """``(low, high)`` for a number, or ``None`` for a switch."""
    return _NUMBER_RANGE.get(key)


def parse(key: str, raw: object) -> Tuple[Optional[str], str]:
    """``(value_for_the_environment, error)`` — one of the two is always empty.

    The single validator behind both ways a setting can be changed: the
    ``/kame set`` command and the panel. They used to be one, and the day the
    panel arrived was the day they could start disagreeing about what ``5.5``
    means for a setting that counts whole things. Now they cannot.

    The value returned is the *canonical environment string*, because the
    environment is where a change is written — see the module docstring for
    why that is the durable surface rather than the config file.
    """
    key = canonical(key)
    if not known(key):
        return None, f"{key} is not a KAME setting"
    text = str(raw).strip() if raw is not None else ""
    if key in ALL_NUMBERS:
        try:
            number = float(text)
        except (TypeError, ValueError):
            unit = UNITS.get(key, "seconds")
            return None, f"{key} takes a number of {unit}; {text!r} is not one"
        low, high = _NUMBER_RANGE.get(key, (float("-inf"), float("inf")))
        if number < low or number > high:
            return None, (
                f"{key} accepts {low:g} to {high:g}; {number:g} is outside that"
            )
        floor = _NUMBER_FLOOR_ABOVE_ZERO.get(key)
        if floor is not None and 0.0 < number < floor:
            return None, (
                f"{key} is either 0 (off) or at least {floor:g} — anything "
                "shorter fires while a healthy provider is still connecting"
            )
        if key in _NUMBER_INTEGRAL and number != int(number):
            return None, f"{key} counts whole things; {number:g} is not a whole number"
        return (str(int(number)) if number == int(number) else str(number)), ""
    lowered = text.lower()
    if lowered in _TRUE:
        return "1", ""
    if lowered in _FALSE:
        return "0", ""
    return None, f"{key} takes true or false; {text!r} is neither"


def describe(key: str) -> Dict[str, object]:
    """Everything a UI needs to render one setting as an editable control."""
    is_number = key in ALL_NUMBERS
    low, high = _NUMBER_RANGE.get(key, (0.0, 0.0))
    return {
        "key": key,
        "kind": "number" if is_number else "flag",
        "title": title(key),
        "help": explain(key),
        "value": effective(key),
        "default": ALL_NUMBERS[key] if is_number else False,
        "source": provenance(key),
        "env": env_name(key),
        "group": group_of(key),
        "units": UNITS.get(key, "") if is_number else "",
        "min": low if is_number else None,
        "max": high if is_number else None,
        "step": 1 if (is_number and key in _NUMBER_INTEGRAL) else None,
        "off_or_at_least": _NUMBER_FLOOR_ABOVE_ZERO.get(key),
        "consequential": key in CONSEQUENTIAL,
    }


#: The three shelves a settings screen puts these on, in the order they are
#: shown, each with the sentence that says what the shelf is *for*.
#:
#: Until 1.2.0 there was one list, and it read as twelve equal knobs. They are
#: not equal, and the difference is the only thing a first-time reader needs:
#:
#: * **extra** — one opt-in feature that is off until somebody turns it on. It
#:   is the only setting here that adds behaviour rather than adjusting or
#:   removing it, and it is the only one whose default of "off" is a real
#:   choice rather than "KAME working normally".
#: * **tuning** — numbers with defaults that are already right for the
#:   providers this plugin was built against. Changing one is reasonable and
#:   rarely necessary.
#: * **off** — the escape hatches. Every one of them is named ``*_disabled``
#:   and every one of them takes away something the plugin was installed to
#:   do. They exist so that a person who suspects KAME of breaking their agent
#:   can prove it in one switch, which is a thing worth having and not a thing
#:   worth browsing.
#:
#: The tuple is ``(id, title, note, keys)``. The panel renders one card per
#: entry from exactly this data, so the grouping and its explanation cannot
#: drift apart from the settings they describe.
GROUPS: Tuple[Tuple[str, str, str, Tuple[str, ...]], ...] = (
    (
        "extra",
        "Optional",
        "Off until you turn it on. Nothing here is needed for rotation to "
        "work — this is an extra, for one specific problem.",
        (STREAM_SILENCE_TIMEOUT, NO_MODEL_FALLBACK),
    ),
    (
        "tuning",
        "Tuning",
        "Already set to what this plugin was built against. Safe to change, "
        "rarely worth changing.",
        (DAILY_COOLDOWN, STREAM_RESUME_LIMIT),
    ),
    (
        "off",
        "Turn parts of KAME off",
        "Escape hatches. Each one gives a job back to Hermes and is meant for "
        "proving whether KAME is behind a problem — not for tuning. Leave "
        "them alone unless something is wrong.",
        DISABLE_FLAGS,
    ),
)

#: ``key -> group id``, built once from the table above so there is one place
#: where a setting is assigned to a shelf.
_GROUP_OF: Dict[str, str] = {
    key: group for group, _title, _note, keys in GROUPS for key in keys
}

#: Where a setting goes when :data:`GROUPS` has never heard of it. "tuning" and
#: not "off": a switch that lands on the wrong shelf is a small confusion,
#: while one that lands among the escape hatches reads as a warning it may not
#: deserve.
_UNGROUPED = "tuning"


def group_of(key: str) -> str:
    """Which shelf one setting belongs on."""
    return _GROUP_OF.get(key, _UNGROUPED)


def groups() -> Tuple[Dict[str, object], ...]:
    """The shelves, for a panel that has to title and explain each one.

    Carries the keys it holds as well as the prose, so a renderer can lay the
    screen out from this alone and a setting can never be titled by one side
    and grouped by the other.
    """
    return tuple(
        {"id": group, "title": title_, "note": note, "keys": list(keys)}
        for group, title_, note, keys in GROUPS
    )


def describe_all() -> Tuple[Dict[str, object], ...]:
    """Every setting, in the order a panel should show them.

    Grouped by :data:`GROUPS`, and then — the clause that matters — anything
    this module gained without the table being told about it, at the end.
    A setting missing from a hand-kept list has to show up in the wrong place
    rather than not at all.
    """
    everything = list(ALL_FLAGS) + list(ALL_NUMBERS)
    ordered = [key for _g, _t, _n, keys in GROUPS for key in keys if key in everything]
    ordered += [key for key in everything if key not in ordered]
    return tuple(describe(key) for key in ordered)


def provenance(key: str) -> str:
    """Where the effective value came from: ``environment``, ``config``, ``default``.

    The reason ``/kame get`` prints this rather than only the value: a switch
    that reads "off" because a file says so and one that reads "off" because
    nothing anywhere mentions it are the same word and two different problems,
    and the second one is what somebody is looking at when a setting they wrote
    "did not take".
    """
    for variable in _env_names(key):
        if os.environ.get(variable) is not None:
            return "environment"
    if key in _FROM_CONFIG or key in _NUMBERS_FROM_CONFIG:
        return "config"
    return "default"


def effective(key: str) -> object:
    """The value in force for one setting, flag or number."""
    if key in ALL_NUMBERS:
        return number(key, ALL_NUMBERS[key])
    return is_on(key)


def canonical(key: str) -> str:
    """The current name for a setting, translating a name it used to have.

    The config file and the environment both keep answering to the old names
    (see ``_LEGACY_KEYS`` and ``_LEGACY_ENV_FOR``), and a person who typed one
    of those into a config file a year ago will type the same name at
    ``/kame set``. Refusing it there would make the compatibility promise true
    only where nobody is looking.
    """
    return _LEGACY_KEYS.get(str(key).strip(), str(key).strip())


def known(key: str) -> bool:
    return key in ALL_FLAGS or key in ALL_NUMBERS
