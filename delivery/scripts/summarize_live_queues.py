"""Join the two bounded live-check batches without summing overlapping counts.

This is an offline report. Completion requires current, hash-bound batch audits;
an unavailable disposition remains unavailable in every exported representation.
"""
import argparse
from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path

from delivery.health.results import atomic_json, utc_now


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_batch(out, batch):
    queue = json.loads((out / 'input-queue.json').read_text())
    plans = json.loads((out / 'plans.json').read_text())
    marker = json.loads((out / (batch + '-completion.json')).read_text())
    audit_path = out / 'disposition-audit.json'
    audit = json.loads(audit_path.read_text())
    if marker.get('complete') is not True or marker['disposition_audit_sha256'] != digest(audit_path):
        raise ValueError('missing or stale batch completion: ' + batch)
    if marker['plans_sha256'] != digest(out / 'plans.json') or marker['queue_sha256'] != digest(out / 'input-queue.json'):
        raise ValueError('queue or plans changed after audit: ' + batch)
    source = {r['site_id']: r for r in queue}
    plan_ids = {p['site_id'] for p in plans['sites']}
    if len(source) != len(queue) or plan_ids != set(source) or audit['issues'] or audit['missing']:
        raise ValueError('batch scope/integrity mismatch: ' + batch)
    if len(audit['sites']) != len(source) or {r['site_id'] for r in audit['sites']} != set(source):
        raise ValueError('audit site coverage mismatch: ' + batch)
    rows = []
    for review in audit['sites']:
        site = review['site_id']; original = source[site]
        path = out / 'sites' / (site + '.json')
        if review['result_sha256'] != digest(path):
            raise ValueError('site evidence changed after audit: ' + site)
        result = json.loads(path.read_text())
        if result['live_check'] != original['live_check'] or result['delivery'] != original['delivery']:
            raise ValueError('original site instructions changed: ' + site)
        if batch == 'extra' and result.get('offline_assessment') != {k: original.get(k) for k in ('reason', 'sonnet_strategy', 'opus_strategy')}:
            raise ValueError('offline judgments were not preserved: ' + site)
        rows.append(dict(site_id=site, batch=batch, delivery=result['delivery'], kind=result['kind'],
            final_disposition=review['verdict'], strategy=result['strategy'], confidence=result['confidence'],
            **{k: result[k] for k in ('expected_items', 'count_basis', 'count_confidence', 'hidden_cap_suspected')},
            scope_estimates_available=sum(s['expected_items'] is not None for s in result['count_evidence']['scope_estimates']),
            requests=len(result['requests']), HTTP_responses=result['actual_HTTP_responses'],
            reason=result.get('reason', ''), offline_reason=original.get('reason'),
            sonnet_strategy=original.get('sonnet_strategy'), opus_strategy=original.get('opus_strategy'),
            live_check=original['live_check'], evidence=str(path)))
    return rows, marker


