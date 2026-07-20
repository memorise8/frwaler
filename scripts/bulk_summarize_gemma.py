"""Bulk-summarize documents for a single site_id using local Gemma via ollama.

Usage:
    .venv/bin/python scripts/bulk_summarize_gemma.py \\
        --site ens-hal-science-search \\
        [--port 11434] [--limit 0] [--commit-every 20]

- Reads text blobs from libertree/AAAA/BBBB/AAAABBBBNNNN.txt (fallback: abstract)
- Calls ollama gemma4:26b with the same prompt template as summarizer.py
- Writes result back to documents.summary (resume-safe — skips rows with summary)
"""
import argparse, os, sys, sqlite3, time, requests
from pathlib import Path

ROOT = Path('/data_raid/ruci_workspace/frwaler_job')
sys.path.insert(0, str(ROOT))
from crawler.summarizer import SYSTEM_PROMPT, _clean

BLOB = ROOT / 'libertree'
DB = ROOT / 'data' / 'libertree.db'
MAX_INPUT = 6000
NUM_PREDICT = 1500
NUM_CTX = 8192
MODEL = 'gemma4:26b'

def blob_path(seq_id, ext):
    s = f"{seq_id:012d}"
    return BLOB / s[0:4] / s[4:8] / f"{s}.{ext}"

def load_text(seq_id, abstract):
    p = blob_path(seq_id, 'txt')
    if p.exists():
        try:
            txt = p.read_text(encoding='utf-8')
            if len(txt) > 200:
                return txt
        except Exception:
            pass
    return abstract or ''

def build_prompt(text, title):
    if len(text) > MAX_INPUT:
        text = text[:MAX_INPUT] + "\n…(이하 생략)"
    if title:
        return f"제목: {title}\n\n본문:\n{text}\n\n위 본문을 한국어로 3~5문장으로 요약하세요."
    return f"다음 본문을 한국어로 3~5문장으로 요약하세요:\n\n{text}"

def call_gemma(endpoint, text, title, timeout=600):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(text, title)},
        ],
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": NUM_PREDICT, "num_ctx": NUM_CTX},
    }
    r = requests.post(endpoint, json=payload, timeout=timeout)
    r.raise_for_status()
    return _clean(r.json().get('message', {}).get('content'))

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--site', required=True)
    p.add_argument('--port', type=int, default=11434)
    p.add_argument('--limit', type=int, default=0, help='0=all remaining')
    p.add_argument('--commit-every', type=int, default=20)
    p.add_argument('--timeout', type=int, default=600)
    args = p.parse_args()

    endpoint = f"http://127.0.0.1:{args.port}/api/chat"
    con = sqlite3.connect(str(DB))
    con.execute("PRAGMA journal_mode=WAL")

    # Pending docs
    sql = """
        SELECT seq_id, title, abstract FROM documents
        WHERE site_id = ? AND text_extracted = 1 AND COALESCE(summary,'') = ''
        ORDER BY seq_id
    """
    rows = con.execute(sql, (args.site,)).fetchall()
    if args.limit > 0:
        rows = rows[:args.limit]
    total = len(rows)
    print(f"[bulk-summarize] site={args.site} endpoint={endpoint}")
    print(f"[bulk-summarize] {total} docs pending (summary empty + text_extracted=1)")
    if total == 0:
        print("[bulk-summarize] nothing to do.")
        return

    t_start = time.time()
    n_ok = n_fail = 0
    pending_writes = []
    for i, (seq, title, abstract) in enumerate(rows, 1):
        text = load_text(seq, abstract)
        t0 = time.time()
        try:
            out = call_gemma(endpoint, text, title, timeout=args.timeout)
            elapsed = time.time() - t0
            if out:
                pending_writes.append((out, MODEL, seq))
                n_ok += 1
                status = f"OK ({len(out)}c)"
            else:
                n_fail += 1
                status = "EMPTY"
        except Exception as e:
            elapsed = time.time() - t0
            n_fail += 1
            status = f"ERR:{type(e).__name__}"

        eta_min = (time.time() - t_start) / i * (total - i) / 60
        print(f"[{i}/{total}] seq={seq} {elapsed:>6.1f}s {status:<12s} ok={n_ok} fail={n_fail} ETA={eta_min:.0f}min")

        if len(pending_writes) >= args.commit_every:
            con.executemany(
                "UPDATE documents SET summary=?, summary_model=?, summary_at=CURRENT_TIMESTAMP WHERE seq_id=?",
                pending_writes
            )
            con.commit()
            pending_writes.clear()

    if pending_writes:
        con.executemany(
            "UPDATE documents SET summary=?, summary_model=?, summary_at=CURRENT_TIMESTAMP WHERE seq_id=?",
            pending_writes
        )
        con.commit()
    con.close()
    print(f"\n[bulk-summarize] DONE site={args.site} total={total} ok={n_ok} fail={n_fail}")
    print(f"[bulk-summarize] elapsed={(time.time()-t_start)/60:.1f}min")

if __name__ == '__main__':
    main()
