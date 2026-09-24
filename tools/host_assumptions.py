"""Check the host facts that KAME's *non*-decisions rest on.

Decision 45 says a piece not ported must cite the host's own line. A citation
is a claim about somebody else's code, and somebody else's code moves. This
turns each citation into a check: if Hermes ever changes so that a piece KAME
deliberately did not port becomes reachable, this fails and says so.

Reads host source and executes Gemini and current runtime contracts in isolated
subprocesses. No provider is contacted, no credentials are read and no installed
plugin or production files are changed. Each runner uses a temporary home and
positive/negative controls; HOST_ALERTS.md records exact scope and UI limitations.

    python tools/host_assumptions.py
"""

from __future__ import annotations

# --- do not write into the owner's evidence -------------------------------
# These tools load the real plugin, and the real plugin records what it sees
# beside the *installed* state file. Running the gate therefore appended its
# 13,561 corpus refusals to `refusals.jsonl` on this machine and filled its
# 8 MB ceiling at 14:01 on 2026-09-06 -- twenty minutes before the owner ran
# the session those recordings existed to explain. The evidence for a real
# refusal was lost to a measurement of a synthetic one.
#
# Set before the plugin is imported, so its modules read it at first use. Only
# this process is affected; nothing about the installed plugin changes.
import os as _os

_os.environ.setdefault("KAME_RECORDER_DISABLED", "1")
_os.environ.setdefault("KAME_CALL_TIMINGS_DISABLED", "1")
# ---------------------------------------------------------------------------

import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / "AppData/Local/hermes"))
AGENT = HERMES_HOME / "hermes-agent"

failures: List[str] = []

_gemini_contract_report = None
_runtime_contract_report = None


def _runtime_executed_contract(name):
    """Use host's dependency-complete interpreter; cache one isolated execution."""
    global _runtime_contract_report
    if _runtime_contract_report is None:
        import json
        import subprocess
        candidates = (AGENT / "venv/Scripts/python.exe", AGENT / "venv/bin/python")
        python = next((str(p) for p in candidates if p.is_file()), sys.executable)
        script = Path(__file__).with_name("host_runtime_contracts.py")
        try:
            proc = subprocess.run([python, "-B", str(script), "--host", str(AGENT)],
                                  capture_output=True, text=True, timeout=180)
            report = json.loads(proc.stdout.strip().splitlines()[-1])
            if proc.returncode not in (0, 1) or not isinstance(report, dict):
                raise ValueError("invalid contract process result")
            _runtime_contract_report = report
        except Exception as exc:
            _runtime_contract_report = {"error": f"runtime contract failed: {type(exc).__name__}"}
    report = _runtime_contract_report
    row = report.get("checks", {}).get(name, {})
    if row.get("passed") is True and report.get("mutation_detected", {}).get(name) is True:
        return True, True
    return report.get("error") or row.get("detail") or "unverified runtime contract", True


def _gemini_executed_contract(name):
    """Cache one isolated real-host run, including negative mutation checks."""
    global _gemini_contract_report
    if _gemini_contract_report is None:
        import json
        import subprocess
        script = Path(__file__).with_name("host_gemini_contracts.py")
        try:
            proc = subprocess.run([sys.executable, "-B", str(script), "--host", str(AGENT)],
                capture_output=True, text=True, timeout=90)
            report = json.loads(proc.stdout.strip().splitlines()[-1])
            if proc.returncode not in (0, 1) or not isinstance(report, dict):
                raise ValueError("invalid contract process result")
            _gemini_contract_report = report
        except Exception as exc:
            _gemini_contract_report = {"error": f"contract runner failed: {type(exc).__name__}"}
    report = _gemini_contract_report
    check_row = report.get("checks", {}).get(name, {})
    if check_row.get("passed") is True and report.get("mutation_detected", {}).get(name) is True:
        return True, True
    return report.get("error") or check_row.get("detail") or "contract or negative control did not pass", True



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


def the_empty_retry_never_asks_the_pool(_=None):
    """Legacy entrypoint; actual current-host behavior and negative control."""
    return _runtime_executed_contract("empty_retry")


def the_only_outside_selection_is_per_turn(_=None):
    """Legacy entrypoint; actual current-host behavior and negative control."""
    return _runtime_executed_contract("pool_selection")


def the_key_only_changes_on_the_error_path(_=None):
    """Legacy entrypoint; actual current-host behavior and negative control."""
    return _runtime_executed_contract("credential_swap")


