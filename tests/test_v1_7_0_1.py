# v1.7.0.1 — fourteen keys were one credential, and the ladder never saw them.
#
# Written from the owner's own run of 2026-09-05, 19:10-19:28, and not from
# reasoning about the code. What that run recorded:
#
#   * 79 journal blocks, every one of them under the credential id `379e9e`,
#     while the Events tab named fourteen distinct keys rotating.
#   * `under_predictions`: zero, across all 79 — so `WindowStat.looks_short`
#     never became true and `escalate.stretch`, the one mechanism allowed to
#     widen a bench on measured evidence, was never reached.
#   * The visible cost: the same key was handed back every minute for five
#     minutes and refused on its first request of each minute, ~65 calls that
#     had no chance.
#
# Root cause, in one line: `pool_binding._expand` rebuilds the pool as the
# parent row *plus* one child row per key, `candidates()` iterated in pool
# order, and `_add` keeps the first claimant — so the parent claimed all
# fourteen parts and every child row was skipped as already-seen.

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_hermes_v1701"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
dispatch_mod = importlib.import_module(f"{PACKAGE}.dispatch_binding")
journal_mod = importlib.import_module(f"{PACKAGE}.core.journal")
multikey_mod = importlib.import_module(f"{PACKAGE}.core.multikey")
escalate_mod = importlib.import_module(f"{PACKAGE}.core.escalate")
host_text_mod = importlib.import_module(f"{PACKAGE}.host_text")
control_mod = importlib.import_module(f"{PACKAGE}.control")
events_mod = importlib.import_module(f"{PACKAGE}.core.events")

candidates = dispatch_mod.candidates
counted_as = dispatch_mod.counted_as

MODEL = "gemini:gemini-3.8-flash"

KEY1 = "AIzaSyTest1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
KEY2 = "AIzaSyTest2bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
KEY3 = "AIzaSyTest3cccccccccccccccccccccccccccccc"


class FakeEntry:
    def __init__(self, key, entry_id, source="manual"):
        self.runtime_api_key = key
        self.id = entry_id
        self.source = source


class FakePool:
    def __init__(self, entries):
        self._entries = list(entries)

    def entries(self):
        return list(self._entries)


class FakeAgent:
    def __init__(self, pool=None, api_key=""):
        self.api_key = api_key
        self._credential_pool = pool


def _expanded_pool():
    """The shape `pool_binding._expand` leaves behind: parent, then children."""
    blob = f"{KEY1},{KEY2},{KEY3}"
    parent = FakeEntry(blob, "379e9e")
    children = [
        FakeEntry(key, multikey_mod.child_id("379e9e", key), source="kame-part-1")
        for key in (KEY1, KEY2, KEY3)
    ]
    return FakePool([parent] + children)


# --- 1. The child row owns its own key -----------------------------------


def test_a_split_key_is_named_by_its_own_row_not_the_list_it_came_from():
    """Before this release the parent claimed every part and won on order."""
    keys, entry_by_key = candidates(FakeAgent(pool=_expanded_pool()))

    assert sorted(keys) == sorted([KEY1, KEY2, KEY3])
    ids = {entry_by_key[key].id for key in keys}
    assert len(ids) == 3, "three keys must be three credentials, not one row"
    assert "379e9e" not in ids


def test_the_list_row_still_supplies_a_part_nobody_else_owns():
    """Splitting can be off. Then the parent is all there is, and it serves."""
    blob = f"{KEY1},{KEY2}"
    pool = FakePool([FakeEntry(blob, "379e9e")])

    keys, entry_by_key = candidates(FakeAgent(pool=pool))

    assert sorted(keys) == sorted([KEY1, KEY2])
    assert all("," not in key for key in keys)
    # One row, so both parts point at it — and `counted_as` is what keeps the
    # journal from reading them as one credential anyway.
    assert counted_as(entry_by_key[KEY1], KEY1) != counted_as(entry_by_key[KEY2], KEY2)


def test_a_row_that_carries_one_key_keeps_its_own_id():
    """No second name for a credential that already has one."""
    entry = FakeEntry(KEY1, "plain-row")
    assert counted_as(entry, KEY1) == "plain-row"


