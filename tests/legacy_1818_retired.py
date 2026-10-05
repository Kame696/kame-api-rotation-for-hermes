"""1.8.1.8 tests whose subject 1.8.1.9 removed — each with why, and what replaced it.

1.8.1.9 keeps every 1.8.1.8 test file and runs the whole suite against the new
code (``tools/legacy_dispatch.py`` answers the old ``dispatch_binding`` import
with the new transport). What cannot pass is listed here instead of deleted:
a test whose *subject* is gone — a wrapper around a Hermes function, the pool
binding, the in-chat spinner, a removed setting — is skipped with the reason
and the test that now holds the same promise. ``conftest.py`` applies it.

Anything failing that is NOT listed here is a real failure.
"""

from __future__ import annotations

# --- reasons ---------------------------------------------------------------

POOL = ("the pool binding (rewrapped CredentialPool methods) is removed in 1.8.1.9 "
        "(plugin catalog rule 9); the carousel rotates inside KAME's client — "
        "test_transport_1819, test_facade_1819, tools/host_suite_1819.py")
WRAP = ("1.8.1.9 wraps no Hermes function; it hands Hermes a client through a "
        "provider profile — test_facade_1819, test_source_invariants_1819")
SHIM = ("1.8.1.8 watched text through shims on the agent's delivery callbacks; "
        "at the client every character Hermes shows passes through the transport "
        "itself — test_transport_1819 (stitching, tool-call replay, stop rules)")
SPINNER = ("the in-chat spinner line needed the agent, which the client does not "
           "have; the same line is drawn above the Desktop composer "
           "(desktop/plugin.js KameComposerLine; host_assumptions 'Desktop would "
           "actually show KAME's status line')")
TIMEOUT = ("1.8.1.8 scoped the silence timeout by wrapping Hermes' reader; 1.8.1.9 "
           "sets it on the request it owns — test_transport_1819::TestTheSilenceTimeout")
KNOB = ("setting removed in 1.8.1.9 with the binding it tuned (spread_disabled, "
        "live_status_disabled, ...): CHANGELOG 1.8.1.9 'Changed'")
SOURCE = ("reads dispatch_binding.py / pool_binding.py source; the same scan runs "
          "on transport.py in test_source_invariants_1819")
VERSION = "pins the 1.8.1.8 version string; the release is 1.8.1.9"
GEMINI_SLOTS = ("the Gemini parallel tool-call repair (gemini_slots) is removed: Hermes "
                "fixed the merge upstream (#111686)")
QUOTA_ID = ("quota_id_binding wrapped Hermes' Gemini error factory; 1.8.1.9 keeps the "
            "body on its own connection — test_facade_1819::TestTheQuotaWindowSurvivesTheStream, "
            "tools/host_gemini_contracts.py factory_paths")
AUX = ("the auxiliary-lane announcement fed the removed pool binding; auxiliary "
       "calls now get KAME's own client (test_facade_1819)")
RESET = ("on_session_reset only cleared the in-chat spinner, which is gone; the "
         "hook is no longer registered (manifest: two hooks)")
STUB = ("1.8.1.8 got Hermes' partial-stream stub and could hand back the same "
        "object; below Hermes the stream itself is handed back and Hermes builds "
        "the stub — test_transport_1819::TestAToolCallCutOnEveryKey")
INTERRUPT = ("the legacy host fires text and sets the stop flag inside one call; the "
             "transport checks the stop before it yields that text, so nothing was "
             "shown — Hermes' own interrupt path runs, as with its own client")
IDENTITY = ("asserts the host received the very same dict object; through a client "
            "the request travels as keyword arguments — same content, new dict")
SEES_ALL = ("1.8.1.8 refused to continue text it could not see (delivered outside its "
            "funnel); 1.8.1.9 sees every character and continues the answer on "
            "another key — test_transport_1819::TestAStreamCutMidAnswer")
VIGIL = ("the wait notice spoke through agent._emit_status; the client has no agent — "
         "the wait is on the Desktop composer line, the chip and /kame")
BREAKER = ("cleared the agent's stale-stream counter after a rotation; Hermes never "
           "sees a rotation now, so its counter only counts its own stale streams")
FILES = "lists the 1.8.1.8 package files (scope.py, dispatch_binding.py)"
SPREAD_HELP = KNOB