def a_content_refusal_never_reaches_the_hook(_=None):
    """Legacy entrypoint; actual current-host behavior and negative control."""
    return _runtime_executed_contract("content_refusal")


def the_hook_still_carries_the_two_counts(_=None):
    """Legacy entrypoint; actual current-host behavior and negative control."""
    return _runtime_executed_contract("success_fields")


def most_error_reports_never_reach_the_classifier(_=None):
    """Legacy entrypoint; actual current-host behavior and negative control."""
    return _runtime_executed_contract("error_classification")


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


def the_stream_retry_is_a_reconnect_not_a_second_go_at_a_spent_key(_=None):
    """Legacy entrypoint; actual current-host behavior and negative control."""
    return _runtime_executed_contract("stream_retry")


def kame_never_writes_the_hosts_stream_variables(_=None):
    """The other half of the probe above, aimed at KAME rather than the host.

    A comment saying "we do not touch this" is not a check. This one reads the
    shipped plugin and fails if an assignment to one of the host's stream
    variables has come back.

    1.8.1.1 removes the old exception entirely. ``_SilenceTimeout`` now carries
    a ContextVar and KAME wraps the host's per-call ``env_float`` reader, so two
    simultaneous requests can have different silence budgets without either
    one changing process-global environment state. The explicit host variable
    still wins. This probe checks both halves of that contract.
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

    binding = plugin_dir / "dispatch_binding.py"
    if not binding.is_file():
        return "dispatch_binding.py not found", True
    body = binding.read_text(encoding="utf-8", errors="replace")
    required = (
        '_SILENCE_TIMEOUT_VALUE = contextvars.ContextVar',
        'def _scoped_timeout_reader',
        '_SILENCE_TIMEOUT_VALUE.get()',
        '_SILENCE_TIMEOUT_VALUE.set(',
        '_SILENCE_TIMEOUT_VALUE.reset(',
        'VARIABLE = "HERMES_STREAM_READ_TIMEOUT"',
        'os.environ.get(self.VARIABLE) is not None',
    )
    for marker in required:
        if marker not in body:
            return f"scoped stream timeout contract moved ({marker})", True
    return True, True


def a_mid_stream_drop_is_returned_and_not_raised(_=None):
    """Legacy entrypoint; actual current-host behavior and negative control."""
    return _runtime_executed_contract("partial_stream")


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
    call rather than at import, so KAME can wrap that reader and supply a
    ContextVar value for exactly one logical attempt. Read at import time, the
    scoped override would be a no-op that looked like a feature.
    """
    body = read("agent/chat_completion_helpers.py") if text is None else text
    if isinstance(body, list):
        body = "\n".join(body)
    if not body:
        return "chat_completion_helpers.py not found", True
    if 'env_float("HERMES_STREAM_READ_TIMEOUT"' not in body:
        return "the read timeout is no longer taken from the environment per call", True
    return True, True


def the_stream_worker_inherits_the_callers_context(text=None):
    """Why the 1.8.1.2 ContextVar timeout reaches the request at all.

    KAME sets ``stream_silence_timeout_seconds`` in a ContextVar around one
    attempt. The host runs the streaming request on a worker thread, and a new
    ``threading.Thread`` starts with an EMPTY context unless its target is
    wrapped. Hermes wraps every worker target in ``_context_thread_target``
    (``contextvars.copy_context().run``). If a release starts that thread
    without the wrapper, the scoped timeout silently stops existing.
    """
    body = read("agent/chat_completion_helpers.py") if text is None else text
    if isinstance(body, list):
        body = "\n".join(body)
    if not body:
        return "chat_completion_helpers.py not found", True
    if "def _context_thread_target" not in body:
        return "the host no longer defines _context_thread_target", True
    if "copy_context" not in body:
        return "_context_thread_target no longer copies the caller's context", True
    if body.count("target=_context_thread_target(") < 1:
        return "no worker thread is started through _context_thread_target", True
    return True, True

def the_agent_still_funnels_visible_text_through_one_method(_=None):
    """Legacy entrypoint; actual current-host behavior and negative control."""
    return _runtime_executed_contract("visible_delivery")


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
    # The literal sits on one line; the call around it need not. Hermes 0.21.5
    # wrapped ``.test(value)`` across three lines when it widened the gate to
    # ``(?:still\s+)?waiting on`` -- the same single test, reformatted, and a
    # probe that insisted on one line reported the gate as gone.
    match = re.search(r"return\s+/(\^[^\n]+?)/i\.test\(\s*value\s*\)", body)
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


