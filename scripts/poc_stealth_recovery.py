"""PoC: try stealth_fetcher on 15 blocked sites and report success rate.

Usage:
    .venv/bin/python scripts/poc_stealth_recovery.py [--n 15]
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path('/data_raid/ruci_workspace/frwaler_job')
sys.path.insert(0, str(ROOT))

from crawler.stealth_fetcher import StealthSession

BLOCKED_CSV = ROOT / 'data/audit/blocked.csv'
OUT_JSON = ROOT / 'data/audit/stealth_poc_results.json'


EXCLUDE_CATEGORIES = {'robots_disallow'}  # ethical: respect robots.txt


def pick_samples(n_total: int = 15) -> list[dict]:
    """Pick sites from blocked.csv, skip robots_disallow."""
    rows = [r for r in csv.DictReader(open(BLOCKED_CSV))
            if r.get('category') not in EXCLUDE_CATEGORIES
            and r.get('url')]
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r.get('category', 'other'), []).append(r)
    # priority order
    quota = {
        'cf_ip_block': 9,
        'server_403': 4,
        'not_found': 2,
    }
    seen_hosts: set[str] = set()
    chosen: list[dict] = []
    for cat, q in quota.items():
        for r in by_cat.get(cat, []):
            if len(chosen) >= n_total:
                break
            if r['host'] in seen_hosts:
                continue
            chosen.append(r)
            seen_hosts.add(r['host'])
            if sum(1 for x in chosen if x.get('category') == cat) >= q:
                break
    # fill remaining
    for r in rows:
        if len(chosen) >= n_total:
            break
        if r['host'] in seen_hosts:
            continue
        chosen.append(r)
        seen_hosts.add(r['host'])
    return chosen[:n_total]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--n', type=int, default=15)
    args = p.parse_args()

    samples = pick_samples(args.n)
    print(f"[poc] {len(samples)} sample sites selected")
    for i, s in enumerate(samples, 1):
        print(f"  {i:>2}. [{s.get('category','?'):<15}] {s['host']}")

    sess = StealthSession()
    results = []
    t0 = time.time()
    for i, s in enumerate(samples, 1):
        url = s.get('url')
        if not url:
            continue
        host = s['host']
        ts = time.time()
        html, info = sess.fetch_html(url)
        elapsed = time.time() - ts
        ok = info.get('final_reason') == 'ok'
        results.append({
            'host': host,
            'url': url,
            'category_input': s.get('category'),
            'final_layer': info.get('final_layer'),
            'final_reason': info.get('final_reason'),
            'attempts': info.get('attempts'),
            'html_len': len(html) if html else 0,
            'elapsed_seconds': round(elapsed, 1),
            'success': ok,
        })
        flag = '✓' if ok else '✗'
        print(f"  [{i}/{len(samples)}] {flag} {elapsed:>5.1f}s  {info.get('final_reason'):<25}  {host}")

    total_elapsed = time.time() - t0
    n_ok = sum(1 for r in results if r['success'])
    n_challenge = sum(1 for r in results if r['final_reason'] == 'cloudflare_challenge')
    n_403 = sum(1 for r in results if r['final_reason'] == '403_forbidden')
    n_timeout = sum(1 for r in results if 'timeout' in (r['final_reason'] or ''))

    summary = {
        'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'samples': len(samples),
        'total_elapsed_seconds': round(total_elapsed, 1),
        'avg_per_site_seconds': round(total_elapsed / max(1, len(samples)), 1),
        'counts': {
            'ok': n_ok,
            'cloudflare_challenge': n_challenge,
            '403_forbidden': n_403,
            'timeout_like': n_timeout,
            'other_fail': len(samples) - n_ok - n_challenge - n_403 - n_timeout,
        },
        'success_rate': round(100 * n_ok / max(1, len(samples)), 1),
        'per_site': results,
    }
    OUT_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n[poc] === 결과 ===")
    print(f"  성공 (ok)          : {n_ok}/{len(samples)} ({summary['success_rate']}%)")
    print(f"  cloudflare 챌린지  : {n_challenge}")
    print(f"  403 forbidden      : {n_403}")
    print(f"  timeout            : {n_timeout}")
    print(f"  기타 실패          : {summary['counts']['other_fail']}")
    print(f"  평균 응답 시간     : {summary['avg_per_site_seconds']}s")
    print(f"  결과 JSON          : {OUT_JSON}")


if __name__ == '__main__':
    main()
