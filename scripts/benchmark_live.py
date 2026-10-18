"""Run every prerecorded worker sample through the deployed live Gemini check.

Measures agreement with the curated expected decision and end-to-end latency.
Uses the public prototype Admin account and the normal API, so it exercises the
same path a judge would. Live checks are practice only and create no evidence.

Usage: python scripts/benchmark_live.py https://<service-url> [--out docs/LIVE-BENCHMARK.md]
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys

import httpx

VARIANTS = ('correct', 'incorrect', 'corrected', 'still_incorrect')


def session(base):
    client = httpx.Client(base_url=base, headers={'Origin': base}, timeout=120, follow_redirects=False)
    response = client.post('/api/login', json={'username': 'admin', 'password': 'admin123'})
    response.raise_for_status()
    return client, response.json()['csrf_token']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('base')
    parser.add_argument('--out', default='docs/LIVE-BENCHMARK.md')
    args = parser.parse_args()
    base = args.base.rstrip('/')
    health = httpx.get(base + '/api/health', timeout=30).json()
    if not health.get('provider_configured'):
        sys.exit('Live Gemini is not configured on ' + base)
    client, csrf = session(base)
    runs = client.get('/api/prototype/demos').json()
    rows = []
    for run in runs:
        client, csrf = session(base)  # one session per scenario keeps within the hourly session budget
        for variant in VARIANTS:
            response = client.post(f"/api/prototype/demos/{run['id']}/live-check", json={'variant': variant},
                                   headers={'X-CSRF-Token': csrf})
            if response.status_code == 200:
                result = response.json()
                rows.append({'scenario': run['title'], 'variant': variant, 'expected': result['expected_decision'],
                             'live': result['decision'], 'agrees': result['agrees_with_prepared'],
                             'latency_ms': result['latency_ms']})
            else:
                rows.append({'scenario': run['title'], 'variant': variant, 'expected': None, 'live': 'error',
                             'agrees': False, 'latency_ms': None, 'error': response.json().get('detail')})
            print(rows[-1], flush=True)
    completed = [row for row in rows if row['live'] != 'error']
    latencies = sorted(row['latency_ms'] for row in completed)
    agree = sum(row['agrees'] for row in completed)
    blocked_ok = sum(1 for row in completed if row['expected'] != 'passed' and row['live'] != 'passed')
    should_block = sum(1 for row in completed if row['expected'] != 'passed')
    false_pass = sum(1 for row in completed if row['expected'] != 'passed' and row['live'] == 'passed')
    summary = {
        'measured_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'service': base,
        'model': health.get('provider_model'), 'samples': len(rows), 'completed': len(completed),
        'agreement': f'{agree}/{len(completed)}', 'incorrect_samples_blocked': f'{blocked_ok}/{should_block}',
        'false_passes': false_pass,
        'latency_median_s': round(statistics.median(latencies) / 1000, 1) if latencies else None,
        'latency_p90_s': round(latencies[max(0, int(len(latencies) * 0.9) - 1)] / 1000, 1) if latencies else None,
    }
    lines = ['# Live Gemini benchmark on the prerecorded samples', '',
             'Each of the 28 synthetic worker clips (7 scenarios × 4 variants) was sent through the deployed '
             'live check: Gemini transcribes the audio and compares it with every requirement; the server derives '
             'the decision. Expected decisions are the curated labels. Synthetic English TTS audio only — this is '
             'not a field accuracy measurement.', '', '```json', json.dumps(summary, indent=2), '```', '',
             '| Scenario | Sample | Expected | Live Gemini | Agrees | Latency (s) |', '| --- | --- | --- | --- | --- | --- |']
    for row in rows:
        latency = '' if row['latency_ms'] is None else f"{row['latency_ms'] / 1000:.1f}"
        lines.append(f"| {row['scenario']} | {row['variant']} | {row['expected'] or ''} | {row['live']} | {'yes' if row['agrees'] else 'no'} | {latency} |")
    Path(args.out).write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
