"""1.8.2.7: a Settings page a first-time user can read, and the author's name on it.

* Every setting carries a weight (`impact`) and a one-line "when to change it";
  the few big ones lead the page, the escape hatches are folded.
* The 1.8.2.3 panel called a `StringSetting` component that was never written,
  so the Settings tab threw at the first text setting. A source check below
  now fails for *any* component used and not defined, and the page is
  rendered end to end with the real settings list.
* The plugin is credited to KAME (github.com/Kame696), with a star button;
  no donation wallet and no AI-authorship wording ship in either port.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "hermes-kame-api-rotation"
A0 = ROOT.parent / "agent-zero" / "current"
PLUGIN_JS = SRC / "desktop" / "plugin.js"

sys.path.insert(0, str(SRC))
import importlib
import importlib.util as _ilu

if "hermes_kame_api_rotation" not in sys.modules:
    _spec = _ilu.spec_from_file_location("hermes_kame_api_rotation", SRC / "__init__.py",
                                         submodule_search_locations=[str(SRC)])
    _pkg = _ilu.module_from_spec(_spec)
    sys.modules["hermes_kame_api_rotation"] = _pkg
    _spec.loader.exec_module(_pkg)
settings = importlib.import_module("hermes_kame_api_rotation.settings")

UI = PLUGIN_JS.read_text(encoding="utf-8")


class TestEverySettingSaysHowMuchItMatters(unittest.TestCase):
    def test_every_setting_has_a_weight_and_a_when(self):
        for row in settings.describe_all():
            self.assertIn(row["impact"], {"high", "medium", "low", "escape", "retired"}, row["key"])
            self.assertTrue(row["when"], row["key"])

    def test_the_big_ones_are_the_ones_a_user_actually_chooses(self):
        high = {row["key"] for row in settings.describe_all() if row["impact"] == "high"}
        # 1.8.2.7 (decisions/0027): the switch time matters; "ignore my list" is advanced now
        # 1.8.2.8: the choice is what a user decides; its number is "medium"
        self.assertEqual(high, {settings.HEDGE_MODE, settings.MODEL_SWITCH, settings.MAX_TOTAL_WAIT})

    def test_every_escape_hatch_is_marked_as_one(self):
        for key in settings.DISABLE_FLAGS:
            self.assertEqual(settings.impact(key), "escape", key)

    def test_the_retired_setting_says_so(self):
        self.assertEqual(settings.impact(settings.PACING_INTERVAL), "retired")

    def test_only_the_escape_shelf_is_folded(self):
        folded = [g["id"] for g in settings.groups() if g["folded"]]
        self.assertEqual(folded, ["off"])


class TestThePanelUsesOnlyWhatItDefines(unittest.TestCase):
    def test_every_component_rendered_is_defined_or_imported(self):
        used = set(re.findall(r"\bh\(\s*([A-Z][A-Za-z0-9]*)\s*,", UI))
        defined = set(re.findall(r"^(?:async\s+)?function\s+([A-Z][A-Za-z0-9]*)\s*\(", UI, re.M))
        defined |= set(re.findall(r"^const\s+([A-Z][A-Za-z0-9]*)\s*=", UI, re.M))
        imported_block = UI.split("from '@hermes/plugin-sdk'", 1)[0]
        defined |= set(re.findall(r"\b([A-Z][A-Za-z0-9]*)\b", imported_block))
        self.assertEqual(sorted(used - defined), [])

    def test_string_settings_have_a_control(self):
        self.assertIn("function StringSetting(", UI)

    def test_the_panel_never_names_a_shelf(self):
        # The shelf a setting sits on, and whether it folds, come from
        # settings.groups() — the JavaScript does not keep its own copy.
        for group in settings.groups():
            self.assertNotIn(f"'{group['id']}'", UI.replace("'_rest'", "").replace("'_top'", ""))

    def test_events_lead_to_the_background_jobs(self):
        events_page = UI.split("function EventsPage", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("$tab.set('secondary')", events_page)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_the_settings_tab_renders_with_the_real_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = {"settings": list(settings.describe_all()), "groups": list(settings.groups())}
            src = os.path.join(tmp, "settings.json")
            out = os.path.join(tmp, "settings.html")
            with open(src, "w", encoding="utf-8") as fh:
                json.dump(data, fh, default=str)
            done = subprocess.run(
                ["node", str(ROOT / "tools" / "panel_preview.mjs"), "--settings", src, "--tab", "settings", "--out", out],
                capture_output=True, text=True, cwd=str(ROOT),
            )
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            with open(out, encoding="utf-8") as fh:
                html = fh.read()
        for text in ("Everything works without changing anything here", "High", "When to change it", "Made by KAME", "Star on GitHub"):
            self.assertIn(text, html)
        # Folded unless one of them is already changed in this environment.
        self.assertTrue("Show the" in html or "Hide these switches" in html)


class TestTheAuthorIsCredited(unittest.TestCase):
    def test_panel_names_kame_and_links_the_profile_and_repo(self):
        self.assertIn("const AUTHOR = 'KAME'", UI)
        self.assertIn("https://github.com/Kame696", UI)
        self.assertIn("https://github.com/Kame696/kame-api-rotation-for-hermes", UI)

    def test_links_open_through_hermes_own_door_never_by_themselves(self):
        self.assertIn("os.openExternal(url)", UI)
        # Only a click opens anything: openLink is called from onClick handlers.
        calls = re.findall(r"openLink\(", UI)
        on_click = re.findall(r"onClick: \(\) => openLink\(", UI)
        self.assertEqual(len(calls) - 1, len(on_click))  # minus the definition

    def test_license_and_manifests_name_the_author(self):
        self.assertIn("Kame696", (ROOT / "LICENSE").read_text(encoding="utf-8"))
        self.assertIn('author: "Kame696"', (SRC / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertIn("Kame696", (ROOT / "hermes-kame-provider" / "plugin.yaml").read_text(encoding="utf-8"))


def _shipped_texts():
    roots = [SRC, ROOT / "hermes-kame-provider"]
    files = [ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "LICENSE"]
    if A0.is_dir():
        roots.append(A0)
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".js", ".md", ".yaml", ".yml", ".json", ".html"} \
                    and "__pycache__" not in path.parts and "tests" not in path.parts:
                files.append(path)
    for path in files:
        yield path, path.read_text(encoding="utf-8", errors="replace")


class TestNothingElseShips(unittest.TestCase):
    def test_no_donation_or_wallet(self):
        pattern = re.compile(r"bitcoin|\bBTC\b|donat|ko-fi|buymeacoffee|paypal\.me", re.I)
        hits = [str(p.relative_to(ROOT.parent)) for p, text in _shipped_texts() if pattern.search(text)]
        self.assertEqual(hits, [])

    def test_no_funding_button(self):
        self.assertFalse((ROOT / ".github" / "FUNDING.yml").exists())
        self.assertFalse((A0 / ".github" / "FUNDING.yml").exists())

    def test_no_ai_authorship_wording(self):
        pattern = re.compile(r"co-authored-by|generated (with|by) (an )?ai|written by (an )?ai", re.I)
        hits = []
        for path, text in _shipped_texts():
            for match in pattern.finditer(text):
                line = text[max(0, match.start() - 40): match.end() + 40]
                # A model or provider *name* is not authorship: "anthropic:claude…"
                # identities, and the Agent Zero alias table that lets a user
                # type "claude" for the anthropic provider.
                if "anthropic:claude" in line.lower() or '"claude"' in line.lower():
                    continue
                hits.append(f"{path.name}: {line.strip()[:90]}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