def a_cut_answer_still_appends_a_row_the_client_never_sees(_=None):
    """Legacy entrypoint; actual current-host behavior and negative control."""
    return _runtime_executed_contract("continuation_row")


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


def the_agent_never_runs_on_the_event_loop(_=None):
    """Legacy entrypoint; actual current-host behavior and negative control."""
    return _runtime_executed_contract("off_event_loop")


def the_status_channel_is_still_there_and_still_safe(_=None):
    """Legacy entrypoint; actual current-host behavior and negative control."""
    return _runtime_executed_contract("status_delivery")



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
    plugin = Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation/desktop/plugin.js"
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
    """Keep KAME's manifest generation aligned with the host installer.

    Older Hermes had a private installer cap of 1 while the loader understood
    2, so KAME deliberately declared v1. Current Hermes fixed that drift: the
    installer imports ``SUPPORTED_MANIFEST_VERSION`` from ``plugins_manifest``.
    This probe verifies that shared gate still exists and that KAME does not
    declare anything newer than the host accepts. KAME intentionally remains
    on v1 while it uses no v2-only syntax, preserving older installer support.
    The old private constant was named ``_SUPPORTED_MANIFEST_VERSION``; keeping
    that historical marker here also makes the invariant suite prove the old
    split-gate regression is still represented.
    """
    body = (AGENT / "hermes_cli/plugins_cmd.py").read_text(encoding="utf-8", errors="replace")
    # Two installer shapes are both real, released Hermes: the tagged 0.21.3
    # (v2026.9.14) still carries the private ``_SUPPORTED_MANIFEST_VERSION = 1``,
    # and 0.21.4+ imports the loader's shared constant. What the invariant
    # needs is not which shape the host has but that KAME's declaration fits
    # under whichever cap this installer enforces -- so each shape yields its
    # cap, and only a missing cap or a declaration above it fails.
    private = re.search(r"^_SUPPORTED_MANIFEST_VERSION\s*=\s*(\d+)", body, re.MULTILINE)
    if "from hermes_cli.plugins_manifest import SUPPORTED_MANIFEST_VERSION" in body:
        manifest_body = (AGENT / "hermes_cli/plugins_manifest.py").read_text(
            encoding="utf-8", errors="replace"
        )
        match = re.search(r"^SUPPORTED_MANIFEST_VERSION\s*=\s*(\d+)", manifest_body, re.MULTILINE)
        if not match:
            return "the shared manifest-version constant moved", True
        supported = int(match.group(1))
    elif private:
        supported = int(private.group(1))
    else:
        return "the installer no longer shares the loader's manifest-version gate", True

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
    return True, True


def gemini_still_reads_an_assistant_turn_as_a_model_turn(_=None):
    """Exercise assistant-to-model conversion through the actual contents builder."""
    return _gemini_executed_contract("assistant_mapping")


def a_refusal_still_arrives_with_the_provider_words_in_it(_=None):
    """Exercise provider text and numeric status through the actual error factory."""
    return _gemini_executed_contract("refusal_fields")



def the_host_still_appends_its_own_guidance_to_a_429(_=None):
    """Exercise host guidance addition and KAME evidence separation."""
    return _gemini_executed_contract("guidance_separation")


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


def the_two_functions_the_carousel_wraps_still_exist(_=None):
    """The carousel rides unsupported surface. This makes that loud.

    ``dispatch_binding`` installs itself with ``setattr`` on
    ``agent.chat_completion_helpers``, over ``interruptible_streaming_api_call``
    and ``interruptible_api_call``. Nothing in Hermes' plugin API offers an
    alternative, and that was checked rather than assumed in 1.5.0: of the 33
    hooks in ``hermes_cli/plugins.py``'s ``VALID_HOOKS``, ``pre_llm_call``
    returns context injection only, the ``on_stream_*`` family is documented as
    observers that "cannot transform the stream", and ``ctx.llm`` is a facade
    for a plugin's *own* out-of-band calls. None of them can swap the
    credential and re-drive the request, which is the whole of what a carousel
    does. So the patch is necessary.

    What is not necessary is finding out it broke by noticing that rotation
    stopped. A rename upstream would leave KAME registered, reporting itself
    active, and choosing no keys at all -- the exact shape of the nine days
    that produced ``integrity.py``. This check turns that into a named failure
    the moment the host moves.
    """
    path = AGENT / "agent/chat_completion_helpers.py"
    if not path.is_file():
        return "chat_completion_helpers.py not found", False
    body = path.read_text(encoding="utf-8", errors="replace")
    missing = [
        name
        for name in ("interruptible_streaming_api_call", "interruptible_api_call")
        if f"def {name}" not in body
    ]
    if missing:
        return (
            "the carousel patches "
            + ", ".join(missing)
            + " and the host no longer defines it -- rotation is off until this is re-pointed",
            False,
        )
    return True, True


