"""Export a read-only status preview from explicitly ordered audit runs.

This is a last-check snapshot, not a live worker monitor. Later runs replace
earlier verdicts even when the earlier run saved documents.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re

LABELS = {
    'sample_saved': '표본 저장 확인', 'partial': '일부 저장 후 중단',
    'code_error': '실행 오류', 'blocked': '차단 신호', 'timeout': '시간 내 미확인',
    'http_error': 'HTTP 오류', 'zero': '0건 · 원인 미확인',
    'pdf_limited': '원문 검사 제한', 'unverified': '미검증',
}


def classify(result, log=''):
    if result.get('samples'):
        return 'partial' if result.get('error') or not result.get('completed') else 'sample_saved'
    if result.get('error') not in (None, '', 'probe_timeout'):
        return 'code_error'
    if result.get('pdf_suppressed'):
        return 'pdf_limited'
    statuses = set(result.get('http_statuses', {}))
    if statuses & {'403', '429'} or re.search(r'HTTP\s+(403|429)\b|access denied|captcha challenge', log, re.I):
        return 'blocked'
    if result.get('error') == 'probe_timeout':
        return 'timeout'
    if any(code.isdigit() and int(code) >= 400 for code in statuses):
        return 'http_error'
    return 'zero'


def collect(registry, runs, checked_date):
    rows = {site['id']: {'site_id': site['id'], 'name': site['name'],
        'status': 'unverified', 'checked_date': None, 'samples': [],
        'source_file': site['source'].removeprefix('/app/'), 'history': [],
        'error': None, 'http_statuses': {}, 'evidence': None} for site in registry}
    for run in runs:
        for path in sorted((run / 'results').glob('*.json')):
            result = json.loads(path.read_text())
            row = rows.get(result['id'])
            if row is None:
                continue
            log_path = run / 'logs' / (result['id'] + '.log')
            log = log_path.read_text(errors='replace') if log_path.exists() else ''
            status = classify(result, log)
            evidence = str(path)
            row.update(status=status, checked_date=checked_date, samples=result.get('samples', []),
                       error=result.get('error'), http_statuses=result.get('http_statuses', {}), evidence=evidence)
            row['history'].append({'run': str(run), 'status': status, 'sample_count': len(result.get('samples', []))})
    return list(rows.values())


HTML = '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>수집기 검사 현황</title><style>
body{font:16px system-ui;margin:32px auto;max-width:1200px;padding:0 20px;background:#f6f8fa;color:#172b3a}h1{margin-bottom:8px}
.note{line-height:1.65;color:#46596a}input,select{padding:12px;border:1px solid #aab7c3;border-radius:6px;margin:6px;max-width:90%}
article{background:white;border:1px solid #d9e1e8;border-radius:8px;padding:18px;margin:12px 0;overflow-wrap:anywhere}
.top{display:flex;gap:16px;justify-content:space-between;flex-wrap:wrap}.badge{font-weight:700;color:#95510e}.ok{color:#087343}code,small{color:#546574}
button{padding:10px;margin:8px;cursor:pointer}a{color:#145d91}pre{white-space:pre-wrap;font-size:13px}summary{cursor:pointer;margin-top:12px}
</style><h1>수집기 검사 현황</h1>
<p class="note">최근 검사 결과를 모은 조회용 시제품입니다. 운영 중·대기 중 여부는 연결하지 않았습니다.<br>
표본 저장 확인은 전량 수집 성공을 뜻하지 않습니다. 0건은 신규 문서 없음과 오류를 아직 구분하지 못한 상태입니다.<br>
과거 검사는 당시 코드·환경의 결과이며, 현재 배포 코드의 정상 여부를 보증하지 않습니다.</p>
<p id="counts"></p><label>검색 <input id="search" placeholder="수집기 ID 또는 이름"></label>
<label>최근 검사 <select id="filter"><option value="">전체</option></select></label><p id="matches"></p><div id="rows"></div>
<button id="prev">이전</button><span id="page"></span><button id="next">다음</button>
<script type="application/json" id="data">__DATA__</script><script>
const data=JSON.parse(document.getElementById('data').textContent),labels=data.labels;let page=0;
const el=(tag,text)=>{const e=document.createElement(tag);e.textContent=text??'';return e};
for(const [key,label] of Object.entries(labels)){const o=el('option',label);o.value=key;document.getElementById('filter').append(o)}
document.getElementById('counts').textContent=`등록 ${data.rows.length}개 · 생성 ${data.generated_at} · `+Object.entries(data.counts).map(([k,v])=>`${labels[k]} ${v}`).join(' / ');
function render(){const q=document.getElementById('search').value.toLowerCase(),f=document.getElementById('filter').value;
const rows=data.rows.filter(r=>(!f||r.status===f)&&`${r.site_id} ${r.name}`.toLowerCase().includes(q));
const pages=Math.max(1,Math.ceil(rows.length/30));page=Math.max(0,Math.min(page,pages-1));const target=document.getElementById('rows');target.replaceChildren();
document.getElementById('matches').textContent=`검색 결과 ${rows.length}개`;
for(const r of rows.slice(page*30,(page+1)*30)){const a=el('article'),top=el('div');top.className='top';top.append(el('strong',r.name));
const badge=el('span',labels[r.status]);badge.className='badge'+(r.status==='sample_saved'?' ok':'');top.append(badge);a.append(top,el('code',r.site_id),el('p',`검사일 ${r.checked_date??'미검증'} · 표본 ${r.samples.length}건 · HTTP ${Object.keys(r.http_statuses).join(', ')||'기록 없음'}`));
if(r.error)a.append(el('pre',r.error));const d=el('details');d.append(el('summary','근거와 표본 보기'),el('small',r.source_file));
for(const s of r.samples){const p=el('p');let url;try{url=new URL(s.url)}catch{}if(url&&['https:','http:'].includes(url.protocol)){const link=el('a',s.title);link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';p.append(link)}else p.textContent=s.title;d.append(p)}
for(const h of r.history)d.append(el('p',`${labels[h.status]} · ${h.sample_count}건 · ${h.run}`));a.append(d);target.append(a)}
document.getElementById('page').textContent=`${page+1} / ${pages}`;document.getElementById('prev').disabled=page===0;document.getElementById('next').disabled=page===pages-1;}
for(const id of ['search','filter'])document.getElementById(id).addEventListener('input',()=>{page=0;render()});
document.getElementById('prev').onclick=()=>{page--;render()};document.getElementById('next').onclick=()=>{page++;render()};render();
</script></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--runs', type=Path, nargs='+', required=True, help='Oldest first, newest last; one inspection date per export')
    parser.add_argument('--checked-date', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--console-output', type=Path, help='Also write the portable console snapshot')
    args = parser.parse_args()
    rows = collect(json.loads(args.registry.read_text())['registry'], args.runs, args.checked_date)
    data = {'generated_at': datetime.now(timezone.utc).isoformat(), 'labels': LABELS,
            'counts': dict(Counter(r['status'] for r in rows)), 'rows': rows}
    args.out.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False)
    (args.out / 'crawler-status.json').write_text(payload + '\n')
    (args.out / 'crawler-status.html').write_text(HTML.replace('__DATA__', payload.replace('<', '\\u003c')))
    if args.console_output:
        portable = json.loads(payload)
        for row in portable['rows']:
            row['samples'] = row['samples'][:3]
            row.pop('evidence', None)
            row['history'] = [{k: entry[k] for k in ('status', 'sample_count')} for entry in row['history']]
        args.console_output.parent.mkdir(parents=True, exist_ok=True)
        args.console_output.write_text(json.dumps(portable, ensure_ascii=False, separators=(',', ':')) + '\n')
    print(json.dumps(data['counts'], ensure_ascii=False))


if __name__ == '__main__':
    main()
