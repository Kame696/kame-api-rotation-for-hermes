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
import math
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

# 1.7.0.0. Writes each provider refusal to ``refusals.jsonl`` beside the state
# file, keys redacted, nothing decided by it. It exists because the field that
# separates a per-minute quota from a per-day one was invisible to every version
# before this one, so it appears in no log and cannot be tested against.
#
# On here, off before publishing. An install of someone else's is not a place to
# start writing files without being asked, and this one is here to answer a
# question this repository has open — not to be a feature.
RECORDER_DISABLED = "refusal_recorder_disabled"

# 1.7.0.1. Google names the quota window in ``quotaId`` and Hermes' own Gemini
# adapter reads past it: it keeps ``google.rpc.ErrorInfo`` and drops
# ``QuotaFailure`` and ``RetryInfo``. 300 of 300 journal rows on the owner's
# first real session read ``window: unknown`` because of it. KAME keeps the
# parsed body on the exception the adapter builds and changes nothing else.
QUOTA_ID_DISABLED = "quota_id_disabled"

# 1.7.0.1. One line per attempt: how long until anything came back, how long
# until the answer started, how long the whole attempt took, and how much of
# the turn had already gone on waiting. Written so "is it slow?" can be
# answered with a number instead of a memory. Decides nothing.
CALL_TIMINGS_DISABLED = "call_timings_disabled"

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

# 1.8.0.0, on by default since the continuity gate (G4, PLAN_1.8.0.0.md)
# passed — see DEFAULTS_ON below. Decisions/0006: the owner's three Hermes
# profiles (base, k, lo1) share the same seventeen physical keys, and
# without this, one profile benching a key is invisible to the other two,
# which then spend a call finding out the hard way.
#
# ``core.shared_health`` cannot read this entry directly (``core`` imports
# nothing from the host layer — see that module's own docstring), so this
# declaration alone does not reach the plugin's read path. The value it
# resolves to is pushed into ``core.shared_health`` once, at registration,
# by ``dispatch_binding.install()`` calling ``shared_health.
# set_config_default(is_on(SHARE_POOL_HEALTH))`` — the same pattern
# ``daily_cooldown_s``/``max_hold_s`` already use to reach the carousel.
#
# RED_TEAM.md F4 (2026-09-19): until that wiring existed, this comment said
# "off by default" and ``core.shared_health.enabled()`` read only the
# environment, so a fresh install shipped the feature off however this
# entry, ``DEFAULTS_ON`` and ``plugin.yaml`` described it. The environment
# variable remains the escape hatch for turning it off by hand.
SHARE_POOL_HEALTH = "share_pool_health"

# 1.8.1.0. An unsized per-minute/rate-limit throttle — no retry hint, no
# window, nothing the provider named a number for — normally rests for the
# flat :data:`UNSIZED_THROTTLE_REST` above. This switch makes that rest
# double instead, per consecutive such refusal on the same credential and
# model: 1s, 2s, 4s, 8s, 16s, 32s … bounded by :data:`MAX_HOLD`. The streak
# resets the moment that credential answers, and the moment any refusal — on
# this one or an earlier one — carries a number the provider actually stated;
# a stated number is still never multiplied, only obeyed (R10/R11, unchanged).
#
# **This is an experiment, not a correction.** The owner's own belief is that
# a refused request costs no quota, so probing faster than the measured flat
# rest should be free to try. Simulated against his own corpus with call
# suppression (see ``core.carousel._escalate`` and decisions/0007 for the
# tables), the doubling ladder sends about 242 more refused calls than the
# flat 30s and nets +13 seconds returned against the flat rest's +193 — worse
# on both counts measured so far. It ships on anyway, because the premise it
# tests has never been measured either way in this project and the owner
# asked to watch it react to his own live traffic rather than be told the
# answer in advance.
#
# On by default in this release for exactly that reason — the experiment only
# runs if somebody is watching it, and the owner is the one who asked to run
# it, on his own install. Off restores :data:`UNSIZED_THROTTLE_REST`'s flat
# rest exactly as every prior release computed it. Declared and wired the
# same way :data:`MAX_HOLD` and :data:`UNSIZED_THROTTLE_REST` are — read
# environment-first, pushed onto the one ``Carousel`` the dispatch path marks
# and selects against by ``dispatch_binding.install()``, not left decorative.
UNSIZED_THROTTLE_BACKOFF = "unsized_throttle_backoff"

