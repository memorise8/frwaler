from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final


CANONICAL_DB_PATH: Final = Path(__file__).resolve().parent.parent / "libertree-app" / "data" / "libertree.db"
TRANSLATION_SCHEMA_USER_VERSION: Final = 1
TRANSLATION_STATES: Final = ("pending", "running", "completed", "failed", "skipped")
JOB_STATES: Final = ("created", "running", "completed", "failed", "cancelled")
ITEM_STATES: Final = ("pending", "running", "completed", "failed", "skipped")


class TranslationSchemaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MigrationReceipt:
    applied: bool
    user_version: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PreflightReceipt:
    document_count: int
    integrity_check: str
    user_version: int
    schema_fingerprint: str


TABLE_DDLS: Final = (
    """
    CREATE TABLE document_translations (
        translation_id INTEGER PRIMARY KEY,
        seq_id INTEGER NOT NULL REFERENCES documents(seq_id) ON DELETE RESTRICT,
        source_field TEXT NOT NULL CHECK(source_field IN ('title', 'description')),
        target_locale TEXT NOT NULL CHECK(target_locale GLOB '[a-z][a-z]-[A-Z][A-Z]'),
        source_fingerprint TEXT NOT NULL CHECK(length(source_fingerprint) = 64),
        model_version TEXT NOT NULL CHECK(length(model_version) BETWEEN 1 AND 128),
        prompt_version TEXT NOT NULL CHECK(length(prompt_version) BETWEEN 1 AND 128),
        state TEXT NOT NULL CHECK(state IN ('pending', 'running', 'completed', 'failed', 'skipped')),
        translation_text TEXT,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        last_error_code TEXT CHECK(last_error_code IS NULL OR (length(last_error_code) BETWEEN 1 AND 64 AND last_error_code GLOB '[a-z0-9_]*')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        claimed_at TEXT,
        completed_at TEXT,
        CHECK((state != 'completed') OR translation_text IS NOT NULL),
        CHECK((state != 'failed') OR last_error_code IS NOT NULL),
        UNIQUE(seq_id, source_field, target_locale, source_fingerprint, model_version, prompt_version)
    )
    """,
    """
    CREATE TABLE document_translation_jobs (
        job_id TEXT PRIMARY KEY CHECK(length(job_id) BETWEEN 1 AND 64),
        target_locale TEXT NOT NULL CHECK(target_locale GLOB '[a-z][a-z]-[A-Z][A-Z]'),
        model_version TEXT NOT NULL CHECK(length(model_version) BETWEEN 1 AND 128),
        prompt_version TEXT NOT NULL CHECK(length(prompt_version) BETWEEN 1 AND 128),
        source_manifest_fingerprint TEXT NOT NULL CHECK(length(source_manifest_fingerprint) = 64),
        state TEXT NOT NULL CHECK(state IN ('created', 'running', 'completed', 'failed', 'cancelled')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        last_error_code TEXT CHECK(last_error_code IS NULL OR (length(last_error_code) BETWEEN 1 AND 64 AND last_error_code GLOB '[a-z0-9_]*')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        started_at TEXT,
        completed_at TEXT,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE document_translation_job_items (
        job_id TEXT NOT NULL REFERENCES document_translation_jobs(job_id) ON DELETE RESTRICT,
        translation_id INTEGER NOT NULL REFERENCES document_translations(translation_id) ON DELETE RESTRICT,
        seq_id INTEGER NOT NULL REFERENCES documents(seq_id) ON DELETE RESTRICT,
        source_field TEXT NOT NULL CHECK(source_field IN ('title', 'description')),
        target_locale TEXT NOT NULL CHECK(target_locale GLOB '[a-z][a-z]-[A-Z][A-Z]'),
        source_fingerprint TEXT NOT NULL CHECK(length(source_fingerprint) = 64),
        model_version TEXT NOT NULL CHECK(length(model_version) BETWEEN 1 AND 128),
        prompt_version TEXT NOT NULL CHECK(length(prompt_version) BETWEEN 1 AND 128),
        state TEXT NOT NULL CHECK(state IN ('pending', 'running', 'completed', 'failed', 'skipped')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        last_error_code TEXT CHECK(last_error_code IS NULL OR (length(last_error_code) BETWEEN 1 AND 64 AND last_error_code GLOB '[a-z0-9_]*')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        claimed_at TEXT,
        completed_at TEXT,
        PRIMARY KEY(job_id, translation_id),
        UNIQUE(job_id, seq_id, source_field, target_locale, source_fingerprint, model_version, prompt_version)
    )
    """,
)

INDEX_DDLS: Final = (
    "CREATE INDEX idx_document_translations_current ON document_translations(seq_id, source_field, target_locale, state)",
    "CREATE INDEX idx_document_translations_state ON document_translations(state, updated_at)",
    "CREATE INDEX idx_translation_jobs_state ON document_translation_jobs(state, updated_at)",
    "CREATE INDEX idx_translation_job_items_claim ON document_translation_job_items(job_id, state, seq_id)",
    "CREATE INDEX idx_translation_job_items_translation ON document_translation_job_items(translation_id)",
)

EXPECTED_TABLE_COLUMNS: Final = {
    "document_translations": frozenset({"translation_id", "seq_id", "source_field", "target_locale", "source_fingerprint", "model_version", "prompt_version", "state", "translation_text", "attempts", "last_error_code", "created_at", "updated_at", "claimed_at", "completed_at"}),
    "document_translation_jobs": frozenset({"job_id", "target_locale", "model_version", "prompt_version", "source_manifest_fingerprint", "state", "attempts", "last_error_code", "created_at", "started_at", "completed_at", "updated_at"}),
    "document_translation_job_items": frozenset({"job_id", "translation_id", "seq_id", "source_field", "target_locale", "source_fingerprint", "model_version", "prompt_version", "state", "attempts", "last_error_code", "created_at", "updated_at", "claimed_at", "completed_at"}),
}

