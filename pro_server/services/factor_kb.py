from __future__ import annotations
import json
import sqlite3
from typing import List, Optional

from ..schemas import FactorOut, FactorSource
from ..settings import pro_settings


def _row_to_factor(row: sqlite3.Row) -> FactorOut:
    """Convert sqlite3 Row to FactorOut.
    Row order: id, factor_name, part_type, weight, rationale, sources, direction, thresholds, updated_at
    """
    sources_raw = row["sources"]
    try:
        sources_list = json.loads(sources_raw) if sources_raw else []
    except (json.JSONDecodeError, TypeError):
        sources_list = []

    thresholds_raw = row["thresholds"]
    try:
        thresholds = json.loads(thresholds_raw) if thresholds_raw else {}
    except (json.JSONDecodeError, TypeError):
        thresholds = {}

    return FactorOut(
        factor_name=row["factor_name"],
        part_type=row["part_type"],
        weight=row["weight"],
        direction=row["direction"] or "",
        rationale=row["rationale"] or "",
        sources=[FactorSource(**s) for s in sources_list if isinstance(s, dict)],
        thresholds=thresholds if thresholds else None,
    )


def load_factors(part_type: str = "bjt") -> List[FactorOut]:
    """Read all space_factors rows for the given part_type, ordered by weight DESC."""
    conn = sqlite3.connect(pro_settings.screening_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM space_factors WHERE part_type=? ORDER BY weight DESC",
            (part_type,),
        ).fetchall()
        return [_row_to_factor(r) for r in rows]
    finally:
        conn.close()


def get_factor(factor_name: str, part_type: str = "bjt") -> Optional[FactorOut]:
    """Fetch one factor by name; None if not found."""
    conn = sqlite3.connect(pro_settings.screening_db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM space_factors WHERE factor_name=? AND part_type=?",
            (factor_name, part_type),
        ).fetchone()
        return _row_to_factor(row) if row else None
    finally:
        conn.close()


if __name__ == "__main__":
    from ..migrations import run_migrations
    run_migrations(pro_settings.screening_db_path)
    factors = load_factors("bjt")
    print(f"load_factors('bjt') returned {len(factors)} items")
    print("factor_kb OK")
