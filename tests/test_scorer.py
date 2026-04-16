"""Unit tests for pro_server.services.scorer."""
import pytest

from pro_server.schemas import BjtParameters, FactorOut
from pro_server.services.scorer import (
    rule_map,
    score_report,
    FACTOR_NAME_TO_PARAM_KEY,
    CONFIDENCE_HAIRCUT,
)
from pro_server.services.heritage_db import VECTOR_KEYS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_factors():
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
        FactorOut(factor_name="power_derating_margin", part_type="bjt", weight=0.15,
                  direction="margin", rationale="Power derating",
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


# ---------------------------------------------------------------------------
# rule_map — lower_is_better
# ---------------------------------------------------------------------------

def test_rule_map_lower_is_better_excellent():
    score = rule_map(1e-8, "lower_is_better",
                     {"excellent": 1e-8, "good": 1e-7, "poor": 1e-6})
    assert score == 1.0


def test_rule_map_lower_is_better_poor():
    score = rule_map(1e-6, "lower_is_better",
                     {"excellent": 1e-8, "good": 1e-7, "poor": 1e-6})
    # At exactly 'poor' boundary piecewise returns 0.3
    assert abs(score - 0.3) < 0.05


def test_rule_map_lower_is_better_worse_than_poor():
    score = rule_map(1e-4, "lower_is_better",
                     {"excellent": 1e-8, "good": 1e-7, "poor": 1e-6})
    assert score < 0.3


# ---------------------------------------------------------------------------
# rule_map — higher_is_better
# ---------------------------------------------------------------------------

def test_rule_map_higher_is_better_at_excellent():
    score = rule_map(100, "higher_is_better",
                     {"excellent": 80, "good": 50, "poor": 20})
    assert score == 1.0


def test_rule_map_higher_is_better_below_poor():
    score = rule_map(5, "higher_is_better",
                     {"excellent": 80, "good": 50, "poor": 20})
    assert score < 0.3


# ---------------------------------------------------------------------------
# rule_map — margin (ratio already pre-computed by caller)
# ---------------------------------------------------------------------------

def test_rule_map_margin_excellent():
    # ratio 2.0 == excellent threshold
    score = rule_map(2.0, "margin",
                     {"excellent": 2.0, "good": 1.5, "poor": 1.1})
    assert score == 1.0


def test_rule_map_margin_below_poor():
    score = rule_map(0.8, "margin",
                     {"excellent": 2.0, "good": 1.5, "poor": 1.1})
    assert score < 0.3


# ---------------------------------------------------------------------------
# rule_map — None → 0.5
# ---------------------------------------------------------------------------

def test_rule_map_none_value():
    score = rule_map(None, "lower_is_better",
                     {"excellent": 1e-8, "good": 1e-7, "poor": 1e-6})
    assert score == 0.5


# ---------------------------------------------------------------------------
# score_report — ideal BJT, empty heritage
# ---------------------------------------------------------------------------

def test_score_ideal_bjt_no_heritage():
    ideal = BjtParameters(
        vceo_v=100.0, vcbo_v=120.0, vebo_v=8.0, ic_max_a=0.25,
        hfe_min=120.0, hfe_max=400.0, icbo_a_at_vcb=5e-9,
        ft_hz=5e8, pd_w=1.5, tj_max_c=200.0, polarity="NPN",
        package="hermetic",
    )
    report = score_report(ideal, _make_factors(), [], [], VECTOR_KEYS,
                          extraction_confidence=1.0, input_mpn="IDEAL-1")
    assert report.overall_score > 0.7
    assert report.status in {"pass", "caution"}


# ---------------------------------------------------------------------------
# score_report — weak BJT, no heritage
# ---------------------------------------------------------------------------

def test_score_weak_bjt_no_heritage():
    weak = BjtParameters(
        vceo_v=15.0, vcbo_v=None, vebo_v=3.0, ic_max_a=0.05,
        hfe_min=20.0, hfe_max=80.0, icbo_a_at_vcb=5e-6,
        ft_hz=2e7, pd_w=0.3, tj_max_c=125.0, polarity="NPN",
        package="plastic",
    )
    report = score_report(weak, _make_factors(), [], [], VECTOR_KEYS,
                          extraction_confidence=0.6, input_mpn="WEAK-1")
    assert report.overall_score < 0.6
    codes = {f.code for f in report.risk_flags}
    assert "TID_drift_risk" in codes
    assert "gain_margin_risk" in codes
    assert "parametric_gap" in codes


# ---------------------------------------------------------------------------
# score_report — with heritage self-match
# ---------------------------------------------------------------------------

def test_score_with_heritage_self_match():
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
    ideal = BjtParameters(
        vceo_v=100.0, vcbo_v=120.0, vebo_v=8.0, ic_max_a=0.25,
        hfe_min=120.0, hfe_max=400.0, icbo_a_at_vcb=5e-9,
        ft_hz=5e8, pd_w=1.5, tj_max_c=200.0, polarity="NPN",
        package="hermetic",
    )
    report = score_report(ideal, _make_factors(), heritage_rows,
                          heritage_vectors, VECTOR_KEYS,
                          extraction_confidence=1.0, input_mpn="IDEAL-1")
    assert len(report.heritage_matches) == 3
    top = report.heritage_matches[0]
    assert top.mpn == "IDEAL-1"
    assert top.similarity >= 0.99


# ---------------------------------------------------------------------------
# confidence haircut — never exceeds CONFIDENCE_HAIRCUT
# ---------------------------------------------------------------------------

def test_confidence_haircut():
    ideal = BjtParameters(
        vceo_v=100.0, vcbo_v=120.0, vebo_v=8.0, ic_max_a=0.25,
        hfe_min=120.0, hfe_max=400.0, icbo_a_at_vcb=5e-9,
        ft_hz=5e8, pd_w=1.5, tj_max_c=200.0, polarity="NPN",
        package="hermetic",
    )
    heritage_rows = [
        {"mpn": f"PART-{i}", "manufacturer": "MockCo", "qual_level": "JANS",
         "parameters": {"vceo_v": 100.0, "vcbo_v": 120.0, "vebo_v": 8.0,
                        "ic_max_a": 0.25, "hfe_min": 120.0,
                        "icbo_a_at_vcb": 5e-9, "ft_hz": 5e8, "pd_w": 1.5,
                        "tj_max_c": 200.0},
         "source_url": None}
        for i in range(5)
    ]
    heritage_vectors = [
        [row["parameters"].get(k) for k in VECTOR_KEYS]
        for row in heritage_rows
    ]
    report = score_report(ideal, _make_factors(), heritage_rows,
                          heritage_vectors, VECTOR_KEYS,
                          extraction_confidence=1.0)
    assert report.confidence <= CONFIDENCE_HAIRCUT + 1e-9
