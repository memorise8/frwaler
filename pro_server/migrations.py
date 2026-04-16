import json
import os
import sqlite3


def run_migrations(db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS space_factors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            factor_name TEXT NOT NULL,
            part_type TEXT NOT NULL,
            weight REAL NOT NULL,
            rationale TEXT,
            sources TEXT,
            direction TEXT,
            thresholds TEXT,
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(factor_name, part_type)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS heritage_parts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mpn TEXT NOT NULL,
            manufacturer TEXT NOT NULL,
            part_type TEXT,
            qual_level TEXT,
            parameters TEXT,
            heritage_notes TEXT,
            source_url TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(mpn, manufacturer)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_heritage_parts_part_type
        ON heritage_parts (part_type)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS screening_results (
            id TEXT PRIMARY KEY,
            input_mpn TEXT,
            input_source TEXT,
            parameters TEXT,
            factor_scores TEXT,
            overall_score REAL,
            status TEXT,
            confidence REAL,
            heritage_matches TEXT,
            risk_flags TEXT,
            license_key TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_screening_results_license_created
        ON screening_results (license_key, created_at)
    """)
    conn.commit()
    conn.close()


def seed_if_empty(db_path: str, factors_json_path: str, heritage_json_path: str) -> None:
    conn = sqlite3.connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM space_factors WHERE part_type='bjt'"
    ).fetchone()[0]
    if count == 0:
        try:
            with open(factors_json_path, "r") as f:
                factors = json.load(f)
            for row in factors:
                conn.execute(
                    """INSERT OR IGNORE INTO space_factors
                       (factor_name, part_type, weight, rationale, sources, direction, thresholds)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["factor_name"],
                        row["part_type"],
                        row["weight"],
                        row.get("rationale"),
                        json.dumps(row.get("sources", [])),
                        row.get("direction"),
                        json.dumps(row.get("thresholds", {})),
                    ),
                )
            conn.commit()
        except FileNotFoundError:
            print(f"Warning: factors seed file not found: {factors_json_path}")

    heritage_count = conn.execute(
        "SELECT COUNT(*) FROM heritage_parts WHERE part_type='bjt'"
    ).fetchone()[0]
    if heritage_count == 0:
        try:
            with open(heritage_json_path, "r") as f:
                parts = json.load(f)
            for row in parts:
                conn.execute(
                    """INSERT OR IGNORE INTO heritage_parts
                       (mpn, manufacturer, part_type, qual_level, parameters, heritage_notes, source_url)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["mpn"],
                        row["manufacturer"],
                        row.get("part_type"),
                        row.get("qual_level"),
                        json.dumps(row.get("parameters", {})),
                        row.get("heritage_notes"),
                        row.get("source_url"),
                    ),
                )
            conn.commit()
        except FileNotFoundError:
            print(f"Warning: heritage seed file not found: {heritage_json_path}")

    conn.close()