def test_a_key_the_pool_does_not_know_is_still_nameless():
    """`record_block` drops those on purpose; nothing here invents a name."""
    assert counted_as(None, KEY1) == ""


# --- 2. The measurement the ladder reads ---------------------------------
#
# Replaying the owner's 71 real rotation events of 19:23:07-19:28:07 through
# `journal.short_streak` + `escalate.stretch`, the way `pool_binding` calls
# them — the streak is read *before* the refusal in flight is written down:
#
#     one id for all fourteen   longest streak 1   stretch fired  0 times
#     one id per key            longest streak 5   stretch fired 43 times
#
# `summarize().under_predictions` was never the blind counter — on the same
# events it reads 5 with the shared id and 57 with per-key ids, and it only
# feeds the report. `short_streak` is what feeds the ladder, and it needs a
# *consecutive* run on one credential. Interleave fourteen keys into one
# series and every link is a different key refusing a second earlier, whose
# deadline has not lapsed, so the run breaks at the first step. Every time.


def _rounds(journal, credential_ids, *, start, rounds, rest=54.0):
    """The owner's shape: sweep the keys, rest to the next minute, sweep again.

    Returns the (credential_id, at) pairs in order so a caller can ask what
    the streak was at the moment each refusal arrived — which is when
    `pool_binding._remember` asks.
    """
    asked = []
    at = start
    for _ in range(rounds):
        for offset, credential_id in enumerate(credential_ids):
            moment = at + offset
            asked.append((credential_id, moment))
            journal.record_block(
                at=moment,
                provider="gemini",
                model=MODEL,
                credential_id=credential_id,
                status_code=429,
                reset_at=at + rest,
                sized_by=journal_mod.SIZED_BY_KAME,
                reason="rate_limit",
            )
        at += 60.0
    return asked


def _longest_streak(credential_ids, *, rounds=5):
    """Replay, asking for the streak before each refusal is recorded."""
    journal = journal_mod.Journal()
    longest = 0
    at = 1788647000.0
    for _ in range(rounds):
        for offset, credential_id in enumerate(credential_ids):
            moment = at + offset
            longest = max(
                longest,
                journal_mod.short_streak(
                    journal,
                    credential_id=credential_id,
                    model=MODEL,
                    window="unknown",
                    at=moment,
                ),
            )
            journal.record_block(
                at=moment,
                provider="gemini",
                model=MODEL,
                credential_id=credential_id,
                status_code=429,
                reset_at=at + 54.0,
                sized_by=journal_mod.SIZED_BY_KAME,
                reason="rate_limit",
            )
        at += 60.0
    return longest


def test_one_id_for_every_key_breaks_the_run_the_ladder_needs():
    """The 1.7.0.0 behaviour, pinned so the regression cannot return quietly."""
    assert _longest_streak(["379e9e"] * 3) < 2, (
        "interleaved, each link is another key whose deadline is still a "
        "minute out, so the run resets at the first step"
    )


def test_a_key_measured_short_twice_in_a_row_reaches_the_ladder():
    ids = [multikey_mod.child_id("379e9e", key) for key in (KEY1, KEY2, KEY3)]
    assert _longest_streak(ids) >= escalate_mod.STRIKES_BEFORE_STRETCHING


def test_the_widening_is_bounded_and_only_after_two_measurements():
    """One strike changes nothing; the ceiling is what stops a guess growing."""
    assert escalate_mod.stretch(
        reset_at=1788647054.0, now=1788647000.0, strikes=1,
        window="unknown", reason="rate_limit", source="",
    ) is None

    widened = escalate_mod.stretch(
        reset_at=1788647054.0, now=1788647000.0, strikes=99,
        window="unknown", reason="rate_limit", source="",
    )
    assert widened is not None
    grown = widened - 1788647000.0
    assert grown <= 54.0 * escalate_mod.MAX_FACTOR


# --- 3. The dedupe that was swallowing the rest ---------------------------


