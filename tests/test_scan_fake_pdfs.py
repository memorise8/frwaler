# -*- coding: utf-8 -*-
import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import scan_fake_pdfs
from scripts.scan_fake_pdfs import classify_blob, is_fake, reset_pdf_row


class TestIsFake(unittest.TestCase):
    def _blob(self, content: bytes) -> Path:
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        f.write(content); f.close()
        return Path(f.name)

    def test_html_is_fake(self):
        p = self._blob(b"<html><body>login required</body></html>")
        self.assertTrue(is_fake(classify_blob(p), p.stat().st_size))

    def test_small_unknown_is_fake(self):
        p = self._blob(b"\r\n\r\n\r\nsome error text")  # kedi 실사례 패턴
        self.assertTrue(is_fake(classify_blob(p), p.stat().st_size))

    def test_https_text_is_fake(self):
        p = self._blob(b"https://redirected.example.org/real.pdf")
        self.assertTrue(is_fake(classify_blob(p), p.stat().st_size))

    def test_real_pdf_not_fake(self):
        p = self._blob(b"%PDF-1.7 rest-of-file")
        self.assertFalse(is_fake(classify_blob(p), p.stat().st_size))

    def test_hwp_ole_not_fake(self):
        p = self._blob(b"\xd0\xcf\x11\xe0" + b"\x00" * 100)  # OLE(HWP) 정상 바이너리
        self.assertFalse(is_fake(classify_blob(p), p.stat().st_size))

    def test_large_unknown_not_fake(self):
        p = self._blob(b"\x00" * 20480)  # 20KB unknown — 보수적으로 유지
        self.assertFalse(is_fake(classify_blob(p), p.stat().st_size))


class TestResetIncludesText(unittest.TestCase):
    def test_reset_clears_text_extracted(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE documents (seq_id INTEGER PRIMARY KEY,"
                     " pdf_downloaded INT, pdf_size_bytes INT, pdf_sha256 TEXT,"
                     " text_extracted INT)")
        conn.execute("INSERT INTO documents VALUES (1, 1, 999, 'abc', 1)")
        reset_pdf_row(conn, 1)
        row = conn.execute("SELECT pdf_downloaded, pdf_size_bytes, pdf_sha256,"
                           " text_extracted FROM documents WHERE seq_id=1").fetchone()
        self.assertEqual(row, (0, 0, "", 0))


class TestScannerThreadedReset(unittest.TestCase):
    """Regression guard for the thread-local write-connection fix.

    Scanner.process_row() used to write via the main-thread ``self.conn``
    from ThreadPoolExecutor workers, which sqlite3 rejects ("SQLite objects
    created in a thread can only be used in that same thread"). The fix
    routes writes through ``Scanner._get_write_conn()``, a thread-local
    connection opened lazily per worker thread. This test drives a real
    multi-worker reset over a real (non-memory) sqlite file and asserts
    every row actually gets reset — it would fail (rows left un-reset,
    per-row thread errors) if someone reverts process_row() to write via
    self.conn.
    """

    NUM_ROWS = 12

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.db_path = self.tmp_path / "test.db"
        self.blob_root = self.tmp_path / "libertree"
        self.blob_root.mkdir(parents=True, exist_ok=True)

        # Real file-backed DB (not :memory:) — worker threads each need
        # their own connection to the *same* database file.
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            """
            CREATE TABLE documents (
                seq_id         INTEGER PRIMARY KEY,
                collected_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                site_id        TEXT,
                post_number    TEXT,
                meta_url       TEXT,
                pdf_downloaded INT,
                pdf_size_bytes INT,
                pdf_sha256     TEXT,
                text_extracted INT
            )
            """
        )
        for seq_id in range(1, self.NUM_ROWS + 1):
            conn.execute(
                "INSERT INTO documents "
                "(seq_id, site_id, pdf_downloaded, pdf_size_bytes, pdf_sha256, text_extracted) "
                "VALUES (?, ?, 1, 999, 'deadbeef', 1)",
                (seq_id, f"site-{seq_id}"),
            )
        conn.commit()
        conn.close()

        # blob_path_for() uses the module-level BLOB_ROOT constant; point
        # it at our temp dir instead of chdir'ing the whole test process.
        self._blob_root_patch = mock.patch.object(
            scan_fake_pdfs, "BLOB_ROOT", self.blob_root
        )
        self._blob_root_patch.start()

        for seq_id in range(1, self.NUM_ROWS + 1):
            blob_path = scan_fake_pdfs.blob_path_for(seq_id)
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            blob_path.write_bytes(b"<html>err</html>")

    def tearDown(self):
        self._blob_root_patch.stop()
        self.tmpdir.cleanup()

    def test_threaded_reset_resets_all_rows_and_deletes_blobs(self):
        scanner = scan_fake_pdfs.Scanner(
            db_path=self.db_path, reset=True, delete_blob=True, workers=4
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            scanner.run()

        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute(
            "SELECT pdf_downloaded, pdf_size_bytes, pdf_sha256, text_extracted "
            "FROM documents ORDER BY seq_id"
        ).fetchall()
        conn.close()

        self.assertEqual(len(rows), self.NUM_ROWS)
        for row in rows:
            self.assertEqual(row, (0, 0, "", 0))

        for seq_id in range(1, self.NUM_ROWS + 1):
            blob_path = scan_fake_pdfs.blob_path_for(seq_id)
            self.assertFalse(blob_path.exists())


class TestScannerBlobRootGuard(unittest.TestCase):
    """Regression guard for M1: a wrong-cwd --reset run must not silently
    wipe every row. Scanner.run() should raise RuntimeError before touching
    the DB if BLOB_ROOT does not exist (belt-and-suspenders alongside the
    CLI pre-flight check in main()).
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        # Deliberately do NOT create a "libertree" dir here.
        self.db_path = self.tmp_path / "test.db"

        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            """
            CREATE TABLE documents (
                seq_id         INTEGER PRIMARY KEY,
                site_id        TEXT,
                pdf_downloaded INT,
                pdf_size_bytes INT,
                pdf_sha256     TEXT,
                text_extracted INT
            )
            """
        )
        conn.execute(
            "INSERT INTO documents VALUES (1, 'site-1', 1, 999, 'deadbeef', 1)"
        )
        conn.commit()
        conn.close()

        # Point BLOB_ROOT at a path inside the temp dir that we never create,
        # simulating a wrong-cwd invocation (no "libertree/" directory).
        self.missing_blob_root = self.tmp_path / "libertree"
        self._blob_root_patch = mock.patch.object(
            scan_fake_pdfs, "BLOB_ROOT", self.missing_blob_root
        )
        self._blob_root_patch.start()

    def tearDown(self):
        self._blob_root_patch.stop()
        self.tmpdir.cleanup()

    def test_reset_without_blob_root_raises(self):
        scanner = scan_fake_pdfs.Scanner(db_path=self.db_path, reset=True)
        with self.assertRaises(RuntimeError):
            scanner.run()

        # Row must be untouched -- the guard must fire before any DB write.
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT pdf_downloaded, pdf_size_bytes, pdf_sha256, text_extracted "
            "FROM documents WHERE seq_id=1"
        ).fetchone()
        conn.close()
        self.assertEqual(row, (1, 999, "deadbeef", 1))

    def test_delete_blob_without_blob_root_raises(self):
        scanner = scan_fake_pdfs.Scanner(db_path=self.db_path, delete_blob=True)
        with self.assertRaises(RuntimeError):
            scanner.run()


if __name__ == "__main__":
    unittest.main()
