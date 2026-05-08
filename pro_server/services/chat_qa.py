"""Natural-language procurement Q&A backed by the local SQLite catalogs.

Pipeline:
1. parse_intent  -> LLM (gpt-4o-mini, JSON mode) extracts structured filters.
2. search_candidates -> SQL over products.db + heritage_parts, deterministic scoring.
3. generate_answer -> LLM writes a Korean recommendation grounded in the candidates.

The OpenAI client is constructed lazily; import-time use does not require a key.
Errors during OpenAI calls surface a Korean message so the router can return 500.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ----- Constants -----------------------------------------------------------

INTENT_SYSTEM_PROMPT = (
    "당신은 우주/방산용 전자부품 조달 도우미입니다. "
    "사용자의 한국어 또는 영어 자연어 요청을 분석해 검색용 구조화 필터로 변환하세요. "
    "응답은 반드시 지정된 JSON 스키마 그대로 반환합니다. "
    "값이 명확히 지정되지 않으면 null을 사용하세요."
)

INTENT_JSON_SCHEMA_HINT = """
출력 JSON 스키마:
{
  "device_type": "bjt"|"mosfet"|"diode"|"resistor"|"capacitor"|"ic_linear"|"ic_logic"|"ic_power"|"optoelectronics"|"any",
  "filters": {
    "vceo_min": float|null,
    "ic_min": float|null,
    "vds_min": float|null,
    "rds_on_max_mohm": float|null,
    "resistance_ohm": float|null,
    "tolerance_max_pct": float|null,
    "capacitance_uf": float|null,
    "voltage_v": float|null,
    "case_size": string|null,
    "dielectric": string|null,
    "package_smd_only": boolean,
    "temp_min_c": float|null,
    "temp_max_c": float|null,
    "qual_required": ["MIL-PRF","AEC-Q101","AEC-Q200","JANTX","JANSR","Hi-Rel","Class S","Class 3"]|null
  },
  "intent_summary": "추출된 의도 한 줄 요약 (한국어)"
}
""".strip()

ANSWER_SYSTEM_PROMPT = (
    "당신은 우주/방산용 전자부품 조달 전문가입니다. "
    "주어진 후보 부품 리스트(이미 점수 순 정렬됨)와 사용자 질문을 바탕으로 "
    "근거가 명확한 한국어 추천 답변을 작성하세요. 답변은 다음 형식을 따릅니다:\n"
    "1. 추천 부품 1~2개 (MPN과 제조사 명시)\n"
    "2. 핵심 스펙 비교 근거 (VCEO, Ic, RDS(on), 용량, 인증 등)\n"
    "3. 주의사항 (예: 가격/재고 정보는 현재 미보유, 데이터시트 확인 필요)\n"
    "허위 정보는 만들지 말고, 후보에 없는 정보는 '확인 필요'라고 명시하세요."
)

# Surface-mount package whitelist used when the user asks for SMD-only.
SMD_PACKAGES = (
    "SOT23",
    "SOT89",
    "SOT223",
    "SOT143",
    "DPAK",
    "TO-252",
    "TO-263",
    "MELF",
    "BGA",
    "SO-8",
    "SOIC",
    "QFN",
    "TDSON",
    "SuperSO8",
)

KNOWN_QUAL_TOKENS = {
    "MIL-PRF": ["mil-prf", "mil prf", "mil-prf-19500", "mil-prf-55365", "mil-prf-123"],
    "AEC-Q101": ["aec-q101", "automotive"],
    "AEC-Q200": ["aec-q200"],
    "JANTX": ["jantx", "jantxv"],
    "JANSR": ["jansr", "jans"],
    "Hi-Rel": ["hi-rel", "hirel", "high reliability"],
    "Class S": ["class s", "space"],
    "Class 3": ["class 3", "class iii"],
}


# ----- LLM client helpers --------------------------------------------------


def _make_openai_client(settings) -> Any:
    """Lazily import + construct an OpenAI client. Raises if no key configured."""
    api_key = getattr(settings, "openai_api_key", "") or ""
    if not api_key.strip():
        raise RuntimeError(
            "OpenAI API 키가 설정되지 않았습니다. PRO_OPENAI_API_KEY 환경 변수를 확인하세요."
        )
    from openai import OpenAI  # local import keeps module importable without the package

    return OpenAI(api_key=api_key)


# ----- Intent parsing ------------------------------------------------------


def _empty_filters() -> Dict[str, Any]:
    return {
        "vceo_min": None,
        "ic_min": None,
        "vds_min": None,
        "rds_on_max_mohm": None,
        "resistance_ohm": None,
        "tolerance_max_pct": None,
        "capacitance_uf": None,
        "voltage_v": None,
        "case_size": None,
        "dielectric": None,
        "package_smd_only": False,
        "temp_min_c": None,
        "temp_max_c": None,
        "qual_required": None,
    }


def _coerce_intent(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize the LLM-returned JSON into the canonical schema."""
    device_type = (raw.get("device_type") or "any").lower().strip()
    filters = _empty_filters()
    incoming = raw.get("filters") or {}

    def _to_float(v: Any) -> Optional[float]:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    for k in ("vceo_min", "ic_min", "vds_min", "rds_on_max_mohm",
              "resistance_ohm", "tolerance_max_pct", "capacitance_uf",
              "voltage_v", "temp_min_c", "temp_max_c"):
        filters[k] = _to_float(incoming.get(k))

    case_size = incoming.get("case_size")
    filters["case_size"] = str(case_size).strip() if case_size else None
    dielectric = incoming.get("dielectric")
    filters["dielectric"] = str(dielectric).strip() if dielectric else None
    filters["package_smd_only"] = bool(incoming.get("package_smd_only", False))

    quals = incoming.get("qual_required")
    if isinstance(quals, list) and quals:
        filters["qual_required"] = [str(q).strip() for q in quals if str(q).strip()]
    else:
        filters["qual_required"] = None

    return {
        "device_type": device_type,
        "filters": filters,
        "intent_summary": (raw.get("intent_summary") or "").strip(),
    }


