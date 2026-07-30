from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.db_libertree import init_db
from crawler.translation_schema import (
    TranslationSchemaError,
    canonical_db_path,
    create_snapshot,
    migrate_translation_schema,
    preflight_canonical_db,
    schema_fingerprint,
    validate_translation_schema,
)


def _fixture_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    init_db(conn)
    conn.execute(
        "INSERT INTO sites(site_id, site_name, site_url) VALUES ('test', 'Test', 'https://test.invalid')"
    )
    conn.execute(
        "INSERT INTO documents(site_id, meta_url, title, abstract) VALUES ('test', 'https://test.invalid/1', 'source title', 'source abstract')"
    )
    conn.commit()
    return conn


def _documents_digest(conn: sqlite3.Connection) -> str:
    columns = [row[1] for row in conn.execute("PRAGMA table_info(documents)")]
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM documents ORDER BY seq_id"
    ).fetchall()
    return hashlib.sha256(repr((columns, rows)).encode()).hexdigest()


class TestTranslationSchema(unittest.TestCase):
    def test_migrate_translation_schema_on_fresh_fixture_preserves_documents_and_fts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            conn = _fixture_db(Path(temporary_directory) / "fresh.db")
            before_digest = _documents_digest(conn)
            before_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            conn.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(title)")
            conn.commit()
            receipt = migrate_translation_schema(conn)
            self.assertTrue(receipt.applied)
            self.assertEqual(_documents_digest(conn), before_digest)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], before_count)
            self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='documents_fts'").fetchone())
            validate_translation_schema(conn)
            self.assertGreaterEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            conn.close()

    def test_migrate_translation_schema_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            conn = _fixture_db(Path(temporary_directory) / "repeat.db")
            migrate_translation_schema(conn)
            first_fingerprint = schema_fingerprint(conn)
            first_version = conn.execute("PRAGMA user_version").fetchone()[0]
            receipt = migrate_translation_schema(conn)
            self.assertFalse(receipt.applied)
            self.assertEqual(schema_fingerprint(conn), first_fingerprint)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], first_version)
            conn.close()

    def test_migrate_translation_schema_rolls_back_interrupted_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            conn = _fixture_db(Path(temporary_directory) / "interrupted.db")
            before_digest = _documents_digest(conn)
            with self.assertRaisesRegex(RuntimeError, "injected interruption"):
                migrate_translation_schema(conn, fail_after_step=1)
            self.assertEqual(_documents_digest(conn), before_digest)
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_translations'").fetchone())
            conn.close()

    def test_snapshot_restore_remains_compatible_with_translation_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            conn = _fixture_db(directory / "snapshot-source.db")
            migrate_translation_schema(conn)
            snapshot = create_snapshot(conn, directory / "snapshot.db")
            conn.close()
            restored = sqlite3.connect(snapshot)
            validate_translation_schema(restored)
            self.assertEqual(restored.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
            restored.close()

    def test_migrate_translation_schema_rejects_missing_documents_or_conflicting_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            conn = sqlite3.connect(Path(temporary_directory) / "wrong-schema.db")
            conn.execute("CREATE TABLE document_translations (translation_id TEXT PRIMARY KEY)")
            conn.commit()
            with self.assertRaisesRegex(TranslationSchemaError, "documents"):
                migrate_translation_schema(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 1)
            conn.close()

    def test_preflight_rejects_papers_and_symlink_drift_and_never_writes(self) -> None:
        root = canonical_db_path().parents[2]
        papers = root / "data" / "papers.db"
        compatibility_link = root / "data" / "libertree.db"
        with self.assertRaisesRegex(TranslationSchemaError, "canonical"):
            preflight_canonical_db(papers)
        with self.assertRaisesRegex(TranslationSchemaError, "symlink"):
            preflight_canonical_db(compatibility_link)

    def test_migrate_translation_schema_reports_busy_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "busy.db"
            writer = _fixture_db(path)
            writer.execute("BEGIN IMMEDIATE")
            contender = sqlite3.connect(path, timeout=0.01)
            with self.assertRaisesRegex(TranslationSchemaError, "busy"):
                migrate_translation_schema(contender)
            writer.rollback()
            contender.close()
            writer.close()