# 1.8.1.0. Where the doubling ladder above stops growing. The owner asked for
# ``1 -> 2 -> 4 -> 8 -> etc``; run uncapped over his own corpus it produced
# 138 holds over 300s against 43 for the flat rest, and contradicted-hold
# seconds went from 999 to 37,162 — a punishment longer than the recovery it
# was waiting for, which his own closed Decision 12 forbids. His measured
# recovery for this shape is 23-34s at the fastest, so the default is 64: the
# ladder runs 1, 2, 4, 8, 16, 32, 64 and then holds at 64. Set it to 3600 for
# the uncapped doubling he originally asked for; ``max_hold_seconds`` still
# bounds everything either way.
UNSIZED_BACKOFF_MAX = "unsized_backoff_max_seconds"

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
    RECORDER_DISABLED: "KAME_RECORDER_DISABLED",
    QUOTA_ID_DISABLED: "KAME_QUOTA_ID_DISABLED",
    CALL_TIMINGS_DISABLED: "KAME_CALL_TIMINGS_DISABLED",
    NO_MODEL_FALLBACK: "KAME_NO_MODEL_FALLBACK",
    SHARE_POOL_HEALTH: "KAME_SHARE_POOL_HEALTH",
    UNSIZED_THROTTLE_BACKOFF: "KAME_UNSIZED_BACKOFF",
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

# 1.8.0.0. Not the knob the paragraph above refuses — that one bounds how long
# the carousel may keep *rotating inside one turn*, and reintroducing it would
# cut off a turn that was about to recover. This bounds something else: how
# long any single *rest* may hold one credential, whatever produced the
# number — a provider's own stated wait, ``daily_quota_cooldown_seconds``
# above, ``core.carousel``'s escalation ladder, or the widening
# ``core.escalate.stretch`` applies on the host-block path. Before this the
# only bound on any of them was ``carousel.HARD_DELAY_CAP_S``, a day, and
# ``research/1.8.0.0/gate/1707.json``'s ``real-17`` is what that cost: five
# real ``openai-codex`` ``usage_limit_reached`` refusals, each carrying a
# ``resets_in_seconds`` near 12,000, held a key for 3h+ on the provider's word
# alone.
#
# The owner closed this 2026-09-15 (``PLAN_1.8.0.0.md`` G8, ``decisions/0004``):
# no credential sits out longer than this, ever, on any path. Past it the key
# returns to selection and is probed; if the provider still refuses, it is
# held again — so a genuinely five-hour outage costs about one refused
# request per hour, which is the accepted price. That is the same asymmetry
# every rest in ``core.carousel`` is already built on: a rest that is too
# short costs one failed request in milliseconds, a rest that is too long
# costs a healthy key for its whole duration, silently — and silently is the
# expensive direction, which is why the default is an hour and not a day.
MAX_HOLD = "max_hold_seconds"