def parse_intent(question: str, openai_client) -> Dict[str, Any]:
    """LLM-driven intent extraction. Returns the canonical schema dict."""
    user_prompt = (
        f"{INTENT_JSON_SCHEMA_HINT}\n\n"
        f"사용자 질문: {question.strip()}\n\n"
        "위 스키마에 맞춰 JSON만 반환하세요."
    )
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=500,
            temperature=0.0,
        )
        content = resp.choices[0].message.content or "{}"
        tokens = resp.usage.total_tokens if resp.usage else 0
        raw = json.loads(content)
    except Exception as exc:  # pragma: no cover - exercised at runtime only
        logger.exception("parse_intent failed")
        raise RuntimeError(f"의도 분석 실패: {exc}") from exc

    intent = _coerce_intent(raw)
    intent["_tokens_used"] = tokens
    return intent


# ----- Heritage helpers ----------------------------------------------------


def _load_heritage(screening_path: str, device_type: str) -> List[Dict[str, Any]]:
    """Pull every heritage row for the requested device_type (small tables)."""
    if device_type not in ("bjt", "mosfet", "any"):
        return []
    conn = sqlite3.connect(screening_path)
    conn.row_factory = sqlite3.Row
    try:
        if device_type == "any":
            cur = conn.execute(
                "SELECT mpn, manufacturer, part_type, qual_level, parameters, "
                "heritage_notes, source_url FROM heritage_parts"
            )
        else:
            cur = conn.execute(
                "SELECT mpn, manufacturer, part_type, qual_level, parameters, "
                "heritage_notes, source_url FROM heritage_parts WHERE part_type=?",
                (device_type,),
            )
        rows = []
        for r in cur.fetchall():
            try:
                params = json.loads(r["parameters"]) if r["parameters"] else {}
            except json.JSONDecodeError:
                params = {}
            rows.append({
                "mpn": r["mpn"],
                "manufacturer": r["manufacturer"],
                "part_type": r["part_type"],
                "qual_level": r["qual_level"],
                "parameters": params,
                "heritage_notes": r["heritage_notes"],
                "source_url": r["source_url"],
            })
        return rows
    finally:
        conn.close()


