"""Check the host facts that KAME's *non*-decisions rest on.

Decision 45 says a piece not ported must cite the host's own line. A citation
is a claim about somebody else's code, and somebody else's code moves. This
turns each citation into a check: if Hermes ever changes so that a piece KAME
deliberately did not port becomes reachable, this fails and says so.

Nothing here touches the plugin. It reads the installed Hermes source and
asserts the shape KAME reasoned about. No provider is contacted, no
credential is read, no file is written.

    python tools/host_assumptions.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / "AppData/Local/hermes"))
AGENT = HERMES_HOME / "hermes-agent"

failures: List[str] = []


def check(label: str, got, want, meaning: str = "") -> bool:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got  {got!r}")
        print(f"        want {want!r}")
        if meaning:
            print(f"        means: {meaning}")
        failures.append(label)
    return ok


def read(relative: str) -> List[str]:
    return (AGENT / relative).read_text(encoding="utf-8", errors="replace").splitlines()


def find(lines: List[str], pattern: str, *, start: int = 0) -> Optional[int]:
    """1-based line number of the first match at or after ``start``."""
    rx = re.compile(pattern)
    for index in range(start, len(lines)):
        if rx.search(lines[index]):
            return index + 1
    return None


def find_all(lines: List[str], pattern: str) -> List[int]:
    rx = re.compile(pattern)
    return [i + 1 for i, line in enumerate(lines) if rx.search(line)]


def enclosing_def(lines: List[str], line_number: int) -> str:
    for index in range(line_number - 1, -1, -1):
        stripped = lines[index].lstrip()
        if stripped.startswith("def "):
            return stripped.split("(")[0][4:].strip()
    return ""


# ── the assumptions ───────────────────────────────────────────────────────


def the_empty_retry_never_asks_the_pool(loop: List[str]) -> Tuple[object, object]:
    """Agent Zero rotates the key on a blank answer. Hermes cannot be made to.

    ``kame_engine.py:1742`` treats an answer that carried nothing as a
    possible dead-key symptom and rotates to the next key, because in Agent
    Zero KAME owns the call loop. Hermes owns its own: an empty answer is
    retried up to three times by ``continue``-ing the API loop, and that path
    never re-reads the credential pool. Whatever KAME decided when the hook
    fired, the retry goes out on the same key.

    So the region between the empty-response retry and its ``continue`` must
    stay free of every way the live key changes. The day it is not, the port
    becomes worth making.
    """
    start = find(loop, r"Empty response retry")
    if start is None:
        return "the empty-response retry block", "not found"
    stop = find(loop, r"^\s+continue\s*$", start=start)
    if stop is None:
        return "its continue", "not found"
    touches = [
        line
        for line in loop[start:stop]
        if re.search(r"_credential_pool|pool\.select|_swap_credential|\.api_key\s*=", line)
    ]
    return [t.strip()[:60] for t in touches], []


def the_only_outside_selection_is_per_turn(helpers: List[str]) -> Tuple[object, object]:
    """``pool.select()`` is called from one place, and it is not per attempt."""
    sites = find_all(helpers, r"(?<![_\w])pool\.select\(")
    return sorted({enclosing_def(helpers, site) for site in sites}), ["restore_primary_runtime"]


def the_key_only_changes_on_the_error_path(runner: List[str]) -> Tuple[object, object]:
    """The pool swaps the live credential in exactly one function.

    ``_swap_credential`` is reached from ``recover_with_credential_pool``,
    which runs after a *classified error*. This is why every rotation KAME
    influences is a rotation some provider refusal started, and why a signal
    that is not a refusal has no route into key selection.
    """
    sites = find_all(runner, r"^\s*self\.api_key\s*=\s*runtime_key")
    return sorted({enclosing_def(runner, site) for site in sites}), ["_swap_credential"]


def a_content_refusal_never_reaches_the_hook(loop: List[str]) -> Tuple[object, object]:
    """KAME needs no guard against ``finish_reason == "content_filter"``.

    A safety refusal is deterministic for the unchanged prompt, so rotating
    keys against one would burn the whole pool reproducing it. KAME does not
    guard against that because it never sees it: the host returns a
    content-policy result well before the ``post_api_request`` dispatch. A
    guard here would be dead code, and dead code with a test behind it reads
    like a protection that is not there.
    """
    branch = find(loop, r'finish_reason == "content_filter"')
    if branch is None:
        return "the content_filter branch", "not found"
    returned = find(loop, r"return _content_policy_blocked_result", start=branch)
    hook = find(loop, r'"post_api_request"')
    if returned is None or hook is None:
        return "its return and the hook dispatch", "not found"
    return returned < hook, True


def the_hook_still_carries_the_two_counts(loop: List[str]) -> Tuple[object, object]:
    """v0.3.1 reads them, and reads a missing one as unknown rather than zero.

    The fallback is deliberate, so a Hermes that stopped sending these would
    not break KAME — it would quietly cost the empty-answer rule. That is
    exactly the kind of silent loss a check is for.
    """
    present = {
        name
        for name in ("assistant_content_chars", "assistant_tool_call_count", "finish_reason")
        if find(loop, rf"^\s*{name}=") is not None
    }
    return sorted(present), [
        "assistant_content_chars",
        "assistant_tool_call_count",
        "finish_reason",
    ]


def most_error_reports_never_reach_the_classifier(loop: List[str]) -> Tuple[object, object]:
    """Two of the three error reports are not classified, and must not be.

    ``api_request_error`` fires from three places. Only the exception path
    runs ``classify_api_error`` first, which is the hook KAME shapes. The
    other two — an unusable response body and a content refusal — are not
    provider refusals at all, so a plugin that benched a key on them would be
    benching keys for the model being terse.
    """
    sites = find_all(loop, r"_invoke_api_request_error_hook\(")
    classified = 0
    for site in sites:
        window = loop[max(0, site - 30) : site]
        if any("classify_api_error(" in line for line in window):
            classified += 1
    return (len(sites), classified), (3, 1)


def every_api_hook_the_host_offers_is_accounted_for(
    names: Optional[set] = None,
) -> Tuple[object, object]:
    """No API-side hook exists that KAME has never looked at.

    The host dispatches two dozen hook names. Most are about tools, skills,
    sessions and the gateway and have nothing to do with a credential, but
    the ones naming an API request are exactly this plugin's business, and a
    new one appearing is the shape "we left something on the table" takes.

    Three of the four are registered. The fourth, ``api_request_error``, is
    read-only reporting whose two most common sites never run
    ``classify_api_error`` at all — see the check above and section 6.10.23.
    """
    dispatch = re.compile(
        r"(?:invoke_hook|run_hook|has_hook|_invoke_hook)\(\s*\n?\s*['\"]([a-z_]+)['\"]"
    )
    if names is None:
        names = set()
        names.update(_dispatched_hook_names(dispatch))
    return _api_hooks(names), [
        "api_request_error",
        "post_api_request",
        "pre_api_request",
        "transform_api_error_classification",
    ]


def _dispatched_hook_names(dispatch) -> set:
    names = set()
    for path in AGENT.rglob("*.py"):
        parts = set(path.parts)
        if parts & {"venv", "site-packages", "tests", ".hermes-runtime"}:
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        names.update(dispatch.findall(source))
    return names


def _api_hooks(names) -> List[str]:
    # ``api`` as a word, not as a substring: ``api_request_error`` begins with
    # it, so an ``"_api_" in name`` test drops the very hook this check exists
    # to notice — which is what it did on the first run of this check.
    about_an_api_call = re.compile(r"(?:^|_)api(?:_|$)")
    return sorted(name for name in names if about_an_api_call.search(name))


def kame_needs_no_capability_the_host_could_deny(
    text: Optional[str] = None,
) -> Tuple[object, object]:
    """Nothing KAME does sits behind a permission the user must grant.

    Hermes gates seven host surfaces behind declared capabilities, and the
    live logs show it checking ``tools.override`` against this plugin and
    denying it. That deny costs nothing: KAME registers no tools, overrides
    no provider, model, agent, profile or task, and takes no gateway action.
    It reaches the credential pool by wrapping the class, which the registry
    says outright is not what capabilities govern.

    So the plugin installs with no consent prompt and no grant — and this
    check is here for the day that stops being true. A capability naming
    credentials, the pool, or key selection would mean a gate KAME must
    either declare or be silently degraded by, and it would show up as a new
    id here rather than as an error anywhere.
    """
    if text is None:
        registry = AGENT / "hermes_cli" / "plugin_capabilities.py"
        text = registry.read_text(encoding="utf-8", errors="replace")
    ids = set(re.findall(r"[\"']([a-z_]+\.[a-z_]+)[\"']", text))
    about_our_business = re.compile(r"credential|pool|api_key|rotation|quota")
    return sorted(i for i in ids if about_our_business.search(i)), []


def the_plugin_registers_the_four_it_should(
    text: Optional[str] = None,
) -> Tuple[object, object]:
    """And the manifest still claims exactly those, no more and no less."""
    if text is None:
        manifest = Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation" / "plugin.yaml"
        text = manifest.read_text(encoding="utf-8")
    block = text.split("provides_hooks:", 1)[-1].split("\nconfig_schema:", 1)[0]
    return sorted(re.findall(r"^\s*-\s*([a-z_]+)\s*$", block, re.M)), [
        # v1.0.9 added the fourth. A session reset clears the storm filter and
        # the status line, both of which describe a conversation that has just
        # stopped existing. Cooldowns are deliberately not in that list.
        "on_session_reset",
        "post_api_request",
        "pre_api_request",
        "transform_api_error_classification",
    ]


def _joined(text) -> str:
    return "\n".join(text) if isinstance(text, list) else (text or "")


def the_giveup_counter_is_per_session_not_per_key(
    text: Optional[str] = None,
) -> Tuple[object, object]:
    """v1.0.9's load-bearing fact: the host can refuse before the network.

    ``_check_stale_giveup`` raises once ``_consecutive_stale_streams`` reaches
    ``HERMES_STREAM_STALE_GIVEUP`` (5), and it raises *before* any request goes
    out. The counter lives on the agent, so it is per session and not per key,
    which means rotating into it spends the whole pool in milliseconds without
    a packet leaving the machine, and spends it again on the next turn.

    KAME does two things about it and both need this to stay true: it clears
    the counter on every rotation, which is what the host itself does on a
    provider swap, and it treats the breaker's own message as terminal if it
    fires anyway. If the counter ever becomes per key, the clearing becomes
    wrong -- it would be erasing a fact about a key rather than about a turn.
    """
    if text is None:
        text = read("agent/chat_completion_helpers.py")
    joined = _joined(text)
    return (
        "_consecutive_stale_streams" in joined
        and "HERMES_STREAM_STALE_GIVEUP" in joined
        and "agent._consecutive_stale_streams = 0" in joined
    ), True


def the_stream_retry_is_a_reconnect_not_a_second_go_at_a_spent_key(
    text=None,
):
    """Why KAME stopped setting ``HERMES_STREAM_RETRIES`` to 0.

    v1.0.9 read the name and assumed Agent Zero's bug: retries against a key
    already known to be spent. Hermes' variable is a different thing wearing
    the same word. It is consulted in one place, and only the *transient
    network* branch acts on it -- a timeout, a dropped connection, an SSE
    parse error, an empty stream -- where the repair is a fresh socket to the
    same endpoint. A 429 or a 401 arrives as an ``APIStatusError``, never
    enters that branch, and already reaches KAME on the first try.

    So zeroing it bought nothing on the failures the carousel exists for, and
    spent the one recovery that was free and invisible: a blip mid-answer now
    ends the stream, and the user reads
    ``[System: The previous response was cut off...]``.

    This probe holds KAME to leaving it alone. If the host ever starts
    consulting the variable on status errors too, the reasoning above stops
    being true and the decision deserves revisiting.
    """
    if text is None:
        text = read("agent/chat_completion_helpers.py")
    joined = _joined(text)
    return (
        'env_int("HERMES_STREAM_RETRIES"' in joined
        and "_stream_attempt < _max_stream_retries" in joined
        and "Transient network / timeout error. Retry the" in joined
    ), True


def kame_never_writes_the_hosts_stream_variables(_=None):
    """The other half of the probe above, aimed at KAME rather than the host.

    A comment saying "we do not touch this" is not a check. This one reads the
    shipped plugin and fails if an assignment to one of the host's stream
    variables has come back.

    **The one exception, and its terms.** Since 1.1.1 ``_SilenceTimeout`` in
    ``dispatch_binding.py`` lowers ``HERMES_STREAM_READ_TIMEOUT`` around a
    single attempt, because that is how ``stream_silence_timeout_seconds`` is
    implemented -- the host reads the variable inside the call, so a scoped
    change is possible and a watchdog of KAME's own would not be. The terms are
    checked here rather than trusted: the class must name that one variable, it
    must put the previous value back in ``__exit__``, and nothing else in the
    package may assign any of the four. 1.0.9 set a host knob without saying
    so and 1.0.10 had to take it back out; this is what stops the third time.
    """
    plugin_dir = Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation"
    names = (
        "HERMES_STREAM_RETRIES",
        "HERMES_STREAM_READ_TIMEOUT",
        "HERMES_STREAM_STALE_TIMEOUT",
        "HERMES_STREAM_STALE_GIVEUP",
    )
    for source in plugin_dir.rglob("*.py"):
        body = source.read_text(encoding="utf-8", errors="replace")
        for name in names:
            for hit in re.finditer(re.escape(name), body):
                line = body[body.rfind("\n", 0, hit.start()) + 1 : body.find("\n", hit.end())]
                if "os.environ[" in line or "setdefault" in line or "putenv" in line:
                    return f"{source.name}: {line.strip()}", True

    # The indirect write. ``_SilenceTimeout`` assigns through ``self.VARIABLE``,
    # which the loop above cannot see, so the class is read on its own terms.
    binding = plugin_dir / "dispatch_binding.py"
    if not binding.is_file():
        return "dispatch_binding.py not found", True
    body = binding.read_text(encoding="utf-8", errors="replace")
    block = re.search(r"class _SilenceTimeout.*?(?=\n(?:class |def |# ---))", body, re.S)
    if not block:
        # No scoped exception at all is a stricter world than the one described
        # above, and passes for the same reason.
        return True, True
    text = block.group(0)
    if 'VARIABLE = "HERMES_STREAM_READ_TIMEOUT"' not in text:
        return "_SilenceTimeout no longer names the read timeout", True
    for marker in ("def __exit__", "os.environ.pop(self.VARIABLE", "os.environ[self.VARIABLE] = self._previous"):
        if marker not in text:
            return f"_SilenceTimeout no longer restores what it changed ({marker})", True
    if "os.environ.get(self.VARIABLE) is not None" not in text:
        return "_SilenceTimeout no longer stands aside when the user set the variable", True
    return True, True


def a_mid_stream_drop_is_returned_and_not_raised(text=None):
    """Why KAME can continue a cut answer at all, and how it recognises one.

    A stream that ends with no ``finish_reason`` after delivering text and no
    tool calls does not raise. It *returns* a stub tagged
    ``PARTIAL_STREAM_STUB_ID`` with ``finish_reason=length``, which every KAME
    before 1.1.1 read as a successful call. The whole seam in
    ``dispatch_binding`` hangs off that: a return value can be inspected,
    added to, and handed on as one response.
    """
    body = read("agent/chat_completion_helpers.py") if text is None else text
    if isinstance(body, list):
        body = "\n".join(body)
    if not body:
        return "chat_completion_helpers.py not found", True
    if "_text_only_dropped_no_finish" not in body:
        return "the text-only drop guard is gone", True
    if "return _build_partial_stream_stub" not in body:
        return "the drop no longer returns a stub", True
    if "PARTIAL_STREAM_STUB_ID" not in body:
        return "the stub is no longer tagged", True
    return True, True


def a_tool_argument_drop_is_still_tagged_apart(text=None):
    """The one cut KAME hands back rather than continuing.

    A drop that happened while a tool call's arguments were still being
    written carries ``_dropped_tool_names``. Half-written JSON arguments are
    not something a second model call can be asked to finish, so
    ``dispatch_binding._partial_text`` returns ``None`` for it and the stub
    goes back to Hermes exactly as before. If the host stopped tagging it,
    KAME would start stitching answers it must not stitch.
    """
    body = read("agent/chat_completion_helpers.py") if text is None else text
    if isinstance(body, list):
        body = "\n".join(body)
    if not body:
        return "chat_completion_helpers.py not found", True
    if "_dropped_tool_names" not in body:
        return "the tool-argument drop is no longer tagged apart", True
    return True, True


def the_stream_read_timeout_is_read_inside_the_call(text=None):
    """Why ``stream_silence_timeout_seconds`` can be scoped to one attempt.

    ``env_float("HERMES_STREAM_READ_TIMEOUT", 120.0)`` is evaluated inside the
    call rather than at import, so setting the variable around one attempt and
    putting it back afterwards actually changes that attempt and nothing else.
    Read at import time, ``_SilenceTimeout`` would be a no-op that looked like
    a feature.
    """
    body = read("agent/chat_completion_helpers.py") if text is None else text
    if isinstance(body, list):
        body = "\n".join(body)
    if not body:
        return "chat_completion_helpers.py not found", True
    if 'env_float("HERMES_STREAM_READ_TIMEOUT"' not in body:
        return "the read timeout is no longer taken from the environment per call", True
    return True, True


def the_agent_still_funnels_visible_text_through_one_method(_=None):
    """The single point 1.1.1 wraps, and why it is not the two callbacks.

    ``_fire_stream_delta`` scrubs the text and then calls *both*
    ``stream_delta_callback`` and ``_stream_callback`` with the same string. A
    shim on each would see every delta twice, and a stitcher behind it would
    trim the same text twice. Wrapping the method is what makes "what the user
    has seen" a single, correct record.
    """
    body = read("run_agent.py")
    if isinstance(body, list):
        body = "\n".join(body)
    if not body:
        return "run_agent.py not found", True
    if "def _fire_stream_delta" not in body:
        return "the delta funnel is gone", True
    block = re.search(r"def _fire_stream_delta.*?(?=\n    def )", body, re.S)
    if not block:
        return "the delta funnel could not be read", True
    text = block.group(0)
    for marker in ("stream_delta_callback", "_stream_callback"):
        if marker not in text:
            return f"the funnel no longer reaches {marker}", True
    return True, True


def the_bridge_can_still_write_a_file(_=None):
    """How a switch in the panel reaches the plugin that has to act on it.

    A runtime Desktop plugin has no HTTP surface of its own, so the settings
    page writes ``control.json`` next to the snapshot and the Python half picks
    it up on its heartbeat. That rests on one bridge method,
    ``writeTextFile`` -- and on it refusing to create directories, which is why
    the request goes in the directory ``state.py`` already makes.
    """
    preload = HERMES_HOME / "hermes-agent/apps/desktop/electron/preload.ts"
    if not preload.is_file():
        return "preload.ts not found", True
    body = preload.read_text(encoding="utf-8", errors="replace")
    if "writeTextFile" not in body:
        return "the desktop bridge no longer exposes writeTextFile", True
    return True, True


def the_desktop_shows_only_a_wait_notice_that_opens_the_right_way(_=None):
    """Why KAME's status line reads ``⏳ waiting on …`` and not its own words.

    Desktop does not render every ``thinking.delta`` it receives. It runs the
    text through ``providerWaitText``
    (``apps/desktop/src/store/provider-wait.ts``), keeps it only if it opens
    with ⏳/⚠/↻ followed by "waiting on", "no output", "no response" or "model
    returned", and passes the empty string on for everything else -- which
    *clears* the row rather than leaving it alone. v1.0.9 said
    ``KAME API Rotation: 15/15 healthy`` every ten seconds, so it was not only
    invisible: it wiped the core's own explanation each time.

    This probe reads the installed Desktop source, rebuilds the gate from it,
    and runs every line KAME can produce through it.
    """
    ui = HERMES_HOME / "hermes-agent/apps/desktop/src/store/provider-wait.ts"
    if not ui.is_file():
        return "provider-wait.ts not found", True
    body = ui.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"return\s+/(\^.+?)/i\.test\(value\)", body)
    if not match:
        return "the gate is no longer a single regex test", True
    host_pattern = match.group(1).replace("(?:", "(?:")
    gate = re.compile(host_pattern, re.IGNORECASE)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        import importlib.util

        plugin_dir = Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation"
        spec = importlib.util.spec_from_file_location(
            "kame_probe_pkg",
            plugin_dir / "__init__.py",
            submodule_search_locations=[str(plugin_dir)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        db = importlib.import_module("kame_probe_pkg.dispatch_binding")
    except Exception as exc:  # pragma: no cover - probe-only path
        return f"could not load the plugin: {exc}", True

    lines = [
        db.status_line(15, 15, subject="gemini-2.5-pro"),
        db.status_line(12, 15, "on key 3", subject="gemini-2.5-pro", symbol="\u21bb"),
        db.status_line(0, 15, "next key in 1m 23s", subject="a key to come back"),
        db.status_line(
            15, 15, "back after 4m12s", subject="", symbol="\u21bb",
            opener="model returned",
        ),
    ]
    for line in lines:
        if not gate.match(line):
            return f"Desktop would blank the row for: {line}", True
        if not db.passes_desktop_status_gate(line):
            return f"KAME's own copy of the gate disagrees for: {line}", True
    # And the copy must still be a copy: something the host rejects must be
    # rejected here too, or the two have drifted apart in the safe direction
    # only by luck.
    stale = "KAME API Rotation: 15/15 healthy"
    if gate.match(stale) or db.passes_desktop_status_gate(stale):
        return "the gate accepts what v1.0.9 sent, so it is not the gate", True
    return True, True


def a_cut_answer_still_appends_a_row_the_client_never_sees(
    text: Optional[str] = None,
) -> Tuple[object, object]:
    """Why ``/kame`` counts mid-stream cuts instead of trying to repair them.

    Continuing a cut-off answer appends a synthetic user row tagged
    ``_length_continuation_nudge``. The server counts it, the client never
    renders it, and ``_reconcile_client_ordinal`` then refuses the rewind or
    the edit whose ordinal no longer matches. That arithmetic is the host's,
    and KAME rewriting a history the host owns would be a far worse bug than
    the one it set out to fix.

    So KAME's entire answer is to make the cause visible and to stop causing it
    more often than it has to. If this tag ever disappears, the diagnostic in
    ``/kame`` is measuring something that no longer happens and needs to say
    something else instead.
    """
    if text is None:
        text = read("agent/conversation_loop.py")
    return '"_length_continuation_nudge": True' in _joined(text), True


def every_attempt_still_carries_its_own_timeout(
    text: Optional[str] = None,
) -> Tuple[object, object]:
    """v1.0.1's load-bearing fact: a hung socket errors instead of hanging.

    Agent Zero's ADR 0002 removed every artificial timeout and accepted one
    consequence it could not close: *"if a connection genuinely hangs (TCP-level
    stall, never errors, never completes), KAME will wait indefinitely."*

    1.0.1 removes the ceiling here too, and it is only safe to because Hermes
    puts a timeout on each attempt itself — ``_resolved_api_call_timeout()``,
    1800 s by default, passed as ``timeout=`` on the chat-completions call. A
    stalled socket therefore surfaces as an error the carousel rotates on.

    If this ever stops being passed, the unbounded wait becomes the unbounded
    hang the ADR warned about, and that has to fail loudly here rather than be
    discovered by a user whose turn never came back.
    """
    lines = read("agent/chat_completion_helpers.py") if text is None else text
    if isinstance(lines, str):
        lines = lines.splitlines()
    return bool(find(lines, r"timeout=agent\._resolved_api_call_timeout\(\)")), True


def the_agent_never_runs_on_the_event_loop(
    text: Optional[str] = None,
) -> Tuple[object, object]:
    """The other half of why an unbounded wait is safe: it blocks nothing.

    The carousel sleeps in the calling thread. If that thread were the asyncio
    event loop, an hour-long wait would freeze the websocket, the heartbeat and
    the stop button — the wait would *become* the hang.

    It is not: the web server dispatches the blocking agent call through
    ``asyncio.to_thread`` / ``run_in_executor``, so the loop stays free while
    KAME waits. The day that changes, the wait needs a different design, not a
    smaller number.
    """
    lines = read("hermes_cli/web_server.py") if text is None else text
    if isinstance(lines, str):
        lines = lines.splitlines()
    offloaded = bool(
        find(lines, r"asyncio\.to_thread\(") or find(lines, r"run_in_executor\(")
    )
    return offloaded, True


def the_status_channel_is_still_there_and_still_safe(
    text: Optional[str] = None,
) -> Tuple[object, object]:
    """And the wait can still say it is a wait.

    ``_emit_status`` is what turns an unbounded wait from a freeze into a
    decision the user gets to make. Two properties matter and both are the
    host's: it reaches the gateway as well as the CLI, and it swallows its own
    exceptions — "so it cannot interrupt the retry/fallback logic". KAME guards
    the call anyway, but a channel that started raising would be a channel that
    stopped being the right one to use from inside a recovery.
    """
    lines = read("run_agent.py") if text is None else text
    text = chr(10).join(lines) if isinstance(lines, list) else lines
    block = text.split("def _emit_status", 1)
    if len(block) < 2:
        return "missing", "present and exception-safe"
    body = block[1].split("def _emit_warning", 1)[0]
    safe = "except Exception" in body and "status_callback" in body
    return ("present and exception-safe" if safe else "present but unguarded"), (
        "present and exception-safe"
    )



def the_host_repairs_malformed_tool_arguments_itself(
    text: Optional[str] = None,
) -> Tuple[object, object]:
    """Why KAME-Hermes has no equivalent of Agent Zero's tool-argument heal.

    A0 ships ``_10_kame_heal_tool_args.py``: it runs before A0's validator and
    rescues a response call whose arguments came back null, or under a wrong
    key ("content", "answer", "response") instead of "text". On A0 that turns a
    wasted repair round-trip — a whole extra model call, on a rotated pool that
    KAME is trying to conserve — into the answer the model already wrote.

    Hermes does it itself. ``chat_completion_helpers`` tracks truncated tool
    arguments through the stream and decides what to do with them, and
    ``agent_runtime_helpers`` drops empty or malformed ``tool_calls`` arrays off
    assistant messages before they can reach anything. Porting the heal would
    put a second opinion in front of a repair the host already performs, and
    two repairers disagreeing about the same malformed payload is a worse
    failure than the one being fixed.

    If the host ever stops doing this, the port becomes worth making, and this
    is where that shows up.
    """
    lines = read("agent/chat_completion_helpers.py") if text is None else text
    if isinstance(lines, str):
        lines = lines.splitlines()
    return bool(find(lines, r"has_truncated_tool_args")), True


DESKTOP = "apps/desktop/src"


def _desktop(relative: str) -> str:
    path = AGENT / DESKTOP / relative
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def the_slash_row_still_renders_plain_text(_=None):
    """Why ``/kame`` is text and not markdown.

    A plugin command's reply arrives as a system message. Desktop matches it
    with ``SLASH_STATUS_RE`` and paints it with ``LinkifiedText ...
    pretty={false}`` inside a ``whitespace-pre-wrap`` block -- no markdown
    parser anywhere on that path, which is why v1.0.10's headings and tables
    reached the user as their own source.

    If Desktop ever starts rendering that row as markdown, this fails, and
    ``menu.py`` becomes free to use it.
    """
    body = _desktop("components/assistant-ui/thread/system-message.tsx")
    if not body:
        return "system-message.tsx not found", True
    if "SLASH_STATUS_RE" not in body:
        return "the slash reply is no longer matched by SLASH_STATUS_RE", True
    # The multiline branch is the one a panel lands in.
    if not re.search(r"whitespace-pre-wrap[^>]*pretty=\{false\}", body):
        return "the slash reply is no longer rendered with pretty={false}", True
    return True, True


def the_desktop_still_loads_a_standalone_runtime_plugin(_=None):
    """Why the chip installs to ``desktop-plugins/`` and not into the package.

    Both roots go through the same loader, but the unified one -- the desktop
    half of an agent-plugin package -- is loaded with ``defaultEnabled: false``
    to match the Python half's installed-but-inert posture. A status chip that
    only appears after someone finds a toggle is not a status chip, so KAME's
    Desktop half goes to the standalone door, which keeps its default-on
    trust.

    Two facts, both required: the standalone root is still scanned as
    ``<root>/plugin.js``, and the unified root still caps the default. If the
    second one ever changes, the install could move into the package and be
    one directory instead of two.
    """
    body = _desktop("contrib/runtime-loader.ts")
    if not body:
        return "runtime-loader.ts not found", True
    if "desktopPluginsRoot" not in body or "/plugin.js" not in body:
        return "the standalone desktop-plugins door is gone", True
    if not re.search(r"defaultEnabled:\s*false", body):
        return "the unified root no longer caps defaultEnabled", True
    return True, True


def the_sdk_still_exports_what_the_chip_imports(_=None):
    """Every name KAME's Desktop half imports, checked against the SDK.

    A runtime plugin may import ``@hermes/plugin-sdk`` and ``react`` and
    nothing else; the loader rejects anything else outright, and a name the
    SDK stopped exporting fails at import time with the plugin already half
    registered. Reading both sides here turns that into a check that runs
    before the deploy rather than a toast after it.
    """
    plugin = Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation/desktop-ui/plugin.js"
    if not plugin.is_file():
        return "desktop/plugin.js is missing", True
    source = plugin.read_text(encoding="utf-8")

    specifiers = set(re.findall(r"""from\s+['"]([^'"]+)['"]""", source))
    allowed = {"@hermes/plugin-sdk", "react", "react/jsx-runtime", "react/jsx-dev-runtime"}
    if not specifiers <= allowed:
        return f"imports the loader will refuse: {sorted(specifiers - allowed)}", True

    block = re.search(r"""import\s*\{([^}]*)\}\s*from\s*['"]@hermes/plugin-sdk['"]""", source)
    if not block:
        return "the plugin no longer imports the SDK", True
    wanted = {name.strip() for name in block.group(1).split(",") if name.strip()}

    sdk = _desktop("sdk/index.ts")
    if not sdk:
        return "sdk/index.ts not found", True
    exported = set(re.findall(r"\b([A-Za-z_$][\w$]*)\b", sdk))
    missing = sorted(name for name in wanted if name not in exported)
    if missing:
        return f"the SDK no longer exports: {missing}", True
    return True, True


