"""1.8.2.9 / decisions/0032 A6 - the journal can say how much of a model went to background jobs.

Before this, a row carried no channel or task, so "share of the best model's requests spent on side
jobs" could not be computed at all. Rows of the chat read exactly as they always did; only background
rows gain two keys; the reader of the first-word history ignores them (a title's first word is not the
chat's and must not seed the backup trigger).
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_aux_measure_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
timings = importlib.import_module(f"{PACKAGE}.timings")


@pytest.fixture()
def journal(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(__import__("os").environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    timings.forget()
    path = timings._destination()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    yield Path(path)
    timings.forget()


def rows_of(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def write(**kw):
    base = dict(identity="gemini:gemini-3.8-flash", key_fingerprint="key:aa", attempt=1, outcome="answered",
                kind="", status_code=200, started_at=1.0, ended_at=3.0, first_sign_at=2.0, first_text_at=2.5,
                waited_before_s=0.0, pool_waited_before_s=0.0, call_id="c1", chars_seen=10)
    base.update(kw)
    timings.record(**base)


def test_a_chat_row_reads_exactly_as_it_always_did(journal):
    write()
    row = rows_of(journal)[0]
    assert "channel" not in row and "task" not in row


def test_a_background_row_names_its_channel_and_task(journal):
    write(channel="secondary", task="title_generation")
    row = rows_of(journal)[0]
    assert row["channel"] == "secondary" and row["task"] == "title_generation"


def test_a_long_task_name_is_cut(journal):
    write(channel="secondary", task="x" * 200)
    assert len(rows_of(journal)[0]["task"]) <= 40


def test_background_rows_never_seed_the_first_word_history(journal):
    write(channel="secondary", task="title_generation", first_sign_at=9.0, first_text_at=9.5, call_id="b")
    write(call_id="m", first_sign_at=2.0, first_text_at=2.2)
    history = timings.first_word_history()
    assert history == {"gemini:gemini-3.8-flash": [pytest.approx(1.0)]}, "only the chat's 1.0 s counts"


def test_old_rows_without_the_field_are_read_as_before(journal):
    old = {"at": round(time.time(), 3), "identity": "gemini:gemini-3.6-flash", "key": "key:aa", "attempt": 1,
           "outcome": "answered", "kind": "", "status": 200, "ms_total": 3000, "ms_to_first_sign": 1500,
           "ms_to_first_text": 2000, "ms_waited_before": 0, "chars_seen": 5, "rest_s": None}
    journal.write_text(json.dumps(old) + "\n", encoding="utf-8")
    assert timings.first_word_history() == {"gemini:gemini-3.6-flash": [pytest.approx(1.5)]}


def _speed(path):
    out = subprocess.run([sys.executable, "-X", "utf8", "-I", str(ROOT / "tools" / "speed.py"), str(path)],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_speed_reads_a_1_8_2_8_file_and_adds_no_section_for_it(tmp_path):
    old = {"at": round(time.time() - 60, 3), "identity": "gemini:gemini-3.6-flash", "key": "key:aa", "attempt": 1,
           "outcome": "answered", "kind": "", "status": 200, "ms_total": 3000, "ms_to_first_sign": 1500,
           "ms_to_first_text": 2000, "ms_waited_before": 0, "chars_seen": 5, "rest_s": None}
    path = tmp_path / "calls.jsonl"
    path.write_text(json.dumps(old) + "\n", encoding="utf-8")
    text = _speed(path)
    assert "VELOCIDADE" in text and "ESPERA REAL" in text and "O CORTE" in text
    assert "TRABALHO DE FUNDO" not in text


def test_speed_adds_the_background_section_when_there_are_background_rows(tmp_path):
    now = time.time() - 120
    rows = []
    for i in range(6):
        rows.append({"at": now + i, "identity": "gemini:gemini-3.8-flash", "outcome": "answered", "kind": "",
                     "ms_total": 3000, "ms_to_first_text": 2000, "ms_to_first_sign": 1500})
    for i in range(4):
        rows.append({"at": now + 10 + i, "identity": "gemini:gemini-3.8-flash", "outcome": "rotate",
                     "kind": "rate_limit", "ms_total": 500, "channel": "secondary", "task": "title_generation"})
    path = tmp_path / "calls.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    text = _speed(path)
    assert "TRABALHO DE FUNDO" in text
    section = text.split("TRABALHO DE FUNDO", 1)[1]
    assert "gemini:gemini-3.8-flash" in section and "title_generation" in section
    assert "40%" in section, "4 of the 10 recorded attempts on that model were background jobs"
