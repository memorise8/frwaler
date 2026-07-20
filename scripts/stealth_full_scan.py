"""Run stealth_fetcher on ALL blocked sites (except robots_disallow).

Output:
  data/audit/stealth_full_scan.json — per-site result
  data/audit/stealth_recoverable.txt — newline list of hosts that returned 'ok'
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path('/data_raid/ruci_workspace/frwaler_job')
sys.path.insert(0, str(ROOT))
from crawler.stealth_fetcher import StealthSession

BLOCKED_CSV = ROOT / 'data/audit/blocked.csv'
OUT_JSON = ROOT / 'data/audit/stealth_full_scan.json'
OUT_OK_HOSTS = ROOT / 'data/audit/stealth_recoverable.txt'
EXCLUDE_CATEGORIES = {'robots_disallow'}


def process_one(host: str, url: str, category: str):
    sess = StealthSession()
    t0 = time.time()
    html, info = sess.fetch_html(url)
    return {
        'host': host,
        'url': url,
        'category_input': category,
        'final_layer': info.get('final_layer'),
        'final_reason': info.get('final_reason'),
        'html_len': len(html) if html else 0,
        'elapsed_seconds': round(time.time() - t0, 1),
        'success': info.get('final_reason') == 'ok',
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--limit', type=int, default=0, help='0 = all')
    args = p.parse_args()

    # Load + dedupe by host (one URL per host to save time)
    seen: dict[str, dict] = {}
    for r in csv.DictReader(open(BLOCKED_CSV)):
        if r.get('category') in EXCLUDE_CATEGORIES:
            continue
        if not r.get('url'):
            continue
        host = r['host']
        if host in seen:
            continue
        seen[host] = r

    rows = list(seen.values())
    if args.limit > 0:
        rows = rows[:args.limit]
    total = len(rows)
    print(f"[stealth-scan] {total} unique hosts to probe (workers={args.workers})")

    results = []
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(process_one, r['host'], r['url'], r.get('category', '?')): r['host']
            for r in rows
        }
        done = 0
        for f in as_completed(futures):
            done += 1
            res = f.result()
            ok = '✓' if res['success'] else '✗'
            print(f"  [{done:>3}/{total}] {ok} {res['elapsed_seconds']:>5.1f}s "
                  f"{res['final_reason']:<25} {res['host']}")
            results.append(res)

    total_elapsed = time.time() - t_start
    n_ok = sum(1 for r in results if r['success'])
    print(f"\n[stealth-scan] === 전수 결과 ===")
    print(f"  총 시도            : {total}")
    print(f"  성공 (ok)          : {n_ok} ({100*n_ok/max(1,total):.1f}%)")
    print(f"  소요 시간          : {total_elapsed/60:.1f}min")

    summary = {
        'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'total': total,
        'workers': args.workers,
        'success_count': n_ok,
        'success_rate_pct': round(100*n_ok/max(1,total), 1),
        'total_elapsed_seconds': round(total_elapsed, 1),
        'per_site': sorted(results, key=lambda x: (not x['success'], x['host'])),
    }
    OUT_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"  결과 JSON          : {OUT_JSON}")

    ok_hosts = [r['host'] for r in results if r['success']]
    OUT_OK_HOSTS.write_text('\n'.join(sorted(ok_hosts)) + '\n')
    print(f"  성공 host 목록     : {OUT_OK_HOSTS}")

    # category breakdown
    from collections import Counter
    cat_total = Counter(r['category_input'] for r in results)
    cat_ok = Counter(r['category_input'] for r in results if r['success'])
    print(f"\n  카테고리별 성공률:")
    for cat in cat_total:
        total_c = cat_total[cat]
        ok_c = cat_ok.get(cat, 0)
        print(f"    {cat:<20} {ok_c}/{total_c} ({100*ok_c/total_c:.0f}%)")


if __name__ == '__main__':
    main()
