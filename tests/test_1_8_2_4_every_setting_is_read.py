"""1.8.2.4: every setting in the registry has a real reader — not a comment.

The 1.8.2.3 meta-test checked a hand-written list of eleven settings, and a
setting counted as "read" if its name appeared anywhere in the source text,
comments included. ``pacing_interval_seconds`` was shown on the panel, saved,
reset and documented, and nothing read it.

This walks the whole registry (flags, numbers, strings) and parses the plugin
with ``ast``: a setting is read when some module other than ``settings.py``
names its constant as an attribute (``settings.HEDGE_MODE``) or carries its
key as a string literal in code (``_setting("hedge_budget_pct", ...)``), or
the desktop panel reads it by key. Comments and docstrings never count.

A setting kept only so old configurations still load is listed in RETIRED,
and must say so in its own title.

Run:  uv run --with pytest python -m pytest tests/test_1_8_2_4_every_setting_is_read.py -q
"""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hermes-kame-api-rotation"))

import settings  # noqa: E402

SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hermes-kame-api-rotation"))
PANEL = os.path.join(SRC_ROOT, "desktop", "plugin.js")

#: Kept so an older config that sets them still loads; read by nothing.
RETIRED = {settings.PACING_INTERVAL}


def _code_names_and_strings():
    """Attribute names and string literals used in code (docstrings excluded)."""
    attrs, strings = set(), set()
    for root, dirs, files in os.walk(SRC_ROOT):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if not name.endswith(".py") or name == "settings.py" and root == SRC_ROOT:
                continue
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    body = getattr(node, "body", None) or []
                    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                        docstrings.add(id(body[0].value))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    attrs.add(node.attr)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    strings.add(node.value)
    return attrs, strings


def _constant_names(key):
    return {n for n, v in vars(settings).items() if n.isupper() and v == key}


def _settings_accessors():
    """``{constant name: {settings.py functions whose code names it}}``.

    Some settings are read through a helper in ``settings.py`` itself
    (``total_wait_bound`` reads ``MAX_TOTAL_WAIT``); such a setting is read
    when the helper is called from the plugin.
    """
    with open(os.path.join(SRC_ROOT, "settings.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out = {}
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and node.id.isupper():
                out.setdefault(node.id, set()).add(fn.name)
    return out


class TestEverySettingIsRead(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.attrs, cls.strings = _code_names_and_strings()
        with open(PANEL, encoding="utf-8") as fh:
            cls.panel = fh.read()
        cls.everything = list(settings.ALL_FLAGS) + list(settings.ALL_NUMBERS) + list(settings.ALL_STRINGS)
        cls.accessors = _settings_accessors()

    def test_every_setting_has_a_reader(self):
        # Generic readers (``number``, ``is_on``, ``text_value``…) mention no
        # constant by name, so they never make a setting count as read.
        unread = []
        for key in self.everything:
            if key in RETIRED:
                continue
            consts = _constant_names(key)
            helpers = set().union(*(self.accessors.get(c, set()) for c in consts)) if consts else set()
            by_constant = bool(consts & self.attrs) or bool(helpers & self.attrs)
            by_string = key in self.strings
            by_panel = f"'{key}'" in self.panel or f'"{key}"' in self.panel
            if not (by_constant or by_string or by_panel):
                unread.append(key)
        self.assertEqual(unread, [], f"settings nothing reads: {unread}")

    def test_retired_settings_say_so(self):
        for key in RETIRED:
            self.assertIn("no longer used", settings.title(key).lower(), key)

    def test_no_double_percent_reaches_the_panel(self):
        # The panel renders META text as written; a "%%" shows as two signs.
        for key in self.everything:
            for text in (settings.title(key), settings.explain(key), settings.recommendation(key)):
                self.assertNotIn("%%", text, key)


if __name__ == "__main__":
    unittest.main()
