#!/usr/bin/env python3
"""Classify products by device type using rule-based keyword matching."""
import argparse, sqlite3, re, sys, time

DEVICE_RULES = {
    # Order matters — first match wins. More specific types BEFORE generic ones.
    "bjt": {
        "category_patterns": [r"bipolar", r"\bbjt\b", r"bipolar.transistor"],
        "name_patterns": [
            r"^2N[0-9]{3,4}",   # 2N2222, 2N3904, 2N3055
            r"^BC[0-9]{3}",     # BC547, BC557, BC817
            r"^BD[0-9]{3}",     # BD139, BD140
            r"^BF[0-9]{3}",     # BF199, BF862
            r"^MJ[EH]?[0-9]{3,5}", # MJE, MJH power BJTs
            r"^TIP[0-9]{2,3}",  # TIP31, TIP120
            r"^MMBT",           # MMBT3904
            r"^KSP",            # KSP2222
            r"^PN[0-9]{4}",     # PN2222A
            r"^MPSA",           # MPSA42
            r"^ZTX",            # ZTX450
        ],
        "description_patterns": [r"bipolar\s+junction", r"\bbjt\b", r"\bnpn\b.*transistor", r"\bpnp\b.*transistor"],
    },
    "mosfet": {
        "category_patterns": [r"mosfet", r"power\s+mosfet", r"small\s+signal\s+mosfet"],
        "name_patterns": [
            r"^IRF[A-Z]?[0-9]",   # IRF540, IRFP250
            r"^IRHL",              # IRHLNA, IRHLUB (rad-hard)
            r"^IRHN",              # IRHNJ (rad-hard)
            r"^BSS[0-9]",         # BSS138
            r"^BSP[0-9]",
            r"^FD[A-Z]?[0-9]",   # FDS, FDA
            r"^SI[0-9]{4}",       # SI4410
            r"^AO[0-9]{4}",       # AO3400
            r"^DMN[0-9]",
            r"^DMP[0-9]",
            r"^2N7[0-9]{3}",     # 2N7000, 2N7002
            r"^PSMN",
            r"^BUK[0-9]",
            r"^STF[0-9]",
            r"^STP[0-9]",
            r"^IPB[0-9]",
            r"^IPD[0-9]",
            r"^IPA[0-9]",
        ],
        "description_patterns": [r"\bmosfet\b", r"n-channel.*fet", r"p-channel.*fet", r"power\s+fet"],
    },
    "diode": {
        "category_patterns": [r"diode", r"rectifier", r"zener", r"schottky", r"tvs", r"esd\s+protection", r"transient.*suppress"],
        "name_patterns": [
            r"^1N[0-9]{4}",       # 1N4148, 1N5819
            r"^BZX[0-9]",        # BZX84 zener
            r"^BAT[0-9]",        # BAT54 schottky
            r"^BAV[0-9]",        # BAV70
            r"^BAS[0-9]",
            r"^SM[AF][0-9]",     # SMA, SMAF
            r"^SS[0-9]{2}",      # SS14, SS34
            r"^STPS[0-9]",       # STPS schottky
            r"^MURS[0-9]",
            r"^ES[0-9]",
            r"^US[0-9]",
            r"^PMEG",            # Nexperia schottky
            r"^PESD",            # Nexperia ESD
            r"^PRTR",
        ],
        "description_patterns": [r"\bdiode\b", r"\brectifier\b", r"\bzener\b", r"\bschottky\b", r"\btvs\b", r"esd\s+protect"],
    },
    "ic_power": {
        "category_patterns": [r"\bigbt\b", r"power\s+module", r"gate\s+driver"],
        "name_patterns": [
            r"^FP[0-9]+R",       # Infineon IGBT modules
            r"^IKW[0-9]",
            r"^IGW[0-9]",
            r"^FF[0-9]+R",       # Infineon IGBT
        ],
        "description_patterns": [r"\bigbt\b", r"gate\s+driver", r"power\s+module", r"half.?bridge"],
    },
    "ic_linear": {
        "category_patterns": [r"regulator", r"ldo", r"op.?amp", r"comparator", r"amplifier(?!.*power)"],
        "name_patterns": [
            r"^LM[0-9]{3,4}",    # LM317, LM7805
            r"^TL[0-9]{3}",      # TL431, TL072
            r"^LM[CG]?[0-9]",
            r"^OPA[0-9]{3,4}",   # OPA2134
            r"^INA[0-9]{3}",     # INA219
            r"^REF[0-9]",
            r"^TPS[0-9]{4,5}",   # TPS series (TI regulators)
            r"^LP[0-9]{4}",      # LP2985
            r"^MCP[0-9]{4}",
            r"^MAX[0-9]{4}",
            r"^AD[0-9]{4}",
        ],
        "description_patterns": [r"\bldo\b", r"voltage\s+regulator", r"op.?amp", r"operational\s+amplifier", r"\bcomparator\b"],
    },
    "ic_logic": {
        "category_patterns": [r"\blogic\b", r"\bgate\b", r"\bbuffer\b", r"\blatch\b", r"flip.?flop", r"level\s+shift"],
        "name_patterns": [
            r"^SN74",            # TI 74-series
            r"^CD[0-9]{4}",
            r"^74[AHLS]C",
            r"^NX[0-9]",
        ],
        "description_patterns": [r"logic\s+gate", r"\bbuffer\b.*output", r"level\s+shift", r"bus\s+transceiver"],
    },
    "optoelectronics": {
        "category_patterns": [r"opto", r"infrared", r"photo", r"\bled\b", r"ir\s+receiver", r"ir\s+emitter"],
        "name_patterns": [
            r"^TSOP",            # IR receivers
            r"^TSAL",            # IR emitters
            r"^SFH[0-9]",
            r"^OSRAM",
            r"^VSMB",
        ],
        "description_patterns": [r"\bphotodiode\b", r"\bphototransistor\b", r"infrared\s+emit", r"\bir\s+receiver\b", r"led\s+driver"],
    },
    "resistor": {
        "category_patterns": [r"resistor", r"thick\s+film", r"thin\s+film"],
        "name_patterns": [],
        "description_patterns": [r"\bresistor\b", r"thick\s+film\s+chip"],
    },
    "capacitor": {
        "category_patterns": [r"capacitor", r"\bmlcc\b"],
        "name_patterns": [],
        "description_patterns": [r"\bcapacitor\b", r"\bmlcc\b"],
    },
}