def test_keys_refusing_a_second_apart_are_not_one_refusal_repeated():
    """`_already_written` keys on (id, model) with a 2s window.

    Fourteen keys refusing about a second apart shared one id, so all but the
    first looked like the same refusal arriving twice and were never written.
    """
    journal = journal_mod.Journal()
    ids = [multikey_mod.child_id("379e9e", key) for key in (KEY1, KEY2, KEY3)]
    _rounds(journal, ids, start=1788647000.0, rounds=1)

    assert len({block.credential_id for block in journal.blocks()}) == 3




# --- 4. The host's advice does not reach a user who has fourteen keys ------


class FakeGeminiError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message
        self.status_code = 429


REAL_REFUSAL = (
    "Gemini HTTP 429 (RESOURCE_EXHAUSTED): You exceeded your current quota, "
    "please check your plan and billing details.\n"
    "* Quota exceeded for metric: "
    "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
    "limit: 20, model: gemini-3.8-flash\n"
    "Please retry in 53.585627668s."
)

# The host's own paragraph, copied from agent/gemini_native_adapter.py so this
# test is not vacuous when Hermes is not importable and host_text falls back
# to its opening clause. That fallback is exactly the case worth covering: the
# clause is all KAME has, and cutting *at* it is what removes the rest.
HOST_ADVICE = (
    "\n\nYour Google API key is on the free tier (a few hundred requests/day "
    "for Gemini Flash models). Hermes typically makes 3-10 API calls per user "
    "turn, so the free tier is exhausted in a handful of messages and cannot "
    "sustain an agent session. Enable billing on your Google Cloud project and "
    "regenerate the key in a billing-enabled project: "
    "https://aistudio.google.com/apikey"
)


def _with_guidance():
    """The owner's 19:28 error, host paragraph and all."""
    return FakeGeminiError(REAL_REFUSAL + HOST_ADVICE)


def test_the_free_tier_paragraph_is_taken_off_before_the_user_reads_it():
    error = host_text_mod.take_off_the_advice(_with_guidance())

    for phrase in (
        "cannot sustain an agent session",
        "Enable billing",
        "3-10 API calls per user turn",
        "a few hundred requests/day",
    ):
        assert phrase not in error.message, phrase
        assert phrase not in str(error), phrase


def test_the_provider_s_own_words_survive_untouched():
    error = host_text_mod.take_off_the_advice(_with_guidance())

    assert error.message.endswith("Please retry in 53.585627668s.")
    assert "generate_content_free_tier_requests" in error.message
    assert error.status_code == 429
    assert isinstance(error, FakeGeminiError)


def test_an_error_the_host_never_touched_comes_back_unchanged():
    plain = FakeGeminiError(REAL_REFUSAL)
    assert host_text_mod.take_off_the_advice(plain).message == REAL_REFUSAL


def test_a_hostile_error_object_is_still_raisable():
    """Never worth losing a turn over a paragraph."""

    class Awkward(Exception):
        @property
        def message(self):
            raise RuntimeError("no")

    awkward = Awkward("boom")
    assert host_text_mod.take_off_the_advice(awkward) is awkward


# --- 5. A setting change leaves a trace on the timeline -------------------


def _nowhere_real(tmp_path, monkeypatch):
    """Point the plugin's state directory at a scratch folder.

    Without this these tests append to the *installed* plugin's own
    settings-changes.jsonl on whatever machine runs them, which is somebody's
    real evidence file. Found by writing 2 KB into the owner's.
    """
    state_mod = importlib.import_module(f"{PACKAGE}.state")
    monkeypatch.setattr(state_mod, "state_dir", lambda: tmp_path)


def test_turning_a_setting_on_is_recorded_where_the_rotations_are(tmp_path, monkeypatch):
    """The owner set the first-token wait to 10s and only the reset survived."""
    _nowhere_real(tmp_path, monkeypatch)
    events_mod.EVENTS.clear()
    control_mod._record(
        {"action": "set", "key": "stream_silence_timeout_seconds", "ok": True,
         "detail": "in force now and saved"}
    )

    rows = events_mod.EVENTS.recent()
    assert len(rows) == 1
    assert rows[0]["kind"] == events_mod.SETTING
    assert "stream_silence_timeout_seconds" in rows[0]["reason"]
    assert "set" in rows[0]["reason"]
    assert rows[0]["at"]


