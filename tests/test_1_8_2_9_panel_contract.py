"""1.8.2.9 (decisions/0031 2.9): the rotation page and the snapshot agree, without a browser.

``desktop/plugin.js`` lists every snapshot path the new page reads (``PANEL_READS``). This checks
that list against REAL snapshots (``tools/panel_fixtures.py`` drives ``state.snapshot()`` with a
scripted pool), and against the file itself, so a field the Python side stops sending - or one the
page starts reading without declaring - fails here instead of drawing an empty ring on the
owner's screen.

Run:  uv run --with pytest --with pyyaml python -m pytest tests/test_1_8_2_9_panel_contract.py -q
"""
from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JS = (ROOT / "hermes-kame-api-rotation" / "desktop" / "plugin.js").read_text(encoding="utf-8")


def reads():
    body = re.search(r"const PANEL_READS = \[(.*?)\n\]", PLUGIN_JS, re.S).group(1)   # the closing bracket starts a line
    return re.findall(r"'([^']+)'", body)


@pytest.fixture(scope="module")
def snapshots():
    spec = importlib.util.spec_from_file_location("panel_fixtures_for_test", ROOT / "tools" / "panel_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    home = Path(tempfile.mkdtemp(prefix="kame-contract-"))
    loaded = module.load(home)
    out = {}
    for name in ("healthy-idle", "answering", "all-busy-circle", "out-for-today", "keys-to-replace", "first-run",
                 "all-resting", "racing", "probing", "side-jobs", "long-list"):
        out[name] = module.build(name, home, loaded)
    return out


def exists(node, path):
    """Does ``a.b[].c`` resolve in ``node``? A list segment holds when ANY element has the rest."""
    if not path:
        return True
    head, _, rest = path.partition(".")
    many = head.endswith("[]")
    name = head[:-2] if many else head
    if not isinstance(node, dict) or name not in node:
        return False
    value = node[name]
    if many:
        return isinstance(value, list) and any(exists(item, rest) for item in value)
    return exists(value, rest)


def test_every_declared_path_exists_in_a_real_snapshot(snapshots):
    missing = [path for path in reads() if not any(exists(doc, path) for doc in snapshots.values())]
    assert missing == []


def test_the_list_is_sorted_and_has_no_duplicates():
    paths = reads()
    assert paths == sorted(set(paths))


def test_the_page_reads_no_top_level_field_it_did_not_declare():
    section = PLUGIN_JS.split("// -- 1.8.2.9: the rotation page", 1)[1].split("// -- settings ----", 1)[0]
    used = set(re.findall(r"\bsnap\??\.([a-z_]+)", section))
    declared = {path.split(".")[0].replace("[]", "") for path in reads()}
    # fields read by the page for other reasons, not part of the ring/strip contract
    allowed = declared | {"build", "first_run", "installed", "settings", "updated_at"}
    assert used - allowed == set(), f"undeclared: {sorted(used - allowed)}"


def test_the_new_snapshot_fields_are_small():
    import json

    spec = importlib.util.spec_from_file_location("panel_fixtures_size", ROOT / "tools" / "panel_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    home = Path(tempfile.mkdtemp(prefix="kame-contract-size-"))
    loaded = module.load(home)
    doc = module.build("healthy-idle", home, loaded)
    ring_data = [[row["key_states"] for row in doc["pools"]], doc["speed"], [e for e in doc["fallback"]["entries"]]]
    assert len(json.dumps(ring_data)) < 8000          # 4 models x 14 keys, the design allows about 6 KB + the speed block
