import unittest
from delivery.scripts.review_bmlv_incremental import date_precision, order_evidence, parse_table


class BmlvOrderTests(unittest.TestCase):
    def test_date_precision_is_preserved_and_invalid_dates_are_unknown(self):
        for raw, expected in [('05.06.2008', 'day'), ('06.2008', 'month'), ('2025', 'year'),
                              ('', 'unknown'), ('0000-00-01', 'unknown'), ('31.02.2025', 'unknown'),
                              ('13.2025', 'unknown'), ('0000', 'unknown')]:
            self.assertEqual(date_precision(raw), expected)

    def test_missing_tail_date_and_id_inversion_are_not_hidden_by_endpoint_comparison(self):
        rows = [{'id': '912', 'date_raw': '18.02.2019'}, {'id': '22', 'date_raw': '18.05.2001'},
                {'id': '760', 'date_raw': ''}]
        r = order_evidence(rows)
        self.assertEqual(r['unknown_date_ids'], ['760'])
        self.assertEqual(r['numeric_id_descending_inversions'], [{'before': '22', 'after': '760'}])
        self.assertEqual(rows[-1]['date_raw'], '')

    def test_unrecognized_table_row_fails_closed(self):
        with self.assertRaises(ValueError):
            parse_table(b'<table class="publikationen"><tr><td>Changed source markup</td></tr></table>')


if __name__ == '__main__':
    unittest.main()