def the_gemini_adapter_still_merges_parallel_tool_calls(_=None):
    """The bug the repair in ``gemini_slots.py`` exists to undo.

    ``translate_stream_event`` keys a tool-call slot on part index, name and
    thought signature. Two parallel calls to the same tool arrive as the same
    part index, under the same name, with no signature -- so they share a slot
    and their two complete JSON argument objects are concatenated into one
    string that parses as neither. Hermes cannot repair it, substitutes ``{}``,
    reads the empty call as truncated, retries four times and reports
    "Response truncated due to output length limit" on a turn nowhere near a
    length limit.

    ``gemini_slots`` re-checks all of this at runtime before it patches
    anything -- it reproduces the merge on a synthetic stream and proves its
    repair separates it. This static probe is the earlier warning: if the host
    fixes its own bug, the patch should be removed, and this is where that
    shows up.
    """
    path = AGENT / "agent/gemini_native_adapter.py"
    if not path.is_file():
        return "gemini_native_adapter.py not found", True
    body = path.read_text(encoding="utf-8", errors="replace")
    if "def translate_stream_event" not in body:
        return "translate_stream_event is gone -- the patch no longer applies", True
    for marker in ("tool_call_indices", "last_arguments"):
        if marker not in body:
            return f"the merge no longer looks the way KAME patched it ({marker} missing)", True
    return True, True


