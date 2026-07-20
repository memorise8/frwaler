"""Diagnose PDF download failure reason per site.

For each site_id with non-zero `pdf_url + pdf_downloaded=0` count,
HEAD-probe up to SAMPLE samples and classify the dominant reason.

Output: data/audit/pdf_gap_diagnosis.json
"""
import argparse, json, sqlite3, requests, time, sys, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

DB = '/data_raid/ruci_workspace/frwaler_job/data/libertree.db'
OUT = '/data_raid/ruci_workspace/frwaler_job/data/audit/pdf_gap_diagnosis.json'
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; libertree-bot/1.0)"}
TIMEOUT = 12

def classify(url):
    try:
        r = requests.head(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        st = r.status_code
        ct = (r.headers.get('content-type') or '').lower()
        if st == 200 and 'pdf' in ct:
            return 'retry_ok'
        if st == 200 and 'html' in ct:
            return 'embargo_html'
        if st == 200:
            return f'retry_ok'  # other content types but 200
        if st == 404 or st == 410:
            return 'permanent_404'
        if 400 <= st < 500:
            return 'client_error'
        if 500 <= st:
            return 'server_error'
        return 'other'
    except requests.exceptions.SSLError:
        return 'ssl_error'
    except requests.exceptions.Timeout:
        return 'timeout'
    except requests.exceptions.ConnectionError:
        return 'conn_error'
    except Exception:
        return 'other'

def probe_one(site_id, urls):
    outcomes = [classify(u) for u in urls if u]
    counts = Counter(outcomes)
    if not counts:
        return site_id, {'samples': 0, 'reasons': {}, 'dominant': 'no_url'}
    dominant = counts.most_common(1)[0][0]
    return site_id, {
        'samples': sum(counts.values()),
        'reasons': dict(counts),
        'dominant': dominant,
    }

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--sample', type=int, default=3)
    p.add_argument('--workers', type=int, default=8)
    args = p.parse_args()

    con = sqlite3.connect(DB)
    sites_with_gap = con.execute("""
        SELECT site_id,
               SUM(CASE WHEN COALESCE(pdf_url,'')!='' AND pdf_downloaded=0 THEN 1 ELSE 0 END) AS gap
        FROM documents
        GROUP BY site_id
        HAVING gap > 0
        ORDER BY gap DESC
    """).fetchall()

    print(f"[diag] sites with PDF gap: {len(sites_with_gap)}")
    site_urls = {}
    for sid, gap in sites_with_gap:
        rows = con.execute("""
            SELECT pdf_url FROM documents
            WHERE site_id=? AND COALESCE(pdf_url,'')!='' AND pdf_downloaded=0
            LIMIT ?
        """, (sid, args.sample)).fetchall()
        site_urls[sid] = [r[0] for r in rows]
    con.close()

    # Add gap count to result
    gap_map = {sid: g for sid, g in sites_with_gap}

    out = {}
    t_start = time.time()
    done = 0
    total = len(site_urls)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(probe_one, sid, urls) for sid, urls in site_urls.items()]
        for f in as_completed(futures):
            sid, res = f.result()
            res['gap_count'] = gap_map.get(sid, 0)
            out[sid] = res
            done += 1
            if done % 20 == 0 or done == total:
                elapsed = time.time() - t_start
                print(f"[diag] {done}/{total} sites probed elapsed={elapsed:.0f}s")

    payload = {
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'sample_per_site': args.sample,
        'sites': out,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n[diag] saved: {OUT}")
    print(f"[diag] elapsed total: {(time.time()-t_start)/60:.1f}min")

    # Aggregate summary
    dom_counts = Counter(r['dominant'] for r in out.values())
    print(f"\n=== dominant reason 분포 ===")
    for k, v in dom_counts.most_common():
        print(f"  {k:<20s} {v} sites")

if __name__ == '__main__':
    main()
