"""Hybrid rule + k-NN scoring engine for BJT space-grade screening.

Implements the algorithm from plans/keen-honking-ripple.md:
  overall = alpha * rule_score + (1 - alpha) * knn_score
  where rule_score is a weighted mean of per-factor 0..1 scores derived from
  piecewise-linear threshold mapping, and knn_score is a similarity-weighted
  average of heritage qual-level scores.

stdlib + typing only (no numpy).
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

from ..schemas import (
    BjtParameters,
    MosfetParameters,
    FactorOut,
    FactorScore,
    HeritageMatch,
    RiskFlag,
    ScreeningReport,
)
from .heritage_db import qual_level_score

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# Built-in factor_name → BjtParameters attribute name. Single source of truth
# so the JSON seed doesn't have to carry param_key in the DB schema.
FACTOR_NAME_TO_PARAM_KEY: Dict[str, str] = {
    "ICBO_leakage": "icbo_a_at_vcb",
    "hFE_margin": "hfe_min",
    "BVceo_headroom": "vceo_v",
    "power_derating_margin": "pd_w",
    "tj_thermal_margin": "tj_max_c",
    "ft_headroom": "ft_hz",
    "package_hermeticity": "package",
    "Ic_margin": "ic_max_a",
    "Vebo_ESD_margin": "vebo_v",
    "polarity_complement_availability": "polarity",
    # MOSFET factors
    "Vgs_th_shift": "vgs_th_v",
    "BVDSS_headroom": "bvdss_v",
    "Rds_on_margin": "rds_on_ohm",
    "IDSS_leakage": "idss_a",
    "gate_oxide_thickness": "gate_oxide",
    "Qg_margin": "qg_c",
    "Id_margin": "id_max_a",
}

# Design references for 'margin' direction — ratio = value / ref.
DESIGN_REFERENCES: Dict[str, Dict[str, Any]] = {
    "hFE_margin": {"ref": 40.0, "desc": "nominal hFE design target"},
    "power_derating_margin": {"ref": 0.5, "desc": "50% derating factor (W)"},
    "Ic_margin": {"ref": 0.1, "desc": "typical switching current A"},
    # MOSFET design references
    "Vgs_th_shift": {"ref": 1.0, "desc": "1V enhancement threshold reference"},
    "Rds_on_margin": {"ref": 0.5, "desc": "0.5Ω design reference"},
    "Id_margin": {"ref": 1.0, "desc": "1A design reference"},
}

ALPHA = 0.6
CONFIDENCE_HAIRCUT = 0.85
EPS = 1e-9


# --------------------------------------------------------------------------- #
# Rule mapping
# --------------------------------------------------------------------------- #

def _piecewise(v: float, excellent: float, good: float, poor: float,
               higher_better: bool) -> float:
    """Piecewise-linear between (poor, 0.3), (good, 0.7), (excellent, 1.0).
    For lower_is_better direction thresholds are reversed in magnitude
    (excellent < good < poor)."""
    if higher_better:
        if v >= excellent:
            return 1.0
        if v >= good:
            # map [good, excellent] -> [0.7, 1.0]
            span = max(excellent - good, EPS)
            return 0.7 + 0.3 * (v - good) / span
        if v >= poor:
            span = max(good - poor, EPS)
            return 0.3 + 0.4 * (v - poor) / span
        # below poor: decay toward 0
        if v <= 0:
            return 0.0
        return max(0.0, 0.3 * v / max(poor, EPS))
    # lower_is_better: excellent < good < poor
    if v <= excellent:
        return 1.0
    if v <= good:
        span = max(good - excellent, EPS)
        return 1.0 - 0.3 * (v - excellent) / span
    if v <= poor:
        span = max(poor - good, EPS)
        return 0.7 - 0.4 * (v - good) / span
    # worse than poor
    return max(0.0, 0.3 * poor / max(v, EPS))


def rule_map(value: Optional[float], direction: str,
             thresholds: Dict[str, Any]) -> float:
    """Map a single parameter value to 0..1 score."""
    if value is None:
        return 0.5
    thresholds = thresholds or {}

    if direction == "higher_is_better":
        return _piecewise(
            float(value),
            float(thresholds.get("excellent", 1.0)),
            float(thresholds.get("good", 0.5)),
            float(thresholds.get("poor", 0.1)),
            higher_better=True,
        )
    if direction == "lower_is_better":
        return _piecewise(
            float(value),
            float(thresholds.get("excellent", 0.0)),
            float(thresholds.get("good", 1.0)),
            float(thresholds.get("poor", 10.0)),
            higher_better=False,
        )
    if direction == "margin":
        # value is raw param; ratio depends on factor-specific ref.
        # Caller passes ratio directly when known; here we can't resolve
        # factor_name — rule_map is called with already-prepared ratio.
        return _piecewise(
            float(value),
            float(thresholds.get("excellent", 2.0)),
            float(thresholds.get("good", 1.5)),
            float(thresholds.get("poor", 1.0)),
            higher_better=True,
        )
    if direction == "categorical":
        # value here is a string-keyed lookup but signature is float. The
        # scorer dispatches categorical separately — this branch is a guard.
        return 0.5
    if direction == "boolean":
        return 0.7  # neutral per spec
    return 0.5


def _categorical_score(value: Optional[str], thresholds: Dict[str, Any]) -> float:
    if value is None:
        return 0.5
    if not thresholds:
        return 0.5
    if value in thresholds:
        try:
            return float(thresholds[value])
        except (TypeError, ValueError):
            return 0.5
    # case-insensitive fallback
    lowered = str(value).lower()
    for k, v in thresholds.items():
        if str(k).lower() == lowered:
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.5
    return 0.5


def _margin_ratio(factor_name: str, raw_value: float) -> Optional[float]:
    ref = DESIGN_REFERENCES.get(factor_name, {}).get("ref")
    if ref is None or ref == 0:
        return None
    return float(raw_value) / float(ref)


# --------------------------------------------------------------------------- #
# Vectorization / k-NN
# --------------------------------------------------------------------------- #

def _to_vector(params: BaseModel, keys: List[str]) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    for k in keys:
        val = getattr(params, k, None)
        if isinstance(val, (int, float)):
            out.append(float(val))
        else:
            out.append(None)
    return out


def _z_score_normalize(
    vectors: List[List[Optional[float]]],
) -> Tuple[List[float], List[float]]:
    if not vectors:
        return [], []
    n_cols = len(vectors[0])
    means = [0.0] * n_cols
    stds = [1.0] * n_cols
    for j in range(n_cols):
        col = [row[j] for row in vectors if row[j] is not None]
        if not col:
            means[j] = 0.0
            stds[j] = 1.0
            continue
        m = sum(col) / len(col)
        var = sum((x - m) ** 2 for x in col) / len(col)
        s = math.sqrt(var)
        means[j] = m
        stds[j] = s if s > 1e-9 else 1.0
    return means, stds


def _standardize(vec: List[Optional[float]], means: List[float],
                 stds: List[float]) -> List[float]:
    out: List[float] = []
    for i, v in enumerate(vec):
        if v is None:
            out.append(0.0)  # impute mean → 0 after standardization
        else:
            out.append((v - means[i]) / (stds[i] if stds[i] else 1.0))
    return out


def _cosine_similarity(a: List[Optional[float]], b: List[Optional[float]],
                       means: List[float], stds: List[float]) -> float:
    za = _standardize(a, means, stds)
    zb = _standardize(b, means, stds)
    num = sum(x * y for x, y in zip(za, zb))
    na = math.sqrt(sum(x * x for x in za))
    nb = math.sqrt(sum(y * y for y in zb))
    if na < EPS or nb < EPS:
        return 0.0
    cos = num / (na * nb)
    if cos < 0:
        cos = 0.0
    if cos > 1:
        cos = 1.0
    return cos


def _top_k_neighbors(
    query: List[Optional[float]],
    heritage_rows: List[Dict[str, Any]],
    heritage_vectors: List[List[Optional[float]]],
    means: List[float],
    stds: List[float],
    k: int = 3,
) -> List[Tuple[Dict[str, Any], float]]:
    sims: List[Tuple[Dict[str, Any], float]] = []
    for row, vec in zip(heritage_rows, heritage_vectors):
        sim = _cosine_similarity(query, vec, means, stds)
        sims.append((row, sim))
    sims.sort(key=lambda t: t[1], reverse=True)
    return sims[:k]


# --------------------------------------------------------------------------- #
# Risk flags
# --------------------------------------------------------------------------- #

def _risk_flags(params: BaseModel,
                neighbors: List[Tuple[Dict[str, Any], float]],
                factor_scores: List[FactorScore]) -> List[RiskFlag]:
    flags: List[RiskFlag] = []

    icbo = getattr(params, "icbo_a_at_vcb", None)
    if icbo is not None and icbo > 1e-7:
        flags.append(RiskFlag(
            code="TID_drift_risk",
            severity="warning",
            message=f"ICBO {icbo:.2e} A exceeds 100 nA — "
                    "elevated TID leakage drift risk.",
        ))

    hfe_min = getattr(params, "hfe_min", None)
    if hfe_min is not None and hfe_min < 40:
        flags.append(RiskFlag(
            code="gain_margin_risk",
            severity="warning",
            message=f"hFE_min {hfe_min} below 40 — insufficient gain "
                    "margin for EOL operation.",
        ))

    for attr, label in (
        ("vcbo_v", "Vcbo"),
        ("vceo_v", "Vceo"),
        ("ic_max_a", "Ic_max"),
    ):
        if getattr(params, attr, None) is None and hasattr(params, attr):
            flags.append(RiskFlag(
                code="parametric_gap",
                severity="info",
                message=f"{label} not extracted — parametric gap for "
                        f"{attr}.",
            ))

    if not neighbors or neighbors[0][1] < 0.5:
        flags.append(RiskFlag(
            code="no_heritage_neighbor_within_threshold",
            severity="warning",
            message=("No heritage neighbor with similarity >= 0.5; k-NN "
                     "evidence is weak."),
        ))

    for fs in factor_scores:
        if fs.weight >= 0.1 and fs.coverage == 0:
            flags.append(RiskFlag(
                code="missing_high_weight_param",
                severity="critical",
                message=f"Factor '{fs.name}' (weight {fs.weight:.2f}) has no "
                        "extracted value — high-impact parametric gap.",
            ))

    return flags


# --------------------------------------------------------------------------- #
# Main scoring entry point
# --------------------------------------------------------------------------- #

def _score_single_factor(
    params: BaseModel, factor: FactorOut,
) -> Tuple[float, float, Optional[float]]:
    """Returns (score, coverage, numeric_value_for_report)."""
    param_key = FACTOR_NAME_TO_PARAM_KEY.get(factor.factor_name)
    if not param_key:
        return 0.5, 0.0, None
    raw = getattr(params, param_key, None)
    if raw is None:
        return 0.5, 0.0, None

    thresholds = factor.thresholds or {}
    direction = factor.direction

    if direction == "categorical":
        s = _categorical_score(str(raw), thresholds)
        return s, 1.0, None  # categorical: numeric value not applicable
    if direction == "boolean":
        return 0.7, 1.0, None
    if direction == "margin":
        if not isinstance(raw, (int, float)):
            return 0.5, 0.0, None
        ratio = _margin_ratio(factor.factor_name, float(raw))
        if ratio is None:
            return 0.5, 0.0, float(raw)
        return rule_map(ratio, "margin", thresholds), 1.0, float(raw)
    # numeric directions
    if not isinstance(raw, (int, float)):
        return 0.5, 0.0, None
    return rule_map(float(raw), direction, thresholds), 1.0, float(raw)


def score_report(
    params: BaseModel,
    factors: List[FactorOut],
    heritage_rows: List[Dict[str, Any]],
    heritage_vectors: List[List[Optional[float]]],
    vector_keys: List[str],
    extraction_confidence: float = 1.0,
    input_mpn: Optional[str] = None,
    input_source: str = "pdf",
    report_id: Optional[str] = None,
    tokens_used: int = 0,
) -> ScreeningReport:
    # --- 1) rule-based per-factor scores ---
    factor_scores: List[FactorScore] = []
    for f in factors:
        s, cov, numeric_val = _score_single_factor(params, f)
        factor_scores.append(FactorScore(
            name=f.factor_name,
            score=s,
            weight=f.weight,
            coverage=cov,
            rationale=f.rationale,
            sources=f.sources,
            value=numeric_val,
            direction=f.direction,
        ))

    w_sum = sum(fs.weight for fs in factor_scores)
    if w_sum > 0:
        rule_score = sum(fs.score * fs.weight for fs in factor_scores) / w_sum
    else:
        rule_score = 0.5

    # --- 2) k-NN heritage similarity ---
    neighbors: List[Tuple[Dict[str, Any], float]] = []
    if heritage_vectors:
        means, stds = _z_score_normalize(heritage_vectors)
        q = _to_vector(params, vector_keys)
        neighbors = _top_k_neighbors(q, heritage_rows, heritage_vectors,
                                     means, stds, k=3)

    if neighbors:
        # weight by 1/(1 - sim + eps) — closer neighbors dominate
        weights = [1.0 / (1.0 - sim + EPS) for _, sim in neighbors]
        quals = [qual_level_score(row.get("qual_level")) for row, _ in neighbors]
        wsum = sum(weights)
        knn_score = sum(q * w for q, w in zip(quals, weights)) / wsum if wsum > 0 else 0.5
    else:
        knn_score = 0.5

    # --- 3) hybrid ---
    overall = ALPHA * rule_score + (1.0 - ALPHA) * knn_score
    overall = max(0.0, min(1.0, overall))

    # --- 4) confidence ---
    if factor_scores:
        coverage_mean = sum(fs.coverage for fs in factor_scores) / len(factor_scores)
    else:
        coverage_mean = 0.0
    top_sim = neighbors[0][1] if neighbors else 0.0
    density = min(1.0, len(neighbors) / 3.0) * max(0.0, top_sim)
    confidence = (0.5 * coverage_mean + 0.3 * density
                  + 0.2 * max(0.0, min(1.0, extraction_confidence)))
    confidence *= CONFIDENCE_HAIRCUT
    confidence = max(0.0, min(1.0, confidence))

    # --- 5) status ---
    if overall > 0.75 and confidence > 0.5:
        status = "pass"
    elif overall > 0.55:
        status = "caution"
    else:
        status = "fail"

    # --- 6) heritage matches ---
    heritage_matches = [
        HeritageMatch(
            mpn=row.get("mpn") or "unknown",
            manufacturer=row.get("manufacturer"),
            qual_level=row.get("qual_level"),
            similarity=round(sim, 4),
            source_url=row.get("source_url"),
        )
        for row, sim in neighbors
    ]

    risk_flags = _risk_flags(params, neighbors, factor_scores)

    return ScreeningReport(
        id=report_id or str(uuid.uuid4()),
        input_mpn=input_mpn,
        input_source=input_source,
        parameters=params,
        factor_scores=factor_scores,
        overall_score=round(overall, 4),
        status=status,
        confidence=round(confidence, 4),
        heritage_matches=heritage_matches,
        risk_flags=risk_flags,
        extraction_confidence=extraction_confidence,
        tokens_used=tokens_used,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

def _build_mock_factors() -> List[FactorOut]:
    # Mirrors bjt_factors.json — avoids DB dependency for the self-test.
    return [
        FactorOut(factor_name="ICBO_leakage", part_type="bjt", weight=0.25,
                  direction="lower_is_better", rationale="TID leakage",
                  thresholds={"excellent": 1e-8, "good": 1e-7, "poor": 1e-6}),
        FactorOut(factor_name="hFE_margin", part_type="bjt", weight=0.20,
                  direction="margin", rationale="Gain margin",
                  thresholds={"excellent": 2.0, "good": 1.5, "poor": 1.1}),
        FactorOut(factor_name="BVceo_headroom", part_type="bjt", weight=0.15,
                  direction="higher_is_better", rationale="SEB headroom",
                  thresholds={"excellent": 80, "good": 40, "poor": 20}),
        FactorOut(factor_name="power_derating_margin", part_type="bjt",
                  weight=0.15, direction="margin", rationale="Power derating",
                  thresholds={"excellent": 2.0, "good": 1.5, "poor": 1.0}),
        FactorOut(factor_name="tj_thermal_margin", part_type="bjt", weight=0.10,
                  direction="higher_is_better", rationale="Thermal margin",
                  thresholds={"excellent": 200, "good": 175, "poor": 150}),
        FactorOut(factor_name="ft_headroom", part_type="bjt", weight=0.05,
                  direction="higher_is_better", rationale="ft margin",
                  thresholds={"excellent": 3e8, "good": 1e8, "poor": 3e7}),
        FactorOut(factor_name="package_hermeticity", part_type="bjt", weight=0.05,
                  direction="categorical", rationale="Package",
                  thresholds={"hermetic": 1.0, "metal-can": 0.9,
                              "ceramic": 0.9, "plastic": 0.3}),
        FactorOut(factor_name="Ic_margin", part_type="bjt", weight=0.03,
                  direction="margin", rationale="Ic margin",
                  thresholds={"excellent": 2.0, "good": 1.5, "poor": 1.0}),
        FactorOut(factor_name="Vebo_ESD_margin", part_type="bjt", weight=0.01,
                  direction="higher_is_better", rationale="Vebo ESD",
                  thresholds={"excellent": 7, "good": 5, "poor": 3}),
        FactorOut(factor_name="polarity_complement_availability", part_type="bjt",
                  weight=0.01, direction="boolean", rationale="Complement",
                  thresholds={"has_complement": 1.0, "no_complement": 0.6}),
    ]


if __name__ == "__main__":
    from .heritage_db import VECTOR_KEYS

    factors = _build_mock_factors()

    # --- Scenario 1: ideal BJT, empty heritage ---
    ideal = BjtParameters(
        vceo_v=100.0, vcbo_v=120.0, vebo_v=8.0, ic_max_a=0.25,
        hfe_min=120.0, hfe_max=400.0, icbo_a_at_vcb=5e-9,
        ft_hz=5e8, pd_w=1.5, tj_max_c=200.0, polarity="NPN",
        package="hermetic",
    )
    r1 = score_report(ideal, factors, [], [], VECTOR_KEYS,
                      extraction_confidence=1.0, input_mpn="IDEAL-1")
    print(f"[1] ideal/no-heritage: overall={r1.overall_score} "
          f"status={r1.status} confidence={r1.confidence} "
          f"flags={[f.code for f in r1.risk_flags]}")
    assert r1.overall_score > 0.7, f"expected overall>0.7, got {r1.overall_score}"
    assert r1.status in {"pass", "caution"}, f"status={r1.status}"

    # --- Scenario 2: weak BJT, missing vcbo ---
    weak = BjtParameters(
        vceo_v=15.0, vcbo_v=None, vebo_v=3.0, ic_max_a=0.05,
        hfe_min=20.0, hfe_max=80.0, icbo_a_at_vcb=5e-6,
        ft_hz=2e7, pd_w=0.3, tj_max_c=125.0, polarity="NPN",
        package="plastic",
    )
    r2 = score_report(weak, factors, [], [], VECTOR_KEYS,
                      extraction_confidence=0.6, input_mpn="WEAK-1")
    codes = {f.code for f in r2.risk_flags}
    print(f"[2] weak/no-heritage: overall={r2.overall_score} "
          f"status={r2.status} confidence={r2.confidence} flags={codes}")
    assert r2.overall_score < 0.6, f"expected overall<0.6, got {r2.overall_score}"
    assert r2.status in {"caution", "fail"}, f"status={r2.status}"
    assert "TID_drift_risk" in codes, f"missing TID_drift_risk in {codes}"
    assert "parametric_gap" in codes, f"missing parametric_gap in {codes}"
    assert "gain_margin_risk" in codes, f"missing gain_margin_risk in {codes}"

    # --- Scenario 3: ideal BJT, with heritage (self-match) ---
    heritage_rows = [
        {"mpn": "IDEAL-1", "manufacturer": "MockCo", "qual_level": "JANS",
         "parameters": {"vceo_v": 100.0, "vcbo_v": 120.0, "vebo_v": 8.0,
                        "ic_max_a": 0.25, "hfe_min": 120.0,
                        "icbo_a_at_vcb": 5e-9, "ft_hz": 5e8, "pd_w": 1.5,
                        "tj_max_c": 200.0},
         "source_url": "https://example.com/ideal1"},
        {"mpn": "SIMILAR-1", "manufacturer": "MockCo", "qual_level": "JANTXV",
         "parameters": {"vceo_v": 80.0, "vcbo_v": 100.0, "vebo_v": 7.0,
                        "ic_max_a": 0.2, "hfe_min": 100.0,
                        "icbo_a_at_vcb": 1e-8, "ft_hz": 4e8, "pd_w": 1.2,
                        "tj_max_c": 200.0},
         "source_url": "https://example.com/similar1"},
        {"mpn": "DIFF-1", "manufacturer": "MockCo", "qual_level": "JANTX",
         "parameters": {"vceo_v": 30.0, "vcbo_v": 40.0, "vebo_v": 5.0,
                        "ic_max_a": 0.1, "hfe_min": 60.0,
                        "icbo_a_at_vcb": 5e-7, "ft_hz": 1e8, "pd_w": 0.6,
                        "tj_max_c": 150.0},
         "source_url": "https://example.com/diff1"},
    ]
    heritage_vectors = [
        [row["parameters"].get(k) for k in VECTOR_KEYS]
        for row in heritage_rows
    ]
    r3 = score_report(ideal, factors, heritage_rows, heritage_vectors,
                      VECTOR_KEYS, extraction_confidence=1.0,
                      input_mpn="IDEAL-1")
    print(f"[3] ideal/with-heritage: overall={r3.overall_score} "
          f"status={r3.status} confidence={r3.confidence} "
          f"top={r3.heritage_matches[0].mpn}@{r3.heritage_matches[0].similarity}")
    assert len(r3.heritage_matches) == 3, f"expected 3 matches"
    assert r3.heritage_matches[0].mpn == "IDEAL-1", \
        f"expected self-match first, got {r3.heritage_matches[0].mpn}"
    assert r3.heritage_matches[0].similarity > 0.95, \
        f"expected self-similarity near 1.0, got {r3.heritage_matches[0].similarity}"
    assert r3.confidence > 0.6, f"expected confidence>0.6, got {r3.confidence}"

    print("scorer OK")