def test_the_value_is_not_written_down(tmp_path, monkeypatch):
    """`control` is a general path and a future setting could carry a secret."""
    _nowhere_real(tmp_path, monkeypatch)
    events_mod.EVENTS.clear()
    control_mod._record(
        {"action": "set", "key": "stream_silence_timeout_seconds",
         "value": "10", "ok": True, "detail": "in force now and saved"}
    )

    assert "10" not in events_mod.EVENTS.recent()[0]["reason"]


def test_a_whole_panel_reset_still_names_itself(tmp_path, monkeypatch):
    _nowhere_real(tmp_path, monkeypatch)
    events_mod.EVENTS.clear()
    control_mod._record({"action": "reset", "ok": True, "detail": "all back"})

    assert "every setting" in events_mod.EVENTS.recent()[0]["reason"]


def test_a_setting_row_is_not_painted_as_a_fault():
    assert events_mod.SETTING in events_mod.GOOD_KINDS
    assert events_mod.SETTING in events_mod._KINDS


# --- 6. The recorder was reading the quieter of the two lanes -------------


def test_the_rotation_path_records_the_payload_it_refused_on():
    """A tripwire on the source, and it says so.

    The honest test would drive a real refusal through `_on_failure`, which
    needs an agent, a pool and a live exception. This checks the weaker thing
    that still catches the actual regression: 1.7.0.0 recorded only in the
    classification hook, Hermes fires that hook for a fraction of failures,
    and the owner's first session logged 79 refusals in the quota journal
    against 1 line in `refusals.jsonl`.
    """
    source = (PLUGIN_DIR / "dispatch_binding.py").read_text(encoding="utf-8")
    inside = source.split("def _on_failure")[1]
    assert "recorder.record(" in inside, (
        "the in-turn rotation path must write the refusal down; the "
        "classification hook does not see most of them"
    )


def test_the_recording_keeps_the_host_s_own_words():
    """`ev.message` has Hermes' paragraph removed. A recording must not.

    A corpus already edited cannot answer a question about the edit — and the
    edit is exactly what `host_text` exists to make visible.
    """
    source = (PLUGIN_DIR / "dispatch_binding.py").read_text(encoding="utf-8")
    call = source.split("recorder.record(")[1].split(chr(10) + "        )")[0]
    assert "error_message=exc_str" in call
    assert "error_message=message" not in call


def test_both_lanes_still_record():
    """The classification hook keeps its call; this release adds one."""
    hook = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    assert "recorder.record(" in hook


# --- 7. The field that says per-minute or per-day ------------------------
#
# Google's two free-tier quotas report the identical metric name. Only
# `quotaId` separates them, and `gemini_native_adapter.gemini_http_error`
# walks `details`, keeps `google.rpc.ErrorInfo`, and drops the `QuotaFailure`
# entry that carries it. The owner's first measured session: 300 journal rows,
# 300 of them `window: unknown`, on a pool where every refusal was Google's.

import os
import types

quota_id_mod = importlib.import_module(f"{PACKAGE}.quota_id_binding")
classify_mod = importlib.import_module(f"{PACKAGE}.core.classify")

GOOGLE_429_BODY = {
    "error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": "You exceeded your current quota.",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "quotaMetric": "generativelanguage.googleapis.com/"
                        "generate_content_free_tier_requests",
                        "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                        "quotaValue": "20",
                    }
                ],
            },
            {
                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                "retryDelay": "53.585627668s",
            },
        ],
    }
}


class FakeResponse:
    def __init__(self, text, *, drained=False):
        self._text = text
        self._drained = drained
        self.status_code = 429
        self.headers = {}

    @property
    def text(self):
        if self._drained:
            raise RuntimeError("the stream was already consumed")
        return self._text


class HostError(Exception):
    """What the host's factory returns: no body, by construction."""

    def __init__(self, message):
        super().__init__(message)
        self.status_code = 429


def _fake_host(monkeypatched):
    module = types.ModuleType(quota_id_mod.HOST_MODULE)

    def gemini_http_error(response, *, body_text=None):
        # The host's own shape: it parses, uses what it wants, keeps no body.
        return HostError("Gemini HTTP 429 (RESOURCE_EXHAUSTED): quota")

    module.gemini_http_error = gemini_http_error
    monkeypatched[quota_id_mod.HOST_MODULE] = module
    return module


