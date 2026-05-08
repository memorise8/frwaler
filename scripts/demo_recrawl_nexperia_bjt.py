"""Demo re-crawl: Nexperia SMD BJT candidates (Vceo>=60V, Ic>=1A).

Current DB has 0 Nexperia BJTs (crawler missed the family). This script:
  1) Seeds known BJT MPNs in the SMD power BJT range (Vceo >= 60V, Ic >= 1A),
  2) Verifies datasheet PDF availability via Nexperia's _SER.pdf / .pdf URL pattern,
  3) Parses Vceo / Ic_max / Tj_max / Ptot from the 'Limiting values' table on
     page 1 of each PDF using pdfplumber,
  4) Inserts/updates rows in products.db with device_type='bjt' and full specs.

This proves: "we can extract BJT Vceo/Ic by re-crawling vendor sites" without
needing any paid API.
"""
from __future__ import annotations

import argparse
import io
import json
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

# Curated NPN/PNP SMD BJT candidates for the 60V+ / 1A+ band.
# Series datasheets cover all gain bins (e.g., -10/-16/-25/-40).
SEED_BJTS = [
    # (mpn_for_url, friendly_label, type, expected_pkg)
    ("PBSS5160T",      "PNP 60V 1A SOT223",        "PNP",  "SOT223"),
    ("PBSS4160T",      "NPN 60V 1A SOT223",        "NPN",  "SOT223"),
    ("BCP56",          "NPN 80V 1A SOT223",        "NPN",  "SOT223"),
    ("BCP55",          "NPN 60V 1A SOT223",        "NPN",  "SOT223"),
    ("BCP53",          "PNP 80V 1A SOT223",        "PNP",  "SOT223"),
    ("BCP52",          "PNP 60V 1A SOT223",        "PNP",  "SOT223"),
    ("BCP69",          "NPN 20V 1.5A SOT223",      "NPN",  "SOT223"),
    ("BCP68",          "PNP 20V 1.5A SOT223",      "PNP",  "SOT223"),
    ("PBHV9050T",      "NPN 90V 0.5A SOT223",      "NPN",  "SOT223"),
    ("PBHV9540T",      "NPN 95V 0.4A SOT223",      "NPN",  "SOT223"),
    ("PBSS4140T",      "NPN 40V 1A SOT223",        "NPN",  "SOT223"),
    ("PBSS5140T",      "PNP 40V 1A SOT223",        "PNP",  "SOT223"),
    ("PXTA42",         "NPN 300V 0.5A SOT89",      "NPN",  "SOT89"),
    ("PXTA92",         "PNP 300V 0.5A SOT89",      "PNP",  "SOT89"),
    ("BCV62",          "PNP dual 30V SOT143",      "PNP",  "SOT143"),
    ("BCV61",          "NPN dual 30V SOT143",      "NPN",  "SOT143"),
]


def try_datasheet_url(client: httpx.Client, mpn: str) -> tuple[str, bytes] | None:
    """Try {MPN}.pdf, then {MPN}_SER.pdf, return (url, pdf_bytes)."""
    for variant in (f"{mpn}.pdf", f"{mpn}_SER.pdf"):
        url = f"https://assets.nexperia.com/documents/data-sheet/{variant}"
        r = client.get(url, follow_redirects=True)
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and "pdf" in ct and len(r.content) > 5000:
            return url, r.content
    return None


_NUM = r"(-?\d+(?:\.\d+)?)"