def the_error_hook_still_hands_over_type_and_code(_=None):
    """Why 1.5.0 stopped discarding two of the hook's arguments.

    ``error_classifier.py`` computes ``error_type`` as literally
    ``type(error).__name__`` and ``error_code`` through ``_extract_error_code``,
    then passes both to ``transform_api_error_classification``. KAME reads them
    now: the class name is the only evidence a transport failure carries, and
    the host's code extractor walks the exception's cause chain deeper than
    this plugin's own path list.

    If the host stops sending them, KAME does not break -- both default to
    empty and every other source still runs -- but it quietly loses the one
    signal that survives a payload with no status and no body, which is worth
    being told about.
    """
    path = AGENT / "agent/error_classifier.py"
    if not path.is_file():
        return "error_classifier.py not found", True
    body = path.read_text(encoding="utf-8", errors="replace")
    if "type(error).__name__" not in body:
        return "error_type is no longer the exception's class name", True
    if "_extract_error_code" not in body:
        return "the host no longer derives error_code", True
    return True, True



def the_error_factory_is_still_one_function(_=None):
    """Exercise both real client entrypoints with an unread simulated HTTP error body."""
    return _gemini_executed_contract("factory_paths")


CHECKS = (
    ("agent/conversation_loop.py", "an empty answer is retried on the same key", the_empty_retry_never_asks_the_pool),
    ("agent/agent_runtime_helpers.py", "primary restore selects once when needed, not every request", the_only_outside_selection_is_per_turn),
    ("run_agent.py", "credential swap updates the active key and client inputs", the_key_only_changes_on_the_error_path),
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
    ("agent/conversation_loop.py", "a cut answer appends a tagged continuation row (UI visibility separate)", a_cut_answer_still_appends_a_row_the_client_never_sees),
    # v1.1.1. The three facts the stream seam rests on: the drop is a return
    # value, the one cut that must not be continued is still tagged apart, and
    # the read timeout is still read per call.
    ("agent/chat_completion_helpers.py", "a mid-stream drop is returned, not raised", a_mid_stream_drop_is_returned_and_not_raised),
    ("agent/chat_completion_helpers.py", "a tool-argument drop is still tagged apart", a_tool_argument_drop_is_still_tagged_apart),
    ("agent/chat_completion_helpers.py", "the stream read timeout is read inside the call", the_stream_read_timeout_is_read_inside_the_call),
    ("agent/chat_completion_helpers.py", "the stream worker inherits the caller's context", the_stream_worker_inherits_the_callers_context),
    # v1.4.0. The three facts behind reading evidence off the exception: the
    # guidance the host appends and KAME has to take back off, the fields the
    # error carries, and the one member of `details` the host drops.
    ("agent/gemini_native_adapter.py", "the host still appends its own guidance to a 429", the_host_still_appends_its_own_guidance_to_a_429),
    ("agent/gemini_native_adapter.py", "the provider error still carries its evidence", the_provider_error_still_carries_its_evidence),
    ("agent/gemini_native_adapter.py", "the host still discards RetryInfo", the_host_still_discards_retry_info),
    # v1.5.0. The surface the carousel rides on, and the two hook arguments
    # this release stopped throwing away.
    ("agent/chat_completion_helpers.py", "the two functions the carousel wraps still exist", the_two_functions_the_carousel_wraps_still_exist),
    ("agent/error_classifier.py", "the hook still hands over error_type and error_code", the_error_hook_still_hands_over_type_and_code),
    # v1.7.0.1. The one function `quota_id_binding` wraps, and the argument
    # the streaming path hands it -- the two facts that let the quota window
    # be read at all.
    ("agent/gemini_native_adapter.py", "the error factory is still one wrappable function", the_error_factory_is_still_one_function),
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
    # The migrated check executes a deliberately broken callback, rather than
    # deleting a string from a source file the current host no longer uses.
    _runtime_executed_contract("success_fields")
    check(
        "a host that stopped sending the counts would be caught",
        (_runtime_contract_report or {}).get("mutation_detected", {}).get("success_fields") is True,
        True,
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
    print("all current host contracts passed; see HOST_ALERTS.md for scope and retained limitations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
