"""Audit correctness is separate from plugin performance correctness."""
import importlib.util
from pathlib import Path

p = Path(__file__).resolve().parents[1] / 'tools/latency_audit_1817.py'
s = importlib.util.spec_from_file_location('latency_audit', p)
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)


def row(at, attempt, elapsed, total, outcome='answered', cid='9-100000000000'):
    return {'at': at, 'attempt': attempt, 'ms_elapsed_before': elapsed,
            'ms_total': total, 'outcome': outcome, 'identity': 'gemini:model',
            'call_id': cid, 'ms_pool_waited_before': 0}


def test_future_and_other_identity_are_not_production():
    a = row(101, 1, 0, 1000)
    b = dict(a, at=1800000000)
    c = dict(a, identity='sim:gate-model')
    r = m.summarize([(1, a), (2, b), (3, c)], 100, 110, 9, 'gemini:model')
    assert r['attempt_rows'] == 1
    assert r['excluded'] == {'outside_interval': 1, 'other_identity': 1}


def test_switch_gap_excludes_prior_request_duration():
    r = m.summarize([(1, row(101, 1, 10, 1000, 'rotate')),
                     (2, row(103, 2, 1028, 1972))], 100, 110, 9, 'gemini:model')
    assert r['rotation_gap_ms']['median'] == 18
    assert r['calls'][0]['observed_elapsed_ms'] == 3000


def test_cumulative_pool_wait_is_not_added_per_attempt():
    a = dict(row(101, 1, 1000, 1000, 'rotate'), ms_pool_waited_before=1000)
    b = dict(row(103, 2, 2500, 500), ms_pool_waited_before=1500)
    r = m.summarize([(1, a), (2, b)], 100, 110, 9, 'gemini:model')
    assert r['calls'][0]['pool_wait_ms'] == 1500


def test_temporal_nesting_is_labeled_not_summed():
    a = row(105, 1, 0, 5000)
    b = row(104, 1, 0, 2000, cid='9-102000000000')
    r = m.summarize([(1, a), (2, b)], 100, 110, 9, 'gemini:model')
    assert r['calls'][1]['temporally_enclosed_by'] == [a['call_id']]
    assert r['calls'][0]['observed_elapsed_ms'] == 5000


def test_incomplete_rotation_is_not_completed_answer():
    r = m.summarize([(1, row(101, 1, 0, 1000, 'rotate'))], 100, 110, 9, 'gemini:model')
    assert r['calls'][0]['complete'] is False