def test_the_quota_id_survives_the_adapter():
    module = _fake_host(sys.modules)
    try:
        assert quota_id_mod.install() is True
        error = module.gemini_http_error(FakeResponse(json.dumps(GOOGLE_429_BODY)))
        assert isinstance(error, HostError), "the host's own error, not a new one"
        assert error.body["error"]["details"][0]["violations"][0]["quotaId"]
    finally:
        quota_id_mod.uninstall()
        sys.modules.pop(quota_id_mod.HOST_MODULE, None)


def test_the_classifier_can_finally_name_the_window():
    """The whole point: `window: unknown` on 300 of 300 rows becomes a word."""
    module = _fake_host(sys.modules)
    try:
        quota_id_mod.install()
        error = module.gemini_http_error(FakeResponse(json.dumps(GOOGLE_429_BODY)))
        named = classify_mod.stated_window(error_body=error.body, error=error)
    finally:
        quota_id_mod.uninstall()
        sys.modules.pop(quota_id_mod.HOST_MODULE, None)

    assert named and named != "unknown", "the quotaId said PerDay"


def test_a_drained_stream_is_never_asked_for_its_text_again():
    """The streaming path hands the text over; asking twice is how this crashes."""
    module = _fake_host(sys.modules)
    try:
        quota_id_mod.install()
        drained = FakeResponse("", drained=True)
        error = module.gemini_http_error(
            drained, body_text=json.dumps(GOOGLE_429_BODY)
        )
        assert error.body["error"]["code"] == 429
    finally:
        quota_id_mod.uninstall()
        sys.modules.pop(quota_id_mod.HOST_MODULE, None)


def test_a_body_that_cannot_be_read_costs_nothing():
    module = _fake_host(sys.modules)
    try:
        quota_id_mod.install()
        error = module.gemini_http_error(FakeResponse("", drained=True))
        assert getattr(error, "body", None) is None
        assert error.status_code == 429
    finally:
        quota_id_mod.uninstall()
        sys.modules.pop(quota_id_mod.HOST_MODULE, None)


def test_installing_twice_does_not_stack_wrappers():
    module = _fake_host(sys.modules)
    try:
        quota_id_mod.install()
        first = module.gemini_http_error
        quota_id_mod.install()
        assert module.gemini_http_error is first
    finally:
        quota_id_mod.uninstall()
        sys.modules.pop(quota_id_mod.HOST_MODULE, None)


def test_uninstall_gives_the_host_its_own_function_back():
    module = _fake_host(sys.modules)
    original = module.gemini_http_error
    try:
        quota_id_mod.install()
        assert module.gemini_http_error is not original
        quota_id_mod.uninstall()
        assert module.gemini_http_error is original
    finally:
        sys.modules.pop(quota_id_mod.HOST_MODULE, None)


def test_the_switch_turns_it_off():
    module = _fake_host(sys.modules)
    original = module.gemini_http_error
    os.environ["KAME_QUOTA_ID_DISABLED"] = "1"
    try:
        assert quota_id_mod.install() is False
        assert module.gemini_http_error is original
    finally:
        os.environ.pop("KAME_QUOTA_ID_DISABLED", None)
        sys.modules.pop(quota_id_mod.HOST_MODULE, None)


def test_a_host_without_the_adapter_is_not_a_failure():
    sys.modules.pop(quota_id_mod.HOST_MODULE, None)
    blocked = types.ModuleType("nothing")
    assert quota_id_mod.install() in (False, True)


# --- 8. The panel says "what the provider actually sent", so it must ------
#
# Reported by the owner after 1.7.0.1 shipped: the free-tier paragraph was
# still on his screen. `take_off_the_advice` covers the error that is raised,
# and `surfaced` was 0 that session — nothing was raised. The Events tab is
# fed from a different string, and that is where he was reading it.


def test_the_panel_detail_carries_no_sentence_the_provider_did_not_send():
    said = REAL_REFUSAL + HOST_ADVICE
    shown = host_text_mod.without_advice(said)

    for phrase in (
        "cannot sustain an agent session",
        "Enable billing",
        "3-10 API calls per user turn",
    ):
        assert phrase not in shown, phrase
    assert shown.endswith("Please retry in 53.585627668s.")