def classify_one(category: str, name: str, description: str) -> str:
    cat_lower = (category or "").lower()
    name_upper = (name or "").upper().strip()
    desc_lower = (description or "").lower()

    # Pass 1: category
    for dtype, rules in DEVICE_RULES.items():
        for pat in rules["category_patterns"]:
            if re.search(pat, cat_lower, re.IGNORECASE):
                return dtype

    # Pass 2: name/MPN pattern
    for dtype, rules in DEVICE_RULES.items():
        for pat in rules["name_patterns"]:
            if re.search(pat, name_upper, re.IGNORECASE):
                return dtype

    # Pass 3: description keywords
    for dtype, rules in DEVICE_RULES.items():
        for pat in rules["description_patterns"]:
            if re.search(pat, desc_lower, re.IGNORECASE):
                return dtype

    return "other"


def print_stats(counts: dict, total: int) -> None:
    print("\n=== 소자 유형 분류 결과 ===")
    classified = total - counts.get("other", 0)
    for dtype, count in sorted(counts.items(), key=lambda x: -x[1]):
        pct = count / total * 100 if total else 0
        print(f"{dtype:<20s}: {count:>7,}건 ({pct:.1f}%)")
    print("─" * 35)
    print(f"{'total':<20s}: {total:>7,}건")
    classified_pct = classified / total * 100 if total else 0
    print(f"{'classified':<20s}: {classified:>7,}건 ({classified_pct:.1f}%)")


def main():
    parser = argparse.ArgumentParser(description="Classify products by device type")
    parser.add_argument("--db", default="data/products.db")
    parser.add_argument("--dry-run", action="store_true", help="Show stats without updating DB")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # 1. Add column if missing (idempotent)
    if not args.dry_run:
        try:
            conn.execute("ALTER TABLE products ADD COLUMN device_type TEXT")
            conn.commit()
            print("Added device_type column.")
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                pass  # already exists
            else:
                raise

    # 2. Fetch all rows
    print(f"Fetching rows from {args.db}...")
    t0 = time.time()
    rows = conn.execute("SELECT id, category, name, description FROM products").fetchall()
    total = len(rows)
    print(f"Loaded {total:,} rows in {time.time()-t0:.1f}s")

    # 3. Classify each
    print("Classifying...")
    t1 = time.time()
    results = []
    counts: dict = {}
    for row in rows:
        dtype = classify_one(row["category"], row["name"], row["description"])
        results.append((dtype, row["id"]))
        counts[dtype] = counts.get(dtype, 0) + 1

    print(f"Classified {total:,} rows in {time.time()-t1:.1f}s")

    # 4. If not dry-run: batch UPDATE in 1000-row chunks
    if not args.dry_run:
        print("Updating DB...")
        t2 = time.time()
        chunk_size = 1000
        for i in range(0, len(results), chunk_size):
            chunk = results[i:i + chunk_size]
            conn.executemany("UPDATE products SET device_type = ? WHERE id = ?", chunk)
        conn.commit()
        conn.close()
        print(f"Updated {total:,} rows in {time.time()-t2:.1f}s")
    else:
        conn.close()
        print("[dry-run] DB not modified.")

    # 5. Print stats
    print_stats(counts, total)


if __name__ == "__main__":
    main()
