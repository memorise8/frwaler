"""Unit tests for pro_server.services.factor_kb."""
import pytest

from pro_server.services.factor_kb import load_factors, get_factor


@pytest.mark.usefixtures("seed_test_db")
class TestFactorKb:
    def test_load_factors_returns_ten(self):
        factors = load_factors("bjt")
        assert len(factors) == 10

    def test_weights_sum_to_one(self):
        factors = load_factors("bjt")
        total = sum(f.weight for f in factors)
        assert abs(total - 1.0) < 0.001

    def test_get_factor_icbo_exists(self):
        f = get_factor("ICBO_leakage", "bjt")
        assert f is not None
        assert f.factor_name == "ICBO_leakage"

    def test_get_factor_nonexistent_returns_none(self):
        f = get_factor("nonexistent_factor_xyz", "bjt")
        assert f is None

    def test_factors_have_valid_directions(self):
        valid = {"lower_is_better", "higher_is_better", "margin", "categorical", "boolean"}
        factors = load_factors("bjt")
        for f in factors:
            assert f.direction in valid, f"unexpected direction '{f.direction}' for {f.factor_name}"
