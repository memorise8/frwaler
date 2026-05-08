"""Extract MLCC ordering-table rows from cached Vishay datasheets.

Reads the 10 PDFs already in data/datasheets/vishay/ and emits a per-variant
parametric record for each (case_size, dielectric, voltage, capacitance, MPN)
combination, then upserts them into products as new rows so the existing
"VJ Hi-Rel Series" series-level row gains many concrete-MPN siblings.

Strategy: VJ Hi-Rel and similar Vishay series each contain
  • a "Capacitance Range" matrix (case × dielectric → cap min/max + voltage)
  • an "Ordering Information / Part Number Decoder" telling how to encode
    capacitance, dielectric, voltage, tolerance, termination into an MPN.

We don't reconstruct every MPN. Instead, we extract the matrix rows so that a
later filter ("0805 / X7R / max cap") becomes a SQL query on real values.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import uuid

import pdfplumber

PDF_DIR = "data/datasheets/vishay"

CASE_RE = re.compile(r"\b(02016|0201|0402|0603|0805|1206|1210|1808|1812|1825|"
                     r"2220|2225|2820|3640)\b")
DIELECTRIC_RE = re.compile(r"\b(C0G|NP0|X5R|X7R|X8R|Y5V|BP|BX|BR|"
                           r"COG\(NP0\)|C0G \(NP0\))\b", re.I)
VOLTAGE_RE = re.compile(r"\b(\d{1,3}(?:\.\d)?)\s*V\b")
# Capacitance with unit, e.g. "1.0 pF", "390 nF", "1.0 µF", "10 μF"
CAP_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(pF|nF|[µuμ]F)",
    re.I,
)


_UNIT = {"pF": 1, "nF": 1000, "µF": 1_000_000, "uF": 1_000_000, "μF": 1_000_000}


def cap_to_pf(value: float, unit: str) -> int:
    return int(value * _UNIT[unit.replace("μ", "µ").replace("u", "µ").lower()
                             .replace("p", "p").replace("n", "n").replace("µ", "µ")
                             .upper()
                             .replace("F", "F")])


def normalize_unit(unit: str) -> str:
    u = unit.lower().replace("μ", "µ").replace("u", "µ")
    return {"pf": "pF", "nf": "nF", "µf": "µF"}[u]


def cap_value_pf(value: float, unit: str) -> float:
    u = normalize_unit(unit)
    return value * {"pF": 1, "nF": 1_000, "µF": 1_000_000}[u]


def find_cap_matrix(text: str) -> list[dict]:
    """Find lines that look like '<dielectric>? <case> <voltage> <cap_min> <cap_max>'.

    Vishay matrix rows look like:
        X7R  1812 500 3.3 nF 1.0 µF
    or w/o leading dielectric (continuation):
        0805 200 150 pF 390 nF

    Returns a list of {dielectric, case, voltage, cap_min_pf, cap_max_pf}.
    """
    rows = []
    current_diel = None
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Pick up dielectric token at the start of a line (sticky)
        m = re.match(r"^(C0G\s*\(NP0\)|C0G|NP0|X[58]R|X7R|Y5V|BP|BX|BR)\s+(.*)",
                     line, re.I)
        if m:
            current_diel = m.group(1).upper().replace(" ", "").replace("(NP0)", "")
            rest = m.group(2)
        else:
            rest = line

        # Try: "<case> <voltage> <cap1> <unit1> <cap2> <unit2>"
        m2 = re.match(
            r"^(\d{4})\s+(\d{1,3})\s+"
            r"(\d+(?:\.\d+)?)\s*(pF|nF|[µuμ]F)\s+"
            r"(\d+(?:\.\d+)?)\s*(pF|nF|[µuμ]F)\b",
            rest, re.I,
        )
        if m2:
            case, volt, cmin, umin, cmax, umax = m2.groups()
            try:
                rows.append({
                    "dielectric": current_diel or "?",
                    "case_size": case,
                    "voltage_v": int(volt),
                    "cap_min_pf": cap_value_pf(float(cmin), umin),
                    "cap_max_pf": cap_value_pf(float(cmax), umax),
                    "cap_min_displ": f"{cmin} {normalize_unit(umin)}",
                    "cap_max_displ": f"{cmax} {normalize_unit(umax)}",
                })
            except KeyError:
                pass
    return rows


def encode_cap_three_digit(cap_pf: float) -> str:
    """Vishay/MIL three-digit capacitance code: significant digits + multiplier."""
    s = f"{cap_pf:.0f}"
    if cap_pf < 10:
        return f"{cap_pf:.1f}"
    if cap_pf < 100:
        return f"{int(cap_pf)}"
    # significant digits + power of 10
    digits = s.lstrip("0")
    if len(digits) <= 2:
        return digits
    return digits[:2] + str(len(digits) - 2)


def build_synth_mpn(series_prefix: str, case: str, diel: str,
                    cap_pf: float, voltage_v: int, tol_code: str = "K") -> str:
    """Construct a *representative* MPN for the case/diel/cap combo.

    Example for VJ Hi-Rel: VJ0805Y106KXAAQ
      VJ + 0805 + Y(=X7R) + 106(=10µF) + K(=10%) + X(=25V code) + A...
    We don't try to be perfect; we just produce something searchable.
    """
    diel_code = {"C0G": "A", "NP0": "A", "X7R": "Y", "X5R": "G",
                 "X8R": "H", "BP": "B", "BX": "X", "BR": "R"}.get(diel, "?")
    return (f"{series_prefix}{case}{diel_code}"
            f"{encode_cap_three_digit(cap_pf)}{tol_code}"
            f"V{voltage_v}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/products.db")
    ap.add_argument("--pdf-dir", default=PDF_DIR)
    args = ap.parse_args()

    if not os.path.isdir(args.pdf_dir):
        print(f"[err] {args.pdf_dir} not found")
        return 1

    conn = sqlite3.connect(args.db)
    pdfs = sorted([f for f in os.listdir(args.pdf_dir) if f.endswith(".pdf")])
    print(f"[mlcc] {len(pdfs)} cached PDFs to mine")

    total_rows = 0
    for fname in pdfs:
        docid = fname.replace(".pdf", "")
        path = os.path.join(args.pdf_dir, fname)
        try:
            with pdfplumber.open(path) as pdf:
                chunks = [p.extract_text() or "" for p in pdf.pages[:6]]
            text = "\n".join(chunks)
        except Exception as e:
            print(f"  {docid}: PDF read err: {e}")
            continue

        # series headline lookup
        srow = conn.execute(
            "SELECT name, specs, metadata FROM products "
            "WHERE site_id='vishay' AND external_id=?", (docid,),
        ).fetchone()
        series_name = srow[0] if srow else f"docid-{docid}"
        try:
            srow_specs = json.loads(srow[1]) if srow and srow[1] else {}
        except Exception:
            srow_specs = {}
        try:
            srow_meta = json.loads(srow[2]) if srow and srow[2] else {}
        except Exception:
            srow_meta = {}

        prefix = "VJ" if series_name.startswith("VJ") else "CDR" \
            if "CDR" in series_name else "GA" if series_name.startswith("GA") \
            else "??"

        rows = find_cap_matrix(text)
        print(f"  {docid} ({series_name[:35]}): {len(rows)} matrix rows")

        for r in rows:
            # store cap range row as its own product entry
            extid = f"{docid}-{r['case_size']}-{r['dielectric']}-{r['voltage_v']}V"
            specs = {
                **srow_specs,  # carry series-level qual/dielectric tags
                "case_size": r["case_size"],
                "dielectric_concrete": r["dielectric"],
                "voltage_v": r["voltage_v"],
                "cap_min_pf": r["cap_min_pf"],
                "cap_max_pf": r["cap_max_pf"],
                "cap_min_displ": r["cap_min_displ"],
                "cap_max_displ": r["cap_max_displ"],
                "series_docid": docid,
                "series_name": series_name,
                "synthetic_mpn_template": build_synth_mpn(
                    prefix, r["case_size"], r["dielectric"],
                    r["cap_max_pf"], r["voltage_v"]),
            }
            existing = conn.execute(
                "SELECT id FROM products WHERE site_id='vishay' AND external_id=?",
                (extid,),
            ).fetchone()
            payload = (
                str(uuid.uuid4()), extid,
                f"{series_name} ({r['case_size']}/{r['dielectric']}/{r['voltage_v']}V)",
                "Capacitors > Ceramic > Multilayer SMD (matrix)",
                f"{r['dielectric']} dielectric, case {r['case_size']}, "
                f"{r['voltage_v']}V, "
                f"{r['cap_min_displ']}-{r['cap_max_displ']}",
                json.dumps(specs, ensure_ascii=False),
                f"https://www.vishay.com/en/product/{docid}/",
                json.dumps(srow_meta),
            )
            if existing is None:
                conn.execute(
                    "INSERT INTO products (id, site_id, external_id, name, brand, "
                    "category, description, specs, url, metadata, "
                    "device_type, availability) "
                    "VALUES (?,'vishay',?,?,'Vishay',?,?,?,?,?,'capacitor','active')",
                    payload,
                )
            else:
                conn.execute(
                    "UPDATE products SET specs=?, description=?, name=? "
                    "WHERE site_id='vishay' AND external_id=?",
                    (payload[5], payload[4], payload[2], extid),
                )
            total_rows += 1
        conn.commit()

    print(f"\n[done] {total_rows} matrix rows materialized")

    # Demo query: 0805 X7R 25V with cap >= 1 µF
    print("\n=== demo: 0805 / X7R / 25V / cap_max >= 1 µF / MIL-PRF ===")
    rs = conn.execute("""
      SELECT external_id, name, specs FROM products
      WHERE site_id='vishay'
        AND device_type='capacitor'
        AND json_extract(specs, '$.case_size')='0805'
        AND json_extract(specs, '$.dielectric_concrete')='X7R'
        AND json_extract(specs, '$.voltage_v') >= 25
      ORDER BY json_extract(specs, '$.cap_max_pf') DESC
      LIMIT 10
    """).fetchall()
    if not rs:
        print("  (no rows — table may need 25V voltage rows; many series only "
              "list 50V+ in matrix)")
    for eid, nm, sp in rs:
        s = json.loads(sp)
        print(f"  {eid:35} max={s.get('cap_max_displ')} V={s.get('voltage_v')}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