# --- whole modules that cannot import (subject removed) --------------------

COLLECT_IGNORE = {
    "test_aux.py": AUX,
    "test_field.py": "the Settings key-field probe (field_binding) is removed; " + WRAP,
    "test_resolver.py": "the runtime resolver binding is removed; KAME's client splits lists itself — test_facade_1819",
    "test_profile_leases_1818.py": "scope.py (per-home leases of host patches) is removed with the patches",
}

# --- nodeid prefixes --------------------------------------------------------

RETIRED = {
    "tests/test_binding.py::": POOL,
    "tests/test_v1_8_1_0_newer_host_pool.py::": POOL,
    "tests/test_v1_8_1_6.py::TestAHoldWrittenBeforeTheRule": POOL,
    "tests/test_v1_8_1_6.py::TestAsTheHoldIsWritten": POOL,
    "tests/test_v1_8_0_0_ceiling.py::TestTheHostBlockPathIsBounded": POOL,
    "tests/test_v1_7_0_6_policy.py::test_persist_guard_preserves_host_reset_options": POOL,
    "tests/test_v1_6_0_2.py::TestTheUnsizedThrottleFloor": POOL,
    "tests/test_v1_6_0_1.py::TestThePanelOnlyListsProvidersWithKeysBehindThem": POOL,
    "tests/test_plugin.py::TestAnAnswerThatCarriedNothing::test_an_ordinary_answer_is_still_believed":
        "post_api_request fed the pool binding; " + "test_transport_1819::TestWhatTheJournalIsToldAnswered",
    "tests/test_plugin.py::TestAnAnswerThatCarriedNothing::test_a_tool_call_with_no_prose_is_still_believed":
        "test_transport_1819::TestWhatTheJournalIsToldAnswered",
    "tests/test_plugin.py::TestAnAnswerThatCarriedNothing::test_a_host_that_reports_neither_number_changes_nothing":
        "test_transport_1819::TestWhatTheJournalIsToldAnswered",
    "tests/test_plugin.py::TestRegistration::test_registers_exactly_the_four_hooks_it_needs": RESET,
    "tests/test_plugin.py::TestReadingTheSwitchesAtRegistration::test_the_other_switch_is_read_at_the_same_time": KNOB,
    "tests/test_settings.py::TestReadingTheConfig::test_a_switch_set_in_the_config_is_read": KNOB,
    "tests/test_settings.py::TestReadingTheConfig::test_nothing_configured_leaves_everything_running": KNOB,
    "tests/test_settings.py::TestTheManifestDeclaresThem::test_every_switch_is_declared_where_hermes_looks": KNOB,
    "tests/test_settings.py::TestTheNumbers::test_a_host_that_rejects_the_read_changes_nothing": KNOB,
    "tests/test_settings.py::TestWhichSourceWins::test_the_two_switches_do_not_read_each_other": KNOB,
    "tests/test_status.py::TestShowingTheSpread::test_the_help_explains_the_section": SPREAD_HELP,
    "tests/test_v1_0_9.py::TestTheKameCommand": KNOB,
    "tests/test_v1_0_9.py::TestTheSettingsAreReadableAndTraceable": KNOB,
    "tests/test_v1_1_0.py::TestTheSnapshotIsTheDoorToARealPanel::test_a_setting_carries_its_provenance": KNOB,
    "tests/test_dispatch.py::TestInstalling": WRAP,
    "tests/test_v1_0_8.py::TestBindBySignature": WRAP,
    "tests/test_v1_0_8.py::TestJitter::test_install_passes_active_jitter":
        "reads dispatch_binding.install; the production jitter is wired in facade.TRANSPORT",
    "tests/test_dispatch.py::TestAFailureThatMustBeRaised::test_a_drop_after_the_user_saw_text_is_not_replayed": SEES_ALL,
    "tests/test_v1_1_1.py::TestWhatIsNeverStitched::test_a_connection_that_dies_after_visible_text_is_not_replayed": SEES_ALL,
    "tests/test_v1_1_1.py::TestWhatIsNeverStitched::test_a_host_with_no_delivery_funnel_never_stitches": SEES_ALL,
    "tests/test_v1_1_1.py::TestWhatIsNeverStitched::test_a_request_shape_kame_does_not_recognise_is_never_rewritten":
        "_resume_budget no longer takes the agent — test_transport_1819::TestWhatIsDecidedByEvidence",
    "tests/test_v1_6_0_4.py::TestTextKameCannotCaptureStillForbidsAReplay": SEES_ALL,
    "tests/test_waiting.py::TestStreamIntegrity::test_text_already_delivered_is_never_delivered_twice": SEES_ALL,
    "tests/test_waiting.py::TestStreamIntegrity::test_a_stream_that_started_is_not_replayed_even_after_a_long_wait": SEES_ALL,
    "tests/test_waiting.py::TestStreamIntegrity::test_the_shim_returns_what_the_real_callback_returned": SHIM,
    "tests/test_waiting.py::TestWaitingForAKeyToComeBack::test_the_user_is_told_a_long_wait_is_a_wait": VIGIL,
    "tests/test_v1_8_0_0_invariants.py::TestI6KameVoiceNeverEntersHistory": VIGIL,
    "tests/test_stream_recovery_1818.py::": SHIM,
    "tests/test_v1_6_0_4.py::TestTheEndOfStreamSentinelIsNotAnAnswer": SHIM,
    "tests/test_v1_6_0_4.py::TestTheRuleIsWrittenWhereItIsRead": SHIM,
    "tests/test_v1_6_0_4.py::TestTheSilenceTimeoutStillSeesEverything": SHIM,
    "tests/test_v1_6_0_4.py::TestTheSpinnerIsNotAnAnswer": SHIM,
    "tests/test_v1_6_0_4.py::TestWhichChannelCountsAsDelivery": SHIM,
    "tests/test_v1_8_1_7.py::test_reasoning_liveness_on_a_coarse_clock_does_not_claim_text": SHIM,
    "tests/test_v1_1_1.py::TestTheRenamedTimeout::test_it_leaves_the_key_before_the_cut_rather_than_after": TIMEOUT,
    "tests/test_v1_1_1.py::TestTheRenamedTimeout::test_the_host_variable_is_put_back_afterwards": TIMEOUT,
    "tests/test_v1_1_1.py::TestTheRenamedTimeout::test_a_number_the_user_set_themselves_is_never_overruled": TIMEOUT,
    "tests/test_v1_1_1.py::TestTheRenamedTimeout::test_a_local_model_is_left_alone": TIMEOUT,
    "tests/test_v1_1_1.py::TestTheRenamedTimeout::test_off_by_default_changes_nothing": TIMEOUT,
    "tests/test_v1_8_1_1_reliability.py::test_scoped_timeout_isolated_between_threads_and_host_workers": TIMEOUT,
    "tests/test_v1_8_1_1_reliability.py::test_timeout_cleanup_after_exception_and_explicit_host_override": TIMEOUT,
    "tests/test_v1_0_8.py::TestSpinnerThrottle": SPINNER,
    "tests/test_v1_0_9.py::TestTheStatusLine": SPINNER,
    "tests/test_v1_0_9.py::TestOneProcessServesEveryConversation": SPINNER,
    "tests/test_v1_7_0_2.py::test_a_changed_line_is_not_held_back_by_the_new_cadence": SPINNER,
    "tests/test_v1_7_0_2.py::test_a_changed_line_still_has_a_floor": SPINNER,
    "tests/test_v1_7_0_2.py::test_the_same_line_is_still_throttled": SPINNER,
    "tests/test_v1_0_9.py::TestASessionResetForgetsTheConversationNotTheCalendar": RESET,
    "tests/test_v1_0_9.py::TestTheHostBreakerIsNotAKeyFailure": BREAKER,
    "tests/test_v1_1_0.py::TestTheRepairFiresOnlyOnTheCorruption": GEMINI_SLOTS,
    "tests/test_v1_8_0_0_invariants.py::TestI9ProveTheChainAtRuntimeBeforePatchingTheHost": GEMINI_SLOTS,
    "tests/test_v1_7_0_1.py::test_a_body_that_cannot_be_read_costs_nothing": QUOTA_ID,
    "tests/test_v1_7_0_1.py::test_a_drained_stream_is_never_asked_for_its_text_again": QUOTA_ID,
    "tests/test_v1_7_0_1.py::test_a_host_without_the_adapter_is_not_a_failure": QUOTA_ID,
    "tests/test_v1_7_0_1.py::test_installing_twice_does_not_stack_wrappers": QUOTA_ID,
    "tests/test_v1_7_0_1.py::test_the_classifier_can_finally_name_the_window": QUOTA_ID,
    "tests/test_v1_7_0_1.py::test_the_quota_id_survives_the_adapter": QUOTA_ID,
    "tests/test_v1_7_0_1.py::test_uninstall_gives_the_host_its_own_function_back": QUOTA_ID,
    "tests/test_v1_7_0_1.py::test_both_ends_of_an_attempt_are_timed": SOURCE,
    "tests/test_v1_7_0_1.py::test_both_panel_rows_are_cleaned": SOURCE,
    "tests/test_v1_7_0_1.py::test_the_plugin_still_works_without_the_module": SOURCE,
    "tests/test_v1_7_0_1.py::test_the_recording_is_not_cleaned": SOURCE,
    "tests/test_v1_7_0_1.py::test_the_recording_keeps_the_host_s_own_words": SOURCE,
    "tests/test_v1_7_0_1.py::test_the_rotation_path_records_the_payload_it_refused_on": SOURCE,
    "tests/test_v1_6_0_1.py::TestEveryRotationIsOnTheScreen::test_the_kinds_that_were_never_written_now_are": SOURCE,
    "tests/test_v1_6_0_1.py::TestARefusedKeyStopsBeingOffered::test_the_screen_can_tell_resting_from_out_for_good": SOURCE,
    "tests/test_v1_7_0_5.py::test_the_vocabulary_covers_what_dispatch_actually_writes": SOURCE,
    "tests/test_v1_8_1_0_backoff.py::TestTheRungIsVisible::test_dispatch_hands_the_rung_to_the_timings_line": SOURCE,
    "tests/test_v1_8_1_0_backoff.py::TestTheRungIsVisible::test_the_status_surface_says_whether_it_is_on": SOURCE,
    "tests/test_v1_1_2.py::TestTheRefusalIsRecognisedByWhatItSays::test_no_provider_is_named_anywhere_in_the_decision": SOURCE,
    "tests/test_v1_8_0_0_invariants.py::TestI1EvidenceNeverIdentity": SOURCE,
    "tests/test_v1_8_0_0_invariants.py::TestI2TrustTheConnection::test_no_deleted_watchdog_construct_has_come_back": SOURCE,
    "tests/test_v1_8_0_0_invariants.py::TestI7NoKeyMaterialAnywhere::test_every_events_add_call_site_fingerprints_the_key_first": SOURCE,
    "tests/test_v1_8_0_0_invariants.py::TestI8NoProviderErrorTextIsKept::test_journal_record_block_is_fed_only_short_canonical_reasons": SOURCE,
    "tests/test_v1_6_0_0.py::test_a_main_lane_refusal_ends_the_auxiliary_lane_note": AUX,
    "tests/test_v1_6_0_0.py::test_the_note_ends_even_when_the_classifier_declines": AUX,
    "tests/test_v1_1_3.py::TestAStreamThatStoppedInsideAToolCall::test_the_stub_goes_back_untouched": STUB,
    "tests/test_v1_1_2.py::TestNoWayOutOfTheLoopLosesTheAnswer::test_an_interrupt_after_a_cut_keeps_what_was_shown": INTERRUPT,
    "tests/test_serial_release_1818.py::test_serial_failure_rotation_keeps_the_agent_and_payload": IDENTITY,
    "tests/test_serial_release_1818.py::test_stale_environment_and_config_cannot_launch_a_second_request": IDENTITY,
    "tests/test_serial_release_1818.py::test_experiment_is_absent_from_package_schema_and_settings": FILES,
    "tests/test_v1_8_1_0_backoff.py::TestTheSettingItself::test_the_current_patch_version_is_written_consistently": VERSION,
    "tests/test_v1_8_1_7.py::test_current_manifest_and_core_version_agree": VERSION,
    "tests/test_desktop_resume_1818.py::test_both_desktop_display_sites_use_the_null_aware_formatter":
        "counts two call sites of resumeProgress; 1.8.1.9 adds a third (the composer line), "
        "which uses the same null-aware formatter",
}


def reason_for(nodeid: str) -> str:
    """The retirement reason for ``nodeid``, or ``""`` when it is not retired."""
    best = ""
    for prefix in RETIRED:
        if nodeid.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return RETIRED.get(best, "")
