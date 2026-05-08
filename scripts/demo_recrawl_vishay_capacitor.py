"""Demo re-crawl: Vishay capacitors with full parametric specs.

Two strands:
  1) Tantalum: catalog JSON has T-coded parametric fields (T9505=cap,
     T1112=voltage, T2018=mount, T2019=case, T9000=dielectric, P3315=case_size,
     P5897=dimensions). We decode and update specs. Tantalum is space-relevant
     (MIL-PRF-55365 covers solid tantalum chip caps).
  2) MLCC SMD: catalog JSON returns 0 (Vishay's ceramic database is series-
     level only). We pull product links from the HTML index and store
     series headline + dielectric mentions + applications text.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
API = "https://www.vishay.com/_next/data/vishay-nextjs-ic/en"
WEB = "https://www.vishay.com"


# Tantalum T-code -> human label (from observed catalog values).
T_LABELS = {
    "T9000": "dielectric",        # "Tantalum, Wet" / "Tantalum, Solid"
    "T2018": "mounting_tech",     # "Surface Mount" / "Through Hole"
    "T2019": "package_form",      # "Chip" / "Axial" / "Radial"
    "T9505": "capacitance_uF",
    "T1112": "voltage_max_V",
    "P3315": "case_code",
    "P5897": "dimensions_mm",
    "P1001": "series",
}


def html_unescape(s: str) -> str:
    return (s.replace("&#181;", "µ")
             .replace("&#8211;", "-")
             .replace("&amp;", "&"))


def parse_tantalum(item: dict) -> dict:
    out = {}
    for code, label in T_LABELS.items():
        v = item.get(code)
        if v in (None, "", []):
            continue
        if label == "capacitance_uF":
            # value like "<!-- 22000--> 22 µF" → 22 µF
            v = re.sub(r"<!--.*?-->", "", str(v)).strip()
        out[label] = html_unescape(str(v).strip())
    return out


def matches_cap_demo(specs: dict) -> bool:
    """Want: 10µF, 10-50V, surface-mount tantalum (Molded or Conformal Coated)."""
    cap = (specs.get("capacitance_uF") or "").lower()
    if not (cap.startswith("10 µf") or cap.startswith("10µf")):
        return False
    try:
        v = float((specs.get("voltage_max_V") or "0"))
    except (TypeError, ValueError):
        v = 0
    if not (10 <= v <= 50):
        return False
    if "surface" not in (specs.get("mounting_tech") or "").lower():
        return False
    return True


def update_db_specs(conn: sqlite3.Connection, docid: str, specs: dict,
                    insert_extras: dict | None = None) -> str:
    """Update specs JSON for an existing row; return 'updated' / 'inserted' / 'skipped'.

    If the row doesn't exist and insert_extras is given, insert a new row.
    """
    cur = conn.execute(
        "SELECT specs FROM products WHERE site_id='vishay' AND external_id=?",
        (docid,),
    )
    row = cur.fetchone()
    if row is not None:
        existing = {}
        try:
            existing = json.loads(row[0]) if row[0] else {}
        except Exception:
            pass
        merged = {**existing, **specs}
        conn.execute(
            "UPDATE products SET specs=? WHERE site_id='vishay' AND external_id=?",
            (json.dumps(merged, ensure_ascii=False), docid),
        )
        return "updated"
    if insert_extras is None:
        return "skipped"
    import uuid
    conn.execute(
        "INSERT INTO products (id, site_id, external_id, name, brand, "
        "category, description, specs, url, metadata, device_type) "
        "VALUES (?, 'vishay', ?, ?, 'Vishay', ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            docid,
            insert_extras.get("name") or specs.get("series") or f"docid-{docid}",
            insert_extras.get("category") or "Capacitors > Ceramic > Multilayer SMD",
            insert_extras.get("description") or "",
            json.dumps(specs, ensure_ascii=False),
            insert_extras.get("url") or f"https://www.vishay.com/en/product/{docid}/",
            json.dumps(insert_extras.get("metadata") or {}),
            insert_extras.get("device_type") or "capacitor",
        ),
    )
    return "inserted"


def run_tantalum(client: httpx.Client, conn: sqlite3.Connection,
                 limit: int, dry_run: bool) -> int:
    print("[tantalum] fetching catalog...")
    r = client.get(f"{API}/capacitors/tantalum.json")
    r.raise_for_status()
    items = r.json().get("pageProps", {}).get("paramResults", []) or []
    print(f"[tantalum] {len(items)} parametric rows")

    by_docid: dict[str, dict] = {}
    for it in items:
        d = str(it.get("P1000"))
        specs = parse_tantalum(it)
        if not matches_cap_demo(specs):
            continue
        # keep first hit per docid
        if d not in by_docid:
            by_docid[d] = specs

    print(f"[tantalum] {len(by_docid)} unique series match 10µF / 10-50V / SMD chip")

    updated = inserted = 0
    for d, specs in list(by_docid.items())[:limit]:
        if not dry_run:
            extras = {
                "name": specs.get("series") or f"docid-{d}",
                "category": "Capacitors > Tantalum",
                "description": f"Vishay tantalum capacitor: "
                               f"{specs.get('capacitance_uF')} / "
                               f"{specs.get('voltage_max_V')}V "
                               f"{specs.get('package_form')}",
                "device_type": "capacitor",
            }
            res = update_db_specs(conn, d, specs, insert_extras=extras)
            conn.commit()
            if res == "updated":
                updated += 1
            elif res == "inserted":
                inserted += 1
        view = {k: specs.get(k) for k in (
            "series", "capacitance_uF", "voltage_max_V",
            "package_form", "case_code", "dimensions_mm",
        ) if specs.get(k)}
        print(f"  docid={d}  {view}")
    print(f"[tantalum] updated={updated} inserted={inserted}")
    return updated + inserted


def run_mlcc(client: httpx.Client, conn: sqlite3.Connection,
             limit: int, dry_run: bool) -> int:
    """MLCC SMD: pull docids from HTML and tag series + dielectric."""
    print("[mlcc] fetching ceramic-multilayer-smd HTML...")
    r = client.get(f"{WEB}/en/capacitors/ceramic/ceramic-multilayer-smd/")
    r.raise_for_status()
    docids = sorted(set(int(d) for d in re.findall(
        r'href="/(?:en/)?product/(\d+)', r.text)))
    print(f"[mlcc] {len(docids)} MLCC SMD docids")

    updated = 0
    for d in docids[:limit]:
        try:
            jr = client.get(f"{API}/product/{d}.json")
            if jr.status_code != 200:
                continue
            data = jr.json().get("pageProps", {})
            cor = (data.get("pCorResults") or [{}])[0]
            apps = re.sub(r"<[^>]+>", " ", html_unescape(cor.get("applications") or ""))
            apps = re.sub(r"\s+", " ", apps).strip()[:200]

            # Look for dielectric & MIL/space mentions in the product HTML
            wr = client.get(f"{WEB}/en/product/{d}/")
            html = wr.text if wr.status_code == 200 else ""
            dielectrics = sorted(set(re.findall(
                r"\b(X7R|X5R|X8R|NP0|C0G|Y5V|Z5U)\b", html)))
            qual_keywords = []
            for kw in ["DLA", "DSCC", "MIL-PRF", "MIL-STD",
                       "Hi-Rel", "ESCC", "AEC-Q200", "Class 3"]:
                if kw.lower() in html.lower():
                    qual_keywords.append(kw)

            specs = {
                "series": cor.get("headline"),
                "title": html_unescape(cor.get("title") or ""),
                "dielectric": dielectrics,
                "qualifications": qual_keywords,
                "applications": apps,
                "device_class": "ceramic_mlcc",
            }
            if not dry_run:
                extras = {
                    "name": cor.get("headline") or f"docid-{d}",
                    "category": "Capacitors > Ceramic > Multilayer SMD",
                    "description": html_unescape(cor.get("title") or ""),
                    "device_type": "capacitor",
                    "url": f"{WEB}/en/product/{d}/",
                }
                res = update_db_specs(conn, str(d), specs, insert_extras=extras)
                conn.commit()
                if res in ("updated", "inserted"):
                    updated += 1
            print(f"  docid={d}  series={specs['series']!r}  "
                  f"dielectric={dielectrics}  qual={qual_keywords}")
            time.sleep(0.3)
        except Exception as e:
            print(f"  docid={d}: {type(e).__name__}: {e}")
    print(f"[mlcc] db updated: {updated}")
    return updated


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/products.db")
    ap.add_argument("--limit-tantalum", type=int, default=25)
    ap.add_argument("--limit-mlcc", type=int, default=25)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    with httpx.Client(headers={"User-Agent": UA},
                      follow_redirects=True, timeout=30.0) as c:
        a = run_tantalum(c, conn, args.limit_tantalum, args.dry_run)
        print()
        b = run_mlcc(c, conn, args.limit_mlcc, args.dry_run)
    print(f"\n[done] tantalum updated={a}  mlcc updated={b}  dry_run={args.dry_run}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
