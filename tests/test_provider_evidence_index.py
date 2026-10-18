"""Evidence navigation must preserve scope and resolve actual file lines."""
import json
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_index_keeps_every_registered_provider_without_certifying_mentions():
    module = runpy.run_path(str(ROOT / "tools/provider_evidence_index.py"))
    data = module["build"]()
    inventory = json.loads((ROOT / "research/1.7.0.7/reconciled-provider-inventory.json").read_text(encoding="utf-8"))
    assert [r["provider"] for r in data["hermes"]] == [r["provider"] for r in inventory["hermes"]]
    for row in data["hermes"]:
        assert row["error_taxonomy_acceptance"] == "not certified by this index"
        for ref in row["note_mentions"] + row["test_mentions"]:
            lines = (ROOT / ref["path"]).read_text(encoding="utf-8").splitlines()
            assert 1 <= ref["line"] <= len(lines)
            assert row["provider"].lower() in lines[ref["line"]-1].lower()
    assert all(r["implementation"] == "out of scope until owner approval" for r in data["agent_zero_reference"])
