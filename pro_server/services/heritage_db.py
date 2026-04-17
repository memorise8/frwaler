from __future__ import annotations
import json
import sqlite3
from typing import Any, Dict, List, Optional

from ..settings import pro_settings

# In-process cache
_CACHE: Dict[str, Any] = {"count": -1, "part_type": None, "rows": None, "vectors": None}

# Parameter keys for vectorization (order-sensitive — scorer depends on this order)
VECTOR_KEYS = [
    "vceo_v", "vcbo_v", "vebo_v", "ic_max_a", "hfe_min",
    "icbo_a_at_vcb", "ft_hz", "pd_w", "tj_max_c",
]

MOSFET_VECTOR_KEYS = [
    "bvdss_v", "vgs_th_v", "rds_on_ohm", "id_max_a",
    "idss_a", "qg_c", "pd_w", "tj_max_c",
]

_QUAL_SCORES = {
    "JANS": 1.0,
    "JANSR": 0.95,
    "JANTXV": 0.9,
    "JANTX": 0.85,
    "SMD-5962": 0.85,
    "ESCC": 0.9,
    "MIL-PRF-19500": 0.75,
    "COTS-upscreened": 0.6,
}


def qual_level_score(qual_level: Optional[str]) -> float:
    """Map qual_level string → 0..1 score for k-NN weighting."""
    if not qual_level:
        return 0.5
    return _QUAL_SCORES.get(qual_level, 0.5)


def _fetch_rows(part_type: str) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(pro_settings.screening_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT mpn, manufacturer, qual_level, parameters, source_url "
            "FROM heritage_parts WHERE part_type=?",
            (part_type,),
        ).fetchall()
        result = []
        for r in rows:
            params_raw = r["parameters"]
            try:
                params = json.loads(params_raw) if params_raw else {}
            except (json.JSONDecodeError, TypeError):
                params = {}
            result.append(
                {
                    "mpn": r["mpn"],
                    "manufacturer": r["manufacturer"],
                    "qual_level": r["qual_level"],
                    "parameters": params,
                    "source_url": r["source_url"],
                }
            )
        return result
    finally:
        conn.close()


def _get_count(part_type: str) -> int:
    conn = sqlite3.connect(pro_settings.screening_db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM heritage_parts WHERE part_type=?", (part_type,)
        ).fetchone()[0]
    finally:
        conn.close()


def _build_vectors(rows: List[Dict[str, Any]],
                   vector_keys: Optional[List[str]] = None) -> List[List[Optional[float]]]:
    keys = vector_keys if vector_keys is not None else VECTOR_KEYS
    vectors = []
    for row in rows:
        params = row["parameters"]
        vectors.append([params.get(k) for k in keys])
    return vectors


def list_heritage(part_type: str = "bjt") -> List[Dict[str, Any]]:
    """Return list of heritage part dicts. Cache invalidated when COUNT(*) changes."""
    current_count = _get_count(part_type)
    if _CACHE["count"] != current_count or _CACHE["part_type"] != part_type:
        rows = _fetch_rows(part_type)
        _CACHE["count"] = current_count
        _CACHE["part_type"] = part_type
        _CACHE["rows"] = rows
        _CACHE["vectors"] = _build_vectors(rows)
    return _CACHE["rows"]  # type: ignore[return-value]


def get_vectors(
    part_type: str = "bjt",
    vector_keys: Optional[List[str]] = None,
) -> tuple[List[Dict[str, Any]], List[List[Optional[float]]]]:
    """Returns (heritage_rows, vectors) aligned with vector_keys (default VECTOR_KEYS for bjt)."""
    list_heritage(part_type)  # ensures rows cache is warm
    rows = _CACHE["rows"]
    vkeys = vector_keys if vector_keys is not None else (
        MOSFET_VECTOR_KEYS if part_type == "mosfet" else VECTOR_KEYS
    )
    vectors = _build_vectors(rows, vkeys)
    return rows, vectors  # type: ignore[return-value]


if __name__ == "__main__":
    from ..migrations import run_migrations
    run_migrations(pro_settings.screening_db_path)
    rows = list_heritage("bjt")
    print(f"list_heritage('bjt') returned {len(rows)} rows")
    parts, vecs = get_vectors("bjt")
    print(f"get_vectors('bjt') returned {len(parts)} parts, {len(vecs)} vectors")
    print("heritage_db OK")
