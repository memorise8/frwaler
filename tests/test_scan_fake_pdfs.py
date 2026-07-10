# -*- coding: utf-8 -*-
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


if __name__ == "__main__":
    unittest.main()