def summarize(base):
    rows = []; markers = {}
    for batch, directory in (('primary', 'live-check-20261012'), ('extra', 'live-check-extra-20261012')):
        batch_rows, markers[batch] = load_batch(base / directory, batch)
        rows.extend(batch_rows)
    if len(rows) != 872 or len({r['site_id'] for r in rows}) != 872 or sum(r['delivery'] for r in rows) != 582:
        raise ValueError('expected 872 unique sites, including 582 delivery sites')
    rows.sort(key=lambda r: (r['batch'] != 'primary', not r['delivery'], r['kind'], r['site_id']))
    out = base / 'live-check-combined-20261012'; out.mkdir(exist_ok=True)
    buffer = io.StringIO(); writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader(); writer.writerows(rows)
    temporary = out / 'summary.csv.tmp'; temporary.write_text(buffer.getvalue()); temporary.replace(out / 'summary.csv')
    summary = dict(updated_at=utc_now(), total=872, processed=len(rows), delivery_total=582,
        batches={batch: dict(total=m['total'], delivery=363 if batch == 'primary' else 219, counts=m['counts']) for batch,m in markers.items()},
        counts=dict(Counter(r['final_disposition'] for r in rows)),
        delivery_counts=dict(Counter(r['final_disposition'] for r in rows if r['delivery'])),
        strategies=dict(Counter(r['strategy'] for r in rows)), confidences=dict(Counter(r['confidence'] for r in rows)),
        expected_item_counts_available=sum(r['expected_items'] is not None for r in rows),
        expected_item_counts_unknown=sum(r['expected_items'] is None for r in rows),
        count_bases=dict(Counter(r['count_basis'] for r in rows)),
        hidden_cap_suspected_sites=sum(r['hidden_cap_suspected'] for r in rows),
        scoped_count_sites=sum(r['scope_estimates_available'] > 0 for r in rows),
        requests_recorded=sum(r['requests'] for r in rows), HTTP_responses=sum(r['HTTP_responses'] for r in rows),
        maximum_requests_per_site=max(r['requests'] for r in rows),
        completion_kind='bounded_live_check_with_explicit_unavailable_dispositions',
        batch_completed=True, full_collection_verification=False, documents_saved=0, database_connections=0,
        summed_expected_items=None, sum_not_performed_reason='Different scopes and duplicate registrations are not proven disjoint.',
        summary_csv=str(out / 'summary.csv'), summary_csv_sha256=digest(out / 'summary.csv'),
        completion_evidence={b: str(base / ('live-check-20261012' if b == 'primary' else 'live-check-extra-20261012') / (b + '-completion.json')) for b in markers})
    defects = dict(confirmed=[], rejected_candidates=[], candidates=[], batch_evidence={})
    for batch, directory in (('primary','live-check-20261012'), ('extra','live-check-extra-20261012')):
        reviewed_path=base/directory/'collector-defects-reviewed.json'
        reviewed=json.loads(reviewed_path.read_text())
        candidates=json.loads((base/directory/'collector-defects.json').read_text())
        for key in ('confirmed','rejected_candidates'):
            defects[key].extend(dict(batch=batch, **r) for r in reviewed[key])
        defects['candidates'].extend(dict(batch=batch, **r) for r in candidates['findings'])
        defects['batch_evidence'][batch]=str(reviewed_path)
    defects.update(updated_at=utc_now(), limitation='Candidates are not confirmed defects; no collector or database execution was performed.')
    atomic_json(out/'collector-defects.json',defects)
    summary['confirmed_defect_counts']=dict(Counter(r['kind'] for r in defects['confirmed']))
    summary['unreviewed_candidate_list']=str(out/'collector-defects.json')
    count=summary['counts']
    report=[
        '라이브 확인 2개 대기열 결과',
        f"작성: {summary['updated_at']}",
        '기존 615개(납품 363개) 처리 후 추가 257개(납품 219개)를 확인했습니다. 중복 없는 총 872개, 납품 대상 582개입니다.',
        f"전략 확정 {count.get('strategy_confirmed',0)}개 / 접근·정책 제한 {count.get('access_or_policy_unavailable',0)}개 / 맞춤 검증 필요로 확정 불가 {count.get('custom_verification_unavailable',0)}개.",
        '완료는 제한된 라이브 확인과 불가 사유 기록의 완료입니다. 전체 데이터 수집 성공이나 모든 요청 조건 통과를 뜻하지 않습니다.',
        f"사이트 수준 예상 건수 기록 {summary['expected_item_counts_available']}개, 알 수 없음(null) {summary['expected_item_counts_unknown']}개. 숨은 상한 의심 {summary['hidden_cap_suspected_sites']}개.",
        '여러 목록의 범위·중복을 확정하지 못한 경우 사이트 예상 건수는 null로 두고, 개별 목록에서 확인한 건수는 사이트 JSON의 count_evidence.scope_estimates에 보존했습니다.',
        '사이트맵 수치는 URL 수이며 문서 수와 다를 수 있습니다. 보고 총건수는 표시 범위에 대한 관측값이고, 신뢰도·상한 의심 여부를 함께 읽어야 합니다. 범위 중복 때문에 예상 건수를 전 사이트 합산하지 않았습니다.',
        f"누적 요청 기록 {summary['requests_recorded']}건, HTTP 응답 {summary['HTTP_responses']}건, 사이트당 최대 {summary['maximum_requests_per_site']}건(한도 30건).",
        '추가 목록은 목록·총건수 메타데이터만 요청했습니다. 기존 단계에서 이미 얻은 상세 메타데이터는 이력으로 보존했고 다시 요청하지 않았습니다. 문서 저장·DB 연결·배포·외부 발송은 수행하지 않았습니다.',
        'Sonnet/Opus 의견과 reason, live_check 원문은 추가 사이트 JSON 및 통합 CSV에 보존했습니다.',
        '수집기 결함은 확정·기각·미확정 후보를 collector-defects.json에 구분했습니다. 확인된 결함 수: '+json.dumps(summary['confirmed_defect_counts'],ensure_ascii=False),
        '통합 CSV: '+str(out/'summary.csv'),
        '사이트별 증거: CSV evidence 열의 JSON 및 각 배치 disposition-audit.json',
    ]
    (out/'report.txt').write_text('\n\n'.join(report)+'\n')
    summary['report']=str(out/'report.txt')
    atomic_json(out / 'summary.json', summary)
    progress_path = base / 'progress.json'; progress = json.loads(progress_path.read_text())
    progress['live_structure_check_combined'] = summary; atomic_json(progress_path, progress)
    ledger_path = base / 'continuous-20261010/site-progress.json'; ledger = json.loads(ledger_path.read_text())
    mapping = {r['site_id']:r for r in rows}
    for record in ledger['sites']:
        if record['site_id'] in mapping:
            row = mapping[record['site_id']]
            record['live_structure_check'].update(final_disposition=row['final_disposition'], bounded_check_completed=True,
                full_collection_verification=False)
    atomic_json(ledger_path, ledger)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--base', required=True, type=Path)
    print(json.dumps(summarize(parser.parse_args().base), ensure_ascii=False))
