"""Does KAME still recognise the text the HOST adds to a provider's error?

The other gates ask whether KAME is right about a payload. This one asks a
question none of them can: **is the payload still the payload?**

Hermes does not always hand a hook the provider's message. For Google it
builds the message and then appends its own advice to it
(``agent/gemini_native_adapter.py``, :907 and :913) before anything
downstream — including this plugin — ever sees it. That advice is Hermes
talking to the user about Hermes. It is not evidence, and 1.6.0.2 strips it
before a single pattern reads a word.

Stripping is anchored on the opening words of each footer, so the gate that
matters is not "is the code still there" but "do those words still start the
footer". A Hermes release that rewords the first line silently restores the
bug, and the measured cost of that bug is:

    provider said   "Please retry in 6.89161299s"
    KAME concluded  billing, account scope, one hour

because the host's own sentences "...the free tier is exhausted..." and
"...a billing-enabled project..." each match ``_BILLING_PATTERNS`` on their
own. So this runs three checks, in order of how badly they fail:

1. **Discovery.** Every constant the adapter concatenates onto an error
   message, whether or not KAME knows about it. A new footer nobody told the
   plugin about is the failure mode this whole file exists for.
2. **Recognition.** Each known footer is removed completely by
   ``strip_host_prose``.
3. **Consequence.** Each footer, on its own, is declined by ``classify``.
   That is the assertion with teeth: it is exactly what returned ``billing``
   before this release.

    python tools/host_prose.py
    KAME_HERMES_ROOT=... python tools/host_prose.py
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
sys.path.insert(0, str(PLUGIN_DIR))

from core.classify import classify, strip_host_prose  # noqa: E402

HERMES = Path(
    os.environ.get("KAME_HERMES_ROOT", Path.home() / "AppData/Local/hermes/hermes-agent")
)
ADAPTER = HERMES / "agent" / "gemini_native_adapter.py"

#: Footers this plugin has been told about. A constant discovered in the
#: adapter that is not in this tuple is a failure, not a warning: it means the
#: host started saying something new into a string KAME classifies.
KNOWN = ("_FREE_TIER_GUIDANCE", "_STANDARD_KEY_GUIDANCE")


def _string_constants(tree):
    """Every module-level ``NAME = "..."`` whose value is a plain string."""
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, str):
            out[target.id] = value
    return out


def _appended_names(tree):
    """Names used as ``<something> + NAME`` anywhere in the module.

    Deliberately broader than the two call sites known today. The question is
    not "are those two lines unchanged" but "is anything being welded onto an
    error message", and a narrow search answers the wrong one.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            for side in (node.left, node.right):
                if isinstance(side, ast.Name) and side.id.isupper() or (
                    isinstance(side, ast.Name) and side.id.startswith("_")
                ):
                    names.add(side.id)
    return names


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover
        pass

    if not ADAPTER.is_file():
        print(f"host adapter not found: {ADAPTER}")
        print("set KAME_HERMES_ROOT to the hermes-agent directory")
        return 2

    tree = ast.parse(ADAPTER.read_text(encoding="utf-8", errors="replace"))
    constants = _string_constants(tree)
    appended = _appended_names(tree)

    # 1. discovery ---------------------------------------------------------
    found = sorted(n for n in appended if n in constants and len(constants[n]) > 80)
    print(f"\n[1] prose the adapter welds onto a message  ({ADAPTER.name})")
    unknown = [n for n in found if n not in KNOWN]
    for name in found:
        mark = "  " if name in KNOWN else "??"
        print(f"    {mark} {name}  ({len(constants[name])} chars)")
    missing = [n for n in KNOWN if n not in constants]
    for name in missing:
        print(f"    !! {name} is gone from the host")

    problems = len(unknown) + len(missing)
    if unknown:
        print("\n    A constant KAME has not been told about is appended to an")
        print("    error message. Until it is stripped, every pattern in")
        print("    core/classify.py reads the host's opinion as the provider's.")

    # 2. recognition -------------------------------------------------------
    print("\n[2] does strip_host_prose still remove them?")
    for name in KNOWN:
        text = constants.get(name)
        if text is None:
            continue
        probe = "Gemini HTTP 429 (RESOURCE_EXHAUSTED): quota." + text
        left = strip_host_prose(probe)
        ok = left == "Gemini HTTP 429 (RESOURCE_EXHAUSTED): quota."
        print(f"    {'ok' if ok else 'NO'}  {name}")
        if not ok:
            problems += 1
            print(f"        {len(left)} chars survived; the anchor no longer matches")
            print(f"        host now opens with: {text.strip()[:72]!r}")

    # 3. consequence -------------------------------------------------------
    print("\n[3] classified alone, does each footer still claim nothing?")
    for name in KNOWN:
        text = constants.get(name)
        if text is None:
            continue
        verdict = classify(
            provider="gemini", model="gemini-3.7-flash", status_code=500,
            error_message="Internal error." + text, now_epoch=1_000_000.0,
        )
        ok = verdict is None
        print(f"    {'ok' if ok else 'NO'}  {name}"
              + ("" if ok else f"  -> {verdict.reason}"))
        if not ok:
            problems += 1

    print()
    if problems:
        print(f"{problems} problem(s). The host's voice is reaching the classifier.")
        return 1
    print("the host's advice is removed before classification, and claims nothing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