def parse_limiting_values(pdf_bytes: bytes) -> dict:
    """Extract Vceo, Vcbo, Ic_max, Tj_max, Ptot.

    Nexperia datasheets break the Quick-reference and Limiting-values tables
    so that the symbol's subscript falls on the line below the parameter row,
    e.g.::

        V    collector-emitter open base   -  -  -60 V
        CEO
        voltage

    We match by parameter description ("collector-emitter open base",
    "collector current", etc.) and pull the last signed number before the unit.
    """
    out: dict = {}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text_chunks = []
        for page in pdf.pages[:3]:
            try:
                t = page.extract_text() or ""
                text_chunks.append(t)
            except Exception:
                pass
    text = "\n".join(text_chunks)
    if not text:
        return out

    def grab_v(label_pat: str) -> float | None:
        m = re.search(
            label_pat + r"[^\n]*?" + _NUM + r"\s*V\b",
            text, re.I,
        )
        return abs(float(m.group(1))) if m else None

    def grab_curr(label_pat: str) -> float | None:
        m = re.search(
            label_pat + r"[^\n]*?" + _NUM + r"\s*(m?A)\b",
            text, re.I,
        )
        if not m:
            return None
        v = abs(float(m.group(1)))
        if m.group(2).lower() == "ma":
            v /= 1000.0
        return v

    v = grab_v(r"collector-?emitter\s+open\s+base")
    if v is not None:
        out["vceo_v"] = v

    v = grab_v(r"collector-?base\s+open\s+emitter")
    if v is not None:
        out["vcbo_v"] = v

    v = grab_v(r"emitter-?base\s+open\s+collector")
    if v is not None:
        out["vebo_v"] = v

    ic = grab_curr(r"\bcollector\s+current\b")
    if ic is not None:
        out["ic_max_a"] = ic

    icm = grab_curr(r"peak\s+collector\s+current")
    if icm is not None:
        out["icm_peak_a"] = icm

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

    m = re.search(r"-?55\s*°?C\s*(?:to|~|–)\s*\+?(\d{2,3})\s*°?C", text)
    if m:
        out["temp_range"] = f"-55 to +{m.group(1)}"

    # AEC-Q101 / radiation / military markers
    quals = []
    for kw in ["AEC-Q101", "AEC-Q100", "MIL-PRF", "Hi-Rel", "JANTX",
               "automotive", "Automotive"]:
        if kw in text:
            quals.append(kw)
    if quals:
        out["qualifications"] = sorted(set(quals))

    return out


def upsert_bjt(conn: sqlite3.Connection, mpn: str, label: str,
               polarity: str, package: str, datasheet_url: str | None,
               specs: dict) -> str:
    cur = conn.execute(
        "SELECT id FROM products WHERE site_id='nexperia' AND external_id=?",
        (mpn,),
    )
    row = cur.fetchone()
    full_specs = {
        **specs,
        "polarity": polarity,
        "package": package,
        "label": label,
    }
    metadata = {"datasheet_url": datasheet_url} if datasheet_url else {}

    if row is None:
        conn.execute(
            "INSERT INTO products (id, site_id, external_id, name, brand, "
            "category, description, specs, url, metadata, device_type, availability) "
            "VALUES (?, 'nexperia', ?, ?, 'Nexperia', ?, ?, ?, ?, ?, 'bjt', ?)",
            (
                str(uuid.uuid4()),
                mpn,
                mpn,
                "Bipolar Transistors > Power BJT",
                f"{polarity} {label}",
                json.dumps(full_specs, ensure_ascii=False),
                f"https://www.nexperia.com/product/{mpn}",
                json.dumps(metadata),
                "active",
            ),
        )
        return "inserted"

    conn.execute(
        "UPDATE products SET specs=?, device_type='bjt', metadata=? "
        "WHERE site_id='nexperia' AND external_id=?",
        (json.dumps(full_specs, ensure_ascii=False),
         json.dumps(metadata), mpn),
    )
    return "updated"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/products.db")
    ap.add_argument("--limit", type=int, default=len(SEED_BJTS))
    ap.add_argument("--delay", type=float, default=0.6)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    inserted = updated = no_pdf = 0

    with httpx.Client(headers={"User-Agent": UA},
                      follow_redirects=True, timeout=25.0) as c:
        for mpn, label, polarity, pkg in SEED_BJTS[: args.limit]:
            res = try_datasheet_url(c, mpn)
            if res is None:
                no_pdf += 1
                print(f"  {mpn:12} ❌ no PDF found")
                continue
            url, pdf_bytes = res
            specs = parse_limiting_values(pdf_bytes)
            if not args.dry_run:
                op = upsert_bjt(conn, mpn, label, polarity, pkg, url, specs)
                conn.commit()
                if op == "inserted":
                    inserted += 1
                else:
                    updated += 1
            print(f"  {mpn:12} ✓ pdf={len(pdf_bytes)//1024}KB  "
                  f"vceo={specs.get('vceo_v')}V  ic={specs.get('ic_max_a')}A  "
                  f"tj={specs.get('tj_max_c')}°C  ptot={specs.get('ptot_w')}W  "
                  f"trange={specs.get('temp_range')}")
            time.sleep(args.delay)

    print(f"\n[done] inserted={inserted} updated={updated} no_pdf={no_pdf}  "
          f"dry_run={args.dry_run}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
