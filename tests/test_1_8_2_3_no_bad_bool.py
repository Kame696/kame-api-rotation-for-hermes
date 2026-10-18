"""Regression: bool(x, kw=...) is a TypeError at runtime.

Born 2026-10-12 in the wild: a mechanical edit put ``call=call`` inside a
``bool()`` on the streaming failure path, so every mid-stream provider error
on a real Gemini pool raised ``bool() takes no keyword arguments`` and cut
the agent mid-answer. This gate parses every module with ``ast`` so no edit
ever reintroduces the shape, string-tricks or not.
"""
import ast
import os
import unittest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hermes-kame-api-rotation"))


def _iter_calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


class TestNoKeywordBool(unittest.TestCase):
    def test_no_bool_with_kwargs_anywhere(self):
        offenders = []
        for root, dirs, files in os.walk(_SRC):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), filename=path)
                for call in _iter_calls(tree):
                    if isinstance(call.func, ast.Name) and call.func.id == "bool":
                        if call.keywords or len(call.args) > 1:
                            offenders.append(f"{name}:{call.lineno}")
        self.assertEqual(offenders, [], f"bool() called with kwargs/extra args: {offenders}")


if __name__ == "__main__":
    unittest.main()