# 1.8.0.1. What ``core.carousel.UNSIZED_THROTTLE_REST_S`` / ``core.quota.
# DEFAULT_UNSIZED_THROTTLE_BENCH_SECONDS`` rest an unsized per-minute/rate-
# limit throttle for — the flat re-probe applied only once a refusal has
# never once named a number for this ``provider:model``.
#
# Declared the same way :data:`MAX_HOLD` is above it: a number, not a switch,
# read environment-first, and pushed into the one ``Carousel`` the dispatch
# path actually marks and selects against by ``dispatch_binding.install()`` —
# not left decorative the way ``KAME_MAX_HOLD`` briefly was before RED_TEAM.md
# F3 caught it (see that constant's own comment).
#
# **Thirty, not forty-five.** 1.8.0.1 set this to 45 on a curve that
# double-counted retries — a credential retried several times inside one
# window had its same retry counted once per earlier refusal it followed.
# An independent reviewer, who never read this module, recounted each retry
# exactly once over the whole real corpus (471 retries that followed a bare
# 429) and found 30 the argmax, not 45:
#
#   rest                     avoided   delayed   net seconds returned
#   20s (before 1.8.0.1)         6         0            +5
#   30s                        388         6          +304
#   45s (1.8.0.1)               394         9          +200
#   60s                        409         9           +77
#   90s                        413        11          -236
#
# Almost all of the waste — 388 of 413 — is already gone at 30s. A refused
# round trip was measured at about 850ms, which is what makes this trade
# computable at all. See ``core.carousel.UNSIZED_THROTTLE_REST_S`` for the
# full measurement, which this setting's default matches exactly, and
# ``research/1.8.0.1/expected/rest-decision.md`` for the independent
# re-derivation (+480 for 30s against +385 for 45s, under its own counting).
#
# **Zero means zero.** Unlike :data:`MAX_HOLD`, which refuses to switch off
# (a real ceiling with no ceiling at all is the defect G8 closed), this one
# has an honest off position: a credential the provider refused with no
# number at all is offered again immediately, which is the "spin" the owner
# asked to be able to try against his own traffic and compare against the
# measured 30s — 0 of 73 answered under 30s in the owner's original session,
# so this module's own bet is that spinning will look worse, not that it
# cannot be tried. The range's ceiling, 300s, is
# ``core.carousel.RL_BACKOFF_CAP_S`` — a number past that is not "an unsized
# throttle resting longer", it is a different, provider-stated case this
# setting has no business touching.
UNSIZED_THROTTLE_REST = "unsized_throttle_rest_seconds"

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
    MAX_HOLD: "KAME_MAX_HOLD",
    UNSIZED_THROTTLE_REST: "KAME_UNSIZED_REST",
    UNSIZED_BACKOFF_MAX: "KAME_UNSIZED_BACKOFF_MAX",
}

#: Names this plugin used to answer to, and still does. Read only when the
#: current name says nothing at all, so a config that carries both is decided
#: by the one the user wrote most recently rather than by dictionary order.
#:
#: ``first_token_patience_seconds`` is the pre-1.1.1 name — one release older
#: than ``silent_stream_patience_seconds`` above it. ``decisions/0001-invariants.md``
#: I11 names three spellings for this one setting and says "all three still
#: answer"; this one was missing from this table (``tests/test_v1_8_0_0_invariants.py``'s
#: ``TestI11ADeprecatedSettingNameWorksForEver`` gate caught the gap — grep
#: across the plugin, ``tests/`` and ``CHANGELOG.md`` found the name nowhere
#: but in the CHANGELOG's own history and in the decision's text). Restored
#: rather than dropped from I11, per the same rule that keeps the 1.0.9 name
#: below working: a setting somebody wrote a year ago must not become a
#: silent no-op because a maintainer preferred a different word.
_LEGACY_KEYS: Dict[str, str] = {
    "silent_stream_patience_seconds": STREAM_SILENCE_TIMEOUT,
    "first_token_patience_seconds": STREAM_SILENCE_TIMEOUT,
}

_LEGACY_ENV_FOR: Dict[str, str] = {
    STREAM_SILENCE_TIMEOUT: "KAME_SILENT_STREAM_PATIENCE",
}

