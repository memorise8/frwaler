"""Offline tier transitions and single-server throughput, with quality caveats."""
import argparse
from collections import Counter
import csv
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import time

from delivery.health.results import atomic_json, utc_now
from delivery.scripts.review_structure_discovery import audit_site

TIERS = ('높음', '보통', '낮음', '불안정', '불가')


def watch(out, interval=10):
    """Sample only the owned runner process. Never launch or modify its work."""
    out = Path(out); state = json.loads((out/'runner.json').read_text()); pid = state['pid']
    execution = json.loads((out/'resume-20261012/execution.json').read_text())
    if pid != execution['pid']:
        raise ValueError('runner ownership changed before monitoring')
    target = out/'resume-20261012/resources.jsonl'
    with target.open('x') as stream:
        while True:
            state = json.loads((out/'runner.json').read_text())
            if state['pid'] != pid or state['status'] != 'running': break
            try:
                stat = (Path('/proc')/str(pid)/'stat').read_text().rsplit(') ', 1)[1].split()
            except FileNotFoundError:
                break
            sample = {'at': utc_now(), 'pid': pid, 'finished': state['finished'],
                      'cpu_seconds': (int(stat[11])+int(stat[12]))/os.sysconf('SC_CLK_TCK'),
                      'rss_bytes': int(stat[21])*os.sysconf('SC_PAGE_SIZE'),
                      'threads': int(stat[17])}
            stream.write(json.dumps(sample)+'\n'); stream.flush()
            time.sleep(interval)


def reviewed_tier(result, audit):
    # Parser failures/redirect loops are not evidence that a source is impossible.
    if audit['audited_status'] == 'unmeasurable':
        return '불가'
    raw = result['measured_tier']
    if audit['audited_status'] == 'needs_review':
        # An unrecognised custom list/JS shell is insufficient evidence, not
        # demonstrated source instability. Reserve unstable for contradictions.
        inconsistent = any(any(token in issue for token in
            ('mixed_date_order', 'repeated_identity', 'wraparound', 'changed_on_repeat'))
            for issue in audit['issues'])
        return '불안정' if inconsistent else '낮음'
    return raw


def transitions(rows):
    matrix = {old: {new: 0 for new in TIERS} for old in TIERS}
    for row in rows:
        matrix[row['initial_tier']][row['reviewed_tier']] += 1
    initial = {tier: sum(matrix[tier].values()) for tier in TIERS}
    current = {tier: sum(matrix[old][tier] for old in TIERS) for tier in TIERS}
    return {'initial_among_processed': initial, 'reviewed_tiers': current,
            'delta_among_processed': {t: current[t]-initial[t] for t in TIERS},
            'matrix': matrix, 'changed_sites': sum(r['initial_tier'] != r['reviewed_tier'] for r in rows)}


def phase_metrics(state, results):
    start = datetime.fromisoformat(state['started_at'])
    end = datetime.fromisoformat(state.get('finished_at') or utc_now())
    seconds = max(0, (end-start).total_seconds())
    responses = 0
    for result in results:
        for request in result.get('requests', []):
            if request.get('checked_at') and request.get('status') is not None:
                at = datetime.fromisoformat(request['checked_at'])
                responses += start <= at <= end
    return {'workers': state['workers'], 'status': state['status'], 'started_at': start.isoformat(),
            'ended_or_observed_at': end.isoformat(), 'wall_seconds': seconds,
            'scheduled': state['scheduled'], 'finished': state['finished'],
            'sites_per_minute': round(state['finished']*60/seconds, 3) if seconds else None,
            'responses_received_for_completed_sites': responses,
            'responses_per_second': round(responses/seconds, 3) if seconds else None,
            'note': 'HTTP responses include errors; DNS/network failures and in-flight sites are excluded. Different site mixes prevent a controlled speedup comparison.'}


