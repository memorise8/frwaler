# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class DbBackendTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("LIBERTREE_DB_BACKEND", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("LIBERTREE_DB_BACKEND", None)
        else:
            os.environ["LIBERTREE_DB_BACKEND"] = self._saved

    def test_default_is_sqlite(self):
        from crawler import db_backend, db_libertree
        self.assertIs(db_backend.get_backend(), db_libertree)

    def test_env_postgres(self):
        from crawler import db_backend, db_pg
        os.environ["LIBERTREE_DB_BACKEND"] = "postgres"
        self.assertIs(db_backend.get_backend(), db_pg)

    def test_explicit_name_overrides_env(self):
        from crawler import db_backend, db_libertree
        os.environ["LIBERTREE_DB_BACKEND"] = "postgres"
        self.assertIs(db_backend.get_backend("sqlite"), db_libertree)

    def test_unknown_raises(self):
        from crawler import db_backend
        with self.assertRaises(ValueError):
            db_backend.get_backend("mysql")


if __name__ == "__main__":
    unittest.main()