def test_a_message_the_host_never_touched_is_returned_whole():
    assert host_text_mod.without_advice(REAL_REFUSAL) == REAL_REFUSAL
    assert host_text_mod.without_advice("") == ""


def test_both_panel_rows_are_cleaned():
    """One row per lane: a mid-answer drop and an ordinary rotation."""
    source = (PLUGIN_DIR / "dispatch_binding.py").read_text(encoding="utf-8")
    assert source.count("detail=host_text.without_advice(ev.raw_message)") == 2
    assert "detail=ev.raw_message" not in source


def test_the_recording_is_not_cleaned():
    """A corpus already edited cannot answer a question about the edit."""
    source = (PLUGIN_DIR / "dispatch_binding.py").read_text(encoding="utf-8")
    call = source.split("recorder.record(")[1].split(chr(10) + "        )")[0]
    assert "without_advice" not in call


# --- 9. The terse refusal is the same condition worded shorter ------------
#
# Measured on the owner's session of 2026-09-06: of 594 recorded 429s, 447
# arrived as `{"error": {"code": 429, "message": "Resource has been exhausted
# (e.g. check quota).", "status": "RESOURCE_EXHAUSTED"}}` — no quotaId, no
# retryDelay, no details at all — against 51 carrying the whole QuotaFailure.
#
# Read alone each terse one is a throttle worth 20 seconds, so the pool was
# swept again twenty seconds later: 251 refusals in seven minutes on
# gemini-3.7-flash, every one against a day that was already spent. It only
# stopped when a verbose refusal happened to arrive and said PerDay.

carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")


def _pool_of(n=3):
    return [f"key-{i}" for i in range(n)]


def _carousel():
    return carousel_mod.Carousel()


IDENTITY = "gemini:gemini-3.7-flash"


def test_a_terse_throttle_after_a_named_day_is_read_as_that_day():
    """The window memory still fires: the terse refusal is read as the day.

    1.7.0.6 narrows the memory to the same credential. Reading its terse
    refusal as the named window retains the 1.7.0.1 behavior — the
    kind recorded against the key is ``daily``, not ``rate_limit``, which is
    what stops seven minutes of 20-second sweeps against a spent day.

    The size is now the pool's business rather than a strike counter's. While
    the pool is answering the rest stays short whichever label it wears, and
    the hour arrives from silence — see
    ``test_the_named_day_costs_the_hour_once_the_pool_is_quiet`` below.
    """
    car = _carousel()
    keys = _pool_of()
    car.mark(IDENTITY, keys[0], ok=False, kind="daily", delay=0.0)

    car.mark(IDENTITY, keys[0], ok=False, kind="rate_limit", delay=0.0)

    assert car.snapshot()[IDENTITY]["kinds"] == ["daily"], (
        "the provider named the day for this credential and the terse refusal is "
        "the same condition worded shorter — this key is not filed as a bare "
        "throttle"
    )


def test_the_named_day_costs_the_hour_once_the_pool_is_quiet():
    """1.7.0.2. The terse refusal read as a day still reaches the hour.

    It reaches it from the pool going silent rather than from one key
    repeating itself, which is the only version of "the day is over" that the
    owner's measurements support: keys labelled PerDay answered again 6 to 36
    minutes later, twenty-one times, and never needed an hour.
    """
    car = _carousel()
    keys = _pool_of()
    now = 5_000_000.0
    car.mark(IDENTITY, keys[0], ok=False, kind="daily", delay=0.0, now=now)

    quiet = now + carousel_mod.POOL_SILENCE_BEFORE_THE_DAY_S + 1.0
    applied = car.mark(
        IDENTITY, keys[0], ok=False, kind="rate_limit", delay=0.0, now=quiet
    )
    assert applied >= carousel_mod.DAILY_COOLDOWN_S