def the_installer_still_stops_at_manifest_version_one(_=None):
    """Why plugin.yaml says ``manifest_version: 1`` and not 2.

    Hermes carries two constants with the same job and different values:
    ``hermes_cli/plugins.py`` (the loader) understands 2, and
    ``hermes_cli/plugins_cmd.py`` (the installer behind ``hermes plugins
    install`` and the Desktop plugin dashboard) understands 1 -- and *raises*
    on anything higher rather than warning. A manifest that declares 2 loads
    fine once it is on disk and cannot be installed from a repository at all,
    which is the one thing a marketplace listing has to be able to do.

    This check exists to be the reason the number goes back up: when the
    installer's own constant reaches 2, this fails, and the manifest can
    follow it.
    """
    body = (AGENT / "hermes_cli/plugins_cmd.py").read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^_SUPPORTED_MANIFEST_VERSION\s*=\s*(\d+)", body, re.MULTILINE)
    if not match:
        return "the installer no longer gates on manifest_version", True
    supported = int(match.group(1))

    manifest = Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation/plugin.yaml"
    declared = re.search(
        r"^manifest_version:\s*(\d+)", manifest.read_text(encoding="utf-8"), re.MULTILINE
    )
    if not declared:
        return "plugin.yaml no longer declares manifest_version", True
    if int(declared.group(1)) > supported:
        return (
            f"plugin.yaml declares manifest_version {declared.group(1)} and the "
            f"installer refuses anything above {supported}"
        ), True
    if supported > 1:
        return f"the installer now supports manifest_version {supported}", True
    return True, True


