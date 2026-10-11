import unittest
from delivery.scripts.review_bmleh_incremental import advisory_order


class BmlehOrderTests(unittest.TestCase):
    def test_month_precision_is_not_invented_day_and_inversion_is_retained(self):
        r=advisory_order([{'trailing':'Expertise June 2020'}, {'trailing':'Report December 2020'},
                          {'trailing':'Version dated 8 December 2023'}])
        self.assertEqual((r['month_precision'],r['day_precision']),(2,1))
        self.assertEqual(r['month_order_inversions'][0]['before_period'],(2020,6))
        self.assertEqual(r['month_order_inversions'][0]['after_period'],(2020,12))

    def test_unknown_or_partial_label_does_not_become_precise_date(self):
        r=advisory_order([{'trailing':'Spring 2024'},{'trailing':'2023'}])
        self.assertEqual(r['unknown_precision'],2);self.assertEqual(r['day_precision'],0)

if __name__=='__main__':unittest.main()
