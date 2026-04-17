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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS screening_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id TEXT NOT NULL,
            factor_name TEXT,
            rating TEXT NOT NULL,
            comment TEXT,
            license_key TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (report_id) REFERENCES screening_results(id),
            UNIQUE(report_id, factor_name, license_key)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_feedback_report ON screening_feedback(report_id)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            license_key TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (license_key) REFERENCES licenses(key)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)
    """)

    # Add email and ip columns to licenses table if they don't exist
    for col_sql in [
        "ALTER TABLE licenses ADD COLUMN email TEXT",
        "ALTER TABLE licenses ADD COLUMN ip TEXT",
    ]:
        try:
            conn.execute(col_sql)
        except Exception:
            pass  # Column already exists

    conn.commit()
    conn.close()


def _seed_factors(conn, factors_json_path: str, part_type: str) -> None:
    count = conn.execute(
        "SELECT COUNT(*) FROM space_factors WHERE part_type=?", (part_type,)
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


def _seed_heritage(conn, heritage_json_path: str, part_type: str) -> None:
    count = conn.execute(
        "SELECT COUNT(*) FROM heritage_parts WHERE part_type=?", (part_type,)
    ).fetchone()[0]
    if count == 0:
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


def seed_if_empty(db_path: str, factors_json_path: str, heritage_json_path: str,
                  mosfet_factors_json_path: str = "",
                  mosfet_heritage_json_path: str = "") -> None:
    conn = sqlite3.connect(db_path)

    _seed_factors(conn, factors_json_path, "bjt")
    _seed_heritage(conn, heritage_json_path, "bjt")

    if mosfet_factors_json_path:
        _seed_factors(conn, mosfet_factors_json_path, "mosfet")
    if mosfet_heritage_json_path:
        _seed_heritage(conn, mosfet_heritage_json_path, "mosfet")

    conn.close()
