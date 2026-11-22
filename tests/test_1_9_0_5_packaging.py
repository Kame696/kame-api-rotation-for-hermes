"""1.9.0.5 (A5, H) - one package ships; the version and the words agree everywhere."""
from __future__ import annotations

import importlib.util
import re
import zipfile

from tests.h1905 import ROOT, PLUGIN_DIR

VERSION = "1.9.0.5"
DESCRIPTION = ("Rotates many API keys per provider so rate limits and quota errors never stop your agent: the "
               "healthiest key for every call, the next model when one is busy, and a live line in the chat "
               "showing what it did.")


def _packager():
    spec = importlib.util.spec_from_file_location("kame_packager_1905", ROOT / "tools" / "package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_build_ships_only_the_rotation_plugin_and_the_optional_bridge():
    pk = _packager()
    assert [p.name for p in pk.SOURCES] == ["hermes-kame-api-rotation"]
    assert [p.name for p in pk.OPTIONAL] == ["hermes-kame-bridge"]
    assert (ROOT / "hermes-kame-provider" / "plugin.yaml").exists()      # kept in the repo, not shipped


def test_the_all_in_one_has_no_provider_package(tmp_path, monkeypatch):
    pk = _packager()
    monkeypatch.setattr(pk, "DIST", tmp_path)
    assert pk.build_all_in_one(pk.SOURCES + pk.OPTIONAL) == 0
    archive = tmp_path / f"KAME-for-Hermes-{VERSION}-all-in-one.zip"
    roots = {n.split("/", 1)[0] for n in zipfile.ZipFile(archive).namelist()}
    assert roots == {"hermes-kame-api-rotation", "hermes-kame-bridge"}


def test_the_version_is_the_same_everywhere():
    manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    assert re.search(rf'^version: "{re.escape(VERSION)}"$', manifest, re.M)
    core = (PLUGIN_DIR / "core" / "__init__.py").read_text(encoding="utf-8")
    assert f'__version__ = "{VERSION}"' in core
    assert f'version: "{VERSION}"' in (ROOT / "hermes-kame-bridge" / "plugin.yaml").read_text(encoding="utf-8")
    assert f"badge/version-{VERSION}-blue" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"## [{VERSION}]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_the_description_is_the_planned_sentence():
    manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    assert f'description: "{DESCRIPTION}"' in manifest


def test_the_catalog_entry_is_one_entry_with_the_same_words():
    entry = ROOT / "deliverables" / VERSION / "catalog-entry.yaml"
    if not entry.is_file():
        import pytest
        pytest.skip("the catalog entry draft lives in the development tree only")
    text = entry.read_text(encoding="utf-8")
    assert len(re.findall(r"^name:", text, re.M)) == 1 and "hermes-kame-provider" not in text
    assert f'description: "{DESCRIPTION}"' in text
    assert f'version: "{VERSION}"' in text
    assert re.search(r"^sha: TBD-at-release$", text, re.M)


def test_the_readme_has_the_keys_tutorial_in_the_panels_words():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    panel = (PLUGIN_DIR / "desktop" / "plugin.js").read_text(encoding="utf-8")
    assert "What's new in 1.9.0.5" in readme
    for words in ("Paste all your keys, separated by commas, into the key field of that provider in Hermes settings.",
                  "API Rotation checks them."):
        assert words in readme and words in panel