def gemini_still_reads_an_assistant_turn_as_a_model_turn(_=None):
    """Why a continuation needs two shapes, and why the second one is valid.

    The native adapter translates ``role: assistant`` to a Gemini ``model``
    content, so 1.1.1's prefill -- a trailing assistant message -- is exactly
    the request the API answers with ``400 INVALID_ARGUMENT: Requests ending
    with a model turn are not supported``. 1.1.2's second shape appends a
    short user turn after the prefill, which the same translation makes a
    ``user`` content: alternation-valid, and ending where the API insists.

    If the mapping ever stops being assistant->model, the whole reasoning
    behind ``stitch.continuation(trailing_user=True)`` needs re-reading.
    """
    path = AGENT / "agent/gemini_native_adapter.py"
    if not path.is_file():
        return "gemini_native_adapter.py not found", True
    body = path.read_text(encoding="utf-8", errors="replace")
    if not re.search(r'gemini_role\s*=\s*"model"\s+if\s+role\s*==\s*"assistant"', body):
        return "assistant is no longer translated to a model turn", True
    if "_build_gemini_contents" not in body:
        return "the contents builder KAME reasoned about is gone", True
    return True, True


def a_refusal_still_arrives_with_the_provider_words_in_it(_=None):
    """Why ``stitch.refuses_prefill`` can match on what the provider said.

    ``GeminiAPIError`` passes its formatted message to ``Exception.__init__``,
    so ``str(exc)`` is the whole sentence -- ``Gemini HTTP 400
    (INVALID_ARGUMENT): Requests ending with a model turn are not supported``
    -- and that is the only place the words live: the class carries a ``code``
    and a ``status_code``, but no ``.message`` attribute. KAME reads
    ``getattr(exc, "message", "")`` *and* ``str(exc)`` for that reason.

    Two facts: the message still reaches the exception's own text, and the
    status still travels as an integer ``status_code`` so a 400 is still read
    as terminal rather than as a reason to spend the pool.
    """
    path = AGENT / "agent/gemini_native_adapter.py"
    if not path.is_file():
        return "gemini_native_adapter.py not found", True
    body = path.read_text(encoding="utf-8", errors="replace")
    if "class GeminiAPIError" not in body or "super().__init__(message)" not in body:
        return "the provider error no longer carries its message as its text", True
    if not re.search(r'message\s*=\s*f"Gemini HTTP \{status\}', body):
        return "the refusal sentence is no longer built into the message", True
    if "status_code=status" not in body.replace(" ", "").replace("\n", "") and (
        "status_code" not in body
    ):
        return "the status no longer travels with the error", True
    return True, True



