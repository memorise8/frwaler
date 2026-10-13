import unittest

from crawler.document_dates import normalize_date


class FrenchMonthAbbreviationTests(unittest.TestCase):
    def test_cerema_abbreviations_normalize_to_day(self):
        for raw, value in (("02 Aoû 2024", "2024-08-02"), ("27 Avr 2026", "2026-04-27"),
                           ("12 Fév 2025", "2025-02-12"), ("11 Avr 2024", "2024-04-11"),
                           ("17 Aoû 2017", "2017-08-17"), ("05 Janv 2020", "2020-01-05"),
                           ("09 Juil 2021", "2021-07-09"), ("03 Févr 2022", "2022-02-03")):
            self.assertEqual(normalize_date(raw), {
                "value": value, "raw": raw, "status": "normalized", "precision": "day"})

    def test_existing_abbreviations_and_garbage_unchanged(self):
        self.assertEqual(normalize_date("28 Nov 2013")["value"], "2013-11-28")
        self.assertEqual(normalize_date("02 Déc 2019")["value"], "2019-12-02")
        self.assertEqual(normalize_date("02 Xyz 2024")["status"], "unrecognized")
