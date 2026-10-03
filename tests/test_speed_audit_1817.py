"""Regression: previous failed API attempts are not pool waiting."""
import importlib.util
import io
import json
import time
from pathlib import Path

p = Path(__file__).resolve().parents[1] / 'tools/speed.py'
s = importlib.util.spec_from_file_location('speed_audit', p)
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)


def rendered(monkeypatch, rows):
    buf = io.BytesIO()
    monkeypatch.setattr(m, 'out', buf)
    m.report(rows)
    return buf.getvalue().decode('utf-8')


def test_failed_provider_time_is_not_rotation_wait(monkeypatch):
    r = {'at': time.time(), 'identity': 'gemini:model', 'outcome': 'answered',
         'ms_waited_before': 194390, 'ms_elapsed_before': 194390,
         'ms_total': 29783, 'ms_pool_waited_before': 0}
    text = rendered(monkeypatch, [r])
    assert '0.00s' in text and '224.17s' in text
    assert '87%' not in text
    assert 'host/provedor; inclui resgates' in text
    assert 'nao somar seus tempos' in text


def test_legacy_elapsed_does_not_invent_pool_measurement(monkeypatch):
    r = {'at': time.time(), 'identity': 'gemini:model', 'outcome': 'answered',
         'ms_waited_before': 194390, 'ms_total': 29783}
    assert 'atribuicao desconhecida' in rendered(monkeypatch, [r])


def test_future_and_sim_rows_are_excluded(tmp_path):
    p = tmp_path / 'calls.jsonl'
    rows = [{'at': time.time(), 'identity': 'gemini:model'},
            {'at': time.time()+3600, 'identity': 'gemini:model'},
            {'at': time.time(), 'identity': 'sim:gate-model'}]
    p.write_text('\n'.join(json.dumps(r) for r in rows))
    assert m.load(p, None) == [rows[0]]
