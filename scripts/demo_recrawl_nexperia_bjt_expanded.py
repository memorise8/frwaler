"""Demo re-crawl: expanded Nexperia BJT seed (~75 series).

Curated list of SMD bipolar transistor base names. Many are series PDFs
(e.g. BCP56_SER.pdf covers BCP56-10/-16/-25/-40). For each base name we try
both `<MPN>.pdf` and `<MPN>_SER.pdf` and parse the limiting-values table.
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
import uuid

import httpx
import pdfplumber

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
PDF_DIR = "data/datasheets/nexperia"
_NUM = r"(-?\d+(?:\.\d+)?)"


# Base MPNs / series. (mpn, polarity_hint, package_hint)
SEED_BJTS = [
    # SOT-23 small-signal NPN/PNP
    ("BC817", "NPN", "SOT23"), ("BC846", "NPN", "SOT23"),
    ("BC847", "NPN", "SOT23"), ("BC848", "NPN", "SOT23"),
    ("BC807", "PNP", "SOT23"), ("BC856", "PNP", "SOT23"),
    ("BC857", "PNP", "SOT23"), ("BC858", "PNP", "SOT23"),
    # Higher-current SOT-23
    ("BCX17", "PNP", "SOT23"), ("BCX19", "NPN", "SOT23"),
    ("BCX42", "NPN", "SOT89"), ("BCX52", "PNP", "SOT89"),
    ("BCX53", "PNP", "SOT89"), ("BCX54", "PNP", "SOT89"),
    ("BCX56", "NPN", "SOT89"), ("BCX70", "NPN", "SOT23"),
    ("BCX71", "PNP", "SOT23"),
    # SOT-223 medium-power
    ("BCP51", "PNP", "SOT223"), ("BCP52", "PNP", "SOT223"),
    ("BCP53", "PNP", "SOT223"), ("BCP54", "NPN", "SOT223"),
    ("BCP55", "NPN", "SOT223"), ("BCP56", "NPN", "SOT223"),
    ("BCP68", "PNP", "SOT223"), ("BCP69", "NPN", "SOT223"),
    # PBSS (low VCEsat) — Nexperia's space/auto family
    ("PBSS4032T", "NPN", "SOT23"), ("PBSS4140T", "NPN", "SOT223"),
    ("PBSS4160T", "NPN", "SOT223"), ("PBSS4350T", "NPN", "SOT223"),
    ("PBSS4350Z", "NPN", "SOT223"), ("PBSS4360T", "NPN", "SOT223"),
    ("PBSS5032T", "PNP", "SOT23"), ("PBSS5140T", "PNP", "SOT223"),
    ("PBSS5160T", "PNP", "SOT223"), ("PBSS5350T", "PNP", "SOT223"),
    ("PBSS5360T", "PNP", "SOT223"),
    # PBHV (high voltage)
    ("PBHV9050T", "NPN", "SOT223"), ("PBHV8540T", "NPN", "SOT223"),
    ("PBHV8115T", "NPN", "SOT89"),
    # PXTA / PXTB — high-voltage SOT-89
    ("PXTA42", "NPN", "SOT89"), ("PXTA92", "PNP", "SOT89"),
    ("PXTA44", "NPN", "SOT89"), ("PXTA94", "PNP", "SOT89"),
    # MMBT - small-signal
    ("MMBT2222A", "NPN", "SOT23"), ("MMBT2907A", "PNP", "SOT23"),
    ("MMBT3904", "NPN", "SOT23"), ("MMBT3906", "PNP", "SOT23"),
    ("MMBT5550", "NPN", "SOT23"), ("MMBT5551", "NPN", "SOT23"),
    ("MMBT5401", "PNP", "SOT23"), ("MMBT5089", "NPN", "SOT23"),
    # PMBT — Nexperia small-signal
    ("PMBT2222A", "NPN", "SOT23"), ("PMBT2907A", "PNP", "SOT23"),
    ("PMBT3904", "NPN", "SOT23"), ("PMBT3906", "PNP", "SOT23"),
    ("PMBT5179", "NPN", "SOT23"), ("PMBT5550", "NPN", "SOT23"),
    ("PMBT5401", "PNP", "SOT23"), ("PMBTH10", "NPN", "SOT23"),
    # PMSS / PMST — bias resistor variants
    ("PMSS3904", "NPN", "SOT23"), ("PMSS3906", "PNP", "SOT23"),
    ("PMSTA42", "NPN", "SOT23"), ("PMSTA92", "PNP", "SOT23"),
    # 2N / dual etc
    ("BCV61", "NPN", "SOT143"), ("BCV62", "PNP", "SOT143"),
    ("BCV46", "PNP", "SOT223"), ("BCV47", "NPN", "SOT223"),
    ("BCV49", "NPN", "SOT223"),
    # PHPT — low-VCEsat power BJT (DPAK/SOT223)
    ("PHPT60410PYS", "NPN", "SOT223"), ("PHPT60815PYS", "NPN", "SOT223"),
    ("PHPT61003NY", "NPN", "DPAK"), ("PHPT60410NY", "NPN", "DPAK"),
    # 2PB-series Nexperia
    ("2PB710", "PNP", "SOT23"), ("2PD601", "NPN", "SOT23"),
    ("2PD602", "NPN", "SOT23"),
]


def parse_limiting(pdf_bytes: bytes) -> dict:
    out = {}
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            chunks = [p.extract_text() or "" for p in pdf.pages[:3]]
        text = "\n".join(chunks)
    except Exception:
        return out
    if not text:
        return out

    def gv(p):
        m = re.search(p + r"[^\n]*?" + _NUM + r"\s*V\b", text, re.I)
        return abs(float(m.group(1))) if m else None

    def gi(p):
        m = re.search(p + r"[^\n]*?" + _NUM + r"\s*(m?A)\b", text, re.I)
        if not m:
            return None
        v = abs(float(m.group(1)))
        if m.group(2).lower() == "ma":
            v /= 1000
        return v

    if (v := gv(r"collector-?emitter\s+open\s+base")) is not None:
        out["vceo_v"] = v
    if (v := gv(r"collector-?base\s+open\s+emitter")) is not None:
        out["vcbo_v"] = v
    if (v := gv(r"emitter-?base\s+open\s+collector")) is not None:
        out["vebo_v"] = v
    if (i := gi(r"\bcollector\s+current\b")) is not None:
        out["ic_max_a"] = i
    if (i := gi(r"peak\s+collector\s+current")) is not None:
        out["icm_peak_a"] = i

    m = re.search(r"junction\s+temperature[^\n]*?" + _NUM + r"\s*°?C", text, re.I)
    if m:
        out["tj_max_c"] = int(float(m.group(1)))
    m = re.search(r"total\s+power\s+dissipation[^\n]*?" + _NUM + r"\s*(m?W)",
                  text, re.I)
    if m:
        v = float(m.group(1))
        if m.group(2).lower() == "mw":
            v /= 1000
        out["ptot_w"] = v

    quals = sorted({kw for kw in
                    ["AEC-Q101", "AEC-Q100", "automotive", "Automotive",
                     "MIL-PRF", "Hi-Rel"]
                    if kw in text})
    if quals:
        out["qualifications"] = list(quals)
    return out


def try_pdf(client: httpx.Client, mpn: str) -> tuple[str, bytes] | None:
    cached = os.path.join(PDF_DIR, f"{mpn}.pdf")
    if os.path.exists(cached) and os.path.getsize(cached) > 5000:
        with open(cached, "rb") as fh:
            return f"cache://{cached}", fh.read()
    for v in (f"{mpn}.pdf", f"{mpn}_SER.pdf"):
        url = f"https://assets.nexperia.com/documents/data-sheet/{v}"
        r = client.get(url)
        if (r.status_code == 200 and "pdf" in r.headers.get("content-type", "")
                and len(r.content) > 5000):
            with open(cached, "wb") as fh:
                fh.write(r.content)
            return url, r.content
    return None


def upsert(conn: sqlite3.Connection, mpn: str, polarity: str, pkg: str,
           ds_url: str, specs: dict) -> str:
    row = conn.execute(
        "SELECT id FROM products WHERE site_id='nexperia' AND external_id=?",
        (mpn,)
    ).fetchone()
    full = {**specs, "polarity": polarity, "package": pkg}
    metadata = {"datasheet_url": ds_url} if ds_url else {}
    if row is None:
        conn.execute(
            "INSERT INTO products (id, site_id, external_id, name, brand, "
            "category, description, specs, url, metadata, device_type, availability) "
            "VALUES (?,'nexperia',?,?,'Nexperia','Bipolar Transistors > BJT',?,?,?,?,'bjt','active')",
            (str(uuid.uuid4()), mpn, mpn, f"{polarity} BJT in {pkg}",
             json.dumps(full, ensure_ascii=False),
             f"https://www.nexperia.com/product/{mpn}",
             json.dumps(metadata)),
        )
        return "ins"
    conn.execute(
        "UPDATE products SET specs=?, device_type='bjt', metadata=? "
        "WHERE site_id='nexperia' AND external_id=?",
        (json.dumps(full, ensure_ascii=False), json.dumps(metadata), mpn),
    )
    return "upd"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/products.db")
    ap.add_argument("--limit", type=int, default=len(SEED_BJTS))
    ap.add_argument("--delay", type=float, default=0.4)
    args = ap.parse_args()

    os.makedirs(PDF_DIR, exist_ok=True)
    conn = sqlite3.connect(args.db)
    ins = upd = no_pdf = 0

    with httpx.Client(headers={"User-Agent": UA},
                      follow_redirects=True, timeout=25.0) as c:
        for mpn, pol, pkg in SEED_BJTS[: args.limit]:
            res = try_pdf(c, mpn)
            if res is None:
                no_pdf += 1
                print(f"  {mpn:14} ❌")
                continue
            ds_url, pdf_bytes = res
            specs = parse_limiting(pdf_bytes)
            op = upsert(conn, mpn, pol, pkg,
                        "" if ds_url.startswith("cache://") else ds_url, specs)
            conn.commit()
            if op == "ins":
                ins += 1
            else:
                upd += 1
            print(f"  {mpn:14} {op}  vceo={specs.get('vceo_v')} "
                  f"ic={specs.get('ic_max_a')} tj={specs.get('tj_max_c')}")
            if not ds_url.startswith("cache://"):
                time.sleep(args.delay)

    print(f"\n[done] inserted={ins} updated={upd} no_pdf={no_pdf}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