#: One generation further back than ``_LEGACY_ENV_FOR`` — the environment-variable
#: half of the same ``first_token_patience_seconds`` restoration above. Kept in
#: its own table rather than folded into ``_LEGACY_ENV_FOR`` because
#: ``control.py`` and ``menu.py`` read that dict directly, by value, expecting
#: exactly one legacy spelling per key; widening its values would change what
#: those two read without a reason of their own to change. ``_env_names``
#: below is the only reader that needs the whole chain.
_LEGACY_ENV_FOR_1_0_8: Dict[str, str] = {
    STREAM_SILENCE_TIMEOUT: "KAME_FIRST_TOKEN_PATIENCE",
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
    # A floor of a minute, not zero: "0 (off)" would mean "no ceiling at all",
    # which is the exact defect G8 closes, so this setting cannot be switched
    # off the way ``STREAM_SILENCE_TIMEOUT`` can. A minute is short enough
    # that setting it there is obviously a deliberate, aggressive choice
    # rather than a typo, and long enough that the probe it produces is not a
    # busy-loop. The ceiling on the ceiling is ``carousel.HARD_DELAY_CAP_S``
    # itself, a day — this setting cannot ask for a hold longer than the
    # absolute sanity bound it is meant to tighten.
    MAX_HOLD: (60.0, 86400.0),
    # 0 is a real, honest position here — unlike ``MAX_HOLD`` above, whose
    # floor of 60 exists because "no ceiling at all" is the defect it closes.
    # This setting has no such trap: 0 means an unsized throttle is offered
    # again immediately, which is the "spin" comparison the owner's own
    # comment (see :data:`UNSIZED_THROTTLE_REST`) asked to be able to run.
    # The ceiling, 300, is ``carousel.RL_BACKOFF_CAP_S`` — past it this is no
    # longer "an unsized throttle", it is a provider-stated case with its own
    # rules this setting does not touch.
    UNSIZED_THROTTLE_REST: (0.0, 300.0),
    # A floor of one second, because the ladder's first rung is one second;
    # the ceiling, 3600, is ``MAX_HOLD``'s own default — the uncapped
    # doubling, still bounded by ``max_hold_seconds`` itself.
    UNSIZED_BACKOFF_MAX: (1.0, 3600.0),
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
    # ``float`` reads "nan". Clamping it lands on whatever side ``max`` and
    # ``min`` happen to return, which is not a reading of anything the person
    # wrote -- so it says nothing, like any other unreadable value.
    if math.isnan(number):
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


#: The switches that are on unless the user turns them off. Every other switch
#: defaults off, which is the plugin doing its whole job: an unmentioned switch
#: leaves every feature running.
#:
#: ``NO_MODEL_FALLBACK`` is here because the owner asked for it and the reason
#: is the pool's whole purpose: it rotates, waits, and calls again at the right
#: moment, so a quota that comes back continues the conversation. Hermes'
#: instinct — answer a spent key with a different model, then a different
#: provider — is a sensible default for an install without a pool and the wrong
#: one for an install with fifteen keys. A silent switch mid-conversation is not
#: a recovery: it is a different answer from a different model, found out
#: afterwards.
#: ``SHARE_POOL_HEALTH`` joined this set once G4 (``PLAN_1.8.0.0.md``)
#: passed — ``tools/continuity_gate.py``, ``research/1.8.0.0/continuity/``.
#: ``decisions/0006`` set the switch's own rule ahead of time: on by default
#: only once G4 passes. It did — see ``REPORT.md`` beside that gate's output
#: — so an install with more than one Hermes profile on the owner's machine
#: gets the shared pool-health file without anyone having to opt in by hand.
#: 1.8.1.0 adds ``UNSIZED_THROTTLE_BACKOFF``: on by default in this release,
#: because it ships as an experiment the owner asked to run against his own
#: traffic from the moment he installs it, not as a knob he has to remember
#: to flip first. See that constant's own comment for the measurement this
#: default runs ahead of.
DEFAULTS_ON = frozenset({NO_MODEL_FALLBACK, SHARE_POOL_HEALTH, UNSIZED_THROTTLE_BACKOFF})


def is_on(key: str) -> bool:
    """Whether the named switch is set, environment first.

    Defaults to ``False`` for each except :data:`DEFAULTS_ON`.
    """
    for variable in _env_names(key):
        from_env = _as_flag(os.environ.get(variable, None))
        if from_env is not None:
            return from_env
    return bool(_FROM_CONFIG.get(key, key in DEFAULTS_ON))


def _env_names(key: str) -> Tuple[str, ...]:
    """Every environment variable that speaks for one setting, best name first."""
    current = _ENV_FOR.get(key) or _NUMBER_ENV_FOR.get(key) or ""
    legacy = _LEGACY_ENV_FOR.get(key, "")
    oldest = _LEGACY_ENV_FOR_1_0_8.get(key, "")
    return tuple(name for name in (current, legacy, oldest) if name)

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
    RECORDER_DISABLED,
    QUOTA_ID_DISABLED,
    CALL_TIMINGS_DISABLED,
    SHARE_POOL_HEALTH,
    UNSIZED_THROTTLE_BACKOFF,
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
    # G8's own number: the owner reaffirmed "no punishment above one hour" on
    # 2026-09-15, replacing the 9h daily waits OpenRouter's own header once
    # bought in full, the 24h escalation ceiling, and ``HARD_DELAY_CAP_S``
    # itself as the number anything actually rests for.
    MAX_HOLD: 3600.0,
    # 1.8.0.2 (30, corrected from 45 in 1.8.0.1 — see
    # :data:`UNSIZED_THROTTLE_REST` for the measurement and the correction).
    # 0 of 73 refused credentials ever answered within the old flat 20s,
    # over the owner's own 2026-09-19 session.
    UNSIZED_THROTTLE_REST: 30.0,
    # 1.8.1.0: see :data:`UNSIZED_BACKOFF_MAX`.
    UNSIZED_BACKOFF_MAX: 64.0,
}

