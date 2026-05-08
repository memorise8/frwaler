"""Demo re-crawl: parse Infineon BJT datasheets for Vceo / Ic / Tj / Ptot.

Pipeline per row in `products WHERE site_id='infineon' AND device_type='bjt'`:
  1) Fetch HTML part page (https://www.infineon.com/part/<OPN>)
  2) Extract first PDF URL via the same regex used in crawler/sites/infineon.py
  3) Download PDF (cache to data/datasheets/infineon/<OPN>.pdf)
  4) Parse Limiting-values table for Vceo / Vcbo / Vebo / Ic / Tj / Ptot
  5) Update products.specs (merge) and metadata.datasheet_url
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sqlite3
import sys
import time

import httpx
import pdfplumber

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
PDF_DIR = "data/datasheets/infineon"
_PDF_URL_RE = re.compile(
    r'https?://www\.infineon\.com/assets/[^\s"\'<>]+?\.pdf', re.IGNORECASE
)
_NUM = r"(-?\d+(?:\.\d+)?)"


def extract_datasheet_url(html: str, opn: str) -> str | None:
    urls = _PDF_URL_RE.findall(html or "")
    if not urls:
        return None
    seen = set()
    deduped = [u for u in urls if not (u in seen or seen.add(u))]
    opn_slug = opn.lower().replace("-", "").replace("_", "")
    for u in deduped:
        slug = u.lower().split("/")[-1].replace("-", "").replace("_", "")
        if opn_slug and opn_slug in slug:
            return u
    return deduped[0]


def parse_limiting(pdf_bytes: bytes) -> dict:
    out = {}
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            chunks = []
            for page in pdf.pages[:4]:
                try:
                    chunks.append(page.extract_text() or "")
                except Exception:
                    pass
            text = "\n".join(chunks)
    except Exception:
        return out
    if not text:
        return out

    def grab_v(pat):
        m = re.search(pat + r"[^\n]*?" + _NUM + r"\s*V\b", text, re.I)
        return abs(float(m.group(1))) if m else None

    def grab_i(pat):
        m = re.search(pat + r"[^\n]*?" + _NUM + r"\s*(m?A)\b", text, re.I)
        if not m:
            return None
        v = abs(float(m.group(1)))
        if m.group(2).lower() == "ma":
            v /= 1000.0
        return v

    v = grab_v(r"collector-?emitter\s+(?:open\s+base|voltage)")
    if v is not None:
        out["vceo_v"] = v
    v = grab_v(r"collector-?base\s+(?:open\s+emitter|voltage)")
    if v is not None:
        out["vcbo_v"] = v
    v = grab_v(r"emitter-?base\s+(?:open\s+collector|voltage)")
    if v is not None:
        out["vebo_v"] = v
    ic = grab_i(r"\bcollector\s+current\b")
    if ic is not None:
        out["ic_max_a"] = ic

    m = re.search(r"junction\s+temperature[^\n]*?" + _NUM + r"\s*°?C", text, re.I)
    if m:
        out["tj_max_c"] = int(float(m.group(1)))
    m = re.search(r"total\s+power\s+dissipation[^\n]*?" + _NUM + r"\s*(m?W)",
                  text, re.I)
    if m:
        v = float(m.group(1))
        if m.group(2).lower() == "mw":
            v /= 1000.0
        out["ptot_w"] = v

    quals = []
    for kw in ["AEC-Q101", "AEC-Q100", "automotive", "Automotive",
               "MIL-PRF", "Hi-Rel"]:
        if kw in text:
            quals.append(kw)
    if quals:
        out["qualifications"] = sorted(set(quals))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/products.db")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--delay", type=float, default=0.3)
    ap.add_argument("--cache-pdf", action="store_true", default=True)
    args = ap.parse_args()

    os.makedirs(PDF_DIR, exist_ok=True)
    conn = sqlite3.connect(args.db)

    rows = conn.execute(
        "SELECT external_id, url, specs, metadata FROM products "
        "WHERE site_id='infineon' AND device_type='bjt' ORDER BY external_id"
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"[infineon-bjt] {len(rows)} rows to process")

    parsed = parse_ok = no_url = no_pdf = err = 0
    with httpx.Client(headers={"User-Agent": UA},
                      follow_redirects=True, timeout=20.0) as c:
        for i, (opn, url, specs_raw, meta_raw) in enumerate(rows, 1):
            try:
                specs = json.loads(specs_raw) if specs_raw else {}
                meta = json.loads(meta_raw) if meta_raw else {}
            except Exception:
                specs, meta = {}, {}

            ds = meta.get("datasheet_url")
            if not ds:
                hr = c.get(url)
                if hr.status_code != 200:
                    no_url += 1
                    continue
                ds = extract_datasheet_url(hr.text, opn)
                if not ds:
                    no_url += 1
                    continue
                meta["datasheet_url"] = ds

            cached = os.path.join(PDF_DIR, f"{opn}.pdf")
            if args.cache_pdf and os.path.exists(cached):
                with open(cached, "rb") as fh:
                    pdf_bytes = fh.read()
            else:
                pr = c.get(ds)
                if pr.status_code != 200 or "pdf" not in pr.headers.get("content-type", ""):
                    no_pdf += 1
                    continue
                pdf_bytes = pr.content
                if args.cache_pdf:
                    with open(cached, "wb") as fh:
                        fh.write(pdf_bytes)

            params = parse_limiting(pdf_bytes)
            if params:
                parse_ok += 1
            specs.update(params)

            conn.execute(
                "UPDATE products SET specs=?, metadata=? "
                "WHERE site_id='infineon' AND external_id=?",
                (json.dumps(specs, ensure_ascii=False),
                 json.dumps(meta), opn),
            )
            conn.commit()
            parsed += 1

            if parsed <= 5 or parsed % 30 == 0:
                print(f"  [{i}/{len(rows)}] {opn:20} "
                      f"vceo={params.get('vceo_v')} ic={params.get('ic_max_a')} "
                      f"tj={params.get('tj_max_c')}")
            time.sleep(args.delay)
        # after loop fall-through
            # break unused
    print(f"\n[done] processed={parsed} parsed_ok={parse_ok} "
          f"no_url={no_url} no_pdf={no_pdf} err={err}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