def test_a_refusal_that_names_its_own_number_is_still_obeyed():
    """The memory is about this model; the number is about this refusal."""
    car = _carousel()
    keys = _pool_of()
    car.mark(IDENTITY, keys[0], ok=False, kind="daily", delay=0.0)

    rest = car.mark(
        IDENTITY, keys[1], ok=False, kind="rate_limit", delay=21.0, stated=True
    )

    assert rest <= 60.0, "a stated number outranks anything remembered"


def test_one_key_answering_forgets_the_day():
    car = _carousel()
    keys = _pool_of()
    car.mark(IDENTITY, keys[0], ok=False, kind="daily", delay=0.0)
    car.mark(IDENTITY, keys[1], ok=True)

    rest = car.mark(IDENTITY, keys[2], ok=False, kind="rate_limit", delay=0.0)

    assert rest < carousel_mod.DAILY_COOLDOWN_S, (
        "a day that is over does not serve a request, so the success is proof "
        "the memory is stale"
    )


def test_the_memory_does_not_cross_models():
    car = _carousel()
    keys = _pool_of()
    car.mark(IDENTITY, keys[0], ok=False, kind="daily", delay=0.0)

    other = car.mark(
        "gemini:gemini-3.8-flash", keys[1], ok=False, kind="rate_limit",
        delay=0.0
    )

    assert other < carousel_mod.DAILY_COOLDOWN_S


def test_a_plain_throttle_never_teaches_the_memory():
    """Only a window longer than a rolling minute is worth remembering."""
    car = _carousel()
    keys = _pool_of()
    car.mark(IDENTITY, keys[0], ok=False, kind="rate_limit", delay=0.0)

    rest = car.mark(IDENTITY, keys[1], ok=False, kind="rate_limit", delay=0.0)

    assert rest < carousel_mod.DAILY_COOLDOWN_S
    assert "rate_limit" not in carousel_mod.NAMED_WINDOW_KINDS


# --- 10. A setting change has to outlive the tab that shows it ------------


