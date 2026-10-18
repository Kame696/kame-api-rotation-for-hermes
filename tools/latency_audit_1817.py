"""Read-only production latency audit. Output contains metadata, never content."""
from __future__ import annotations
import argparse
import collections
import datetime as dt
import json
import statistics
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home() / 'AppData/Local/hermes'


def summarize(rows, start, end, pid, identity):
    selected = []
    excluded = collections.Counter()
    for line, row in rows:
        if not start <= row.get('at', 0) <= end:
            excluded['outside_interval'] += 1
            continue
        if row.get('identity') != identity:
            excluded['other_identity'] += 1
            continue
        if not row.get('call_id', '').startswith(f'{pid}-'):
            excluded['other_process'] += 1
            continue
        selected.append((line, row))
    grouped = collections.defaultdict(list)
    for line, row in selected:
        grouped[row['call_id']].append((line, row))
    calls, gaps = [], []
    for cid, attempts in grouped.items():
        attempts.sort(key=lambda pair: pair[1]['attempt'])
        last = attempts[-1][1]
        begin = int(cid.split('-', 1)[1]) / 1e9
        finish = max(row['at'] for _, row in attempts)
        for (pn, p), (qn, q) in zip(attempts, attempts[1:]):
            if q['attempt'] == p['attempt'] + 1:
                gap = q['ms_elapsed_before'] - p['ms_elapsed_before'] - p['ms_total']
                gaps.append({'ms': gap, 'previous_line': pn, 'next_line': qn})
        calls.append({
            'call_id': cid, 'start_at': begin, 'end_at': finish,
            'start_local': dt.datetime.fromtimestamp(begin).isoformat(timespec='milliseconds'),
            'end_local': dt.datetime.fromtimestamp(finish).isoformat(timespec='milliseconds'),
            'observed_elapsed_ms': last['ms_elapsed_before'] + last['ms_total'],
            'final_outcome': last['outcome'],
            'complete': last['outcome'] in ('answered', 'raise'),
            'pool_wait_ms': max(row.get('ms_pool_waited_before', 0) for _, row in attempts),
            'lines': [n for n, _ in attempts],
            'attempts': [{k: row.get(k) for k in (
                'attempt', 'outcome', 'kind', 'status', 'ms_total', 'ms_to_first_sign',
                'ms_to_first_text', 'ms_elapsed_before', 'ms_pool_waited_before',
                'rest_s', 'rest_source')} for _, row in attempts],
        })
    calls.sort(key=lambda row: row['start_at'])
    for call in calls:
        parents = [p for p in calls if p is not call and p['complete'] and
                   p['start_at'] < call['start_at'] and p['end_at'] >= call['end_at']]
        # Temporal containment is a diagnostic lead, not proof of thread parentage.
        call['temporally_enclosed_by'] = [p['call_id'] for p in parents]
    values = [row['ms'] for row in gaps]
    return {
        'interval_start': start, 'interval_end': end, 'pid': pid, 'identity': identity,
        'attempt_rows': len(selected), 'call_count': len(calls),
        'outcomes': dict(collections.Counter(row['outcome'] for _, row in selected)),
        'statuses': dict(collections.Counter(str(row.get('status')) for _, row in selected
                                            if row.get('status') is not None)),
        'excluded': dict(excluded),
        'rotation_gap_ms': {'count': len(values), 'min': min(values) if values else None,
                            'median': statistics.median(values) if values else None,
                            'max': max(values) if values else None},
        'rotation_gaps': gaps, 'calls': calls,
        'scope': 'attempts, not user turns; ms_total includes host rescue; no summing nested calls',
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--start', required=True, help='Local ISO datetime')
    parser.add_argument('--end', help='Optional local ISO datetime; defaults to now')
    parser.add_argument('--identity', default='gemini:gemini-3.8-flash')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    if not out.is_relative_to(ROOT):
        raise ValueError('Output must stay inside the active project')
    source = HOME / 'plugin-data/hermes-kame-api-rotation/calls.jsonl'
    rows = []
    for n, line in enumerate(source.read_text(encoding='utf-8').splitlines(), 1):
        try:
            rows.append((n, json.loads(line)))
        except ValueError:
            continue
    end = dt.datetime.fromisoformat(args.end).timestamp() if args.end else time.time()
    result = summarize(rows, dt.datetime.fromisoformat(args.start).timestamp(),
                       end, args.pid, args.identity)
    result['source'] = str(source)
    state = json.loads((source.parent / 'state.json').read_text(encoding='utf-8'))
    result['loaded_processes'] = [{k: p.get(k) for k in (
        'pid', 'role', 'profile', 'version', 'build', 'installed', 'reason',
        'updated_at', 'counters')} for p in state.get('processes', {}).values()]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in (
        'attempt_rows', 'call_count', 'outcomes', 'statuses', 'rotation_gap_ms', 'scope')}))


if __name__ == '__main__':
    main()
