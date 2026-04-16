"""Integration tests for the BJT screening FastAPI router.

Uses FastAPI TestClient (httpx). Requires:
  - fastapi
  - httpx
  - pytest

Both DB fixtures (seed_test_db, license_db) are session-scoped and
provide a seeded screening DB and a valid 'TEST-KEY-123' license key.
"""
import pytest

# Guard: skip entire module if fastapi/httpx not installed
fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402 — after importorskip


@pytest.fixture(scope="module")
def client(seed_test_db, license_db):
    """Build a TestClient wired to a seeded, licensed test DB."""
    # Wire auth to the test license DB before building the app
    import pro_server.auth as _auth
    _auth.LICENSE_DB = license_db

    from pro_server.main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


LICENSE_HEADER = {"x-license-key": "TEST-KEY-123"}
BAD_UUID = "00000000-0000-0000-0000-000000000000"

# First known MPN from the heritage seed (22 rows, first is JANSR2N2222AUB)
KNOWN_MPN = "JANSR2N2222AUB"


class TestScreenBjt:
    def test_post_known_mpn_200(self, client):
        resp = client.post(
            "/pro/api/screen-bjt",
            data={"mpn": KNOWN_MPN},
            headers=LICENSE_HEADER,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] is not None
        assert len(body["heritage_matches"]) > 0

    def test_post_known_mpn_heritage_match_contains_input(self, client):
        resp = client.post(
            "/pro/api/screen-bjt",
            data={"mpn": KNOWN_MPN},
            headers=LICENSE_HEADER,
        )
        assert resp.status_code == 200
        mpns = [m["mpn"].upper() for m in resp.json()["heritage_matches"]]
        assert any(KNOWN_MPN.upper() in m or m in KNOWN_MPN.upper() for m in mpns)

    def test_post_no_file_no_mpn_400(self, client):
        resp = client.post(
            "/pro/api/screen-bjt",
            data={},
            headers=LICENSE_HEADER,
        )
        assert resp.status_code == 400

    def test_post_missing_license_header_401_or_422(self, client):
        resp = client.post(
            "/pro/api/screen-bjt",
            data={"mpn": KNOWN_MPN},
        )
        # FastAPI returns 422 when a required Header param is missing,
        # or 401 if the dependency raises HTTPException first.
        assert resp.status_code in {401, 422}

    def test_post_unknown_mpn_404(self, client):
        resp = client.post(
            "/pro/api/screen-bjt",
            data={"mpn": "COMPLETELY_UNKNOWN_PART_XYZ_999"},
            headers=LICENSE_HEADER,
        )
        assert resp.status_code == 404


class TestGetScreening:
    def test_get_bad_uuid_404(self, client):
        resp = client.get(
            f"/pro/api/screen-bjt/{BAD_UUID}",
            headers=LICENSE_HEADER,
        )
        assert resp.status_code == 404


class TestFactors:
    def test_get_factors_200_at_least_10(self, client):
        resp = client.get("/pro/api/factors", headers=LICENSE_HEADER)
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) >= 10

    def test_get_factors_missing_license_401_or_422(self, client):
        resp = client.get("/pro/api/factors")
        assert resp.status_code in {401, 422}
