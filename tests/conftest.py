"""Shared fixtures for BJT screening tests.

Sets PRO_SCREENING_DB_PATH and PRO_LICENSE_DB_PATH to per-session temp
files BEFORE any pro_server imports happen, so pydantic_settings picks
up the overrides.
"""
import json
import os
import sqlite3
import tempfile

import pytest

# Auto-load project .env into os.environ so tests pick up GEMINI_KEY etc.
# (does not override already-set env vars — test fixtures below take priority).
try:
    from dotenv import load_dotenv
    _repo_root = os.path.dirname(os.path.dirname(__file__))
    load_dotenv(os.path.join(_repo_root, ".env"), override=False)
except ImportError:
    pass  # python-dotenv is optional

# ---------------------------------------------------------------------------
# Env vars must be set before pro_server is imported anywhere.
# We use a module-level tmp dir so the path is stable for the whole session.
# ---------------------------------------------------------------------------
_SESSION_TMPDIR = tempfile.mkdtemp(prefix="bjt_test_")
_SCREENING_DB = os.path.join(_SESSION_TMPDIR, "screening.db")
_LICENSE_DB = os.path.join(_SESSION_TMPDIR, "licenses.db")

os.environ["PRO_SCREENING_DB_PATH"] = _SCREENING_DB
os.environ["PRO_LICENSE_DB_PATH"] = _LICENSE_DB

# Now safe to import pro_server modules
from pro_server.migrations import run_migrations, seed_if_empty  # noqa: E402
from pro_server.auth import init_license_db  # noqa: E402

# Paths to the real seed files shipped with the project
_REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
_FACTORS_JSON = os.path.join(_REPO_ROOT, "pro_server", "data", "bjt_factors.json")
_HERITAGE_JSON = os.path.join(_REPO_ROOT, "pro_server", "data", "heritage_bjt_seed.json")


@pytest.fixture(scope="session")
def seed_test_db(tmp_path_factory):
    """Run migrations and seed factors + heritage into the temp screening DB."""
    run_migrations(_SCREENING_DB)
    seed_if_empty(_SCREENING_DB, _FACTORS_JSON, _HERITAGE_JSON)
    # Invalidate heritage_db in-process cache so it re-reads the seeded DB
    from pro_server.services import heritage_db
    heritage_db._CACHE["count"] = -1
    return _SCREENING_DB


@pytest.fixture(scope="session")
def license_db(tmp_path_factory):
    """Create a licenses.db with one active test key and wire auth."""
    init_license_db(_LICENSE_DB)
    conn = sqlite3.connect(_LICENSE_DB)
    conn.execute(
        "INSERT OR IGNORE INTO licenses (key, owner, plan, active) VALUES (?, ?, ?, ?)",
        ("TEST-KEY-123", "Test User", "pro", 1),
    )
    conn.commit()
    conn.close()
    return _LICENSE_DB