def build(out):
    out = Path(out)
    manifest = json.loads((out/'manifest.json').read_text())
    results, rows = [], []
    for source in manifest['sites']:
        path = out/'sites'/(source['site_id']+'.json')
        if not path.exists():
            continue
        result = json.loads(path.read_text()); results.append(result)
        audit = audit_site(result)
        rows.append({'site_id': source['site_id'], 'initial_tier': source['initial_tier'],
                     'raw_tier': result['measured_tier'], 'reviewed_tier': reviewed_tier(result, audit),
                     'audited_status': audit['audited_status'], 'issues': audit['issues'],
                     'evidence_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    resume = out/'resume-20261012'
    before = json.loads((resume/'existing-site-hashes.json').read_text())
    modified = [site for site, digest in before.items()
                if hashlib.sha256((out/'sites'/(site+'.json')).read_bytes()).hexdigest() != digest]
    states = [json.loads((resume/'runner.json').read_text()), json.loads((out/'runner.json').read_text())]
    report = {'updated_at': utc_now(), 'registered': len(manifest['sites']), 'processed': len(rows),
              'pending_first_pass': len(manifest['sites'])-len(rows),
              'first_pass_complete': len(rows) == len(manifest['sites']),
              'audited_statuses': dict(Counter(r['audited_status'] for r in rows)),
              'raw_tiers': dict(Counter(r['raw_tier'] for r in rows)),
              'initial_all_sites': dict(Counter(r['initial_tier'] for r in manifest['sites'])),
              **transitions(rows), 'rows': rows,
              'original_results_preserved': not modified, 'modified_existing_sites': modified,
              'prior_results_skipped': len(before), 'phases': [phase_metrics(s, results) for s in states],
              'limits': {'same_host_serial': True, 'gap_after_response_seconds': 1.5,
                         'per_host_ideal_requests_per_second_upper_bound': 1/1.5,
                         'effective_bound': '1 / (response_duration_seconds + 1.5), further reduced by cooldowns',
                         'shared_public_ip_servers': 4, 'servers_used': 1,
                         'multiserver_requirement': 'Disjoint host ownership or a shared cross-server gate; independent host locks do not enforce the total rate.',
                         'measured_capacity_ceiling': False},
              'tier_definitions': {'높음': 'Independent boundary cross-checks accepted by the evidence audit',
                                   '보통': 'A bounded collection route observed without current audit issues',
                                   '낮음': 'Accessible or reviewable, but list/date/boundary evidence incomplete',
                                   '불안정': 'Observed identity/order/pagination contradictions require reconciliation',
                                   '불가': 'Observed access/transport/entry inability on this server at measurement time'},
              'caveats': ['Tier differences compare estimates with bounded response observations, not source degradation over time.',
                          '불가 is inability observed on this server at this time, not permanent impossibility.',
                          'First-pass completion is separate from unresolved evidence review and whole-source verification.',
                          'No documents saved or database connections made by reconnaissance.']}
    report['whole_goal_complete'] = report['first_pass_complete'] and not report['audited_statuses'].get('needs_review', 0)
    resource_file = resume/'resources.jsonl'
    if resource_file.exists():
        samples = [json.loads(line) for line in resource_file.read_text().splitlines() if line.strip()]
        if samples:
            seconds = (datetime.fromisoformat(samples[-1]['at'])-datetime.fromisoformat(states[-1]['started_at'])).total_seconds()
            report['resources'] = {'samples': len(samples), 'peak_sampled_rss_MiB': round(max(s['rss_bytes'] for s in samples)/2**20, 2),
                                   'last_cpu_seconds': samples[-1]['cpu_seconds'],
                                   'average_cpu_percent_of_one_core_until_last_sample': round(100*samples[-1]['cpu_seconds']/seconds, 2) if seconds else None,
                                   'last_sample_at': samples[-1]['at'], 'evidence': str(resource_file)}
    atomic_json(out/'tier-throughput-report.json', report)
    stream = io.StringIO(); writer = csv.writer(stream)
    writer.writerow(['initial_tier', *TIERS])
    for tier in TIERS: writer.writerow([tier, *[report['matrix'][tier][t] for t in TIERS]])
    (out/'tier-transition-matrix.csv').write_text(stream.getvalue(), encoding='utf-8-sig')
    print(json.dumps({k: report[k] for k in ('processed','pending_first_pass','reviewed_tiers','delta_among_processed','audited_statuses','whole_goal_complete')}, ensure_ascii=False))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--out', required=True)
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    watch(args.out) if args.watch else build(args.out)