def the_host_still_appends_its_own_guidance_to_a_429(_=None):
    """Why ``host_text`` exists, and why it imports rather than copies.

    Hermes appends ``_FREE_TIER_GUIDANCE`` to every free-tier 429, and that
    paragraph contains the words "requests/day". ``quota._PER_DAY_MARKERS``
    matches "/day", so a sixty-second per-minute throttle arrived carrying, in
    the host's own handwriting, the phrase that means "spent for the day" --
    and was benched for an hour, on key after key. Seventy-nine such lines in
    nine days against a pool of fourteen.

    Two facts, and the second is the one that decays quietly: the block is
    still appended, and it still contains the day phrase. The day Hermes
    rewords it, a copied literal stops matching and the bug comes back
    invisibly -- which is why ``host_text`` imports the constant and this
    check fails loudly when the name moves.
    """
    path = AGENT / "agent/gemini_native_adapter.py"
    if not path.is_file():
        return "gemini_native_adapter.py not found", True
    body = path.read_text(encoding="utf-8", errors="replace")
    if "_FREE_TIER_GUIDANCE" not in body:
        return "the free-tier guidance block is gone -- host_text may be dead code", True
    if "requests/day" not in body:
        return "the guidance no longer says requests/day -- re-check the day markers", True
    if "message = message + _FREE_TIER_GUIDANCE" not in body.replace("  ", " "):
        return "the guidance is no longer appended to the message", True
    return True, True


