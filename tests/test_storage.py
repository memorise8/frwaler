# -*- coding: utf-8 -*-
"""Unit tests for crawler.storage path utilities."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler.storage import (
    MAX_DOC_ID,
    doc_id_to_path,
    doc_id_to_pdf_path,
    doc_id_to_txt_path,
    doc_id_to_relative_pdf,
    doc_id_to_relative_txt,
    ensure_parent,
)


def _norm(p: Path) -> str:
    return str(p).replace(os.sep, "/")


class TestDocIdToPath(unittest.TestCase):
    """Validates the user-supplied folder layout examples literally."""

    cases = [
        (0,             "data/0000/0000/000000000000.pdf"),
        (9_999,         "data/0000/0000/000000009999.pdf"),
        (10_000,        "data/0000/0001/000000010000.pdf"),
        (19_999,        "data/0000/0001/000000019999.pdf"),
        (20_000,        "data/0000/0002/000000020000.pdf"),
        (100_000_000,   "data/0001/0000/000100000000.pdf"),
        (999_999_990_000, "data/9999/9999/999999990000.pdf"),
        (999_999_999_999, "data/9999/9999/999999999999.pdf"),
    ]

    def test_pdf_paths_match_user_examples(self):
        for doc_id, expected in self.cases:
            with self.subTest(doc_id=doc_id):
                got = _norm(doc_id_to_pdf_path(doc_id, root="data"))
                self.assertEqual(got, expected)

    def test_txt_paths_mirror_pdf_with_extension(self):
        for doc_id, expected_pdf in self.cases:
            with self.subTest(doc_id=doc_id):
                expected_txt = expected_pdf.replace(".pdf", ".txt")
                got = _norm(doc_id_to_txt_path(doc_id, root="data"))
                self.assertEqual(got, expected_txt)

    def test_extension_normalisation(self):
        # Both ".pdf" and "pdf" must produce the same path.
        with_dot = _norm(doc_id_to_path(123, ".pdf", root="data"))
        no_dot = _norm(doc_id_to_path(123, "pdf", root="data"))
        self.assertEqual(with_dot, no_dot)

    def test_relative_helpers(self):
        self.assertEqual(doc_id_to_relative_pdf(0), "data/0000/0000/000000000000.pdf")
        self.assertEqual(doc_id_to_relative_pdf(100_000_000), "data/0001/0000/000100000000.pdf")
        self.assertEqual(doc_id_to_relative_txt(9999), "data/0000/0000/000000009999.txt")

    def test_negative_id_rejected(self):
        with self.assertRaises(ValueError):
            doc_id_to_path(-1, ".pdf", root="data")

    def test_overflow_rejected(self):
        with self.assertRaises(ValueError):
            doc_id_to_path(MAX_DOC_ID + 1, ".pdf", root="data")

    def test_non_integer_rejected(self):
        with self.assertRaises(TypeError):
            doc_id_to_path("123", ".pdf", root="data")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            doc_id_to_path(True, ".pdf", root="data")  # bool must not be accepted

    def test_ensure_parent_creates_directory(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "0001" / "0002" / "000100020003.pdf"
            self.assertFalse(target.parent.exists())
            ensure_parent(target)
            self.assertTrue(target.parent.is_dir())


class TestDetectExtension(unittest.TestCase):
    """Magic-byte sniffer used by ``cmd_download`` for extensionless URLs."""

    def setUp(self):
        from crawler.storage import detect_extension
        self.detect = detect_extension

    def test_pdf_signature(self):
        self.assertEqual(self.detect(b"%PDF-1.7\n%..."), ".pdf")

    def test_hwp_ole_signature(self):
        self.assertEqual(self.detect(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1xx"), ".hwp")

    def test_hwpx_zip_signature(self):
        self.assertEqual(self.detect(b"PK\x03\x04xxxxxx"), ".hwpx")

    def test_html_falls_back_to_bin(self):
        self.assertEqual(self.detect(b"<!DOCTYPE html>"), ".bin")

    def test_empty_input_falls_back_to_bin(self):
        self.assertEqual(self.detect(b""), ".bin")


class TestFollowupBehaviour(unittest.TestCase):
    """Codex-review followup invariants: schema/adapter/queries.

    These exercise the integration points that, if broken, would re-introduce
    the P1/P2 issues from the Codex review (txt_path-too-early, missing
    metadata, papers-only stats).
    """

    def setUp(self):
        # Stub heavy converter deps so importing crawler.* doesn't blow up.
        import sys as _s, types as _t
        class _Stub: pass
        _s.modules.setdefault("bs4", _t.SimpleNamespace(BeautifulSoup=_Stub))
        _s.modules.setdefault(
            "olefile",
            _t.SimpleNamespace(isOleFile=lambda p: False, OleFileIO=_Stub),
        )

        import sqlite3
        from crawler import db as _db
        self._db = _db
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        _db.init_db(self.conn)
        _db.register_site(self.conn, "s", "Site", "http://s")

    def tearDown(self):
        self.conn.close()

    def test_save_document_does_not_set_paths(self):
        from crawler.base_crawler import BaseCrawler

        class _C(BaseCrawler):
            site_id = "s"; site_name = "S"; base_url = "http://s"
            def crawl(self, limit=None): pass

        bc = _C(db_conn=self.conn)
        doc_id = bc._save_document({"site_id": "s", "external_id": "1", "title": "T"})
        row = self.conn.execute(
            "SELECT pdf_path, txt_path FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        self.assertIsNone(row["pdf_path"], "pdf_path must not be set on save")
        self.assertIsNone(row["txt_path"], "txt_path must not be set on save")

    def test_pending_convert_uses_txt_path_null_marker(self):
        # Insert a downloaded document with txt_path NULL → must appear in queue.
        doc_id = self._db.upsert_document(self.conn, {
            "site_id": "s", "external_id": "1", "pdf_url": "http://x",
        })
        self._db.update_document_paths(
            self.conn, doc_id,
            pdf_path="data/0000/0000/000000000001.pdf",
            download_status="downloaded",
        )
        pending = self._db.get_documents_pending_convert(self.conn)
        self.assertEqual(len(pending), 1)

        # After txt_path is filled (e.g. by convert_site_files) it must drop out.
        self._db.update_document_paths(
            self.conn, doc_id, txt_path="data/0000/0000/000000000001.txt",
        )
        self.assertEqual(len(self._db.get_documents_pending_convert(self.conn)), 0)

    def test_paper_to_document_preserves_metadata(self):
        import json
        from crawler import livertree_adapter as la
        paper = {
            "site_id": "nts-taxlaw-pd",
            "external_id": "1",
            "title": "T",
            "category": "고시",
            "doi": "10.x/y",
            "metadata": json.dumps({
                "documentTypeName": "예규",
                "documentNumber": "A-2024-001",
            }),
        }
        doc = la.paper_to_document(paper)
        self.assertIn("metadata", doc)
        md = json.loads(doc["metadata"])
        self.assertEqual(md["documentTypeName"], "예규")
        self.assertEqual(md["documentNumber"], "A-2024-001")
        # category/doi top-level fields are promoted into metadata so the
        # finolaw shim can surface them via parseMetadata().
        self.assertEqual(md["category"], "고시")
        self.assertEqual(md["doi"], "10.x/y")

    def test_get_stats_counts_documents_not_papers(self):
        # After init_db, papers is empty. Insert into documents only.
        self._db.upsert_document(self.conn, {"site_id": "s", "external_id": "1"})
        self._db.upsert_document(self.conn, {"site_id": "s", "external_id": "2"})
        rows = self._db.get_stats(self.conn)
        # rows is per-site; pick our site
        for r in rows:
            if r["id"] == "s":
                self.assertEqual(r["paper_count"], 2)
                return
        self.fail("site 's' not found in get_stats output")

    def test_convert_limit_advances_past_converted_rows(self):
        """Codex re-review P2: ``convert --limit N`` must skip already-
        converted rows up front; otherwise repeated batches stall on the
        lowest IDs."""
        from crawler import storage as _st

        # 5 downloaded rows; rows 1,2 already have txt_path set.
        for i in range(1, 6):
            doc_id = self._db.upsert_document(self.conn, {
                "site_id": "s", "external_id": str(i), "pdf_url": "http://x",
            })
            self._db.update_document_paths(
                self.conn, doc_id,
                pdf_path=_st.doc_id_to_relative(doc_id, ".pdf"),
                download_status="downloaded",
            )
        for i in (1, 2):
            self._db.update_document_paths(
                self.conn, i, txt_path=_st.doc_id_to_relative_txt(i),
            )

        # The pending-convert query must return rows 3, 4, 5 (the unconverted ones).
        # The LIMIT-applied SELECT in convert_site_files mirrors this query.
        rows = self.conn.execute(
            """SELECT id FROM documents
               WHERE download_status = 'downloaded'
                 AND (txt_path IS NULL OR txt_path = '')
               ORDER BY id LIMIT 3"""
        ).fetchall()
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, [3, 4, 5])

    def test_pdf_url_change_resets_download_state(self):
        """Codex round-8 P2: when an attachment URL changes on recrawl,
        the previously downloaded file is stale, so pdf_path/txt_path/
        download_status must be cleared so the next download/convert
        cycle picks up the new URL."""
        doc_id = self._db.upsert_document(self.conn, {
            "site_id": "s", "external_id": "1",
            "pdf_url": "http://host/old.pdf", "title": "T",
        })
        self._db.update_document_paths(
            self.conn, doc_id,
            pdf_path="data/0000/0000/000000000001.pdf",
            txt_path="data/0000/0000/000000000001.txt",
            download_status="downloaded",
            original_filename="old.pdf",
        )
        # Recrawl with a different attachment URL.
        self._db.upsert_document(self.conn, {
            "site_id": "s", "external_id": "1",
            "pdf_url": "http://host/new.pdf", "title": "T",
        })
        row = self.conn.execute(
            "SELECT pdf_url, pdf_path, txt_path, download_status, original_filename "
            "FROM documents WHERE id=?",
            (doc_id,),
        ).fetchone()
        self.assertEqual(row["pdf_url"], "http://host/new.pdf")
        self.assertIsNone(row["pdf_path"])
        self.assertIsNone(row["txt_path"])
        self.assertIsNone(row["download_status"])
        self.assertIsNone(row["original_filename"])

    def test_pdf_url_change_deletes_stale_on_disk_files(self):
        """Codex round-9 P2: when pdf_url changes, the on-disk
        canonical files (.pdf/.txt/.hwp/...) must be removed so
        cmd_download / convert_site_files can't silently reuse them."""
        import tempfile as _tempfile, os as _os
        from crawler import storage as _storage

        # Use a private data root so the test doesn't touch real data/.
        old_root = _os.environ.get("LIVERTREE_DATA_ROOT")
        tmp_root = _tempfile.mkdtemp()
        _os.environ["LIVERTREE_DATA_ROOT"] = tmp_root
        # storage caches DEFAULT_DATA_ROOT at import — reload to pick up env.
        import importlib
        importlib.reload(_storage)
        try:
            doc_id = self._db.upsert_document(self.conn, {
                "site_id": "s", "external_id": "z",
                "pdf_url": "http://h/A.pdf",
            })
            self._db.update_document_paths(
                self.conn, doc_id,
                pdf_path=_storage.doc_id_to_relative(doc_id, ".pdf"),
                txt_path=_storage.doc_id_to_relative_txt(doc_id),
                download_status="downloaded",
            )
            pdf = _storage.doc_id_to_path(doc_id, ".pdf")
            txt = _storage.doc_id_to_txt_path(doc_id)
            _storage.ensure_parent(pdf)
            pdf.write_bytes(b"%PDF-old")
            txt.write_text("old extracted text")
            self.assertTrue(pdf.exists() and txt.exists())

            self._db.upsert_document(self.conn, {
                "site_id": "s", "external_id": "z",
                "pdf_url": "http://h/B.pdf",
            })
            self.assertFalse(pdf.exists(), "stale pdf was not deleted")
            self.assertFalse(txt.exists(), "stale txt was not deleted")
        finally:
            if old_root is None:
                _os.environ.pop("LIVERTREE_DATA_ROOT", None)
            else:
                _os.environ["LIVERTREE_DATA_ROOT"] = old_root
            importlib.reload(_storage)

    def test_pdf_url_unchanged_preserves_download_state(self):
        """Counterpart to the URL-change reset: routine recrawls that
        don't change pdf_url must NOT wipe download state."""
        doc_id = self._db.upsert_document(self.conn, {
            "site_id": "s", "external_id": "2",
            "pdf_url": "http://host/x.pdf", "title": "T",
        })
        self._db.update_document_paths(
            self.conn, doc_id,
            pdf_path="data/0000/0000/000000000002.pdf",
            download_status="downloaded",
        )
        self._db.upsert_document(self.conn, {
            "site_id": "s", "external_id": "2",
            "pdf_url": "http://host/x.pdf", "title": "T updated",
        })
        row = self.conn.execute(
            "SELECT pdf_path, download_status FROM documents WHERE id=?",
            (doc_id,),
        ).fetchone()
        self.assertEqual(row["download_status"], "downloaded")
        self.assertEqual(row["pdf_path"], "data/0000/0000/000000000002.pdf")

    def test_recrawl_preserves_llm_summary_and_metadata(self):
        """Codex re-review P2: a normal recrawl must not wipe LLM-generated
        summary or previously-stored metadata when the new payload omits them."""
        import json as _json
        from crawler import livertree_adapter as _la

        doc_id = self._db.upsert_document(self.conn, {
            "site_id": "s", "external_id": "9", "title": "Original",
            "metadata": _json.dumps({"k": "v"}),
        })
        self._db.update_document_summary(self.conn, doc_id, "LLM summary text")

        # Recrawl: same external_id, no summary, no metadata, title changed.
        paper = {"site_id": "s", "external_id": "9", "title": "Updated"}
        self._db.upsert_document(self.conn, _la.paper_to_document(paper))

        row = self.conn.execute(
            "SELECT summary, title, metadata FROM documents WHERE id=?",
            (doc_id,),
        ).fetchone()
        self.assertEqual(row["summary"], "LLM summary text")
        self.assertEqual(row["metadata"], _json.dumps({"k": "v"}))
        self.assertEqual(row["title"], "Updated")  # non-preserved fields still update


if __name__ == "__main__":
    unittest.main()