#: What each number counts, for a UI that has to label a field and for a
#: reader who should not have to infer the unit from the name.
UNITS = {
    DAILY_COOLDOWN: "seconds",
    STREAM_SILENCE_TIMEOUT: "seconds",
    STREAM_RESUME_LIMIT: "times",
    MAX_HOLD: "seconds",
    UNSIZED_THROTTLE_REST: "seconds",
    UNSIZED_BACKOFF_MAX: "seconds",
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
    RECORDER_DISABLED: (
        "Stop recording refusals",
        "KAME writes each refusal a provider sends to refusals.jsonl beside its "
        "own state — status, message and body, with API keys removed first — and "
        "decides nothing from it. It is there because the field that tells a "
        "per-minute quota from a per-day one could not be read before 1.7.0.0, "
        "so it appears in no older log and no rule about it can be checked "
        "against real traffic. Writes only, never interrupts a call, stops at "
        "8 MB. Turn this on to stop the writing.",
    ),
    CALL_TIMINGS_DISABLED: (
        "Stop timing calls",
        "KAME writes one line per attempt to calls.jsonl beside its own state: "
        "how long until anything came back, how long until the answer started, "
        "how long the attempt took in total, and how much of the turn had "
        "already been spent waiting for a key. It exists so “is it slow, and "
        "whose fault is it” can be answered with numbers rather than from "
        "memory — a turn that took two minutes is a quota problem or a slow "
        "model, and those look identical from outside. No key, no prompt and no "
        "answer text is written; only durations, a model name and the same key "
        "fingerprint the panel already shows. Writes only, never interrupts a "
        "call, stops at 4 MB. Turn this on to stop the writing.",
    ),
    QUOTA_ID_DISABLED: (
        "Stop reading the quota window",
        "Google's per-minute and per-day free-tier quotas report the identical "
        "metric name and differ only in one field, quotaId, which Hermes' Gemini "
        "adapter parses and throws away before KAME sees the error. KAME keeps "
        "the parsed error body on the exception so that field survives; nothing "
        "else about the error changes, and no other provider is touched. Turn "
        "this on to leave the adapter exactly as Hermes wrote it.",
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
    SHARE_POOL_HEALTH: (
        "Share key health across profiles",
        "For an install with more than one Hermes profile on the same "
        "machine, using the same API keys. Off, each profile learns a "
        "key's rest and its recovery on its own, so one profile can bench a "
        "key while another calls it in the same second and both are told "
        "the same refusal. On, every profile reads and writes one file "
        "outside any profile's own folder, so a bench or a success in one "
        "is seen by the others, and a hold survives restarting Hermes "
        "instead of being thrown away with the process. Never a key — only "
        "a hash of one. Turning this on never makes a hold longer than the "
        "ceiling above already allows, and does nothing at all if only one "
        "profile is running.",
    ),
    UNSIZED_THROTTLE_BACKOFF: (
        "Short ladder for a bare RESOURCE_EXHAUSTED",
        "Applies to one error only: an HTTP 429 whose structured status is "
        "RESOURCE_EXHAUSTED and that carries no retry number and no quotaId "
        "(Gemini's bare refusal). On, that key rests 1s, then 2, 4, 8, 16, "
        "32... on each consecutive repeat on the same key and model, up to "
        "the cap below, instead of the flat rest. Every other throttle keeps "
        "the flat rest. It resets to 1s the moment that key answers, or when "
        "a refusal carries a number the provider stated; a stated number is "
        "never multiplied. In real use it made fewer calls per minute than "
        "the flat rest (24 against 29) without answering less. Off restores "
        "the flat rest exactly.",
    ),
    UNSIZED_BACKOFF_MAX: (
        "Where the ladder stops",
        "The longest rest the bare RESOURCE_EXHAUSTED ladder above will "
        "reach. The default, 64, runs 1, 2, 4, 8, 16, 32, 64 seconds and "
        "then holds at 64: the fastest measured recovery for this error is "
        "23-34s, so going far past 64 only keeps a key out longer than it "
        "needed. Set it to 3600 for the uncapped doubling (1, 2, 4 ... up "
        "to an hour). The ceiling on any hold still bounds it. Clamped to "
        "1-3600.",
    ),
    MAX_HOLD: (
        "Ceiling on any hold",
        "No credential sits out longer than this, whatever set the rest — a "
        "provider's own stated wait, the daily cooldown above, or this "
        "plugin's own escalation. Past it the key returns to selection and "
        "is probed; a refusal that repeats is held again, so a genuinely "
        "long outage costs about one refused request per interval instead "
        "of a healthy key sitting out for however long a provider claimed.",
    ),
    UNSIZED_THROTTLE_REST: (
        "Rest after an unsized throttle",
        "How long a credential rests after a per-minute or rate-limit "
        "refusal that never once named a number - no retry hint, no window. "
        "Measured on real traffic: a refused key retried within 30s answered "
        "0 of 73 times, and over the whole recorded corpus 30s avoids 388 of "
        "413 avoidable refused calls while delaying 6 answers - the best "
        "return of any rest tried. Set to 0 to offer the credential again "
        "immediately instead. Clamped to 0-300; past 300 this is no longer "
        "an unsized throttle's business.",
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
            number = math.nan
        if math.isnan(number):
            # "nan" parses as a float and then passes every range check,
            # because every comparison with it is false.
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
        "default": ALL_NUMBERS[key] if is_number else (key in DEFAULTS_ON),
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
        "Extras, each for one specific problem. Rotation works without any "
        "of them; the ones that ship on say so on their own line.",
        (STREAM_SILENCE_TIMEOUT, NO_MODEL_FALLBACK, SHARE_POOL_HEALTH, UNSIZED_THROTTLE_BACKOFF),
    ),
    (
        "tuning",
        "Tuning",
        "Already set to what this plugin was built against. Safe to change, "
        "rarely worth changing.",
        (DAILY_COOLDOWN, STREAM_RESUME_LIMIT, MAX_HOLD, UNSIZED_THROTTLE_REST, UNSIZED_BACKOFF_MAX),
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
