"""Unit tests for pro_server.services.heritage_db."""
import pytest

from pro_server.services.heritage_db import (
    list_heritage,
    get_vectors,
    qual_level_score,
    VECTOR_KEYS,
)


@pytest.mark.usefixtures("seed_test_db")
class TestHeritageDb:
    def test_list_heritage_returns_at_least_20(self):
        # The seed file has 22 rows
        rows = list_heritage("bjt")
        assert len(rows) >= 20

    def test_get_vectors_lengths_match(self):
        rows, vectors = get_vectors("bjt")
        assert len(rows) == len(vectors)

    def test_vector_keys_length(self):
        _, vectors = get_vectors("bjt")
        assert all(len(v) == len(VECTOR_KEYS) for v in vectors)

    def test_heritage_rows_have_mpn(self):
        rows = list_heritage("bjt")
        for r in rows:
            assert "mpn" in r and r["mpn"]


class TestQualLevelScore:
    def test_jans_is_1(self):
        assert qual_level_score("JANS") == 1.0

    def test_cots_upscreened(self):
        assert qual_level_score("COTS-upscreened") == 0.6

    def test_none_returns_midpoint(self):
        score = qual_level_score(None)
        assert 0.4 <= score <= 0.6

    def test_unknown_string_returns_midpoint(self):
        score = qual_level_score("TOTALLY_UNKNOWN_QUAL")
        assert 0.4 <= score <= 0.6

    def test_jantxv(self):
        assert qual_level_score("JANTXV") == 0.9

    def test_jantx(self):
        assert qual_level_score("JANTX") == 0.85