def the_provider_error_still_carries_its_evidence(_=None):
    """The five fields ``core.evidence`` harvests off a failed call.

    Until 1.4.0 the binding read ``getattr(exc, "message", "")`` -- an
    attribute this class does not define -- and nothing else. The cascade in
    ``quota`` therefore had nothing to size from: ``reset_at`` was set on 0 of
    276 recorded blocks and 67 % of cooldowns were guesses. Every one of these
    fields was on the exception the whole time.

    If any of them is renamed, the harvest silently gets quieter rather than
    failing, which is the failure mode this whole release exists to end.
    """
    path = AGENT / "agent/gemini_native_adapter.py"
    if not path.is_file():
        return "gemini_native_adapter.py not found", True
    body = path.read_text(encoding="utf-8", errors="replace")
    missing = [
        name
        for name in ("self.code", "self.status_code", "self.response",
                     "self.retry_after", "self.details")
        if name not in body
    ]
    if missing:
        return "the error no longer carries " + ", ".join(missing), True
    return True, True


def the_host_still_discards_retry_info(_=None):
    """Why ``evidence.retry_info_seconds`` reads the raw body.

    The adapter walks ``error.details`` and harvests **only**
    ``google.rpc.ErrorInfo``. ``google.rpc.RetryInfo`` -- the one member
    carrying ``retryDelay`` -- is dropped, and ``retry_after`` is populated
    only from a ``Retry-After`` header that Gemini does not send. That single
    omission is why ``reset_at`` was null on all 276 recorded blocks.

    The day the host harvests RetryInfo itself, reading the raw body becomes
    redundant rather than wrong -- so this check exists to say so, not to
    guard against breakage.
    """
    path = AGENT / "agent/gemini_native_adapter.py"
    if not path.is_file():
        return "gemini_native_adapter.py not found", True
    body = path.read_text(encoding="utf-8", errors="replace")
    if "google.rpc.ErrorInfo" not in body:
        return "the details walk has changed shape entirely", True
    if "google.rpc.RetryInfo" in body:
        return "the host now reads RetryInfo itself -- evidence.retry_info_seconds may be redundant", True
    return True, True