EXPECTED_INDEXES: Final = frozenset({
    "idx_document_translations_current",
    "idx_document_translations_state",
    "idx_translation_jobs_state",
    "idx_translation_job_items_claim",
    "idx_translation_job_items_translation",
})

REQUIRED_TABLE_SQL: Final = {
    "document_translations": (
        "check(statein('pending','running','completed','failed','skipped'))",
        "unique(seq_id,source_field,target_locale,source_fingerprint,model_version,prompt_version)",
        "referencesdocuments(seq_id)ondeleterestrict",
    ),
    "document_translation_jobs": (
        "check(statein('created','running','completed','failed','cancelled'))",
        "check(length(source_manifest_fingerprint)=64)",
    ),
    "document_translation_job_items": (
        "check(statein('pending','running','completed','failed','skipped'))",
        "unique(job_id,seq_id,source_field,target_locale,source_fingerprint,model_version,prompt_version)",
        "referencesdocument_translations(translation_id)ondeleterestrict",
    ),
}


def canonical_db_path() -> Path:
    return CANONICAL_DB_PATH


def _require_documents_table(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    if "seq_id" not in columns:
        raise TranslationSchemaError("documents table with seq_id is required")


def _existing_translation_objects(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE name IN (?, ?, ?)",
        tuple(EXPECTED_TABLE_COLUMNS),
    )
    return {str(row[0]) for row in rows}


def validate_translation_schema(conn: sqlite3.Connection) -> None:
    _require_documents_table(conn)
    existing_tables = _existing_translation_objects(conn)
    expected_tables = set(EXPECTED_TABLE_COLUMNS)
    if existing_tables != expected_tables:
        raise TranslationSchemaError("translation schema tables are incomplete or missing")
    for table_name, expected_columns in EXPECTED_TABLE_COLUMNS.items():
        actual_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
        if actual_columns != expected_columns:
            raise TranslationSchemaError(f"translation schema mismatch for {table_name}")
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        if row is None or row[0] is None:
            raise TranslationSchemaError(f"translation schema SQL is unavailable for {table_name}")
        table_sql = "".join(str(row[0]).lower().split())
        if not all(required_sql in table_sql for required_sql in REQUIRED_TABLE_SQL[table_name]):
            raise TranslationSchemaError(f"translation schema constraints mismatch for {table_name}")
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    if not EXPECTED_INDEXES.issubset(indexes):
        raise TranslationSchemaError("translation schema indexes are incomplete")


def schema_fingerprint(conn: sqlite3.Connection) -> str:
    names = tuple(sorted((*EXPECTED_TABLE_COLUMNS.keys(), *EXPECTED_INDEXES)))
    placeholders = ", ".join("?" for _ in names)
    rows = conn.execute(
        f"SELECT type, name, sql FROM sqlite_master WHERE name IN ({placeholders}) ORDER BY type, name",
        names,
    ).fetchall()
    payload = "\n".join(f"{row[0]}:{row[1]}:{row[2] or ''}" for row in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def migrate_translation_schema(conn: sqlite3.Connection, *, fail_after_step: int | None = None) -> MigrationReceipt:
    _require_documents_table(conn)
    existing_tables = _existing_translation_objects(conn)
    if existing_tables:
        validate_translation_schema(conn)
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        return MigrationReceipt(False, user_version, schema_fingerprint(conn))
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as error:
        if "locked" in str(error).lower() or "busy" in str(error).lower():
            raise TranslationSchemaError("database is busy") from error
        raise
    try:
        for step, ddl in enumerate((*TABLE_DDLS, *INDEX_DDLS), start=1):
            conn.execute(ddl)
            if fail_after_step == step:
                raise RuntimeError("injected interruption")
        current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        target_version = max(current_version, TRANSLATION_SCHEMA_USER_VERSION)
        conn.execute(f"PRAGMA user_version={target_version}")
        validate_translation_schema(conn)
        fingerprint = schema_fingerprint(conn)
        conn.commit()
        return MigrationReceipt(True, target_version, fingerprint)
    except (RuntimeError, sqlite3.DatabaseError):
        conn.rollback()
        raise


def create_snapshot(conn: sqlite3.Connection, snapshot_path: Path) -> Path:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(snapshot_path) as snapshot:
        conn.backup(snapshot)
    return snapshot_path


def _reject_symlink_path(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise TranslationSchemaError("symlink path is not accepted for canonical migration")
        current = current.parent


def preflight_canonical_db(path: Path | str | None = None) -> PreflightReceipt:
    candidate = CANONICAL_DB_PATH if path is None else Path(path)
    _reject_symlink_path(candidate)
    expected = os.path.abspath(CANONICAL_DB_PATH)
    if os.path.abspath(candidate) != expected:
        raise TranslationSchemaError("only the canonical libertree.db path is accepted")
    if candidate.name == "papers.db" or not candidate.is_file():
        raise TranslationSchemaError("canonical database is unavailable")
    connection_uri = f"file:{candidate}?mode=ro&immutable=1"
    with sqlite3.connect(connection_uri, uri=True) as conn:
        _require_documents_table(conn)
        integrity_check = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity_check != "ok":
            raise TranslationSchemaError("canonical database integrity check failed")
        document_count = int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        return PreflightReceipt(document_count, integrity_check, user_version, schema_fingerprint(conn))
