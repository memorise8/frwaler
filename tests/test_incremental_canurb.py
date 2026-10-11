import unittest
from delivery.scripts.review_canurb_incremental import order_evidence, read
from crawler.sites import CRAWLERS


class CanurbOrderTests(unittest.TestCase):
    def test_first_last_ids_cannot_hide_interior_reversals(self):
        records = [{'post_id': str(i), 'link_type': 'detail_html'} for i in (10313, 2120, 2697, 431)]
        evidence = order_evidence(records)
        self.assertEqual(evidence['inferred_order'], ('arbitrary', 'order_ambiguous'))
        self.assertEqual(evidence['proof']['date']['missing'], 4)
        self.assertEqual(evidence['descending_id_inversions'], [{'index': 1, 'before': '2120', 'after': '2697'}])

    def test_linked_cards_are_included_in_scope(self):
        records = [{'post_id': str(i), 'link_type': kind} for i, kind in
                   ((5, 'detail_html'), (2, 'direct_pdf'), (4, 'external_publication'))]
        self.assertEqual(order_evidence(records)['link_types'],
                         {'detail_html': 1, 'direct_pdf': 1, 'external_publication': 1})

    def test_new_date_markup_invalidates_missing_date_review(self):
        c = CRAWLERS['canurb-org-publications'](None, delay=0)
        self.addCleanup(c._session.close)
        raw = b'<article class="publications post-1"><a href="/publications/item/"><h4>Publication</h4></a><time datetime="2026-10-12">Date</time></article>'
        with self.assertRaisesRegex(ValueError, 'date markup'):
            read(c, raw)


if __name__ == '__main__':
    unittest.main()