CHECKS = (
    ("agent/conversation_loop.py", "an empty answer is retried on the same key", the_empty_retry_never_asks_the_pool),
    ("agent/agent_runtime_helpers.py", "the pool is asked once a turn, not once a call", the_only_outside_selection_is_per_turn),
    ("run_agent.py", "the live key changes on the error path only", the_key_only_changes_on_the_error_path),
    ("agent/conversation_loop.py", "a content refusal returns before the hook", a_content_refusal_never_reaches_the_hook),
    ("agent/conversation_loop.py", "the success hook still carries what v0.3.1 reads", the_hook_still_carries_the_two_counts),
    ("agent/conversation_loop.py", "only the exception path is classified", most_error_reports_never_reach_the_classifier),
    # v1.0.1. The three facts the unbounded wait rests on. Agent Zero could not
    # make these claims, which is why its own ADR had to accept the hang.
    ("agent/chat_completion_helpers.py", "every attempt carries its own timeout", every_attempt_still_carries_its_own_timeout),
    ("hermes_cli/web_server.py", "the agent runs off the event loop", the_agent_never_runs_on_the_event_loop),
    ("run_agent.py", "the wait can say it is a wait", the_status_channel_is_still_there_and_still_safe),
    # v1.0.2. Why the tool-argument heal on the Agent Zero side stays there.
    ("agent/chat_completion_helpers.py", "the host repairs malformed tool arguments itself", the_host_repairs_malformed_tool_arguments_itself),
    # v1.0.9. The three facts behind this release's refusals: the breaker
    # KAME must not rotate into, the retry KAME switches off, and the row
    # KAME refuses to try to rewrite.
    ("agent/chat_completion_helpers.py", "the give-up counter is per session, not per key", the_giveup_counter_is_per_session_not_per_key),
    ("agent/chat_completion_helpers.py", "the stream retry is a reconnect, not a second go at a spent key", the_stream_retry_is_a_reconnect_not_a_second_go_at_a_spent_key),
    ("agent/conversation_loop.py", "a cut answer still appends a row the client never sees", a_cut_answer_still_appends_a_row_the_client_never_sees),
    # v1.1.1. The three facts the stream seam rests on: the drop is a return
    # value, the one cut that must not be continued is still tagged apart, and
    # the read timeout is still read per call.
    ("agent/chat_completion_helpers.py", "a mid-stream drop is returned, not raised", a_mid_stream_drop_is_returned_and_not_raised),
    ("agent/chat_completion_helpers.py", "a tool-argument drop is still tagged apart", a_tool_argument_drop_is_still_tagged_apart),
    ("agent/chat_completion_helpers.py", "the stream read timeout is read inside the call", the_stream_read_timeout_is_read_inside_the_call),
    # v1.4.0. The three facts behind reading evidence off the exception: the
    # guidance the host appends and KAME has to take back off, the fields the
    # error carries, and the one member of `details` the host drops.
    ("agent/gemini_native_adapter.py", "the host still appends its own guidance to a 429", the_host_still_appends_its_own_guidance_to_a_429),
    ("agent/gemini_native_adapter.py", "the provider error still carries its evidence", the_provider_error_still_carries_its_evidence),
    ("agent/gemini_native_adapter.py", "the host still discards RetryInfo", the_host_still_discards_retry_info),
)