def _heritage_index(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {r["mpn"].upper(): r for r in rows}


# ----- Filter -> SQL translation ------------------------------------------


def _temp_overlap(spec_temp: Optional[str], req_min: Optional[float],
                   req_max: Optional[float]) -> Optional[float]:
    """Parse Vishay-style "-55 to +125" and compute Tj-style margin (°C above req_max)."""
    if not spec_temp or not isinstance(spec_temp, str):
        return None
    m = re.search(r"(-?\d+)\s*(?:to|~|/|–|-)\s*\+?(-?\d+)", spec_temp)
    if not m:
        return None
    try:
        lo, hi = float(m.group(1)), float(m.group(2))
    except ValueError:
        return None
    if req_max is not None and hi < req_max:
        return None  # caller treats None as fail
    if req_min is not None and lo > req_min:
        return None
    if req_max is None:
        return hi  # raw upper bound used as margin signal
    return hi - req_max


def _qualification_hits(specs: Dict[str, Any], qual_required: Optional[List[str]]) -> List[str]:
    """Return the subset of qual_required that the spec row satisfies."""
    if not qual_required:
        return []
    quals_field = specs.get("qualifications") or []
    hay = " ".join([str(q).lower() for q in quals_field])
    extras = []
    for k in ("automotive", "qualifications", "rating"):
        v = specs.get(k)
        if v:
            extras.append(str(v).lower())
    hay = " ".join([hay, *extras])
    hits = []
    for q in qual_required:
        tokens = KNOWN_QUAL_TOKENS.get(q, [q.lower()])
        if any(tok in hay for tok in tokens):
            hits.append(q)
    return hits


def _passes_resistance(specs: Dict[str, Any], req_ohm: float) -> bool:
    lo_raw = specs.get("res_min_displ") or specs.get("res_min_value")
    hi_raw = specs.get("res_max_displ") or specs.get("res_max_value")
    lo = _parse_resistor_magnitude(lo_raw)
    hi = _parse_resistor_magnitude(hi_raw)
    if lo is None or hi is None:
        return False
    return lo <= req_ohm <= hi


def _parse_resistor_magnitude(v: Any) -> Optional[float]:
    """Vishay ranges are textual: '1', '10K', '10M', '4M99'. Return ohms."""
    if v is None:
        return None
    s = str(v).strip().upper().replace(" ", "")
    if not s:
        return None
    m = re.match(r"^(\d+\.?\d*)([KMRG]?)(\d*)$", s)
    if m:
        base = float(m.group(1))
        suffix = m.group(2)
        tail = m.group(3)
        mult = {"": 1.0, "R": 1.0, "K": 1e3, "M": 1e6, "G": 1e9}[suffix]
        result = base * mult
        if tail:
            try:
                result += float("0." + tail) * mult
            except ValueError:
                pass
        return result
    try:
        return float(s)
    except ValueError:
        return None


def _capacitance_uf(specs: Dict[str, Any]) -> Optional[float]:
    raw = specs.get("capacitance_uF") or specs.get("capacitance_uf")
    if raw is not None:
        s = str(raw).lower().replace(" ", "").replace("µ", "u")
        m = re.match(r"^(\d+\.?\d*)(uf|nf|pf)?", s)
        if m:
            v = float(m.group(1))
            unit = m.group(2) or "uf"
            return {"uf": v, "nf": v / 1e3, "pf": v / 1e6}[unit]
    cap_min_pf = specs.get("cap_min_pf")
    cap_max_pf = specs.get("cap_max_pf")
    try:
        if cap_min_pf is not None and cap_max_pf is not None:
            return (float(cap_min_pf) + float(cap_max_pf)) / 2.0 / 1e6
        if cap_max_pf is not None:
            return float(cap_max_pf) / 1e6
    except (TypeError, ValueError):
        return None
    return None


def _voltage_v(specs: Dict[str, Any]) -> Optional[float]:
    for k in ("voltage_max_V", "voltage_max_v", "voltage_v"):
        v = specs.get(k)
        if v is None:
            continue
        try:
            return float(re.sub(r"[^0-9.]+", "", str(v)) or 0)
        except ValueError:
            continue
    return None


def _is_smd_package(specs: Dict[str, Any]) -> bool:
    mt = str(specs.get("mounting_tech") or specs.get("Mounting Type") or "").lower()
    if "surface" in mt or "smd" in mt or "smt" in mt:
        return True
    pkg = str(specs.get("package") or specs.get("Package Name") or
              specs.get("Infineon Package") or "").upper()
    return any(p.upper() in pkg for p in SMD_PACKAGES)


# ----- Candidate search ----------------------------------------------------


def _query_products(products_path: str, intent: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    device_type = intent["device_type"]
    f = intent["filters"]

    where = []
    params: List[Any] = []
    if device_type and device_type != "any":
        where.append("device_type = ?")
        params.append(device_type)

    # Numeric SQL prefilters - only when applicable to the device_type.
    if device_type in ("bjt", "any") and f.get("vceo_min") is not None:
        where.append("CAST(json_extract(specs, '$.vceo_v') AS REAL) >= ?")
        params.append(float(f["vceo_min"]))
    if device_type in ("bjt", "any") and f.get("ic_min") is not None:
        where.append("CAST(json_extract(specs, '$.ic_max_a') AS REAL) >= ?")
        params.append(float(f["ic_min"]))
    if device_type in ("mosfet", "any") and f.get("vds_min") is not None:
        where.append(
            "(CAST(json_extract(specs, '$.bvdss_v') AS REAL) >= ? OR "
            "CAST(json_extract(specs, '$.vds_v') AS REAL) >= ?)"
        )
        params.extend([float(f["vds_min"]), float(f["vds_min"])])
    if device_type in ("mosfet", "any") and f.get("rds_on_max_mohm") is not None:
        # rds_on stored in ohms; convert mohm threshold
        threshold_ohm = float(f["rds_on_max_mohm"]) / 1000.0
        where.append(
            "(CAST(json_extract(specs, '$.rds_on_ohm') AS REAL) <= ? OR "
            "CAST(json_extract(specs, '$.rds_on_ohm') AS REAL) IS NULL)"
        )
        params.append(threshold_ohm)

    # Tolerance prefilter (resistor)
    if device_type in ("resistor", "any") and f.get("tolerance_max_pct") is not None:
        where.append(
            "(CAST(json_extract(specs, '$.tolerance_displ') AS REAL) <= ? OR "
            "json_extract(specs, '$.tolerance_displ') IS NULL)"
        )
        params.append(float(f["tolerance_max_pct"]))

    # Voltage prefilter (capacitor)
    if device_type in ("capacitor", "any") and f.get("voltage_v") is not None:
        where.append(
            "(CAST(json_extract(specs, '$.voltage_max_V') AS REAL) >= ? OR "
            "CAST(json_extract(specs, '$.voltage_v') AS REAL) >= ? OR "
            "json_extract(specs, '$.voltage_max_V') IS NULL)"
        )
        params.extend([float(f["voltage_v"]), float(f["voltage_v"])])

    # Only consider rows with non-trivial specs
    where.append("length(specs) > 5")

    where_sql = " AND ".join(where) if where else "1=1"
    # Pull a generous superset and let Python rerank/filter.
    sql_limit = max(limit * 10, 200)
    sql = (
        "SELECT external_id, brand, name, device_type, specs, metadata, url "
        f"FROM products WHERE {where_sql} LIMIT ?"
    )
    params.append(sql_limit)

    conn = sqlite3.connect(products_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            specs = json.loads(r["specs"]) if r["specs"] else {}
        except json.JSONDecodeError:
            specs = {}
        try:
            meta = json.loads(r["metadata"]) if r["metadata"] else {}
        except json.JSONDecodeError:
            meta = {}
        out.append({
            "source": "products",
            "mpn": r["external_id"] or r["name"],
            "manufacturer": r["brand"],
            "device_type": r["device_type"],
            "specs": specs,
            "datasheet_url": meta.get("datasheet_url"),
            "url": r["url"],
        })
    return out


def _score_candidate(cand: Dict[str, Any], intent: Dict[str, Any],
                      heritage_idx: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Apply Python-side scoring + soft filters. Mutates cand with score fields."""
    f = intent["filters"]
    specs = cand["specs"]
    breakdown: Dict[str, float] = {}
    reasons: List[str] = []

    # Hard filter satisfaction (50 pts) - assume baseline pass since SQL prefiltered.
    base = 50.0
    breakdown["base_match"] = base

    # Soft filters that the SQL didn't fully cover -----------------------------------

    if intent["device_type"] == "resistor" and f.get("resistance_ohm") is not None:
        if not _passes_resistance(specs, float(f["resistance_ohm"])):
            base -= 30.0
            breakdown["base_match"] = base
            breakdown["resistance_miss"] = -30.0
        else:
            reasons.append(f"저항 범위 만족({f['resistance_ohm']}Ω)")

    if intent["device_type"] == "capacitor" and f.get("capacitance_uf") is not None:
        cap = _capacitance_uf(specs)
        if cap is None:
            breakdown["capacitance_miss"] = -20.0
            base -= 20.0
            breakdown["base_match"] = base
        else:
            req = float(f["capacitance_uf"])
            ratio = cap / req if req else 0.0
            if 0.5 <= ratio <= 2.0:
                reasons.append(f"용량 {cap:.3g}µF (요청 {req}µF)")
            else:
                breakdown["capacitance_miss"] = -15.0
                base -= 15.0
                breakdown["base_match"] = base

    if intent["device_type"] == "capacitor" and f.get("voltage_v") is not None:
        v = _voltage_v(specs)
        if v is not None and v < float(f["voltage_v"]):
            breakdown["voltage_miss"] = -25.0
            base -= 25.0
            breakdown["base_match"] = base

    if f.get("case_size"):
        case_field = str(specs.get("size_device_style") or specs.get("case_size") or "").lower()
        if case_field and f["case_size"].lower() not in case_field:
            breakdown["case_size_miss"] = -10.0
        elif case_field:
            reasons.append(f"케이스 {case_field}")

    if f.get("dielectric"):
        diel = str(specs.get("dielectric") or specs.get("dielectric_concrete") or "").lower()
        if diel and f["dielectric"].lower() not in diel:
            breakdown["dielectric_miss"] = -10.0
        elif diel:
            reasons.append(f"유전체 {diel}")

    if f.get("package_smd_only"):
        if not _is_smd_package(specs):
            breakdown["smd_miss"] = -20.0
            base -= 20.0
            breakdown["base_match"] = base

    # Heritage bonus -----------------------------------------------------------------
    heritage_bonus = 0.0
    heritage_row = heritage_idx.get(str(cand["mpn"]).upper())
    if heritage_row:
        heritage_bonus = 30.0
        ql = str(heritage_row.get("qual_level") or "").upper()
        if ql.startswith("JAN"):
            heritage_bonus = 50.0
        reasons.append(f"Heritage {ql or '등재'}")
    breakdown["heritage_bonus"] = heritage_bonus

    # AEC / qualification bonus ------------------------------------------------------
    aec_bonus = 0.0
    qual_required = f.get("qual_required") or []
    quals_field = specs.get("qualifications") or []
    quals_lower = " ".join(str(x).lower() for x in quals_field) + " " + str(specs.get("automotive") or "").lower()
    if "aec-q101" in quals_lower or "aec-q200" in quals_lower:
        aec_bonus += 20.0
    if qual_required:
        hits = _qualification_hits(specs, qual_required)
        if hits:
            aec_bonus += 10.0 * len(hits)
            reasons.append("인증 " + ",".join(hits))
        else:
            breakdown["qual_miss"] = -20.0
            base -= 20.0
            breakdown["base_match"] = base
    breakdown["aec_bonus"] = aec_bonus

    # Temperature margin bonus -------------------------------------------------------
    temp_margin = 0.0
    req_max = f.get("temp_max_c")
    req_min = f.get("temp_min_c")
    tj = specs.get("tj_max_c")
    if tj is not None:
        try:
            tj_v = float(tj)
            if req_max is None or tj_v >= float(req_max) + 25:
                temp_margin = 10.0
        except (TypeError, ValueError):
            pass
    elif specs.get("temp"):
        margin = _temp_overlap(specs.get("temp"), req_min, req_max)
        if margin is not None and (req_max is None or margin >= 25):
            temp_margin = 10.0
        elif margin is None and req_max is not None:
            breakdown["temp_miss"] = -10.0
    breakdown["temp_margin"] = temp_margin

    # Penalize missing primary spec --------------------------------------------------
    if intent["device_type"] == "bjt" and specs.get("vceo_v") is None:
        breakdown["missing_key_spec"] = -20.0
        base -= 20.0
        breakdown["base_match"] = base
    if intent["device_type"] == "mosfet" and specs.get("bvdss_v") is None and specs.get("vds_v") is None:
        breakdown["missing_key_spec"] = -20.0
        base -= 20.0
        breakdown["base_match"] = base

    score = max(0.0, min(100.0, base + heritage_bonus + aec_bonus + temp_margin))

    explanation_bits: List[str] = []
    if reasons:
        explanation_bits.extend(reasons[:3])
    if not explanation_bits:
        explanation_bits.append("주요 스펙 매칭")

    cand["score"] = round(score, 1)
    cand["score_breakdown"] = {k: round(v, 1) for k, v in breakdown.items()}
    cand["explanation"] = " · ".join(explanation_bits)
    return cand


def search_candidates(filters: Dict[str, Any], device_type: str,
                      db_paths: Dict[str, str], limit: int = 30) -> List[Dict[str, Any]]:
    """Query products + heritage and return ranked candidates (already scored)."""
    intent = {"device_type": device_type or "any", "filters": filters or _empty_filters()}

    heritage_rows = _load_heritage(db_paths["screening"], intent["device_type"])
    heritage_idx = _heritage_index(heritage_rows)

    # 1. Catalog rows
    products_candidates = _query_products(db_paths["products"], intent, limit)
    scored = [_score_candidate(c, intent, heritage_idx) for c in products_candidates]

    # 2. Heritage rows that didn't appear in products (or even if they did, surface them)
    seen_mpns = {c["mpn"].upper() for c in scored}
    for row in heritage_rows:
        if intent["device_type"] not in ("any", row["part_type"]):
            continue
        # Skip if already represented; the products row already gets the heritage bonus.
        if row["mpn"].upper() in seen_mpns:
            continue
        cand = {
            "source": "heritage",
            "mpn": row["mpn"],
            "manufacturer": row["manufacturer"],
            "device_type": row["part_type"],
            "specs": row["parameters"],
            "datasheet_url": row.get("source_url"),
            "url": row.get("source_url"),
        }
        # Heritage rows skip SQL prefilters; check the same logical filters in Python.
        if _heritage_passes_filters(cand["specs"], intent):
            scored.append(_score_candidate(cand, intent, heritage_idx))

    # Sort + truncate
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:limit]


def _heritage_passes_filters(specs: Dict[str, Any], intent: Dict[str, Any]) -> bool:
    f = intent["filters"]
    dt = intent["device_type"]

    def _num(v: Any) -> Optional[float]:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    if dt in ("bjt", "any") and f.get("vceo_min") is not None:
        v = _num(specs.get("vceo_v"))
        if v is None or v < float(f["vceo_min"]):
            return False
    if dt in ("bjt", "any") and f.get("ic_min") is not None:
        v = _num(specs.get("ic_max_a"))
        if v is None or v < float(f["ic_min"]):
            return False
    if dt in ("mosfet", "any") and f.get("vds_min") is not None:
        v = _num(specs.get("bvdss_v")) or _num(specs.get("vds_v"))
        if v is None or v < float(f["vds_min"]):
            return False
    if dt in ("mosfet", "any") and f.get("rds_on_max_mohm") is not None:
        v = _num(specs.get("rds_on_ohm"))
        if v is not None and v > float(f["rds_on_max_mohm"]) / 1000.0:
            return False
    return True


# ----- Answer generation ---------------------------------------------------


def _candidate_brief(cand: Dict[str, Any]) -> Dict[str, Any]:
    """Compact view passed to the answer LLM."""
    specs = cand.get("specs") or {}
    keep_keys = (
        "vceo_v", "ic_max_a", "polarity", "package", "tj_max_c", "qualifications",
        "bvdss_v", "vds_v", "rds_on_ohm", "id_max_a", "vgs_th_v",
        "capacitance_uF", "voltage_max_V", "dielectric", "case_code",
        "res_min_displ", "res_max_displ", "tolerance_displ", "size_device_style",
        "automotive", "temp", "mounting_tech",
    )
    spec_brief = {k: specs[k] for k in keep_keys if k in specs}
    return {
        "source": cand.get("source"),
        "mpn": cand.get("mpn"),
        "manufacturer": cand.get("manufacturer"),
        "device_type": cand.get("device_type"),
        "score": cand.get("score"),
        "explanation": cand.get("explanation"),
        "datasheet_url": cand.get("datasheet_url"),
        "specs": spec_brief,
    }


def generate_answer(question: str, intent: Dict[str, Any],
                    candidates: List[Dict[str, Any]], openai_client) -> Dict[str, Any]:
    """Synthesize a Korean recommendation from the top candidates."""
    top = candidates[:5]
    if not top:
        prompt = (
            f"사용자 질문: {question}\n\n"
            f"파싱된 의도: {intent.get('intent_summary', '')}\n\n"
            "조건에 맞는 부품을 데이터베이스에서 찾지 못했습니다. "
            "사용자에게 한국어로 부드럽게 안내하고, 어떤 조건을 완화하거나 추가 정보를 주면 도움이 될지 제안하세요."
        )
    else:
        briefs = [_candidate_brief(c) for c in top]
        prompt = (
            f"사용자 질문: {question}\n\n"
            f"파싱된 의도: {json.dumps(intent.get('intent_summary') or intent.get('filters'), ensure_ascii=False)}\n\n"
            f"점수 순 후보 부품 (Top {len(top)}):\n"
            f"{json.dumps(briefs, ensure_ascii=False, indent=2)}\n\n"
            "위 후보를 근거로 한국어 추천 답변을 작성하세요. "
            "가격/재고 정보는 현재 미보유라는 점을 짧게 명시하세요."
        )

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=700,
            temperature=0.2,
        )
        text = resp.choices[0].message.content or ""
        tokens = resp.usage.total_tokens if resp.usage else 0
    except Exception as exc:
        logger.exception("generate_answer failed")
        raise RuntimeError(f"답변 생성 실패: {exc}") from exc

    return {"answer": text.strip(), "tokens_used": tokens}


# ----- Top-level entry point ----------------------------------------------


def answer_chat(question: str, db_paths: Dict[str, str], settings) -> Dict[str, Any]:
    """End-to-end: parse intent, search candidates, generate the answer."""
    if not question or not question.strip():
        raise ValueError("question is empty")

    client = _make_openai_client(settings)
    intent = parse_intent(question, client)
    intent_tokens = intent.pop("_tokens_used", 0) or 0

    candidates = search_candidates(
        filters=intent["filters"],
        device_type=intent["device_type"],
        db_paths=db_paths,
        limit=30,
    )

    answer_pkg = generate_answer(question, intent, candidates, client)

    return {
        "intent": intent,
        "candidates": candidates,
        "answer": answer_pkg["answer"],
        "tokens_used": int(intent_tokens) + int(answer_pkg.get("tokens_used", 0)),
    }
