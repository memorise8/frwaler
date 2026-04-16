"""Unit tests for pro_server.services.normalizer."""
import pytest

from pro_server.services.normalizer import parse_value, normalize_bjt_params


# ---------------------------------------------------------------------------
# parse_value
# ---------------------------------------------------------------------------

def test_parse_na():
    v, u, n = parse_value("100 nA")
    assert abs(v - 1e-7) < 1e-20
    assert u == "nA"
    assert n is None


def test_parse_negative_volts():
    v, u, n = parse_value("-5.0 V")
    assert v == -5.0
    assert u == "V"
    assert n is None


def test_parse_pulsed_note():
    v, u, n = parse_value("1A (pulsed 10A)")
    assert v == 1.0
    assert u == "A"
    assert n is not None and "pulsed" in n


def test_parse_range_hyphen():
    v, u, n = parse_value("40-200")
    assert v == 40.0
    assert n is not None and "range" in n


def test_parse_range_endash():
    v, u, n = parse_value("40\u2013200")   # en-dash
    assert v == 40.0
    assert n is not None and "range" in n


def test_parse_min_prefix():
    v, u, n = parse_value("min 40")
    assert v == 40.0
    assert n == "min"


def test_parse_max_prefix():
    v, u, n = parse_value("max 200")
    assert v == 200.0
    assert n == "max"


def test_parse_degree_symbol():
    v, u, n = parse_value("125°C")
    assert v == 125.0
    assert u == "C"


def test_parse_space_celsius():
    v, u, n = parse_value("125 C")
    assert v == 125.0
    assert u == "C"


def test_parse_none():
    v, u, n = parse_value(None)
    assert v is None
    assert u is None
    assert n is None


def test_parse_empty_string():
    v, u, n = parse_value("")
    assert v is None
    assert u is None
    assert n is None


def test_parse_numeric_float():
    v, u, n = parse_value(3.3)
    assert v == 3.3
    assert u is None


# ---------------------------------------------------------------------------
# normalize_bjt_params
# ---------------------------------------------------------------------------

def test_normalize_vceo_volts():
    out = normalize_bjt_params({"Vceo": "40V"})
    assert abs(out["vceo_v"] - 40.0) < 1e-9


def test_normalize_hfe_min_unitless():
    out = normalize_bjt_params({"hfe(min)": "100"})
    assert out["hfe_min"] == 100.0


def test_normalize_icbo_nanoamp():
    out = normalize_bjt_params({"ICBO": "1nA"})
    assert abs(out["icbo_a_at_vcb"] - 1e-9) < 1e-20


def test_normalize_combined():
    raw = {"Vceo": "40V", "hfe(min)": "100", "ICBO": "1nA"}
    out = normalize_bjt_params(raw)
    assert abs(out["vceo_v"] - 40.0) < 1e-9
    assert out["hfe_min"] == 100.0
    assert abs(out["icbo_a_at_vcb"] - 1e-9) < 1e-20


def test_normalize_unknown_key_ignored():
    out = normalize_bjt_params({"totally_unknown_key": "999"})
    assert out == {}


def test_normalize_polarity_uppercased():
    out = normalize_bjt_params({"polarity": "npn"})
    assert out["polarity"] == "NPN"


def test_normalize_package_stripped():
    out = normalize_bjt_params({"package": " TO-92 "})
    assert out["package"] == "TO-92"