def test_the_change_is_written_to_a_file_not_only_to_the_ring(tmp_path, monkeypatch):
    """Events is 150 rows in memory: gone on restart, gone on Clear events."""
    state_mod = importlib.import_module(f"{PACKAGE}.state")
    monkeypatch.setattr(state_mod, "state_dir", lambda: tmp_path)

    control_mod._record(
        {"action": "set", "key": "stream_silence_timeout_seconds",
         "value": "10", "ok": True, "detail": "in force now and saved"}
    )

    written = tmp_path / control_mod.CHANGES_FILENAME
    assert written.is_file()
    row = json.loads(written.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert row["key"] == "stream_silence_timeout_seconds"
    assert row["action"] == "set"
    assert row["at"]
    # Not `"10" not in json.dumps(row)`: the epoch timestamp contains those
    # digits about a tenth of the time, which made this test fail on a clock
    # rather than on a defect. Assert the absence of the field itself.
    assert "value" not in row, "the value is deliberately not stored"
    assert set(row) == {"at", "action", "key", "ok", "detail"}


def test_a_directory_that_cannot_be_written_never_breaks_the_panel(monkeypatch):
    state_mod = importlib.import_module(f"{PACKAGE}.state")
    monkeypatch.setattr(state_mod, "state_dir", lambda: None)

    control_mod._record({"action": "reset", "ok": True, "detail": "back"})


# --- 11. How long it took, so "is it slow?" stops being a memory ----------

timings_mod = importlib.import_module(f"{PACKAGE}.timings")


def _timed(tmp_path, monkeypatch, enable_writer=True, **over):
    state_mod = importlib.import_module(f"{PACKAGE}.state")
    monkeypatch.setattr(state_mod, "state_dir", lambda: tmp_path)
    # Enable only this sandboxed writer; do not depend on earlier tests having
    # changed the suite-wide recording-disable environment or settings cache.
    if enable_writer:
        monkeypatch.setattr(timings_mod, "_off", lambda: False)
    timings_mod.forget()
    row = dict(
        identity=MODEL, key_fingerprint="key:ab12cd", attempt=3, outcome="answered",
        started_at=100.0, ended_at=104.5, first_sign_at=100.4, first_text_at=102.0,
        waited_before_s=61.0, chars_seen=1280,
    )
    row.update(over)
    timings_mod.record(**row)
    written = tmp_path / timings_mod.FILENAME
    if not written.is_file():
        return None
    return json.loads(written.read_text(encoding="utf-8").strip().splitlines()[-1])


def test_the_four_durations_are_written(tmp_path, monkeypatch):
    row = _timed(tmp_path, monkeypatch)

    assert row["ms_total"] == 4500
    assert row["ms_to_first_sign"] == 400
    assert row["ms_to_first_text"] == 2000
    assert row["ms_waited_before"] == 61000
    assert row["attempt"] == 3
    assert row["chars_seen"] == 1280


def test_first_sign_and_first_text_are_not_the_same_number(tmp_path, monkeypatch):
    """On a thinking model reasoning arrives long before the answer does."""
    row = _timed(tmp_path, monkeypatch)
    assert row["ms_to_first_sign"] < row["ms_to_first_text"]


def test_an_attempt_that_never_answered_says_so_rather_than_zero(tmp_path, monkeypatch):
    row = _timed(
        tmp_path, monkeypatch, outcome="rotate", kind="daily", status_code=429,
        first_sign_at=None, first_text_at=None, chars_seen=0,
    )

    assert row["ms_to_first_text"] is None, "zero would be a different, false fact"
    assert row["ms_to_first_sign"] is None
    assert row["kind"] == "daily"
    assert row["status"] == 429


def test_nothing_secret_can_reach_the_file(tmp_path, monkeypatch):
    row = _timed(tmp_path, monkeypatch, key_fingerprint="key:ab12cd")
    written = json.dumps(row)

    assert "AIzaSy" not in written
    for field in row:
        assert field in {
            "at", "identity", "key", "attempt", "outcome", "kind", "status",
            "ms_total", "ms_to_first_sign", "ms_to_first_text",
            "ms_waited_before", "chars_seen", "rest_s",
            "ms_elapsed_before", "ms_pool_waited_before", "call_id",
            # 1.8.1.0: which rung of the doubling backoff produced rest_s.
            "rest_source",
        }, field


def test_a_broken_clock_is_dropped_not_written_negative(tmp_path, monkeypatch):
    row = _timed(tmp_path, monkeypatch, started_at=100.0, ended_at=90.0)
    assert row["ms_total"] is None


def test_the_switch_turns_it_off(tmp_path, monkeypatch):
    monkeypatch.setenv("KAME_CALL_TIMINGS_DISABLED", "1")
    importlib.import_module(f"{PACKAGE}.settings").forget()
    assert _timed(tmp_path, monkeypatch, enable_writer=False) is None


def test_recording_never_raises(tmp_path, monkeypatch):
    state_mod = importlib.import_module(f"{PACKAGE}.state")
    monkeypatch.setattr(state_mod, "state_dir", lambda: None)
    timings_mod.forget()
    timings_mod.record(identity=MODEL, started_at=1.0, ended_at=2.0)


def test_the_ceiling_stops_the_writing(tmp_path, monkeypatch):
    state_mod = importlib.import_module(f"{PACKAGE}.state")
    monkeypatch.setattr(state_mod, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(timings_mod, "CEILING_BYTES", 10)
    timings_mod.forget()
    written = tmp_path / timings_mod.FILENAME
    written.write_text("x" * 64, encoding="utf-8")

    timings_mod.record(identity=MODEL, started_at=1.0, ended_at=2.0)

    assert written.read_text(encoding="utf-8") == "x" * 64


def test_both_ends_of_an_attempt_are_timed():
    """A tripwire: the failing branch and the answering branch, one each."""
    source = (PLUGIN_DIR / "dispatch_binding.py").read_text(encoding="utf-8")
    assert source.count("timings.record(") == 2
    assert 'outcome="answered"' in source


def test_the_plugin_still_works_without_the_module():
    """Deleting it must change no behaviour. Nothing may read it back."""
    for name in ("dispatch_binding.py", "core/carousel.py", "core/classify.py"):
        source = (PLUGIN_DIR / name).read_text(encoding="utf-8")
        assert "timings.record" not in source or name == "dispatch_binding.py"
        assert "from .timings import" not in source
        assert "timings." not in source.replace("timings.record", "")