def main() -> int:
    if not AGENT.is_dir():
        print(f"Hermes not found at {AGENT}")
        return 2

    print("the host facts KAME's non-decisions rest on\n")
    sources = {}
    for relative, label, probe in CHECKS:
        if relative not in sources:
            sources[relative] = read(relative)
        got, want = probe(sources[relative])
        check(label, got, want, meaning=(probe.__doc__ or "").strip().splitlines()[0])

    # These two read the whole tree rather than one file, so they sit outside
    # the table above.
    for label, probe in (
        ("every API-side hook the host offers is accounted for", every_api_hook_the_host_offers_is_accounted_for),
        ("KAME needs no capability the host could deny", kame_needs_no_capability_the_host_could_deny),
        ("and KAME registers exactly the four it should", the_plugin_registers_the_four_it_should),
        ("KAME writes no host stream variable outside one scoped exception", kame_never_writes_the_hosts_stream_variables),
        ("Desktop would actually show KAME's status line", the_desktop_shows_only_a_wait_notice_that_opens_the_right_way),
        # v1.1.0. The four facts the Desktop half and the Gemini repair rest on.
        ("a slash command's reply is still plain text", the_slash_row_still_renders_plain_text),
        ("the standalone desktop-plugin door is still default-on", the_desktop_still_loads_a_standalone_runtime_plugin),
        ("the SDK still exports what the chip imports", the_sdk_still_exports_what_the_chip_imports),
        ("Gemini's adapter still merges parallel tool calls", the_gemini_adapter_still_merges_parallel_tool_calls),
        # v1.1.1. The funnel the seam wraps, and the bridge the settings panel
        # writes back through.
        ("the agent funnels visible text through one method", the_agent_still_funnels_visible_text_through_one_method),
        ("the desktop bridge can still write a file", the_bridge_can_still_write_a_file),
        # v1.1.2. The three facts behind the prefill refusal and the manifest
        # number: what the installer will accept, what an assistant turn
        # becomes, and where the provider's own words end up.
        ("the installer still stops at manifest_version 1", the_installer_still_stops_at_manifest_version_one),
        ("Gemini still reads an assistant turn as a model turn", gemini_still_reads_an_assistant_turn_as_a_model_turn),
        ("a refusal still arrives with the provider's words in it", a_refusal_still_arrives_with_the_provider_words_in_it),
    ):
        got, want = probe()
        check(label, got, want, meaning=(probe.__doc__ or "").strip().splitlines()[0])

    # Decision 42: a harness that only ever passes has not been shown to
    # measure anything. Break one on purpose and confirm it notices.
    print("\n  -- and the checks themselves --")
    sabotaged = list(sources["agent/conversation_loop.py"])
    for index, line in enumerate(sabotaged):
        if re.match(r"^\s*assistant_content_chars=", line):
            sabotaged[index] = "                        # removed on purpose"
            break
    got, want = the_hook_still_carries_the_two_counts(sabotaged)
    check(
        "a host that stopped sending the counts would be caught",
        got == want,
        False,
        meaning="the check does not read what it claims to read",
    )

    # The other two read the world rather than a file, so they are handed a
    # doctored world instead of a doctored file. A new API hook appearing in
    # the host is the whole point of the first one — it has to notice.
    got, want = every_api_hook_the_host_offers_is_accounted_for(
        {"pre_api_request", "post_api_request", "transform_api_error_classification",
         "api_request_error", "post_api_stream_chunk"}
    )
    check(
        "a new API hook in the host would be caught",
        got == want,
        False,
        meaning="the check would not notice a surface KAME has never seen",
    )
    got, want = the_plugin_registers_the_four_it_should(
        "provides_hooks:\n  - pre_api_request\n  - post_api_request\nconfig_schema:\n"
    )
    check(
        "a hook quietly dropped from the manifest would be caught",
        got == want,
        False,
        meaning="the check would not notice KAME unregistering itself",
    )

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        print("\nA failure here is not a bug in KAME. It means Hermes changed under")
        print("a decision KAME made about it — read DESIGN.md section 4 and decide again.")
        return 1
    print("every host fact KAME reasoned about still holds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
