"""The human study and expandable machine export must not silently drift."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_export_matches_current_rule_vocabulary():
    spec = importlib.util.spec_from_file_location("kame_catalogue_export", ROOT / "tools/export_error_catalogue.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stored = json.loads((ROOT / "knowledge_base/error-catalogue.json").read_text(encoding="utf-8"))
    assert stored == module.export()
    assert len(stored["exact_names"]) > 20
    assert module.catalog.look_up("provider_specific_unknown_case_706") is None
